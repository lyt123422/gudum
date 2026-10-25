"""看图输入处理（评审 P1-5）：QQ 图片 URL → 校验 → base64 data URL。

QQ 的图片 URL 是带签名的临时地址，由模型服务端拉取可能 403/过期；
默认在机器人进程内下载，校验域名白名单、大小、真实格式后转 base64 上传。
"""
from __future__ import annotations

import base64
import re

import httpx

_MAX_BYTES = 6 * 1024 * 1024
_TIMEOUT = 10.0
# QQ 多媒体域名白名单（评审 P1-5）：qpic.cn / qq.com / qq.com.cn 的子域
_ALLOWED_HOST_RE = re.compile(r"^[\w.-]+\.(qpic\.cn|qq\.com|qq\.com\.cn)$", re.I)


def host_allowed(url: str) -> bool:
    try:
        host = httpx.URL(url).host or ""
    except Exception:
        return False
    return bool(_ALLOWED_HOST_RE.match(host))


def sniff_mime(data: bytes) -> str:
    """魔数判真实格式；认不出返回空串（Content-Type 可伪造）。"""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


async def fetch_data_url(url: str) -> str:
    """下载并转 data URL；任何不合规都抛 ValueError（文案可直接给用户）。"""
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("图片地址不合法")
    if not host_allowed(url):
        raise ValueError("图片域名不在 QQ 多媒体白名单")
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.content
    if len(data) < 100:
        raise ValueError("图片太小，可能已失效")
    if len(data) > _MAX_BYTES:
        raise ValueError("图片超过 6MB")
    mime = sniff_mime(data) or resp.headers.get("content-type", "").split(";")[0].strip()
    if not mime.startswith("image/"):
        raise ValueError("这不是一张图片")
    return "data:" + mime + ";base64," + base64.b64encode(data).decode()
