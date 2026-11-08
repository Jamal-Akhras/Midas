# ADR 0002: Quality gates and schema migrations

Date: 2026-11-08 · Status: Accepted

## Context

The repository had tests and CI but no linter, formatter, type checker,
coverage floor or migration tool. One test depended on the operator's
`.env`; CI was green only because CI had no keys. Schema changes were
`create_all` plus two hand-written `ALTER TABLE` statements in `init_db`,
which could not express a column rename or a data migration and left no
record of what the live ledger's schema was.

## Decision

- **ruff** for linting (pycodestyle, pyflakes, isort, bugbear, pyupgrade,
  simplify) and formatting at line length 100. The initial format is one
  mechanical commit; everything after is enforced by CI and pre-commit.
- **mypy** with the pydantic plugin on the whole package
  (`check_untyped_defs`), and the strict profile for new modules
  (`midas.research`, `midas.strategy`), with `warn_return_any` off there
  because pandas ships no types. Legacy modules were fixed rather than
  excluded: the 126 initial errors were real `Optional` dereferences and a
  stage runner that returned `BaseModel` instead of the schema type.
- **Coverage floor** at 60%, measured in CI, raised as the research and
  strategy packages add tested code. The browser console is excluded.
- **Alembic** for migrations. `midas init` creates and stamps an empty
  database, adopts a pre-Alembic ledger (adds the two hand-added columns,
  stamps head), or upgrades a versioned one. A test upgrades an empty
  database through every migration and asserts autogenerate would add
  nothing, so models and migrations cannot drift silently.
- **CodeQL** and **Dependabot** on the repository; a **Dockerfile** with a
  non-root user and the ledger on a mounted volume.

## Consequences

Local and CI runs enforce the same rules. A new table or column is a
migration file reviewed in the diff. Adopting the live July 2026 ledger
was tested on a copy before the code was committed.

## Alternatives considered

- Pyright instead of mypy: comparable; mypy's pydantic plugin decided it.
- Excluding legacy modules from mypy: faster, but it would have left the
  `Optional` dereferences that caused real `AttributeError`s in place.
