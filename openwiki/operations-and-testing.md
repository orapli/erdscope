# Operations and testing

## Local test levels

### Fast focused checks

Run the suite nearest the change:

```bash
python3 -m unittest tests.test_merge_ir -v          # authority, identity, provenance
python3 -m unittest tests.test_config_validation -v # strict config syntax
python3 -m unittest tests.test_pipeline -v           # CLI-to-serialized-IR wiring
python3 -m unittest tests.test_demo -v               # installed-style demo behavior
python3 -m unittest tests.test_build -v              # amalgamation determinism
python3 -m unittest tests.test_sqlalchemy_provider -v # SQLAlchemy AST provider
python3 -m unittest tests.test_erd -v                # Laravel parser/provider behavior
python3 -m unittest tests.test_provider_contract -v   # typed provider contracts
```

Parser and exporter behavior is concentrated in `tests/test_erd.py`; typed DBML/Mermaid input has dedicated `tests/test_dbml_input.py` and `tests/test_mermaid_input.py`; projection/diff contracts are in `tests/test_emit_*.py` and `tests/test_diff.py`; golden compatibility is in `tests/test_characterization.py`.

### Full dependency-free pass

```bash
python3 -m unittest discover -s tests
```

CI runs this before installing optional packages because zero required dependencies is a product guarantee. Optional tests must skip cleanly rather than fail.

### Full optional/browser pass

```bash
pip install playwright openpyxl pyyaml
playwright install chromium
python3 -m unittest discover -s tests -v
```

- Playwright drives `tests/test_e2e.py` in Chromium.
- `openpyxl` round-trips generated workbooks in tests.
- PyYAML enables YAML config tests; JSON config remains dependency-free.

Automated browser coverage does not currently include Firefox or WebKit.

## Database integration tests

`.github/workflows/ci.yml` runs a parallel job against MySQL 8.4 and PostgreSQL 16 service containers. `tests/test_db_integration.py` verifies tables, comments, indexes, FKs, unique-FK one-to-one promotion, composite PK/index behavior, PostgreSQL expression indexes, and parity between Python-driver and CLI-fallback paths.

The tests are gated locally by:

- `ERDSCOPE_IT_MYSQL_URL`
- `ERDSCOPE_IT_POSTGRES_URL`

They create/drop disposable databases whose names must satisfy the suite’s safety guard. Read the test setup before pointing it at any server. Never use a production endpoint.

The MySQL fallback deliberately maps bare batch output `NULL` to SQL null, accepting that literal text `NULL` is indistinguishable on that path (`src/erdscope/db/base.py:_unescape_mysql_field`). Preserve driver/fallback parity when touching decoding.

## CI contract

The main `test` job performs:

1. Dependency-free unittest discovery.
2. Optional dependency and Chromium installation.
3. Full unittest/browser pass, including E2E coverage for alignment, auto-expanded-table promotion, and group obstacle layout.
4. `python3 tools/build_single_file.py --check`.
5. Deterministic `docs/index.html` regeneration and diff check.
6. Screenshot generator smoke test.

The DB integration job runs independently. The project advertises Python 3.9+, but CI currently selects one latest `3.x` Linux interpreter rather than a version/OS matrix.

## Generated-artifact runbook

After any split Python or viewer change:

```bash
python3 tools/build_single_file.py
python3 tools/build_single_file.py --check
```

After viewer or demo-visible changes:

```bash
python3 docs/gen_demo.py
git diff --exit-code docs/index.html
python3 docs/gen_shots.py
```

Treat drift as a build failure. `erd.py` and `docs/index.html` are committed artifacts; source fragments and generators are authoritative.

## Performance benchmark

The manual benchmark is outside unittest discovery:

```bash
python3 benchmarks/gen_schema.py --tables 300 --out /tmp/bench_300.db
python3 benchmarks/run_bench.py --tables 100,300,1000
```

`benchmarks/run_bench.py` generates synthetic SQLite schemas, measures median CLI time, reference fetch/merge/export phases, first paint and relayout in headless Chromium, output size, and best-effort memory. Browser metrics require Playwright/Chromium in the configured benchmark interpreter.

Published repository guidance shows CLI generation remains small at 1,000 tables while browser initial layout/re-layout becomes the bottleneck. The benchmark flags first paint over 3 seconds or re-layout over 1 second; practical docs recommend narrowing with `--only`/`--exclude` around very large schemas (roughly 900+ tables). These are environment-specific observations, not hard guarantees.

For large image exports, the viewer caps PNG canvas dimensions and may reduce scale; SVG is the reliable fallback (`src/erdscope/viewer.html`).

## Connection and security notes

- Recommend read-only DB accounts for normal use.
- Config rejects password fields and full connection URLs. MySQL/PostgreSQL credentials should come from environment/client config or hidden interactive prompting as documented in `README.md`.
- SQLite opens source databases read-only.
- Provider metadata strips URL passwords.
- Runtime `--adapter` files are imported Python and must be trusted.
- Generated HTML contains schema names/comments and possibly defaults; treat it as sensitive documentation if the source schema is sensitive.

## Troubleshooting guide

| Symptom | First checks |
|---|---|
| `erd.py is out of date` | Rebuild from `src/erdscope/`; inspect changes rather than editing artifact |
| YAML config fails | Install PyYAML or use JSON; inspect strict unknown/type errors |
| Unknown DB scheme | Confirm URL and plugin loaded before dispatch; inspect adapter registry |
| `--models` not detected | Check overlay `detect()` expectations and path: Rails/Django/Prisma/Laravel use shallow marker checks, while SQLAlchemy recursively detects model content at build depth; for Laravel project roots, verify `app/Models` contains Eloquent evidence; use typed `<overlay>.models` sources or `--table-map`/config for static-analysis gaps |
| Unexpected duplicate/missing edge | Inspect association identity, FK/name aliases, and `reconcile_db_fks()` tests |
| FK badge missing | Verify a final association has `foreign_key`; names alone do not produce `fk_columns` |
| Empty result after filtering | Check comma/repeatable glob patterns; pipeline intentionally fails when no table remains |
| Browser slow on huge schema | Narrow tables before generation; use benchmark to distinguish generation from layout cost |
| Driver and CLI outputs differ | Reproduce with `tests/test_db_integration.py`; focus on TSV/COPY null and escaping paths |
| Typed DBML/Mermaid input changes | Run `tests/test_dbml_input.py` and `tests/test_mermaid_input.py`; check source kind and merge rank |
| Projection output changes | Run the matching `tests/test_emit_*.py` contract; preserve deterministic ordering and stdout/path collision rules |

## Pre-release checklist

Before pushing a `v*` tag:

- Working tree reviewed and generated artifacts current.
- Full dependency-free and optional/browser suites pass.
- Real DB integration passes for DB-layer changes.
- `erd.py --check`, demo regeneration diff, and screenshot smoke test pass.
- `pyproject.toml`, `CHANGELOG.md`, and tag version agree.
- `python -m build` succeeds and package contents are inspected.

The tag workflow uses PyPI trusted publishing and requires no API token in repository secrets (`.github/workflows/release.yml`).
