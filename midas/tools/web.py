"""Open-web research: search (Brave, then Tavily) + page fetch.
For the Deep Research Agent. Results are data with URLs; the honest-data
rule means anything not returned by these tools cannot be a dossier fact."""

import logging
import re

import httpx

from midas.config import settings
from midas.tools import demo_data
from midas.tools.base import ToolError, ToolResult

logger = logging.getLogger(__name__)

MAX_PAGE_CHARS = 8000


def web_search(query: str, count: int = 5) -> ToolResult:
    """Web search returning [{title, url, snippet}]."""
    if settings.effective_demo_mode:
        return ToolResult(data=demo_data.WEB_RESULTS["default"], source="demo")
    if not (settings.brave_api_key or settings.tavily_api_key):
        raise ToolError(
            "no web search configured (set BRAVE_API_KEY or TAVILY_API_KEY, "
            "both free tiers); web claims must go in 'unverified'"
        )
    try:
        if settings.brave_api_key:
            r = httpx.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": count},
                headers={
                    "X-Subscription-Token": settings.brave_api_key,
                    "Accept": "application/json",
                },
                timeout=15,
            )
            r.raise_for_status()
            results = r.json().get("web", {}).get("results", [])
            return ToolResult(
                data=[
                    {
                        "title": x.get("title", ""),
                        "url": x.get("url", ""),
                        "snippet": (x.get("description") or "")[:300],
                    }
                    for x in results[:count]
                ],
                source="brave",
            )
        r = httpx.post(
            "https://api.tavily.com/search",
            json={"api_key": settings.tavily_api_key, "query": query, "max_results": count},
            timeout=15,
        )
        r.raise_for_status()
        return ToolResult(
            data=[
                {
                    "title": x.get("title", ""),
                    "url": x.get("url", ""),
                    "snippet": (x.get("content") or "")[:300],
                }
                for x in r.json().get("results", [])[:count]
            ],
            source="tavily",
        )
    except httpx.HTTPError as exc:
        raise ToolError(f"web search failed: {type(exc).__name__}") from exc


def fetch_page(url: str) -> ToolResult:
    """Fetch a web page and return readable text (tags stripped, capped)."""
    if settings.effective_demo_mode:
        return ToolResult(
            data={
                "url": url,
                "text": "Demo page content: net revenue retention 118%, "
                "management guided FY revenue up 15-17%, CFO transition in progress.",
            },
            source="demo",
        )
    try:
        r = httpx.get(
            url,
            timeout=20,
            follow_redirects=True,
            headers={"User-Agent": "Midas personal research bot"},
        )
        r.raise_for_status()
    except httpx.HTTPError as exc:
        raise ToolError(f"fetch failed: {type(exc).__name__}") from exc
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", r.text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return ToolResult(data={"url": url, "text": text[:MAX_PAGE_CHARS]}, source="web")
