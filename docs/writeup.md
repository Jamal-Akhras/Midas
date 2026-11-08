# What an LLM trading committee actually costs, and what it got wrong

*November 2026. Midas is a personal paper-trading system; the numbers
below come from its own audit trail and from backtests anyone can rerun
with `midas research report`.*

In July 2026 I built Midas: a committee of LLM agents that research a
stock with real tools, argue the bull and bear case on different models,
hand a defined-risk option structure to a deterministic risk engine, and
issue a verdict that a human approves. Every tool result, prompt, response
and state change is written to an append-only ledger. It ran on a paper
account for ten days, then sat idle. In November I came back to it with a
question the architecture had never been asked: would any of this beat a
savings account?

This is what the record and the backtests said, and what I changed.

## What the audit trail showed

The trail is the best part of the system, because it does not flatter it.

**The two-model debate was one model.** The design's core idea is that the
bull and bear cases come from different providers so their disagreement
carries information. A test proved the routing table leads them
differently. In July the Anthropic and Gemini keys were empty, the router
fell through, and twenty debates ran bull and bear on gpt-4o. The test
passed the whole time. Nothing checked what actually happened.

**A verdict hallucinated its prices.** On 17 July the Investor agent
proposed a GOOGL entry zone of 135 to 145, a stop at 20.85 and a target of
12, while the tool quote in the same conversation said 346.87. The schema
checked that the fields existed, not that they made sense. The thesis was
auto-approved and sat waiting for a price that would never come. A
different gate, meant to stop chasing gaps, saved the account by accident.

**The only loss halt was a bookkeeping bug.** Equity "fell" 15% in one
monitor pass because a sale's proceeds were lost to a write race between
two processes. The halt fired correctly on a loss that had not happened.

**The expected moves were boilerplate.** Across twenty cases the bear side
said -5% in fourteen of them and the bull side said +5, +10 or +12% over
30 days almost every time. These numbers drive strike selection and stop
placement. The model was pattern-matching the prompt, not estimating a
distribution.

**The economics were impossible at the account size.** At $1,000 and
$0.50 a day, model spend was 12% of equity a year, three times the hurdle
a savings account sets, before any trading edge. Position sizes of $75
made the 1%-risk framework meaningless.

None of these would have been visible without the ledger. All of them
were invisible to the tests.

## What the backtests showed

The committee's deterministic skeleton (pullback-in-uptrend entries, a 5%
stop, a 21-day time stop, a regime throttle, the same caps the live code
enforces) can be run without a model. On S&P 500 names from 2007, with
costs and today's survivorship-biased constituent list helping it, it made
6.2% a year with a 40% drawdown against SPY's 10.6% and 55%. Since 2022,
with cash at 4%, it returned roughly zero. The 42% win rate and +0.08R
average trade say why: a 5% stop on a large cap is a week of noise, and the
time stop truncates the winners the thesis needs.

The simplest rule that beat cash in every window was "hold SPY above its
200-day average, else T-bills": 8.2% a year, Sharpe 0.59 against SPY's
0.54, maximum drawdown 21% against 55%. A diversified version over five
ETFs did similar work with shallower drawdowns. Nothing exotic, nothing a
model is needed for.

Two other probes closed off two other hopes. A walk-forward LSTM on
thirteen years of out-of-sample daily data predicted direction with 52.4%
accuracy, below "always up" at 52.9%. And of the three best-documented
intraday rules on ten years of 5-minute bars, the only one that replicates
(the first-candle opening-range breakout) earns its entire 27% a year
gross from about three basis points per trade: at half a basis point of
slippage per side it keeps 17%, at one it keeps 7%, at two it loses money.
The edge is inside the spread.

## What changed

The conclusion was not "LLMs do not work". It was that the committee is a
good research and audit machine wrapped around a trading style that loses,
and that the system had no way of knowing either fact about itself.

**The return engine is now rules.** A core book holds equal slices of five
ETFs, each only while above its 200-day average, rebalanced monthly. It
trades through the same autonomy ladder, broker mirroring and audit trail
as the committee, in its own book, so the committee's stops, reviews and
statistics cannot touch it. The function it trades is the function the
backtest report measures.

**The committee's judgement enters only as a brake, with a ledger.** An
event analyst reads news and filings into structured flags with a source
URL each. A high-severity negative flag vetoes a core slice for a month.
Every flag is scored against the forward return, and the stats compare
negative flags against the baseline of every stock the screener looked at.
If negative flags do not run below baseline, the veto goes.

**The gates moved from tests to run time.** The committee now reads which
provider produced each case from the router's own decision record and
refuses the run if they match. A deterministic sanity check compares every
trade verdict with the live quote and re-prompts once with the problems.
Both write to the audit trail, so the next review reads evidence instead of
hoping.

**Costs have a second ledger.** Behind Tollgate, the gateway I built for
this purpose, every call's charge comes back in a header and is stored
beside the router's own estimate; a divergence event fires when they
disagree. It would have caught the stale price table in a day.

**The repository can now be reviewed.** Lint, types, a coverage floor,
migrations with a test that the chain reproduces the models, CodeQL, a
decision log, and a results document that is generated rather than
written. The throwaway scripts became a typed, tested research package;
on the review's cached data it reproduces the review's figures to four
decimals.

## What it cost

Two July paper runs spent $1.96 on model calls over six days, all on
OpenAI, mostly on deep research and verdicts. The November review and
rebuild spent nothing on models: the backtests are pure code, the LSTM
ran on a local GPU in thirty seconds, and the demo path exercises every
agent at zero spend. The core book costs nothing to run. The event
analyst costs about a cent per ticker per scan on Haiku 5.5.

## What is next

The core book needs a live paper record, which the design now gives it
the same way it gives the committee one: months at L1 or L2 on a seed
large enough for sizing to mean something, judged by the same statistics.
The flag ledger needs a few hundred scored flags before it says anything.
The intraday rule stays a research sleeve with a promotion gate written
down in advance: measured slippage at or below half a basis point per side
on QQQ over six months of paper fills, or it stays on the shelf.

The honest summary for anyone building something similar: put the ledger
in first, backtest the rules you are about to automate before you automate
them, and make the system check its own design at run time. The model
layer was the least of the problems. Finding that out required everything
else.
