# Changelog

All notable changes to Midas are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project has
not yet cut a release, so everything sits under Unreleased.

## [Unreleased]

### Added
- `midas.research.text` and `midas research tone-study`: word-list tone for
  earnings calls, a point-in-time rank, and the study that tests it against
  the weeks after with the earnings-day reaction as a control and a shuffled
  placebo. The protocol and pass mark were committed before any result
  (`docs/studies/2026-11-earnings-call-tone.md`). Result: the tone score
  moves with the earnings-day reaction (rank correlation 0.15) and predicts
  nothing from the next session on, so the study fails and no model is
  trained.
- `midas research backtest --years N`: the account as the live settings
  describe it, replayed over the last N years - the core book with its
  T-bill sleeve and the swing rules as code (reclaim, structural stop,
  room, entry on strength, the binary regime block, the trial), with the
  swing trades, the committees a daily cycle would have run and a table
  by year. `midas_clone` takes the newer rules as opt-in keywords; its
  defaults and every figure in `docs/results.md` are unchanged.
- `CORE_CASH_TICKER`: the share of the core allocation that no slice holds
  (off trend, vetoed) waits in a T-bill ETF in the same book, bought when a
  slice leaves its trend and sold first when one returns. The backtest
  always paid the T-bill rate there; the live book paid zero, and 36% of
  the account sat idle for the first fortnight of the November run.
- The swing book as a trial (`SWING_TRIAL_TRADES`, `SWING_TRIAL_START`):
  after that many closed trades an average result at or below zero ends
  new swing entries (a failing `swing_trial` risk check) and the daily
  cycle, with one `swing.trial_ended` event. Read from the first N trades
  only. Off by default.
- `midas.research.events`: event studies of the entry rules (next-open
  entry, costs, Newey-West t on daily means), each beside a baseline or a
  mirror, and four account rows in the catalogue (the core book at 60%,
  85% and 100% of equity, idle cash at zero or in T-bills). Both are in
  the regenerated `docs/results.md`; ADR 0008 records what they decided.
- Entry on strength (`ENTRY_REQUIRE_STRENGTH`, `ENTRY_MAX_INDEX_DROP_PCT`): the
  executor fills a shares entry only inside its zone, at or above the prior
  close, and not while SPY is down more than the limit on the day; otherwise
  the thesis waits. The verdict checks reject a zone that cannot fill that
  way (below the last close, or a single price), and cap a pullback's first
  target at the retest of its 20-day high. SNPS, WFC and INTU (2026-11-13 to
  11-17) were all bought below the prior close on falling days.
- The technical block carries `levels` (structural stop, first target, the
  reward-to-risk between them), the screener's facts line states the room,
  and the setup gate drops a pullback with no room
  (`SETUP_GATE_REQUIRE_ROOM`) and any candidate reporting inside the entry
  window, before a committee is spent on it.
- `TRIM_MIN_REMAINDER_USD`: a trim that would leave less is refused; the
  portfolio manager and the re-evaluator are told `trim_allowed`.
- Post-mortems are handed `entry_facts` and name what their lesson is about;
  a stop lesson reaches later committees only when the stop was inside one
  ATR of the fill, and post-mortems written before this are not fed.
- `python -m midas ...` runs the CLI without the launcher binary pip writes
  into the Scripts folder, and `scripts\check.cmd` takes a `python.exe` path
  as its first argument. On 2026-11-16 Windows Smart App Control refused
  that launcher, which had stopped the monitor loop and the scheduled checks.
- Exact earnings dates from the calendar: a code-only tool
  (`get_earnings_dates`, Finnhub's calendar 200 days back and 120 ahead,
  with yfinance filling the last report date when the free calendar's
  month of history has none) gives the last report date and the next one.
  The technical block carries
  both as days, a gap within three days of a report is named
  post-earnings, the screener's facts line names a report inside 30 days
  either side, and the entry window and the earnings review trigger read
  this code-sourced date before any date the catalyst or fundamentals
  agent wrote. An unknown date is None, never an error.
- The setup label is the code's: the technical block names what a chart
  IS (`pullback-in-uptrend`, `gap-and-hold`, or `none` with the reason)
  from completed bars, and carries the structure a setup is made of - the
  20-day high and the swing low since it, the 14-day ATR, the last volume
  gap and whether its low held, whether the last close reclaimed the
  10-day mean and the prior close, relative strength against SPY. The
  screen computes these facts for every ticker before the screener runs,
  hands them to it as one line per ticker, and overwrites each candidate's
  `setup` with the code's label (the model's own goes to
  `screener_setup`).
- An entry trigger (`SETUP_GATE_REQUIRE_RECLAIM`): a pullback whose last
  completed close is not back above its 10-day mean and the prior close is
  still falling, and the gate drops it until it is. A chart with no
  code-defined setup is dropped with the reason.
- A stop-against-ATR rule (`MIN_STOP_ATR`): a verdict's stop closer than
  that many 14-day ATRs to the entry is rejected by the sanity gate and
  re-prompted once with the swing low to put it under.
- An earnings entry window (`ENTRY_EARNINGS_BLACKOUT_DAYS`): a trade
  verdict with earnings inside the window must declare `earnings_play`
  (the event is the thesis, held through it) or rule no_trade; an
  earnings play is sized at half risk by the risk engine, in the
  committee's provisional check and again at execution, from the run's
  own catalysts/fundamentals date.
- The daily cycle waits `AUTO_CYCLE_AFTER_OPEN_MINUTES` after the open
  before screening (both November entries were made inside the first
  hour, on the day's worst prints).
- An opening guard on stop-breach exits: the monitor's stop layer exits at
  once only through a disaster floor (`STOP_DISASTER_MULTIPLE` original
  risk units below entry), logs rather than acts inside
  `STOP_OPENING_GUARD_MINUTES`, and otherwise needs the breach on
  `STOP_CONFIRMATION_PASSES` consecutive passes. The count survives a
  restart (`stop_breaches` in system state); reclaiming the stop is audited.
- A premarket gap review: a held ticker gapping past
  `PREMARKET_GAP_ALERT_PCT` is reviewed by the reevaluator in that same
  premarket pass, with the news tool and the gap facts. It may hold under
  the guard, exit on the first in-session pass (before entries and the
  stop layer), or raise the stop via `new_stop`; a cut is refused.
- A portfolio-review guard: a position younger than
  `PM_EXIT_MIN_AGE_HOURS` is exempt from PM exits and trims unless a
  research artifact or event flag newer than its entry exists; refused
  actions are audited as `portfolio.action_refused`. The daily review runs
  at/after `PORTFOLIO_REVIEW_AFTER_ET` instead of on whichever pass first
  finds a position.
- A reward-to-risk floor (`MIN_REWARD_TO_RISK`): a shares verdict's first
  target must be at least that many stop distances from the worst
  accepted entry. The sanity gate re-prompts once; the risk engine and the
  executor re-check it.
- An evidence audit: bull and bear write each evidence item as
  `artifact: claim (date)`; code counts the items that name an artifact
  and the investor is told the count per side.
- The screen context carries the ticker's last swing exit inside ten days
  (when, price, outcome, why) as a hard line for a committee re-entering
  a stopped-out name.
- The post-mortem receives the whole record: daily bars from the session
  before entry to the exit, the filled orders, every audit event on the
  position or its thesis, and every review checkpoint.
- Real-time pushes (`TELEGRAM_PUSH_EVENTS`): each monitor pass ends with
  one Telegram message listing its fills, stop exits, gap flags, PM
  actions, approvals and halts. The brief flags a swing position marked
  below its stop.
- A setup gate: after the price/volume floors, each screen candidate's
  setup label is checked in code against the technical block (above the
  50-day average, within `SETUP_GATE_MAX_PULLBACK_PCT` of the 20-day
  high, not down more than `SETUP_GATE_MAX_5D_DROP_PCT` in five sessions,
  and for a pullback a 5d/20d volume ratio at most
  `SETUP_GATE_MAX_VOLUME_RATIO`). Failing charts are dropped with their
  reasons recorded; the screen context carries the same verdict for
  manual runs. The screener's task now lists the deep scan's quant scores
  and the gate's limits.
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
- The event scan reads each item once. News and filings are fetched in code
  first; the analyst is shown only what no earlier scan has shown it, and
  is not called when nothing is new (`flags.skipped`). A flag for an event
  that already has one (same kind, and the same article or dates within
  three days) is not written again (`flags.duplicate`). One Netflix article
  had been flagged on seven consecutive scans, each restarting the 30-day
  veto and counting again in the scored ledger.
- `routing.yaml` takes `task_options`; the scan tasks (`event_scan`,
  `screening`) run at temperature 0, so the same text gets the same reading.
  Sent to OpenAI and Gemini; accepted and not yet sent to Anthropic.
- The daily cycle stands aside while the swing book has no room for the
  smallest position, counting an approved thesis that has not filled as a
  position taken (`risk.engine.swing_room_usd`). In a ten-year replay about
  nine committees in ten had run with no slot free.
- The room rule and the first-target cap apply to a gap-and-hold as well as
  a pullback, wherever a 20-day high is overhead: six of the ten committees
  that ended at the level checks (2026-11-13 to 11-19) were gap-and-holds
  under their high. One at its high has no retest to measure and passes.
- The stop-distance check measures from the lowest fill the entry zone
  allows, not from the zone high (INTU filled a fifth of an ATR from its
  stop). Half size halves the notional cap as well as the risk budget. A
  position whose verdict declared an earnings play is no longer sent to
  review for its own earnings. A trim the floor refused is not logged as a
  portfolio action.
- Prompts: the investor is told entries fill on strength from the last
  close up, to start from the code's levels, and that a price invalidator
  is the stop; the portfolio manager and the re-evaluator are told the stop
  is code's and to check a price invalidator against the mark.
- The catalyst prompt admits only dated scheduled events and disclosed
  corporate actions; analyst notes, reiterations and the price move itself
  are not catalysts. The investor places the stop at the structural level
  (the pullback low or just under the 50-day), not a percentage. The
  portfolio manager is told an invalidator triggers only on a fact dated
  after the entry. The post-mortem must say "cause not in the record"
  rather than name an event the record does not contain.
- The event scan covers every open swing position and approved thesis as
  well as the core universe and the watchlist.
- The risk check reports a bull/bear conviction tie as a tie instead of a
  bull win. The investor, triage, bull and bear prompts state that analyst
  ratings, price targets and headline counts are opinion, not evidence
  about a setup, and that a bear case grounded in the technical block
  beats a bull case grounded in opinion.
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
- `get_news` ordered Finnhub's epoch dates and Alpaca's ISO dates as text,
  so Finnhub's headlines always ranked last and were the ones cut. Both are
  now parsed to one UTC clock before the sort.
- `get_filings` answered every ETF with an error (no company registrant);
  it now returns an empty list. Three of the five core tickers had failed
  the call on every scan.
- The research data loader drops rows that are not sessions. `^VIX` carried a
  value on Memorial Day and Labor Day 2026; those two rows left every 200-day
  average blank for 200 sessions, so no trend rule in `docs/results.md` traded
  between May and November 2026. Regenerated: figures move by 0.2 points at
  most, and ADR 0008 quotes the corrected ones.
- The test baseline pins the core allocation, the cash sleeve, the regime
  throttle and the swing trial: six tests read the operator's `.env` and
  failed once the live account moved off the defaults.
- An ordinary stop could never confirm: the monitor pass counted a breach
  in a session it closed without committing, so every pass counted "one of
  two" and wrote no pending line. SNPS sat under its stop on three sessions
  (2026-11-17 to 11-19); only the disaster floor and the agents could exit.
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
