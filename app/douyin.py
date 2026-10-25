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
import random
import threading
import time
from pathlib import Path

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
        if data.get("date") == _today():
            return int(data.get("minutes", 0))
    except (OSError, ValueError):
        pass
    return 0


def _add_daily(minutes: int) -> None:
    data = {"date": _today(), "minutes": _daily_minutes() + max(0, minutes)}
    try:
        DAILY_FILE.parent.mkdir(parents=True, exist_ok=True)
        DAILY_FILE.write_text(json.dumps(data), encoding="utf-8")
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
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for path in candidates:
        if Path(path).exists():
            return path
    return None


def _ensure_page():
    """在调用线程里确保浏览器已打开（browser_lock 由调用方持有）。"""
    if _state["page"] is not None:
        return _state["page"]
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
    page.get("https://www.douyin.com/?recommend=1")
    _state["page"] = page
    _start_watchdog()
    time.sleep(2)
    return page


def _close_page() -> None:
    page, _state["page"] = _state["page"], None
    if page is not None:
        try:
            page.quit()
        except Exception:
            pass


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
        page.scroll.down(random.randint(150, 350))
        time.sleep(random.uniform(1, 2.5))
    # 再点一次关闭（评论按钮是开关），失败按 Esc 兜底
    _find_and_click(page, _COMMENT_OPEN)
    try:
        page.actions.key("Escape")
    except Exception:
        pass
    time.sleep(random.uniform(0.5, 1))
    return True


def _visit_author(page) -> bool:
    """点进当前作者主页逛几秒作品列表再回来。"""
    for sel in _AUTHOR_LINKS:
        try:
            ele = page.ele(f"css:{sel}", timeout=0.5)
            if not ele:
                continue
            ele.click()
            time.sleep(random.uniform(3, 6))
            page.scroll.down(random.randint(200, 500))
            time.sleep(random.uniform(1.5, 3))
            page.back()
            time.sleep(random.uniform(1, 2))
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
            with _state["browser_lock"]:
                page = _state["page"]
                if page is None:
                    break
                # 行为抽签（借鉴散步模式）：大概率刷下一条，小概率翻评论区/进主页
                roll = random.random()
                browsed = False
                if roll < 0.08:
                    browsed = _browse_comments(page)    # 8% 翻评论区
                elif roll < 0.13:
                    browsed = _visit_author(page)       # 5% 进作者主页逛逛
                if not browsed:
                    if random.random() < 0.1:           # 偶尔回看上一条
                        page.scroll.up(random.randint(400, 800))
                        time.sleep(random.uniform(3, 6))
                    page.scroll.down(random.randint(350, 800))
                _touch()
                # 自动点赞：按概率给当前这条点个赞（真人也这样）
                if random.random() < _state["like_rate"]:
                    ok, _ = _find_and_click(page, _LIKE_SELECTORS)
                    if ok:
                        time.sleep(random.uniform(0.8, 1.6))
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
                if _state["page"] is None:
                    continue
                if time.time() - _state["last_active"] > IDLE_CLOSE_MIN * 60 \
                        and not _alive():
                    _close_page()   # 没在刷又没人操控：关掉省资源

    _state["watchdog"] = threading.Thread(target=_watch, daemon=True)
    _state["watchdog"].start()


# ---------- 对外操作（QQ 指令入口） ----------

def start(minutes: int) -> tuple[bool, str]:
    with _state["browser_lock"]:
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
            return False, f"操作失败：{str(exc)[:120]}"
        finally:
            _touch()


def next_video() -> tuple[bool, str]:
    def _do(page):
        page.scroll.down(random.randint(350, 800))
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


def comment(text: str) -> tuple[bool, str]:
    if not text or len(text) > 200:
        return False, "评论内容为空或超过 200 字"

    def _do(page):
        ok, hit = _find_and_click(page, _COMMENT_INPUTS)
        if not ok:
            return False, "没找到评论框（试「抖音截图」看看）"
        time.sleep(0.5)
        page.ele(f"css:{hit}").input(text)
        time.sleep(0.4)
        done, _ = _find_and_click(page, ['[data-e2e="comment-submit"]'])
        if not done:   # 有的版本没有提交按钮，回车提交
            page.ele(f"css:{hit}").input("\n")
        return True, f"已发出评论：「{text[:40]}」"
    return _act(_do)


def search(keyword: str) -> tuple[bool, str]:
    if not keyword:
        return False, "要搜什么？用法：抖音搜索 关键词"

    def _do(page):
        page.get(f"https://www.douyin.com/search/{keyword}")
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
