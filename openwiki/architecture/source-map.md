# Source map

Use this page to move from a question or change request to the smallest relevant source and test set.

## Product and entrypoints

| Concern | Primary source | Verification / companion evidence |
|---|---|---|
| Package metadata and console script | `pyproject.toml` | `README.md`, `CHANGELOG.md` |
| CLI arguments and orchestration | `src/erdscope/cli.py` | `tests/test_pipeline.py`, `tests/test_erd.py` |
| Zero-setup sample | `src/erdscope/demo.py` | `tests/test_demo.py`, `examples/`, `docs/gen_demo.py` |
| Generated distributable | `tools/build_single_file.py`, `erd.py` | `tests/test_build.py`, CI build-drift check |

`erd.py` is output evidence, not the editing source. Search it only when validating the packaged artifact or reproducing installed behavior.

## Domain and data flow

| Concern | Primary source | Focused tests |
|---|---|---|
| Provider/provenance contract and type normalization | `src/erdscope/ir.py`, `src/erdscope/header.py` | `tests/test_merge_ir.py`, `tests/test_characterization.py` |
| Layer precedence and association reconciliation | `src/erdscope/merge.py` | `tests/test_merge_ir.py`, `tests/test_pipeline.py` |
| Config parsing and strict syntactic validation | `src/erdscope/config.py` | `tests/test_config_validation.py`, `tests/test_erd.py` |
| Config providers, drop/reference validation | `src/erdscope/providers.py` | `tests/test_pipeline.py`, `tests/test_merge_ir.py` |
| Output serialization, projections, and filtering | `src/erdscope/cli.py:serialize_for_viewer`, `_finish`, `src/erdscope/{emit,digest,dbml,mermaid,plantuml}.py` | `tests/test_pipeline.py`, `tests/test_emit_*.py`, `tests/test_diff.py` |
| Typed input normalization and dispatch | `src/erdscope/sources.py`, `src/erdscope/dbml.py`, `src/erdscope/mermaid.py` | `tests/test_dbml_input.py`, `tests/test_mermaid_input.py`, `tests/test_config_validation.py` |

Read [Schema merge domain](../domain/schema-merge.md) before modifying this area. `REFACTOR_PLAN.md` provides historical design context, but current source and tests are authoritative where comments or plans are stale.

## Database integrations

| Engine/concern | Source | Test anchors |
|---|---|---|
| Registry, plugin loading, shared IR construction, TSV decoding | `src/erdscope/db/base.py` | `tests/test_erd.py`, `tests/test_pipeline.py` |
| MySQL acquisition and fallback | `src/erdscope/db/mysql.py` | `tests/test_db_integration.py`, `tests/test_erd.py` |
| PostgreSQL acquisition and fallback | `src/erdscope/db/postgres.py` | `tests/test_db_integration.py`, `tests/test_erd.py` |
| SQLite read-only acquisition | `src/erdscope/db/sqlite.py` | `tests/test_erd.py`, `tests/test_demo.py` |
| Real database fixtures | `tests/fixture_db/` | `.github/workflows/ci.yml` `db-integration` job |

The integration suite compares driver and command-line fallback output for MySQL/PostgreSQL. Preserve this parity when changing row decoding or catalog queries.

## Framework integrations

| Framework/concern | Source | Test anchors |
|---|---|---|
| Registry, detection, inflection, FK inference | `src/erdscope/frameworks/base.py` | `tests/test_pipeline.py`, `tests/test_erd.py`, `tests/test_provider_contract.py` |
| Rails static parser | `src/erdscope/frameworks/rails.py` | Rails fixtures under `tests/fixture_app` and parser tests |
| Django AST parser | `src/erdscope/frameworks/django.py` | `tests/fixture_django/`, pipeline/parser tests |
| Prisma parser | `src/erdscope/frameworks/prisma.py` | `tests/fixture_prisma`, pipeline/parser tests |
| SQLAlchemy declarative-model parser | `src/erdscope/frameworks/sqlalchemy.py` | `tests/test_sqlalchemy_provider.py`, `tests/test_provider_contract.py`, `tests/fixture_sqlalchemy/` |
| Laravel Eloquent association parser | `src/erdscope/frameworks/laravel.py` | `tests/test_erd.py`, `tests/test_provider_contract.py`, `tests/fixture_laravel/` |

Detection is priority ordered; the first overlay whose `detect()` succeeds owns an untyped `--models` path, and the registry exposes each overlay as a typed `<overlay>.models` source. Marker-based overlays use shallow root-level detection, while content-based SQLAlchemy detection recurses to the same depth as its builder. Runtime plugin overlays use the same registry as built-ins.

## Viewer and exporters

| Concern | Source | Verification |
|---|---|---|
| Interactive HTML/CSS/JS and layout interactions | `src/erdscope/viewer.html` | `tests/test_e2e.py`, `docs/gen_shots.py` |
| Excel generation and HTML sentinel | `src/erdscope/exporters.py` | `tests/test_erd.py`, optional `openpyxl` round trip |
| Demo HTML | `docs/gen_demo.py` → `docs/index.html` | CI deterministic diff check |
| User manuals | `docs/manual.html`, `docs/manual.ja.html` | Manual review; README links |
| Excel styling example | `excel-template.xlsx`, `gen_excel_template.py` | Excel-related unit tests |

Viewer changes normally require rebuilding `erd.py` and regenerating `docs/index.html`. Screenshot PNGs are rendering-dependent and smoke-tested rather than byte-diffed.

## Quality, operations, and release

| Concern | Source |
|---|---|
| Main CI and real-DB services | `.github/workflows/ci.yml` |
| Tag-driven PyPI trusted publishing | `.github/workflows/release.yml` |
| Scheduled OpenWiki refresh | `.github/workflows/openwiki-update.yml` |
| Unit/characterization/pipeline suites | `tests/test_*.py` |
| Browser E2E | `tests/test_e2e.py` |
| Synthetic large-schema generation and timing | `benchmarks/gen_schema.py`, `benchmarks/run_bench.py` |
| Release history | `CHANGELOG.md`, recent `git log` |

## Existing documentation hierarchy

- `README.md`: concise user installation, usage, dependencies, feature, performance, and test guidance.
- `docs/manual.html` and `docs/manual.ja.html`: detailed end-user reference and troubleshooting.
- `examples/README.md`: committed SQLite demo workflow.
- `CHANGELOG.md`: release-level behavior changes and rationale.
- `openwiki/`: engineer-oriented synthesis and navigation. `openwiki/INSTRUCTIONS.md` is user-authored control metadata and should not be regenerated.

Avoid duplicating the full CLI/manual here; link to the existing user docs and keep this wiki focused on engineering decisions and safe change paths.
