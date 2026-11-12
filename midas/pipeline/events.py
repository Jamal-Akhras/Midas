"""Event flags: the committee's reading of news and filings as structured,
sourced, scored data.

The event analyst produces flags (kind, severity, direction, source) for a
ticker. Flags never open positions. A high-severity negative flag vetoes a
core-book slice (midas.strategy.core); every flag is later scored against
the ticker's forward return, so "does the LLM's event reading predict
anything?" has a ledger instead of an opinion."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from midas.agents.defs import AGENT_DEFS
from midas.audit import record_event
from midas.config import settings
from midas.models import (
    ActorType,
    EventFlag,
    PipelineRun,
    RunKind,
    RunStatus,
    ScreenCandidate,
    SystemState,
    aware,
    utcnow,
)
from midas.pipeline.runner import run_agent_stage
from midas.providers.router import ModelRouter

logger = logging.getLogger(__name__)

LAST_SCAN_KEY = "last_event_scan"


def run_event_scan(
    session_factory: Callable[[], Session], router: ModelRouter, tickers: Sequence[str]
) -> list[dict[str, Any]]:
    """One event-analyst run per ticker; every flag becomes an EventFlag row
    stamped with the price at the time, for later scoring."""
    from midas.tools.base import ToolContext
    from midas.tools.registry import TOOLS

    results: list[dict[str, Any]] = []
    for raw in tickers:
        ticker = raw.strip().upper()
        if not ticker:
            continue
        with session_factory() as db:
            run = PipelineRun(kind=RunKind.event_scan, ticker=ticker)
            db.add(run)
            db.flush()
            record_event(
                db,
                event_type="run.created",
                summary=f"Event scan run created for {ticker}",
                actor_type=ActorType.system,
                run_id=run.id,
            )
            db.commit()
            try:
                artifact, parsed = run_agent_stage(
                    db,
                    router,
                    run,
                    AGENT_DEFS["event-analyst"],
                    task_text=(
                        "Scan recent news and filings for this ticker and report "
                        "structured event flags. No flags is a normal answer."
                    ),
                    ticker=ticker,
                )
                price: float | None = None
                try:
                    quote = ToolContext(db, run_id=run.id, ticker=ticker).executor(TOOLS)(
                        "get_quote", {"ticker": ticker}
                    )
                    price = float(quote["data"]["price"])
                except Exception:  # a flag without a price still counts; it just cannot be scored
                    logger.warning("no quote for %s at flag time", ticker)
                flags = []
                for item in parsed.flags:
                    flag = EventFlag(
                        run_id=run.id,
                        artifact_id=artifact.id,
                        ticker=ticker,
                        kind=item.kind,
                        severity=item.severity,
                        direction=item.direction,
                        summary=item.summary,
                        source_url=item.source_url,
                        event_date=item.event_date,
                        price_at_flag=price,
                    )
                    db.add(flag)
                    flags.append(flag)
                db.flush()
                run.status = RunStatus.done
                record_event(
                    db,
                    event_type="flags.recorded",
                    summary=f"{ticker}: {len(flags)} event flag(s)"
                    + (
                        " ["
                        + ", ".join(f"{f.severity} {f.direction} {f.kind}" for f in flags)
                        + "]"
                        if flags
                        else ""
                    ),
                    actor_type=ActorType.agent,
                    run_id=run.id,
                    payload={"n_flags": len(flags), "unverified": parsed.unverified},
                )
                db.commit()
                results.append(
                    {
                        "ticker": ticker,
                        "flags": [
                            {"kind": f.kind, "severity": f.severity, "direction": f.direction}
                            for f in flags
                        ],
                    }
                )
            except Exception as exc:
                logger.exception("event scan failed for %s", ticker)
                run.status = RunStatus.failed
                run.error = f"{type(exc).__name__}: {str(exc)[:500]}"
                record_event(db, event_type="run.failed", summary=run.error, run_id=run.id)
                db.commit()
                results.append({"ticker": ticker, "error": type(exc).__name__})
    return results


def update_flag_outcomes(db: Session, max_quotes: int = 10) -> int:
    """Fill forward returns on flags (~10 and ~20 sessions: 14/28 days),
    the same way the screen ledger is scored."""
    from midas.tools.base import ToolContext
    from midas.tools.registry import TOOLS

    now = utcnow()
    done = 0
    rows = db.scalars(select(EventFlag).where(EventFlag.fwd_20d_pct.is_(None))).all()
    for row in rows:
        if done >= max_quotes:
            break
        if not row.price_at_flag:
            continue
        age = (now - aware(row.created_at)).days
        needs_10 = row.fwd_10d_pct is None and age >= 14
        needs_20 = age >= 28
        if not (needs_10 or needs_20):
            continue
        try:
            execute = ToolContext(db, ticker=row.ticker).executor(TOOLS)
            price = float(execute("get_quote", {"ticker": row.ticker})["data"]["price"])
        except Exception:
            db.rollback()
            continue
        pct = round((price / row.price_at_flag - 1) * 100, 2)
        if needs_10:
            row.fwd_10d_pct = pct
        if needs_20:
            row.fwd_20d_pct = pct
            if row.fwd_10d_pct is None:
                row.fwd_10d_pct = pct
        done += 1
    db.flush()
    return done


def flag_stats(db: Session) -> dict[str, Any]:
    """Do negative flags precede weak forward returns? Compared against the
    screen ledger's average as the baseline for 'a stock Midas looked at'."""
    flags = db.scalars(select(EventFlag)).all()
    scored = [f for f in flags if f.fwd_10d_pct is not None]
    negative = [f for f in scored if f.direction == "negative"]
    neg_high = [f for f in negative if f.severity == "high"]
    positive = [f for f in scored if f.direction == "positive"]
    baseline = [
        c.fwd_10d_pct
        for c in db.scalars(select(ScreenCandidate).where(ScreenCandidate.fwd_10d_pct.isnot(None)))
        if c.fwd_10d_pct is not None
    ]

    def avg(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 2) if values else None

    return {
        "note": "fwd 10-session returns after a flag vs the screen ledger baseline; "
        "the veto earns its keep only if negative flags run below baseline",
        "n_flags": len(flags),
        "n_scored": len(scored),
        "avg_fwd_10d_negative_pct": avg(
            [f.fwd_10d_pct for f in negative if f.fwd_10d_pct is not None]
        ),
        "avg_fwd_10d_negative_high_pct": avg(
            [f.fwd_10d_pct for f in neg_high if f.fwd_10d_pct is not None]
        ),
        "avg_fwd_10d_positive_pct": avg(
            [f.fwd_10d_pct for f in positive if f.fwd_10d_pct is not None]
        ),
        "avg_fwd_10d_screen_baseline_pct": avg(baseline),
    }


def maybe_event_scan(
    session_factory: Callable[[], Session], router: ModelRouter
) -> list[dict[str, Any]] | None:
    """On configured weekdays (EVENT_SCAN_WEEKDAYS), scan the core universe,
    the watchlist, every open swing position and every approved thesis once
    per day. Empty config = manual only. (The scan once covered the ETFs
    and the watchlist but not NVDA, the one open swing position.)"""
    days = {d.strip().lower()[:3] for d in settings.event_scan_weekdays.split(",") if d.strip()}
    if not days or utcnow().strftime("%a").lower()[:3] not in days:
        return None
    today = utcnow().date().isoformat()
    with session_factory() as db:
        row = db.get(SystemState, LAST_SCAN_KEY)
        if row is not None and str(row.value.get("at", ""))[:10] == today:
            return None
        if row is None:
            db.add(SystemState(key=LAST_SCAN_KEY, value={"at": utcnow().isoformat()}))
        else:
            row.value = {"at": utcnow().isoformat()}
        db.commit()
    from midas.models import SWING_BOOK, Position, PositionStatus, ThesisStatus, TradeThesis
    from midas.strategy.core import core_universe

    with session_factory() as db:
        held = [
            p.ticker
            for p in db.scalars(
                select(Position).where(
                    Position.status == PositionStatus.open, Position.book == SWING_BOOK
                )
            )
        ]
        approved = [
            t.ticker
            for t in db.scalars(
                select(TradeThesis).where(
                    TradeThesis.status == ThesisStatus.approved, TradeThesis.book == SWING_BOOK
                )
            )
        ]
    tickers = list(dict.fromkeys([*core_universe(), *settings.watchlist_tickers, *held, *approved]))
    return run_event_scan(session_factory, router, tickers)
