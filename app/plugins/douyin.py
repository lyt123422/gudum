"""刷抖音插件（高自由度遥控版）：QQ 里遥控本机浏览器刷抖音/互动。

指令（管理员）：
    刷抖音 [分钟]      开刷（默认30，上限60；随机停留+滑动+概率点赞）
    停止抖音           停止并关闭浏览器
    抖音暂停 / 抖音继续 暂停/恢复自动滑动（浏览器保留可遥控）
    抖音下一个         立刻划下一条
    抖音点赞           给当前视频点赞
    抖音关注           关注当前作者
    抖音评论 <内容>    对当前视频发评论（内容人工写，防垃圾）
    抖音搜索 <词>      搜索并浏览结果
    抖音截图           截当前画面发给你（看它在看什么/调试神器）
    抖音状态           会话/今日用时/点赞率/上次异常
    抖音点赞率 <0~1>   自动点赞概率（默认 0.15）
"""
from __future__ import annotations

import asyncio

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment
from nonebot.params import CommandArg

from .. import douyin, plugin_manager
from .chat import _cmd_gate

cmd_watch = on_command("刷抖音", rule=_cmd_gate, priority=4, block=True)
cmd_ai_watch = on_command("AI刷抖音", aliases={"ai刷抖音", "Ai刷抖音"},
                          rule=_cmd_gate, priority=4, block=True)
cmd_stop = on_command("停止抖音", aliases={"停止刷抖音"},
                      rule=_cmd_gate, priority=4, block=True)
cmd_pause = on_command("抖音暂停", rule=_cmd_gate, priority=4, block=True)
cmd_resume = on_command("抖音继续", rule=_cmd_gate, priority=4, block=True)
cmd_next = on_command("抖音下一个", rule=_cmd_gate, priority=4, block=True)
cmd_like = on_command("抖音点赞", rule=_cmd_gate, priority=4, block=True)
cmd_follow = on_command("抖音关注", rule=_cmd_gate, priority=4, block=True)
cmd_comment = on_command("抖音评论", rule=_cmd_gate, priority=4, block=True)
cmd_search = on_command("抖音搜索", rule=_cmd_gate, priority=4, block=True)
cmd_shot = on_command("抖音截图", rule=_cmd_gate, priority=4, block=True)
cmd_dy_status = on_command("抖音状态", rule=_cmd_gate, priority=4, block=True)
cmd_rate = on_command("抖音点赞率", rule=_cmd_gate, priority=4, block=True)
cmd_help = on_command("抖音帮助", rule=_cmd_gate, priority=4, block=True)


def _admin(event: MessageEvent) -> bool:
    from .chat import _is_admin  # noqa: PLC0415 运行时引用避免加载顺序问题

    return _is_admin(event.user_id)


async def _guard(matcher, event: MessageEvent, need_plugin: bool = True) -> bool:
    if need_plugin and not plugin_manager.is_enabled("douyin"):
        await matcher.finish("刷抖音插件没安装/没开启：控制台 → 🧩 插件 市场里安装")
    if not _admin(event):
        await matcher.finish("抖音遥控只对管理员开放（自动化有账号风险）")
    return True


async def _run(matcher, fn, *args):
    """浏览器操作是同步阻塞的：放线程池执行，不卡消息处理。"""
    ok, msg = await asyncio.to_thread(fn, *args)
    if ok:
        await matcher.finish("✅ " + msg)
    await matcher.finish("⚠ " + msg)


@cmd_watch.handle()
async def _(matcher, event: MessageEvent, args: Message = CommandArg()):  # noqa: B008
    await _guard(matcher, event)
    text = args.extract_plain_text().strip()
    minutes = int(text) if text.isdigit() else 30
    await _run(matcher, douyin.start, minutes)


@cmd_ai_watch.handle()
async def _(matcher, event: MessageEvent, args: Message = CommandArg()):  # noqa: B008
    await _guard(matcher, event)
    text = args.extract_plain_text().strip()
    minutes = int(text) if text.isdigit() else 10
    await _run(matcher, douyin.start, minutes, True)


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


@cmd_follow.handle()
async def _(matcher, event: MessageEvent):
    await _guard(matcher, event)
    await _run(matcher, douyin.follow_current)


@cmd_comment.handle()
async def _(matcher, event: MessageEvent, args: Message = CommandArg()):  # noqa: B008
    await _guard(matcher, event)
    await _run(matcher, douyin.comment, args.extract_plain_text().strip())


@cmd_search.handle()
async def _(matcher, event: MessageEvent, args: Message = CommandArg()):  # noqa: B008
    await _guard(matcher, event)
    await _run(matcher, douyin.search, args.extract_plain_text().strip())


@cmd_shot.handle()
async def _(matcher, event: MessageEvent):
    await _guard(matcher, event)
    path = await asyncio.to_thread(douyin.screenshot)
    if path and path.exists():
        await matcher.finish(MessageSegment.image(f"file:///{path.resolve().as_posix()}"))
    await matcher.finish("⚠ 截图失败（浏览器可能没打开，或看「抖音状态」的上次异常）")


@cmd_dy_status.handle()
async def _(matcher, event: MessageEvent):
    await _guard(matcher, event, need_plugin=False)
    s = await asyncio.to_thread(douyin.status)
    lines = ["📊 抖音遥控状态",
             f"· 浏览器: {'开着' if s['browser_open'] else '关闭'}"
             f" | 自动刷: {'进行中' if s['running'] else '停止'}"
             + ("（已暂停）" if s["paused"] else ""),
             f"· 本次: {s['elapsed_min']}/{s['plan_min']} 分钟 | 今日累计: {s['today_min']} 分钟",
             f"· 自动点赞率: {s['like_rate']:.0%}（抖音点赞率 0~1 可调）"]
    if s["last_error"]:
        lines.append(f"· 上次异常: {s['last_error']}")
    await matcher.finish("\n".join(lines))


@cmd_help.handle()
async def _(matcher, event: MessageEvent):
    await matcher.finish(
        "🎬 抖音遥控·指令表（管理员）\n"
        "· AI刷抖音 [分钟] —— AI 看画面决定 看多久/点赞/关注/划走（需多模态模型）\n"
        "· 刷抖音 [分钟] —— 随机模式开刷（停留/翻评论/逛主页/概率点赞）\n"
        "· 抖音暂停 / 抖音继续 —— 暂停滑动但浏览器保留，可随时插手\n"
        "· 抖音下一个 —— 划走这条\n"
        "· 抖音点赞 / 抖音关注 —— 互动当前视频/作者\n"
        "· 抖音评论 <内容> —— 发评论（内容自己写）\n"
        "· 抖音搜索 <词> —— 搜着看\n"
        "· 抖音截图 —— 把它在看的画面发你\n"
        "· 抖音状态 / 抖音点赞率 <0~1> / 停止抖音\n"
        "首次使用：开刷后在浏览器里扫码登录一次，登录态会一直记住")


@cmd_rate.handle()
async def _(matcher, event: MessageEvent, args: Message = CommandArg()):  # noqa: B008
    await _guard(matcher, event)
    text = args.extract_plain_text().strip()
    try:
        rate = float(text)
    except ValueError:
        await matcher.finish("用法：抖音点赞率 0.2（每五条赞一条）")
    await _run(matcher, douyin.set_like_rate, rate)
