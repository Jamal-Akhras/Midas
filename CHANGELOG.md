# Changelog

All notable changes to Midas are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project has
not yet cut a release, so everything sits under Unreleased.

## [Unreleased]

### Added
- Apache-2.0 licence.
- This changelog.
- Quality gates: ruff (lint and format), mypy with the pydantic plugin,
  a coverage floor, pre-commit, CodeQL and Dependabot. CI runs lint,
  typecheck and tests on Python 3.10, 3.12 and 3.13.
- Alembic migrations (`midas/migrations`). `midas init` stamps fresh
  databases, adopts pre-Alembic ledgers, and upgrades versioned ones.
- Dockerfile (non-root, sqlite ledger on a mounted volume) and a
  `midas ui --host` flag for container use.

### Changed
- Dependency ranges carry upper bounds on the next major version.
- The one-off options backfill script moved to `scripts/`.

### Fixed
- `test_news_raises_without_keys` no longer depends on whether the
  operator's `.env` contains Alpaca keys.

## 2026-07-10 to 2026-07-23 (pre-changelog history)

Six commits covering M0 to M3: fork skeleton, domain models, autonomy
ladder, model router with write-ahead cost logging, tool layer, committee
pipeline, deterministic risk engine, paper execution mirrored to Alpaca,
monitor, outcome scoring, analyst workstation UI, evidence integrity,
options mirroring, counterfactual ledger, post-mortem agent, CI.
