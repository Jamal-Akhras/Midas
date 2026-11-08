# ADR 0005: Event flags are scored vetoes, never entry signals

Date: 2026-11-08 · Status: Accepted

## Context

The evidence for LLMs as entry-signal generators is poor (the review's
literature sweep and Midas's own July record agree). The evidence for
LLMs reading documents into structured, sourced facts is good, and Midas
already does that well in its dossiers. The committee's expected-move
numbers were pattern-matched boilerplate; its event reading was not.

## Decision

An `event-analyst` agent turns news and filings for one ticker into
`EventFlag` rows: kind (from a fixed vocabulary), severity, direction, a
one-sentence summary, the source URL, and the price at the time. The
schema rejects unsourced flags; "no flags" is the expected answer.

Flags have exactly two uses:

1. **Veto.** A high-severity negative flag inside `CORE_VETO_DAYS` zeroes
   that ticker's core slice for the rebalance. It never adds to a
   position and never changes the swing book.
2. **Scoring.** The monitor fills each flag's 10- and 20-session forward
   return, the same way it scores screened candidates. `midas stats`
   reports the average forward return after negative flags against the
   screen ledger's baseline. If negative flags do not run below baseline,
   the veto is not earning its keep and should be removed.

Scans run on demand (`midas flags scan`) or on configured weekdays over
the core universe and watchlist, with the usual per-stage cost ceiling.

## Consequences

The LLM's judgement enters the return engine only as a brake, and only
with a ledger that can prove the brake is useful. For an index-ETF core
the veto will rarely fire; the scored record is still the cheapest
experiment on whether the committee's event reading predicts anything,
which also bears on the swing book.

## Alternatives considered

- Flags as entry signals or weight tilts: no evidence they help, and the
  core book's point is that its rule is testable without a model.
- Free-text alerts: unscorable. The fixed vocabulary and required source
  are what make the ledger possible.
