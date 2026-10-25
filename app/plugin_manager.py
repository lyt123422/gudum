"""插件管理：内置插件开关 + 精选市场（NoneBot 生态第三方插件的安装/卸载）。

设计：
- 内置插件（联网搜索/角色语音/表情包）代码在内核里，用 data/plugins.json 开关，
  面板即点即生效（2 秒缓存），不用重启。
- 第三方插件来自 pip（nonebot-plugin-*），安装后写入 plugins.json，
  内核启动时按名单动态加载（main.py）。
- 市场目录是人工精选白名单：既是质量门槛，也防任意 pip 包注入。
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / "data" / "plugins.json"

BUILTIN: list[dict] = [
    {"id": "websearch", "name": "联网搜索",
     "desc": "时效性问题自动联网；「联网 <问题>」强制搜索（免费无 Key）", "default": True},
    {"id": "voice", "name": "角色语音",
     "desc": "「语音开」后回复附带配音；人设可指定专属音色（edge-tts 免费）", "default": True},
    {"id": "memes", "name": "表情包",
     "desc": "存表情 / 表情包 / 斗图，本地表情库", "default": True},
]

# 精选市场（pip 包名务必真实存在；描述里写清"装完怎么用"，不装糊涂账）
# type=local 表示"本地内置插件，安装=装依赖+开启"，不是 pip 第三方
MARKET: list[dict] = [
    {"pip": "nonebot-plugin-htmlrender", "module": "nonebot_plugin_htmlrender",
     "name": "HTML 渲染图片", "cat": "底座",
     "desc": "把网页/HTML 渲染成图片的底座组件。单独装没有对话功能，"
             "其他插件（日报、卡片类）会依赖它"},
    {"pip": "nonebot-plugin-apscheduler", "module": "nonebot_plugin_apscheduler",
     "name": "定时任务", "cat": "底座",
     "desc": "cron 定时器底座，开发者向。普通用户装了没有直接可玩的功能"},
    {"pip": "nonebot-plugin-moegoe", "module": "nonebot_plugin_moegoe",
     "name": "原神角色语音", "cat": "语音",
     "desc": "VITS 角色音色合成，比 edge-tts 更有角色感。装后用它的指令"
             "（如 原神语音），首次使用可能要下载模型"},
    {"pip": "nonebot-plugin-random", "module": "nonebot_plugin_random",
     "name": "随机素材", "cat": "娱乐",
     "desc": "随机发图/语音/视频。装完需要自己往 data/random 放素材，"
             "否则没内容可发"},
    {"pip": "nonebot-plugin-chatgpt", "module": "nonebot_plugin_chatgpt",
     "name": "ChatGPT 对话", "cat": "AI",
     "desc": "⚠ 另一套 LLM 对话：装完要在 .env 配它的 API Key 才能用，"
             "且会和内置聊天抢消息（二选一，想用先关内置聊天）"},
]

_MARKET_PIPS = {item["pip"] for item in MARKET}
_CACHE: dict = {"at": 0.0, "state": {}}
_CACHE_TTL = 2.0


def _load_state() -> dict:
    now = time.monotonic()
    if _CACHE["state"] and now - _CACHE["at"] < _CACHE_TTL:
        return _CACHE["state"]
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}
    state.setdefault("builtin", {})
    state.setdefault("thirdparty", {})
    _CACHE.update(at=now, state=state)
    return state


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)
    _CACHE.update(at=time.monotonic(), state=state)


def is_enabled(plugin_id: str) -> bool:
    state = _load_state()
    saved = state["builtin"].get(plugin_id)
    if isinstance(saved, bool):
        return saved
    return next((b["default"] for b in BUILTIN if b["id"] == plugin_id), False)


def set_enabled(plugin_id: str, enabled: bool) -> bool:
    """内置插件或第三方 pip 名都收；返回是否成功。"""
    state = _load_state()
    if plugin_id in {b["id"] for b in BUILTIN}:
        state["builtin"][plugin_id] = enabled
    elif plugin_id in state["thirdparty"]:
        state["thirdparty"][plugin_id]["enabled"] = enabled
    else:
        return False
    _save_state(state)
    return True


def enabled_thirdparty() -> list[str]:
    """内核启动时动态加载的第三方插件模块名（module 显式声明，评审 P2-9）。"""
    state = _load_state()
    return [info.get("module") or module_of(pip)
            for pip, info in state["thirdparty"].items()
            if isinstance(info, dict) and info.get("enabled")]


def module_of(pip_name: str) -> str:
    """pip 包名 → import 模块名（市场条目显式声明，不再用 replace 猜）。"""
    entry = next((m for m in MARKET if m["pip"] == pip_name), None)
    if entry and entry.get("module"):
        return entry["module"]
    return pip_name.replace("-", "_")


def thirdparty_pips() -> set[str]:
    """当前已安装的第三方插件 pip 名集合（面板判断 toggle 是否需要重启）。"""
    return {pip for pip, info in _load_state()["thirdparty"].items()
            if isinstance(info, dict)}


def builtin_status() -> list[dict]:
    return [{**item, "enabled": is_enabled(item["id"])} for item in BUILTIN]


def thirdparty_status() -> list[dict]:
    state = _load_state()
    return [{"pip": pip, "name": info.get("name", pip),
             "enabled": bool(info.get("enabled"))}
            for pip, info in state["thirdparty"].items()
            if isinstance(info, dict)]


def _pip(*args: str) -> tuple[bool, str]:
    """在面板自己的 venv 里跑 pip；返回 (成败, 输出尾部)。"""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", *args, "--no-input"],
            capture_output=True, text=True, errors="replace", timeout=300)
    except subprocess.TimeoutExpired:   # 评审 P2-7：超时要接住，不能炸面板
        return False, "安装超时（5 分钟），可重试或换个网络"
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0, output.strip()[-400:]


_install_lock = threading.Lock()   # 评审 P2-8：装/卸串行，防并发写坏状态


def install(pip_name: str) -> tuple[bool, str]:
    with _install_lock:
        return _install_locked(pip_name)


def _install_locked(pip_name: str) -> tuple[bool, str]:
    if pip_name not in _MARKET_PIPS:
        return False, "不在精选市场白名单里，拒绝安装"
    entry = next(m for m in MARKET if m["pip"] == pip_name)
    if entry.get("type") == "local":
        # 本地插件：安装 = 装齐依赖 + 开启对应内置插件（运行时开关，无需重启内核）
        for dep in entry.get("deps", []):
            ok, log = _pip("install", dep)
            if not ok:
                ok, mirror_log = _pip("install", dep, "-i",
                                      "https://pypi.tuna.tsinghua.edu.cn/simple")
                log = mirror_log or log
            if not ok:
                return False, f"依赖 {dep} 安装失败：{log[-200:]}"
        state = _load_state()
        state["builtin"][entry["id"]] = True
        _save_state(state)
        return True, f"依赖已装好，「{entry['name']}」已开启，立即生效"
    ok, log = _pip("install", pip_name)
    if not ok:
        # 国内直连 PyPI 偶尔超时：自动换清华镜像重试一次（不用 VPN）
        ok, mirror_log = _pip("install", pip_name, "-i",
                              "https://pypi.tuna.tsinghua.edu.cn/simple")
        log = mirror_log or log
    if not ok:
        return False, log
    state = _load_state()
    name = next((m["name"] for m in MARKET if m["pip"] == pip_name), pip_name)
    state["thirdparty"].setdefault(pip_name, {})["enabled"] = True
    state["thirdparty"][pip_name]["name"] = name
    state["thirdparty"][pip_name]["module"] = module_of(pip_name)
    _save_state(state)
    return True, log


def uninstall(pip_name: str) -> tuple[bool, str]:
    with _install_lock:
        return _uninstall_locked(pip_name)


def _uninstall_locked(pip_name: str) -> tuple[bool, str]:
    state = _load_state()
    if pip_name not in state["thirdparty"]:
        return False, "没有安装过这个插件"
    state["thirdparty"][pip_name]["enabled"] = False
    _save_state(state)
    ok, log = _pip("uninstall", "-y", pip_name)
    if ok:
        state["thirdparty"].pop(pip_name, None)
        _save_state(state)
    return ok, log
