"""Demo provider: the full committee at zero AI spend.

Implements the same ModelProvider protocol and the same tool-loop code path:
agents with tools first emit scripted tool calls (so DataSnapshots and
provenance exist in demo too), then produce canned final outputs that
validate against the real schemas. Costs are logged under provider 'demo'.

The agent is identified by a [[agent:<slug>]] marker in the system prompt;
the ticker is read from a 'Ticker: XXX' line in the conversation.
"""

import json
import re
import uuid
from collections.abc import Callable

from midas.providers.base import Message, ModelResponse, ToolCall, ToolSpec
from midas.tools.demo_data import QUOTES


def _quote(ticker: str) -> dict:
    return QUOTES.get(ticker, {"price": 50.0, "sector": "Technology"})


def _screen(ticker: str) -> dict:
    return {
        "candidates": [
            {
                "ticker": "ACME",
                "setup": "breakout-pullback",
                "score": 82,
                "price": QUOTES["ACME"]["price"],
                "avg_volume": QUOTES["ACME"]["avg_volume"],
                "rationale": "New highs on rising volume; pulled back 2% to the 20-day.",
                "data_points": ["price 184.20", "avg_volume 8.4M", "+1.7% vs prev close"],
            },
            {
                "ticker": "DUNE",
                "setup": "post-earnings-drift",
                "score": 74,
                "price": QUOTES["DUNE"]["price"],
                "avg_volume": QUOTES["DUNE"]["avg_volume"],
                "rationale": "Beat and raised; drifting up on steady volume.",
                "data_points": ["price 91.80", "avg_volume 5.6M", "+3.8% vs prev close"],
            },
        ],
        "universe_size": 5,
        "notes": "Demo screen over the 5-ticker demo universe.",
    }


def _fundamentals(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "recent_quarters": [
            {"period": "2026Q1", "eps_actual": 1.42, "eps_estimate": 1.31, "surprise_pct": 8.4},
            {"period": "2025Q4", "eps_actual": 1.28, "eps_estimate": 1.25, "surprise_pct": 2.4},
        ],
        "next_earnings_date": "",
        "projected_eps_next_q": 1.51,
        "projected_revenue_growth_pct": 16.5,
        "key_ratios": {"pe_forward": 24.3, "ps": 6.1, "debt_to_equity": 0.38},
        "guidance_history": "Raised guidance the last two quarters; beat both.",
        "unverified": ["FY27 margin target mentioned in press coverage - no filing confirmed it"],
    }


def _dossier(ticker: str) -> dict:
    ts = "RETRIEVED_AT"  # placeholder replaced with tool retrieved_at by prompt content
    return {
        "ticker": ticker,
        "sections": [
            {
                "topic": "filings",
                "claims": [
                    {
                        "claim": "Revenue +18% YoY with gross margin up 140bps (10-Q).",
                        "source_url": "https://www.sec.gov/Archives/edgar/data/0000000000/demo-10q.htm",
                        "retrieved_at": ts,
                    },
                    {
                        "claim": "Top-3 customer concentration is 41% of revenue (10-K risk factors).",
                        "source_url": "https://www.sec.gov/Archives/edgar/data/0000000000/demo-10k.htm",
                        "retrieved_at": ts,
                    },
                    {
                        "claim": "8-K: multi-year partnership with a top-5 cloud provider; CFO retiring with 6-month handover.",
                        "source_url": "https://www.sec.gov/Archives/edgar/data/0000000000/demo-8k.htm",
                        "retrieved_at": ts,
                    },
                ],
            },
            {
                "topic": "insiders",
                "claims": [
                    {
                        "claim": "CEO bought 25,000 shares ($4.6M) 18 days ago.",
                        "source_url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=4&demo=1",
                        "retrieved_at": ts,
                    },
                ],
            },
            {
                "topic": "coverage",
                "claims": [
                    {
                        "claim": "Third-party research reports net revenue retention of 118%.",
                        "source_url": "https://research.example.com/demo/deep-dive",
                        "retrieved_at": ts,
                    },
                ],
            },
        ],
        "management_quality_notes": "Founder-CEO, 6-year tenure, prior scale-ups; CFO transition is the main watch item.",
        "insider_activity_summary": "Net insider buying; CEO purchase dwarfs CFO's routine sale.",
        "unverified": ["Rumored second cloud partnership - no filing or credible source found"],
        "overall_take": "Fundamentally strong with one governance watch item (CFO transition).",
    }


def _catalysts(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "upcoming_earnings_date": "",
        "catalysts": [
            {
                "event": "Earnings report",
                "date": "",
                "expected_impact": "high",
                "source_url": "https://news.example.com/demo/cloud-partnership",
            },
            {
                "event": "Cloud partnership rollout milestones",
                "date": "",
                "expected_impact": "medium",
                "source_url": "https://news.example.com/demo/cloud-partnership",
            },
        ],
        "sector_context": "Quality growth in favor; sector flows positive.",
        "unverified": [],
    }


def _triage(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "passable": True,
        "reasons": ["Liquidity and price floors met", "Research surface is clean"],
        "data_quality_flags": [],
    }


def _bull(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "stance": "bull",
        "conviction": 7.5,
        "core_argument": "Beat-and-raise cadence plus a new top-5 cloud partnership compounds distribution; insider buying confirms.",
        "key_evidence": ["10-Q revenue +18% YoY", "8-K partnership", "CEO $4.6M purchase"],
        "expected_move_pct": 12.0,
        "expected_timeframe_days": 45,
        "what_would_change_my_mind": ["Partnership rollout slips", "NRR drops below 110%"],
    }


def _bear(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "stance": "bear",
        "conviction": 5.0,
        "core_argument": "41% top-3 customer concentration and a CFO exit into a premium multiple leaves no room for a guidance miss.",
        "key_evidence": [
            "10-K customer concentration 41%",
            "CFO transition 8-K",
            "Forward P/E 24.3 vs sector 19",
        ],
        "expected_move_pct": -9.0,
        "expected_timeframe_days": 45,
        "what_would_change_my_mind": [
            "Concentration falls below 30%",
            "Seasoned CFO hire announced",
        ],
    }


def _structure(ticker: str) -> dict:
    from datetime import date, timedelta

    price = _quote(ticker)["price"]
    return {
        "ticker": ticker,
        "picked": {
            "strategy": "call_debit_spread",
            "expiry": (date.today() + timedelta(days=49)).isoformat(),
            "long_strike": round(price, 0),
            "short_strike": round(price * 1.05, 0),
            "rationale": "49 DTE clears the 30-60 window; spread caps premium while capturing the expected +12% move.",
        },
        "alternatives_considered": [
            {
                "strategy": "long_call",
                "expiry": (date.today() + timedelta(days=49)).isoformat(),
                "long_strike": round(price * 1.05, 0),
                "short_strike": None,
                "rationale": "More convexity, but ~2.4x the premium at risk.",
            },
        ],
    }


def _verdict(ticker: str) -> dict:
    price = _quote(ticker)["price"]
    return {
        "ticker": ticker,
        "verdict": "trade",
        "direction": "bullish",
        "instrument": "option",
        "entry_zone_low": round(price * 0.98, 2),
        "entry_zone_high": round(price * 1.01, 2),
        "stop_price": round(price * 0.91, 2),
        "targets": [round(price * 1.10, 2), round(price * 1.18, 2)],
        "thesis_summary": "Beat-and-raise compounder with a new cloud distribution channel; bear case is real (concentration, CFO exit) but priced.",
        "what_invalidates_this": [
            "Underlying closes below the stop level",
            "Partnership rollout delayed past next earnings",
            "Guidance cut or NRR below 110%",
        ],
        "review_schedule": {
            "cadence_days": 1,
            "event_triggers": ["earnings", "invalidation", "dte"],
        },
        "no_trade_reason": "",
    }


def _reevaluation(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "decision": "hold",
        "reasons": [
            "Thesis intact: no filing or news contradicts the entry case",
            "Price inside entry zone; stop not threatened",
        ],
        "thesis_still_valid": True,
        "time_pressure_note": "35+ DTE remaining; theta not yet decisive.",
        "next_review_days": 1,
    }


def _event_flags(ticker: str) -> dict:
    """ACME carries one sourced medium flag so the scan path is exercised;
    every other demo ticker is quiet, which is the normal answer."""
    if ticker != "ACME":
        return {"ticker": ticker, "flags": [], "unverified": []}
    return {
        "ticker": ticker,
        "flags": [
            {
                "kind": "management_change",
                "severity": "medium",
                "direction": "negative",
                "summary": "CFO departure announced; successor named from inside.",
                "source_url": "https://news.example.com/demo/cfo-exit",
                "event_date": "",
            }
        ],
        "unverified": ["Rumoured activist stake - no filing found"],
    }


# slug -> (scripted tool calls, final output builder)
SCRIPTS: dict[str, tuple[list[tuple[str, dict]], Callable[[str], dict]]] = {
    "event-analyst": (
        [("get_news", {"ticker": "TICKER"}), ("get_filings", {"ticker": "TICKER"})],
        _event_flags,
    ),
    "screener": (
        [
            ("get_quote", {"ticker": "ACME"}),
            ("get_quote", {"ticker": "DUNE"}),
            ("get_daily_bars", {"ticker": "ACME", "days": 60}),
        ],
        _screen,
    ),
    "fundamentals": ([("get_earnings", {"ticker": "TICKER"})], _fundamentals),
    "deep-research": (
        [
            ("get_filings", {"ticker": "TICKER"}),
            ("get_insider_transactions", {"ticker": "TICKER"}),
            ("web_search", {"query": "TICKER management quality"}),
        ],
        _dossier,
    ),
    "catalyst": (
        [("get_news", {"ticker": "TICKER"}), ("get_earnings", {"ticker": "TICKER"})],
        _catalysts,
    ),
    "triage": ([], _triage),
    "bull": ([], _bull),
    "bear": ([], _bear),
    "structurer": ([], _structure),
    "investor": ([("get_quote", {"ticker": "TICKER"})], _verdict),
    "reevaluator": ([("get_quote", {"ticker": "TICKER"})], _reevaluation),
    "postmortem": (
        [],
        lambda t: {
            "ticker": t,
            "what_worked": ["stop discipline held"],
            "what_failed": ["entered extended after a gap day"],
            "lesson": "wait for the pullback; the market reopens tomorrow",
        },
    ),
}

_MARKER = re.compile(r"\[\[agent:([a-z-]+)\]\]")
_TICKER = re.compile(r"Ticker:\s*([A-Z]{1,6})")


class DemoProvider:
    name = "demo"

    def complete(
        self,
        *,
        model: str,
        system_prompt: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        json_output: bool = False,
        json_schema: dict | None = None,
        max_tokens: int = 4000,
    ) -> ModelResponse:
        marker = _MARKER.search(system_prompt)
        slug = marker.group(1) if marker else ""
        script_calls, final_fn = SCRIPTS.get(slug, ([], lambda t: {"note": "no demo fixture"}))

        ticker = "ACME"
        for m in messages:
            if isinstance(m.get("content"), str):
                match = _TICKER.search(m["content"])
                if match:
                    ticker = match.group(1)
                    break

        already_called = any(m.get("role") == "assistant" and m.get("tool_calls") for m in messages)
        if tools and script_calls and not already_called:
            allowed = {t.name for t in tools}
            calls = [
                ToolCall(
                    id=str(uuid.uuid4()),
                    name=name,
                    arguments={k: (ticker if v == "TICKER" else v) for k, v in args.items()},
                )
                for name, args in script_calls
                if name in allowed
            ]
            if calls:
                return self._resp(model, "", calls)
        if tools:
            return self._resp(model, "Research complete; ready to summarize.", [])

        payload = final_fn(ticker)
        # stamp the placeholder retrieved_at with something honest-looking
        text = json.dumps(payload).replace("RETRIEVED_AT", "1970-01-01T00:00:00+00:00 (demo)")
        return self._resp(model, text, [])

    def _resp(self, model: str, text: str, calls: list[ToolCall]) -> ModelResponse:
        return ModelResponse(
            provider=self.name,
            model=model,
            text=text,
            tool_calls=calls,
            tokens_in=200,
            tokens_out=max(len(text) // 4, 20),
            latency_ms=5,
        )
