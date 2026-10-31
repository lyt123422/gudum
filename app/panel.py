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

from . import (jailbreak, personas, plugin_manager, proxy_manager,
               schedule_core, workbuddy_setup)
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
_spawn_ts: dict[str, float] = {"bot": 0.0, "napcat": 0.0}   # 滤幽灵码：启动 8 秒内的码不展示
NAPCAT_STATE_FILE = ROOT / "data" / "napcat_login_state.json"   # runner 输出检测落盘

# 模型提供商预设与分档档案（设计参考 AstrBot「模型提供商」页：各家独立配置）
PROXY_PROVIDER = "WorkBuddy反代"   # 反代激活时的模型源档案名（评审 v4 反代方案）

PROVIDER_PRESETS: dict[str, dict | None] = {
    "DeepSeek": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    "智谱GLM": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4.7-flash"},
    "通义千问": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "model": "qwen-plus"},
    # moonshot-v1-* 已于 2026-08-31 下线（Kimi 官方模型列表），预设换 kimi-k3.1-pro
    "Kimi": {"base_url": "https://api.moonshot.cn/v1", "model": "kimi-k3.1-pro"},
    "硅基流动": {"base_url": "https://api.siliconflow.cn/v1",
                "model": "deepseek-ai/DeepSeek-V3.2"},
    "Ollama本地": {"base_url": "http://localhost:11434/v1", "model": "qwen3:8b"},
    "自定义": None,
}
PROVIDERS_FILE = ROOT / "data" / "providers.json"
MEM_DIR = ROOT / "data" / "memory"


def _clear_all_memory_files() -> int:
    """清空全部会话记忆文件（面板侧直删；与内核 chat.memory_clear_all 同义）。

    换人格=换说话方式：旧人设的回复留在记忆里会继续影响新人格。
    """
    cleared = 0
    if MEM_DIR.exists():
        for f in MEM_DIR.glob("*.json"):
            try:
                f.unlink()
                cleared += 1
            except OSError:
                pass
    return cleared


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
    # 评审 v4 反代：反代激活时模型源显示为反代名
    if proxy_manager.config().get("active") and \
            base_url.rstrip("/") == proxy_manager.base_url().rstrip("/"):
        return PROXY_PROVIDER
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


def _rotate_log(outfile: Path, max_bytes: int = 5 * 1024 * 1024) -> None:
    """面板托管的进程日志无轮转会无限膨胀（实测 840KB/两周）：超 5MB 留一代 .old。"""
    try:
        if outfile.exists() and outfile.stat().st_size > max_bytes:
            old = outfile.with_suffix(outfile.suffix + ".old")
            if old.exists():
                old.unlink()
            outfile.rename(old)
    except OSError:
        pass   # 文件被占用（进程还在写）就等下次启动再轮转


def _spawn(name: str, module: str, outfile: Path) -> None:
    _stop(name)
    _rotate_log(outfile)
    with outfile.open("a", encoding="utf-8") as fh:
        fh.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} 启动 =====\n")
    fh = outfile.open("a", encoding="utf-8")
    _procs[name] = subprocess.Popen(
        [str(VENV_PY), "-m", *module.split()],
        cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    fh.close()   # 父进程句柄立即关闭（子进程有自己的继承副本），防句柄泄漏
    _spawn_ts[name] = time.time()


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
    proxy_on = bool(proxy_manager.config().get("active"))   # 评审 v4 反代：互斥
    proxy_note = ""
    provider = str(body.get("provider") or "").strip()
    if provider and provider not in PROVIDER_PRESETS \
            and provider != PROXY_PROVIDER:
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
        # 配置审计 F1：归属按地址不按前端高亮——高亮 DeepSeek 却填了 GLM 地址时，
        # 存进 GLM 档案，而不是把 GLM 的 Key/地址覆盖进 DeepSeek 档案
        home = next((name for name, preset in PROVIDER_PRESETS.items()
                     if preset and preset["base_url"].rstrip("/")
                     == base_url.rstrip("/")), None)
        if home and home != provider:
            provider = home
        profiles = _load_profiles()
        old_profile = profiles.get(provider, {})
        requested_base = base_url.rstrip("/")
        old_base = str(old_profile.get("base_url", "")).rstrip("/")
        is_local = base_url.startswith(("http://127.0.0.1", "http://localhost"))
        # 评审 v3 P0-1：换 Base URL 不带新 Key = 把旧 Key 发往新地址，拒绝；
        # 本地地址（Ollama）无需 Key，放行但清掉旧 Key（审计 F3：既放行又不外带）
        keyless_local_switch = (requested_base != old_base
                                and not values.get("DEEPSEEK_API_KEY") and is_local)
        if requested_base != old_base and not values.get("DEEPSEEK_API_KEY") \
                and not is_local:
            return {"ok": False, "error": "更换 Base URL 时必须同时填写 API Key"}
        old_key = str(old_profile.get("api_key", ""))
        had_masked = bool(old_key and _looks_masked(old_key))
        if had_masked:
            old_key = ""   # 评审 v3 P2-7：历史掩码 Key 不可复用
        if not values.get("DEEPSEEK_API_KEY") and requested_base == old_base \
                and not old_key and not is_local:
            # 评审 v4 P2-10：区分"没 Key"和"掩码 Key"
            if had_masked:
                return {"ok": False,
                        "error": "该档案保存的是掩码 Key，请重新输入完整 Key"}
            return {"ok": False,
                    "error": "该提供商还没有可用的 API Key，请先填写"}
        new_key = values.get("DEEPSEEK_API_KEY") or \
            (old_key if requested_base == old_base else "")
        new_model = values.get("DEEPSEEK_MODEL") or old_profile.get("model", "")
        if not new_key and not is_local:
            return {"ok": False,
                    "error": "该提供商还没有可用的 API Key，请先填写（本地 Ollama 除外）"}
        profiles[provider] = {"base_url": base_url, "api_key": new_key,
                              "model": new_model}
        _save_profiles(profiles)
        values["DEEPSEEK_BASE_URL"] = base_url
        if new_key:
            values["DEEPSEEK_API_KEY"] = new_key
        elif keyless_local_switch:
            # 换到本地无 Key 服务：清空 .env 旧 Key，绝不把它发往新地址
            values["DEEPSEEK_API_KEY"] = ""
        values["DEEPSEEK_MODEL"] = new_model
    elif values.get("DEEPSEEK_BASE_URL"):
        # 评审 v3 P0-1 + 审计 F3：换地址不带 Key → 本地地址放行但清掉旧 Key，
        # 非本地地址拒绝——两条路都保证旧 Key 绝不外带
        new_base = values["DEEPSEEK_BASE_URL"].rstrip("/")
        old_base = before.get("DEEPSEEK_BASE_URL", "").rstrip("/")
        if new_base != old_base and not values.get("DEEPSEEK_API_KEY"):
            if new_base.startswith(("http://127.0.0.1", "http://localhost")):
                values["DEEPSEEK_API_KEY"] = ""
            else:
                return {"ok": False, "error": "更换 Base URL 时必须同时填写 API Key"}
    if admin_list:
        values["SUPERUSERS"] = json.dumps(admin_list, ensure_ascii=False)
    if body.get("napcat_password_clear"):
        values["NAPCAT_QUICK_PASSWORD"] = ""   # 审计 F5：留了清除入口，否则密码无法撤掉
    if proxy_on and any(k in values for k in
                        ("DEEPSEEK_BASE_URL", "DEEPSEEK_API_KEY",
                         "DEEPSEEK_MODEL")):
        # 评审 v4 反代互斥 + 审计 F2：直连改动只存档案，模型源保持反代；
        # 反代自身地址的回存（模型页显示的就是反代源）不算直连修改
        direct = {k: v for k, v in values.items()
                  if k in ("DEEPSEEK_BASE_URL", "DEEPSEEK_API_KEY",
                           "DEEPSEEK_MODEL")}
        if direct.get("DEEPSEEK_BASE_URL", "").rstrip("/") \
                == proxy_manager.base_url().rstrip("/"):
            direct.pop("DEEPSEEK_BASE_URL", None)
        if direct:
            if not provider or provider == PROXY_PROVIDER:
                # body 没带 provider（没点胶囊）：按地址归属补存，不再静默丢弃
                home = next((name for name, preset in PROVIDER_PRESETS.items()
                             if preset and preset["base_url"].rstrip("/")
                             == direct.get("DEEPSEEK_BASE_URL", "").rstrip("/")),
                            None) or "自定义"
                profiles = _load_profiles()
                old_profile = profiles.get(home, {})
                profiles[home] = {
                    "base_url": direct.get("DEEPSEEK_BASE_URL")
                    or old_profile.get("base_url", ""),
                    "api_key": direct.get("DEEPSEEK_API_KEY")
                    or old_profile.get("api_key", ""),
                    "model": direct.get("DEEPSEEK_MODEL")
                    or old_profile.get("model", "")}
                _save_profiles(profiles)
                provider = home
            # 审计 F2：同步刷新“切回直连”快照——反代一停，恢复的就是用户最新改的
            # 直连配置，而不是激活反代那天的旧 Key
            cfg_now = proxy_manager.config()
            prev = dict(cfg_now.get("previous") or {})
            for env_key, snap_key in (("DEEPSEEK_BASE_URL", "base_url"),
                                      ("DEEPSEEK_API_KEY", "api_key"),
                                      ("DEEPSEEK_MODEL", "model")):
                if direct.get(env_key):
                    prev[snap_key] = direct[env_key]
            cfg_now["previous"] = prev
            proxy_manager.save_config(cfg_now)
            proxy_note = ("🔀 反代运行中：直连配置已存入「" + provider + "」档案"
                          "并同步切回快照，停止反代后生效")
            values = {k: v for k, v in values.items()
                      if k not in ("DEEPSEEK_BASE_URL", "DEEPSEEK_API_KEY",
                                   "DEEPSEEK_MODEL")}
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
    return {"ok": True, "bot_restarted": bot_restarted,
            "note": proxy_note or None}


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
    cleared = _clear_all_memory_files()
    return {"ok": True, "name": name, "memory_cleared": cleared}


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


@app.get("/api/jailbreak/selftest")
def api_jailbreak_selftest() -> dict:
    """自检：条款能不能拼、状态是否真生效、内核进程里有没有加载破甲。

    存在的意义：本次实测发现"面板改了没生效"的头号原因是内核/面板进程跑的还是旧代码。
    这里把代码版本、内核插件表、条款长度一次摆出来，让这类问题自己暴露。
    """
    lines: list[str] = []
    problems: list[str] = []

    # 1) 条款能否拼出来
    try:
        clause = jailbreak.render_clause()
        has_core = "【回答尺度" in clause
        has_red = "【不可覆盖的红线" in clause
        lines.append(f"· 条款拼接：{len(clause)} 字，"
                     f"含尺度段={has_core}，含红线段={has_red}")
        if not (has_core and has_red):
            problems.append("条款拼接不完整（缺尺度段或红线段）")
    except Exception as exc:
        lines.append(f"· 条款拼接：异常 {exc}")
        problems.append(f"条款拼接异常：{exc}")

    # 2) 状态是否真生效
    st = jailbreak.status()
    lines.append(f"· 状态：插件开关={st['enabled']}，暂停={st['paused']}，"
                 f"实际生效={st['active']}，预设={st['preset']}")
    if st["enabled"] and st["paused"]:
        problems.append("破甲处于暂停状态，不会注入（发「破甲继续」恢复）")
    if not st["enabled"]:
        problems.append("破甲插件开关是关闭的，不会注入（点「启动破甲」）")

    # 3) 内核进程里有没有这个模块（面板与内核是两个进程，改代码不重启就白改）
    kernel: dict = {}
    try:
        resp = httpx.get(f"http://127.0.0.1:{BOT_PORT}/healthz", timeout=2.0)
        kernel = resp.json() if resp.status_code == 200 else {}
    except Exception as exc:
        lines.append(f"· 内核：连不上（{str(exc)[:60]}）—— 内核没运行时破甲自然不生效")
        problems.append("内核没在运行")
    if kernel:
        plugins = kernel.get("plugins") or []
        jb = kernel.get("jailbreak")
        lines.append(f"· 内核：已加载插件 {plugins}")
        if jb is None:
            lines.append("· 内核破甲字段：缺失 —— 内核跑的是旧代码，需要重启内核")
            problems.append("内核进程是旧代码（healthz 没有 jailbreak 字段），重启内核才能加载破甲")
        else:
            lines.append(f"· 内核破甲状态：{jb}")
            if isinstance(jb, dict) and jb.get("active") is not True:
                problems.append("内核侧破甲未生效（看上面内核破甲状态）")
        model = (kernel.get("deepseek") or {}).get("model")
        if model:
            lines.append(f"· 当前模型：{model}")
            if "deepseek" not in str(model).lower() and "gemini" in str(model).lower():
                lines.append("  提示：走的是第三方/代理模型，服务端可能有自己的内容策略，"
                             "破甲只能改本机注入的提示词")

    # 4) 管理员范围
    try:
        env = load_env(ENV_FILE)
        scope = (env.get("JAILBREAK_SCOPE") or "admin").strip().lower()
        supers = env.get("SUPERUSERS", "")
        lines.append(f"· 生效范围：{scope}（admin=仅管理员）；SUPERUSERS={supers}")
        if scope == "admin" and supers in ("", '["10000"]'):
            problems.append("还没有真实管理员（SUPERUSERS 仍是占位 10000），"
                            "admin 范围下谁都不会生效")
    except Exception as exc:
        lines.append(f"· 配置读取失败：{exc}")

    return {
        "ok": not problems,
        "report": "\n".join(lines + (["", "发现的问题："] + [f"！{p}" for p in problems]
                                    if problems else ["", "没有发现问题。"])),
        "clause_preview": jailbreak.render_clause()[:300],
    }


@app.get("/api/jailbreak")
async def api_jailbreak() -> dict:
    """破甲状态：开关 / 暂停 / 预设 / 可选预设列表。"""
    return jailbreak.status()


@app.post("/api/jailbreak/set")
async def api_jailbreak_set(request: Request) -> dict:
    """改破甲：enabled / paused / preset 三个字段，传哪个改哪个。"""
    body = await _body_json(request)
    if "enabled" in body:
        jailbreak.set_enabled(bool(body["enabled"]))
    if "paused" in body:
        jailbreak.set_paused(bool(body["paused"]))
    if "preset" in body:
        name = str(body["preset"] or "").strip()
        if name and not jailbreak.set_preset(name):
            return {"ok": False, "error": f"没有这个预设：{name}"}
    return {"ok": True, **jailbreak.status()}


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


# ---------- 反代（WorkBuddy）----------

@app.get("/api/proxy")
async def api_proxy() -> dict:
    """反代状态 + 模型列表（Key 只用不回传）。"""
    st = await asyncio.to_thread(proxy_manager.status)
    cfg = await asyncio.to_thread(proxy_manager.config)
    key = cfg.get("api_key") or proxy_manager.key_from_settings()
    health = await proxy_manager.health() if st["running"] else None
    accounts = int((health or {}).get("accounts") or 0)
    accounts_ready = int((health or {}).get("accounts_ready") or 0)
    models: list[str] = []
    if st["running"]:
        try:
            models = await proxy_manager.models(key)
        except Exception:
            models = []
    install = await asyncio.to_thread(workbuddy_setup.status, cfg.get("path"))
    return {"status": st,
            "config": {"path": cfg.get("path", ""), "port": cfg.get("port", 8788),
                       "model": cfg.get("model", "")},
            "models": models,
            "accounts": accounts,
            "accounts_ready": accounts_ready,
            "api_key_ready": bool(key),
            "install": install,
            "active_provider": _live_provider_name(load_env(ENV_FILE).get(
                "DEEPSEEK_BASE_URL", ""))}


@app.post("/api/proxy/config")
async def api_proxy_config(request: Request) -> dict:
    body = await _body_json(request)
    cfg = await asyncio.to_thread(proxy_manager.config)
    path = str(body.get("path") or "").strip()
    if path:
        found = await asyncio.to_thread(workbuddy_setup.locate, path)
        if not found:
            return {"ok": False, "error": f"路径下没有 wb_proxy.py: {path}"}
        cfg["path"] = found
    port = str(body.get("port") or "").strip()
    if port:
        if not port.isdigit():
            return {"ok": False, "error": "端口必须是数字"}
        cfg["port"] = int(port)
    api_key = str(body.get("api_key") or "").strip()
    if api_key and not _looks_masked(api_key):
        cfg["api_key"] = api_key   # 手填优先；留空=自动读 settings.json
    proxy_manager.save_config(cfg)
    return {"ok": True, "path": cfg.get("path", ""),
            "install": await asyncio.to_thread(
                workbuddy_setup.status, cfg.get("path"))}


@app.post("/api/proxy/install")
async def api_proxy_install() -> dict:
    """下载并安装 WorkBuddy-Hub 到 data/workbuddy-hub（已有安装直接复用）。"""
    cfg = proxy_manager.config()
    try:
        info = await asyncio.to_thread(
            workbuddy_setup.ensure_installed, cfg.get("path"))
    except workbuddy_setup.SetupError as exc:
        return {"ok": False, "error": str(exc),
                "install": await asyncio.to_thread(
                    workbuddy_setup.status, cfg.get("path"))}
    cfg["path"] = info["path"]
    proxy_manager.save_config(cfg)
    return {"ok": True, **info}


@app.post("/api/proxy/start")
async def api_proxy_start() -> dict:
    """启动反代 → 健康校验 → 自动接入（写档案+切模型源+重启内核）。"""
    ok, msg = await asyncio.to_thread(proxy_manager.start)
    if not ok:
        return {"ok": False, "error": msg,
                "install": await asyncio.to_thread(
                    workbuddy_setup.status,
                    proxy_manager.config().get("path"))}
    cfg = await asyncio.to_thread(proxy_manager.config)
    key = cfg.get("api_key") or proxy_manager.key_from_settings()
    health = await proxy_manager.health()
    accounts_ready = int((health or {}).get("accounts_ready") or 0)
    if not accounts_ready:
        return {"ok": True, "activated": False, "models": [],
                "accounts_ready": 0,
                "note": "反代已运行，但还没有可用账号：先点「🌐 登录 WorkBuddy」"
                        "登录账号，就绪后会自动接入"}
    models: list[str] = []
    try:
        models = await proxy_manager.models(key)
    except Exception:
        models = []
    if not models:
        return {"ok": True, "activated": False, "models": models,
                "accounts_ready": accounts_ready,
                "note": "反代已启动，但还没法接入：确认账号已就绪后重试"}
    # 反代列表里没有的模型名不要直接激活，否则聊天时才发现 404
    if cfg.get("model") and cfg["model"] not in models:
        return {"ok": True, "activated": False, "models": models,
                "note": f"当前模型 {cfg['model']} 不在反代列表，"
                        "请在反代页重新选择（例如 deepseek-v4.1-flash）"}
    before = load_env(ENV_FILE)
    if not cfg.get("previous"):
        cfg["previous"] = {"base_url": before.get("DEEPSEEK_BASE_URL", ""),
                           "api_key": before.get("DEEPSEEK_API_KEY", ""),
                           "model": before.get("DEEPSEEK_MODEL", "")}
    cfg["model"] = cfg.get("model") or models[0]
    cfg["active"] = True
    proxy_manager.save_config(cfg)
    profiles = _load_profiles()
    profiles[PROXY_PROVIDER] = {"base_url": proxy_manager.base_url(),
                                "api_key": key, "model": cfg["model"]}
    _save_profiles(profiles)
    update_env({"DEEPSEEK_BASE_URL": proxy_manager.base_url(),
                "DEEPSEEK_API_KEY": key,
                "DEEPSEEK_MODEL": cfg["model"]})
    restarted = await _restart_kernel_if_running()
    return {"ok": True, "activated": True, "models": models,
            "model": cfg["model"], "restarted": restarted}


@app.post("/api/proxy/stop")
async def api_proxy_stop() -> dict:
    ok, msg = await asyncio.to_thread(proxy_manager.stop)
    cfg = proxy_manager.config()
    prev = cfg.get("previous") or {}
    if prev.get("base_url"):
        try:
            update_env({"DEEPSEEK_BASE_URL": prev["base_url"],
                        "DEEPSEEK_API_KEY": prev.get("api_key", ""),
                        "DEEPSEEK_MODEL": prev.get("model", "")})
        except EnvValueError:
            pass
    cfg["active"] = False
    cfg["previous"] = None
    proxy_manager.save_config(cfg)
    restarted = await _restart_kernel_if_running()
    detail = msg if not ok else (msg + "；已切回直连模型源")
    return {"ok": True, "restarted": restarted, "detail": detail}


@app.post("/api/proxy/model")
async def api_proxy_model(request: Request) -> dict:
    body = await _body_json(request)
    model = str(body.get("model") or "").strip()
    if not model:
        return {"ok": False, "error": "模型名不能为空"}
    if not re.fullmatch(r"[\w.:/-]{1,80}", model):
        return {"ok": False, "error": "模型名只能是字母/数字/-_.:/，不要带空格或换行"}
    cfg = proxy_manager.config()
    # 反代在跑时严格校验模型 ID；防止把 deepseek4.1flash 这种不存在的写法
    # 写进去，直到聊天才报 404。本地无 Key 且网关未开鉴权时也能查。
    if await asyncio.to_thread(proxy_manager.is_running):
        try:
            available = await proxy_manager.models(
                cfg.get("api_key") or proxy_manager.key_from_settings())
        except Exception:
            available = []
        if available and model not in available:
            sample = "、".join(available[:8])
            return {"ok": False,
                    "error": f"模型 {model} 不在反代 /v1/models；可用：{sample}…"}
    cfg["model"] = model
    proxy_manager.save_config(cfg)
    if not cfg.get("active"):
        return {"ok": True, "restarted": False}
    try:
        update_env({"DEEPSEEK_MODEL": model})
    except EnvValueError as exc:
        return {"ok": False, "error": str(exc)}
    profiles = _load_profiles()
    profile = profiles.get(PROXY_PROVIDER)
    if isinstance(profile, dict):
        profile["model"] = model
        profiles[PROXY_PROVIDER] = profile
        _save_profiles(profiles)
    was_running = _running("bot") or (await asyncio.to_thread(
        _pid_on_port, BOT_PORT)) is not None
    restarted = await _restart_kernel_if_running() if was_running else False
    if was_running and not restarted:
        return {"ok": False,
                "error": "模型已写入配置，但内核重启失败；请到运行控制手动重启"}
    return {"ok": True, "restarted": restarted}


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
    _atomic_write(path, raw if raw.endswith("\n") else raw + "\n")
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
            _clear_all_memory_files()   # 回落=换了人格：记忆一并带走
    return {"ok": True}


# ---------- 定时任务（面板配置端，纯 core 不依赖 nonebot） ----------

async def _kernel_schedule_running() -> bool:
    """向内核 healthz 询问调度器状态；内核没起就返回 False。"""
    try:
        async with httpx.AsyncClient(timeout=0.6) as client:
            resp = await client.get(f"http://127.0.0.1:{BOT_PORT}/healthz")
            return bool((resp.json().get("schedule") or {}).get("running"))
    except Exception:
        return False


@app.get("/api/schedule")
async def api_schedule() -> dict:
    tasks = await asyncio.to_thread(schedule_core.merged_tasks)
    return {"ok": True, "tasks": tasks,
            "running": await _kernel_schedule_running(),
            "max_tasks": schedule_core.MAX_TASKS}


@app.post("/api/schedule/save")
async def api_schedule_save(request: Request) -> dict:
    body = await _body_json(request)
    name = str(body.get("name") or "").strip()
    cron = str(body.get("cron") or "").strip()
    content = str(body.get("content") or "").strip()
    target = str(body.get("target") or "").strip()
    task_type = "private" if str(body.get("type")) == "private" else "group"
    raw_enabled = body.get("enabled", True)
    enabled = raw_enabled if isinstance(raw_enabled, bool) else \
        str(raw_enabled).lower() in ("true", "1", "yes", "on")
    # 前端已剥前缀，但直接调 API / 手工填表也可能带，服务端再兜一层
    if target.lower().startswith("friend:"):
        task_type, target = "private", target.split(":", 1)[1].strip()
    existing = await asyncio.to_thread(schedule_core.load_tasks)
    if name and any(t["name"] == name for t in existing):
        ok, msg = await asyncio.to_thread(
            schedule_core.update_task, name, cron=cron, content=content,
            target=target, type=task_type, enabled=enabled)
    else:
        ok, msg = await asyncio.to_thread(
            schedule_core.add_task, name, cron, content, target, task_type, enabled)
    return {"ok": ok, "error": "" if ok else msg, "msg": msg,
            "tasks": await asyncio.to_thread(schedule_core.merged_tasks)}


@app.post("/api/schedule/delete")
async def api_schedule_delete(request: Request) -> dict:
    body = await _body_json(request)
    name = str(body.get("name") or "").strip()
    ok, msg = await asyncio.to_thread(schedule_core.remove_task, name)
    return {"ok": ok, "error": "" if ok else msg, "msg": msg,
            "tasks": await asyncio.to_thread(schedule_core.merged_tasks)}


@app.post("/api/schedule/toggle")
async def api_schedule_toggle(request: Request) -> dict:
    body = await _body_json(request)
    name = str(body.get("name") or "").strip()
    enabled = bool(body.get("enabled"))
    ok, msg = await asyncio.to_thread(schedule_core.set_enabled, name, enabled)
    return {"ok": ok, "error": "" if ok else msg, "msg": msg,
            "tasks": await asyncio.to_thread(schedule_core.merged_tasks)}


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


def _kill_orphan_napcat() -> None:
    """清理不受面板管理的协议端残留（按命令行匹配进程树）。

    旧面板死亡/重复启动会留下双实例：两个 NapCat 同时登一个号会互踢，
    内核日志表现就是连续 "WebSocket closed by peer"（2026-10-31 11:47 实测）。
    powershell 自身命令行也含匹配词，用 $PID 排除。
    """
    ps = ("Get-CimInstance Win32_Process | Where-Object { "
          "$_.ProcessId -ne $PID -and $_.CommandLine -match "
          "'app\\.napcat_runner|napcat\\\\node\\.exe' } | ForEach-Object { "
          "& taskkill /F /T /PID $_.ProcessId 2>$null | Out-Null }")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=15)
    except Exception:   # noqa: BLE001
        pass


async def _restart_napcat(account: str = "", qr: bool = False) -> None:
    """停止 → 清孤儿实例 → 按当前 .env 重生成反向 WS 配置 → 拉起协议端。

    account 必须是目标号：NapCat 以 -q <目标号> 启动，快登有效直接连上，
    失效/无记录自动回落到二维码（谁扫登录谁）。
    """
    await asyncio.to_thread(_stop, "napcat")
    await asyncio.to_thread(_kill_orphan_napcat)
    await asyncio.to_thread(
        subprocess.run,
        [str(VENV_PY), "-m", "app.napcat_config"], cwd=ROOT, capture_output=True)
    spec = f"app.napcat_runner --account {account}".strip()
    await asyncio.to_thread(_spawn, "napcat", spec, NAPCAT_OUT)


@app.post("/api/napcat/start")
async def api_napcat_start() -> dict:
    env = load_env(ENV_FILE)
    await _restart_napcat(env.get("NAPCAT_ACCOUNT", ""))
    return {"ok": True}


def _napcat_accounts() -> list[str]:
    """NapCat 历史登录过的号：内外两层 config 目录里的按号配置文件。"""
    accounts: set[str] = set()
    for cfg_dir in (ROOT / "napcat" / "napcat" / "config", ROOT / "napcat" / "config"):
        if not cfg_dir.exists():
            continue
        for pattern in ("napcat_*.json", "onebot11_*.json"):
            for f in cfg_dir.glob(pattern):
                uin = f.stem.split("_", 1)[1]
                if uin.isdigit():
                    accounts.add(uin)
    return sorted(accounts)


@app.get("/api/napcat/accounts")
async def api_napcat_accounts() -> dict:
    """可换的号列表：历史登录记录 + 当前配置 + 实际在线（healthz 为准）。"""
    env = load_env(ENV_FILE)
    configured = env.get("NAPCAT_ACCOUNT", "").strip()
    online = ""
    try:
        async with httpx.AsyncClient(timeout=0.6) as client:
            resp = await client.get(f"http://127.0.0.1:{BOT_PORT}/healthz")
            info = ((resp.json().get("onebot") or {}).get("login_info") or {})
            online = str(info.get("user_id") or "")
    except Exception:
        pass
    accounts = _napcat_accounts()
    for a in (configured, online):
        if a.isdigit() and a not in accounts:
            accounts.append(a)
            accounts.sort()
    return {"ok": True,
            "accounts": [{"qq": a, "configured": a == configured,
                          "online": a == online} for a in accounts],
            "configured": configured, "online": online}


@app.post("/api/napcat/switch")
async def api_napcat_switch(request: Request) -> dict:
    """换号 / 扫码登录指定号：写配置 → 清孤儿 → 重启协议端（-q 目标号）。

    目标号快登有效则直接连上；失效/没登录过则 NapCat 自动出二维码——
    二维码是「谁扫谁登录」，不看配置写的号，所以前端必须提示用目标号的手机扫。
    实测（2026-10-31 日志）：不带 -q 启动时 NapCat 发现本机有快登记录会直接
    自动登录当前号、二维码不出现——所以扫码登录也必须指定目标号。
    """
    body = await _body_json(request)
    qq = str(body.get("qq") or "").strip()
    if not qq.isdigit():
        return {"ok": False, "error": "QQ 号必须是纯数字"}
    try:
        update_env({"NAPCAT_ACCOUNT": qq})
    except EnvValueError as exc:
        return {"ok": False, "error": str(exc)}
    await _restart_napcat(qq)
    # 换号结果探测：快登成功 / 出二维码 / 超时，给用户一句明确的话
    result = ""
    for _ in range(15):
        await asyncio.sleep(1)
        result = await _napcat_probe(qq)
        if result:
            break
    if result == "online":
        note = f"✅ 已切换到 {qq} 并直接连上，无需扫码"
    elif result == "waiting_qr":
        note = (f"📷 二维码已生成：用 {qq} 的手机到「🚀 运行控制」页扫码"
                "（约 2 分钟有效，手机上点「授权登录」）")
    else:
        note = (f"已切换到 {qq}，15 秒内未检测到登录或出码："
                " NapCat 可能还在启动，稍后看「🚀 运行控制」页状态或日志")
    return {"ok": True, "qq": qq, "result": result or "timeout", "note": note}


@app.post("/api/napcat/stop")
async def api_napcat_stop() -> dict:
    await asyncio.to_thread(_stop, "napcat")   # taskkill 阻塞调用入线程池（评审 P2-11）
    return {"ok": True}


def _napcat_qr_waiting() -> bool:
    """runner 在协议端输出里检测到「快登失败→二维码回落」才置真——
    快登成功路径里 NapCat 顺手刷新出来的码（幽灵码）不算。5 分钟内有效。"""
    try:
        data = json.loads(NAPCAT_STATE_FILE.read_text(encoding="utf-8"))
        return (data.get("phase") == "waiting_qr"
                and time.time() - float(data.get("ts") or 0) < 300)
    except (OSError, ValueError):
        return False


async def _napcat_probe(qq: str) -> str:
    """探测换号结果：online（内核已连上目标号）/ waiting_qr（已出码）/ 空。"""
    try:
        async with httpx.AsyncClient(timeout=0.6) as client:
            resp = await client.get(f"http://127.0.0.1:{BOT_PORT}/healthz")
            info = ((resp.json().get("onebot") or {}).get("login_info") or {})
            if str(info.get("user_id") or "") == qq:
                return "online"
    except Exception:   # noqa: BLE001
        pass
    return "waiting_qr" if _napcat_qr_waiting() else ""


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
    mem_dir = MEM_DIR
    napcat_up = round(time.time() - _spawn_ts["napcat"], 1) \
        if _running("napcat") and _spawn_ts["napcat"] else 0
    return {"bot_running": _running("bot") or kernel_up,
            "napcat_running": _running("napcat"),
            "qq_connected": qq_connected,
            "logged_in_qq": logged_in_uid,
            "claim_code": claim_code,
            "api_key_set": bool(env.get("DEEPSEEK_API_KEY")),
            "memory_sessions": len(list(mem_dir.glob("*.json"))) if mem_dir.exists() else 0,
            "bot_port": BOT_PORT,
            "napcat_uptime": napcat_up,
            "qr_waiting": _napcat_qr_waiting()}


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
