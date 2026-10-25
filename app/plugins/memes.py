"""表情包插件：本地表情库的收藏与斗图。

- 存表情 [名字]   —— 回复一条带图的消息发送，把那张图收进表情库
- 表情包 [关键词] —— 随机/按关键词发一张（群聊斗图用）
- 表情列表       —— 看库里都有什么
数据在 data/memes/，纯本地。
"""
from __future__ import annotations

import re
import time

import httpx
from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment
from nonebot.params import CommandArg

from .. import plugin_manager
from ..envutil import ROOT

MEME_DIR = ROOT / "data" / "memes"

cmd_save = on_command("存表情", priority=4, block=True)
cmd_meme = on_command("表情包", aliases={"斗图", "来个表情"}, priority=4, block=True)
cmd_list = on_command("表情列表", aliases={"表情包列表"}, priority=4, block=True)


async def _meme_enabled(matcher) -> bool:
    """表情包是可选插件：没开时给一句指引而不是装死。"""
    if plugin_manager.is_enabled("memes"):
        return True
    await matcher.finish("表情包插件没开启：控制台 → 🧩 插件 里可以打开")
    return False


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff-]", "_", name.strip())[:30]


async def _image_url_from_reply(bot: Bot, event: MessageEvent) -> str | None:
    """从"回复消息"里取被引用那张图的 URL。"""
    reply_seg = next((s for s in event.message if s.type == "reply"), None)
    if reply_seg is None:
        return None
    message_id = reply_seg.data.get("id")
    if not message_id:
        return None
    try:
        raw = await bot.get_msg(message_id=int(message_id))
    except Exception as exc:
        logger.warning("取被回复消息失败: {}", exc)
        return None
    segments = raw.get("message") or []
    if isinstance(segments, str):
        return None
    for seg in segments:
        if isinstance(seg, dict) and seg.get("type") == "image":
            url = (seg.get("data") or {}).get("url")
            if url:
                return str(url)
    return None


@cmd_save.handle()
async def _(matcher, bot: Bot, event: MessageEvent, args: Message = CommandArg()):  # noqa: B008
    await _meme_enabled(matcher)
    url = await _image_url_from_reply(bot, event)
    if not url:
        await matcher.finish("用法：回复一条带图的消息，发「存表情 名字」（名字可省略）")
    name = _safe_name(args.extract_plain_text() or f"meme_{int(time.time())}")
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        data = resp.content
    except Exception as exc:
        logger.warning("表情包下载失败: {}", exc)
        await matcher.finish("表情下载失败，稍后再试")
    if len(data) < 200 or len(data) > 10 * 1024 * 1024:
        await matcher.finish("这张图好像不太对（太小或超过 10MB），没收")
    MEME_DIR.mkdir(parents=True, exist_ok=True)
    path = MEME_DIR / f"{name}.jpg"
    path.write_bytes(data)
    await matcher.finish(f"🧩 已收藏表情「{name}」（库里现有 {len(list(MEME_DIR.glob('*.jpg')))} 张）")


@cmd_meme.handle()
async def _(matcher, args: Message = CommandArg()):  # noqa: B008
    await _meme_enabled(matcher)
    MEME_DIR.mkdir(parents=True, exist_ok=True)
    keyword = _safe_name(args.extract_plain_text())
    files = sorted(MEME_DIR.glob("*.jpg"))
    if keyword:
        files = [f for f in files if keyword in f.stem] or files
    if not files:
        await matcher.finish("表情包库还是空的：回复一张图发「存表情 名字」来收录")
    pick = files[int(time.time()) % len(files)]   # 伪随机就够了
    await matcher.finish(MessageSegment.image(f"file:///{pick.resolve().as_posix()}"))


@cmd_list.handle()
async def _(matcher):
    await _meme_enabled(matcher)
    MEME_DIR.mkdir(parents=True, exist_ok=True)
    names = [f.stem for f in sorted(MEME_DIR.glob("*.jpg"))]
    if not names:
        await matcher.finish("表情库是空的，用「存表情」收录第一张吧")
    await matcher.finish("🧩 表情库（{}）：{}".format(
        len(names), "、".join(names[:20]) + ("…" if len(names) > 20 else "")))
