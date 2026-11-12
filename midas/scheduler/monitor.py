"""Scheduler/monitor loop: the recurring half Verdict never needed.

run_monitor_once() is idempotent and safe to call from cron, a systemd
timer, or `midas monitor --loop`. It:

1. sweeps stale runs (crash recovery),
2. refreshes marks + enforces loss-limit halts,
3. executes approved-but-unexecuted theses (L1+),
4. re-evaluates open positions that are due (scheduled cadence, earnings
   proximity, invalidation price hit, DTE decay),
5. scores newly closed positions.
"""

import logging
import time as _time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from midas.agents.defs import AGENT_DEFS
from midas.audit import record_event
from midas.config import settings
from midas.execution import executor, paper_broker
from midas.models import (
    SWING_BOOK,
    ActorType,
    CheckpointStatus,
    CheckpointTrigger,
    Direction,
    Instrument,
    OrderStatus,
    PipelineRun,
    Position,
    PositionStatus,
    ResearchArtifact,
    ReviewCheckpoint,
    RunKind,
    RunStatus,
    ThesisStatus,
    TradeThesis,
    aware,
    utcnow,
)
from midas.outcomes.scoring import score_closed_positions
from midas.pipeline.runner import render_content, run_agent_stage
from midas.providers.router import ModelRouter
from midas.risk import engine as risk_engine

logger = logging.getLogger(__name__)

STALE_RUN_MINUTES = 60


def sweep_stale_runs(db: Session, max_age_minutes: int = STALE_RUN_MINUTES) -> int:
    """Fail 'running' runs abandoned by a dead process so tickers can run
    again. Completed artifacts survive; a resumed run skips past them."""
    cutoff = utcnow() - timedelta(minutes=max_age_minutes)
    stale = [
        r
        for r in db.scalars(select(PipelineRun).where(PipelineRun.status == RunStatus.running))
        if aware(r.updated_at or r.created_at) < cutoff
    ]
    for r in stale:
        r.status = RunStatus.failed
        r.error = "orphaned by process restart"
        record_event(
            db,
            event_type="run.failed",
            summary=f"run {r.id} orphaned by process restart",
            run_id=r.id,
        )
    if stale:
        db.flush()
        logger.info("sweep: failed %d orphaned run(s)", len(stale))
    return len(stale)


def due_positions(db: Session) -> list[tuple[Position, CheckpointTrigger, str]]:
    """Open positions needing review now, with the trigger and a reason.

    Priority: invalidation (always) > earnings proximity > DTE decay >
    scheduled cadence. Event triggers other than invalidation respect a
    20-hour cooldown so a standing condition reviews once a day, not once
    a pass."""
    from midas.models import SystemState

    now = utcnow()
    due: list[tuple[Position, CheckpointTrigger, str]] = []
    breach_row = db.get(SystemState, BREACH_STATE_KEY)
    breaches: dict[str, Any] = dict(breach_row.value) if breach_row else {}
    for p in db.scalars(
        select(Position).where(Position.status == PositionStatus.open, Position.book == SWING_BOOK)
    ):
        thesis = db.get(TradeThesis, p.thesis_id)
        recently_reviewed = p.last_reviewed_at is not None and now - aware(
            p.last_reviewed_at
        ) < timedelta(hours=20)
        # invalidation price hit (event trigger beats the calendar). The stop
        # lives on the UNDERLYING, so options check the underlying quote,
        # not the option mark.
        underlying_price = _underlying_price(db, p)
        stop = p.stop_price or (thesis.stop_price if thesis else 0)
        if underlying_price is not None and thesis is not None and stop:
            breached = (
                underlying_price <= stop
                if thesis.direction == Direction.bullish
                else underlying_price >= stop
            )
            if breached:
                reason = _stop_breach_verdict(db, p, thesis, underlying_price, stop, breaches)
                if reason is not None:
                    due.append((p, CheckpointTrigger.invalidation, reason))
                continue  # a contested stop is the stop layer's; no LLM review meanwhile
            if str(p.id) in breaches:
                breaches.pop(str(p.id), None)
                record_event(
                    db,
                    event_type="stop.breach_cleared",
                    summary=f"{p.symbol}: reclaimed its stop {stop} ({underlying_price})",
                    actor_type=ActorType.system,
                    subject_type="position",
                    subject_id=p.id,
                )
        # earnings inside the lookahead window (matters doubly for options)
        earnings_note = None if recently_reviewed else _earnings_proximity(db, p)
        if earnings_note:
            due.append((p, CheckpointTrigger.earnings, earnings_note))
            continue
        # DTE decay for options
        if (
            p.instrument == Instrument.option
            and p.option_detail.get("expiry")
            and not recently_reviewed
        ):
            from datetime import date

            dte = (date.fromisoformat(p.option_detail["expiry"]) - date.today()).days
            if dte <= settings.dte_exit_review_threshold:
                due.append((p, CheckpointTrigger.dte, f"{dte} DTE remaining"))
                continue
        # scheduled cadence
        if p.next_review_at is None or aware(p.next_review_at) <= now:
            due.append((p, CheckpointTrigger.scheduled, "scheduled re-evaluation"))
    if breaches or breach_row is not None:
        _upsert_state(db, BREACH_STATE_KEY, breaches)
    return due


BREACH_STATE_KEY = "stop_breaches"


def _minutes_since_open() -> float | None:
    """Minutes since 09:30 New York (negative before the open); None when
    the clock cannot be read, which the guard treats as 'past the open'."""
    try:
        from zoneinfo import ZoneInfo

        et = utcnow().astimezone(ZoneInfo("America/New_York"))
    except Exception:
        return None
    return round((et.hour - 9) * 60 + et.minute - 30 + et.second / 60, 2)


def _stop_breach_verdict(
    db: Session,
    p: Position,
    thesis: TradeThesis,
    price: float,
    stop: float,
    breaches: dict[str, Any],
) -> str | None:
    """A breached stop becomes an exit reason (str) or stays pending (None).

    Three layers, all code: the DISASTER FLOOR (stop minus (multiple-1)
    original risk units) exits at once at any time; inside the opening
    guard an ordinary breach only logs; after it, the breach must hold on
    STOP_CONFIRMATION_PASSES consecutive passes. NVDA 2026-11-12: stop
    159.43 hit at 155.60 four minutes after the open, 162.83 twenty minutes
    later. `breaches` is the per-position pass count, persisted in
    system_state so a restart does not forget it."""
    pid = str(p.id)
    bullish = thesis.direction == Direction.bullish
    multiple = float(settings.stop_disaster_multiple)
    r0 = abs(p.avg_price - (thesis.stop_price or stop))
    if multiple > 1.0 and r0 > 0:
        floor = stop - (multiple - 1.0) * r0 if bullish else stop + (multiple - 1.0) * r0
        if (price <= floor) if bullish else (price >= floor):
            breaches.pop(pid, None)
            return (
                f"underlying {price} breached disaster floor {round(floor, 2)} "
                f"(stop {stop}; floor = stop minus {multiple - 1.0:g} risk unit)"
            )
    today = utcnow().date().isoformat()
    entry = dict(breaches.get(pid) or {})

    def _note(kind: str) -> None:
        if entry.get("noted") == today:
            return
        entry["noted"] = today
        record_event(
            db,
            event_type="stop.breach_pending",
            summary=f"{p.symbol}: {price} below stop {stop} - {kind}; "
            f"disaster floor would exit at once",
            actor_type=ActorType.system,
            subject_type="position",
            subject_id=p.id,
        )

    guard = int(settings.stop_opening_guard_minutes)
    minutes = _minutes_since_open()
    if guard > 0 and minutes is not None and minutes < guard:
        _note(f"inside the {guard}-minute opening guard, not acted on")
        breaches[pid] = {**entry, "count": 0}
        return None
    passes = max(int(settings.stop_confirmation_passes), 1)
    count = int(entry.get("count") or 0) + 1
    if count >= passes:
        breaches.pop(pid, None)
        confirmed = f" (confirmed on {count} consecutive passes)" if passes > 1 else ""
        return f"underlying {price} breached stop {stop}{confirmed}"
    _note(f"pass {count} of {passes} needed to confirm")
    breaches[pid] = {
        **entry,
        "count": count,
        "first_at": entry.get("first_at") or utcnow().isoformat(),
        "price": price,
    }
    return None


def _earnings_proximity(db: Session, p: Position) -> str | None:
    """Earnings date within the lookahead window, read from the latest
    catalysts/fundamentals artifacts (tool-sourced, snapshotted data)."""
    from datetime import date

    from midas.models import ArtifactType
    from midas.pipeline.runner import latest_artifact_for_ticker

    date_str = ""
    cat = latest_artifact_for_ticker(db, p.ticker, ArtifactType.catalysts)
    if cat is not None:
        date_str = cat.content.get("upcoming_earnings_date") or ""
    if not date_str:
        fund = latest_artifact_for_ticker(db, p.ticker, ArtifactType.fundamentals)
        if fund is not None:
            date_str = fund.content.get("next_earnings_date") or ""
    if not date_str:
        return None
    try:
        days = (date.fromisoformat(date_str) - date.today()).days
    except ValueError:
        return None
    if 0 <= days <= settings.earnings_lookahead_days:
        return f"earnings on {date_str} ({days} day(s) away)"
    return None


def _underlying_price(db: Session, p: Position) -> float | None:
    from midas.tools.base import ToolContext
    from midas.tools.registry import TOOLS

    ticker = p.ticker  # read attributes BEFORE any flush can poison the session
    try:
        execute = ToolContext(db, ticker=ticker).executor(TOOLS)
        return float(execute("get_quote", {"ticker": ticker})["data"]["price"])
    except Exception:
        try:
            db.rollback()  # un-poison the session; the pass must survive
        except Exception:
            pass
        logger.warning("could not fetch underlying quote for %s", ticker)
        return None


def execute_stop_exit(session_factory: Callable[[], Session], position_id, reason: str) -> str:
    """Invalidation-price breach: the exit is DETERMINISTIC CODE. The
    reevaluator never gets a vote on a breached stop - a stop a model can
    override is not a stop. Routed through the autonomy ladder ($0 AI)."""
    from midas.models import ApprovalRequest, ApprovalStatus

    with session_factory() as db:
        position = db.get(Position, position_id)
        if position is None or position.status != PositionStatus.open:
            return "already closed"
        pending = db.scalar(
            select(ApprovalRequest).where(
                ApprovalRequest.subject_type == "order_proposal",
                ApprovalRequest.subject_id == position.id,
                ApprovalRequest.status == ApprovalStatus.pending,
            )
        )
        if pending is not None:
            return "awaiting operator"
        db.add(
            ReviewCheckpoint(
                position_id=position.id,
                due_at=utcnow(),
                trigger=CheckpointTrigger.invalidation,
                status=CheckpointStatus.done,
                decision="exit",
                notes=f"{reason} (deterministic code exit)",
            )
        )
        record_event(
            db,
            event_type="stop.breach_exit",
            summary=f"{position.symbol}: {reason} - deterministic exit",
            actor_type=ActorType.system,
            subject_type="position",
            subject_id=position.id,
        )
        executor.propose_or_execute_exit(
            db,
            position,
            decision="exit",
            reasons=[reason, "stop breach exits are code, not a model decision"],
        )
        db.commit()
        return "exit"


def reevaluate_position(
    session_factory: Callable[[], Session],
    router: ModelRouter,
    position_id,
    trigger: CheckpointTrigger,
    reason: str,
) -> str:
    """Run the re-evaluation agent for one position and route its decision
    through the autonomy ladder. Returns the decision."""
    with session_factory() as db:
        position = db.get(Position, position_id)
        assert position is not None, f"review of unknown position {position_id}"
        thesis = db.get(TradeThesis, position.thesis_id)
        assert thesis is not None, f"position {position_id} has no thesis"
        run = PipelineRun(
            kind=RunKind.reevaluation, ticker=position.ticker, position_id=position.id
        )
        db.add(run)
        db.flush()
        checkpoint = ReviewCheckpoint(
            position_id=position.id,
            due_at=utcnow(),
            trigger=trigger,
            notes=reason,
        )
        db.add(checkpoint)

        thesis_block = render_content(
            {
                "ticker": thesis.ticker,
                "direction": thesis.direction.value,
                "instrument": thesis.instrument.value,
                "entry_zone": [thesis.entry_zone_low, thesis.entry_zone_high],
                "stop_price": thesis.stop_price,
                "targets": thesis.targets,
                "thesis_summary": thesis.thesis_summary,
                "invalidators": thesis.invalidators,
            }
        )
        position_block = render_content(
            {
                "symbol": position.symbol,
                "qty": position.qty,
                "avg_price": position.avg_price,
                "last_mark_price": position.last_mark_price,
                "opened_at": str(position.opened_at),
                "option_detail": position.option_detail,
                "trigger": trigger.value,
                "trigger_reason": reason,
            }
        )
        artifact, reeval = run_agent_stage(
            db,
            router,
            run,
            AGENT_DEFS["reevaluator"],
            task_text=(
                "Re-evaluate this OPEN position against its thesis.\n\n"
                f"Original thesis:\n{thesis_block}\n\n"
                f"Position:\n{position_block}"
            ),
            ticker=position.ticker,
        )
        checkpoint.status = CheckpointStatus.done
        checkpoint.decision = reeval.decision
        checkpoint.artifact_id = artifact.id
        position.last_reviewed_at = utcnow()
        position.next_review_at = utcnow() + timedelta(days=reeval.next_review_days)
        run.status = RunStatus.done
        record_event(
            db,
            event_type="position.reviewed",
            summary=f"{position.symbol}: {reeval.decision} ({trigger.value}: {reason})",
            actor_type=ActorType.agent,
            run_id=run.id,
            subject_type="position",
            subject_id=position.id,
            payload={"decision": reeval.decision, "reasons": reeval.reasons},
        )
        if reeval.decision in ("exit", "trim", "roll"):
            executor.propose_or_execute_exit(
                db,
                position,
                decision=reeval.decision,
                reasons=reeval.reasons,
                artifact_id=artifact.id,
            )
        db.commit()
        return reeval.decision


def execute_approved_theses(db: Session) -> int:
    """L1+: submit entries for approved theses that never executed.
    When several compete for limited budget/slots, the strongest case goes
    first - allocation by bull conviction, not by arrival order."""
    if settings.autonomy_level < 1:
        return 0
    from midas.models import ArtifactType
    from midas.pipeline.runner import existing_artifact

    def _conviction(th: TradeThesis) -> float:
        try:
            art = existing_artifact(db, th.run_id, ArtifactType.bull_case)
            return float(art.content.get("conviction", 5)) if art else 5.0
        except Exception:
            return 5.0

    # swing theses only: the core book has its own executor (core.execute_plan)
    pending = sorted(
        db.scalars(
            select(TradeThesis).where(
                TradeThesis.status == ThesisStatus.approved, TradeThesis.book == SWING_BOOK
            )
        ),
        key=_conviction,
        reverse=True,
    )
    count = 0
    for thesis in pending:
        if (utcnow() - aware(thesis.created_at)).days >= 5:
            thesis.status = ThesisStatus.rejected
            record_event(
                db,
                event_type="thesis.expired",
                summary=f"{thesis.ticker}: never reached its entry "
                f"zone within 5 days - thesis expired",
                subject_type="thesis",
                subject_id=thesis.id,
            )
            continue
        try:
            order = executor.execute_thesis(
                db, thesis, approved_by=f"investor_agent(L{settings.autonomy_level})"
            )
            if order.status == OrderStatus.filled:
                count += 1
        except executor.ThesisInvalidated:
            db.commit()  # the rejection and its audit event are the record
        except executor.AwaitingEntryZone as exc:
            _note_waiting(db, thesis, str(exc))
        except executor.ExecutionRefused as exc:
            record_event(
                db,
                event_type="order.refused",
                summary=f"{thesis.ticker}: {exc}",
                subject_type="thesis",
                subject_id=thesis.id,
            )
            if "position already open" in str(exc):
                thesis.status = ThesisStatus.rejected  # permanent: stop retrying
        except Exception as exc:
            # one broken entry must not take the pass - or another thesis's
            # already-committed fill - down with it: discard its partial
            # work, note it, move on; it is retried next pass
            logger.exception("entry for %s failed", thesis.ticker)
            db.rollback()
            record_event(
                db,
                event_type="order.failed",
                summary=f"{thesis.ticker}: entry failed ({type(exc).__name__}); retried next pass",
                subject_type="thesis",
                subject_id=thesis.id,
            )
    return count


def _note_waiting(db: Session, thesis: TradeThesis, note: str) -> None:
    """A thesis above its entry zone is a normal state, not an error: audit
    it once, then stay silent until the price enters the zone or the thesis
    expires - no rejected order row and no event per pass."""
    from midas.models import AuditEvent

    seen = db.scalar(
        select(AuditEvent).where(
            AuditEvent.event_type == "thesis.waiting", AuditEvent.subject_id == thesis.id
        )
    )
    if seen is not None:
        return
    record_event(
        db,
        event_type="thesis.waiting",
        summary=f"{thesis.ticker}: {note}",
        subject_type="thesis",
        subject_id=thesis.id,
    )


def committee_cooldown(db: Session, ticker: str, price: float | None = None) -> str | None:
    """Why the auto-cycle should NOT spend a committee on this ticker now:
    its latest verdict was no_trade inside COMMITTEE_COOLDOWN_DAYS, the
    price has not entered that verdict's entry zone, and no event flag has
    landed since. None = run it. Re-asking the same question about the same
    facts is the spend the cooldown exists to stop."""
    from midas.models import ArtifactType, EventFlag
    from midas.pipeline.runner import latest_artifact_for_ticker

    days = int(settings.committee_cooldown_days)
    ticker = ticker.upper()
    if days <= 0 or not ticker:
        return None
    art = latest_artifact_for_ticker(db, ticker, ArtifactType.verdict)
    if art is None or art.content.get("verdict") != "no_trade":
        return None
    ruled_at = aware(art.created_at)
    if ruled_at < utcnow() - timedelta(days=days):
        return None
    lo, hi = art.content.get("entry_zone_low"), art.content.get("entry_zone_high")
    if price is None:
        try:
            from midas.tools.market_data import get_quote

            price = float(get_quote(ticker).data["price"])
        except Exception:
            price = None
    if price is not None and lo and hi and float(lo) <= float(price) <= float(hi):
        return None  # the price the verdict asked for has arrived
    flags = db.scalars(select(EventFlag).where(EventFlag.ticker == ticker)).all()
    if any(aware(f.created_at) > ruled_at for f in flags):
        return None  # new facts since the verdict
    return f"skipped (cooldown: no_trade on {ruled_at.date().isoformat()})"


_calendar_cache: dict = {"date": "", "open": True}


def _market_open_today() -> bool:
    """Is today a US trading day? Asks Alpaca's calendar (cached per day);
    falls back to the weekday check when unreachable. Deterministic safety
    layers (stop-breach exits, marks, standing stops) run regardless -
    this only gates LLM work that would re-read unchanged data."""
    today = utcnow().date().isoformat()
    if _calendar_cache["date"] == today:
        return _calendar_cache["open"]
    is_open = utcnow().weekday() < 5  # fallback: plain weekday
    if settings.alpaca_api_key and settings.alpaca_api_secret:
        try:
            import httpx

            r = httpx.get(
                f"{settings.alpaca_paper_base_url}/v2/calendar",
                params={"start": today, "end": today},
                headers={
                    "APCA-API-KEY-ID": settings.alpaca_api_key,
                    "APCA-API-SECRET-KEY": settings.alpaca_api_secret,
                },
                timeout=15,
            )
            if r.status_code == 200:
                is_open = any(d.get("date") == today for d in r.json())
        except Exception as exc:
            logger.warning("calendar check failed: %s", type(exc).__name__)
    _calendar_cache.update(date=today, open=is_open)
    return is_open


_clock_cache: dict = {"at": 0.0, "open": None}


def market_session_open() -> bool | None:
    """Is the market open RIGHT NOW? Alpaca's clock is exact (holidays,
    half-days, session hours). Cached 60s; falls back to calendar-day +
    RTH-window when unreachable. None = genuinely unknown."""
    if _time.monotonic() - _clock_cache["at"] < 60 and _clock_cache["open"] is not None:
        return _clock_cache["open"]
    is_open = None
    if settings.alpaca_api_key and settings.alpaca_api_secret:
        try:
            import httpx

            r = httpx.get(
                f"{settings.alpaca_paper_base_url}/v2/clock",
                headers={
                    "APCA-API-KEY-ID": settings.alpaca_api_key,
                    "APCA-API-SECRET-KEY": settings.alpaca_api_secret,
                },
                timeout=10,
            )
            if r.status_code == 200:
                is_open = bool(r.json().get("is_open"))
        except Exception as exc:
            logger.warning("clock check failed: %s", type(exc).__name__)
    if is_open is None:
        is_open = _market_open_today() and _us_market_hours()
    _clock_cache.update(at=_time.monotonic(), open=is_open)
    return is_open


_regime_cache: dict = {"date": "", "risk_on": True, "detail": ""}


def market_regime(db: Session) -> dict:
    """Risk-on/off from SPY vs its 200-day average (cached per day).
    Data unavailable -> risk-on with a warning: stops still protect, and a
    data outage should not silently freeze the whole strategy."""
    today = utcnow().date().isoformat()
    if _regime_cache["date"] == today:
        return dict(_regime_cache)
    risk_on, detail = True, "no data (defaulting risk-on)"
    if settings.regime_filter_enabled:
        try:
            from midas.tools.market_data import get_daily_bars

            bars = get_daily_bars("SPY", days=210).data
            closes = [b["close"] for b in bars]
            if len(closes) >= 150:
                ma = sum(closes[-200:]) / len(closes[-200:])
                spot = closes[-1]
                risk_on = spot >= ma
                detail = f"SPY {spot:.0f} vs 200d {ma:.0f}"
        except Exception as exc:
            logger.warning("regime check failed: %s", type(exc).__name__)
    else:
        detail = "filter disabled"
    _regime_cache.update(date=today, risk_on=risk_on, detail=detail)
    _upsert_state(db, "market_regime", {"date": today, "risk_on": risk_on, "detail": detail})
    return dict(_regime_cache)


def update_counterfactuals(db: Session, max_quotes: int = 10) -> int:
    """Fill forward returns on screened candidates: ~10 and ~20 trading
    days (14/28 calendar). The traded-vs-skipped comparison in stats() is
    only as good as this ledger."""
    from midas.models import ScreenCandidate
    from midas.tools.base import ToolContext
    from midas.tools.registry import TOOLS

    now = utcnow()
    done = 0
    rows = db.scalars(select(ScreenCandidate).where(ScreenCandidate.fwd_20d_pct.is_(None))).all()
    for row in rows:
        if done >= max_quotes:
            break
        age = (now - aware(row.created_at)).days
        needs_10 = row.fwd_10d_pct is None and age >= 14
        needs_20 = age >= 28
        if not (needs_10 or needs_20) or not row.price_at_screen:
            continue
        try:
            execute = ToolContext(db, ticker=row.ticker).executor(TOOLS)
            price = float(execute("get_quote", {"ticker": row.ticker})["data"]["price"])
        except Exception:
            db.rollback()
            continue
        pct = round((price / row.price_at_screen - 1) * 100, 2)
        if needs_10:
            row.fwd_10d_pct = pct
        if needs_20:
            row.fwd_20d_pct = pct
            if row.fwd_10d_pct is None:
                row.fwd_10d_pct = pct
        done += 1
    db.flush()
    return done


def run_post_mortems(
    session_factory: Callable[[], Session], router: ModelRouter, outcome_ids: list
) -> list[str]:
    """One cheap LLM call per closed position: what should future
    committees learn? Lessons feed the investor's context."""
    from midas.models import TradeOutcome

    lessons = []
    for oid in outcome_ids:
        with session_factory() as db:
            outcome = db.get(TradeOutcome, oid)
            if outcome is None:
                continue
            existing = db.scalar(
                select(PipelineRun).where(
                    PipelineRun.kind == RunKind.post_mortem,
                    PipelineRun.position_id == outcome.position_id,
                )
            )
            if existing is not None:
                continue  # once per position, ever
            position = db.get(Position, outcome.position_id)
            thesis = db.get(TradeThesis, outcome.thesis_id)
            if position is None or thesis is None:
                continue  # orphaned outcome: nothing to learn from
            run = PipelineRun(
                kind=RunKind.post_mortem, ticker=position.ticker, position_id=position.id
            )
            db.add(run)
            db.flush()
            record = render_content(
                {
                    "thesis": {
                        "direction": thesis.direction.value,
                        "instrument": thesis.instrument.value,
                        "entry_zone": [thesis.entry_zone_low, thesis.entry_zone_high],
                        "stop": thesis.stop_price,
                        "targets": thesis.targets,
                        "summary": thesis.thesis_summary,
                        "invalidators": thesis.invalidators,
                    },
                    "position": {
                        "symbol": position.symbol,
                        "qty": position.qty,
                        "entry": position.avg_price,
                        "opened_at": str(position.opened_at),
                        "closed_at": str(position.closed_at),
                    },
                    "outcome": {
                        "pnl_usd": outcome.pnl_usd,
                        "pnl_pct": outcome.pnl_pct,
                        "holding_days": outcome.holding_days,
                        "exit_kind": outcome.exit_kind,
                    },
                }
            )
            try:
                _, pm = run_agent_stage(
                    db,
                    router,
                    run,
                    AGENT_DEFS["postmortem"],
                    task_text=("Write the post-mortem for this closed position.\n\n" + record),
                    ticker=position.ticker,
                )
                run.status = RunStatus.done
                lessons.append(pm.lesson)
            except Exception:
                logger.exception("post-mortem failed for %s", position.symbol)
                run.status = RunStatus.failed
            db.commit()
    return lessons


def recent_lessons(db: Session, limit: int = 5) -> list[str]:
    from midas.models import ArtifactType

    arts = db.scalars(
        select(ResearchArtifact)
        .where(ResearchArtifact.artifact_type == ArtifactType.post_mortem)
        .order_by(ResearchArtifact.created_at.desc())
        .limit(limit)
    ).all()
    return [a.content.get("lesson", "") for a in arts if a.content.get("lesson")]


def _us_market_hours() -> bool:
    """Roughly inside US regular trading hours - options chains and quotes
    are only honest then. Uses the IANA zone when available; otherwise a
    conservative UTC window valid under both EST and EDT."""
    now = utcnow()
    try:
        from zoneinfo import ZoneInfo

        et = now.astimezone(ZoneInfo("America/New_York"))
        return (et.hour, et.minute) >= (9, 45) and et.hour < 16
    except Exception:
        return 14 <= now.hour < 20  # intersection of EST/EDT RTH in UTC


def _upsert_state(db: Session, key: str, value: dict) -> None:
    from midas.models import SystemState

    row = db.get(SystemState, key)
    if row is None:
        db.add(SystemState(key=key, value=value))
    else:
        row.value = value
    db.flush()


def maybe_auto_cycle(session_factory: Callable[[], Session], router: ModelRouter) -> dict | None:
    """Budget-aware cadence: on configured weekdays (AUTO_CYCLE_WEEKDAYS,
    e.g. "mon,thu"), run one screen + committees on gate-passers per day.
    Empty config = fully manual. Theses still obey the autonomy ladder."""
    days = {d.strip().lower()[:3] for d in settings.auto_cycle_weekdays.split(",") if d.strip()}
    if not days or utcnow().strftime("%a").lower()[:3] not in days:
        return None
    if settings.auto_cycle_market_hours_only and not _us_market_hours():
        return None
    if not _market_open_today():
        return None  # holidays: a 'weekday' with no market is not a trading day
    with session_factory() as db:
        if not market_regime(db)["risk_on"] and settings.regime_throttle <= 0:
            return None  # binary risk-off: no research spend on refused entries
    from datetime import datetime as _dt

    from midas.models import SystemState
    from midas.pipeline.committee import run_committee
    from midas.pipeline.screen import run_screen

    # Guard on the persisted cycle record, NOT on screen rows: an orphaned
    # 'running' screen (killed process) or a manual Screen click must never
    # block the day's cycle.
    with session_factory() as db:
        row = db.get(SystemState, "last_auto_cycle")
        val = dict(row.value) if row else {}
    if str(val.get("at", ""))[:10] == utcnow().date().isoformat():
        stuck = [t for t, s in (val.get("results") or {}).items() if s == "committee running..."]
        age_min = (utcnow() - aware(_dt.fromisoformat(val["at"]))).total_seconds() / 60
        if not stuck or age_min < 20:
            return None  # cycle done, or another process is mid-cycle
        # cycle died mid-committees: finish ONLY the stuck ones
        results = dict(val.get("results") or {})
        for t in stuck:
            try:
                r = run_committee(session_factory, router, t)
                results[t] = r.status.value
            except Exception as exc:
                logger.exception("cycle-resume committee failed for %s", t)
                results[t] = f"failed ({type(exc).__name__})"
            with session_factory() as db:
                _upsert_state(db, "last_auto_cycle", {**val, "results": results})
                db.commit()
        return {"resumed": stuck, "committees": results}

    if settings.deep_scan_enabled:
        try:
            from midas.pipeline.deep_scan import run_deep_scan

            with session_factory() as db:
                ds = run_deep_scan(db)
                db.commit()
            logger.info("deep scan kept %s setups", len(ds.get("kept", [])))
        except Exception:
            logger.exception("deep scan failed; screening universe only")
    content = run_screen(session_factory, router)
    results = {}
    candidates = sorted(
        content.get("candidates", []), key=lambda c: c.get("score", 0), reverse=True
    )
    with session_factory() as db:
        held = {p.ticker for p in risk_engine.open_positions(db)}
        cooling: dict[str, str] = {}
        for cand in candidates:
            ticker = str(cand.get("ticker") or "")
            if ticker in held:
                continue
            why = committee_cooldown(db, ticker, cand.get("price"))
            if why:
                cooling[ticker] = why
    # skipped candidates do not consume a seat: the cap counts committees run
    fresh = [
        c for c in candidates if c.get("ticker") not in held and c.get("ticker") not in cooling
    ]
    for cand in candidates:
        if cand.get("ticker") in held:
            results[cand["ticker"]] = "skipped (already held - PM covers it)"
        elif cand.get("ticker") in cooling:
            results[cand["ticker"]] = cooling[cand["ticker"]]
    kept = fresh[: max(settings.auto_cycle_max_committees, 0)]
    for cand in fresh[len(kept) :]:
        results[cand["ticker"]] = "skipped (committee cap)"

    def _persist():
        with session_factory() as db:
            _upsert_state(
                db,
                "last_auto_cycle",
                {
                    "at": utcnow().isoformat(),
                    "screened": len(candidates),
                    "candidates": [
                        {"ticker": c.get("ticker"), "score": c.get("score")} for c in candidates
                    ],
                    "results": dict(results),
                },
            )
            db.commit()

    _persist()  # visible in the UI even while committees are still running
    for cand in kept:
        t = cand["ticker"]
        results[t] = "committee running..."
        _persist()
        try:
            r = run_committee(session_factory, router, t)
            results[t] = r.status.value
        except Exception as exc:
            logger.exception("auto-cycle committee failed for %s", t)
            results[t] = f"failed ({type(exc).__name__})"
        _persist()
    return {"screened": len(candidates), "committees": results}


def _premarket_now() -> bool:
    """Between ~6:00 ET and the open on a trading day."""
    if not _market_open_today() or market_session_open():
        return False
    now = utcnow()
    try:
        from zoneinfo import ZoneInfo

        et = now.astimezone(ZoneInfo("America/New_York"))
        return 6 <= et.hour < 10
    except Exception:
        return 10 <= now.hour < 14


def premarket_gap_sweep(db: Session) -> list[str]:
    """Weekend/overnight news shows up as a premarket gap. We never TRADE
    premarket (thin, unmirrorable fills) - but a held ticker gapping hard
    gets flagged so its review fires on the FIRST pass after the open."""
    if settings.premarket_gap_alert_pct <= 0 or not _premarket_now():
        return []
    from midas.tools.base import ToolContext
    from midas.tools.registry import TOOLS

    flagged: list[str] = []
    now = utcnow()
    for p in db.scalars(
        select(Position).where(Position.status == PositionStatus.open, Position.book == SWING_BOOK)
    ):
        ticker = p.ticker
        if p.next_review_at is not None and aware(p.next_review_at) <= now:
            continue  # already due
        try:
            execute = ToolContext(db, ticker=ticker).executor(TOOLS)
            q = execute("get_quote", {"ticker": ticker})["data"]
            prev = float(q.get("prev_close") or 0)
            price = float(q.get("price") or 0)
        except Exception:
            db.rollback()
            continue
        if not prev or not price:
            continue
        gap_pct = (price - prev) / prev * 100
        if abs(gap_pct) >= settings.premarket_gap_alert_pct:
            p.next_review_at = now  # due on the first open-session pass
            record_event(
                db,
                event_type="premarket.gap_flagged",
                summary=f"{p.symbol}: premarket {gap_pct:+.1f}% vs prev close "
                f"- review scheduled for the open",
                actor_type=ActorType.system,
                subject_type="position",
                subject_id=p.id,
            )
            flagged.append(ticker)
    _upsert_state(db, "last_premarket_sweep", {"at": utcnow().isoformat(), "flagged": flagged})
    db.flush()
    return flagged


def ratchet_stops(db: Session) -> list[dict]:
    """Deterministic stop ratchet for open SHARES positions. Monotonic -
    stops only move up: to breakeven once the position is up BREAKEVEN_AT_R
    risk units, then trailing below the TRAIL_LOOKBACK_DAYS-day low. The
    ORIGINAL thesis stop is never mutated; the live stop is on the position."""
    from midas.tools.market_data import get_daily_bars

    changes: list[dict] = []
    if settings.breakeven_at_r <= 0:
        return changes
    for p in db.scalars(
        select(Position).where(
            Position.status == PositionStatus.open,
            Position.instrument == Instrument.shares,
            Position.book == SWING_BOOK,
        )
    ):
        thesis = db.get(TradeThesis, p.thesis_id)
        if thesis is None or thesis.direction != Direction.bullish:
            continue
        stop = p.stop_price or thesis.stop_price or 0.0
        mark = p.last_mark_price
        if not stop or not mark:
            continue
        r0 = p.avg_price - (thesis.stop_price or 0.0)
        new_stop = stop
        if r0 > 0 and stop < p.avg_price and mark >= p.avg_price + settings.breakeven_at_r * r0:
            new_stop = p.avg_price
        if new_stop >= p.avg_price:  # at/past breakeven: trail the lows
            try:
                bars = get_daily_bars(p.ticker, settings.trail_lookback_days).data
                if bars:
                    trail = round(
                        float(min(b["low"] for b in bars[-settings.trail_lookback_days :])), 2
                    )
                    if trail < mark:  # a trail at/above market is no trail
                        new_stop = max(new_stop, trail)
            except Exception:
                logger.warning("trail bars unavailable for %s", p.ticker)
        if new_stop >= mark:
            continue  # never rest a stop at/above the market; breach path owns that
        if new_stop > stop + 0.005:
            p.stop_price = round(new_stop, 2)
            record_event(
                db,
                event_type="stop.ratcheted",
                summary=f"{p.symbol}: stop {round(stop, 2)} -> {p.stop_price} "
                f"(mark {round(mark, 2)}, entry {round(p.avg_price, 2)})",
                actor_type=ActorType.system,
                subject_type="position",
                subject_id=p.id,
            )
            changes.append({"symbol": p.symbol, "from": round(stop, 2), "to": p.stop_price})
    db.flush()
    return changes


def apply_time_stops(db: Session) -> list[str]:
    """Deterministic time stop: a position held past MAX_HOLDING_DAYS is
    dead capital plus open risk - exit it (via the autonomy ladder)."""
    from midas.models import ApprovalRequest, ApprovalStatus

    if settings.max_holding_days <= 0:
        return []
    out: list[str] = []
    now = utcnow()
    positions = list(
        db.scalars(
            select(Position).where(
                Position.status == PositionStatus.open, Position.book == SWING_BOOK
            )
        )
    )
    for p in positions:
        days = (now - aware(p.opened_at)).days
        if days < settings.max_holding_days:
            continue
        pending = db.scalar(
            select(ApprovalRequest).where(
                ApprovalRequest.subject_type == "order_proposal",
                ApprovalRequest.subject_id == p.id,
                ApprovalRequest.status == ApprovalStatus.pending,
            )
        )
        if pending is not None:
            continue  # already waiting on the operator
        try:
            record_event(
                db,
                event_type="stop.time",
                summary=f"{p.symbol}: time stop after {days}d (limit {settings.max_holding_days}d)",
                actor_type=ActorType.system,
                subject_type="position",
                subject_id=p.id,
            )
            executor.propose_or_execute_exit(
                db,
                p,
                decision="exit",
                reasons=[f"time stop: held {days} days >= {settings.max_holding_days}-day limit"],
            )
            out.append(p.symbol)
        except Exception:
            # one position's broken exit must not roll back another's fill
            logger.exception("time stop for %s failed", p.symbol)
            db.rollback()
    return out


def _pm_review_time_reached() -> bool:
    """Is it at/after PORTFOLIO_REVIEW_AFTER_ET (New York clock) today?
    Unparseable setting = no time gate."""
    raw = str(settings.portfolio_review_after_et or "").strip()
    if not raw:
        return True
    try:
        hh, mm = (int(x) for x in raw.split(":", 1))
    except ValueError:
        return True
    now = utcnow()
    try:
        from zoneinfo import ZoneInfo

        et = now.astimezone(ZoneInfo("America/New_York"))
        return (et.hour, et.minute) >= (hh, mm)
    except Exception:
        return (now.hour, now.minute) >= (hh + 5, mm)  # EST fallback


# the system's own opinions about a position are not facts newer than it
_OPINION_ARTIFACTS = frozenset({"reevaluation", "post_mortem", "portfolio_review"})


def pm_exit_guard(db: Session, position: Position) -> str | None:
    """Why the portfolio manager may NOT exit or trim this position now.
    None = allowed. A position younger than PM_EXIT_MIN_AGE_HOURS can only
    be exited on a research artifact or event flag dated after its entry:
    the facts the committee weighed when it said trade cannot be the facts
    that overrule it minutes later (AXP, 2026-11-12)."""
    from midas.models import EventFlag

    hours = int(settings.pm_exit_min_age_hours)
    if hours <= 0:
        return None
    opened = aware(position.opened_at)
    if utcnow() - opened >= timedelta(hours=hours):
        return None
    flags = db.scalars(select(EventFlag).where(EventFlag.ticker == position.ticker)).all()
    if any(aware(f.created_at) > opened for f in flags):
        return None
    arts = db.scalars(
        select(ResearchArtifact).where(ResearchArtifact.ticker == position.ticker)
    ).all()
    for art in arts:
        kind = getattr(art.artifact_type, "value", art.artifact_type)
        if str(kind) in _OPINION_ARTIFACTS:
            continue
        if aware(art.created_at) > opened:
            return None
    age_min = int((utcnow() - opened).total_seconds() // 60)
    return (
        f"opened {age_min} min ago and nothing newer than its entry is on record "
        f"(no research artifact or event flag since); the committee's facts "
        f"stand until {hours}h or new facts"
    )


def maybe_portfolio_review(
    session_factory: Callable[[], Session], router: ModelRouter
) -> dict | None:
    """Once per day, at/after PORTFOLIO_REVIEW_AFTER_ET: the portfolio-
    manager agent reviews the WHOLE book against the freshest committee
    research and the exposure budget. Trim/exit decisions route through
    the autonomy ladder exactly like reevaluator decisions; it cannot buy,
    and it cannot exit a fresh position on the facts it was opened on
    (pm_exit_guard)."""
    from midas.models import ArtifactType
    from midas.pipeline.runner import latest_artifact_for_ticker

    if not market_session_open():
        return None  # PM exits execute at L2: only act when fills are real
    if not _pm_review_time_reached():
        return None  # not on the pass that happens to find the day's first fill
    with session_factory() as db:
        positions = risk_engine.open_positions(db)
        if not positions:
            return None
        day_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        already = [
            r
            for r in db.scalars(
                select(PipelineRun).where(PipelineRun.kind == RunKind.portfolio_review)
            )
            if aware(r.created_at) >= day_start
            and r.status not in (RunStatus.failed, RunStatus.cancelled)
        ]
        if already:
            return None

        run = PipelineRun(kind=RunKind.portfolio_review, ticker="")
        db.add(run)
        db.flush()

        equity = paper_broker.refresh_equity(db)
        deployed = risk_engine.open_notional_usd(db)
        budget = equity * risk_engine.swing_budget_pct() / 100
        book = []
        guards: dict[str, str] = {}
        for p in positions:
            thesis = db.get(TradeThesis, p.thesis_id)
            guard = pm_exit_guard(db, p)
            if guard:
                guards[p.ticker] = guard
            entry: dict[str, Any] = {
                "ticker": p.ticker,
                "symbol": p.symbol,
                "instrument": p.instrument.value,
                "qty": p.qty,
                "avg_price": p.avg_price,
                "mark": p.last_mark_price,
                "sector": p.sector,
                "opened_at": str(p.opened_at),
                "thesis_summary": thesis.thesis_summary if thesis else "",
                "stop_price": thesis.stop_price if thesis else None,
                "targets": thesis.targets if thesis else [],
                "invalidators": thesis.invalidators if thesis else [],
                "exit_allowed": guard is None,
                "exit_guard": guard
                or "exit/trim allowed: the position is old enough or facts newer than "
                "its entry exist",
            }
            fresh: dict[str, dict] = {}
            for at in (ArtifactType.verdict, ArtifactType.bull_case, ArtifactType.bear_case):
                art = latest_artifact_for_ticker(db, p.ticker, at)
                if art is not None:
                    fresh[at.value] = {"created_at": str(art.created_at), "content": art.content}
            entry["latest_research"] = fresh
            book.append(entry)

        context = render_content(
            {
                "account_equity": round(equity, 2),
                "deployed_notional": round(deployed, 2),
                "exposure_budget": round(budget, 2),
                "max_open_positions": settings.max_open_positions,
                "open_positions": book,
            },
            limit=24000,
        )

        try:
            artifact, review = run_agent_stage(
                db,
                router,
                run,
                AGENT_DEFS["portfolio-manager"],
                task_text=(
                    "Review this ENTIRE portfolio. Decide hold/trim/"
                    "exit for every open position.\n\n" + context
                ),
            )
        except Exception:
            run.status = RunStatus.failed
            db.commit()
            raise

        results: dict = {"assessment": review.assessment[:300]}
        for action in review.actions:
            pos = next(
                (
                    p
                    for p in positions
                    if p.ticker == action.ticker and p.status == PositionStatus.open
                ),
                None,
            )
            if pos is None or action.decision == "hold":
                results[action.ticker] = action.decision
                continue
            guard = guards.get(pos.ticker)
            if guard:
                # the PM may argue; code does not act on a re-litigated verdict
                results[action.ticker] = f"{action.decision} refused (guard: {guard})"
                record_event(
                    db,
                    event_type="portfolio.action_refused",
                    summary=f"PM wanted to {action.decision} {pos.symbol} - refused: "
                    f"nothing newer than its entry on record ({guard[:120]})",
                    actor_type=ActorType.system,
                    run_id=run.id,
                    subject_type="position",
                    subject_id=pos.id,
                    payload={"decision": action.decision, "reasons": action.reasons},
                )
                continue
            executor.propose_or_execute_exit(
                db,
                pos,
                decision=action.decision,
                reasons=action.reasons,
                artifact_id=artifact.id,
            )
            results[action.ticker] = f"{action.decision} routed (L{settings.autonomy_level})"
            record_event(
                db,
                event_type="portfolio.action",
                summary=f"PM: {action.decision} {pos.symbol} - {'; '.join(action.reasons)[:200]}",
                actor_type=ActorType.agent,
                run_id=run.id,
                subject_type="position",
                subject_id=pos.id,
            )
        run.status = RunStatus.done
        db.commit()
        return results


def _strategy_fingerprint() -> str:
    """Hash of everything that defines the strategy: risk caps, universe,
    model routing, and every agent prompt. If any of it changes, the L2
    evidence cohort restarts - results from strategy A are not evidence
    for strategy B."""
    import hashlib
    import json as _json
    from pathlib import Path

    from midas import providers
    from midas.agents.defs import PROMPTS_DIR

    h = hashlib.sha256()
    caps = {
        k: getattr(settings, k)
        for k in (
            "per_trade_risk_pct",
            "max_notional_pct_per_trade",
            "max_total_notional_pct",
            "max_premium_pct_per_trade",
            "max_total_premium_pct",
            "daily_loss_limit_pct",
            "weekly_loss_limit_pct",
            "max_open_positions",
            "max_positions_per_sector",
            "max_sector_notional_pct",
            "min_dte_at_entry",
            "min_open_interest",
            "max_bid_ask_spread_pct",
            "max_holding_days",
            "breakeven_at_r",
            "trail_lookback_days",
            "option_slippage_pct",
            "option_fee_per_contract",
            "universe",
            "auto_cycle_max_committees",
            "paper_starting_cash",
            "fractional_shares",
            "regime_throttle",
            "regime_filter_enabled",
            "core_enabled",
            "core_universe",
            "core_allocation_pct",
            "core_trend_window",
            "core_veto_days",
            "setup_gate_enabled",
            "setup_gate_max_pullback_pct",
            "setup_gate_max_5d_drop_pct",
            "setup_gate_max_volume_ratio",
            "pm_exit_min_age_hours",
            "stop_opening_guard_minutes",
            "stop_confirmation_passes",
            "stop_disaster_multiple",
        )
    }
    h.update(_json.dumps(caps, sort_keys=True, default=str).encode())
    try:
        h.update((Path(providers.__file__).parent / "routing.yaml").read_bytes())
        for f in sorted(PROMPTS_DIR.glob("*.md")):
            h.update(f.read_bytes())
    except Exception:
        pass
    return h.hexdigest()[:16]


def _record_l2_ledger(db: Session) -> None:
    """Immutable start-of-L2 record: the L3 bar's '30 days at L2' must be
    provable from the database, not from memory. Dropping below L2 resets
    the clock (and is audited)."""
    from midas.models import SystemState

    row = db.get(SystemState, "l2_since")
    if settings.autonomy_level >= 2:
        fp = _strategy_fingerprint()
        changed = (
            row is not None
            and row.value.get("at")
            and row.value.get("fingerprint") not in (None, fp)
        )
        if row is None or not row.value.get("at") or changed:
            value = {
                "at": utcnow().isoformat(),
                "autonomy": settings.autonomy_level,
                "universe": settings.universe,
                "fingerprint": fp,
            }
            if row is None:
                db.add(SystemState(key="l2_since", value=value))
            else:
                row.value = value
            record_event(
                db,
                event_type="autonomy.l2_started",
                summary=(
                    "L2 evidence clock RESTARTED - strategy fingerprint changed"
                    if changed
                    else "L2 evidence clock started"
                ),
                actor_type=ActorType.system,
            )
    elif row is not None and row.value.get("at"):
        row.value = {}
        record_event(
            db,
            event_type="autonomy.l2_reset",
            summary="autonomy dropped below L2 - evidence clock reset",
            actor_type=ActorType.system,
        )
    db.flush()


PASS_LOCK_KEY = "monitor_pass_lock"
PASS_LOCK_TTL_MINUTES = 25


def acquire_pass_lock(db: Session) -> str | None:
    """Single-writer discipline across processes: only one monitor pass
    (loop, UI button, or maintenance script) may run at a time. Returns a
    token, or None when another holder is active and unexpired."""
    import uuid as _uuid
    from datetime import datetime as _dt

    from midas.models import SystemState

    now = utcnow()
    row = db.get(SystemState, PASS_LOCK_KEY)
    if row and row.value.get("expires_at"):
        try:
            if aware(_dt.fromisoformat(row.value["expires_at"])) > now:
                return None
        except ValueError:
            pass
    token = _uuid.uuid4().hex
    value = {
        "holder": token,
        "expires_at": (now + timedelta(minutes=PASS_LOCK_TTL_MINUTES)).isoformat(),
    }
    if row is None:
        db.add(SystemState(key=PASS_LOCK_KEY, value=value))
    else:
        row.value = value
    db.commit()
    check = db.get(SystemState, PASS_LOCK_KEY)
    return token if check and check.value.get("holder") == token else None


def release_pass_lock(db: Session, token: str) -> None:
    from midas.models import SystemState

    row = db.get(SystemState, PASS_LOCK_KEY)
    if row and row.value.get("holder") == token:
        row.value = {}
        db.commit()


def run_monitor_once(
    session_factory: Callable[[], Session], router: ModelRouter, interval_minutes: int | None = None
) -> dict:
    with session_factory() as db:
        token = acquire_pass_lock(db)
    if token is None:
        return {"skipped": "another monitor pass holds the lock"}
    try:
        return _run_monitor_pass(session_factory, router, interval_minutes)
    finally:
        with session_factory() as db:
            release_pass_lock(db, token)


def run_monitor_forever(
    session_factory: Callable[[], Session],
    router: ModelRouter,
    interval_minutes: int,
    *,
    sleep: Callable[[float], None] = _time.sleep,
    on_summary: Callable[[dict], None] | None = None,
) -> None:
    """`midas monitor --loop`: a pass every `interval_minutes`, until interrupted.

    A pass that raises outside the per-stage guards is logged and the loop
    carries on. A month-long paper run must not end because one network call
    failed at the top of a pass."""
    while True:
        try:
            summary = run_monitor_once(session_factory, router, interval_minutes)
            if on_summary is not None:
                on_summary(summary)
        except KeyboardInterrupt:
            logger.info("monitor loop stopped")
            return
        except Exception:
            logger.exception("monitor pass failed; next attempt in %d minutes", interval_minutes)
        try:
            sleep(interval_minutes * 60)
        except KeyboardInterrupt:
            logger.info("monitor loop stopped")
            return


def _run_monitor_pass(
    session_factory: Callable[[], Session], router: ModelRouter, interval_minutes: int | None = None
) -> dict:
    """One monitor pass. Returns a summary dict for the CLI/logs."""
    from midas.config import reload_settings

    reload_settings()  # Settings-tab changes apply within one pass
    summary: dict[str, Any] = {
        "swept": 0,
        "executed": 0,
        "reviewed": [],
        "scored": 0,
        "halted": None,
        "auto_cycle": None,
    }
    with session_factory() as db:
        summary["swept"] = sweep_stale_runs(db)
        equity = paper_broker.refresh_equity(db)
        from midas.models.history import record_equity_point

        record_equity_point(db)
        broker_acct = None
        if settings.alpaca_mirror_orders:
            broker_acct = paper_broker.fetch_alpaca_account()
        if broker_acct and broker_acct["equity"] > 0:
            _upsert_state(
                db,
                "broker_equity",
                {
                    "at": utcnow().isoformat(),
                    "alpaca": broker_acct["equity"],
                    "internal": round(equity, 2),
                },
            )
            div_pct = abs(equity - broker_acct["equity"]) / broker_acct["equity"] * 100
            if div_pct > 3.0:
                record_event(
                    db,
                    event_type="ops.alert",
                    summary=(
                        f"BOOKS DIVERGENCE {div_pct:.1f}%: internal equity "
                        f"{equity:,.0f} vs Alpaca {broker_acct['equity']:,.0f}"
                        f" - one ledger is wrong; investigate before trusting either"
                    ),
                    actor_type=ActorType.system,
                )
        risk_engine.update_equity_anchors(db, equity)
        _record_l2_ledger(db)
        was_halted = risk_engine.get_halt(db) is not None
        halt_reason = risk_engine.enforce_loss_halt(db, equity)
        halt = risk_engine.get_halt(db)
        if halt is not None:
            # A halt stops NEW risk: research, entries, rebalances and LLM
            # reviews. Risk management - stop breaches, time stops, ratchets,
            # standing stops, reconciliation - runs regardless: the day the
            # account is down 3% is the day the stops matter most.
            summary["halted"] = halt_reason or str(halt.get("reason") or "halted")
            if halt_reason and not was_halted:
                record_event(db, event_type="pipeline.halted", summary=halt_reason)
        halted = halt is not None
        db.commit()  # marks, anchors and the halt are facts before any order work
        session_open = market_session_open()
        summary["session_open"] = session_open
        regime = market_regime(db)
        summary["regime"] = regime["detail"] + (" [RISK-OFF]" if not regime["risk_on"] else "")
        summary["premarket_flags"] = premarket_gap_sweep(db)
        regime_ok = regime["risk_on"] or settings.regime_throttle > 0
        summary["executed"] = (
            execute_approved_theses(db) if session_open and regime_ok and not halted else 0
        )
        if session_open and settings.core_enabled and not halted:
            try:
                from midas.strategy import core as core_book

                summary["core"] = core_book.run_rebalance(db)
            except Exception:
                logger.exception("core rebalance failed")
        db.commit()
        summary["stops_ratcheted"] = ratchet_stops(db)
        db.commit()
        summary["time_stops"] = apply_time_stops(db) if session_open else []
        try:
            summary["standing_stops"] = paper_broker.sync_standing_stops(db)
        except Exception:
            logger.exception("standing stop sync failed")
        db.commit()
        due = [(p.id, trig, reason) for p, trig, reason in due_positions(db)]

    def _review(position_id: Any, trigger: CheckpointTrigger, reason: str) -> None:
        try:
            if trigger == CheckpointTrigger.invalidation:
                decision = execute_stop_exit(session_factory, position_id, reason)
            else:
                decision = reevaluate_position(
                    session_factory, router, position_id, trigger, reason
                )
            summary["reviewed"].append(
                {"position_id": str(position_id), "trigger": trigger.value, "decision": decision}
            )
        except Exception:
            logger.exception("re-evaluation failed for position %s", position_id)

    # Closed market: no honest fill exists ANYWHERE - breaches wait for the
    # open (GTC stops fire there first anyway) and reviews would read stale
    # data. Stop-breach exits are deterministic code and go FIRST, before
    # any LLM work can delay them - halted or not.
    if session_open:
        for position_id, trigger, reason in due:
            if trigger == CheckpointTrigger.invalidation:
                _review(position_id, trigger, reason)

    if halted:
        summary["skipped_for_halt"] = [
            "entries",
            "core_rebalance",
            "auto_cycle",
            "event_scan",
            "reevaluations",
            "portfolio_review",
        ]
    else:
        try:
            summary["auto_cycle"] = maybe_auto_cycle(session_factory, router)
        except Exception:
            logger.exception("auto-cycle failed")
        if summary["auto_cycle"] is not None and session_open and regime_ok:
            # Act on the yes: a thesis the cycle just approved is executed
            # now, on the facts the committee judged - not on the next pass's
            # price up to an interval later. Same gates as the first call
            # (session open, regime; this is the not-halted branch).
            with session_factory() as db:
                try:
                    summary["executed"] += execute_approved_theses(db)
                    db.commit()
                except Exception:
                    logger.exception("post-cycle entries failed")
        try:
            from midas.pipeline.events import maybe_event_scan

            summary["event_scan"] = maybe_event_scan(session_factory, router)
        except Exception:
            logger.exception("event scan failed")
        if session_open:
            for position_id, trigger, reason in due:
                if trigger != CheckpointTrigger.invalidation:
                    _review(position_id, trigger, reason)
        try:
            summary["portfolio_review"] = maybe_portfolio_review(session_factory, router)
        except Exception:
            logger.exception("portfolio review failed")

    with session_factory() as db:
        scored = score_closed_positions(db)
        summary["scored"] = len(scored)
        scored_ids = [o.id for o in scored]
        try:
            summary["counterfactuals_updated"] = update_counterfactuals(db)
            from midas.pipeline.events import update_flag_outcomes

            summary["flag_outcomes_updated"] = update_flag_outcomes(db)
        except Exception:
            logger.exception("counterfactual update failed")
        db.commit()
    if scored_ids:
        try:
            summary["lessons"] = run_post_mortems(session_factory, router, scored_ids)
        except Exception:
            logger.exception("post-mortems failed")

    with session_factory() as db:
        from midas.config import settings as _s

        if _s.alpaca_api_key and _s.alpaca_api_secret:
            try:
                summary["reconcile"] = paper_broker.reconcile_mirrored_orders(db)
            except Exception:
                logger.exception("reconciliation failed")
        from midas.models import SystemState

        hb = db.get(SystemState, "monitor_heartbeat")
        value = {
            "at": utcnow().isoformat(),
            "next_pass_at": (
                (utcnow() + timedelta(minutes=interval_minutes)).isoformat()
                if interval_minutes
                else None
            ),
            "summary": {k: v for k, v in summary.items() if k in ("executed", "scored", "halted")},
        }
        if hb is None:
            db.add(SystemState(key="monitor_heartbeat", value=value))
        else:
            hb.value = value
        db.commit()
    return summary
