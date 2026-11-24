# ADR 0008: The core book runs most of the account; the swing book is a trial

Date: 2026-11-23 · Status: Accepted

## Context

Two weeks into the November 2026 paper run the account stood at -3.05%.
The swing book had made five trades and lost on all five (-2.1% of the
account); the core book, holding two of its five slices, had lost 0.9%
in a market that fell 9% peak to trough. Three facts came out of the
review of every decision in that record.

**The account was built to earn the savings rate.** The core book held
60% of equity in five trend-filtered slices and nothing in the slices
that were off trend, and the swing budget sat in cash between trades. On
average 30% of the account was invested. The backtest behind the core
book pays the T-bill rate on every off-trend slice; the live book paid
zero. Measured on the same data (docs/results.md, from 2005):

| Account | Return a year | Since 2022 | Worst drawdown |
|---|---|---|---|
| Core 60%, idle cash at zero (live) | 4.6% | 4.5% | -6.7% |
| Core 60%, the rest in T-bills | 5.6% | 6.7% | -6.7% |
| Core 85%, the rest in T-bills | 7.1% | 7.7% | -9.4% |
| Core 100% | 8.1% | 8.4% | -11.0% |
| T-bills | 1.7% | 3.9% | 0% |

**No entry rule the record suggested has an edge.** Nine sessions of
live decisions hinted at four changes: drop the reclaim gate (the names
it dropped did better), stop entries while the index is under its
10-day mean, buy the dip instead of the reclaim, and trade the
gap-and-hold. Each was put to every signal day in the 22-year set
(`midas.research.events`). The reclaim gate is neutral to slightly
positive. The index condition adds nothing in raw return beyond the
200-day rule already in force. Buying the dip paid until about 2012 and
has paid nothing since 2020. The gap-and-hold drifts +0.78% over SPY in
20 sessions, and so do a failed up gap (+1.02%) and a down gap (+0.78%):
the drift is survivorship in a list of today's index members, not a
setup. The pullback setup itself returns 0.48% per 10 sessions after
costs against 0.33% for any stock in an uptrend.

**The committee's spend went on trades that could not pass.** Of $3.36
spent on models in eleven sessions, 86% was committees and 44% was ten
committees whose trade verdict then failed the code's level checks; six
of those ten were gap-and-holds under their 20-day high, which no gate
checked for room.

## Decision

1. **The core book runs 85% of the account** (`CORE_ALLOCATION_PCT=85`
   in the live configuration; the default stays 60), and the share of
   its allocation that no slice holds waits in a T-bill ETF
   (`CORE_CASH_TICKER`, live value SGOV). The sleeve is a target in the
   same book: bought when a slice leaves its trend, sold first when one
   returns. The swing budget is what the core allocation leaves, 15%:
   one position.
2. **The swing book is a trial with its stop rule written first**
   (`SWING_TRIAL_TRADES=20`, `SWING_TRIAL_START=2026-11-20`): after 20
   closed trades opened under the rules of 2026-11-20, an average
   result at or below zero ends new swing entries and the research
   spent on them. The verdict is read from the first 20 trades only.
   Open positions keep their stops and reviews.
3. **No swing entries while SPY is below its 200-day average**
   (`REGIME_THROTTLE=0`, the binary mode that already existed). Over 22
   years the reclaimed setup returned 0.31% per 10 sessions below the
   200-day (t-statistic of the excess -0.3) against 0.52% above it (3.9).
4. **The room rule covers both code setups** wherever a 20-day high is
   overhead, so a gap-and-hold that cannot pay 1.5 to 1 to the retest
   is dropped before a committee is spent on it.
5. `MIN_REWARD_TO_RISK` stays at 1.5. With no edge in the setup, fewer
   trades cost nothing.

All of these settings are in the strategy fingerprint, so the L2
evidence clock restarts with them.

## Consequences

The expected return of the account moves from about the savings rate to
about twice it, with a worst historical drawdown near 9%, and none of
that depends on a model. The swing book costs at most one 13% position
and stops itself if it keeps losing; the trial's result will be a fact
in the audit trail (`swing.trial_ended`) rather than a judgement made
after the trades are known.

The 15% the core book does not run is modelled as T-bills in the table
above and is plain cash in the live account, so the live figure sits a
little under the 7.1% row.

The allocation takes effect at the next monthly rebalance; the core book
is not rebalanced mid-month to adopt it, because the rule it trades reads
the month-end close.

## Alternatives considered

- **Core at 100%, no swing book.** The best expected return in the
  table, and it ends the one part of the system that produces a live
  record of model judgement. Kept as the outcome the trial leads to if
  it fails.
- **A T-bill sweep for the swing budget too.** It would need a sale
  ahead of every swing entry for about 0.5% a year on 15% of the
  account.
- **A momentum tilt in the screen.** The top momentum quintile looks
  better in the constituent backtest, but the real fund (MTUM) has
  matched SPY since 2013 (14.0% against 14.2% a year).
- **Removing the reclaim gate** on the live evidence. The 22-year test
  says the live result was noise.
