"""WorkBuddy 反代管理器：启动/停止本地模型网关 + 健康探活 + 守护。

WorkBuddy2API-Hub（F:\\WorkBuddy-Hub，MIT）把腾讯 workbuddy.ai / codebuddy.cn
封装成 OpenAI 兼容接口。gudum 把它当"本地模型提供商"托管：

- 启动：端口已在监听则识别为运行中（含用户手动/看板启动），不重复拉起
- 健康判定：GET /health（ok=true）
- Key：默认自动读 accounts/settings.json 里第一个启用的 api_keys[].key
- 激活/切回（模型源切换）在 panel.py——本模块只管进程与健康

线程模型：守护线程 60 秒探活；反代模式激活期间挂掉自动拉起，
连续 3 次失败置 last_error 供面板红色告警（不做自动切回直连——
避免静默烧用户直连 Key 的额度，由用户手动切）。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import httpx
from nonebot.log import logger

ROOT = Path(__file__).resolve().parents[1]
PROXY_FILE = ROOT / "data" / "proxy.json"

DEFAULT_PATH = r"F:\WorkBuddy-Hub"
DEFAULT_PORT = 8788

_state: dict = {"proc": None, "watchdog": None,
                "last_error": "", "failures": 0}
_lock = threading.Lock()


# ---------- 配置（data/proxy.json） ----------

def config() -> dict:
    try:
        data = json.loads(PROXY_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("path", DEFAULT_PATH)
            data.setdefault("port", DEFAULT_PORT)
            data.setdefault("api_key", "")
            data.setdefault("model", "")
            data.setdefault("active", False)
            data.setdefault("previous", None)
            return data
    except (OSError, ValueError):
        pass
    return {"path": DEFAULT_PATH, "port": DEFAULT_PORT, "api_key": "",
            "model": "", "active": False, "previous": None}


def save_config(cfg: dict) -> None:
    PROXY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROXY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PROXY_FILE)


def base_url() -> str:
    return f"http://127.0.0.1:{config()['port']}/v1"


# ---------- Key / 健康 / 模型 ----------

def key_from_settings(path: Path | None = None) -> str:
    """从 WorkBuddy 的 accounts/settings.json 读第一个启用且未删除的 Key。"""
    cfg = config()
    p = path or (Path(cfg["path"]) / "accounts" / "settings.json")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        for item in data.get("api_keys") or []:
            if isinstance(item, dict) and item.get("enabled") \
                    and not item.get("deleted_at") and item.get("key"):
                return str(item["key"])
    except (OSError, ValueError):
        pass
    return ""


def _client(timeout: float = 3.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout,
                             headers={"User-Agent": "gudum-proxy-manager"})


async def health() -> dict | None:
    """GET /health → 解析 JSON；不通返回 None。"""
    try:
        async with _client() as client:
            resp = await client.get(f"http://127.0.0.1:{config()['port']}/health")
            if resp.status_code == 200 and isinstance(resp.json(), dict):
                return resp.json()
    except Exception:
        pass
    return None


async def models(api_key: str) -> list[str]:
    """GET /v1/models → 模型 id 列表（需要 Bearer Key）。"""
    async with _client(timeout=8.0) as client:
        resp = await client.get(
            f"http://127.0.0.1:{config()['port']}/v1/models",
            headers={"Authorization": f"Bearer {api_key}"})
        resp.raise_for_status()
        data = resp.json()
    return sorted(str(m.get("id")) for m in data.get("data", [])
                  if isinstance(m, dict) and m.get("id"))


# ---------- 进程 ----------

def _port_listening(port: int) -> bool:
    try:
        result = subprocess.run(["netstat", "-ano"], capture_output=True,
                                text=True, errors="replace", timeout=5)
        for line in (result.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[-1].isdigit() \
                    and parts[0].upper() == "TCP" and "LISTENING" in parts[3].upper():
                local = parts[1]
                if local.endswith(f":{port}"):
                    return True
    except Exception:
        pass
    return False


def _python_exe(proxy_path: Path) -> str:
    bundled = proxy_path / "python" / "python.exe"
    if bundled.exists():
        return str(bundled)
    import sys
    return sys.executable   # wb_proxy 仅标准库，gudum 的 venv python 即可


def _running_mine() -> bool:
    proc = _state["proc"]
    return bool(proc and proc.poll() is None)


def is_running() -> bool:
    """端口在监听即视为运行中（含用户手动/看板启动的外部实例）。"""
    return _port_listening(config()["port"])


def start() -> tuple[bool, str]:
    with _lock:
        if is_running():
            return True, "反代已在运行（检测到端口监听），直接可用"
        cfg = config()
        proxy_path = Path(cfg["path"])
        script = proxy_path / "wb_proxy.py"
        if not script.exists():
            return False, f"没找到 {script}：请确认反代路径设置"
        exe = _python_exe(proxy_path)
        logfile = (ROOT / "data" / "proxy.log").open("a", encoding="utf-8")
        logfile.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 =====\n")
        try:
            _state["proc"] = subprocess.Popen(
                [exe, str(script), "--port", str(cfg["port"])],
                cwd=proxy_path, stdout=logfile, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as exc:   # noqa: BLE001
            logfile.close()
            return False, f"启动失败：{str(exc)[:150]}"
        finally:
            logfile.close()
        deadline = time.time() + 15
        while time.time() < deadline:
            if _port_listening(cfg["port"]):
                _start_watchdog()
                return True, f"反代已启动（端口 {cfg['port']}）"
            if _state["proc"].poll() is not None:
                return False, "反代进程启动后立即退出，请看 data/proxy.log"
            time.sleep(0.5)
        return False, "启动超时（15 秒内端口未监听），请看 data/proxy.log"


def stop() -> tuple[bool, str]:
    proc = _state["proc"]
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:   # noqa: BLE001
            try:
                proc.kill()
            except Exception:
                pass
        _state["proc"] = None
        return True, "反代已停止"
    if is_running():
        return False, ("反代是外部启动的实例（非本面板拉起），"
                       "请到 WorkBuddy 窗口/看板停止，或任务管理器结束 wb_proxy 进程")
    return True, "本来就没在运行"


def _start_watchdog() -> None:
    if _state["watchdog"] and _state["watchdog"].is_alive():
        return

    def _watch():
        while config().get("active"):
            time.sleep(60)
            if not config().get("active"):
                break
            if is_running():
                _state["failures"] = 0
                continue
            _state["failures"] += 1
            logger.warning("反代探活失败({}/3)，尝试自动拉起", _state["failures"])
            ok, msg = start()
            if not ok:
                if _state["failures"] >= 3:
                    _state["last_error"] = f"反代连续拉起失败: {msg[:120]}"
                    logger.warning("{}", _state["last_error"])

    _state["watchdog"] = threading.Thread(target=_watch, daemon=True)
    _state["watchdog"].start()


def status() -> dict:
    cfg = config()
    running = is_running()
    external = running and not _running_mine()
    return {"running": running, "external": external,
            "active": bool(cfg.get("active")),
            "model": cfg.get("model", ""),
            "port": cfg["port"], "path": cfg["path"],
            "last_error": _state["last_error"]}
