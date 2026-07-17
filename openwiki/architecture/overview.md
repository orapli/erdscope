# Architecture overview

## Architectural intent

`erdscope` separates development maintainability from distribution simplicity:

- Engineers edit concern-oriented fragments under `src/erdscope/`.
- `tools/build_single_file.py` concatenates them in a fixed order as one flat Python module.
- The same build replaces a sentinel in `exporters.py` with the complete HTML/CSS/JS viewer.
- The generated `erd.py` is committed, packaged as the only `py-module`, and exposed as the `erdscope` console command (`pyproject.toml`).

This is an amalgamation, **not a conventional importable `erdscope` package**. Fragments share module globals and rely on build order. The design keeps the zero-required-dependency, single-file product promise while making the source reviewable.

## End-to-end pipeline

The primary entrypoint is `src/erdscope/cli.py:main()`.

1. **Parse CLI and select demo or normal execution.** `demo` delegates to `src/erdscope/demo.py`, creates a temporary SQLite database from embedded SQL, disables config discovery for determinism, and re-enters `_run_pipeline()`.
2. **Load and validate config.** `src/erdscope/config.py:load_config()` discovers `.erdscope.json`, then `.yml`, then `.yaml`, unless disabled. CLI-provided mirror options take precedence because argparse suppresses their defaults.
3. **Load plugins.** Config `adapters` load before CLI `--adapter`; later registration wins. Plugin code is imported into the process and is trusted executable code.
4. **Collect provider layers.** `db_provider()` dispatches a URL to a `DBAdapter`. Each repeatable `--models` path dispatches to a `FrameworkOverlay`. Config `tables:` and `relations:` become additional config providers.
5. **Validate and merge.** Config drops are checked against lower layers before application. `merge_ir()` performs identity merge, association reconciliation, PK/index normalization, and derived fields. Final config references are checked afterward.
6. **Post-process.** Optional `*_id` inference runs, then `--only`/`--exclude` filtering. `fk_columns` is derived from final associations rather than guessed in the viewer.
7. **Serialize and export.** `serialize_for_viewer()` converts internal association provenance into the legacy flags consumed by HTML and Excel. `_finish()` safely embeds JSON into `HTML_TEMPLATE`, writes HTML, and optionally invokes `write_excel()`.

The pipeline deliberately skips database connection and password prompting when no DB URL is supplied (`src/erdscope/cli.py`).

## Core data boundaries

### ProviderResult

Every source enters the merge as a dictionary created by `src/erdscope/ir.py:make_provider_result()`:

```text
{
  source: {kind: db|framework|config, provider: ..., location?: ...},
  tables: <sparse IR fragments>,
  warnings: []
}
```

Database locations are sanitized before recording (`src/erdscope/providers.py:_password_free_url`). Sparse fragments are significant: an absent field does not participate in conflict resolution; an explicit config empty value can clear or replace lower-layer data.

### Internal merged IR

Tables contain columns, indexes, a primary key, associations, optional comments, and derived `fk_columns`/`schema_missing`. Merged associations carry structured `provenance` and `sources`. The detailed merge contract is canonical in [Schema merge domain](../domain/schema-merge.md).

### Output compatibility boundary

The viewer and Excel exporter predate structured provenance. `serialize_for_viewer()` deep-copies the IR, removes `provenance`/`sources`, and emits only `manual`, `db_fk`, or `inferred` booleans; declared framework associations carry no flag. Tests explicitly prevent internal fields from leaking (`tests/test_pipeline.py`).

The HTML template receives three substitutions: title, max rows, and serialized data. JSON escapes `</` so source comments cannot terminate the embedding script; substitutions are ordered to prevent user text from being interpreted as placeholders (`src/erdscope/cli.py:_finish`).

## Integration architecture

### Database adapters

`src/erdscope/db/base.py` defines `DBAdapter`, `register_adapter`, and the URL-scheme registry. Built-ins:

- MySQL: metadata from `information_schema`; PyMySQL first, `mysql` CLI fallback.
- PostgreSQL: metadata from `pg_catalog`; psycopg 3, psycopg2, then `psql` fallback.
- SQLite: stdlib `sqlite3`, opened read-only; views and internal tables excluded.

Built-ins normalize into a common information-schema-like IR builder. Unique single-column FKs become `has_one`. Runtime plugins can register adapters or overlays through `--adapter`.

### Framework overlays

`src/erdscope/frameworks/base.py` defines `FrameworkOverlay`, its registry, and priority-based detection. Rails parsing is association-oriented; Django and Prisma also contribute columns. These are static parsers, so dynamic framework behavior may require `--table-map`, config corrections, or a custom plugin.

### Viewer and exports

`src/erdscope/viewer.html` is a standalone SVG application with no runtime data fetch. It provides exploration, filtering, layout editing, persistent named views, and PNG/SVG/Mermaid/PlantUML exports. State is stored in browser `localStorage`, namespaced by document title. `src/erdscope/exporters.py` writes XLSX directly with stdlib ZIP/XML and supports a five-style-cell template contract.

## Generated build constraints

`tools/build_single_file.py::MODULES` is the assembly source of truth. Directory entries include every `*.py`, placing `base.py` first and sorting the rest. Consequences:

- Moving a definition can break load order even when each fragment looks valid alone.
- A stray helper Python file in `db/` or `frameworks/` is automatically shipped.
- `viewer.html` cannot contain a triple double quote because it is embedded in a raw triple-quoted string.
- Packaging without rebuilding can publish stale code; `python3 tools/build_single_file.py --check` is mandatory before release.

## Why the architecture evolved this way

Recent git history shows a coherent progression:

- The initial database-first, single-file tool expanded from MySQL to PostgreSQL.
- A layered provider/IR merge replaced direct overlay mutation so database, code, and config could each stand alone.
- The large viewer and then Python concerns were split out while retaining deterministic single-file distribution.
- Adapter and overlay registries made built-in and runtime extensions possible without abandoning amalgamation.
- SQLite and `demo` strengthened the zero-setup story; the demo uses the normal pipeline instead of a parallel implementation.
- Real MySQL/PostgreSQL CI was added after fallback discrepancies exposed the need to compare driver and CLI paths.

See `CHANGELOG.md` and targeted commits around the split build, plugin loading, demo, and DB integration tests for exact evolution.

## Architectural watch-outs

- Treat runtime plugin paths as trusted code execution.
- Keep merge order deterministic; never emit from unordered sets.
- Preserve the internal/output provenance boundary unless coordinating viewer, Excel, and compatibility tests.
- Static framework parsing is intentionally bounded; do not claim runtime equivalence.
- Composite PKs exist, but composite FK semantics are not end-to-end supported.
- For very large schemas, browser layout—not CLI generation—is the dominant cost; see [Operations and testing](../operations-and-testing.md).
