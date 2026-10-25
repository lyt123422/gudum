"""DeepSeek QQ 机器人入口：NoneBot2 + OneBot v11（NapCat 反向 WebSocket 接入）。"""
import asyncio
import os
import time
from pathlib import Path

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

from .log_setup import log_startup_banner, setup_logging
from .plugin_manager import enabled_thirdparty

setup_logging()
nonebot.init()
driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)
nonebot.load_plugins(str(Path(__file__).resolve().parent / "plugins"))
# 市场安装的第三方插件：按 plugins.json 的启用名单动态加载
for module_name in enabled_thirdparty():
    try:
        nonebot.load_plugin(module_name)
    except Exception as exc:
        nonebot.logger.warning("第三方插件加载失败 {}: {}", module_name, exc)

_STARTED_AT = time.time()
log_startup_banner(sorted(plug.name for plug in nonebot.get_loaded_plugins()))


def _register_healthz() -> None:  # 仅本机排障用；HOST 已限定 127.0.0.1
    from nonebot import get_app

    app = get_app()

    @app.get("/healthz")
    async def healthz() -> dict:
        bots = nonebot.get_bots()
        payload = {
            "pid": os.getpid(),
            "uptime_seconds": round(time.time() - _STARTED_AT, 1),
            "plugins": sorted(plug.name for plug in nonebot.get_loaded_plugins()),
            "onebot": {
                "connected": len(bots),
                "bots": [{"type": b.type, "id": b.self_id} for b in bots.values()],
            },
        }
        # 登录账号信息：面板靠它自动回写 NAPCAT_ACCOUNT（扫码用的是哪个号）
        if bots:
            bot = next(iter(bots.values()))
            try:
                # 评审 v3 P2-21：NapCat 卡住时 healthz 不能被拖死
                info = await asyncio.wait_for(bot.get_login_info(), timeout=1.0)
                payload["onebot"]["login_info"] = {
                    "user_id": info.get("user_id"),
                    "nickname": info.get("nickname"),
                }
            except Exception as exc:
                payload["onebot"]["login_info_error"] = str(exc)[:120]
        try:
            from .plugins import chat as _chat
            payload["deepseek"] = {
                "model": _chat.client.model,
                "api_key_set": bool(_chat._cfg("DEEPSEEK_API_KEY", "")),
                "memory_sessions": _chat.memory_count(),
            }
        except Exception as exc:
            payload["deepseek"] = {"error": str(exc)[:120]}
        try:
            from .plugins import schedule as _schedule
            from . import plugin_manager as _pm
            payload["schedule"] = {
                "running": _schedule.is_running(),
                "enabled": _pm.is_enabled("schedule"),
            }
        except Exception as exc:
            payload["schedule"] = {"running": False, "error": str(exc)[:120]}
        try:
            from . import jailbreak as _jb
            payload["jailbreak"] = _jb.status()
        except Exception as exc:
            payload["jailbreak"] = {"error": str(exc)[:120]}
        return payload


if __name__ == "__main__":
    _register_healthz()
    nonebot.run()
