"""应用日志接入与凭证脱敏；库模块仍使用标准 logging。

落盘策略: data/logs/bot-YYYYMMDD.log 按日滚动, 保留 14 天, 全部经 redact_log 脱敏。
"""
from __future__ import annotations

import logging
import re
import sys
import time
from datetime import date
from pathlib import Path
from typing import TextIO

import nonebot.log
from nonebot.log import LoguruHandler, default_filter, default_format, logger

from .envutil import ENV_FILE, load_env

# 凭证脱敏：键值对(token/api[_-]?key/secret/password 等变体)、Bearer、裸 sk- Key
# （评审 P1-6：\bapi_key\b 匹配不到 DEEPSEEK_API_KEY，因为 _ 是单词字符）
_TOKEN = re.compile(
    r"(?i)(\b\w*(?:api[_-]?key|token|secret|password)\w*\b[\"']?"
    r"\s*(?:[=:]\s*|\s+)[\"']?)([^\s\"'&,<>]+)"
)
_BEARER = re.compile(r"(?i)(\bBearer\s+)[^\s\"'&,<>]+")
_SK = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")
_sink_id: int | None = None

LOG_DIR = Path("data") / "logs"
LOG_RETENTION_DAYS = 14


def redact_log(text: str) -> str:
    """处理键值对凭证、Bearer 与裸 sk- Key，保留可定位问题的非敏感内容。"""
    return _SK.sub("[REDACTED]",
                   _BEARER.sub(r"\1[REDACTED]", _TOKEN.sub(r"\1[REDACTED]", text)))


def write_redacted(stream: TextIO, text: str) -> None:
    """Windows 控制台编码不兼容时转义字符，仍只写脱敏后的内容。"""
    clean = redact_log(text)
    try:
        stream.write(clean)
    except UnicodeEncodeError:
        encoding = stream.encoding or "utf-8"
        stream.write(clean.encode(encoding, errors="backslashreplace").decode(encoding))
    stream.flush()


class DailyRedactedFile:
    """按日滚动的脱敏文件 sink；损坏时静默停写文件, 不影响控制台。"""

    def __init__(self, directory: Path, retention_days: int = LOG_RETENTION_DAYS):
        self._dir = directory
        self._retention = retention_days
        self._fh: TextIO | None = None
        self._day: date | None = None

    def write(self, text: str) -> None:
        today = date.today()
        if self._fh is None or self._day != today:
            self._rotate(today)
        if self._fh is None:
            return
        clean = redact_log(text if text.endswith("\n") else text + "\n")
        try:
            self._fh.write(clean)
            self._fh.flush()
        except OSError:
            self._close()

    def _rotate(self, today: date) -> None:
        self._close()
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._fh = (self._dir / f"bot-{today:%Y%m%d}.log").open(
                "a", encoding="utf-8", errors="backslashreplace")
            self._day = today
            self._cleanup()
        except OSError:
            self._fh = None
            self._day = None

    def _cleanup(self) -> None:
        logs = sorted(self._dir.glob("bot-*.log"))
        for stale in logs[: max(0, len(logs) - self._retention)]:
            try:
                stale.unlink()
            except OSError:
                pass

    def _close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def close(self) -> None:
        self._close()


_file_sink = DailyRedactedFile(LOG_DIR)


def _write_log(message: str) -> None:
    # 最终格式化后脱敏，异常堆栈也不能泄露凭证。
    try:
        write_redacted(sys.stdout, str(message))
    except OSError:
        pass
    try:
        _file_sink.write(str(message))
    except OSError:
        pass


def log_file_path() -> Path | None:
    """当前落盘日志路径（诊断指引用）；文件 sink 未就绪时为 None。"""
    if _file_sink._fh is not None and _file_sink._day is not None:
        return LOG_DIR / f"bot-{_file_sink._day:%Y%m%d}.log"
    return None


def setup_logging() -> None:
    """入口调用一次：保留 NoneBot 格式/等级，并接入 app.* 的 INFO 日志。"""
    global _sink_id
    if _sink_id is None:
        logger.remove(nonebot.log.logger_id)
        _sink_id = logger.add(_write_log, level=0, filter=default_filter,
                              format=default_format, diagnose=False, colorize=False)
    app_logger = logging.getLogger("app")
    app_logger.setLevel(logging.INFO)
    if not any(isinstance(handler, LoguruHandler) for handler in app_logger.handlers):
        app_logger.addHandler(LoguruHandler())
    app_logger.propagate = False


def log_startup_banner(loaded_plugins: list[str]) -> None:
    """启动横幅: 插件清单 + OneBot 连接状态；首次连上 QQ 时私聊通知管理员。"""
    import os

    from nonebot import get_driver

    logger.info("bot 启动 | pid={} 插件数={} 插件=[{}] 日志落盘={}",
                os.getpid(), len(loaded_plugins), ", ".join(sorted(loaded_plugins)) or "-",
                log_file_path() or "(文件sink未就绪)")

    driver = get_driver()
    state = {"notified": False}

    @driver.on_bot_connect
    async def _on_bot_connect(bot) -> None:  # pragma: no cover - 真机链路
        try:
            info = await bot.get_login_info()
            logger.info("OneBot 已连接 | adapter={} qq={} 昵称={}",
                        bot.type, info.get("user_id"), info.get("nickname"))
        except Exception as exc:
            logger.warning("OneBot 已连接 | adapter={} (login_info 获取失败: {})",
                           bot.type, exc)
        # 崩溃重启是无声的，这条私聊让「机器人什么时候重开过」有据可查。
        # 默认关闭：内核频繁重启（调试/装插件）会轰炸管理员（实测投诉），
        # 想要的人在 .env 里设 BOT_ONLINE_NOTIFY=true
        if load_env(ENV_FILE).get("BOT_ONLINE_NOTIFY", "false").lower() \
                not in ("true", "1", "yes"):
            return
        if state["notified"]:
            return
        state["notified"] = True
        superusers = list(get_driver().config.superusers)
        if not superusers or not str(superusers[0]).isdigit():
            return
        try:
            await bot.call_api(
                "send_private_msg", user_id=int(superusers[0]),
                message=f"🟢 机器人已上线（PID {os.getpid()}，"
                        f"{time.strftime('%m-%d %H:%M')}）")
        except Exception as exc:
            logger.warning("上线通知发送失败: {}", exc)
