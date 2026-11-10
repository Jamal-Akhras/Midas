"""Tool registry: which tools each agent may call. Toolsets are minimal on
purpose - the screener does not need EDGAR; the structurer needs no web."""

from midas.tools import edgar, fundamentals_news, market_data, web
from midas.tools.base import Tool, params

TOOLS: dict[str, Tool] = {
    "get_quote": Tool(
        name="get_quote",
        description="Latest price, previous close, average daily volume and sector for a ticker.",
        parameters=params({"ticker": {"type": "string"}}, ["ticker"]),
        fn=market_data.get_quote,
    ),
    "get_daily_bars": Tool(
        name="get_daily_bars",
        description="Daily OHLCV bars (most recent last). Use for trend/setup analysis.",
        parameters=params(
            {
                "ticker": {"type": "string"},
                "days": {"type": "integer", "minimum": 5, "maximum": 250},
            },
            ["ticker"],
        ),
        fn=market_data.get_daily_bars,
    ),
    "get_options_chain": Tool(
        name="get_options_chain",
        description="Options chain: expiry, dte, type, strike, bid/ask/mid, open interest, volume, IV, delta.",
        parameters=params({"ticker": {"type": "string"}}, ["ticker"]),
        fn=market_data.get_options_chain,
    ),
    "get_filings": Tool(
        name="get_filings",
        description="Recent SEC filings (10-K, 10-Q, 8-K, DEF 14A) with URLs from EDGAR.",
        parameters=params(
            {
                "ticker": {"type": "string"},
                "forms": {"type": "string", "description": "comma-separated form filter"},
            },
            ["ticker"],
        ),
        fn=edgar.get_filings,
    ),
    "get_insider_transactions": Tool(
        name="get_insider_transactions",
        description=(
            "Insider transactions over the last 90 days WITH direction when the source "
            "provides it (direction_known=true: net shares bought vs sold, counts by "
            "transaction code P/S/A/M/F/G); otherwise an EDGAR Form 4 filing list that "
            "carries no direction (direction_known=false)."
        ),
        parameters=params({"ticker": {"type": "string"}}, ["ticker"]),
        fn=edgar.get_insider_transactions,
    ),
    "get_earnings": Tool(
        name="get_earnings",
        description="Earnings history vs estimates, next earnings date, analyst consensus estimates, key ratios.",
        parameters=params({"ticker": {"type": "string"}}, ["ticker"]),
        fn=fundamentals_news.get_earnings,
    ),
    "get_news": Tool(
        name="get_news",
        description="Recent company news with URLs and dates.",
        parameters=params({"ticker": {"type": "string"}}, ["ticker"]),
        fn=fundamentals_news.get_news,
    ),
    "web_search": Tool(
        name="web_search",
        description="Web search returning title/url/snippet results.",
        parameters=params({"query": {"type": "string"}}, ["query"]),
        fn=web.web_search,
    ),
    "fetch_page": Tool(
        name="fetch_page",
        description="Fetch a URL and return its readable text (capped).",
        parameters=params({"url": {"type": "string"}}, ["url"]),
        fn=web.fetch_page,
    ),
}

TOOLSETS: dict[str, list[str]] = {
    "screener": ["get_quote", "get_daily_bars"],
    "fundamentals": ["get_earnings", "get_quote"],
    "deep-research": [
        "get_filings",
        "get_insider_transactions",
        "web_search",
        "fetch_page",
        "get_news",
    ],
    "catalyst": ["get_news", "get_earnings", "web_search"],
    "investor": ["get_quote", "get_daily_bars"],
    "reevaluator": ["get_quote", "get_daily_bars", "get_news", "get_earnings"],
    "event-analyst": ["get_news", "get_filings", "get_earnings"],
}


def toolset(agent_slug: str) -> dict[str, Tool]:
    return {name: TOOLS[name] for name in TOOLSETS.get(agent_slug, [])}
