"""抖音真机插件：QQ 里遥控安卓手机真刷抖音（真滑动/真声音/真点赞）。

指令（管理员）：
    连接手机          —— 诊断+连接 USB 手机（首次用先发这个）
    刷抖音 [分钟]     —— AI 看手机屏幕两帧做决策，真滑动/真声音
    停止抖音 / 抖音暂停 / 抖音继续 / 抖音下一个 / 抖音点赞 / 抖音截图 / 抖音状态 / 抖音帮助
属于可选插件：控制台「🧩 插件 → 🛒 插件市场」安装并开启。
"""
from __future__ import annotations

import asyncio
import base64

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment
from nonebot.params import CommandArg

from .. import douyin, plugin_manager
from .chat import _cmd_gate

cmd_connect = on_command("连接手机", rule=_cmd_gate, priority=4, block=True)
# 「刷抖音」与「AI刷抖音」本就是同一个功能（V3-REVIEW P3：合并重复指令）
cmd_watch = on_command("刷抖音", aliases={"AI刷抖音", "ai刷抖音"},
                       rule=_cmd_gate, priority=4, block=True)
cmd_stop = on_command("停止抖音", aliases={"停止刷抖音"},
                      rule=_cmd_gate, priority=4, block=True)
cmd_pause = on_command("抖音暂停", rule=_cmd_gate, priority=4, block=True)
cmd_resume = on_command("抖音继续", rule=_cmd_gate, priority=4, block=True)
cmd_next = on_command("抖音下一个", rule=_cmd_gate, priority=4, block=True)
cmd_like = on_command("抖音点赞", rule=_cmd_gate, priority=4, block=True)
cmd_shot = on_command("抖音截图", rule=_cmd_gate, priority=4, block=True)
cmd_dy_status = on_command("抖音状态", rule=_cmd_gate, priority=4, block=True)
cmd_help = on_command("抖音帮助", rule=_cmd_gate, priority=4, block=True)


def _admin(event: MessageEvent) -> bool:
    from .chat import _is_admin  # noqa: PLC0415 运行时引用避免加载顺序问题

    return _is_admin(event.user_id)


async def _guard(matcher, event: MessageEvent, need_plugin: bool = True) -> bool:
    if need_plugin and not plugin_manager.is_enabled("douyin"):
        await matcher.finish("抖音真机插件没安装/没开启：控制台 → 🧩 插件 市场里安装")
    if not _admin(event):
        await matcher.finish("抖音遥控只对管理员开放（真机自动化要谨慎用）")
    return True


async def _run(matcher, fn, *args):
    ok, msg = await asyncio.to_thread(fn, *args)
    if ok:
        await matcher.finish("✅ " + msg)
    await matcher.finish("⚠ " + msg)


@cmd_connect.handle()
async def _(matcher, event: MessageEvent):
    await _guard(matcher, event)
    await _run(matcher, douyin.connect)


@cmd_watch.handle()
async def _(matcher, event: MessageEvent, args: Message = CommandArg()):  # noqa: B008
    await _guard(matcher, event)
    text = args.extract_plain_text().strip()
    minutes = int(text) if text.isdigit() else 10
    await _run(matcher, douyin.start, minutes)


@cmd_stop.handle()
async def _(matcher, event: MessageEvent):
    await _guard(matcher, event)
    await _run(matcher, douyin.stop)


@cmd_pause.handle()
async def _(matcher, event: MessageEvent):
    await _guard(matcher, event)
    await _run(matcher, douyin.pause)


@cmd_resume.handle()
async def _(matcher, event: MessageEvent):
    await _guard(matcher, event)
    await _run(matcher, douyin.resume)


@cmd_next.handle()
async def _(matcher, event: MessageEvent):
    await _guard(matcher, event)
    await _run(matcher, douyin.next_video)


@cmd_like.handle()
async def _(matcher, event: MessageEvent):
    await _guard(matcher, event)
    await _run(matcher, douyin.like_current)


@cmd_shot.handle()
async def _(matcher, event: MessageEvent):
    await _guard(matcher, event)
    shot = await asyncio.to_thread(douyin.screenshot)
    if shot:
        b64 = base64.b64encode(shot).decode()
        await matcher.finish(MessageSegment.image(f"base64://{b64}"))
    await matcher.finish("⚠ 截图失败：手机未连接或屏幕不可用，发「连接手机」诊断")


@cmd_dy_status.handle()
async def _(matcher, event: MessageEvent):
    await _guard(matcher, event, need_plugin=False)
    s = await asyncio.to_thread(douyin.status)
    lines = ["📊 抖音真机状态",
             "· 模式: AI 看手机屏幕两帧决策（真滑动/真声音）",
             f"· 手机: {'已连接' if s['connected'] else '未连接（发「连接手机」）'}"
             f" | 刷视频: {'进行中' if s['running'] else '停止'}"
             + ("（已暂停）" if s["paused"] else ""),
             f"· 本次: {s['elapsed_min']}/{s['plan_min']} 分钟 | 今日累计: {s['today_min']} 分钟"]
    if s["last_error"]:
        lines.append(f"· 上次异常: {s['last_error']}")
    await matcher.finish("\n".join(lines))


@cmd_help.handle()
async def _(matcher, event: MessageEvent):
    await matcher.finish(
        "🎬 抖音真机·指令表（管理员）\n"
        "· 连接手机 —— 首次用先发这个：诊断 USB/授权/抖音/代理\n"
        "· 刷抖音 [分钟] —— AI 看手机屏幕做决策，真滑动真声音\n"
        "· 抖音暂停 / 抖音继续 / 抖音下一个 / 抖音点赞\n"
        "· 抖音截图 —— 把手机当前画面发你\n"
        "· 抖音状态 / 停止抖音\n"
        "准备：手机开「USB 调试」连电脑、装好抖音。"
        "AI 连续决策失败会自动结束会话（日志写原因）")
