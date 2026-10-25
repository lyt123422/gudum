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
    """下载并转 data URL；任何不合规都抛 ValueError（文案可直接给用户）。

    评审 v3 P2-4：不自动跟随重定向——手动最多 3 跳，每一跳都复查域名白名单，
    防止 QQ 图 302 到任意域名；流式读取，超 6MB 中途终止。
    """
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("图片地址不合法")
    current = url
    data = b""
    async with httpx.AsyncClient(timeout=_TIMEOUT,
                                 headers={"User-Agent": "Mozilla/5.0"},
                                 follow_redirects=False) as client:
        for _ in range(3):
            if not host_allowed(current):
                raise ValueError("图片域名不在 QQ 多媒体白名单")
            resp = await client.get(current)
            if resp.status_code in (301, 302, 303, 307, 308):
                current = resp.headers.get("location", "")
                if not current:
                    raise ValueError("图片重定向失败")
                continue
            resp.raise_for_status()
            size = 0
            chunks = []
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > _MAX_BYTES:
                    raise ValueError("图片超过 6MB")
                chunks.append(chunk)
            data = b"".join(chunks)
            break
    if not data:
        raise ValueError("图片下载为空")
    if len(data) < 100:
        raise ValueError("图片太小，可能已失效")
    mime = sniff_mime(data) or resp.headers.get("content-type", "").split(";")[0].strip()
    if not mime.startswith("image/"):
        raise ValueError("这不是一张图片")
    return "data:" + mime + ";base64," + base64.b64encode(data).decode()
