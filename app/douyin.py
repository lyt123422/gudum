"""抖音真机助手：QQ 遥控安卓手机真刷抖音（真滑动/真声音/真点赞）。

与已移除的"网页版"本质不同：手机上真的在播放视频（有声音），
我们只做触控模拟（uiautomator2 无障碍级）+ AI 看手机屏幕两帧画面做决策。

前置条件（一次性）：
1. 安卓手机打开「开发者选项 → USB 调试」，USB 连电脑并允许调试授权
2. 手机装好抖音 App
3. 控制台插件市场安装「抖音真机」（自动装 uiautomator2）；adb 缺失会自动下载
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import random
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import Path

from nonebot.log import logger

ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = ROOT / "tools" / "adb"
DAILY_FILE = ROOT / "data" / "douyin_daily.json"

DOUYIN_PACKAGE = "com.ss.android.ugc.aweme"

MAX_SESSION_MIN = 60
MAX_DAILY_MIN = 120

_state: dict = {"device": None, "dev_lock": threading.Lock(),
                "connect_lock": threading.Lock(),
                "thread": None, "stop": threading.Event(),
                "pause": threading.Event(), "started_at": 0.0,
                "plan_min": 0, "last_active": 0.0,
                "ai_failures": 0, "gen": 0, "last_error": "", "watchdog": None}


# ---------- adb / 设备 ----------

def _adb_candidates() -> list[str]:
    exe = "adb.exe" if shutil.which("where") else "adb"
    local = Path.home() / "AppData" / "Local"
    return [
        str(TOOLS_DIR / "platform-tools" / exe),
        str(local / "Android" / "Sdk" / "platform-tools" / exe),
        r"C:\platform-tools\adb.exe",
    ]


def adb_path() -> str | None:
    """已有 adb 就用（PATH 或常见位置），没有返回 None。"""
    found = shutil.which("adb")
    if found:
        return found
    for c in _adb_candidates():
        if Path(c).exists():
            return c
    return None


def download_adb() -> tuple[bool, str]:
    """从 Google 官方源下载 platform-tools（约 5MB）解压到 tools/adb/。"""
    import urllib.request
    url = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
    zip_path = TOOLS_DIR / "platform-tools.zip"
    try:
        TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        logger.info("下载 adb platform-tools（约 5MB）...")
        with urllib.request.urlopen(url, timeout=60) as resp, \
                zip_path.open("wb") as fh:
            fh.write(resp.read())
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(TOOLS_DIR)
        zip_path.unlink(missing_ok=True)
        target = TOOLS_DIR / "platform-tools" / "adb.exe"
        if target.exists():
            return True, str(target)
        return False, "解压后没找到 adb.exe"
    except Exception as exc:   # noqa: BLE001
        return False, f"adb 下载失败（可手动下载 {url} 解压到 {TOOLS_DIR}）: {str(exc)[:120]}"


def ensure_adb() -> tuple[bool, str]:
    got = adb_path()
    if got:
        return True, got
    ok, result = download_adb()
    return ok, result


def _adb(*args: str, timeout: int = 15) -> tuple[bool, str]:
    adb = adb_path()
    if not adb:
        return False, "adb 不可用"
    try:
        result = subprocess.run([adb, *args], capture_output=True, text=True,
                                errors="replace", timeout=timeout)
        return result.returncode == 0, (result.stdout or "") + (result.stderr or "")
    except Exception as exc:   # noqa: BLE001
        return False, str(exc)[:150]


def device_status() -> dict:
    """连接手机用的诊断信息：adb/设备/抖音安装 各环节一目了然。"""
    adb = adb_path()
    out = {"adb": bool(adb), "adb_path": adb or "",
           "device": "", "douyin": False, "agent": False}
    if not adb:
        return out
    ok, text = _adb("devices")
    if ok:
        for line in text.splitlines()[1:]:
            line = line.strip()
            if line and "\tdevice" in line:
                out["device"] = line.split("\t")[0]
                break
            if "unauthorized" in line:
                out["device"] = line.split("\t")[0] + "（未授权：手机上点允许）"
    if out["device"] and "未授权" not in out["device"]:
        ok, text = _adb("-s", out["device"], "shell",
                        "pm list packages com.ss.android.ugc.aweme")
        out["douyin"] = ok and "aweme" in text
        try:
            import uiautomator2 as u2   # noqa: PLC0415
            d = u2.connect(out["device"])
            out["agent"] = bool(d.info.get("displayWidth"))
        except Exception as exc:   # noqa: BLE001
            out["agent_error"] = str(exc)[:120]
    return out


def connect() -> tuple[bool, str]:
    """诊断+连接：adb → 授权 → u2 代理 → 抖音安装检查，缺啥说啥。"""
    if _alive():   # 评审 v3 P1-3：会话进行中禁止重连（防新旧双 Device 同操手机）
        return False, "正在刷视频，先发「停止抖音」再重连手机"
    with _state["connect_lock"]:
        st = device_status()
        if not st["adb"]:
            ok, result = download_adb()
            if not ok:
                return False, result
            st = device_status()
        if not st["device"]:
            return False, ("没看到手机：检查 USB 线、手机上「USB 调试」已开、"
                           "并点击手机弹出的「允许 USB 调试」授权")
        if "未授权" in st["device"]:
            return False, "手机弹了授权框：点「允许 USB 调试」（可勾选一律允许）"
        if not st["douyin"]:
            return False, "手机上没装抖音 App，先装好再连"
        if not st.get("agent"):
            err = st.get("agent_error", "")
            return False, (f"自动化代理没就绪（{err or '首次连接需在手机上确认安装提示'}），"
                           "手机上点允许后重试一次")
        try:
            import uiautomator2 as u2   # noqa: PLC0415
            with _state["dev_lock"]:
                _state["device"] = u2.connect(st["device"])
        except Exception as exc:   # noqa: BLE001
            return False, f"连接失败：{str(exc)[:120]}"
    return True, f"✅ 手机已连接（{st['device']}），抖音已装，可以发「刷抖音」了"


def _d():
    dev = _state["device"]
    if dev is None:
        raise RuntimeError("手机未连接：发「连接手机」")
    return dev


def _in_douyin(dev) -> bool:
    try:
        return dev.app_current().get("package") == DOUYIN_PACKAGE
    except Exception:
        return False


# ---------- 会话账本 ----------

def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _daily_minutes() -> int:
    try:
        data = json.loads(DAILY_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("date") == _today():
            return int(data.get("minutes", 0))
    except (OSError, ValueError, TypeError):
        pass
    return 0


def _add_daily(minutes: int) -> None:
    data = {"date": _today(), "minutes": _daily_minutes() + max(0, minutes)}
    try:
        DAILY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = DAILY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(DAILY_FILE)
    except OSError:
        pass


def _touch() -> None:
    _state["last_active"] = time.time()


def _alive() -> bool:
    return bool(_state["thread"] and _state["thread"].is_alive())


def status() -> dict:
    with _state["dev_lock"]:
        dev = _state["device"]
        connected = dev is not None and _device_alive(dev)
        if dev is not None and not connected:
            # 死引用清掉（评审 v3 P1-7）：拔线后不再假报"已连接"
            _state["device"] = None
            _state["last_error"] = _state["last_error"] or "手机已断开，发「连接手机」重连"
    return {"running": _alive(), "paused": _state["pause"].is_set(),
            "connected": connected,
            "elapsed_min": round((time.time() - _state["started_at"]) / 60, 1)
            if _alive() else 0,
            "plan_min": _state["plan_min"], "today_min": _daily_minutes(),
            "last_error": _state["last_error"]}


# ---------- AI 决策（真机屏幕两帧） ----------

def _ai_client():
    from .deepseek_api import DeepSeekClient
    from .envutil import ENV_FILE, load_env
    env = load_env(ENV_FILE)
    # 真机决策专用：短超时+零重试（评审 v3 P1-5：否则一次决策最长 135s，
    # stop 的 8 秒 join 必然超时变成僵尸会话）
    return DeepSeekClient(
        base_url=env.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com",
        api_key=env.get("DEEPSEEK_API_KEY") or "",
        model=env.get("DEEPSEEK_MODEL") or "deepseek-chat",
        timeout=30, max_retries=0, temperature=0.2)


def _to_rgb_pil(img):
    """u2 截图可能是 PIL(含 RGBA/P)/bytes/numpy——统一转 RGB PIL（评审 v3 P2-2）。"""
    from PIL import Image
    if isinstance(img, (bytes, bytearray)):
        img = Image.open(io.BytesIO(bytes(img)))
    if not isinstance(img, Image.Image):
        import numpy as np   # noqa: PLC0415
        img = Image.fromarray(np.asarray(img))
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img


def _frame(dev) -> bytes:
    """手机当前屏幕 → JPEG 字节（真画面：有声音有播放，我们只取视觉帧）。

    缩到最长边 1280：两帧全分辨率直进视觉模型既慢又贵（评审 v3 P2-2）。
    """
    img = _to_rgb_pil(dev.screenshot())
    if max(img.size) > 1280:
        img.thumbnail((1280, 1280))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82)
    return buf.getvalue()


def _ai_decide(shot1: bytes, shot2: bytes) -> dict:
    """同一条视频先后两帧真机画面 → 视觉模型决策。"""
    from .deepseek_api import DeepSeekError

    def _d(shot: bytes) -> str:
        return "data:image/jpeg;base64," + base64.b64encode(shot).decode()

    messages = [
        {"role": "system", "content": (
            "你在替用户刷抖音（真机，带声音），看到的是同一条视频先后两刻的屏幕截图。"
            "判断内容后决定下一步。只返回一行 JSON："
            "{\"action\":\"next|like|watch|follow\",\"watch_seconds\":<5-25的整数>,"
            "\"reason\":\"<=15字理由\"}。"
            "规则：普通/不感兴趣→next；内容确实出彩才 like；"
            "创作者非常对味才 follow；值得继续看→watch。宁可 next，不要滥点赞。")},
        {"role": "user", "content": [
            {"type": "text", "text": "同一条视频先后两刻的屏幕截图，决定下一步："},
            {"type": "image_url", "image_url": {"url": _d(shot1)}},
            {"type": "image_url", "image_url": {"url": _d(shot2)}}]}]

    async def _call() -> str:
        client = _ai_client()
        try:
            return await client.chat(messages)
        finally:
            await client.aclose()   # 评审 v3 P1-5：一次性客户端用完即关

    try:
        text = asyncio.run(asyncio.wait_for(_call(), timeout=25))
    except DeepSeekError as exc:
        raise RuntimeError(exc.message) from exc
    start_idx = text.find("{")
    if start_idx < 0:
        raise RuntimeError(f"AI 没有返回 JSON：{text[:60]}")
    depth, end_idx = 0, -1
    for i in range(start_idx, len(text)):   # 取第一个平衡花括号块（评审 v3 P2-3）
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end_idx = i
                break
    if end_idx < 0:
        raise RuntimeError(f"AI 返回的 JSON 不完整：{text[:60]}")
    try:
        data = json.loads(text[start_idx:end_idx + 1])
    except ValueError as exc:
        raise RuntimeError(f"AI 返回的不是合法 JSON：{text[:60]}") from exc
    action = data.get("action", "next")
    if action not in ("next", "like", "watch", "follow"):
        action = "next"
    try:
        watch = int(data.get("watch_seconds") or 5)
    except (TypeError, ValueError):
        watch = 5
    return {"action": action, "watch": max(3, min(30, watch)),
            "reason": str(data.get("reason", ""))[:40]}


# ---------- 真机操作 ----------

_recent: list[str] = []   # 最近 AI 决策（最新在后），供聊天上下文引用


def recent_decisions() -> list[str]:
    return list(_recent)


def _record_decision(d: dict) -> None:
    name = {"next": "划走", "like": "点赞", "watch": "多看",
            "follow": "关注"}.get(d["action"], d["action"])
    _recent.append(f"{name}（{d['reason']}）")
    if len(_recent) > 8:
        del _recent[:-8]


def _swipe_next(dev) -> None:
    """上滑切下一条：起止点带随机偏移，时长随机（真人手势）。"""
    w, h = dev.window_size()
    x = int(w * random.uniform(0.42, 0.58))
    y1 = int(h * random.uniform(0.72, 0.82))
    y2 = int(h * random.uniform(0.18, 0.30))
    dev.swipe(x, y1, x, y2, duration=random.uniform(0.15, 0.35))


def _double_tap_like(dev) -> bool:
    """抖音双击屏幕中央 = 点赞（比找按钮稳）。"""
    w, h = dev.window_size()
    x, y = int(w * random.uniform(0.45, 0.55)), int(h * random.uniform(0.40, 0.48))
    try:
        if hasattr(dev, "double_click"):   # 评审 v3 P3：规范双击，间隔随机
            dev.double_click(x, y, duration=random.uniform(0.08, 0.12))
            return True
        dev.click(x, y)
        time.sleep(random.uniform(0.08, 0.12))
        dev.click(x, y)
        return True
    except Exception:
        return False


def _follow(dev) -> bool:
    """点「关注」按钮；已关注/互相关注绝不点（评审 v3 P1-4：防误取关）。"""
    try:
        if dev(textMatches=r"^(已关注|互相关注)$").exists(timeout=0.5):
            return False
        ele = dev(textMatches=r"^关注$")
        if ele.exists(timeout=1):
            ele.click()
            return True
    except Exception:
        pass
    return False


def _open_douyin(dev) -> None:
    if not _in_douyin(dev):
        dev.app_start(DOUYIN_PACKAGE)
        _pause_aware_sleep(random.uniform(3, 5))   # 评审 v3 P2-14：睡眠可被 stop 打断


def _pause_aware_sleep(seconds: float) -> None:
    stop = _state["stop"]
    end = time.time() + seconds
    while time.time() < end and not stop.is_set():
        if _state["pause"].is_set():
            time.sleep(0.5)
            continue
        time.sleep(min(1.0, max(0.05, end - time.time())))


def _device_alive(dev) -> bool:
    """设备对象是否还活着（拔线/手机重启后 info 会失败）。评审 v3 P1-7。"""
    try:
        return bool(dev) and bool(dev.info.get("displayWidth"))
    except Exception:
        return False


def _ensure_foreground(dev) -> bool:
    """亮屏+解锁+确认在抖音前台；亮屏但停在锁屏的情况也会处理。"""
    for _ in range(2):
        try:
            dev.screen_on()
            dev.unlock()
            if _in_douyin(dev):
                return True
        except Exception:
            return False
        time.sleep(1)
    return False


def _execute_decision(dev, d: dict, consecutive_watch: int) -> int:
    """执行一条 AI 决策，返回新的连续观看计数（评审 v3 P1-1：永远只滑一次）。"""
    if d["action"] == "watch":
        consecutive_watch += 1
        if consecutive_watch >= 2:
            _swipe_next(dev)
            consecutive_watch = 0
        else:
            _pause_aware_sleep(d["watch"])
        return consecutive_watch
    if d["action"] == "like":
        _double_tap_like(dev)
        _pause_aware_sleep(random.uniform(1.5, 3))
    elif d["action"] == "follow":
        _follow(dev)
        _pause_aware_sleep(random.uniform(2, 4))
    _swipe_next(dev)
    return 0


def _ai_session_loop(minutes: int, gen: int) -> None:
    """真机 AI 刷主循环：开抖音 → 两帧截图 → AI 决策 → 真滑动/真点赞。

    gen（代数）：stop/新会话都会递增；循环内发现代数变化立即退出，
    杜绝"stop 后旧线程还在操作手机"的僵尸会话（评审 v3 P1-3）。
    """
    stop = _state["stop"]
    session_start = time.time()   # 评审 v3 P2-16：快照，finally 用它算时长
    deadline = session_start + minutes * 60
    consecutive_watch = 0
    try:
        while not stop.is_set() and time.time() < deadline \
                and gen == _state["gen"]:
            with _state["dev_lock"]:
                dev = _state["device"]
            if dev is None or not _device_alive(dev):
                _state["last_error"] = "手机已断开，发「连接手机」重连"
                logger.warning("{}", _state["last_error"])
                with _state["dev_lock"]:
                    _state["device"] = None
                break
            try:
                if not _ensure_foreground(dev):
                    _state["last_error"] = "手机锁屏/未解锁，请解锁后重试"
                    logger.warning("{}", _state["last_error"])
                    break
                if not _in_douyin(dev):
                    _open_douyin(dev)
                shot1 = _frame(dev)
            except Exception as exc:   # noqa: BLE001
                _state["last_error"] = f"手机不可用: {str(exc)[:150]}"
                logger.warning("{}", _state["last_error"])
                with _state["dev_lock"]:
                    if _state["device"] is not None \
                            and not _device_alive(_state["device"]):
                        _state["device"] = None
                break
            _touch()
            _pause_aware_sleep(min(random.uniform(4, 6),
                                   max(0.1, deadline - time.time())))
            if stop.is_set() or gen != _state["gen"]:
                return
            try:
                shot2 = _frame(dev)
            except Exception as exc:   # noqa: BLE001
                _state["last_error"] = f"手机不可用: {str(exc)[:150]}"
                break
            _touch()
            try:
                d = _ai_decide(shot1, shot2)
                _state["ai_failures"] = 0
            except Exception as exc:   # noqa: BLE001
                _state["ai_failures"] += 1
                logger.warning("AI 决策失败({}/3): {}", _state["ai_failures"],
                               str(exc)[:120])
                if _state["ai_failures"] >= 3:
                    _state["last_error"] = f"AI 连续失败，会话结束: {str(exc)[:120]}"
                    logger.warning("AI 连续失败，结束本次刷视频会话")
                    stop.set()
                    break
                _pause_aware_sleep(random.uniform(3, 6))
                continue
            # 决策花了最长 25 秒：期间 stop/新会话可能已发生，动作必须作废
            if stop.is_set() or gen != _state["gen"]:
                return
            logger.info("🤖 AI 刷抖音(真机): {} ({})", d["action"], d["reason"])
            _record_decision(d)
            try:
                consecutive_watch = _execute_decision(dev, d, consecutive_watch)
            except Exception as exc:   # noqa: BLE001
                _state["last_error"] = f"触控失败: {str(exc)[:150]}"
                break
            # 连续刷 6~12 分钟歇 1~3 分钟（真人在手机上也会歇），不超会话期
            if time.time() - _state.get("last_rest", session_start) \
                    > random.uniform(360, 720):
                _state["last_rest"] = time.time()
                rest = min(random.uniform(60, 180),
                           max(0.0, deadline - time.time()))
                logger.info("抖音真机散步：休息 {:.0f}s", rest)
                _pause_aware_sleep(rest)
    except Exception as exc:   # noqa: BLE001
        _state["last_error"] = str(exc)[:200]
    finally:
        _add_daily(int((time.time() - session_start) / 60) or 1)


def _start_watchdog() -> None:
    if _state["watchdog"] and _state["watchdog"].is_alive():
        return

    def _watch():
        while True:
            time.sleep(60)
            # 真机模式不需要关浏览器；只做会话超时兜底
            if _alive() and _state["started_at"] \
                    and time.time() - _state["started_at"] > (MAX_SESSION_MIN + 5) * 60:
                stop()

    _state["watchdog"] = threading.Thread(target=_watch, daemon=True)
    _state["watchdog"].start()


# ---------- 对外操作 ----------

def start(minutes: int) -> tuple[bool, str]:
    with _state["dev_lock"]:
        if _state["thread"] and not _state["thread"].is_alive():
            _state["thread"] = None
        if _alive():
            return False, "已经在刷了，发「抖音暂停」或「停止抖音」"
    minutes = max(1, min(minutes, MAX_SESSION_MIN))
    if _daily_minutes() + minutes > MAX_DAILY_MIN:
        left = max(0, MAX_DAILY_MIN - _daily_minutes())
        return False, f"今日上限 {MAX_DAILY_MIN} 分钟，还剩 {left} 分钟"
    # 评审 v3 P1-5：启动前做真配置检查（原来的 _ai_client() 空建客户端是假验证）
    from .envutil import ENV_FILE, load_env
    env = load_env(ENV_FILE)
    if not (env.get("DEEPSEEK_API_KEY") or "").strip():
        return False, "还没填 API Key：控制台 → 模型配置"
    if not (env.get("DEEPSEEK_MODEL") or "").strip():
        return False, "还没设置模型：控制台 → 模型配置"
    ok, msg = connect()
    if not ok:
        return False, msg
    _state["stop"].clear()
    _state["pause"].clear()
    _state["started_at"] = time.time()
    _state["plan_min"] = minutes
    _state["last_error"] = ""
    _state["ai_failures"] = 0
    _state["gen"] = _state["gen"] + 1   # 新会话作废一切旧循环（评审 v3 P1-3）
    _recent.clear()                     # 评审 v3 P2-15：决策记录不跨会话
    _state["last_rest"] = time.time()   # 评审 v3 P3：休息计时随会话重置
    _state["thread"] = threading.Thread(target=_ai_session_loop,
                                        args=(minutes, _state["gen"]),
                                        daemon=True)
    _state["thread"].start()
    _start_watchdog()
    _touch()
    return True, (f"🤖 AI 真机刷抖音开刷（约 {minutes} 分钟）：手机上真开抖音"
                  "（有声音），AI 看真实屏幕两帧做决策。手机保持解锁+亮屏+USB 连接")


def stop() -> tuple[bool, str]:
    _state["stop"].set()
    _state["gen"] = _state["gen"] + 1   # 评审 v3 P1-3：代数作废，决策中的旧循环不再执行动作
    thread = _state["thread"]
    if thread and thread.is_alive():
        thread.join(timeout=8)
    if thread is not None and thread.is_alive():
        # 线程还卡在 AI 调用/触控里：不能谎报已停，也不能立刻允许新会话
        return False, "停止中：当前动作还没结束，稍等几秒再发「停止抖音」"
    _state["thread"] = None
    _state["plan_min"] = 0
    return True, "已停止（手机上的抖音保持打开，可自己接着看）"


def pause() -> tuple[bool, str]:
    if not _alive():
        return False, "现在没在刷"
    _state["pause"].set()
    return True, "已暂停（抖音还在手机上播着，可随时「抖音继续」）"


def resume() -> tuple[bool, str]:
    _state["pause"].clear()
    return True, "继续刷" if _alive() else "会话已结束，重新发「刷抖音」"


def next_video() -> tuple[bool, str]:
    with _state["dev_lock"]:
        dev = _state["device"]
    if dev is None:
        return False, "手机未连接"
    try:
        _swipe_next(dev)
        return True, "下一条"
    except Exception as exc:   # noqa: BLE001
        return False, f"滑动失败：{str(exc)[:120]}"


def like_current() -> tuple[bool, str]:
    with _state["dev_lock"]:
        dev = _state["device"]
    if dev is None:
        return False, "手机未连接"
    try:
        ok = _double_tap_like(dev)
        return (True, "已双击点赞 ❤") if ok else (False, "双击失败")
    except Exception as exc:   # noqa: BLE001
        return False, f"点赞失败：{str(exc)[:120]}"


def screenshot() -> bytes | None:
    with _state["dev_lock"]:
        dev = _state["device"]
    if dev is None:
        return None
    try:
        return _frame(dev)
    except Exception as exc:   # noqa: BLE001
        _state["last_error"] = str(exc)[:150]
        return None
