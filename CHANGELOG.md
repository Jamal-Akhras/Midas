# Changelog

All notable changes to Midas are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project has
not yet cut a release, so everything sits under Unreleased.

## [Unreleased]

### Added
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
