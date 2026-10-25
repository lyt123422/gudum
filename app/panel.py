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

ROOT = Path(__file__).resolve().parents[1]
_PAGE_PATH = Path(__file__).resolve().parent / "panel_page.html"
ENV_FILE = ROOT / ".env"
PANEL_PORT = 8090
BOT_PORT = 8081
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
    if request.url.path == "/":
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:")
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
    from openai import OpenAI
    client = OpenAI(base_url=base or "https://api.deepseek.com",
                    api_key=key or "EMPTY", timeout=20)
    start = time.time()
    try:
        resp = client.chat.completions.create(
            model=model or "deepseek-chat",
            messages=[{"role": "user", "content": "只回复两个字：正常"}],
            max_tokens=20)
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        hint = ""
        if "401" in text or "api_key" in text.lower() or "authentication" in text.lower():
            hint = "（API Key 没填或填错）"
        elif "402" in text:
            hint = "（账户余额不足）"
        return {"ok": False, "error": (text[:300] + hint) if text else "调用失败"}
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
    return {"base_url": env.get("DEEPSEEK_BASE_URL", ""),
            "api_key": _mask(env.get("DEEPSEEK_API_KEY", "")),
            "api_key_set": bool(env.get("DEEPSEEK_API_KEY")),
            "model": env.get("DEEPSEEK_MODEL", ""),
            "live_provider": _live_provider_name(env.get("DEEPSEEK_BASE_URL", "")),
            "persona_name": active.name,
            "napcat_account": env.get("NAPCAT_ACCOUNT", ""),
            "napcat_password_set": bool(env.get("NAPCAT_QUICK_PASSWORD")),
            "superuser": (env.get("SUPERUSERS", "").strip("[]\" ").split(",") or [""])[0]}


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
    provider = str(body.get("provider") or "").strip()
    if provider and provider not in PROVIDER_PRESETS:
        return {"ok": False, "error": f"未知的提供商: {provider}"}
    # 只更新请求里显式携带的非空字段：掩码/空值=不修改，残缺请求不清配置
    field_map = {"base_url": "DEEPSEEK_BASE_URL", "api_key": "DEEPSEEK_API_KEY",
                 "model": "DEEPSEEK_MODEL", "napcat_account": "NAPCAT_ACCOUNT",
                 "napcat_password": "NAPCAT_QUICK_PASSWORD"}
    values = {env_key: str(body.get(k)).strip() for k, env_key in field_map.items()
              if body.get(k)}
    superuser = str(body.get("superuser") or "").strip()
    # 字段合法性（评审 P1-5）：数字字段、URL 协议
    if superuser and not superuser.isdigit():
        return {"ok": False, "error": "管理员 QQ 必须是纯数字"}
    if values.get("NAPCAT_ACCOUNT") and not values["NAPCAT_ACCOUNT"].isdigit():
        return {"ok": False, "error": "QQ 号必须是纯数字"}
    if values.get("DEEPSEEK_BASE_URL") and not \
            values["DEEPSEEK_BASE_URL"].startswith(("http://", "https://")):
        return {"ok": False, "error": "Base URL 必须以 http:// 或 https:// 开头"}
    if provider:
        base_url = values.get("DEEPSEEK_BASE_URL", "")
        if not base_url:
            return {"ok": False, "error": "Base URL 不能为空"}
        # 该提供商档案独立保存；Key/模型留空时沿用档案里的旧值（评审 P2-13：
        # 保存其余字段不允许把已存 Key 抹掉）
        profiles = _load_profiles()
        old_profile = profiles.get(provider, {})
        new_key = values.get("DEEPSEEK_API_KEY") or old_profile.get("api_key", "")
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
    if superuser:
        values["SUPERUSERS"] = f'["{superuser}"]'
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
    # 配置在内核启动时读取：DEEPSEEK_* 有实际变化才重启，避免无谓断连
    before = load_env(ENV_FILE)
    kernel_keys = ("DEEPSEEK_BASE_URL", "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL")
    changed = any(values.get(k) is not None and values.get(k, "") != before.get(k, "")
                  for k in kernel_keys)
    if _running("bot") and changed:
        await asyncio.to_thread(_spawn, "bot", "app.main", BOT_OUT)
    return {"ok": True, "bot_restarted": bool(_running("bot") and changed)}


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
    # 出于安全（评审 P0-1）：只有"测试当前已保存的地址"才允许用已存 Key；
    # 未知地址必须显式提供 Key，防止借测试接口把 Key 发往任意地址
    if not key and base.rstrip("/") == env.get("DEEPSEEK_BASE_URL", "").strip().rstrip("/"):
        key = env.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        return {"ok": False,
                "error": "请先填 API Key（不会拿已保存的 Key 去请求其他地址）"}
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
    ok = plugin_manager.set_enabled(plugin_id, bool(body.get("enabled")))
    return {"ok": ok, "error": None if ok else "没有这个插件"}


async def _restart_kernel_if_running() -> bool:
    if not (_running("bot") or _pid_on_port(BOT_PORT)):
        return False
    await asyncio.to_thread(_spawn, "bot", "app.main", BOT_OUT)
    return True


@app.post("/api/plugin/install")
async def api_plugin_install(request: Request) -> dict:
    body = await _body_json(request)
    pip_name = str(body.get("pip") or "").strip()
    if pip_name not in {m["pip"] for m in plugin_manager.MARKET}:
        return {"ok": False, "error": "不在精选市场里"}
    ok, log = await asyncio.to_thread(plugin_manager.install, pip_name)
    if not ok:
        return {"ok": False, "error": f"安装失败：{log[-200:]}"}
    # 本地插件是运行时开关，不用重启；第三方 pip 插件需要重启内核加载
    restarted = False
    if not pip_name.startswith("local:"):
        restarted = await _restart_kernel_if_running()
    return {"ok": True, "restarted": restarted, "log": log[-200:]}


@app.post("/api/plugin/uninstall")
async def api_plugin_uninstall(request: Request) -> dict:
    body = await _body_json(request)
    pip_name = str(body.get("pip") or "").strip()
    ok, log = await asyncio.to_thread(plugin_manager.uninstall, pip_name)
    if not ok:
        return {"ok": False, "error": f"卸载失败：{log[-200:]}"}
    await _restart_kernel_if_running()
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


def _kill_pid(pid: int) -> None:
    if Path("C:/Windows").exists():  # Windows：/T 连子进程树一起收
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True)
    else:
        import os
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


@app.post("/api/bot/stop")
async def api_bot_stop() -> dict:
    await asyncio.to_thread(_stop, "bot")
    # 内核也可能是 start.bat 手动启动的：按端口补刀，保证按钮永远有效
    pid = await asyncio.to_thread(_pid_on_port, BOT_PORT)
    if pid:
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
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(content[-lines:])
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
