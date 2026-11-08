# Midas

[![ci](https://github.com/Jamal-Akhras/Midas/actions/workflows/ci.yml/badge.svg)](https://github.com/Jamal-Akhras/Midas/actions/workflows/ci.yml)

A personal, single-user trading committee bot. A committee of AI agents
researches candidates with real tools (market data, SEC EDGAR, news, web
search), argues bull vs bear on different models, structures defined-risk
option trades through deterministic code checks, and issues fully-audited
verdicts that the operator approves - paper trading only.

![The Midas Analyst Workstation: live committee stance (bull vs bear), a deterministic risk budget, open positions, the equity curve, and a per-agent cost scoreboard](docs/images/workstation.png)

**This is decision-support tooling, not financial advice.** Single user,
own money, no public exposure. Every decision can be reconstructed months
later from the audit trail alone.

Forked from the Verdict multi-agent platform's architecture: model router
with write-ahead cost logging, commit-per-stage orchestration with crash
resume, agents-as-data, append-only audit ledger, demo mode on the same
code path as production.

## Scope

- **Swing options**: long calls/puts and debit spreads, minimum 14 DTE at
  entry (30-60 preferred). Defined-risk only - no short premium, ever.
- **Long-term holdings**: share positions held weeks to years.
- Daily bars and scheduled re-evaluations. Never tick-level speed.

## Quick start (zero keys, zero spend)

```bash
pip install -e ".[dev]"
midas init          # creates sqlite db; demo mode auto-enables with no keys
midas screen        # screener scans the demo universe
midas run ACME      # full committee pipeline for one ticker
midas cycle         # screen, then committee on every candidate
midas approvals     # see the proposed thesis
midas approve <id>  # approve -> executes on the paper account
midas positions     # open positions + account
midas monitor       # one scheduler pass (re-evaluations, marks, halts)
midas digest        # weekly digest - the L2 review surface
midas stats         # track record vs the L3 bar
midas audit         # the append-only audit trail
midas ui            # the Command Center: http://127.0.0.1:8420
pytest              # the whole suite runs offline in demo mode
```

Add real keys in `.env` (see `.env.example`) to leave demo mode. Any one
model key enables real AI; Alpaca/Finnhub/Brave keys enable real data.

## The committee

| Stage | Actor | Kind |
|---|---|---|
| Universe screen | Screener Agent | LLM + market-data tools |
| Price/volume floors | code | deterministic |
| Fundamentals | Earnings/Fundamentals Agent | LLM + earnings/estimates tools |
| Deep research | Deep Research Agent | LLM + EDGAR, insiders, web search, page fetch |
| Catalysts | Catalyst/News Agent | LLM + news/earnings tools |
| "Passable?" gate | code floors + cheap-model triage | mixed |
| Bull case / Bear case | two agents, **different models**, independent | LLM |
| Option structure | code generates candidates -> LLM assist picks -> code re-validates | mixed |
| Risk & sizing | risk engine | **deterministic code only** |
| Verdict | Investor Agent (risk check is binding) | LLM, high-tier models |
| Execution | paper broker + execution-time risk re-check | code |
| Re-evaluation | Investor Agent on schedule + events (invalidation hit, earnings within 7 days, DTE decay) | LLM + tools |
| Outcome scoring | code, graded by the market | deterministic |

Honest-data rule: agents never state a number a tool did not return in that
conversation. Every dossier claim carries a source URL and retrieved-at
timestamp; unverifiable material lands in an explicit `unverified` list
(schema-enforced).

## Autonomy ladder

Promotion is a **manual config change** (`AUTONOMY_LEVEL`), never something
the system grants itself.

- **L0** (default): committee recommends; the operator approves everything.
- **L1**: approved theses auto-execute on PAPER; exits still proposed.
- **L2**: Investor Agent manages entries and exits on PAPER within hard caps.
- **L3+**: real money. **The code path does not exist in this codebase.**
  `AUTONOMY_LEVEL=3` fails validation on purpose. The bar before anyone
  writes it: >=30 days and >=20 closed paper positions at L2, positive
  expectancy, max drawdown inside the configured limit (`midas stats`
  reports progress).

## Risk rules (enforced in code, twice)

Per-trade risk cap, option premium caps (per-trade and total open), max
open positions, sector concentration, minimum DTE, liquidity floors
(open interest, bid-ask spread), daily/weekly loss limits that **halt the
whole pipeline** (`midas halt --clear` is the only way back), and a daily
AI spend ceiling. All checks run at recommendation time and AGAIN at
execution time; the execution-time result is stored on the order.

## The Analyst Workstation

`midas ui` serves a local web console (127.0.0.1 only, stdlib server, zero
extra dependencies) over the same audited code paths as the CLI. Five tabs:

![A full committee run for AAPL end to end: each agent output with its cost, a bull case (8/10) and bear case (4/10) argued side by side, SEC-filing provenance, deterministic option-structure and risk checks, and the final NO TRADE verdict](docs/images/decision-trace.png)

*One committee run, fully traced: the bull and bear analysts argue on different models, the structurer and risk engine are deterministic code (here: no valid option structure, all risk checks pass), every claim links back to a SEC filing or news source, and the verdict — NO TRADE — is recorded with its reasoning.*

- **Overview**: live ticker tape over the universe/watchlist, KPI strip
  (equity, open P&L, realized today/week, premium-at-risk vs cap, win rate,
  AI spend), candlestick chart with the thesis overlaid (entry band, stop,
  targets), committee stance bar (bull vs bear conviction), risk-budget
  gauges, portfolio with CURRENT / BULL / BEAR projection toggles (options
  reprice as payoff-at-expiry under each case's expected move), allocation
  donut, equity curve (snapshots accrue via `EquityPoint`), approvals
  mini-queue, and the committee scoreboard.
- **Research**: run list -> committee tree (every agent node with status,
  model, cost, and a one-line result; code stages marked ⚙) -> decision
  trace (snapshots -> dossier -> cases -> structure -> risk -> verdict ->
  approval -> fills -> re-evals -> outcome, in one timeline) -> full
  artifact drill-down (sourced dossier claims, conviction cards, PASS/FAIL
  check grids, raw data snapshots).
- **Portfolio**: current, bull-case, and bear-case valuations side by side,
  stops, review schedule, jump-to-run links, plus track record vs the L3 bar.
- **Approvals**: approve-and-execute / deny, with "open dossier" links.
- **Audit**: the filterable append-only ledger.

Actions (screen / run ticker / monitor pass / clear halt) execute in
background threads; the UI never bypasses approvals, risk checks, or the
autonomy ladder.

## Provenance

- `DataSnapshot`: every tool result, verbatim, linked to the artifact that
  consumed it.
- `ResearchArtifact`: every agent output, versioned, with model + cost.
- `CostEvent`/`ModelCall`: write-ahead cost log; every prompt/response and
  the router's reasoning, including failed parses and tool-loop steps.
- `AuditEvent`: append-only ledger of every state transition.
- `TradeOutcome`: every closed position scored against its thesis - P&L,
  which case proved right, whether invalidation worked. The eval harness,
  graded by the market.

## Layout

```
midas/
  config.py        settings + risk caps + the L3 bar
  models/          domain (runs, artifacts, snapshots, theses, orders,
                   positions, checkpoints, outcomes) + ops (cost, audit)
  providers/       router (tool loop, write-ahead logging), routing.yaml,
                   anthropic/openai/gemini/demo providers
  tools/           market data (alpaca->yfinance->demo), EDGAR, earnings/news
                   (finnhub/fmp), web search/fetch, snapshot infrastructure
  agents/          schemas (validated outputs), defs (agents as data), prompts/
  risk/            deterministic risk engine + option structure hard checks
  pipeline/        screen + per-ticker committee (commit-per-stage, resume)
  execution/       paper broker (sim), executor (autonomy + re-checks)
  scheduler/       monitor loop (reviews, event triggers, halts, scoring)
  outcomes/        outcome scoring + stats vs the L3 bar
  outcomes/digest  weekly digest builder (the L2 review surface)
  ui/              the Command Center (stdlib web server + single-file UI)
  cli.py           the operator console
tests/             offline tests (demo mode, in-memory SQLite)
```

## Known limitations (honest list)

- Demo option pricing is shape-realistic but value-synthetic; deep OTM
  spreads look unrealistically cheap in demo mode. Real chains via yfinance
  behave properly.
- Equity paper orders mirror to the real Alpaca paper account when keys
  are set (Alpaca's fill price feeds the ledger; the Alpaca dashboard is an
  independent record). Options fill on the internal simulator at live chain
  mids - Alpaca options approval and OCC multi-leg orders are the next step
  if dashboard-visible options trades matter.
- `UNIVERSE=sp500` needs a constituent-list source; use a comma-separated
  `UNIVERSE`/`WATCHLIST` for now.
- Debit spreads are simulated as one net-debit instrument (both strikes in
  the symbol, marked long-mid minus short-mid).
- Roll decisions close the old position; opening the replacement leg is a
  new committee run by design (a roll is a new trade).

## Where an LLM adds value vs code (honest assessment)

LLMs: reading filings/news into sourced dossiers, adversarial bull/bear
argument, thesis articulation, re-evaluation reasoning against a thesis.
Deterministic code (and it stays code): screening floors, liquidity checks,
position sizing, premium caps, loss halts, structure validation, fills,
outcome scoring. A plain screener would beat an LLM at the floors; that is
why the floors are code.
