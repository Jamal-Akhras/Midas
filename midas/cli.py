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
midas research ...            backtests and docs/results.md (see midas research -h)
midas core plan|rebalance     the rules-based core book (plan, or rebalance via the ladder)
midas flags scan|list         event flags from the event analyst
"""

import argparse
import json
import logging
import sys

from sqlalchemy import select


def configure_logging() -> None:
    """Operator-facing INFO log for long-running commands.

    httpx logs every request URL at INFO, and some data vendors (Finnhub,
    FMP) carry their API key in the query string, so its chatter stays at
    WARNING: a key must never reach a terminal, a log file or a pasted
    snippet."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="midas", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
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
    p_brief = sub.add_parser("brief", help="operator brief from the ledger")
    p_brief.add_argument("--send", action="store_true", help="post it to Telegram")
    p_brief.add_argument(
        "--review", action="store_true", help="append a Claude review (needs the claude CLI)"
    )
    p_brief.add_argument(
        "--report", help="a file the review also reads, e.g. logs/checks/latest.txt"
    )
    p_brief.add_argument("--since-hours", type=float, default=None)
    p_brief.add_argument(
        "--compact",
        action="store_true",
        help="the hourly form: a few lines, and a review only when something happened",
    )
    p_notify = sub.add_parser("notify", help="Telegram delivery: setup | test")
    p_notify.add_argument("action", choices=["setup", "test"])
    p_notify.add_argument(
        "--write", action="store_true", help="setup: save the discovered chat id to .env"
    )
    p_ui = sub.add_parser("ui")
    p_ui.add_argument("--port", type=int, default=8420)
    p_ui.add_argument(
        "--host", default="127.0.0.1", help="bind address (loopback only by default; no auth)"
    )
    p_core = sub.add_parser("core", help="rules-based core book")
    p_core.add_argument("action", choices=["plan", "rebalance"])
    p_core.add_argument(
        "--force", action="store_true", help="ignore the monthly cadence and CORE_ENABLED"
    )
    p_flags = sub.add_parser("flags", help="event flags")
    p_flags.add_argument("action", choices=["scan", "list"])
    p_flags.add_argument("tickers", nargs="*")
    p_flags.add_argument("--days", type=int, default=30)
    p_res = sub.add_parser("research", help="backtests; try: midas research -h")
    p_res.add_argument("research_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    if args.cmd == "research":  # no database, no provider keys needed
        from midas.research.cli import main as research_main

        sys.exit(research_main(args.research_args))

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
            session_factory,
            get_router(),
            args.ticker,
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
            pending = db.scalars(
                select(ApprovalRequest).where(ApprovalRequest.status == ApprovalStatus.pending)
            ).all()
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
            print(f"paper account: cash ${account['cash']:,.2f}  equity ${account['equity']:,.2f}")
            for p in db.scalars(select(Position).where(Position.status == PositionStatus.open)):
                mark = p.last_mark_price or p.avg_price
                mult = 100 if p.instrument.value == "option" else 1
                upnl = (mark - p.avg_price) * p.qty * mult
                print(
                    f"  {p.symbol}: {p.qty:g} @ {p.avg_price} "
                    f"(mark {mark}, uP&L {upnl:+.2f}, "
                    f"next review {p.next_review_at})"
                )
    elif args.cmd == "monitor":
        configure_logging()
        from midas.scheduler.monitor import run_monitor_forever, run_monitor_once

        def show(summary: dict) -> None:
            print(json.dumps(summary, indent=2, default=str))

        if args.loop:
            run_monitor_forever(
                session_factory, get_router(), args.interval_minutes, on_summary=show
            )
        else:
            show(run_monitor_once(session_factory, get_router()))
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

        serve(port=args.port, host=args.host)
    elif args.cmd == "core":
        from midas.strategy import core as core_book

        with session_factory() as db:
            if args.action == "plan":
                print(json.dumps(core_book.build_plan(db).to_content(), indent=2, default=str))
            else:
                outcome = core_book.run_rebalance(db, force=args.force)
                db.commit()
                print(json.dumps(outcome, indent=2, default=str))
    elif args.cmd == "flags":
        from midas.pipeline.events import run_event_scan

        if args.action == "scan":
            from midas.strategy.core import core_universe

            tickers = args.tickers or [*core_universe(), *settings.watchlist_tickers]
            print(json.dumps(run_event_scan(session_factory, get_router(), tickers), indent=2))
        else:
            from datetime import timedelta as _td

            from midas.models import EventFlag, aware, utcnow

            with session_factory() as db:
                since = utcnow() - _td(days=args.days)
                rows = db.scalars(select(EventFlag).order_by(EventFlag.created_at.desc())).all()
                for f in rows:
                    if aware(f.created_at) < since:
                        continue
                    fwd = (
                        f"fwd10 {f.fwd_10d_pct:+.1f}%" if f.fwd_10d_pct is not None else "unscored"
                    )
                    print(
                        f"{f.created_at:%Y-%m-%d} {f.ticker:6s} {f.severity:6s} {f.direction:8s} "
                        f"{f.kind:18s} {fwd:14s} {f.summary[:70]}"
                    )
    elif args.cmd == "brief":
        configure_logging()
        from datetime import timedelta
        from pathlib import Path

        from midas.models import utcnow
        from midas.notify.brief import (
            build_brief,
            default_since,
            format_brief,
            mark_brief_sent,
            needs_attention,
            plain_from_html,
            render_telegram,
        )

        reconfigure = getattr(sys.stdout, "reconfigure", None)
        if reconfigure is not None:  # a review may carry characters the console cannot show
            reconfigure(errors="replace")
        if args.send and not (settings.telegram_bot_token and settings.telegram_chat_id):
            print("Telegram is not configured; run `midas notify setup --write` first")
            sys.exit(1)
        with session_factory() as db:
            now = utcnow()
            since = (
                now - timedelta(hours=args.since_hours)
                if args.since_hours
                else default_since(db, now)
            )
            brief = build_brief(db, since=since, now=now)
            text = format_brief(brief)
            review: str | None = None
            if args.review and (not args.compact or needs_attention(brief)):
                from midas.notify.review import claude_review

                report = text
                if args.report:
                    report += "\n\n=== full check report ===\n" + Path(args.report).read_text(
                        encoding="utf-8", errors="replace"
                    )
                review = (
                    claude_review(
                        report,
                        cli=settings.brief_review_cli,
                        model=settings.brief_review_model,
                        timeout_s=settings.brief_review_timeout_s,
                        compact=args.compact,
                    )
                    or "Review unavailable: see the check log on the PC."
                )
                text += "\n\nReview:\n" + review
            print(text)
            if args.send:
                from midas.notify.telegram import send_rich

                html_text = render_telegram(brief, review, compact=args.compact)
                sent = send_rich(
                    html_text,
                    plain_from_html(html_text) if args.compact else text,
                    token=settings.telegram_bot_token,
                    chat_id=settings.telegram_chat_id,
                )
                if not sent:
                    print("\nNOT sent (see the log)")
                    sys.exit(1)
                mark_brief_sent(db, now)
                db.commit()
                print("\nsent to Telegram")
    elif args.cmd == "notify":
        configure_logging()
        from midas.notify.telegram import discover_chat, send_message

        if not settings.telegram_bot_token:
            print(
                "TELEGRAM_BOT_TOKEN is not set. In Telegram, message @BotFather and send /newbot;\n"
                "put the token in .env as TELEGRAM_BOT_TOKEN=...; open your new bot and send it\n"
                "any message; then run `midas notify setup --write`."
            )
            sys.exit(1)
        chat_id = settings.telegram_chat_id
        if args.action == "setup":
            chat = discover_chat(token=settings.telegram_bot_token)
            if chat is None:
                print(
                    "No message found yet. Open your bot in Telegram, send it any message, "
                    "then rerun `midas notify setup --write`."
                )
                sys.exit(1)
            chat_id = chat["chat_id"]
            print(f"chat id {chat_id} ({chat['name'] or 'no name'})")
            if args.write:
                from midas.config import update_setting

                update_setting("TELEGRAM_CHAT_ID", chat_id)
                print("saved TELEGRAM_CHAT_ID to .env")
            elif chat_id != settings.telegram_chat_id:
                print(f"add to .env:  TELEGRAM_CHAT_ID={chat_id}   (or rerun with --write)")
        ok = send_message(
            "Midas: Telegram is connected. Briefs will arrive here.",
            token=settings.telegram_bot_token,
            chat_id=chat_id,
        )
        print("test message sent" if ok else "test message FAILED (see the log)")
        if not ok:
            sys.exit(1)
    elif args.cmd == "audit":
        from midas.models import AuditEvent

        with session_factory() as db:
            events = db.scalars(
                select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(args.limit)
            ).all()
            for e in reversed(events):
                print(f"{e.created_at:%Y-%m-%d %H:%M} [{e.event_type}] {e.summary}")


if __name__ == "__main__":
    main()
