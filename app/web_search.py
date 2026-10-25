"""联网搜索模块：默认 DuckDuckGo Lite 免费无 Key，可选 Tavily（质量更好）。

配置（.env）:
    WEB_SEARCH_ENABLED=true
    SEARCH_PROVIDER=duckduckgo   # duckduckgo | tavily
    TAVILY_API_KEY=              # 用 tavily 时填
"""
from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, unquote, urlparse

import httpx

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
_TIMEOUT = 8.0


def _real_url(href: str) -> str:
    """DDG lite 的跳转链接解出真实 URL。"""
    if href.startswith("//"):
        href = "https:" + href
    try:
        parsed = urlparse(href)
        if "duckduckgo.com" in parsed.netloc and "l/" in parsed.path:
            target = parse_qs(parsed.query).get("uddg", [""])[0]
            if target:
                return unquote(target)
    except ValueError:
        pass
    return href


async def _search_duckduckgo(query: str, limit: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": _UA},
                                 follow_redirects=True) as client:
        resp = await client.post("https://lite.duckduckgo.com/lite/",
                                 data={"q": query})
        resp.raise_for_status()
    text = resp.text
    entries: list[dict] = []
    # lite 页锚点：<a rel="nofollow" href="..." class='result-link'>标题</a>
    # 属性顺序不定、引号单双混用，先抓整标签再单独取 href
    anchor_re = re.compile(r"<a([^>]*result-link[^>]*)>(.*?)</a>", re.S)
    href_re = re.compile(r"href=['\"]([^'\"]+)['\"]")
    snip_re = re.compile(r"class=['\"]result-snippet['\"][^>]*>(.*?)</td>", re.S)
    anchors = anchor_re.findall(text)
    snippets = snip_re.findall(text)
    for i, (attrs, title) in enumerate(anchors[:limit]):
        href_m = href_re.search(attrs)
        if not href_m:
            continue
        snippet = snippets[i] if i < len(snippets) else ""
        clean = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s)).strip()  # noqa: E731
        entries.append({"title": clean(title)[:80],
                        "snippet": clean(snippet)[:200],
                        "url": _real_url(href_m.group(1))})
    return entries


async def _search_tavily(query: str, limit: int, api_key: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post("https://api.tavily.com/search", json={
            "api_key": api_key, "query": query, "max_results": limit})
        resp.raise_for_status()
        data = resp.json()
    return [{"title": (r.get("title") or "")[:80],
             "snippet": (r.get("content") or "")[:200],
             "url": r.get("url", "")}
            for r in data.get("results", []) if r.get("url")]


async def search(query: str, provider: str = "duckduckgo",
                 tavily_key: str = "", limit: int = 5) -> list[dict]:
    """返回 [{title, snippet, url}]；失败抛异常，由调用方降级为普通对话。"""
    if provider == "tavily" and tavily_key:
        return await _search_tavily(query, limit, tavily_key)
    return await _search_duckduckgo(query, limit)
