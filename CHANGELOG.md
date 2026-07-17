# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Typed `sources[]` entries that parse **nothing** (e.g. a Prisma project declared as
  `rails.models`) are now a hard error naming the source id and the layout the type
  expected, instead of a silently empty success. A new per-source `allow_empty: true`
  opts back into accepting an empty result; `rails.project` passes the flag down to
  both of its expanded halves.
- Prisma: composite `@@id([...])` becomes a composite primary key (list form, with
  `primary` flags on the member columns); a single-field `@@unique([x])` now carries
  the same 1:1 signal as an inline `@unique`; a relation whose `fields:` column is
  `@map`ped now reports the real column name as its foreign key.
- Django: `GenericForeignKey` surfaces as a polymorphic `belongs_to` (details-pane
  only, no edge — same treatment as a Rails polymorphic association); an FK whose
  target can't be resolved statically (swappable `AUTH_USER_MODEL`, contenttypes'
  `ContentType`, ...) keeps its column and skips only the edge; two apps defining
  models with the same class name now both keep their tables (previously one
  silently overwrote the other), with bare references preferring the same app.
- A shared provider contract suite (`tests/test_provider_contract.py`) now holds every
  input provider (`rails.models`, `prisma.models`, `django.models`, `rails.schema`) to
  the same checks — typed dispatch, standalone HTML/Excel generation, DB-layer merge,
  1:N / 1:1 / M:N / self-reference, association provenance, empty-input diagnostics —
  over one common fixture domain, plus advanced per-framework fixtures (composite
  keys, named/self relations, explicit m2m, app collisions, swappable models).
- README and the manual (EN/JA) document the **verified versions** of every input:
  MySQL 8.4 / PostgreSQL 16 (CI, real servers), CPython's bundled SQLite, the Rails
  7.x/8.x `schema.rb` format, the Rails 7.x association DSL, Prisma 5/6 schema
  language, Django 4.2/5.x models.

### Fixed

- Prisma: a 1:N **self-relation** (`parent`/`replies`) and mixed **named relations**
  are no longer misread as implicit many-to-many — the m2m pairing now matches
  `@relation` names on both sides instead of "the other model declares some list".
- Prisma: a typed `prisma.models` source pointing at a directory without a
  `schema.prisma` now exits with a clear message instead of a `StopIteration`
  traceback.

## [0.5.0] - 2026-07-17

### Added

- New `rails.schema` provider: statically parses a Rails `db/schema.rb` file (columns,
  primary keys, indexes, real foreign keys) into a new `schema` layer kind — no live
  database and, crucially, **no Ruby execution** required. A `schema.rb`-derived
  foreign key merges as a **schema FK** (a new teal badge in the viewer, its own
  "schema FK" column in the Excel `--excel` export), reconciled against a covering
  declared/DB-FK association exactly like a live DB FK already is.
- Config gains a typed **`sources:`** list — `{ id, type, path }` entries that name
  their own input kind explicitly instead of relying on `--models`/`models`
  auto-detection. Every registered framework overlay gets a `<name>.models` type for
  free (`rails.models`, `prisma.models`, `django.models`, and any `--adapter`-registered
  overlay's own), plus the new `rails.schema` type and a `rails.project` macro that
  expands a Rails app root into both its `rails.schema` and `rails.models` halves.
  `sources` and `models`/`--models` are independent and both apply if both are given.
- Merge authority order extends for the new `schema` layer: physical facts (column
  types, indexes, primary keys) follow **Config > live DB > rails.schema > models**;
  associations/comments (logical names) follow **Config > models > rails.schema > DB**.
- An untyped `--models`/config `models` path pointing directly at a `schema.rb` file
  now auto-detects as `rails.schema` (with a stderr note suggesting `sources[].type`
  to make it explicit), and ambiguous auto-detection (a path matching more than one
  framework overlay) now prints which frameworks matched and which one won, instead of
  resolving silently.

## [0.4.1] - 2026-07-16

### Fixed

- Viewer: the table list's relation-count badge and 🚫 ban button now stay pinned to
  the row's right edge regardless of table name length or whether a logical name is
  present (previously they drifted, sitting right after the name when no logical
  name filled the row). Long table names wrap instead of being truncated with an
  ellipsis.

## [0.4.0] - 2026-07-16

### Added

- `erdscope demo`: try erdscope instantly with no database of your own. Builds the
  sample e-commerce database (the same schema as `examples/demo_shop.db`) in a temp
  directory, runs it through the normal pipeline, and opens the result in a browser.
  Fixes the pip-installed quickstart: `pyproject.toml` ships this project as a single
  `erd.py` module, so `examples/demo_shop.db` was never actually reachable after
  `pip install erdscope`. Every other flag still applies (`--only`, `--excel`, ...);
  config auto-discovery is force-disabled and an explicit `--config` is ignored with a
  warning, so the demo is deterministic regardless of the cwd. New `--no-open` flag
  skips the automatic browser launch (a no-op outside `demo`)
- CI: a `db-integration` job runs `tests/test_db_integration.py` against real MySQL 8.4
  and PostgreSQL 16 service containers, in parallel with the existing `test` job.
  Verifies table/comment/index/FK extraction (including 1:1 promotion from a unique FK,
  composite primary keys/indexes, and PostgreSQL expression indexes) and, for each
  engine, asserts the PyMySQL/psycopg driver path and the dependency-free mysql/psql
  CLI fallback path produce byte-identical output — the regression test for the NULL
  default bug below. Skips cleanly (no database required) unless `ERDSCOPE_IT_MYSQL_URL`
  / `ERDSCOPE_IT_POSTGRES_URL` are set, so it's safe inside a plain `unittest discover`
- Config file: `engine: "sqlite"` is now accepted alongside `"mysql"`/`"postgres"`.
  `database` is then a local file path (relative or absolute), not a database name,
  assembled into a `sqlite:///` URL that round-trips exactly with the CLI's
  `sqlite:///` handling. `host`/`port`/`user` don't apply to a local file and are
  rejected with a clear error rather than silently ignored; paths containing `?`,
  `#`, or control characters are rejected up front because the path is pasted into
  a URL that is re-parsed downstream

### Fixed

- MySQL CLI fallback (no PyMySQL installed): a column with no default was getting a
  literal `default: 'NULL'` (the four-character string) instead of no default at all.
  `mysql --batch` prints SQL NULL as the bare, unescaped word `NULL` (the `\N`
  escape is the `SELECT ... INTO OUTFILE` / `mysqldump` spelling, not the batch
  client's) — which `_unescape_mysql_field()` didn't recognize, so it passed
  straight through as data.
  Only the CLI fallback path was affected; PyMySQL already mapped `None` to `''`. Fixed
  by also mapping the bare `NULL` literal to `''` in `_unescape_mysql_field()` (accepted
  trade-off, documented in code: a column whose comment/default text is genuinely the
  word "NULL" is now indistinguishable from an actual SQL NULL on this path)

### Changed

- Project metadata and module docstring updated to reflect SQLite support and the
  three-source (DB / code / config) model

## [0.3.0] - 2026-07-14

### Added

- SQLite support: `erdscope sqlite:///path/to/app.db`. Reads tables, columns,
  primary keys, foreign keys, unique/1:1 constraints, and indexes via Python's
  built-in `sqlite3` module — zero dependencies, nothing to install. A ready-to-run
  sample database ships in `examples/` (`examples/demo_shop.db`), with
  `examples/README.md` explaining how to try it
- Pluggable database adapters and framework overlays: engines and code parsers are
  now registered classes (`DBAdapter` / `FrameworkOverlay`). Load a custom one at
  runtime from a plugin file with `--adapter path/to/plugin.py` (or config
  `adapters:`) — no rebuild, works against the single-file `erd.py` or the pip install
- Three input sources, any one sufficient (a database is no longer required): the
  DB, application code (`--models`), and a config `tables:` schema now merge as
  layered providers (database → code → config). Config can declare or patch a full
  schema — add/override/drop tables, columns, indexes, and associations — with
  two-phase (syntactic + semantic) validation
- `--models` is repeatable and merges multiple frameworks in order

### Changed

- `erd.py` is now a build artifact assembled from split sources under `src/erdscope/`
  by `tools/build_single_file.py` (CI verifies it stays in sync). Distribution is
  unchanged: still a single, zero-dependency file

## [0.2.0] - 2026-07-10

### Added

- PostgreSQL support: `erdscope postgres://user@host:5432/db` (or `postgresql://`),
  with an optional `?schema=name` (default `public`). Reads tables, columns, comments,
  indexes, and FK constraints from `pg_catalog`; identity/serial columns are marked
  like MySQL's auto_increment; views and individual partitions are excluded
- Connects via psycopg (v3) or psycopg2 when installed, falling back to the `psql`
  CLI (`COPY … TO STDOUT`) with zero dependencies — both paths produce byte-identical
  output. New pip extra: `erdscope[postgres]`
- Config files gain an `engine` key (`"mysql"`, the default, or `"postgres"`, which
  also switches the default port to 5432)

## [0.1.0] - 2026-07-10

### Added

- Single-file, zero-dependency Python CLI for generating interactive ER diagrams from MySQL databases
- Database-first design: reads tables, columns (with SQL types, defaults, extras), comments, indexes, and real FK constraints from `information_schema`
- Code semantics: merge Rails, Prisma, or Django associations with `--models` flag; visually distinct edge types (declared, DB-FK, inferred)
- Interactive HTML viewer with focus/depth/dependency filtering, two-level hiding, and comprehensive table/column search (regex and case-toggle support)
- Named views and shareable links to preserve specific diagram states
- Readable automatic layouts with viewport-aware packing, crossing reduction, drag-to-snap with guide lines, multi-select align/distribute, and Auto-tidy
- Multiple export formats: PNG (2x), SVG, Mermaid, PlantUML, and Excel table-definition workbooks with customizable templates
- Logical names feature: display database comments alongside physical table names with independent modes for live view and exports
- Config file support (`.erdscope.json`, `.yml`, `.yaml`) with auto-discovery and CLI flag overrides
- Excel workbook generation with per-table detail sheets (columns, defaults, keys, comments, indexes, associations)
- Dark mode, print stylesheet, and resizable/collapsible panes
- Built-in `?` help popup in the generated HTML (shortcuts, mouse gestures, link to the online manual)
- PyPI package distribution with `erdscope` CLI entry point
- Comprehensive test suite (unit and E2E browser automation) with optional openpyxl and Playwright support
