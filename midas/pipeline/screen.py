"""Universe screen: Screener Agent scans for setup candidates, then a
deterministic filter re-checks price/volume floors on the cited numbers."""

import logging
from collections.abc import Callable

from sqlalchemy.orm import Session

from midas.agents.defs import AGENT_DEFS
from midas.audit import record_event
from midas.config import settings
from midas.models import ActorType, PipelineRun, RunKind, RunStatus
from midas.pipeline.runner import run_agent_stage
from midas.providers.router import ModelRouter
from midas.risk.engine import get_halt
from midas.tools.demo_data import DEMO_UNIVERSE

logger = logging.getLogger(__name__)


def discover_tickers() -> list[str]:
    """Deterministic discovery: Alpaca's most-actives and market movers.
    Zero AI cost - these only JOIN the screening list; the screener and the
    committee still apply every floor and gate. Empty on any failure."""
    if settings.deep_scan_enabled:
        return []  # superseded: the deep scan does this job WITH floors -
        # most-actives-by-share-volume is structurally penny junk
    if not (
        settings.discovery_enabled
        and settings.alpaca_api_key
        and settings.alpaca_api_secret
        and not settings.effective_demo_mode
    ):
        return []
    import httpx

    headers = {
        "APCA-API-KEY-ID": settings.alpaca_api_key,
        "APCA-API-SECRET-KEY": settings.alpaca_api_secret,
    }
    found: list[str] = []
    top = max(int(settings.discovery_top), 0)
    try:
        r = httpx.get(
            f"{settings.alpaca_data_base_url}/v1beta1/screener/stocks/most-actives",
            params={"by": "volume", "top": top},
            headers=headers,
            timeout=15,
        )
        if r.status_code == 200:
            found += [x["symbol"] for x in r.json().get("most_actives") or []]
        r = httpx.get(
            f"{settings.alpaca_data_base_url}/v1beta1/screener/stocks/movers",
            params={"top": top},
            headers=headers,
            timeout=15,
        )
        if r.status_code == 200:
            body = r.json()
            found += [x["symbol"] for x in (body.get("gainers") or [])]
    except Exception as exc:
        logger.warning("discovery failed: %s", type(exc).__name__)
        return []
    # plain common stock tickers only; skip weird symbols (units/warrants)
    return [t.upper() for t in dict.fromkeys(found) if t.isalpha() and 1 <= len(t) <= 5]


def universe_tickers() -> list[str]:
    if settings.universe == "demo":
        base = list(DEMO_UNIVERSE)
    elif settings.universe == "sp500":
        # The S&P 500 constituent list needs a data source; watchlist covers
        # the personal case until then. Honest failure beats a stale list.
        base = []
    else:
        base = [t.strip().upper() for t in settings.universe.split(",") if t.strip()]
    for t in settings.watchlist_tickers:
        if t not in base:
            base.append(t)
    for t in discover_tickers():
        if t not in base:
            base.append(t)
    return base


def chart_facts(db: Session, run: PipelineRun, tickers: list[str]) -> dict[str, dict | None]:
    """The code-computed technical block per ticker (None where the bars
    failed), with SPY as the relative-strength benchmark. Computed once,
    before the screener runs, and reused by the gate."""
    from midas.pipeline.committee import CONTEXT_BARS, technical_block
    from midas.tools.base import ToolContext
    from midas.tools.registry import TOOLS

    bench: list[dict] | None = None
    try:
        execute = ToolContext(db, run_id=run.id, ticker="SPY").executor(TOOLS)
        bench = execute("get_daily_bars", {"ticker": "SPY", "days": CONTEXT_BARS})["data"]
    except Exception as exc:  # noqa: BLE001 - relative strength is a nicety
        logger.warning("chart facts: SPY bars unavailable (%s)", type(exc).__name__)
    facts: dict[str, dict | None] = {}
    for ticker in tickers:
        try:
            execute = ToolContext(db, run_id=run.id, ticker=ticker).executor(TOOLS)
            bars = execute("get_daily_bars", {"ticker": ticker, "days": CONTEXT_BARS})["data"]
            facts[ticker] = technical_block(bars, benchmark_bars=bench)
        except Exception as exc:  # noqa: BLE001 - best-effort per ticker
            logger.warning("chart facts: bars unavailable for %s (%s)", ticker, type(exc).__name__)
            facts[ticker] = None
    return facts


def screen_task_text(
    tickers: list[str],
    scan_scores: list[str],
    facts: dict[str, dict | None] | None = None,
    scores: dict[str, float] | None = None,
) -> str:
    """The screener's task: the universe, the floors, the deep scan's quant
    ranking when there is one (a structural anchor the model did not have
    before), the code-computed chart facts per ticker, and the gate that
    runs after it."""
    from midas.pipeline.setup_gate import facts_line

    lines = [
        f"Scan this universe for setup candidates. Universe: {', '.join(tickers)}",
        f"Floors (deterministically re-checked): price >= ${settings.min_price_usd}, "
        f"avg daily volume >= {settings.min_avg_daily_volume:,}.",
    ]
    if scan_scores:
        lines.append(
            "Deep-scan quant scores today (0-100, code-computed from completed bars: "
            "uptrend, orderly pullback, contracting volume): "
            + ", ".join(scan_scores)
            + ". A universe name without a score was not scored, not rejected."
        )
    if facts:
        lines.append(
            "Chart facts (code-computed from completed bars; the setup label is assigned "
            "in code and is the one the gate checks - rank and explain, do not relabel):"
        )
        for ticker in tickers:
            if ticker in facts:
                lines.append(facts_line(ticker, facts[ticker], (scores or {}).get(ticker)))
    if settings.setup_gate_enabled:
        lines.append(
            "Setup gate (deterministic, after you): a candidate below its 50-day average, "
            f"more than {settings.setup_gate_max_pullback_pct:.0f}% off its 20-day high, "
            f"or down more than {settings.setup_gate_max_5d_drop_pct:.0f}% in five sessions "
            "is dropped in code; labelling one as a setup wastes the slot."
        )
    return "\n".join(lines)


def apply_setup_gate(
    db: Session,
    run: PipelineRun,
    candidates: list[dict],
    facts: dict[str, dict | None] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Check each floor-passing candidate against the code-computed
    technical block (`facts` when the screen computed them, else fetched
    here); drop the charts no setup describes and record why. Bars that
    cannot be fetched leave the candidate in: the gate removes broken
    charts, it does not punish missing data."""
    if not settings.setup_gate_enabled:
        return candidates, []
    from midas.pipeline.committee import CONTEXT_BARS, technical_block
    from midas.pipeline.setup_gate import setup_gate
    from midas.tools.base import ToolContext
    from midas.tools.registry import TOOLS

    kept: list[dict] = []
    gated: list[dict] = []
    for cand in candidates:
        ticker = str(cand.get("ticker", "")).upper()
        if facts is not None and ticker in facts:
            tech = facts[ticker]
            if tech is None:
                kept.append(cand)
                continue
        else:
            try:
                execute = ToolContext(db, run_id=run.id, ticker=ticker).executor(TOOLS)
                bars = execute("get_daily_bars", {"ticker": ticker, "days": CONTEXT_BARS})["data"]
                tech = technical_block(bars)
            except Exception as exc:  # noqa: BLE001 - best-effort per ticker
                logger.warning(
                    "setup gate: bars unavailable for %s (%s)", ticker, type(exc).__name__
                )
                kept.append(cand)
                continue
        failures = setup_gate(tech, str(cand.get("screener_setup") or cand.get("setup") or ""))
        if not failures:
            kept.append(cand)
            continue
        gated.append(
            {
                "ticker": ticker,
                "setup": cand.get("setup"),
                "screener_setup": cand.get("screener_setup", ""),
                "score": cand.get("score"),
                "reasons": failures,
                "technical": tech,
            }
        )
        record_event(
            db,
            event_type="screen.gated",
            summary=f"{ticker}: dropped by the setup gate - " + "; ".join(failures),
            run_id=run.id,
        )
    return kept, gated


def run_screen(session_factory: Callable[[], Session], router: ModelRouter) -> dict:
    """Returns the screen artifact content (candidates already floor-checked).
    Own session; commits per step."""
    with session_factory() as db:
        halt = get_halt(db)
        if halt:
            raise RuntimeError(f"pipeline halted: {halt['reason']}")
        run = PipelineRun(kind=RunKind.screen)
        db.add(run)
        db.flush()
        record_event(
            db,
            event_type="run.created",
            summary="Screen run created",
            actor_type=ActorType.operator,
            run_id=run.id,
        )
        db.commit()
        run_id = run.id

    with session_factory() as db:
        run = db.get_one(PipelineRun, run_id)
        tickers = universe_tickers()
        from midas.models import SystemState
        from midas.models.base import utcnow as _now

        ds = db.get(SystemState, "deep_scan")
        scan_scores: list[str] = []
        scores: dict[str, float] = {}
        if ds and ds.value.get("date") == _now().date().isoformat():
            for cand in ds.value.get("kept", []):
                t = str(cand.get("ticker", "")).upper()
                if not t:
                    continue
                if t not in tickers:
                    tickers.append(t)
                try:
                    scores[t] = float(cand.get("score") or 0)
                    scan_scores.append(f"{t} {scores[t]:.0f}")
                except (TypeError, ValueError):
                    scan_scores.append(t)
        facts = chart_facts(db, run, tickers)
        db.commit()  # the bar snapshots are the screen's record even if the model fails
        try:
            _, parsed = run_agent_stage(
                db,
                router,
                run,
                AGENT_DEFS["screener"],
                task_text=screen_task_text(tickers, scan_scores, facts, scores),
            )
        except Exception as exc:
            run.status = RunStatus.failed
            run.error = f"screen failed ({type(exc).__name__}): {str(exc)[:800]}"
            record_event(db, event_type="run.failed", summary=run.error, run_id=run.id)
            db.commit()
            raise

        content = parsed.model_dump()
        # a candidate the screener found outside the list it was given still
        # gets its chart facts, so every label the committee reads is the code's
        extra = [
            t
            for t in {str(c.get("ticker", "")).upper() for c in content["candidates"]}
            if t and t not in facts
        ]
        if extra:
            facts.update(chart_facts(db, run, sorted(extra)))
        kept, dropped = [], []
        for cand in content["candidates"]:
            # the setup label is the code's; the screener's own is kept beside it
            tech = facts.get(str(cand.get("ticker", "")).upper())
            if tech is not None:
                cand["screener_setup"] = cand.get("setup", "")
                cand["setup"] = tech["setup"]
                cand["setup_note"] = tech["setup_note"]
            if (
                cand["price"] >= settings.min_price_usd
                and cand["avg_volume"] >= settings.min_avg_daily_volume
            ):
                kept.append(cand)
            else:
                dropped.append({"ticker": cand["ticker"], "reason": "below price/volume floor"})
        kept, gated = apply_setup_gate(db, run, kept, facts)
        content["candidates"] = kept
        content["floor_rejections"] = dropped
        content["setup_rejections"] = gated
        from midas.models import ScreenCandidate

        for cand in kept:
            db.add(
                ScreenCandidate(
                    run_id=run.id,
                    ticker=str(cand.get("ticker", "")).upper(),
                    score=float(cand.get("score") or 0),
                    price_at_screen=float(cand.get("price") or 0),
                )
            )
        run.status = RunStatus.done
        record_event(
            db,
            event_type="run.completed",
            summary=(
                f"Screen kept {len(kept)} candidate(s), floor-dropped {len(dropped)}, "
                f"gate-dropped {len(gated)}"
            ),
            run_id=run.id,
            payload={
                "kept": [c["ticker"] for c in kept],
                "gated": [g["ticker"] for g in gated],
            },
        )
        db.commit()
        return content
