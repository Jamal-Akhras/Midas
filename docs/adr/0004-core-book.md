# ADR 0004: A rules-based core book beside the swing committee

Date: 2026-11-08 · Status: Accepted

## Context

The swing committee's framing (single-name swings with 5% stops, 21-day
time stops, short-dated options) underperformed cash from 2022 and SPY
over twenty years in every variant tested (docs/results.md). The simplest
rule that beat cash in every window, "SPY above its 200-day average, else
T-bills", had Sharpe 0.59 to 0.65 against SPY's 0.54 with a 21% maximum
drawdown against 55%. A diversified version (five ETFs, each held above
its own trend) had similar risk-adjusted returns and shallower drawdowns.

The committee's machinery (ladder, broker mirroring, audit trail, cost
log) is sound and should not be duplicated.

## Decision

Add `midas.strategy.core`: equal slices of `CORE_ALLOCATION_PCT` of
equity across `CORE_UNIVERSE`, each held only while above its
`CORE_TREND_WINDOW`-day average, rebalanced monthly. The rule is the one
`midas.research.strategies.tactical_sma` backtests.

The core book shares the account and the autonomy ladder with the
committee and nothing else:

- Positions and theses carry `book="core"`. The committee's stops, time
  stops, reviews, risk caps, portfolio review and statistics filter on
  `book="swing"`, so a core holding can never be reviewed by the
  Investor agent, time-stopped or counted as committee evidence.
- A plan is a deterministic artifact. At L0 it becomes a blocking
  approval request; at L1+ it executes on paper through the same
  `paper_broker.submit` path as the committee, so Alpaca mirroring, fill
  provenance and the audit trail are identical.
- A denied or executed plan is the month's answer; the cadence is
  recorded in `system_state`, not inferred from positions.
- A high-severity negative event flag (ADR 0005) zeroes a slice for that
  rebalance.

Off by default (`CORE_ENABLED`). The core settings are part of the
strategy fingerprint, so changing them restarts the L2 evidence clock.

## Consequences

Midas has a return engine with published, reproducible evidence behind
it, and the committee becomes what the evidence says it is good at:
research, event reading and audit. The core book's own track record is
measured by the same `docs/results.md` tables, not by `midas stats`.

## Alternatives considered

- Replace the committee: loses the research and audit machinery that is
  the project's actual strength.
- Make `thesis_id` nullable and bypass the thesis model for core orders:
  cleaner on paper, but it would have touched every query in the code
  base and broken the "every order traces to a thesis" invariant that the
  audit trail relies on. A standing per-ticker core thesis keeps it.
