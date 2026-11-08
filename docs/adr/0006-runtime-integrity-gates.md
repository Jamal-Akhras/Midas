# ADR 0006: Integrity gates run at run time, not in tests alone

Date: 2026-11-08 · Status: Accepted

## Context

Two defects found in the July 2026 audit trail:

- Bull and bear were designed to run on different providers, and a test
  proved the routing table leads them differently. In practice the
  Anthropic and Gemini keys were empty, both stages fell through to
  gpt-4o, and twenty "two-model" debates were one model arguing with
  itself. The test passed throughout.
- A GOOGL verdict proposed an entry zone of 135 to 145, a stop at 20.85
  and a target of 12 while the live quote was 346.87. The schema checks
  that fields exist, not that they make sense. The thesis was
  auto-approved at L2 and sat waiting for a price that would never come.

## Decision

Gates that depend on run-time state live in the pipeline and write to the
audit trail:

- **Provider diversity.** After the two case stages the committee reads,
  from the router's own decision record, which provider produced each
  accepted answer. If they match, the run is refused with a
  `committee.provider_collision` event and no further spend. The routing
  test stays as a cheap first line. `REQUIRE_CASE_PROVIDER_DIVERSITY=false`
  allows a deliberate single-provider setup: the collision event is still
  written, with `refused: false`, so the ledger never reads a one-model
  debate as a two-model one.
- **Verdict sanity.** `midas.risk.verdict_checks` compares a trade
  verdict with the live quote: entry zone within 15% of it, stop 2 to 25%
  beyond the entry on the thesis side, targets beyond the entry. A failure
  is recorded, the Investor is re-prompted once with the problems and the
  quote, and the corrected verdict is stored as a new artifact version. A
  second failure ends the run without a thesis.

Both gates are deterministic code, in keeping with the rule that risk and
validation are never prompts.

## Consequences

The audit trail now contains the evidence that the committee's design
held on each run, or the event that says it did not. Re-prompting costs
one extra verdict call in the rare failing case, and both verdicts stay
on record.

## Alternatives considered

- Register a provider for every key and trust the routing table: the
  failure mode is exactly that the table was trusted.
- Tighter prompt wording for the Investor: it was already instructed; the
  model ignored it. Gates do not.
