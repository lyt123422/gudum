"""表情包插件：本地表情库的收藏与斗图。

- 存表情 [名字]   —— 回复一条带图的消息发送，把那张图收进表情库
- 表情包 [关键词] —— 随机/按关键词发一张（群聊斗图用）
- 表情列表       —— 看库里都有什么
数据在 data/memes/，纯本地。
"""
from __future__ import annotations

import random
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import httpx
from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment
from nonebot.params import CommandArg

from .. import plugin_manager, vision
from ..envutil import ROOT
from .chat import _cmd_gate

MEME_DIR = ROOT / "data" / "memes"
_MAX_BYTES = 10 * 1024 * 1024

cmd_save = on_command("存表情", rule=_cmd_gate, priority=4, block=True)
cmd_meme = on_command("表情包", aliases={"斗图", "来个表情"},
                      rule=_cmd_gate, priority=4, block=True)
cmd_list = on_command("表情列表", aliases={"表情包列表"},
                      rule=_cmd_gate, priority=4, block=True)


async def _meme_enabled(matcher) -> bool:
    """表情包是可选插件：没开时给一句指引而不是装死。"""
    if plugin_manager.is_enabled("memes"):
        return True
    await matcher.finish("表情包插件没开启：控制台 → 🧩 插件 里可以打开")
    return False


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff-]", "_", name.strip())[:30]


def _meme_files() -> list[Path]:
    if not MEME_DIR.exists():   # 纯读路径不带 mkdir 副作用
        return []
    return sorted(f for f in MEME_DIR.iterdir()
                  if f.suffix.lower() in (".jpg", ".png", ".gif", ".webp"))


async def _download_image(url: str) -> bytes:
    """下载 QQ 聊天图：逐跳复查域名白名单（防 302 带偏到任意域，评审 P2-18）、
    流式读取超限即断。范式与 vision.fetch_data_url 一致。"""
    current = url
    async with httpx.AsyncClient(timeout=15, follow_redirects=False,
                                 headers={"User-Agent": "Mozilla/5.0"}) as client:
        for _ in range(3):
            if not vision.host_allowed(current):
                raise ValueError("图片域名不在 QQ 多媒体白名单")
            async with client.stream("GET", current) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location", "")
                    if not location:
                        raise ValueError("图片重定向失败")
                    current = urljoin(current, location)
                    continue
                resp.raise_for_status()
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > _MAX_BYTES:
                        raise ValueError("图片超过 10MB")
                return bytes(buf)
    raise ValueError("图片重定向次数过多")


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
    if not vision.host_allowed(url):
        # 评审 P2-18：只收 QQ 聊天图，不让面板/机器人当任意下载器
        await matcher.finish("只收录 QQ 聊天里的图片")
    name = _safe_name(args.extract_plain_text() or f"meme_{int(time.time())}")
    try:
        data = await _download_image(url)
    except ValueError as exc:
        await matcher.finish(f"没收：{exc}")
    except Exception as exc:
        logger.warning("表情包下载失败: {}", exc)
        await matcher.finish("表情下载失败，稍后再试")
    mime = vision.sniff_mime(data)   # 评审 P2-18：魔数判真实格式
    if not mime:
        await matcher.finish("这不是常见图片格式（jpg/png/gif/webp），没收")
    if len(data) < 200 or len(data) > 10 * 1024 * 1024:
        await matcher.finish("这张图好像不太对（太小或超过 10MB），没收")
    MEME_DIR.mkdir(parents=True, exist_ok=True)
    ext = mime.split("/")[1].replace("jpeg", "jpg")
    path = MEME_DIR / f"{name}.{ext}"
    path.write_bytes(data)
    await matcher.finish(
        f"🧩 已收藏表情「{name}」（库里现有 {len(_meme_files())} 张）")


@cmd_meme.handle()
async def _(matcher, args: Message = CommandArg()):  # noqa: B008
    await _meme_enabled(matcher)
    keyword = (args.extract_plain_text() or "").strip()   # 不过 _safe_name：别把「好耶！」洗成匹配不到的「好耶_」
    files = _meme_files()
    if keyword:
        files = [f for f in files if keyword in f.stem] or files
    if not files:
        await matcher.finish("表情包库还是空的：回复一张图发「存表情 名字」来收录")
    pick = random.choice(files)
    await matcher.finish(MessageSegment.image(f"file:///{pick.resolve().as_posix()}"))


@cmd_list.handle()
async def _(matcher):
    await _meme_enabled(matcher)
    names = [f.stem for f in _meme_files()]
    if not names:
        await matcher.finish("表情库是空的，用「存表情」收录第一张吧")
    await matcher.finish("🧩 表情库（{}）：{}".format(
        len(names), "、".join(names[:20]) + ("…" if len(names) > 20 else "")))
