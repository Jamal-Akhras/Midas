# Study: does the tone of an earnings call rank the weeks after it?

Status: **protocol fixed 2026-11-27, before any return was computed.** The
results section at the end is filled in afterwards and this part is not
edited again; the commit history is the evidence of that order.

## Why

Three reviews in November 2026 found no edge in any price-only entry rule,
in a walk-forward LSTM or in RL allocation (docs/results.md, ADR 0008). The
published evidence that text predicts returns is strong for next-day,
long-short, small-cap strategies and thin for what Midas can do: buy a
large cap, a session late, and hold it for weeks. The one formulation with
support at that pace is slow text around earnings. This study asks the
cheapest version of that question first. Nothing is trained: if a word
count cannot find the effect, a transformer is not the next step.

## Data

- **Calls**: the public `kurry/sp500_earnings_transcripts` set (33,362
  calls, 685 symbols, 2005 to 2025; call start time in New York time). Its
  provenance is not documented, so two checks were made before fixing this
  protocol: 25,678 calls belong to symbols in the price data (22,819 since
  2010), and for six companies the call starts a median 0.5 to 2.2 hours
  after the SEC accepted the matching 8-K Item 2.02 earnings release.
- **Prices**: `data/daily.pkl`, daily bars 2004-01-02 to 2026-11-06, the
  S&P 500 list of today. Survivorship lifts every row equally, which is
  why the benchmark below is the same universe on the same day.
- **Words**: the Loughran-McDonald master dictionary (2012 edition), its
  354 positive and 2,355 negative words. Free for research use; not
  redistributed here, like the transcripts.

## Events

- A call is an event if its symbol has prices and its transcript has at
  least 5,000 characters. One event per symbol and signal day.
- **Signal day**: the session on which the call started before 16:00 New
  York. A call at or after 16:00, on a day with no session, or stamped
  exactly 00:00 (time unknown) belongs to the next session.
- **Entry**: the open of the session after the signal day. So every trade
  waits at least one full session after the call began; a transcript is
  published hours after the call, and Midas acts on completed bars.
- **Exit**: the close 20 sessions after the entry (the swing book's time
  stop is 21 sessions).
- The event's stock must pass the liquidity floor of
  `midas.research.events.liquid` on the signal day.

## Score

- Tokens are runs of letters, upper-cased. `P` and `N` count tokens in the
  positive and negative lists over the whole transcript (prepared remarks
  and questions together). At least 50 such words are required.
- `tone = (P - N) / (P + N)`.
- **Abnormal tone** (the primary score) is the tone minus the mean tone of
  the same company's previous four calls; at least two are needed. Managers
  differ in how they talk, so the level says less than the change.
- **Rank**: the event's percentile among all events whose signal day falls
  in the 365 days before its own (at least 200 needed). Top quintile is a
  percentile of 0.8 or more, bottom quintile 0.2 or less. No future call
  enters any rank.

## Outcome

The 20-session return from the entry open, minus the equal-weight mean of
the same return over every liquid name in the universe on that day. Each
signal day's events are averaged first; the statistic is the mean of those
daily means with a Newey-West t over 20 lags (the convention of
`midas.research.events`). Costs of 10 bp a side are reported beside it.
Period: signal days from 2010-01-01 to the end of the data.

## Pass mark

All four, or the project stops here:

1. **The long leg** (Midas can only buy): top-quintile events earn at
   least +0.40% over the universe in 20 sessions, with t of 3 or more.
2. **Recent**: the same leg has the same sign with t of 2 or more on
   signal days since 2020-01-01.
3. **Spread**: top quintile minus bottom quintile is positive with t of 2
   or more (daily differences, on days that have both).
4. **Beyond the price reaction**: split events into three groups by the
   signal-day return over the universe (the market's own verdict on the
   report). The top-minus-bottom tone spread is positive in each group,
   and the average of the three is at least half the unconditional spread.

**Placebo**: abnormal tone shuffled among the events of the same calendar
quarter (seed 0), ranked the same way. Its long leg must have |t| under 2.
If the shuffled score clears the pass mark, the result is void, not good.

## Reported but not judged

Horizons of 5 and 60 sessions; the raw tone level instead of the change;
SPY as the benchmark; the share of negative words alone; after-close calls
entered at the next open instead of a session later; and the plain
price-reaction quintiles with no text (the classic post-earnings drift).
A secondary row that looks good while the primary fails is a hypothesis
for a new protocol, not a pass.

## What each outcome leads to

- **Pass**: stage 3, the same protocol with a text score from a language
  model whose training cutoff is known, tested only after that cutoff.
- **Fail**: stop. No model is trained on the strength of a secondary row.

## Known limits

- Today's index list: companies that left the index are missing.
- The transcript set's source is undocumented; the timestamp check above
  covers six companies, not all.
- The dictionary's 2012 edition postdates the first two test years.
- About 4,500 events per quintile and a per-event spread near 8% put the
  smallest effect this test can see at roughly 0.5% per 20 sessions.

## Results

Run 2026-11-27 on commit 800d369 (protocol fixed in 60e963c), with
`midas research tone-study`. **The study fails its pass mark on all four
criteria; the placebo is clean. Per the protocol the project stops here:
no model is trained.**

Primary: abnormal tone, 20 sessions from the open after the signal day, excess over the same-day universe. 21,991 ranked events, 2010-01-07 to 2025-05-16.

| group | events | days | net return % | excess % | t | since 2020 excess % | t |
|---|---|---|---|---|---|---|---|
| top quintile | 4,391 | 1533 | +0.95 | -0.04 | -0.4 | +0.12 | 0.6 |
| middle three | 12,958 | 2391 | +0.97 | -0.09 | -0.8 | +0.21 | 1.0 |
| bottom quintile | 4,642 | 1538 | +1.10 | -0.04 | -0.2 | -0.01 | -0.0 |
| all calls | 21,991 | 2734 | +1.14 | -0.01 | -0.1 | +0.33 | 2.1 |

Top minus bottom quintile: +0.09% (t 0.4, 961 days with both legs).

Spread inside each third of the earnings-day price reaction:

| reaction group | spread % | t | days |
|---|---|---|---|
| weak reaction | -0.15 | -0.4 | 370 |
| middle reaction | -0.31 | -1.3 | 400 |
| strong reaction | -0.12 | -0.2 | 354 |

Against the pass mark:

| criterion | observed | result |
|---|---|---|
| long leg: at least +0.40% over the universe, t of 3 or more | -0.04%, t -0.4 | FAIL |
| recent: the same sign since 2020 with t of 2 or more | +0.12%, t 0.6 | FAIL |
| spread: top minus bottom quintile positive, t of 2 or more | +0.09%, t 0.4 over 961 days | FAIL |
| beyond the price reaction: spread positive in each reaction group, their mean at least half the unconditional spread | weak reaction -0.15%, middle reaction -0.31%, strong reaction -0.12% | FAIL |
| placebo: shuffled score's long leg has |t| under 2 | -0.03%, t -0.2 | pass |

**Verdict: FAIL.**

Reported, not judged:

| variant | events | long leg excess % | t | since 2020 % | t | spread % | t |
|---|---|---|---|---|---|---|---|
| 5 sessions | 21,991 | -0.02 | -0.2 | +0.02 | 0.1 | +0.05 | 0.4 |
| 60 sessions | 21,991 | -0.29 | -1.2 | -0.50 | -1.0 | +0.48 | 1.3 |
| raw tone level, 20 sessions | 22,381 | -0.19 | -1.4 | -0.30 | -1.1 | -0.51 | -1.9 |
| negative-word share alone, 20 sessions | 22,381 | -0.10 | -0.7 | -0.24 | -0.8 | -0.02 | -0.1 |
| against SPY, 20 sessions | 21,991 | +0.30 | 1.9 | +0.31 | 0.9 | +0.09 | 0.4 |
| after-close calls entered at the next open, 20 sessions | 21,984 | +0.00 | 0.0 | +0.11 | 0.4 | +0.20 | 0.9 |
| price reaction alone, no text, 20 sessions | 22,381 | +0.06 | 0.5 | +0.31 | 1.3 | +0.06 | 0.3 |

### What the result says

The score is not noise. On the earnings day itself the market moves with
it: against the universe, the lowest-tone fifth of calls falls 1.15% and
the highest rises 0.95%, in order across all five groups (t about 14 at
each end), with a rank correlation of 0.15 that holds in every year from
2010 to 2025. A word count is measuring what investors react to.

And by the next session it is in the price. From the open after the
signal day the top fifth earns -0.04% over the universe in 20 sessions
(t -0.4) and the spread over the bottom fifth is +0.09% (t 0.4); the test
could see about 0.5%. Inside each third of the earnings-day reaction the
spread is negative. The plain price reaction, with no text, shows no
drift either (+0.06%, t 0.5): post-earnings drift is not there to be had
in large caps a session late, with or without words.

That matches the published evidence this study was built on: text
predicts returns where it is slow to be read, in small companies and in
the first hours. Midas trades neither.

### Notes on the run

- The first run printed the negative-word row with the same numbers as
  the raw tone level. The score had been written as negative words over
  positive plus negative words, which is tone in other units. It was
  changed to negative words per 1,000 characters and the report rerun;
  the row above is the corrected one. It is an unjudged row and the
  primary result did not move.
- The 60-session spread (+0.48%, t 1.3) and the long leg against SPY
  (+0.30%, t 1.9) are the largest unjudged numbers. Neither is close to
  a result, and the second is the survivorship lift the same-day
  universe was chosen to remove.
- The validity figures in the first paragraph were computed after the
  verdict, to check that a null was not a broken pipeline. They are a
  description of the earnings day, not a tradable result: the reaction
  happens before the entry.

### What would reopen this

A universe where text is read slowly (small and mid caps, with
point-in-time constituents and delisted names), or the ability to act
within the first hour of a release. Neither is a model change.
