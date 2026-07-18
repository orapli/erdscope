# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.7.0] - 2026-07-18

### Added

- **`--emit-json FILE.json`** — writes a canonical, machine-readable JSON
  projection of the final schema (tables/columns/indexes/associations plus
  resolved notes/groups) alongside the HTML, wrapped with a `sha256` content
  fingerprint (`-` for stdout; the HTML is still generated either way). A
  fixed key allowlist (no internal/plugin keys), deterministic column/index/
  association ordering, and 5-value association provenance (declared/manual/
  db_fk/schema_fk/inferred) make the snapshot diff-friendly and stable across
  table/notes/groups/sources reordering — same input, byte-identical output.
  Purely additive: existing HTML/Excel output is untouched.
- **`--emit-config FILE`** — writes the final merged schema as a
  config-authoring file (YAML if `.yml`/`.yaml`, JSON if `.json`, `-` for
  stdout as JSON) that can be re-imported via `--config` for a
  semantically-equivalent ("level1") round trip: same tables, columns,
  types/nullability/defaults, primary-key column sets (composite PKs
  re-derived from column flags, not the possibly-truncated `primary_key`
  field), indexes as (columns, unique) sets, associations as (type, target,
  foreign_key, through, polymorphic) tuples, comments, notes, and groups.
  Not a byte-identical round trip: provenance, `sources`, and config
  drop/`*_mode` operations don't survive one pass through the merged IR. A
  handful of config-load/validation rules were relaxed to make the reimport
  possible: a non-drop index no longer requires a `name`; a relation note's
  `foreign_key`/`name`/`through`/`assoc_type`/`polymorphic` now distinguish
  an explicit `null` (must be absent on the match) from an omitted key
  (wildcard); a polymorphic association's target is no longer required to
  name a real table, and a polymorphic relation note survives `--only`/
  `--exclude` filtering as long as its source table does; a Rails-only
  (`schema_missing`) table's association foreign_key is no longer required
  to name a real column. The YAML writer is deterministic (sorted keys,
  literal block style for multi-line note text) and always quotes any plain
  scalar that YAML's own implicit-typing rules would otherwise misread as a
  bool/int/float on the next load (the "Norway problem"). Purely additive:
  existing HTML/Excel/`--emit-json` output is untouched.
- **`--diff SNAPSHOT.json`** — a CI drift gate: compares this run's schema
  against a previously-saved `--emit-json` snapshot at "level1" (materially
  the same schema, not byte-identical — the same notion `--emit-config`
  round trips to) and exits instead of generating any output. `added` =
  present in this run only, `removed` = present in the snapshot only, at
  every level (tables, then each common table's columns/indexes/
  associations, plus notes/groups); indexes match on `(columns, unique)`
  (name-blind), associations match on `(type, target, name, foreign_key,
  through, polymorphic)` with provenance/sources ignored unless
  `--diff-provenance` is passed. Exit 0 when identical, 1 when different
  (`--diff-exit-zero` to report without failing), 2 on a usage error or an
  unreadable/invalid snapshot. `--diff-format text` (default) or `json`
  (deterministic, for scripting). A matching snapshot `fingerprint` short-
  circuits the comparison. Not combinable with `--emit-json`/`--emit-config`/
  `--excel` (a `--diff` run is comparison-only). Like `--emit-config`, an
  empty-string default vs. no default at all isn't distinguished by any
  provider, so it's the one change `--diff` can't detect — a known level1
  limit, called out in `--diff --help` and the manual.
- The `--emit-json` snapshot shape and its `sha256` fingerprint are the stable
  **format 1** contract, versioned by the top-level `format` field — a breaking
  change to the projection bumps it (to `format` 2, …) rather than silently
  altering what existing consumers parse. Two properties of that contract worth
  stating outright: provenance is **association-limited** (a table, column, or
  index records no "which input won it" marker — only associations do); and an
  empty-string column default and no default at all are indistinguishable to
  every provider on read, so that one difference round-trips through neither
  `--emit-config` nor `--diff`.

## [0.6.0] - 2026-07-18

### Added

- **notes (Phase 1)** — a new config `notes:` list attaches design decisions,
  operational rules, and ADR links to a table, a relationship, or the whole
  diagram. A pure sidecar: it never touches physical schema, merge precedence,
  or association provenance. Two-stage validation (syntax at load, semantics
  against the final merged IR); every error names the offending note id.
  Plain-text only, HTML-escaped, with link URLs restricted to http(s). The
  viewer shows table/relation notes in the detail panel and a global note in
  the legend, and folds note text into both the filter and the Highlight
  search; hidden tables suppress their notes. `--only`/`--exclude` drop any
  note whose endpoint table(s) didn't survive. A notes-free config stays
  byte-for-byte identical (no `notes` key), so the sidecar is inert for
  existing use.
- **groups (Phase 1)** — a new config `groups:` list (`{id, title?, tables,
  color?}`) draws a labeled, rounded frame behind a set of related tables,
  mirroring notes end to end as a pure sidecar. Two-stage validation (unique
  id, non-empty `tables`, no within-group duplicate, hex `color`; every member
  exists in the merged IR; no table may belong to two groups). The viewer
  draws backmost frames sized to their displayed members and follows node
  drags live; drag a group by its title chip to move every member at once;
  a toolbar toggle shows/hides all frames; PNG/SVG export includes them.
  `--only`/`--exclude` trims membership and drops any group left empty. A
  groups-free config stays byte-for-byte identical (no `groups` key).
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
