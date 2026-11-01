"""联网搜索模块：默认 DuckDuckGo，境外被墙环境自动落必应中国；可选 Tavily。

配置（.env）:
    WEB_SEARCH_ENABLED=true
    SEARCH_PROVIDER=duckduckgo   # duckduckgo | bing | tavily
    TAVILY_API_KEY=              # 用 tavily 时填

源选择逻辑（2026-10-31）：国内服务器直连访问不到 DuckDuckGo（实测 000 连接失败），
所以 ddg 失败时自动尝试必应中国（免费无 Key、直连 0.3s 可达）——同一条查询
换到可达的源上重试，直到拿到结果或全部失败。
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


async def _search_bing(query: str, limit: int) -> list[dict]:
    """必应搜索（RSS 输出）：国内服务器直连可达，DuckDuckGo 被墙环境的兜底源。

    RSS 返回 <item><title/><link/><description/>，无需解析页面结构，
    也没有验证码/反爬干扰（2026-10-31 实测国内直连 0.3s）。
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": _UA},
                                 follow_redirects=True) as client:
        resp = await client.get("https://www.bing.com/search",
                                params={"q": query, "format": "rss",
                                        "count": str(limit)})
        resp.raise_for_status()
    return _parse_bing_rss(resp.text, limit)


def _parse_bing_rss(text: str, limit: int) -> list[dict]:
    entries: list[dict] = []
    for item in re.findall(r"<item>(.*?)</item>", text, re.S):
        title_m = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>",
                            item, re.S)
        link_m = re.search(r"<link>(.*?)</link>", item, re.S)
        desc_m = re.search(
            r"<description>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>",
            item, re.S)
        if not (title_m and link_m):
            continue
        clean = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s)).strip()  # noqa: E731
        entries.append({"title": clean(title_m.group(1))[:80],
                        "snippet": clean(desc_m.group(1) if desc_m else "")[:200],
                        "url": (link_m.group(1) or "").strip()})
        if len(entries) >= limit:
            break
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
    """返回 [{title, snippet, url}]；按顺序尝试搜索源，全部失败才抛异常。

    源顺序：tavily（配了 Key 时优先）→ 配置的源 → 必应中国兜底。
    DuckDuckGo 在境内服务器不可达（实测 000 连接失败），必应 RSS 直连 0.3s 可达
    ——自动降级链保证国内服务器也能联网（2026-10-31 实测）。
    """
    attempts: list = []
    if provider == "tavily" and tavily_key:
        attempts.append(lambda: _search_tavily(query, limit, tavily_key))
    if provider == "bing":
        attempts.append(lambda: _search_bing(query, limit))
    attempts.append(lambda: _search_duckduckgo(query, limit))
    if not any(getattr(fn, "__name__", "") == "_search_bing" for fn in attempts):
        attempts.append(lambda: _search_bing(query, limit))
    last_exc: Exception | None = None
    for fn in attempts:
        try:
            results = await fn()
            if results:
                return results
        except Exception as exc:   # noqa: BLE001 记录后换下一个搜索源
            last_exc = exc
    if last_exc:
        raise last_exc
    return []