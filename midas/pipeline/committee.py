"""The per-ticker committee pipeline:

fundamentals + catalysts (cheap) -> passable gate (deterministic filter +
cheap-model triage) -> deep research (expensive, gate-passers only) ->
bull & bear (different models) -> options
structurer (code checks + LLM assist + code re-validation) -> risk engine
(code) -> Investor Agent verdict -> thesis + approval gate.

Commit-per-stage: a crash loses at most the stage in flight; re-running the
same run resumes past completed artifacts.
"""
import json
import logging
import uuid
from datetime import date
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from midas.agents.defs import AGENT_DEFS
from midas.audit import record_event
from midas.config import settings
from midas.models import (
    ActorType, ApprovalRequest, ArtifactType, Direction, Instrument,
    OptionStrategy, OptionStructure, OptionType, PipelineRun, ResearchArtifact,
    RunKind, RunStatus, ThesisStatus, TradeThesis,
)
from midas.pipeline.runner import (
    check_daily_spend, existing_artifact, render_content, run_agent_stage,
    store_code_artifact,
)
from midas.providers.router import ModelRouter
from midas.risk import engine as risk_engine
from midas.risk import structurer_checks
from midas.tools.base import ToolContext
from midas.tools.registry import TOOLS

logger = logging.getLogger(__name__)


class CommitteeResult:
    def __init__(self, run_id: uuid.UUID, status: RunStatus, note: str = "",
                 thesis_id: uuid.UUID | None = None):
        self.run_id = run_id
        self.status = status
        self.note = note
        self.thesis_id = thesis_id

    def __repr__(self) -> str:
        return f"CommitteeResult({self.status.value}: {self.note})"


def run_committee(
    session_factory: Callable[[], Session], router: ModelRouter, ticker: str,
    *, resume_run_id: uuid.UUID | None = None,
) -> CommitteeResult:
    ticker = ticker.upper()

    with session_factory() as db:
        halt = risk_engine.get_halt(db)
        if halt:
            raise RuntimeError(f"pipeline halted: {halt['reason']}")
        check_daily_spend(db)
        if resume_run_id is not None:
            run = db.get(PipelineRun, resume_run_id)
            if run is None or run.ticker != ticker:
                raise ValueError("resume_run_id does not match a run for this ticker")
            run.status = RunStatus.running
            run.error = ""
        else:
            run = PipelineRun(kind=RunKind.committee, ticker=ticker)
            db.add(run)
            db.flush()
            record_event(db, event_type="run.created",
                         summary=f"Committee run created for {ticker}",
                         actor_type=ActorType.operator, run_id=run.id)
        db.commit()
        run_id = run.id

    try:
        return _run_stages(session_factory, router, ticker, run_id)
    except Exception as exc:
        logger.exception("committee run %s failed", run_id)
        with session_factory() as db:
            run = db.get(PipelineRun, run_id)
            if run is not None and run.status == RunStatus.running:
                # Client-visible error carries the exception class only;
                # provider/tool exception text can carry keys and internals.
                run.status = RunStatus.failed
                run.error = (f"stage '{run.stage}' failed "
                             f"({type(exc).__name__}): {str(exc)[:800]}")
                record_event(db, event_type="run.failed", summary=run.error,
                             run_id=run_id)
                db.commit()
        raise


def _done(db: Session, run: PipelineRun, note: str,
          thesis_id: uuid.UUID | None = None,
          status: RunStatus = RunStatus.done) -> CommitteeResult:
    run.status = status
    record_event(db, event_type="run.completed",
                 summary=f"{run.ticker}: {note}", run_id=run.id)
    db.commit()
    return CommitteeResult(run.id, status, note, thesis_id)


def _run_stages(session_factory, router, ticker: str, run_id) -> CommitteeResult:
    # ---- deterministic pre-gate: quote floors ----
    with session_factory() as db:
        run = db.get(PipelineRun, run_id)
        ctx = ToolContext(db, run_id=run_id, ticker=ticker)
        execute = ctx.executor(TOOLS)
        quote = execute("get_quote", {"ticker": ticker})["data"]
        sector = quote.get("sector", "")
        spot = float(quote["price"])
        floors_ok = (spot >= settings.min_price_usd
                     and float(quote.get("avg_volume") or 0) >= settings.min_avg_daily_volume)
        if not floors_ok:
            art = store_code_artifact(
                db, run, agent_slug="gate", artifact_type=ArtifactType.triage,
                ticker=ticker,
                content={"ticker": ticker, "passable": False,
                         "reasons": [f"price ${spot} or volume below floors"],
                         "data_quality_flags": [], "deterministic": True},
            )
            ctx.stamp_artifact(art.id)
            return _done(db, run, "not passable (deterministic floors)")
        db.commit()

    # ---- cheap research stages (the passable gate reads these) ----
    for slug in ("fundamentals", "catalyst"):
        with session_factory() as db:
            run = db.get(PipelineRun, run_id)
            run_agent_stage(
                db, router, run, AGENT_DEFS[slug],
                task_text="Research this candidate for a swing/position trade decision.",
                ticker=ticker,
            )
            db.commit()

    # ---- passable gate: cheap-model triage ----
    with session_factory() as db:
        run = db.get(PipelineRun, run_id)
        _, triage = run_agent_stage(
            db, router, run, AGENT_DEFS["triage"],
            task_text="Is this ticker passable for full committee treatment?",
            ticker=ticker,
        )
        if not triage.passable:
            return _done(db, run, f"not passable: {'; '.join(triage.reasons)}")
        db.commit()

    # ---- deep research: the expensive stage runs ONLY on gate-passers ----
    with session_factory() as db:
        run = db.get(PipelineRun, run_id)
        run_agent_stage(
            db, router, run, AGENT_DEFS["deep-research"],
            task_text="Research this candidate for a swing/position trade decision.",
            ticker=ticker,
        )
        db.commit()

    # ---- bull and bear, independently, different models ----
    for slug in ("bull", "bear"):
        with session_factory() as db:
            run = db.get(PipelineRun, run_id)
            run_agent_stage(
                db, router, run, AGENT_DEFS[slug],
                task_text="Build your strongest honest case from the committee documents.",
                ticker=ticker,
            )
            db.commit()

    # ---- options structurer: code candidates -> LLM assist -> code re-validation ----
    with session_factory() as db:
        run = db.get(PipelineRun, run_id)
        bull = existing_artifact(db, run_id, ArtifactType.bull_case).content
        bear = existing_artifact(db, run_id, ArtifactType.bear_case).content
        direction = ("bullish" if float(bull["conviction"]) >= float(bear["conviction"])
                     else "bearish")
        expected_move = float(bull["expected_move_pct"] if direction == "bullish"
                              else bear["expected_move_pct"])
        equity = _account_equity(db)

        if existing_artifact(db, run_id, ArtifactType.structure) is None:
            ctx = ToolContext(db, run_id=run_id, ticker=ticker)
            execute = ctx.executor(TOOLS)
            quote = execute("get_quote", {"ticker": ticker})["data"]
            spot = float(quote["price"])
            # A missing chain source must not kill the run: the risk stage
            # falls back to a shares thesis when no structure is available.
            chain_reason = ("no chain contracts passed the hard checks "
                            "(DTE/liquidity/spread/premium caps)")
            diagnosis = {}
            try:
                chain = execute("get_options_chain", {"ticker": ticker})["data"]
                candidates = structurer_checks.candidate_structures(
                    chain, direction=direction, spot=spot,
                    expected_move_pct=expected_move, account_equity=equity,
                )
                if not candidates:
                    diagnosis = structurer_checks.failure_histogram(chain)
            except Exception as exc:
                logger.warning("options chain unavailable for %s: %s", ticker, exc)
                candidates = []
                chain_reason = (f"options chain unavailable "
                                f"({type(exc).__name__}) - shares fallback used")
            if not candidates:
                art = store_code_artifact(
                    db, run, agent_slug="structurer",
                    artifact_type=ArtifactType.structure, ticker=ticker,
                    content={"available": False, "direction": direction,
                             "reason": chain_reason, "diagnosis": diagnosis},
                )
                ctx.stamp_artifact(art.id)
                db.commit()
            else:
                db.commit()  # snapshots first; the LLM stage commits itself
                with session_factory() as db2:
                    run2 = db2.get(PipelineRun, run_id)
                    _, assist = run_agent_stage(
                        db2, router, run2, AGENT_DEFS["structurer"],
                        task_text=(
                            "Pick the best structure for the directional case. "
                            "Choose ONLY from these code-checked candidates:\n"
                            + json.dumps(candidates, default=str)
                        ),
                        ticker=ticker,
                        extra_context=f"Direction: {direction}; expected move "
                                      f"{expected_move}% ; spot {spot}",
                    )
                    validated = structurer_checks.validate_pick(
                        assist.picked.model_dump(), candidates)
                    if validated is None:
                        validated = candidates[0]
                        note = "LLM pick was off-list; code fell back to the first candidate"
                    else:
                        note = "LLM pick validated against code candidates"
                    art = existing_artifact(db2, run_id, ArtifactType.structure)
                    art.content = {**art.content, "available": True,
                                   "validated_pick": validated, "direction": direction,
                                   "spot": spot, "validation_note": note}
                    record_event(db2, event_type="structure.validated", summary=note,
                                 run_id=run_id)
                    db2.commit()
        else:
            db.commit()

    # ---- risk engine (deterministic) ----
    with session_factory() as db:
        run = db.get(PipelineRun, run_id)
        structure = existing_artifact(db, run_id, ArtifactType.structure).content
        equity = _account_equity(db)
        if structure.get("available"):
            pick = structure["validated_pick"]
            rc = risk_engine.check_option_trade(
                db, account_equity=equity,
                premium_per_contract=float(pick["est_premium_per_contract"]),
                dte=int(pick["dte"]), sector=sector,
            )
        else:
            # No valid option structure: risk-check a shares fallback with a
            # provisional stop half the expected move away.
            stop = _provisional_stop(structure.get("direction", "bullish"),
                                     spot, expected_move)
            rc = risk_engine.check_shares_trade(
                db, account_equity=equity, entry_price=spot, stop_price=stop,
                sector=sector,
            )
        store_code_artifact(
            db, run, agent_slug="risk-engine",
            artifact_type=ArtifactType.risk_check, ticker=ticker,
            content={**rc.to_content(), "account_equity": equity,
                     "structure_available": bool(structure.get("available")),
                     "note": "binding for the Investor Agent; re-checked at execution"},
        )
        db.commit()

    # ---- investor verdict ----
    with session_factory() as db:
        run = db.get(PipelineRun, run_id)
        prior_thesis = db.scalar(
            select(TradeThesis).where(TradeThesis.run_id == run_id))
        if prior_thesis is not None:  # resume path: never duplicate a thesis
            status = (RunStatus.awaiting_approval
                      if prior_thesis.status == ThesisStatus.proposed
                      else RunStatus.done)
            return _done(db, run, "thesis already exists for this run",
                         prior_thesis.id, status=status)
        _positions = risk_engine.open_positions(db)
        _eq = _account_equity(db)
        _book = "; ".join(
            f"{p.ticker} {p.instrument.value} "
            f"${p.qty * p.avg_price * (100 if p.instrument == Instrument.option else 1):,.0f}"
            f" ({p.sector or 'UNKNOWN'})" for p in _positions) or "empty"
        _deployed = risk_engine.open_notional_usd(db)
        _budget = _eq * settings.max_total_notional_pct / 100
        from midas.scheduler.monitor import recent_lessons

        _lessons = recent_lessons(db)
        _lessons_block = ("\nLESSONS from recently closed trades (earned "
                          "with real outcomes; weigh them): " +
                          " | ".join(_lessons)) if _lessons else ""
        verdict_art, verdict = run_agent_stage(
            db, router, run, AGENT_DEFS["investor"],
            task_text=("Issue your structured verdict for this candidate. "
                       "The risk_check document is binding."),
            ticker=ticker,
            extra_context=(
                "CURRENT PORTFOLIO (weigh concentration and correlation - a "
                "candidate that duplicates existing exposure adds less than "
                f"its standalone case suggests): {_book}. Deployed "
                f"${_deployed:,.0f} of ${_budget:,.0f} budget; "
                f"{len(_positions)}/{settings.max_open_positions} position "
                "slots used." + _lessons_block),
        )
        risk_content = existing_artifact(db, run_id, ArtifactType.risk_check).content
        structure = existing_artifact(db, run_id, ArtifactType.structure).content

        if verdict.verdict != "trade":
            return _done(db, run, f"no_trade: {verdict.no_trade_reason}")
        if not risk_content["ok"]:
            record_event(
                db, event_type="verdict.overridden",
                summary=f"{ticker}: Investor said trade but the risk check failed - "
                        "code wins; no thesis created",
                run_id=run_id,
                payload={"risk_checks": risk_content["checks"]},
            )
            return _done(db, run, "trade verdict overridden by failed risk check")
        if verdict.instrument == "option" and not structure.get("available"):
            record_event(
                db, event_type="verdict.inconsistent",
                summary=f"{ticker}: option verdict but no structure passed the "
                        "hard checks - no thesis created",
                run_id=run_id,
            )
            return _done(db, run, "option verdict without a valid structure")

        thesis = TradeThesis(
            run_id=run_id, verdict_artifact_id=verdict_art.id, ticker=ticker,
            direction=Direction(verdict.direction),
            instrument=Instrument(verdict.instrument),
            entry_zone_low=verdict.entry_zone_low,
            entry_zone_high=verdict.entry_zone_high,
            stop_price=verdict.stop_price, targets=verdict.targets,
            thesis_summary=verdict.thesis_summary,
            invalidators=verdict.what_invalidates_this,
            review_schedule=verdict.review_schedule.model_dump(),
        )
        db.add(thesis)
        db.flush()
        if verdict.instrument == "option":
            pick = structure["validated_pick"]
            strategy = OptionStrategy(pick["strategy"])
            db.add(OptionStructure(
                thesis_id=thesis.id, underlying=ticker, strategy=strategy,
                option_type=OptionType.call if "call" in strategy.value else OptionType.put,
                expiry=date.fromisoformat(pick["expiry"]), dte_at_entry=int(pick["dte"]),
                long_strike=float(pick["long_strike"]),
                short_strike=(float(pick["short_strike"])
                              if pick.get("short_strike") is not None else None),
                est_premium_per_contract=float(pick["est_premium_per_contract"]),
                contracts=int(risk_content["max_qty"]),
                liquidity=pick.get("liquidity", {}), checks=pick.get("checks", {}),
            ))

        record_event(
            db, event_type="thesis.proposed",
            summary=f"{ticker}: {verdict.direction} {verdict.instrument} thesis proposed",
            actor_type=ActorType.agent, run_id=run_id,
            subject_type="thesis", subject_id=thesis.id,
        )

        if settings.autonomy_level >= 1:
            thesis.status = ThesisStatus.approved
            record_event(
                db, event_type="thesis.approved",
                summary=f"{ticker}: thesis auto-approved at autonomy L"
                        f"{settings.autonomy_level} (paper)",
                actor_type=ActorType.agent, run_id=run_id,
                subject_type="thesis", subject_id=thesis.id,
            )
            return _done(db, run, "thesis auto-approved (paper)", thesis.id)

        db.add(ApprovalRequest(
            run_id=run_id, subject_type="thesis", subject_id=thesis.id,
            title=f"Approve {ticker} {verdict.direction} {verdict.instrument} thesis",
            summary=verdict.thesis_summary,
            context={"entry": [verdict.entry_zone_low, verdict.entry_zone_high],
                     "stop": verdict.stop_price, "targets": verdict.targets,
                     "invalidates": verdict.what_invalidates_this},
            blocking=True,
        ))
        record_event(
            db, event_type="approval.requested",
            summary=f"{ticker}: thesis awaits operator approval (L0)",
            run_id=run_id, subject_type="thesis", subject_id=thesis.id,
        )
        return _done(db, run, "thesis proposed; awaiting approval",
                     thesis.id, status=RunStatus.awaiting_approval)


def _account_equity(db: Session) -> float:
    from midas.execution.paper_broker import get_account
    return get_account(db)["equity"]


def _provisional_stop(direction: str, spot: float, expected_move_pct: float) -> float:
    half_move = abs(expected_move_pct) / 2 / 100
    return spot * (1 - half_move) if direction == "bullish" else spot * (1 + half_move)
