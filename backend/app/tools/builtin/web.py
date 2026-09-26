from __future__ import annotations

import json
import os
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.tools.net import UnsafeUrlError, safe_request
from app.tools.registry import ToolContext, register

# --------------------------------------------------------------------------
# HTTP 请求
# --------------------------------------------------------------------------


class HttpRequestArgs(BaseModel):
    url: str = Field(description="完整的 http/https URL")
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"] = "GET"
    headers: dict[str, str] | None = Field(default=None, description="额外请求头")
    params: dict[str, str] | None = Field(default=None, description="查询参数")
    json_body: dict[str, Any] | None = Field(default=None, description="JSON 请求体")


@register(
    name="http_request",
    category="网络",
    description="发起 HTTP 请求并返回状态码与响应体。内网地址会被拒绝。",
    args_schema=HttpRequestArgs,
    dangerous=True,
)
async def http_request(ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    args = HttpRequestArgs(**kwargs)
    try:
        result = await safe_request(
            args.method,
            args.url,
            headers=args.headers,
            params=args.params,
            json_body=args.json_body,
        )
    except UnsafeUrlError as e:
        return {"error": str(e)}
    body = result["body"]
    if isinstance(body, str) and len(body) > 20_000:
        result["body"] = body[:20_000] + "\n…（已截断）"
    return result


# --------------------------------------------------------------------------
# 抓网页
# --------------------------------------------------------------------------


class WebFetchArgs(BaseModel):
    url: str = Field(description="要抓取的网页地址")
    max_chars: int = Field(default=8000, description="返回正文的最大字符数")


def _html_to_text(html: str) -> tuple[str, str]:
    """抽正文。去掉脚本样式导航，保留标题和段落结构。"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe", "svg", "nav", "footer", "header"]):
        tag.decompose()
    title = (soup.title.string or "").strip() if soup.title else ""
    main = soup.find("article") or soup.find("main") or soup.body or soup
    lines: list[str] = []
    for el in main.find_all(["h1", "h2", "h3", "h4", "li", "p", "pre", "blockquote"]):
        text = " ".join(el.get_text(" ", strip=True).split())
        if not text:
            continue
        if el.name.startswith("h"):
            lines.append(f"\n{'#' * int(el.name[1])} {text}")
        elif el.name == "li":
            lines.append(f"- {text}")
        else:
            lines.append(text)
    body = "\n".join(lines)
    if not body.strip():
        body = " ".join(main.get_text(" ", strip=True).split())
    return title, body


@register(
    name="web_fetch",
    category="网络",
    description="抓取网页并转换成干净的正文文本，适合让模型阅读。",
    args_schema=WebFetchArgs,
)
async def web_fetch(ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    args = WebFetchArgs(**kwargs)
    try:
        result = await safe_request("GET", args.url, headers={"User-Agent": "AgentLab/0.1"})
    except UnsafeUrlError as e:
        return {"error": str(e)}
    if result["status"] >= 400:
        return {"error": f"HTTP {result['status']}", "url": result["url"]}
    title, text = _html_to_text(str(result["body"]))
    truncated = len(text) > args.max_chars
    return {
        "url": result["url"],
        "title": title,
        "content": text[: args.max_chars] + ("\n…（已截断）" if truncated else ""),
        "truncated": truncated,
    }


# --------------------------------------------------------------------------
# 搜索
# --------------------------------------------------------------------------


class WebSearchArgs(BaseModel):
    query: str = Field(description="搜索关键词")
    max_results: int = Field(default=5, ge=1, le=20)


async def _tavily(query: str, n: int, key: str) -> list[dict[str, str]]:
    result = await safe_request(
        "POST",
        "https://api.tavily.com/search",
        json_body={"api_key": key, "query": query, "max_results": n},
    )
    data = json.loads(str(result["body"]))
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
        for r in data.get("results", [])
    ]


async def _duckduckgo(query: str, n: int) -> list[dict[str, str]]:
    """没有搜索 API key 时的兜底：解析 DuckDuckGo 的 lite 页面。"""
    from bs4 import BeautifulSoup

    result = await safe_request(
        "POST",
        "https://lite.duckduckgo.com/lite/",
        data=f"q={query}",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Mozilla/5.0 (compatible; AgentLab/0.1)",
        },
    )
    soup = BeautifulSoup(str(result["body"]), "html.parser")
    out: list[dict[str, str]] = []
    for row in soup.select("a.result-link"):
        out.append(
            {
                "title": row.get_text(" ", strip=True),
                "url": row.get("href", ""),
                "snippet": "",
            }
        )
        if len(out) >= n:
            break
    return out


@register(
    name="web_search",
    category="网络",
    description="搜索互联网。配了 TAVILY_API_KEY 就走 Tavily，否则退回 DuckDuckGo。",
    args_schema=WebSearchArgs,
)
async def web_search(ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    args = WebSearchArgs(**kwargs)
    key = os.environ.get("TAVILY_API_KEY")
    try:
        if key:
            results = await _tavily(args.query, args.max_results, key)
            engine = "tavily"
        else:
            results = await _duckduckgo(args.query, args.max_results)
            engine = "duckduckgo"
    except Exception as e:  # noqa: BLE001 - 搜索失败不该让整个 run 挂掉
        from app.api.errors import explain

        return {"error": f"搜索失败：{explain(e)[0]}", "results": []}
    return {"engine": engine, "query": args.query, "results": results}
