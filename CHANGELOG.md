# Changelog

All notable changes to Midas are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project has
not yet cut a release, so everything sits under Unreleased.

## [Unreleased]

### Added
- A `screen_context` artifact per committee run: the screener's record for
  the ticker plus a code-computed technical block on completed bars, read
  by triage, bull, bear and the investor. Every stage's task text states
  the book and its holding window.
- `COMMITTEE_COOLDOWN_DAYS`: a ticker whose latest verdict was no_trade
  sits out the auto-cycle unless its price enters that verdict's entry
  zone or a new event flag lands, so committee seats go to new names.
- `MIN_SWING_NOTIONAL_USD`: a minimum-notional risk check that applies at
  committee time and at execution.
- Insider activity from Finnhub transaction codes (net shares bought and
  sold, counts by code), falling back to a filing list that states it
  carries no direction.
- `midas/notify`: an operator brief built from the ledger (`midas brief`),
  Telegram delivery (`--send`, `midas notify setup|test`) and an optional
  review by Claude Code's CLI with tools disabled (`--review`). The Telegram
  rendering is HTML (bold headers, bullets, the review first, the operator's
  local time) with a plain-text fallback. The check script sends it after
  each report.
- `scripts/check.cmd`: a read-only operator check (positions, last 30 audit
  events, stats) written to `logs/checks/`, for a twice-daily scheduler.
- Apache-2.0 licence, this changelog, and architecture decision records
  in `docs/adr/`.
- Quality gates: ruff (lint and format), mypy with the pydantic plugin and
  a strict profile for new packages, a coverage floor, pre-commit, CodeQL
  and Dependabot. CI runs lint, typecheck and tests on Python 3.10, 3.12
  and 3.13.
- Alembic migrations (`midas/migrations`). `midas init` stamps fresh
  databases, adopts pre-Alembic ledgers, and upgrades versioned ones. A
  test asserts the migration chain reproduces the models exactly.
- Dockerfile (non-root, sqlite ledger on a mounted volume) and a
  `midas ui --host` flag for container use.
- `midas/research`: the backtesting toolkit (data cache, cost-aware
  simulator, strategies, swing-committee clone, intraday rules,
  walk-forward selection, report builder) and `docs/results.md`, generated
  by `midas research report`.
- The core book (`midas/strategy/core`): a trend-filtered ETF allocation
  in its own book, rebalanced monthly through the autonomy ladder, with
  `midas core plan|rebalance`. Off by default (`CORE_ENABLED`).
- Event flags: an event-analyst agent producing sourced, structured
  `EventFlag` rows that veto core slices and are scored against forward
  returns; `midas flags scan|list`; a flag ledger section in `midas stats`.
- Run-time integrity gates: the committee refuses a run whose bull and
  bear cases came from the same provider (or, with the gate off, records
  the collision and carries on); a deterministic sanity gate
  checks trade verdicts against the live quote and re-prompts once.
- Optional LLM gateway support: `OPENAI_BASE_URL` / `ANTHROPIC_BASE_URL`,
  the gateway's cost header stored beside the router's estimate, a
  `cost.divergence` audit event when they disagree; `docs/tollgate.md`.

### Changed
- `routing.yaml` moves to current Anthropic model IDs (Opus/Sonnet/Haiku
  5.5) with prices checked on 2026-11-08; cheap stages lead with Haiku.
- Dependency ranges carry upper bounds on the next major version.
- The one-off options backfill script moved to `scripts/`.
- The whole codebase is formatted with ruff at line length 100.
- `AgentDef` is generic over its output schema; stage results keep their
  pydantic type. Missing upstream artifacts and rows fail loudly.
- README rewritten around the results: core book first, committee as the
  research and audit layer.

### Fixed
- A thesis the auto-cycle approved waited for the next monitor pass, up
  to an interval later, before its first entry attempt. The pass now runs
  the entry executor again after the cycle, under the same session,
  regime and halt gates, so the first attempt is made on the facts the
  committee judged.
- The executor refused any thesis sized under one share. On a $1,000
  account every candidate sizes below one share, so no swing trade could
  execute; the gate is now zero shares.
- The deep scan asked for the SIP feed, was refused on the free plan, fell
  back to IEX (a few percent of real volume) and applied the dollar-volume
  floor to today's bar minutes after the open; 15 of 6,435 symbols
  survived. It now reads the delayed consolidated feed, floors on the
  previous full session, prices from the latest trade and scores completed
  bars only. `get_quote` asks the delayed consolidated feed when the IEX
  print is older than fifteen minutes.
- The core book read the session in progress as a close for its 200-day
  rule and stamped the month before any order went out. It now signals on
  the prior close, records the signal date, stamps the month after
  execution with per-order failure events and retries, tops up an existing
  position instead of opening a second row, and rejects the thesis of a
  slice whose first buy did not fill.
- A loss halt returned from the monitor pass before the stop layer ran. The
  pass now keeps stop management, reconciliation and the heartbeat running
  while halted and records the halt once; stop-breach exits run before the
  LLM auto-cycle.
- Entries at or through the stop are rejected as invalidated instead of
  bought at market; entries above the zone wait with one audit event rather
  than a rejected order per pass; trims halve fractional positions; fills
  are committed as soon as the broker confirms them; the entry queue
  handles only swing-book theses; the swing budget is capped at what the
  core book leaves.
- Fundamentals labelled trailing P/E as forward and TTM growth as
  projected, the earnings calendar returned the last report as the next,
  and a count of Form 4 filings was read as insider selling. Labels are
  honest, the calendar is queried forward with past dates nulled, and the
  prompts no longer treat filing counts as direction.
- In the long-only book the shares fallback was sized on the bear's side
  whenever the bear case won the conviction comparison, failing the
  stop-below-entry check and forcing no_trade. It is now sized as a long
  with the comparison stated to the investor. A no_trade verdict carrying
  an entry zone is rejected by the schema so the repair prompt converts it.
- `EDITABLE_SETTINGS` nested the core-book settings inside the
  REGIME_THROTTLE entry, hiding them from the Settings tab.
- The `Settings` repr printed every credential field; they are `repr=False`.
- Mirrored fills kept the planned quantity in the ledger while Alpaca
  filled the three-decimal quantity it was sent, so every reconciliation
  flagged the book's own fills. The ledger now records the quantity the
  broker filled; the planned quantity stays in the fill event's payload.
- The monitor's INFO log printed every outbound request URL, which for
  Finnhub and FMP includes the API key in the query string. httpx logging
  now stays at WARNING.
- Alpaca daily bars came back as the OLDEST bars in the requested window
  (`limit=days` from a wide start, pagination ignored), so the trend filter,
  average volume and previous close read a window that ended weeks ago at 60
  days and months ago at 200. The tool now pages to the end, keeps the
  newest bars, and sets `end` 16 minutes back so the free plan's
  consolidated feed is used instead of falling back to IEX.
- `midas monitor --loop` no longer exits when a pass raises outside the
  per-stage guards; the failure is logged and the next pass runs on time.
- `test_news_raises_without_keys` no longer depends on whether the
  operator's `.env` contains Alpaca keys.
- 126 mypy findings, including `Optional` dereferences in the executor,
  monitor and scoring paths that could raise `AttributeError` at run time.

## 2026-07-10 to 2026-07-23 (pre-changelog history)

Six commits covering M0 to M3: fork skeleton, domain models, autonomy
ladder, model router with write-ahead cost logging, tool layer, committee
pipeline, deterministic risk engine, paper execution mirrored to Alpaca,
monitor, outcome scoring, analyst workstation UI, evidence integrity,
options mirroring, counterfactual ledger, post-mortem agent, CI.
