"""定时任务插件：管理员配 cron + 内容，到点自动往指定群/好友发消息。

纯数据/cron 逻辑在 app/schedule_core.py（面板也能导入）；
本文件只做 NoneBot 命令、发送、调度 worker，以及内核生命周期钩子。
"""
from __future__ import annotations

import asyncio
import os
import time

from nonebot import get_bot, get_driver, logger, on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg

from .. import plugin_manager, schedule_core as core
from .chat import _cmd_gate, _is_admin

# 兼容旧测试/面板可能引用的名字
STATE_FILE = core.STATE_FILE
RUNTIME_FILE = core.RUNTIME_FILE
MAX_TASKS = core.MAX_TASKS
MAX_CONTENT = core.MAX_CONTENT
MAX_NAME_LEN = core.MAX_NAME_LEN
add_task = core.add_task
update_task = core.update_task
remove_task = core.remove_task
set_enabled = core.set_enabled
list_text = core.list_text
cron_fields = core.cron_fields
cron_valid = core.cron_valid
describe_cron = core.describe_cron
_cron_matches = core.cron_matches
_load = core.load_tasks
_merged = core.merged_tasks


def _cfg(name: str, default: str) -> str:
    v = getattr(get_driver().config, name.lower(), None)
    if v is None or v == "":
        v = os.getenv(name)
    return str(v) if v not in (None, "") else default


SCHEDULE_ENABLED = _cfg("SCHEDULE_ENABLED", "true").lower() in ("true", "1", "yes")
try:
    TICK_SECONDS = max(0.2, float(_cfg("SCHEDULE_TICK", "1")))
except ValueError:
    TICK_SECONDS = 1.0

_worker: asyncio.Task | None = None


def is_running() -> bool:
    return _worker is not None and not _worker.done()


def start_worker() -> None:
    """启动调度协程；必须在已有 running loop 的上下文（on_startup）调用。"""
    global _worker
    if _worker is not None and not _worker.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("定时任务调度器启动失败：当前没有运行中的事件循环")
        return
    _worker = loop.create_task(_loop())


def stop_worker() -> None:
    global _worker
    if _worker is not None and not _worker.done():
        _worker.cancel()
    _worker = None


def plugin_status() -> dict:
    return {"tasks": core.merged_tasks(),
            "running": is_running(),
            "max_tasks": core.MAX_TASKS,
            "enabled": SCHEDULE_ENABLED}


# ---------- 发送与调度 ----------

async def _send(task: dict) -> tuple[bool, str]:
    """按任务目标发消息；返回 (成败, 失败原因)——原因透传给 runtime 供面板排查。"""
    try:
        bot = get_bot()
    except Exception:
        logger.warning("定时任务「{}」：机器人未连接，跳过", task["name"])
        return False, "机器人未连接"
    target = str(task["target"])
    try:
        if task["type"] == "private":
            coro = bot.send_private_msg(user_id=int(target),
                                        message=task["content"],
                                        auto_escape=True)
        else:
            coro = bot.send_group_msg(group_id=int(target),
                                      message=task["content"],
                                      auto_escape=True)
        await asyncio.wait_for(coro, timeout=15)
    except (TimeoutError, asyncio.TimeoutError):   # noqa: UP041 py3.10 两者不同类，都要接
        logger.warning("定时任务「{}」发送超时（15s）", task["name"])
        return False, "发送超时（15 秒）"
    except Exception as exc:
        logger.warning("定时任务「{}」发送失败：{}", task["name"], exc)
        return False, f"发送失败: {str(exc)[:80]}"
    return True, ""


async def _tick_once(now: float | None = None) -> int:
    """一轮调度；返回成功发出的条数。"""
    if not SCHEDULE_ENABLED:
        return 0
    if not plugin_manager.is_enabled("schedule"):
        return 0
    when = time.time() if now is None else now
    mark = core.minute_mark(when)
    sent = 0
    for task in core.due_tasks(when):
        ok, reason = await _send(task)
        core.stamp_runtime(task["name"], mark, ok, reason)
        sent += 1 if ok else 0
    return sent


async def _loop() -> None:
    while True:
        try:
            if SCHEDULE_ENABLED and plugin_manager.is_enabled("schedule"):
                await _tick_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("定时任务调度异常：{}", exc)
        await asyncio.sleep(TICK_SECONDS)


# ---------- 生命周期 ----------

_driver = get_driver()


@_driver.on_startup
async def _start_schedule_worker() -> None:
    # 无论插件当前是否开启都启动；循环里再判断，方便面板运行时开关
    start_worker()


@_driver.on_shutdown
async def _stop_schedule_worker() -> None:
    stop_worker()


# ---------- QQ 指令 ----------

cmd_sched = on_command("定时任务", rule=_cmd_gate, priority=4, block=True)


async def _enabled(matcher) -> bool:
    if plugin_manager.is_enabled("schedule"):
        return True
    await matcher.finish("定时任务插件没开启：控制台 → 🧩 插件 里可以打开")
    return False


@cmd_sched.handle()
async def _(matcher, event: MessageEvent, args: Message = CommandArg()):  # noqa: B008
    await _enabled(matcher)
    if not _is_admin(event.user_id):
        await matcher.finish("定时任务只有管理员能设置哦")
    text = (args.extract_plain_text() or "").strip()
    if not text or text in ("列表", "list"):
        await matcher.finish(core.list_text())
    head, _, rest = text.partition(" ")
    head, rest = head.strip(), rest.strip()
    if head in ("添加", "新增", "add"):
        await matcher.finish(_cmd_add(rest, event))
    if head in ("删除", "移除", "del"):
        if not rest:
            await matcher.finish("用法：定时任务 删除 <名字>")
        ok, msg = core.remove_task(rest)
        await matcher.finish(("✅ " if ok else "❌ ") + msg)
    if head in ("开", "开启", "on"):
        ok, msg = core.set_enabled(rest, True)
        await matcher.finish(("✅ " if ok else "❌ ") + msg)
    if head in ("关", "关闭", "off"):
        ok, msg = core.set_enabled(rest, False)
        await matcher.finish(("✅ " if ok else "❌ ") + msg)
    if head in ("立即", "测试", "run"):
        await matcher.finish(await _cmd_run(rest))
    await matcher.finish(
        "用法：\n定时任务 列表\n定时任务 添加 <名字> | <cron> | <内容> | <群号>\n"
        "定时任务 删除/开启/关闭 <名字>\n定时任务 立即 <名字>\n"
        "cron 例：0 9 * * 1-5 = 工作日 9:00")


def _cmd_add(rest: str, event: MessageEvent) -> str:
    parts = [p.strip() for p in rest.split("|")]
    if len(parts) < 4:
        return ("用法：定时任务 添加 <名字> | <cron> | <内容> | <群号>\n"
                "例：定时任务 添加 早报 | 0 9 * * 1-5 | 大家早！ | 123456\n"
                "发给好友用：定时任务 添加 提醒 | 30 8 * * * | 该起床了 | friend:10001")
    name_raw, cron, content = parts[0], parts[1], parts[2]
    target_raw = parts[3]
    if target_raw.lower().startswith("friend:"):
        task_type, target = "private", target_raw.split(":", 1)[1].strip()
    else:
        task_type = "private" if event.message_type == "private" else "group"
        target = target_raw
    name = core.clean_name(name_raw)
    if any(t["name"] == name for t in core.load_tasks()):
        # QQ 侧同名覆盖对用户是坑（面板保存才是显式 upsert）：先提醒，不静默替换
        return (f"❌ 已有同名任务「{name}」：要改内容去面板保存，"
                f"或先发「定时任务 删除 {name}」再添加")
    ok, msg = core.add_task(name, cron, content, target, task_type)
    return ("✅ " if ok else "❌ ") + msg


async def _cmd_run(name: str) -> str:
    if not name:
        return "用法：定时任务 立即 <名字>"
    task = next((t for t in core.load_tasks() if t["name"] == name), None)
    if task is None:
        return f"没有叫「{name}」的任务"
    ok, reason = await _send(task)
    return f"「{name}」已发出" if ok else f"「{name}」发送失败（{reason}）"
