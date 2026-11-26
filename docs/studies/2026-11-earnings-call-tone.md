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

_To be filled in after the run._
