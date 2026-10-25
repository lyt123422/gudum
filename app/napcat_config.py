"""按 .env 生成 NapCat 的反向 WS 配置。

NapCat 首次登录一个新 QQ 号时不会自动带网络配置，机器人会连不上；
启动协议端前跑一次本脚本，把令牌/端口写进 napcat/config/onebot11_<QQ号>.json。
"""
from __future__ import annotations

import json
from pathlib import Path

from .envutil import load_env

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    env = load_env(ROOT / ".env")
    account = (env.get("NAPCAT_ACCOUNT") or "").strip()
    token = (env.get("ONEBOT_ACCESS_TOKEN") or "").strip()
    port = (env.get("PORT") or "8080").strip()
    if not account:
        print("⚠ 未配置 NAPCAT_ACCOUNT，跳过配置生成"
              "（扫码登录后需在 NapCat WebUI 手动添加反向 WS）")
        return 0
    # NapCat Shell 实际读取的是内层 napcat/napcat/config/（实测踩坑）。
    # 扫码可能登录任意历史账号：内层出现过的所有账号配置全部改写，
    # 保证扫哪个号都能连上正确端口。
    inner = ROOT / "napcat" / "napcat" / "config"
    accounts = {account}
    if inner.exists():
        for old in inner.glob("onebot11_*.json"):
            uin = old.stem.split("_", 1)[1]
            if uin.isdigit():
                accounts.add(uin)
    url = f"ws://127.0.0.1:{port}/onebot/v11/ws"
    payload = {
        "network": {
            "websocketServers": [],
            "websocketClients": [{
                "enable": True,
                "name": "deepseek-qq-bot",
                "url": url + (f"?access_token={token}" if token else ""),
                "messagePostFormat": "array",
                "reportSelfMessage": False,
                "token": token,
                "debug": False,
                "heartInterval": 30000,
                "reconnectInterval": 5000,
            }],
            "httpServers": [],
            "httpClients": [],
            "plugins": [],
        },
        "musicSignUrl": "",
        "enableLocalFile2Url": False,
        "parseMultMsg": False,
    }
    written: set[str] = set()
    for uin in sorted(accounts):
        for cfg_dir in (inner, ROOT / "napcat" / "config"):
            cfg_dir.mkdir(parents=True, exist_ok=True)
            (cfg_dir / f"onebot11_{uin}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            written.add(uin)
    print(f"✓ 已为账号 [{'/'.join(sorted(written))}] 写入反向 WS 配置 → {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
