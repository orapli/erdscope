# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
