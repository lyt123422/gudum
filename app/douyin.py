"""刷抖音·网页版（高自由度遥控）：DrissionPage 接管本机浏览器。

会话模型：浏览器按需打开、常驻（暂停也留着），空闲 20 分钟自动关闭；
自动刷是后台线程，QQ 指令可随时介入（点赞/评论/关注/搜索/截图）。

模拟真人三要素不变：停留随机、滑动距离随机、偶尔回看。
安全限幅：单次 ≤60 分钟、每日 ≤120 分钟；评论只由人工触发（防垃圾）。

注意：抖音网页版是 web-component 播放器（含 shadow DOM），选择器用
多候选 + JS 双路探测；页面改版时先「抖音截图」再修选择器。
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
from pathlib import Path
from urllib.parse import quote

from nonebot.log import logger

ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = ROOT / "data" / "douyin_profile"
SHOT_DIR = ROOT / "data" / "douyin_shots"
DAILY_FILE = ROOT / "data" / "douyin_daily.json"

MAX_SESSION_MIN = 60
MAX_DAILY_MIN = 120
IDLE_CLOSE_MIN = 20     # 无人操控多久后自动关浏览器

_LIKE_SELECTORS = ['xg-icon[data-e2e="video-player-digg"]',
                   '[data-e2e="video-player-digg"]',
                   '.xgplayer-diggs', 'xg-icon.diggs']
_FOLLOW_SELECTORS = ['[data-e2e="video-author-follow"]',
                     'button[data-e2e="video-author-follow"]']
_COMMENT_INPUTS = ['[data-e2e="comment-input"]',
                   'div[contenteditable="true"][data-e2e="comment-input"]']
_COMMENT_OPEN = ['xg-icon[data-e2e="video-player-comment"]',
                 '[data-e2e="video-player-comment"]']
_AUTHOR_LINKS = ['a[data-e2e="video-author-avatar"]',
                 '[data-e2e="video-author"] a',
                 'a[href*="/user/"]']
# 沉浸式播放器的判定标记：URL 带这些才算"在刷视频"（否则是门户宫格页）
_FEED_MARKERS = ("modal_id=", "/video/", "/discover")
_FEED_CARD_SELECTORS = ['[class*="waterfall-videoCardContainer"]',   # 实测：jingxuan 落地页的视频卡
                        '[data-e2e="scroll-list"] li:first-child a',
                        'a[href*="/video/"]']

_state: dict = {"page": None, "browser_lock": threading.Lock(),
                "thread": None, "stop": threading.Event(),
                "pause": threading.Event(), "started_at": 0.0,
                "plan_min": 0, "last_active": 0.0, "like_rate": 0.15,
                "last_error": "", "watchdog": None}


# ---------- 账本与状态 ----------

def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _daily_minutes() -> int:
    try:
        data = json.loads(DAILY_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("date") == _today():   # 评审 P2-4
            return int(data.get("minutes", 0))
    except (OSError, ValueError, TypeError):
        pass
    return 0


def _add_daily(minutes: int) -> None:
    data = {"date": _today(), "minutes": _daily_minutes() + max(0, minutes)}
    try:
        DAILY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = DAILY_FILE.with_suffix(".tmp")   # 评审 P2-4：原子写
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(DAILY_FILE)
    except OSError:
        pass


def _touch() -> None:
    _state["last_active"] = time.time()


def _alive() -> bool:
    return bool(_state["thread"] and _state["thread"].is_alive())


def status() -> dict:
    with _state["browser_lock"]:
        browser_open = _state["page"] is not None
    return {"running": _alive(), "paused": _state["pause"].is_set(),
            "browser_open": browser_open,
            "elapsed_min": round((time.time() - _state["started_at"]) / 60, 1)
            if _alive() else 0,
            "plan_min": _state["plan_min"], "today_min": _daily_minutes(),
            "like_rate": _state["like_rate"],
            "last_error": _state["last_error"]}


# ---------- 浏览器 ----------

def _pick_browser_path() -> str | None:
    """优先 .env 的 CHROME_PATH，再找 Chrome/Edge（含用户级安装路径，评审 P2-5）。"""
    from .envutil import ENV_FILE, load_env
    configured = load_env(ENV_FILE).get("CHROME_PATH", "").strip()
    if configured and Path(configured).exists():
        return configured
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        rf"{local}\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        rf"{local}\Microsoft\Edge\Application\msedge.exe",
    ]
    for path in candidates:
        if path and Path(path).exists():
            return path
    return None


def _page_alive(page) -> bool:
    """DrissionPage 4.x 提供 states.is_alive；异常一律视为已死（评审 P1-2）。"""
    try:
        return bool(page.states.is_alive)
    except Exception:
        return False


def _drop_page() -> None:
    """调用方持锁：先摘引用再强关，page 可能已死所以异常全吞（评审 P1-2）。"""
    page, _state["page"] = _state["page"], None
    if page is not None:
        try:
            page.quit(timeout=3, force=True)
        except Exception:
            try:
                page.quit()
            except Exception:
                pass


def _ensure_page():
    """在调用线程里确保浏览器已打开（browser_lock 由调用方持有）。

    死页对象必须重建：用户手关 Chrome/崩溃后旧对象仍非 None（评审 P1-2）。
    """
    if _state["page"] is not None:
        if _page_alive(_state["page"]):
            return _state["page"]
        _drop_page()
    from DrissionPage import ChromiumOptions, ChromiumPage
    options = ChromiumOptions()
    browser = _pick_browser_path()
    if not browser:
        raise RuntimeError("没找到 Chrome 或 Edge 浏览器")
    options.set_browser_path(browser)
    options.set_user_data_path(str(PROFILE_DIR))
    options.set_argument("--start-maximized")
    options.set_argument("--disable-blink-features=AutomationControlled")
    page = ChromiumPage(options)
    try:   # 评审 P2-6：显式超时，避免任何一步把遥控线程挂死
        page.set.timeouts(base=10, page_load=20, script=10)
    except Exception:
        pass
    try:
        loaded = page.get("https://www.douyin.com/?recommend=1", timeout=20)
    except Exception as exc:
        _state["page"] = page
        _drop_page()
        raise RuntimeError(f"浏览器启动失败：{str(exc)[:120]}") from exc
    if not loaded:
        _state["page"] = page
        _drop_page()
        raise RuntimeError("抖音页面加载失败（网络/风控），已关闭浏览器")
    _state["page"] = page
    _start_watchdog()
    return page


def _close_page() -> None:
    page, _state["page"] = _state["page"], None
    if page is not None:
        try:
            page.quit()
        except Exception:
            pass


def _in_feed(page) -> bool:
    """是否已在沉浸式竖滑播放器里（而不是门户宫格落地页）。"""
    url = page.url or ""
    if any(m in url for m in _FEED_MARKERS):
        return True
    try:
        return bool(page.ele('css:[data-e2e="video-player"]', timeout=0.5))
    except Exception:
        return False


def _enter_feed(page) -> bool:
    """从宫格页点进第一个视频，进入沉浸式播放器（实测：不点进去就只是滚网页）。"""
    for sel in _FEED_CARD_SELECTORS:
        try:
            ele = page.ele(f"css:{sel}", timeout=0.8)
            if not ele:
                continue
            ele.click()
            time.sleep(random.uniform(2, 3.5))
            if _in_feed(page):
                return True
        except Exception:
            continue
    return False


def _wheel(page, dy: int) -> bool:
    """真实滚轮事件切换视频——抖音沉浸流认 wheel，不认 JS 滚动。"""
    try:
        page.actions.scroll(delta_y=dy)
        return True
    except Exception:
        pass
    try:   # 兜底：方向键在沉浸流里同样切视频
        key = "ArrowDown" if dy > 0 else "ArrowUp"
        page.actions.key_down(key)
        page.actions.key_up(key)
        return True
    except Exception:
        return False


def _find_and_click(page, selectors: list[str]) -> tuple[bool, str]:
    """多候选选择器点击；先试普通 DOM，再进 shadow DOM 找 web-component。"""
    for sel in selectors:
        try:
            ele = page.ele(f"css:{sel}", timeout=0.5)
            if ele:
                ele.click()
                return True, sel
        except Exception:
            continue
    # 抖音播放器在 shadow DOM 里：JS 递归穿透查找并点击
    js = """
    const deep = (root, sel) => {
      let el = root.querySelector(sel);
      if (el) return el;
      for (const n of root.querySelectorAll('*')) {
        if (n.shadowRoot) { const f = deep(n.shadowRoot, sel); if (f) return f; }
      } return null; };
    for (const sel of arguments[0]) {
      const el = deep(document, sel);
      if (el) { el.click(); return sel; } }
    return '';
    """
    try:
        hit = page.run_js(js, selectors)
        if hit:
            return True, hit + "(shadow)"
    except Exception:
        pass
    return False, ""


# ---------- 行为库（借鉴成熟养号工具的"散步模式"） ----------

def _browse_comments(page) -> bool:
    """打开评论区翻几眼再关上——真人刷抖音一大半时间在看书。"""
    ok, hit = _find_and_click(page, _COMMENT_OPEN)
    if not ok:
        return False
    time.sleep(random.uniform(1.5, 3))
    for _ in range(random.randint(1, 3)):
        _wheel(page, random.randint(180, 320))   # 评论区是滚轮列表，不是翻页
        time.sleep(random.uniform(1, 2.5))
    # 再点一次关闭（评论按钮是开关），失败按 Esc 兜底
    _find_and_click(page, _COMMENT_OPEN)
    try:   # 评审 P1-3：actions.key 不存在，用 key_down/key_up
        page.actions.key_down("Escape")
        page.actions.key_up("Escape")
    except Exception:
        pass
    time.sleep(random.uniform(0.5, 1))
    return True


def _visit_author(page) -> bool:
    """点进当前作者主页逛几秒作品列表再回来（新标签页要收拾，评审 P2-2）。"""
    for sel in _AUTHOR_LINKS:
        try:
            ele = page.ele(f"css:{sel}", timeout=0.5)
            if not ele:
                continue
            before = page.tabs_count
            ele.click()
            time.sleep(random.uniform(3, 6))
            if page.tabs_count > before:   # 新标签页打开的：关掉多余标签
                page.close_tabs(others=True)
            page.scroll.down(random.randint(200, 500))
            time.sleep(random.uniform(1.5, 3))
            page.back()
            time.sleep(random.uniform(1, 2))
            if "douyin.com" not in (page.url or ""):   # 不在推荐流就送回去
                page.get("https://www.douyin.com/?recommend=1", timeout=20)
            return True
        except Exception:
            continue
    return False


def _scroll_loop(minutes: int) -> None:
    stop = _state["stop"]
    deadline = time.time() + minutes * 60
    try:
        while not stop.is_set() and time.time() < deadline:
            dwell = random.uniform(4, 10) \
                + (random.uniform(8, 14) if random.random() < 0.125 else 0)
            wait_end = time.time() + dwell
            while time.time() < wait_end and not stop.is_set():
                if _state["pause"].is_set():
                    time.sleep(0.5)
                    continue
                time.sleep(min(0.5, max(0.05, wait_end - time.time())))
            if stop.is_set():
                break
            if _state["pause"].is_set():
                continue   # 暂停时不执行"最后一次动作"（评审 P2-1）
            with _state["browser_lock"]:   # 锁只做校验和取引用（评审 P1-2.7）
                page = _state["page"]
                if page is None:
                    break
                if not _page_alive(page):
                    _drop_page()
                    break
            # 动作与睡眠都在锁外：status/stop 不被 5~15 秒睡眠卡住
            try:
                # 实测踩坑：douyin.com 首页是宫格落地页，必须先进沉浸式播放器，
                # 否则"滑动"只是在滚一个普通网页（抽象行为真题）
                if not _in_feed(page) and not _enter_feed(page):
                    page.get("https://www.douyin.com/?recommend=1", timeout=20)
                    continue
                roll = random.random()
                browsed = False
                if roll < 0.08:
                    browsed = _browse_comments(page)    # 8% 翻评论区
                elif roll < 0.13:
                    browsed = _visit_author(page)       # 5% 进作者主页逛逛
                if not browsed:
                    if random.random() < 0.1:           # 偶尔回看上一条
                        _wheel(page, -random.randint(600, 1000))
                        time.sleep(random.uniform(3, 6))
                    _wheel(page, random.randint(600, 1000))   # 滚轮切下一条
                _touch()
                # 连续刷 6~12 分钟后小歇 1~3 分钟（真人节奏，评审对照成熟养号工具）
                if time.time() - _state.get("last_rest", _state["started_at"]) \
                        > random.uniform(360, 720):
                    _state["last_rest"] = time.time()
                    rest = random.uniform(60, 180)
                    logger.info("抖音散步：连续刷了一阵，休息 {:.0f}s", rest)
                    rest_end = time.time() + rest
                    while time.time() < rest_end and not stop.is_set():
                        if _state["pause"].is_set():
                            break
                        time.sleep(1)
                # 自动点赞：按概率给当前这条点个赞（真人也这样）
                if random.random() < _state["like_rate"]:
                    ok, _ = _find_and_click(page, _LIKE_SELECTORS)
                    if ok:
                        time.sleep(random.uniform(0.8, 1.6))
            except Exception as exc:   # noqa: BLE001
                _state["last_error"] = str(exc)[:200]
                with _state["browser_lock"]:
                    if _state["page"] is not None and not _page_alive(_state["page"]):
                        _drop_page()
                break
    except Exception as exc:   # noqa: BLE001
        _state["last_error"] = str(exc)[:200]
    finally:
        _add_daily(int((time.time() - _state["started_at"]) / 60) or 1)


def _start_watchdog() -> None:
    if _state["watchdog"] and _state["watchdog"].is_alive():
        return

    def _watch():
        while True:
            time.sleep(60)
            with _state["browser_lock"]:
                page = _state["page"]
                if page is None:
                    continue
                # 只看活跃时间（评审 P1-2.5）：不再要求自动刷已结束，
                # 否则"暂停 + 挂机"永远关不掉浏览器
                if time.time() - _state["last_active"] > IDLE_CLOSE_MIN * 60:
                    if _page_alive(page):
                        try:
                            page.quit(timeout=3, force=True)
                        except Exception:
                            pass
                    _state["page"] = None

    _state["watchdog"] = threading.Thread(target=_watch, daemon=True)
    _state["watchdog"].start()


# ---------- 对外操作（QQ 指令入口） ----------

def start(minutes: int) -> tuple[bool, str]:
    with _state["browser_lock"]:
        if _state["thread"] and not _state["thread"].is_alive():
            _state["thread"] = None   # 清理已结束的旧线程引用
        if _alive():
            return False, "已经在刷了，发「抖音暂停」或「停止抖音」"
        minutes = max(1, min(minutes, MAX_SESSION_MIN))
        if _daily_minutes() + minutes > MAX_DAILY_MIN:
            left = max(0, MAX_DAILY_MIN - _daily_minutes())
            return False, f"今日上限 {MAX_DAILY_MIN} 分钟，还剩 {left} 分钟"
        try:
            _ensure_page()
        except Exception as exc:   # noqa: BLE001
            _state["last_error"] = str(exc)[:200]
            return False, f"浏览器打开失败：{str(exc)[:120]}"
        _state["stop"].clear()
        _state["pause"].clear()
        _state["started_at"] = time.time()
        _state["plan_min"] = minutes
        _state["last_error"] = ""
        _state["thread"] = threading.Thread(
            target=_scroll_loop, args=(minutes,), daemon=True)
        _state["thread"].start()
        _touch()
        return True, (f"开刷（约 {minutes} 分钟）！浏览器已弹出抖音。"
                      "还没登录过的话：用手机抖音 App 扫一次码，以后全自动。"
                      "想插手随时发：抖音下一个/抖音点赞/抖音评论/抖音截图/抖音帮助")


def stop() -> tuple[bool, str]:
    _state["stop"].set()
    thread = _state["thread"]
    if thread and thread.is_alive():
        thread.join(timeout=5)   # 评审 P1-2.8：等刷视频线程收尾
    _state["thread"] = None
    _state["plan_min"] = 0
    with _state["browser_lock"]:
        had = _state["page"] is not None
        _close_page()
    return True, "已停止并关闭浏览器" if had else "本来就没在刷"


def pause() -> tuple[bool, str]:
    if not _alive():
        return False, "现在没在自动刷（浏览器可能还开着，可直接遥控）"
    _state["pause"].set()
    return True, "已暂停自动滑动（浏览器保留，可遥控或「抖音继续」）"


def resume() -> tuple[bool, str]:
    _state["pause"].clear()
    return True, "继续刷" if _alive() else "自动刷已结束，重新发「刷抖音 分钟」"


def _act(fn):
    """通用遥控入口：确保浏览器在，加锁执行动作。"""
    with _state["browser_lock"]:
        try:
            page = _ensure_page()
        except Exception as exc:   # noqa: BLE001
            return False, f"浏览器打开失败：{str(exc)[:120]}"
        try:
            return fn(page)
        except Exception as exc:   # noqa: BLE001
            _state["last_error"] = str(exc)[:200]
            if not _page_alive(page):   # page 已死：清掉，下次重建（评审 P1-2.4）
                _drop_page()
            return False, f"操作失败：{str(exc)[:120]}"
        finally:
            _touch()


def next_video() -> tuple[bool, str]:
    def _do(page):
        if not _in_feed(page):
            _enter_feed(page)
        _wheel(page, random.randint(600, 1000))
        return True, "下一条"
    return _act(_do)


def like_current() -> tuple[bool, str]:
    def _do(page):
        ok, hit = _find_and_click(page, _LIKE_SELECTORS)
        if not ok:
            return False, "没找到点赞按钮（页面结构可能变了，发「抖音截图」看看）"
        time.sleep(0.3)
        return True, "已点赞 ❤"
    return _act(_do)


def follow_current() -> tuple[bool, str]:
    def _do(page):
        ok, hit = _find_and_click(page, _FOLLOW_SELECTORS)
        if not ok:
            return False, "没找到关注按钮（试「抖音截图」看看当前页面）"
        return True, "已关注当前作者"
    return _act(_do)


def _deep_input(page, selectors: list[str], text: str) -> bool:
    """一次 JS 完成：递归穿透 shadow DOM 找输入框 → focus → 写入 → 派发 input
    事件（评审 P1-3：_find_and_click 拿到的 shadow 命中串不是合法 CSS，不能复用）。"""
    js = """
    const deep = (root, sel) => {
      let el = root.querySelector(sel);
      if (el) return el;
      for (const n of root.querySelectorAll('*')) {
        if (n.shadowRoot) { const f = deep(n.shadowRoot, sel); if (f) return f; }
      } return null; };
    for (const sel of arguments[0]) {
      const el = deep(document, sel);
      if (el) {
        el.focus();
        if (el.isContentEditable) { el.textContent = arguments[1]; }
        else { el.value = arguments[1]; }
        el.dispatchEvent(new InputEvent('input', {bubbles: true}));
        return sel;
      }
    }
    return '';
    """
    try:
        return bool(page.run_js(js, selectors, text))
    except Exception:
        return False


def _press_enter(page) -> None:
    """对当前聚焦元素派发回车（提交按钮选择器不匹配时的兜底）。"""
    js = """
    const el = document.activeElement || document.body;
    for (const type of ['keydown', 'keyup']) {
      el.dispatchEvent(new KeyboardEvent(type, {key: 'Enter', keyCode: 13,
        which: 13, bubbles: true, cancelable: true}));
    }
    """
    try:
        page.run_js(js)
    except Exception:
        pass


def comment(text: str) -> tuple[bool, str]:
    if not text or len(text) > 200:
        return False, "评论内容为空或超过 200 字"

    def _do(page):
        _find_and_click(page, _COMMENT_INPUTS)   # 点开/聚焦（失败交给 deep_input）
        time.sleep(0.5)
        if not _deep_input(page, _COMMENT_INPUTS, text):
            return False, "没找到评论框（试「抖音截图」看看）"
        time.sleep(0.4)
        done, _ = _find_and_click(page, ['[data-e2e="comment-submit"]'])
        if not done:   # 有的版本没有提交按钮，回车提交
            _press_enter(page)
        return True, f"已发出评论：「{text[:40]}」"
    return _act(_do)


def search(keyword: str) -> tuple[bool, str]:
    if not keyword:
        return False, "要搜什么？用法：抖音搜索 关键词"

    def _do(page):
        # 关键词必须 URL 编码，中文/特殊字符直拼会 400（评审 P2-3）
        page.get(f"https://www.douyin.com/search/"
                 f"{quote(keyword.strip()[:50], safe='')}", timeout=20)
        time.sleep(2)
        page.scroll.down(random.randint(300, 600))
        return True, f"已搜索「{keyword}」，正在看搜索结果（发「刷抖音 分钟」可持续刷）"
    return _act(_do)


def screenshot() -> Path | None:
    def _do(page):
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SHOT_DIR / f"dy_{int(time.time())}.png"
        page.get_screenshot(path=str(path), full_page=False)
        for stale in sorted(SHOT_DIR.glob("*.png"))[:-5]:
            try:
                stale.unlink()
            except OSError:
                pass
        return True, path
    ok, result = _act(_do)
    return result if ok and isinstance(result, Path) else None


def set_like_rate(rate: float) -> tuple[bool, str]:
    if not 0 <= rate <= 1:
        return False, "点赞率范围 0~1（0.2 = 每五条赞一条）"
    _state["like_rate"] = rate
    return True, f"自动点赞率已设为 {rate:.0%}"
