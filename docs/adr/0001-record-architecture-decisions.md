# ADR 0001: Record architecture decisions

Date: 2026-11-08 · Status: Accepted

## Context

Midas was built in six large commits in July 2026. The reasoning behind
its structure (why risk is code and not a prompt, why the two cases run on
different models, why real money has no code path) lives in docstrings and
the README, and the reasoning behind later changes would otherwise live
nowhere. The companion project, Tollgate, keeps a decision log and it has
made that repository far easier to review.

## Decision

Keep architecture decision records in `docs/adr/`, one file per decision,
numbered, in the form: context, decision, consequences (and alternatives
when they were seriously weighed). A record is written when the decision
is made, is never edited into a different decision afterwards, and is
superseded by a new record when the decision changes.

The first records (0002 to 0007) document the November 2026 hardening
pass retroactively, because the choices made there are the ones a reader
will most want explained.

## Consequences

Reviewers can see why the system is shaped the way it is without reading
the history. The cost is a short document per structural change, which is
the point: a change that cannot be explained in a page should be looked at
again.
