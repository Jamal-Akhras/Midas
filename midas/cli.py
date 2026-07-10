"""Midas CLI - the operator's console.

  midas init                    create the database
  midas screen                  run the universe screen
  midas run TICKER              run the committee pipeline for one ticker
  midas cycle                   screen, then run the committee on every candidate
  midas digest [--days N]       weekly digest (the L2 review surface)
  midas approvals               list pending approvals
  midas approve ID [--deny]     resolve an approval (approving a thesis executes it)
  midas positions               open positions + account
  midas monitor [--loop]        one monitor pass (or run forever)
  midas stats                   track record vs the L3 bar
  midas halt [--clear]          show or clear the pipeline halt
  midas audit [--limit N]       recent audit trail
  midas ui [--port N]           launch the command center (local web console)
"""
import argparse
import json
import sys
import time

from sqlalchemy import select


def main() -> None:
    parser = argparse.ArgumentParser(prog="midas", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("screen")
    p_run = sub.add_parser("run")
    p_run.add_argument("ticker")
    p_run.add_argument("--resume", default=None, help="run id to resume")
    sub.add_parser("cycle")
    p_digest = sub.add_parser("digest")
    p_digest.add_argument("--days", type=int, default=7)
    sub.add_parser("approvals")
    p_appr = sub.add_parser("approve")
    p_appr.add_argument("approval_id")
    p_appr.add_argument("--deny", action="store_true")
    p_appr.add_argument("--note", default="")
    sub.add_parser("positions")
    p_mon = sub.add_parser("monitor")
    p_mon.add_argument("--loop", action="store_true")
    p_mon.add_argument("--interval-minutes", type=int, default=60)
    sub.add_parser("stats")
    p_halt = sub.add_parser("halt")
    p_halt.add_argument("--clear", action="store_true")
    p_audit = sub.add_parser("audit")
    p_audit.add_argument("--limit", type=int, default=30)
    p_ui = sub.add_parser("ui")
    p_ui.add_argument("--port", type=int, default=8420)
    args = parser.parse_args()

    from midas.config import settings
    from midas.db import init_db, session_factory

    init_db()
    if settings.effective_demo_mode:
        print("[demo mode: no provider keys configured - canned data, zero spend]")

    if args.cmd == "init":
        print("database ready at", settings.database_url)
        return

    from midas.providers.router import get_router

    if args.cmd == "screen":
        from midas.pipeline.screen import run_screen

        content = run_screen(session_factory, get_router())
        print(json.dumps(content, indent=2, default=str))
    elif args.cmd == "run":
        import uuid as _uuid

        from midas.pipeline.committee import run_committee

        result = run_committee(
            session_factory, get_router(), args.ticker,
            resume_run_id=_uuid.UUID(args.resume) if args.resume else None,
        )
        print(f"{result.status.value}: {result.note} (run {result.run_id})")
    elif args.cmd == "cycle":
        from midas.pipeline.committee import run_committee
        from midas.pipeline.screen import run_screen

        content = run_screen(session_factory, get_router())
        tickers = [c["ticker"] for c in content["candidates"]]
        print(f"screen: {len(tickers)} candidate(s): {', '.join(tickers) or '-'}")
        for t in tickers:
            try:
                result = run_committee(session_factory, get_router(), t)
                print(f"  {t}: {result.status.value} - {result.note}")
            except Exception as exc:  # keep cycling; the run recorded its own failure
                print(f"  {t}: failed ({type(exc).__name__})")
    elif args.cmd == "digest":
        from midas.outcomes.digest import build_digest

        with session_factory() as db:
            print(json.dumps(build_digest(db, days=args.days), indent=2, default=str))
    elif args.cmd == "approvals":
        from midas.models import ApprovalRequest, ApprovalStatus

        with session_factory() as db:
            pending = db.scalars(select(ApprovalRequest).where(
                ApprovalRequest.status == ApprovalStatus.pending)).all()
            if not pending:
                print("no pending approvals")
            for a in pending:
                print(f"{a.id}  [{a.subject_type}] {a.title}\n    {a.summary[:160]}")
    elif args.cmd == "approve":
        import uuid as _uuid

        from midas.execution.executor import resolve_approval
        from midas.models import ApprovalRequest

        with session_factory() as db:
            approval = db.get(ApprovalRequest, _uuid.UUID(args.approval_id))
            if approval is None:
                sys.exit("no such approval")
            resolve_approval(db, approval, approve=not args.deny, note=args.note)
            db.commit()
            print(f"{approval.title}: {approval.status.value}")
    elif args.cmd == "positions":
        from midas.execution.paper_broker import get_account, refresh_equity
        from midas.models import Position, PositionStatus

        with session_factory() as db:
            refresh_equity(db)
            db.commit()
            account = get_account(db)
            print(f"paper account: cash ${account['cash']:,.2f}  "
                  f"equity ${account['equity']:,.2f}")
            for p in db.scalars(select(Position).where(
                    Position.status == PositionStatus.open)):
                mark = p.last_mark_price or p.avg_price
                mult = 100 if p.instrument.value == "option" else 1
                upnl = (mark - p.avg_price) * p.qty * mult
                print(f"  {p.symbol}: {p.qty:g} @ {p.avg_price} "
                      f"(mark {mark}, uP&L {upnl:+.2f}, "
                      f"next review {p.next_review_at})")
    elif args.cmd == "monitor":
        from midas.scheduler.monitor import run_monitor_once

        while True:
            summary = run_monitor_once(
                session_factory, get_router(),
                interval_minutes=args.interval_minutes if args.loop else None)
            print(json.dumps(summary, indent=2, default=str))
            if not args.loop:
                break
            time.sleep(args.interval_minutes * 60)
    elif args.cmd == "stats":
        from midas.outcomes.scoring import stats

        with session_factory() as db:
            print(json.dumps(stats(db), indent=2, default=str))
    elif args.cmd == "halt":
        from midas.risk.engine import clear_halt, get_halt

        with session_factory() as db:
            if args.clear:
                clear_halt(db)
                db.commit()
                print("halt cleared")
            else:
                halt = get_halt(db)
                print(json.dumps(halt, indent=2) if halt else "no active halt")
    elif args.cmd == "ui":
        from midas.ui.server import serve

        serve(port=args.port)
    elif args.cmd == "audit":
        from midas.models import AuditEvent

        with session_factory() as db:
            events = db.scalars(select(AuditEvent)
                                .order_by(AuditEvent.created_at.desc())
                                .limit(args.limit)).all()
            for e in reversed(events):
                print(f"{e.created_at:%Y-%m-%d %H:%M} [{e.event_type}] {e.summary}")


if __name__ == "__main__":
    main()
