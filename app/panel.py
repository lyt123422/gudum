"""本地控制台：浏览器里填 URL / Key / 人设 / QQ 号，一键启停、扫码、看日志。

入口：双击 控制台.bat，或 .venv\\Scripts\\python.exe -m app.panel
仅监听 127.0.0.1（本机自用，无鉴权）；机器人内核 8081，控制台 8090。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import personas, plugin_manager
from .envutil import EnvValueError, load_env, update_env as _env_update
from .log_setup import redact_log

ROOT = Path(__file__).resolve().parents[1]
_PAGE_PATH = Path(__file__).resolve().parent / "panel_page.html"
ENV_FILE = ROOT / ".env"
PANEL_PORT = 8090
BOT_PORT = int(load_env(ENV_FILE).get("PORT") or "8081")   # 评审 P2-11：跟随 .env
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
QR_PATH = ROOT / "napcat" / "napcat" / "cache" / "qrcode.png"
LOG_DIR = ROOT / "data" / "logs"
BOT_OUT = ROOT / "data" / "panel_bot.log"
NAPCAT_OUT = ROOT / "data" / "panel_napcat.log"

_procs: dict[str, subprocess.Popen | None] = {"bot": None, "napcat": None}

# 模型提供商预设与分档档案（设计参考 AstrBot「模型提供商」页：各家独立配置）
PROVIDER_PRESETS: dict[str, dict | None] = {
    "DeepSeek": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    "智谱GLM": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash"},
    "通义千问": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "model": "qwen-plus"},
    "Kimi": {"base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
    "硅基流动": {"base_url": "https://api.siliconflow.cn/v1",
                "model": "deepseek-ai/DeepSeek-V3"},
    "Ollama本地": {"base_url": "http://localhost:11434/v1", "model": "qwen2.5:7b"},
    "自定义": None,
}
PROVIDERS_FILE = ROOT / "data" / "providers.json"


def _load_profiles() -> dict[str, dict]:
    try:
        data = json.loads(PROVIDERS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _save_profiles(profiles: dict[str, dict]) -> None:
    PROVIDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(PROVIDERS_FILE,
                  json.dumps(profiles, ensure_ascii=False, indent=2))


def _ensure_profiles(env: dict[str, str]) -> dict[str, dict]:
    """首次使用：把当前生效配置迁进对应提供商档案（URL 匹配预设归档，否则归「自定义」）。"""
    profiles = _load_profiles()
    if profiles:
        return profiles
    live = {"base_url": env.get("DEEPSEEK_BASE_URL", ""),
            "api_key": env.get("DEEPSEEK_API_KEY", ""),
            "model": env.get("DEEPSEEK_MODEL", "")}
    home = next((name for name, preset in PROVIDER_PRESETS.items()
                 if preset and preset["base_url"] == live["base_url"]), None)
    if home:
        profiles[home] = live
    else:
        profiles["自定义"] = live
    # DeepSeek 槽永远给一份默认预设（空 Key），新手开箱即用
    profiles.setdefault("DeepSeek", {"base_url": "https://api.deepseek.com",
                                     "api_key": "", "model": "deepseek-chat"})
    _save_profiles(profiles)
    return profiles


def _live_provider_name(base_url: str) -> str:
    home = next((name for name, preset in PROVIDER_PRESETS.items()
                 if preset and preset["base_url"] == base_url), None)
    return home or "自定义"

app = FastAPI(title="DeepSeek QQ Bot 控制台")

_ALLOWED_ORIGINS = {
    f"http://127.0.0.1:{PANEL_PORT}",
    f"http://localhost:{PANEL_PORT}",
}


def _mask(key: str) -> str:
    """Key 掩码：接口一律只回掩码，前端空值/掩码=不修改（评审 P0-2）。"""
    key = key or ""
    return key[:6] + "…" + key[-4:] if len(key) > 12 else ("…" if key else "")


def _looks_masked(key: str) -> bool:
    """掩码 Key（如 sk-b23…23e8）不是真 Key，写回会覆盖真值（评审 P0-1）。"""
    return "…" in key or "..." in key


@app.middleware("http")
async def _guard(request: Request, call_next):
    """本机 + 跨站双层防护（评审 P0-1）。

    Host 校验防 DNS 重绑定；非 GET 强制 application/json 并校验
    Origin/Sec-Fetch-Site——恶意网页的跨站表单 POST 拿不出合法 JSON
    Content-Type，也过不了 Origin 检查，无法再偷 Key/改配置/停服务。
    """
    host = (request.headers.get("host") or "").rstrip().rsplit(":", 1)[0]
    if host not in ("127.0.0.1", "localhost", "[::1]", "::1"):
        return JSONResponse({"error": "仅限本机访问"}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return JSONResponse({"error": "仅接受 application/json"}, status_code=415)
        origin = request.headers.get("origin")
        if origin and origin not in _ALLOWED_ORIGINS:
            return JSONResponse({"error": "跨站请求被拒绝"}, status_code=403)
        if request.headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
            return JSONResponse({"error": "跨站请求被拒绝"}, status_code=403)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")   # 评审 P2-13
    if request.url.path == "/":
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "frame-ancestors 'none'")
    return response


def _atomic_write(path: Path, text: str) -> None:
    """临时文件+替换：写一半崩溃也不会留下半截配置文件。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# ---------- .env 原位更新（实现下沉在 envutil，保留注释与其他配置行） ----------

def update_env(values: dict[str, str]) -> None:
    _env_update(ENV_FILE, values)


# ---------- 子进程管理 ----------

def _running(name: str) -> bool:
    proc = _procs.get(name)
    return bool(proc and proc.poll() is None)


def _stop(name: str) -> None:
    proc = _procs.get(name)
    if proc and proc.poll() is None:
        # taskkill /T 连子进程一起收（napcat_runner 的 node 子进程否则会变孤儿）
        kill = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True) if Path("C:/Windows").exists() else None
        if kill is None:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
    _procs[name] = None


def _spawn(name: str, module: str, outfile: Path) -> None:
    _stop(name)
    with outfile.open("a", encoding="utf-8") as fh:
        fh.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} 启动 =====\n")
    fh = outfile.open("a", encoding="utf-8")
    _procs[name] = subprocess.Popen(
        [str(VENV_PY), "-m", *module.split()],
        cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    fh.close()   # 父进程句柄立即关闭（子进程有自己的继承副本），防句柄泄漏


# ---------- LLM 连通测试 ----------

def _llm_test(base: str, key: str, model: str) -> dict:
    import logging
    from openai import OpenAI
    start = time.time()
    try:
        client = OpenAI(base_url=base or "https://api.deepseek.com",
                        api_key=key or "EMPTY", timeout=20)
        resp = client.chat.completions.create(
            model=model or "deepseek-chat",
            messages=[{"role": "user", "content": "只回复两个字：正常"}],
            max_tokens=20)
    except Exception as exc:  # noqa: BLE001
        # 评审 v3 P2-8：原始异常可能回显 Key，只回状态码；全文脱敏后进服务端日志
        raw = str(exc)
        logging.getLogger("app").warning("连通测试失败: %s", redact_log(raw))
        status = getattr(exc, "status_code", None)
        status = status if isinstance(status, int) else None
        text = raw.lower()
        if status == 401 or "401" in text or "authentication" in text:
            return {"ok": False, "error": "API Key 无效（HTTP 401）"}
        if status == 402 or "402" in text:
            return {"ok": False, "error": "账户余额不足（HTTP 402）"}
        if status == 404 or "404" in text:
            return {"ok": False, "error": "模型名可能不对（HTTP 404）"}
        if "timed out" in text or "timeout" in text:
            return {"ok": False, "error": "请求超时（服务无响应）"}
        return {"ok": False,
                "error": "调用失败（网络或服务错误），详情已写服务端日志"}
    if not resp.choices:
        return {"ok": False, "error": "服务返回了空响应（模型名可能不对）"}   # 评审 v4 P2-9
    return {"ok": True, "latency_ms": int((time.time() - start) * 1000),
            "reply": (resp.choices[0].message.content or "").strip()}


# ---------- API ----------

@app.get("/api/providers")
async def api_providers() -> dict:
    """提供商预设 + 各家已保存档案（Key 一律掩码，评审 P0-2）。"""
    profiles = _ensure_profiles(load_env(ENV_FILE))
    masked = {name: {**profile, "api_key": _mask(profile.get("api_key", ""))}
              for name, profile in profiles.items()}
    return {"presets": PROVIDER_PRESETS, "saved": masked}


@app.get("/api/config")
async def api_config() -> dict:
    env = load_env(ENV_FILE)
    active = personas.get_active()
    try:
        parsed = json.loads(env.get("SUPERUSERS", "[]"))
        su_list = [str(x) for x in parsed] if isinstance(parsed, list) else []
    except ValueError:
        su_list = []   # 评审 v3 P2-9：写坏的 SUPERUSERS 不再 500
    return {"base_url": env.get("DEEPSEEK_BASE_URL", ""),
            "api_key": _mask(env.get("DEEPSEEK_API_KEY", "")),
            "api_key_set": bool(env.get("DEEPSEEK_API_KEY")),
            "model": env.get("DEEPSEEK_MODEL", ""),
            "live_provider": _live_provider_name(env.get("DEEPSEEK_BASE_URL", "")),
            "persona_name": active.name,
            "napcat_account": env.get("NAPCAT_ACCOUNT", ""),
            "napcat_password_set": bool(env.get("NAPCAT_QUICK_PASSWORD")),
            "superusers": su_list}   # 评审 P2-10：完整列表，前端逗号分隔编辑


async def _body_json(request: Request) -> dict:
    """浏览器 fetch 恒为 UTF-8；Windows 控制台工具可能发 GBK，做双解码兜底。"""
    raw = await request.body()
    for encoding in ("utf-8", "gbk"):
        try:
            data = json.loads(raw.decode(encoding))
            return data if isinstance(data, dict) else {}
        except (UnicodeDecodeError, ValueError):
            continue
    return {}


@app.post("/api/save")
async def api_save(request: Request) -> dict:
    body = await _body_json(request)
    before = load_env(ENV_FILE)   # 评审 P1-1：在任何写入之前快照，changed 才可信
    provider = str(body.get("provider") or "").strip()
    if provider and provider not in PROVIDER_PRESETS:
        return {"ok": False, "error": f"未知的提供商: {provider}"}
    # 只更新请求里显式携带的非空字段：掩码/空值=不修改，残缺请求不清配置
    field_map = {"base_url": "DEEPSEEK_BASE_URL", "api_key": "DEEPSEEK_API_KEY",
                 "model": "DEEPSEEK_MODEL", "napcat_account": "NAPCAT_ACCOUNT",
                 "napcat_password": "NAPCAT_QUICK_PASSWORD"}
    values = {env_key: str(body.get(k)).strip() for k, env_key in field_map.items()
              if body.get(k)}
    if values.get("DEEPSEEK_API_KEY") and _looks_masked(values["DEEPSEEK_API_KEY"]):
        return {"ok": False, "error": "检测到掩码 Key：请输入新 Key，或留空保持不变"}
    superuser = str(body.get("superuser") or "").strip()
    # 管理员支持逗号分隔多人（评审 P2-10）
    admin_list = ([s.strip() for s in re.split(r"[,，、\s]+", superuser) if s.strip()]
                  if superuser else [])
    bad = [s for s in admin_list if not s.isdigit()]
    if bad:
        return {"ok": False, "error": f"管理员 QQ 必须是纯数字: {', '.join(bad)}"}
    # 字段合法性（评审 P1-5）：数字字段、URL 协议
    if values.get("NAPCAT_ACCOUNT") and not values["NAPCAT_ACCOUNT"].isdigit():
        return {"ok": False, "error": "QQ 号必须是纯数字"}
    if values.get("DEEPSEEK_BASE_URL") and not \
            values["DEEPSEEK_BASE_URL"].startswith(("http://", "https://")):
        return {"ok": False, "error": "Base URL 必须以 http:// 或 https:// 开头"}
    if provider:
        base_url = values.get("DEEPSEEK_BASE_URL", "")
        if not base_url:
            return {"ok": False, "error": "Base URL 不能为空"}
        profiles = _load_profiles()
        old_profile = profiles.get(provider, {})
        requested_base = base_url.rstrip("/")
        old_base = str(old_profile.get("base_url", "")).rstrip("/")
        # 评审 v3 P0-1：换 Base URL 不带新 Key = 把旧 Key 发往新地址，拒绝；
        # 只有 base 与档案一致时才允许沿用档案 Key（正常切换流程）
        if requested_base != old_base and not values.get("DEEPSEEK_API_KEY"):
            return {"ok": False, "error": "更换 Base URL 时必须同时填写 API Key"}
        old_key = str(old_profile.get("api_key", ""))
        had_masked = bool(old_key and _looks_masked(old_key))
        if had_masked:
            old_key = ""   # 评审 v3 P2-7：历史掩码 Key 不可复用
        if not values.get("DEEPSEEK_API_KEY") and requested_base == old_base \
                and not old_key and not base_url.startswith(("http://127.0.0.1",
                                                             "http://localhost")):
            # 评审 v4 P2-10：区分"没 Key"和"掩码 Key"
            if had_masked:
                return {"ok": False,
                        "error": "该档案保存的是掩码 Key，请重新输入完整 Key"}
            return {"ok": False,
                    "error": "该提供商还没有可用的 API Key，请先填写"}
        new_key = values.get("DEEPSEEK_API_KEY") or \
            (old_key if requested_base == old_base else "")
        new_model = values.get("DEEPSEEK_MODEL") or old_profile.get("model", "")
        is_local = base_url.startswith(("http://127.0.0.1", "http://localhost"))
        if not new_key and not is_local:
            return {"ok": False,
                    "error": "该提供商还没有可用的 API Key，请先填写（本地 Ollama 除外）"}
        profiles[provider] = {"base_url": base_url, "api_key": new_key,
                              "model": new_model}
        _save_profiles(profiles)
        values["DEEPSEEK_BASE_URL"] = base_url
        values["DEEPSEEK_API_KEY"] = new_key
        values["DEEPSEEK_MODEL"] = new_model
    elif values.get("DEEPSEEK_BASE_URL"):
        # 评审 v3 P0-1：非提供商路径同样不许"换地址留旧 Key"
        new_base = values["DEEPSEEK_BASE_URL"].rstrip("/")
        old_base = before.get("DEEPSEEK_BASE_URL", "").rstrip("/")
        if new_base != old_base and not values.get("DEEPSEEK_API_KEY"):
            return {"ok": False, "error": "更换 Base URL 时必须同时填写 API Key"}
    if admin_list:
        values["SUPERUSERS"] = json.dumps(admin_list, ensure_ascii=False)
    try:
        update_env(values)
    except EnvValueError as exc:
        return {"ok": False, "error": str(exc)}
    persona_text = (body.get("persona") or "").strip()
    if persona_text:   # 人设写入人设库文件（.env 里的 BOT_PERSONA 只作首次种子）
        try:
            personas.set_persona_text(personas.active_name(), persona_text)
        except (KeyError, OSError) as exc:
            return {"ok": False, "error": f"人设保存失败: {exc}"}
    # 内核启动时读配置：DEEPSEEK_*/SUPERUSERS 真变化才重启，避免无谓断连（评审 P1-1）
    restart_keys = ("DEEPSEEK_BASE_URL", "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL",
                    "SUPERUSERS")
    changed = any(values.get(k) is not None and values.get(k, "") != before.get(k, "")
                  for k in restart_keys)
    bot_restarted = False
    if changed and (_running("bot") or _pid_on_port(BOT_PORT)):
        bot_restarted = await _restart_kernel_if_running()
        if not bot_restarted:
            return {"ok": False,
                    "error": "配置已保存，但内核重启失败，请到运行日志排查"}
    return {"ok": True, "bot_restarted": bot_restarted}


@app.post("/api/account/adopt")
async def api_account_adopt(request: Request) -> dict:
    """把实际扫码登录的 QQ 号写进配置（替代轮询自动回写，评审 P2-10）。"""
    body = await _body_json(request)
    qq = str(body.get("qq") or "").strip()
    if not qq.isdigit():
        return {"ok": False, "error": "QQ 号必须是纯数字"}
    try:
        update_env({"NAPCAT_ACCOUNT": qq})
    except EnvValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "qq": qq}


@app.post("/api/test")
async def api_test(request: Request) -> dict:
    body = await _body_json(request)
    env = load_env(ENV_FILE)
    base = (str(body.get("base_url") or "") or env.get("DEEPSEEK_BASE_URL", "")).strip()
    key = (str(body.get("api_key") or "")).strip()
    if not key:
        # 评审 P0-1：只查用户自己保存的 provider 档案（按 base_url 匹配），
        # 绝不把 Key 发往档案之外的地址
        for profile in _load_profiles().values():
            if profile.get("base_url", "").rstrip("/") == base.rstrip("/") \
                    and profile.get("api_key"):
                key = profile["api_key"]
                break
    if not key and base.rstrip("/") == env.get("DEEPSEEK_BASE_URL", "").strip().rstrip("/"):
        key = env.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        return {"ok": False,
                "error": "请先填 API Key（不会拿已保存的 Key 去请求其他地址）"}
    if _looks_masked(key):   # 评审 v3 P2-8：掩码 Key 不可用于测试
        return {"ok": False, "error": "该 Key 是掩码，请输入完整 Key 再测试"}
    model = (str(body.get("model") or "") or env.get("DEEPSEEK_MODEL", "")).strip()
    # 真实 API 调用可达数秒，放线程池执行，不能阻塞事件循环
    return await asyncio.to_thread(_llm_test, base, key, model)


@app.post("/api/persona/switch")
async def api_persona_switch(request: Request) -> dict:
    body = await _body_json(request)
    name = (body.get("name") or "").strip()
    if name not in personas.load_all():
        return {"ok": False, "error": "没有这个人设"}
    personas.set_active(name)
    return {"ok": True, "name": name}


_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4",
                     "COM5", "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2",
                     "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"}


def _persona_path(name: str) -> Path | None:
    """人设名 -> 文件路径；白名单字符 + Windows 保留名过滤，防路径穿越。"""
    raw_name = (name or "").strip()
    if not raw_name or raw_name.upper() in _WINDOWS_RESERVED:
        return None
    if not re.fullmatch(r"[\w\u4e00-\u9fff\- ]{1,40}", raw_name):
        return None   # 含 <>/"'/\ 等的名称一律拒绝（评审 P0-2：人设名 XSS 源头）
    safe = re.sub(r"[^\w\u4e00-\u9fff-]", "_", raw_name)[:40]
    if not safe or safe.startswith("."):
        return None
    return personas.PERSONA_DIR / f"{safe}.txt"


@app.get("/api/plugins")
async def api_plugins() -> dict:
    """插件中心：内置插件状态 + 精选市场 + 已安装第三方。"""
    return {"builtin": plugin_manager.builtin_status(),
            "market": plugin_manager.MARKET,
            "installed": plugin_manager.thirdparty_status()}


@app.post("/api/plugin/toggle")
async def api_plugin_toggle(request: Request) -> dict:
    body = await _body_json(request)
    plugin_id = str(body.get("id") or "").strip()
    if not plugin_id:
        return {"ok": False, "error": "缺少插件 id"}
    enabled = bool(body.get("enabled"))
    ok = plugin_manager.set_enabled(plugin_id, enabled)
    if not ok:
        return {"ok": False, "error": "没有这个插件"}
    # 第三方插件的启用/停用需要真重启内核才生效（评审 P1-4）；内置插件运行时生效
    if plugin_id in plugin_manager.thirdparty_pips() and enabled != (
            plugin_manager.module_of(plugin_id) in await asyncio.to_thread(_kernel_plugins)):
        if _running("bot") or _pid_on_port(BOT_PORT):
            if not await _restart_kernel_if_running():
                return {"ok": False, "error": "内核重启失败，插件状态可能未生效，请看运行日志"}
        else:
            return {"ok": True, "note": "内核未运行，下次启动生效"}   # 评审 v3 P2-10
    return {"ok": True}


def _kernel_identity_ok(pid: int) -> bool:
    """校验端口上的进程确实是本项目内核（healthz pid 或命令行含 app.main）。"""
    try:
        resp = httpx.get(f"http://127.0.0.1:{BOT_PORT}/healthz", timeout=1.5)
        if resp.status_code == 200 and resp.json().get("pid") == pid:
            return True
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
            capture_output=True, text=True, errors="replace", timeout=8).stdout or ""
        return "app.main" in out
    except Exception:
        return False


_restart_lock = asyncio.Lock()   # 评审 v3 P2-10：重启串行，防并发双启


RESTART_POLL_INTERVAL = 0.5   # 端口/健康检查轮询间隔（测试可 patch 此常量提速）


async def _restart_poll_delay() -> None:
    await asyncio.sleep(RESTART_POLL_INTERVAL)


async def _restart_kernel_if_running() -> bool:
    """真重启（评审 P1-4）：找旧内核→校验身份→停→等端口释放→拉起→轮询 healthz。

    成功判定必须 PID 变化（评审 v3 P1-6）：kill 失败时旧内核仍占端口并返回
    200，按旧逻辑会谎报"重启成功"。
    """
    async with _restart_lock:
        old_pid = await asyncio.to_thread(_pid_on_port, BOT_PORT)
        if old_pid and not await asyncio.to_thread(_kernel_identity_ok, old_pid):
            return False   # 端口被陌生进程占用：宁可不动作，也不误杀
        if not (_running("bot") or old_pid is not None):
            return False
        await asyncio.to_thread(_stop, "bot")
        if old_pid:
            killed = await asyncio.to_thread(_kill_pid, old_pid)
            if not killed:
                return False
        for _ in range(20):   # 最多等 10 秒端口释放
            if _pid_on_port(BOT_PORT) is None:
                break
            await _restart_poll_delay()
        else:
            return False   # 端口没释放：新内核起不来，不谎报
        await asyncio.to_thread(_spawn, "bot", "app.main", BOT_OUT)
        for _ in range(40):   # 最多等 20 秒起来
            try:
                resp = await asyncio.to_thread(
                    httpx.get, f"http://127.0.0.1:{BOT_PORT}/healthz", timeout=1.5)
                new_pid = resp.json().get("pid")
                if resp.status_code == 200 and new_pid \
                        and new_pid != old_pid:
                    return True
            except Exception:
                pass
            await _restart_poll_delay()
        return False


def _kernel_plugins() -> list[str]:
    """当前内核已加载的插件名（healthz.plugins）。"""
    try:
        resp = httpx.get(f"http://127.0.0.1:{BOT_PORT}/healthz", timeout=2)
        return list(resp.json().get("plugins") or [])
    except Exception:
        return []


@app.post("/api/plugin/install")
async def api_plugin_install(request: Request) -> dict:
    body = await _body_json(request)
    pip_name = str(body.get("pip") or "").strip()
    if pip_name not in {m["pip"] for m in plugin_manager.MARKET}:
        return {"ok": False, "error": "不在精选市场里"}
    ok, log = await asyncio.to_thread(plugin_manager.install, pip_name)
    if not ok:
        return {"ok": False, "error": f"安装失败：{log[-200:]}"}
    # 本地插件是运行时开关，不用重启；第三方 pip 插件真重启后验证已加载（评审 P1-4）
    restarted = False
    if not pip_name.startswith("local:"):
        restarted = await _restart_kernel_if_running()
        if restarted and plugin_manager.module_of(pip_name) not in \
                await asyncio.to_thread(_kernel_plugins):
            return {"ok": False,
                    "error": f"重启后 {plugin_manager.module_of(pip_name)} "
                             "未出现在内核插件列表，请看运行日志"}
    return {"ok": True, "restarted": restarted, "log": log[-200:]}


@app.post("/api/plugin/uninstall")
async def api_plugin_uninstall(request: Request) -> dict:
    body = await _body_json(request)
    pip_name = str(body.get("pip") or "").strip()
    ok, log = await asyncio.to_thread(plugin_manager.uninstall, pip_name)
    if not ok:
        return {"ok": False, "error": f"卸载失败：{log[-200:]}"}
    if await _restart_kernel_if_running() and plugin_manager.module_of(pip_name) in \
            await asyncio.to_thread(_kernel_plugins):
        return {"ok": False, "error": "卸载后内核仍加载着该插件，请看运行日志"}
    return {"ok": True}


@app.get("/api/personas")
async def api_personas() -> dict:
    """人设库列表（源码级编辑用，学习 AstrBot「可视化+源码」双模式）。"""
    return {"active": personas.active_name(),
            "personas": {p.name: p.raw for p in personas.load_all().values()}}


@app.post("/api/persona/save")
async def api_persona_save(request: Request) -> dict:
    body = await _body_json(request)
    path = _persona_path(body.get("name") or "")
    raw = (body.get("raw") or "").strip()
    if path is None or not raw:
        return {"ok": False, "error": "名称或内容不能为空"}
    parsed = personas.parse_persona(raw, path.stem)
    if not parsed.text:
        return {"ok": False, "error": "缺少 [人设] 段"}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw if raw.endswith("\n") else raw + "\n", encoding="utf-8")
    personas.invalidate()   # 面板进程自己的缓存也要失效
    return {"ok": True, "name": parsed.name}


@app.post("/api/persona/delete")
async def api_persona_delete(request: Request) -> dict:
    body = await _body_json(request)
    name = (body.get("name") or "").strip()
    library = personas.load_all()
    if name not in library:
        return {"ok": False, "error": "没有这个人设"}
    if len(library) <= 1:
        return {"ok": False, "error": "至少保留一套人设"}
    target = library[name].path
    if target:
        target.unlink(missing_ok=True)
    personas.invalidate()
    if personas.active_name() == name:  # 删的是当前人设则回落到第一套
        fallback = next(iter(sorted(library)))
        if fallback != name:
            personas.set_active(fallback)
    return {"ok": True}


# ---------- 定时任务（面板配置端，增删改查走插件自身的状态文件） ----------

def _schedule_mod():
    """延迟导入：内核插件模块带 nonebot 依赖，面板单独运行时不该被拖崩。"""
    from .plugins import schedule
    return schedule


@app.get("/api/schedule")
async def api_schedule() -> dict:
    try:
        mod = _schedule_mod()
    except Exception as exc:
        return {"ok": False, "error": f"定时任务模块不可用：{exc}", "tasks": []}
    return {"ok": True, **await asyncio.to_thread(mod.plugin_status)}


@app.post("/api/schedule/save")
async def api_schedule_save(request: Request) -> dict:
    body = await _body_json(request)
    name = str(body.get("name") or "").strip()
    cron = str(body.get("cron") or "").strip()
    content = str(body.get("content") or "").strip()
    target = str(body.get("target") or "").strip()
    task_type = "private" if str(body.get("type")) == "private" else "group"
    enabled = bool(body.get("enabled", True))
    # 前端已剥前缀，但直接调 API / 手工填表也可能带，服务端再兜一层
    if target.lower().startswith("friend:"):
        task_type, target = "private", target.split(":", 1)[1].strip()
    try:
        mod = _schedule_mod()
    except Exception as exc:
        return {"ok": False, "error": f"定时任务模块不可用：{exc}"}
    if name and any(t["name"] == name for t in await asyncio.to_thread(mod._load)):
        ok, msg = await asyncio.to_thread(
            mod.update_task, name, cron=cron, content=content,
            target=target, type=task_type, enabled=enabled)
    else:
        ok, msg = await asyncio.to_thread(
            mod.add_task, name, cron, content, target, task_type, enabled)
    # 保存即确保调度器在跑（面板侧没有内核事件循环时 add_task 仍可用）
    return {"ok": ok, "error": "" if ok else msg, "msg": msg,
            "tasks": await asyncio.to_thread(lambda: mod.plugin_status()["tasks"])}


@app.post("/api/schedule/delete")
async def api_schedule_delete(request: Request) -> dict:
    body = await _body_json(request)
    name = str(body.get("name") or "").strip()
    try:
        mod = _schedule_mod()
    except Exception as exc:
        return {"ok": False, "error": f"定时任务模块不可用：{exc}"}
    ok, msg = await asyncio.to_thread(mod.remove_task, name)
    return {"ok": ok, "error": "" if ok else msg, "msg": msg,
            "tasks": await asyncio.to_thread(lambda: mod.plugin_status()["tasks"])}


@app.post("/api/schedule/toggle")
async def api_schedule_toggle(request: Request) -> dict:
    body = await _body_json(request)
    name = str(body.get("name") or "").strip()
    enabled = bool(body.get("enabled"))
    try:
        mod = _schedule_mod()
    except Exception as exc:
        return {"ok": False, "error": f"定时任务模块不可用：{exc}"}
    ok, msg = await asyncio.to_thread(mod.set_enabled, name, enabled)
    return {"ok": ok, "error": "" if ok else msg, "msg": msg,
            "tasks": await asyncio.to_thread(lambda: mod.plugin_status()["tasks"])}


@app.post("/api/bot/start")
async def api_bot_start() -> dict:
    # taskkill/netstat 等阻塞调用放线程池，不卡事件循环（保存卡死事故的同款教训）
    await asyncio.to_thread(_spawn, "bot", "app.main", BOT_OUT)
    return {"ok": True}


def _pid_on_port(port: int) -> int | None:
    """按端口找监听进程 PID（netstat 兜底，用于停止非本面板启动的内核）。

    精确解析列（评审 P1-3）：子串匹配会把 80811 当成 8081；
    中文 Windows 的 netstat 输出是 GBK，必须 errors=replace。
    """
    try:
        result = subprocess.run(["netstat", "-ano"], capture_output=True,
                                text=True, errors="replace", timeout=5)
        out = result.stdout or ""
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP" \
                or parts[3].upper() != "LISTENING":
            continue
        local, pid = parts[1], parts[-1]
        host_part, _, port_part = local.rpartition(":")
        if host_part not in ("127.0.0.1", "[::1]"):
            continue
        if port_part != str(port) or not pid.isdigit():
            continue
        return int(pid)
    return None


def _kill_pid(pid: int) -> bool:
    if Path("C:/Windows").exists():  # Windows：/T 连子进程树一起收
        result = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                capture_output=True)
        return result.returncode == 0   # 评审 v3 P1-6：成败要如实上报
    import os
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


@app.post("/api/bot/stop")
async def api_bot_stop() -> dict:
    await asyncio.to_thread(_stop, "bot")
    # 内核也可能是 start.bat 手动启动的：按端口补刀，保证按钮永远有效；
    # 但陌生进程（评审 v3 P2-19）绝不误杀
    pid = await asyncio.to_thread(_pid_on_port, BOT_PORT)
    if pid and await asyncio.to_thread(_kernel_identity_ok, pid):
        await asyncio.to_thread(_kill_pid, pid)
    return {"ok": True}


@app.post("/api/napcat/start")
async def api_napcat_start() -> dict:
    # 先按当前 .env 刷新 NapCat 反向 WS 配置（令牌/端口/换号后自动同步）
    await asyncio.to_thread(
        subprocess.run,
        [str(VENV_PY), "-m", "app.napcat_config"], cwd=ROOT, capture_output=True)
    env = load_env(ENV_FILE)
    account = env.get("NAPCAT_ACCOUNT", "")
    await asyncio.to_thread(
        _spawn, "napcat", f"app.napcat_runner --account {account}".strip(),
        NAPCAT_OUT)
    return {"ok": True}


@app.post("/api/napcat/stop")
async def api_napcat_stop() -> dict:
    await asyncio.to_thread(_stop, "napcat")   # taskkill 阻塞调用入线程池（评审 P2-11）
    return {"ok": True}


@app.get("/api/status")
async def api_status() -> dict:
    # 以 healthz 为准（无论内核是面板启动还是 start.bat 手动启动都能看到）
    kernel_up = False
    qq_connected = False
    logged_in_uid = ""
    try:
        # 必须用异步客户端+短超时：内核没起时同步调用会卡住事件循环约 2 秒，
        # 浏览器 3 秒一次轮询就会把整个面板拖死（保存一直"保存中"的根因）
        async with httpx.AsyncClient(timeout=0.6) as client:
            resp = await client.get(f"http://127.0.0.1:{BOT_PORT}/healthz")
            kernel_up = resp.status_code == 200
            data = resp.json()
            onebot = data.get("onebot", {})
            qq_connected = bool(onebot.get("connected"))
            logged_in_uid = str((onebot.get("login_info") or {}).get("user_id") or "")
            if not logged_in_uid:   # login_info 拿不到时兜底用连接的 self_id
                for bot in onebot.get("bots", []):
                    if str(bot.get("id", "")).isdigit():
                        logged_in_uid = str(bot["id"])
                        break
    except Exception:
        pass
    env = load_env(ENV_FILE)
    # 账号回写只在配置为空时自动进行；配置了别的号时只报告，由用户点"采用"
    # （评审 P2-10：3 秒轮询自动覆盖会把刚保存的新号改回旧号）
    if logged_in_uid.isdigit() and not env.get("NAPCAT_ACCOUNT", "").strip():
        update_env({"NAPCAT_ACCOUNT": logged_in_uid})
        env["NAPCAT_ACCOUNT"] = logged_in_uid
    claim_code = ""
    code_path = ROOT / "data" / "claim_code.txt"
    if code_path.exists():
        try:
            # 过期码不展示（10 分钟有效，与内核口径一致）
            if time.time() - code_path.stat().st_mtime < 600:
                code = code_path.read_text(encoding="utf-8").strip()
                if len(code) == 6 and code.isdigit():
                    claim_code = code
        except OSError:
            pass
    mem_dir = ROOT / "data" / "memory"
    return {"bot_running": _running("bot") or kernel_up,
            "napcat_running": _running("napcat"),
            "qq_connected": qq_connected,
            "logged_in_qq": logged_in_uid,
            "claim_code": claim_code,
            "api_key_set": bool(env.get("DEEPSEEK_API_KEY")),
            "memory_sessions": len(list(mem_dir.glob("*.json"))) if mem_dir.exists() else 0,
            "bot_port": BOT_PORT}


@app.get("/api/qrcode")
async def api_qrcode():
    # QQ 登录二维码约 2 分钟过期：只端新鲜的，过期旧图宁可 404 也不给用户扫死码
    # (本地文件 stat 为微秒级，上线程池得不偿失)
    if QR_PATH.exists() and time.time() - QR_PATH.stat().st_mtime < 150:  # noqa: ASYNC240
        return FileResponse(QR_PATH, media_type="image/png")
    from fastapi.responses import JSONResponse
    return JSONResponse({"error": "二维码未生成或已过期：重启协议端即可刷新"},
                        status_code=404)


def _tail(path: Path, lines: int = 60) -> str:
    """只读文件尾部 64KB（评审 v3 P2-20：大日志不整读）。"""
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 64 * 1024))
            data = fh.read().decode("utf-8", errors="replace")
        return "\n".join(data.splitlines()[-lines:])
    except OSError:
        return ""


@app.get("/api/logs")
async def api_logs() -> dict:
    kernel = _tail(BOT_OUT) or _tail(LOG_DIR / f"bot-{datetime.now():%Y%m%d}.log")
    return {"kernel": kernel or "（暂无日志）",
            "napcat": _tail(NAPCAT_OUT, 25) or "（暂无日志）"}


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    # 每次请求现读页面文件：改 panel_page.html 即时生效，不用重启面板
    return await asyncio.to_thread(_PAGE_PATH.read_text, encoding="utf-8")


def main() -> None:
    url = f"http://127.0.0.1:{PANEL_PORT}"

    # 浏览器等服务真正就绪后再开，且必须放独立线程：webbrowser.open 在
    # Windows 上同步调用会卡住事件循环，启动后几秒内所有请求（包括保存）无响应
    @app.on_event("startup")
    async def _open_browser() -> None:
        if os.getenv("PANEL_NO_OPEN") != "1":
            threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()

    print(f"控制台已启动：{url}（浏览器没自动打开就手动访问这个地址；关掉本窗口 = 控制台退出）")
    uvicorn.run(app, host="127.0.0.1", port=PANEL_PORT, log_level="warning")


if __name__ == "__main__":
    main()
