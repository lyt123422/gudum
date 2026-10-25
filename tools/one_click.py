"""一键启动：拉起控制台，按 .env 的配置自动串联内核与 QQ 协议端。

由 ``一键启动.bat`` 调用。控制台已经运行时不会重复启动；已经运行的内核/QQ
也不会被重启。首次没有配置 Key/QQ 时只打开控制台，等用户填完再启动。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PANEL_URL = "http://127.0.0.1:8090"


def _get_status(timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(PANEL_URL + "/api/status",
                                    timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def _get_proxy(timeout: float = 3.0) -> dict | None:
    try:
        with urllib.request.urlopen(PANEL_URL + "/api/proxy",
                                    timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def _post(path: str, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(
        PANEL_URL + path, data=b"{}", method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except OSError:
            body = ""
        return {"ok": False, "error": f"HTTP {exc.code} {body[:160]}"}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)[:160]}


def _start_panel() -> None:
    env = dict(os.environ)
    env["PANEL_NO_OPEN"] = "1"   # 由本脚本统一打开浏览器，避免开两个
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    subprocess.Popen([sys.executable, "-m", "app.panel"], cwd=ROOT, env=env,
                     creationflags=flags)


def _wait_panel(seconds: int = 60) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _get_status(timeout=1.0) is not None:
            return True
        time.sleep(1)
    return False


def _env() -> dict[str, str]:
    try:
        from app.envutil import load_env
        return load_env(ROOT / ".env")
    except Exception:
        return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="gudum 一键启动")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不启动进程")
    args = parser.parse_args(argv)

    status = _get_status()
    if status is None:
        if args.dry_run:
            print("[dry-run] 控制台未运行，将启动 app.panel 并等待 8090")
            return 0
        print("正在启动控制台…")
        _start_panel()
        if not _wait_panel():
            print("控制台启动超时：请看控制台窗口/日志，或手动运行 控制台.bat")
            return 1
        status = _get_status() or {}
    print(f"控制台已就绪：{PANEL_URL}")
    webbrowser.open(PANEL_URL)

    env = _env()
    key = (env.get("DEEPSEEK_API_KEY") or "").strip()
    account = (env.get("NAPCAT_ACCOUNT") or "").strip()
    bot_running = bool(status.get("bot_running"))
    qq_connected = bool(status.get("qq_connected"))

    if not key:
        print("未配置 DEEPSEEK_API_KEY：控制台已打开，请先填模型 Key。")
    elif bot_running:
        print("机器人内核已在运行，跳过启动。")
    elif args.dry_run:
        print("[dry-run] 将启动机器人内核")
    else:
        result = _post("/api/bot/start")
        print("机器人内核：" + ("已启动" if result.get("ok") else
                               "启动失败 " + str(result.get("error") or "")))

    napcat_exe = ROOT / "napcat" / "node.exe"
    if not account:
        print("未配置 NAPCAT_ACCOUNT：控制台已打开，请填 QQ 小号（或扫码后自动回写）。")
    elif qq_connected:
        print("QQ 已连接，跳过协议端启动。")
    elif not napcat_exe.exists():
        print("未找到 napcat/node.exe：请使用发布包，或按 README 单独安装 NapCat。")
    elif args.dry_run:
        print("[dry-run] 将启动 QQ 协议端")
    else:
        result = _post("/api/napcat/start")
        print("QQ 协议端：" + ("已启动，请扫码/自动登录" if result.get("ok") else
                               "启动失败 " + str(result.get("error") or "")))

    # 反代只在用户以前激活过时才自动拉起；全新安装不打扰用户
    proxy = _get_proxy()
    proxy_cfg = (proxy or {}).get("config") or {}
    proxy_status = (proxy or {}).get("status") or {}
    if proxy_cfg.get("active") and not proxy_status.get("running"):
        if args.dry_run:
            print("[dry-run] 将启动 WorkBuddy 反代")
        else:
            result = _post("/api/proxy/start", timeout=60)
            print("WorkBuddy 反代：" + ("已启动" if result.get("ok") else
                                       "启动失败 " + str(result.get("error") or "")))
    elif proxy_status.get("running"):
        print("WorkBuddy 反代已在运行。")

    print("完成。控制台窗口保持打开；关掉它 = 控制台退出。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
