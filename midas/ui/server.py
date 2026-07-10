"""Midas Analyst Workstation: a local, single-user web console.

Stdlib only (no new dependencies), binds 127.0.0.1, serves one HTML page
plus a JSON API. Actions (screen / committee run / monitor pass) execute in
background threads and surface their state through /api/state polling.

This is an operator console over the same audited code paths the CLI uses -
it never bypasses approvals, risk checks, or the autonomy ladder.
"""
import json
import logging
import threading
import time
import uuid
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sqlalchemy import func, select

from midas.audit import record_event
from midas.config import settings
from midas.db import init_db, session_factory
from midas.execution.executor import resolve_approval
from midas.execution.paper_broker import get_account, refresh_equity
from midas.models import (
    ActorType, ApprovalRequest, ApprovalStatus, ArtifactType, AuditEvent, CostEvent,
    DataSnapshot, EquityPoint, Instrument, ModelCall, Order, PipelineRun,
    Position, PositionStatus, ResearchArtifact, ReviewCheckpoint, SystemState,
    TradeOutcome, TradeThesis, aware, utcnow,
)
from midas.models.history import record_equity_point
from midas.outcomes.scoring import stats
from midas.pipeline.runner import latest_artifact_for_ticker
from midas.providers.router import get_router
from midas.models.domain import StandingStop
from midas.risk.engine import clear_halt, get_halt, open_notional_usd, open_premium_usd
from midas.tools import market_data

logger = logging.getLogger(__name__)

INDEX_PATH = Path(__file__).parent / "index.html"

_activity_lock = threading.Lock()
_activity: list[dict] = []


def _push_activity(kind: str, label: str) -> dict:
    entry = {"id": str(uuid.uuid4())[:8], "kind": kind, "label": label,
             "status": "running", "detail": "", "started": utcnow().isoformat()}
    with _activity_lock:
        _activity.insert(0, entry)
        del _activity[12:]
    return entry


def _finish_activity(entry: dict, status: str, detail: str = "") -> None:
    with _activity_lock:
        entry["status"] = status
        entry["detail"] = detail[:300]


def _spawn(kind: str, label: str, fn) -> None:
    entry = _push_activity(kind, label)

    def run():
        try:
            result = fn()
            _finish_activity(entry, "done", str(result)[:300])
        except Exception as exc:
            logger.exception("%s failed", label)
            _finish_activity(entry, "failed", f"{type(exc).__name__}: {exc}")

    threading.Thread(target=run, daemon=True, name=f"midas-{kind}").start()


_tape_lock = threading.Lock()
_tape_cache: dict = {"at": 0.0, "rows": []}
TAPE_TTL_SECONDS = 60


def _tape_rows() -> list[dict]:
    with _tape_lock:
        if time.monotonic() - _tape_cache["at"] < TAPE_TTL_SECONDS:
            return _tape_cache["rows"]
    from midas.pipeline.screen import universe_tickers

    rows = []
    for t in list(dict.fromkeys(universe_tickers()))[:12]:
        q = _cached_quote(t)
        if q:
            prev = q.get("prev_close") or q["price"]
            chg = (q["price"] - prev) / prev * 100 if prev else 0.0
            rows.append({"ticker": t, "price": round(q["price"], 2),
                         "change_pct": round(chg, 2)})
        else:
            rows.append({"ticker": t, "price": None, "change_pct": None})
    with _tape_lock:
        _tape_cache.update(at=time.monotonic(), rows=rows)
    return rows


_quote_cache: dict = {}
_quote_lock = threading.Lock()
QUOTE_TTL_SECONDS = 30


def _cached_quote(ticker: str):
    now = time.monotonic()
    with _quote_lock:
        hit = _quote_cache.get(ticker)
        if hit and now - hit[0] < QUOTE_TTL_SECONDS:
            return hit[1]
    try:
        data = market_data.get_quote(ticker).data
    except Exception:
        data = None
    with _quote_lock:
        _quote_cache[ticker] = (now, data)
    return data


def _agent_performance(db) -> list[dict]:
    perf: dict[str, dict] = {}
    rows = db.execute(select(
        CostEvent.agent_slug,
        func.count(CostEvent.id),
        func.coalesce(func.sum(CostEvent.cost_usd), 0.0),
    ).group_by(CostEvent.agent_slug)).all()
    for slug, calls, cost in rows:
        if not slug:
            continue
        perf[slug] = {"slug": slug, "model_calls": calls,
                      "cost_usd": round(cost, 4), "parse_failures": 0,
                      "artifacts": 0, "verdict_right": None, "extra": ""}
    fails = db.execute(
        select(CostEvent.agent_slug, func.count(ModelCall.id))
        .join(ModelCall, ModelCall.cost_event_id == CostEvent.id)
        .where(ModelCall.parse_ok.is_(False))
        .group_by(CostEvent.agent_slug)
    ).all()
    for slug, n in fails:
        if slug in perf:
            perf[slug]["parse_failures"] = n
    arts = db.execute(
        select(ResearchArtifact.agent_slug, func.count(ResearchArtifact.id))
        .group_by(ResearchArtifact.agent_slug)
    ).all()
    for slug, n in arts:
        perf.setdefault(slug, {"slug": slug, "model_calls": 0, "cost_usd": 0.0,
                               "parse_failures": 0, "artifacts": 0,
                               "verdict_right": None, "extra": ""})
        perf[slug]["artifacts"] = n

    outcomes = db.scalars(select(TradeOutcome)).all()
    bull_scored = [o.bull_case_right for o in outcomes if o.bull_case_right is not None]
    bear_scored = [o.bear_case_right for o in outcomes if o.bear_case_right is not None]
    if "bull" in perf and bull_scored:
        perf["bull"]["verdict_right"] = round(
            sum(bull_scored) / len(bull_scored) * 100, 1)
    if "bear" in perf and bear_scored:
        perf["bear"]["verdict_right"] = round(
            sum(bear_scored) / len(bear_scored) * 100, 1)
    if "investor" in perf and outcomes:
        wins = sum(1 for o in outcomes if o.pnl_usd > 0)
        perf["investor"]["verdict_right"] = round(wins / len(outcomes) * 100, 1)
        perf["investor"]["extra"] = f"{len(outcomes)} closed"
    if "reevaluator" in perf:
        cps = db.scalars(select(ReviewCheckpoint)).all()
        if cps:
            from collections import Counter

            dist = Counter(c.decision for c in cps if c.decision)
            perf["reevaluator"]["extra"] = " ".join(
                f"{k}:{v}" for k, v in dist.most_common())
    order = ["screener", "fundamentals", "deep-research", "catalyst", "triage",
             "bull", "bear", "structurer", "investor", "reevaluator"]
    return sorted(perf.values(),
                  key=lambda p: order.index(p["slug"]) if p["slug"] in order else 99)


def _display_mark(p: Position) -> float:
    """Freshest DISPLAY mark: live cached quote for shares (30s TTL),
    the monitor's last mark for options (chains are too heavy to poll).
    Display only - the ledger's marks are written by the monitor alone."""
    if p.instrument != Instrument.option:
        q = _cached_quote(p.ticker)
        if q and q.get("price"):
            return float(q["price"])
    return p.last_mark_price or p.avg_price


def _position_payload(db, p: Position) -> dict:
    thesis = db.get(TradeThesis, p.thesis_id)
    sstop = db.scalar(select(StandingStop).where(
        StandingStop.position_id == p.id, StandingStop.active))
    holding_days = max((utcnow() - aware(p.opened_at)).days, 0)
    bull = latest_artifact_for_ticker(db, p.ticker, ArtifactType.bull_case)
    bear = latest_artifact_for_ticker(db, p.ticker, ArtifactType.bear_case)
    quote = _cached_quote(p.ticker)
    mult = 100 if p.instrument == Instrument.option else 1
    mark = _display_mark(p)
    return {
        "id": str(p.id), "ticker": p.ticker, "symbol": p.symbol,
        "instrument": p.instrument.value, "qty": p.qty,
        "avg_price": p.avg_price, "mark": mark, "multiplier": mult,
        "unrealized_usd": round((mark - p.avg_price) * p.qty * mult, 2),
        "cost_usd": round(p.avg_price * p.qty * mult, 2),
        "underlying_price": quote["price"] if quote else None,
        "sector": p.sector,
        "stop_price": p.stop_price or (thesis.stop_price if thesis else None),
        "stop_original": thesis.stop_price if thesis else None,
        "stop_ratcheted": bool(p.stop_price and thesis
                               and p.stop_price > thesis.stop_price),
        "standing_stop": ({"price": sstop.stop_price, "qty": sstop.qty}
                          if sstop else None),
        "holding_days": holding_days,
        "targets": thesis.targets if thesis else [],
        "entry_zone": ([thesis.entry_zone_low, thesis.entry_zone_high]
                       if thesis else None),
        "direction": thesis.direction.value if thesis else None,
        "thesis_summary": thesis.thesis_summary if thesis else "",
        "invalidators": thesis.invalidators if thesis else [],
        "thesis_run_id": str(thesis.run_id) if thesis else None,
        "option_detail": p.option_detail,
        "opened_at": str(p.opened_at), "next_review_at": str(p.next_review_at),
        "bull_move_pct": (bull.content.get("expected_move_pct") if bull else None),
        "bear_move_pct": (bear.content.get("expected_move_pct") if bear else None),
        "bull_conviction": (bull.content.get("conviction") if bull else None),
        "bear_conviction": (bear.content.get("conviction") if bear else None),
        "bull_timeframe_days": (bull.content.get("expected_timeframe_days") if bull else None),
        "bear_timeframe_days": (bear.content.get("expected_timeframe_days") if bear else None),
    }


def build_state() -> dict:
    """READ-ONLY snapshot. The UI must never write account state: a
    2.5s poll writing the row the monitor also writes caused a lost-update
    race that silently swallowed sale proceeds (2026-07-13). The monitor is
    the single writer; equity here is computed from the last marks."""
    with session_factory() as db:
        day_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        spend_today = sum(
            e.cost_usd for e in db.scalars(select(CostEvent))
            if aware(e.created_at) >= day_start
        )
        open_pos = db.scalars(select(Position)
                              .where(Position.status == PositionStatus.open)).all()
        positions = [_position_payload(db, p) for p in open_pos]
        approvals = [
            {"id": str(a.id), "subject_type": a.subject_type, "title": a.title,
             "summary": a.summary, "context": a.context, "run_id": str(a.run_id or ""),
             "created_at": str(a.created_at)}
            for a in db.scalars(select(ApprovalRequest)
                                .where(ApprovalRequest.status == ApprovalStatus.pending)
                                .order_by(ApprovalRequest.created_at.desc()))
        ]
        runs = [
            {"id": str(r.id), "kind": r.kind.value, "ticker": r.ticker,
             "status": r.status.value, "stage": r.stage, "error": r.error,
             "created_at": str(r.created_at)}
            for r in db.scalars(select(PipelineRun)
                                .order_by(PipelineRun.created_at.desc()).limit(40))
        ]
        audit = [
            {"at": str(e.created_at)[:19], "type": e.event_type,
             "summary": e.summary, "actor": e.actor_type.value,
             "run_id": str(e.run_id or "")}
            for e in db.scalars(select(AuditEvent)
                                .order_by(AuditEvent.created_at.desc()).limit(120))
        ]
        outcomes = db.scalars(select(TradeOutcome)).all()
        week_pnl = sum(o.pnl_usd for o in outcomes
                       if aware(o.created_at) >= utcnow() - timedelta(days=7))
        day_pnl = sum(o.pnl_usd for o in outcomes
                      if aware(o.created_at) >= day_start)
        account = get_account(db)
        marked_value = sum(
            _display_mark(p) * p.qty
            * (100 if p.instrument == Instrument.option else 1)
            for p in open_pos)
        account["equity"] = round(account["cash"] + marked_value, 2)
        hb_row = db.get(SystemState, "monitor_heartbeat")
        heartbeat = None
        if hb_row and hb_row.value.get("at"):
            from datetime import datetime as _dt

            at = _dt.fromisoformat(hb_row.value["at"])
            heartbeat = {"at": hb_row.value["at"],
                         "next_pass_at": hb_row.value.get("next_pass_at"),
                         "ago_seconds": int((utcnow() - aware(at)).total_seconds())}
        closed = db.scalars(select(TradeOutcome)
                            .order_by(TradeOutcome.created_at.desc())
                            .limit(30)).all()
        history = []
        for o in closed:
            pos = db.get(Position, o.position_id)
            history.append({
                "at": str(o.created_at)[:16], "symbol": pos.symbol if pos else "?",
                "pnl_usd": o.pnl_usd, "pnl_pct": o.pnl_pct,
                "holding_days": o.holding_days, "exit_kind": o.exit_kind,
                "bull_right": o.bull_case_right,
            })
        try:
            from midas.scheduler.monitor import market_session_open
            market_open = market_session_open()
        except Exception:
            market_open = None
        config_panel = {
            "universe": ("demo" if settings.universe == "demo"
                         else f"{len([t for t in settings.universe.split(',') if t.strip()])} tickers"),
            "auto_cycle_weekdays": settings.auto_cycle_weekdays or "manual only",
            "market_hours_only": settings.auto_cycle_market_hours_only,
            "max_committees_per_day": settings.auto_cycle_max_committees,
            "discovery": settings.discovery_enabled,
            "mirror_orders": settings.alpaca_mirror_orders,
            "standing_stops": settings.alpaca_standing_stops,
            "max_holding_days": settings.max_holding_days,
            # presence ONLY - never values
            "keys": {
                "openai": bool(settings.openai_api_key),
                "anthropic": bool(settings.anthropic_api_key),
                "gemini": bool(settings.gemini_api_key),
                "alpaca": bool(settings.alpaca_api_key and settings.alpaca_api_secret),
                "finnhub": bool(settings.finnhub_api_key),
                "fmp": bool(settings.fmp_api_key),
            },
        }
        be_row = db.get(SystemState, "broker_equity")
        broker_equity = be_row.value if be_row else None
        cyc_row = db.get(SystemState, "last_auto_cycle")
        sweep_row = db.get(SystemState, "last_premarket_sweep")
        gate_utc = None
        try:
            from datetime import datetime as _dtm

            from zoneinfo import ZoneInfo

            et = utcnow().astimezone(ZoneInfo("America/New_York"))
            gate = et.replace(hour=9, minute=45, second=0, microsecond=0)
            gate_utc = gate.astimezone(ZoneInfo("UTC")).isoformat()
        except Exception:
            pass
        pipeline_today = {
            "cycle_gate_utc": gate_utc,
            "auto_cycle": (cyc_row.value if cyc_row and
                           str(cyc_row.value.get("at", ""))[:10]
                           == utcnow().date().isoformat() else None),
            "premarket": (sweep_row.value if sweep_row and
                          str(sweep_row.value.get("at", ""))[:10]
                          == utcnow().date().isoformat() else None),
        }

        from midas.config import EDITABLE_SETTINGS

        settings_editable = [
            {"key": k, "value": getattr(settings, m["attr"]),
             "type": m["type"], "group": m["group"],
             "fingerprint": m["fingerprint"]}
            for k, m in EDITABLE_SETTINGS.items()
        ]
        with _activity_lock:
            activity = [dict(a) for a in _activity]
        return {
            "now": utcnow().isoformat(),
            "mode": "DEMO" if settings.effective_demo_mode else "LIVE-PAPER",
            "autonomy_level": settings.autonomy_level,
            "halt": get_halt(db),
            "account": account,
            "spend_today_usd": round(spend_today, 4),
            "spend_limit_usd": settings.daily_spend_limit_usd,
            "week_realized_pnl": round(week_pnl, 2),
            "day_realized_pnl": round(day_pnl, 2),
            "open_premium_usd": round(open_premium_usd(db), 2),
            "open_notional_usd": round(open_notional_usd(db), 2),
            "tape": _tape_rows(),
            "positions": positions,
            "approvals": approvals,
            "runs": runs,
            "audit": audit,
            "activity": activity,
            "heartbeat": heartbeat,
            "market_open": market_open,
            "history": history,
            "config": config_panel,
            "settings_editable": settings_editable,
            "pipeline_today": pipeline_today,
            "broker_equity": broker_equity,
            "agents": _agent_performance(db),
            "stats": stats(db),
            "risk_caps": {
                "per_trade_risk_pct": settings.per_trade_risk_pct,
                "max_premium_pct_per_trade": settings.max_premium_pct_per_trade,
                "max_total_premium_pct": settings.max_total_premium_pct,
                "daily_loss_limit_pct": settings.daily_loss_limit_pct,
                "weekly_loss_limit_pct": settings.weekly_loss_limit_pct,
                "max_open_positions": settings.max_open_positions,
                "max_positions_per_sector": settings.max_positions_per_sector,
                "max_notional_pct_per_trade": settings.max_notional_pct_per_trade,
                "max_total_notional_pct": settings.max_total_notional_pct,
                "max_holding_days": settings.max_holding_days,
                "breakeven_at_r": settings.breakeven_at_r,
                "trail_lookback_days": settings.trail_lookback_days,
            },
        }


def build_bars(ticker: str, days: int = 90) -> dict:
    """Daily bars for the chart, plus the latest thesis overlay if one
    exists. Direct tool call - no snapshot row, so chart polling does not
    pollute the provenance tables."""
    ticker = ticker.upper()
    try:
        result = market_data.get_daily_bars(ticker, days=days)
        bars, source = result.data, result.source
    except Exception as exc:
        return {"ticker": ticker, "error": f"{type(exc).__name__}", "bars": []}
    with session_factory() as db:
        thesis = db.scalar(select(TradeThesis).where(TradeThesis.ticker == ticker)
                           .order_by(TradeThesis.created_at.desc()))
        overlay = None
        if thesis is not None:
            pos = db.scalar(select(Position).where(
                Position.ticker == ticker,
                Position.status == PositionStatus.open)
                .order_by(Position.opened_at.desc()))
            live_stop = (pos.stop_price if pos and pos.stop_price
                         else thesis.stop_price)
            overlay = {"entry": [thesis.entry_zone_low, thesis.entry_zone_high],
                       "stop": live_stop, "targets": thesis.targets,
                       "stop_ratcheted": bool(pos and pos.stop_price
                                              and pos.stop_price > thesis.stop_price),
                       "direction": thesis.direction.value,
                       "status": thesis.status.value}
    return {"ticker": ticker, "source": source, "bars": bars, "overlay": overlay}


def build_equity_history() -> dict:
    with session_factory() as db:
        points = db.scalars(select(EquityPoint)
                            .order_by(EquityPoint.created_at)).all()
        return {"points": [
            {"at": str(p.created_at)[:19], "equity": p.equity, "cash": p.cash}
            for p in points[-500:]
        ]}


TRACE_EVENT_TYPES = {
    "run.created", "stage.completed", "structure.validated",
    "verdict.overridden", "verdict.inconsistent", "thesis.proposed",
    "thesis.approved", "approval.requested", "approval.approved",
    "approval.denied", "order.submitted", "order.filled", "order.rejected",
    "order.refused", "position.reviewed", "position.closed",
    "position.reduced", "outcome.scored", "run.completed", "run.failed",
}


def build_run_detail(run_id: str) -> dict:
    with session_factory() as db:
        run = db.get(PipelineRun, uuid.UUID(run_id))
        if run is None:
            return {"error": "no such run"}
        artifacts = [
            {"id": str(a.id), "type": a.artifact_type.value,
             "agent": a.agent_slug, "content": a.content, "version": a.version,
             "model": a.model_used, "cost_usd": a.cost_usd,
             "created_at": str(a.created_at)[:19]}
            for a in db.scalars(select(ResearchArtifact)
                                .where(ResearchArtifact.run_id == run.id)
                                .order_by(ResearchArtifact.created_at))
        ]
        snapshots = [
            {"tool": s.tool_name, "params": s.params, "source": s.source,
             "retrieved_at": str(s.retrieved_at)[:19], "ok": s.ok, "error": s.error,
             "preview": json.dumps(s.data, default=str)[:400]}
            for s in db.scalars(select(DataSnapshot)
                                .where(DataSnapshot.run_id == run.id)
                                .order_by(DataSnapshot.created_at))
        ]
        cost = db.scalar(
            select(func.coalesce(func.sum(CostEvent.cost_usd), 0.0))
            .where(CostEvent.run_id == run.id)) or 0.0

        trace = [
            {"at": str(e.created_at)[:19], "type": e.event_type,
             "summary": e.summary, "actor": e.actor_type.value}
            for e in db.scalars(select(AuditEvent)
                                .where(AuditEvent.run_id == run.id)
                                .order_by(AuditEvent.created_at))
            if e.event_type in TRACE_EVENT_TYPES
        ]
        thesis = db.scalar(select(TradeThesis).where(TradeThesis.run_id == run.id))
        thesis_payload = None
        if thesis is not None:
            orders = db.scalars(select(Order).where(Order.thesis_id == thesis.id)
                                .order_by(Order.created_at)).all()
            position_ids = [o.position_id for o in orders if o.position_id]
            checkpoints = (db.scalars(
                select(ReviewCheckpoint)
                .where(ReviewCheckpoint.position_id.in_(position_ids))
                .order_by(ReviewCheckpoint.created_at)).all()
                if position_ids else [])
            outcome = (db.scalar(select(TradeOutcome)
                                 .where(TradeOutcome.thesis_id == thesis.id)))
            thesis_payload = {
                "id": str(thesis.id), "status": thesis.status.value,
                "direction": thesis.direction.value,
                "instrument": thesis.instrument.value,
                "entry": [thesis.entry_zone_low, thesis.entry_zone_high],
                "stop": thesis.stop_price, "targets": thesis.targets,
                "summary": thesis.thesis_summary,
                "invalidators": thesis.invalidators,
                "review_schedule": thesis.review_schedule,
                "orders": [
                    {"at": str(o.created_at)[:19], "side": o.side.value,
                     "symbol": o.symbol, "qty": o.qty,
                     "status": o.status.value, "filled_price": o.filled_price,
                     "reason": o.reason.value, "approved_by": o.approved_by,
                     "risk_ok": (o.risk_check or {}).get("ok")}
                    for o in orders
                ],
                "checkpoints": [
                    {"at": str(c.created_at)[:19], "trigger": c.trigger.value,
                     "decision": c.decision, "notes": c.notes,
                     "status": c.status.value}
                    for c in checkpoints
                ],
                "outcome": ({
                    "pnl_usd": outcome.pnl_usd, "pnl_pct": outcome.pnl_pct,
                    "holding_days": outcome.holding_days,
                    "exit_kind": outcome.exit_kind,
                    "bull_case_right": outcome.bull_case_right,
                    "bear_case_right": outcome.bear_case_right,
                } if outcome else None),
            }
        return {
            "id": str(run.id), "kind": run.kind.value, "ticker": run.ticker,
            "status": run.status.value, "stage": run.stage, "error": run.error,
            "created_at": str(run.created_at)[:19], "cost_usd": round(cost, 4),
            "thesis": thesis_payload,
            "artifacts": artifacts, "snapshots": snapshots, "trace": trace,
        }


DISCONNECTS = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except DISCONNECTS:
            pass  # browser closed/refreshed mid-response - routine, not an error

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except DISCONNECTS:
            pass

    def _json(self, payload, code: int = 200) -> None:
        self._send(code, json.dumps(payload, default=str).encode(),
                   "application/json")

    def do_GET(self):
        try:
            path, _, query = self.path.partition("?")
            if path in ("/", "/index.html"):
                self._send(200, INDEX_PATH.read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._json(build_state())
            elif path == "/api/equity":
                self._json(build_equity_history())
            elif path.startswith("/api/bars/"):
                ticker = path.rsplit("/", 1)[1]
                days = 90
                for part in query.split("&"):
                    if part.startswith("days="):
                        days = max(10, min(int(part[5:] or 90), 250))
                self._json(build_bars(ticker, days))
            elif path.startswith("/api/run/"):
                self._json(build_run_detail(path.rsplit("/", 1)[1]))
            else:
                self._json({"error": "not found"}, 404)
        except DISCONNECTS:
            pass
        except Exception as exc:
            logger.exception("GET %s failed", self.path)
            self._json({"error": type(exc).__name__}, 500)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            router = get_router()

            if self.path == "/api/actions/screen":
                from midas.pipeline.screen import run_screen

                _spawn("screen", "universe screen",
                       lambda: run_screen(session_factory, router))
                self._json({"ok": True})
            elif self.path == "/api/actions/run":
                ticker = str(body.get("ticker", "")).strip().upper()
                if not ticker or not ticker.isalpha() or len(ticker) > 6:
                    self._json({"error": "invalid ticker"}, 400)
                    return
                from midas.pipeline.committee import run_committee

                _spawn("committee", f"committee: {ticker}",
                       lambda: run_committee(session_factory, router, ticker))
                self._json({"ok": True})
            elif self.path == "/api/actions/monitor":
                from midas.scheduler.monitor import run_monitor_once

                _spawn("monitor", "monitor pass",
                       lambda: run_monitor_once(session_factory, router))
                self._json({"ok": True})
            elif self.path == "/api/settings":
                from midas.config import reload_settings, update_setting

                try:
                    change = update_setting(str(body.get("key", "")),
                                            str(body.get("value", "")))
                except (ValueError, Exception) as exc:
                    if not isinstance(exc, ValueError):
                        exc = ValueError(str(exc)[:200])
                    self._json({"error": str(exc)}, code=400)
                    return
                reload_settings()  # re-read the whole file into this process
                with session_factory() as db:
                    record_event(
                        db, event_type="config.changed",
                        summary=(f"{change['key']}: {change['old']} -> "
                                 f"{change['new']}"
                                 + (" [L2 evidence clock will restart]"
                                    if change["fingerprint"] else "")),
                        actor_type=ActorType.operator,
                    )
                    db.commit()
                self._json({"ok": True, **{k: str(v) for k, v in change.items()}})
            elif self.path == "/api/actions/clear-halt":
                with session_factory() as db:
                    clear_halt(db)
                    record_event(db, event_type="halt.cleared",
                                 summary="Halt cleared by operator via workstation")
                    db.commit()
                self._json({"ok": True})
            elif self.path.startswith("/api/approvals/"):
                approval_id = self.path.rsplit("/", 1)[1]
                approve = bool(body.get("approve"))
                with session_factory() as db:
                    approval = db.get(ApprovalRequest, uuid.UUID(approval_id))
                    if approval is None or approval.status != ApprovalStatus.pending:
                        self._json({"error": "approval not pending"}, 400)
                        return
                    resolve_approval(db, approval, approve=approve,
                                     note=str(body.get("note", "")))
                    db.commit()
                self._json({"ok": True, "status": approval.status.value})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:
            logger.exception("POST %s failed", self.path)
            self._json({"error": type(exc).__name__}, 500)


def serve(port: int = 8420) -> None:
    init_db()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    mode = "DEMO" if settings.effective_demo_mode else "LIVE-PAPER"
    print(f"Midas Analyst Workstation: http://127.0.0.1:{port}  [{mode}]")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nworkstation stopped")

# sentinel: server-v2-end
