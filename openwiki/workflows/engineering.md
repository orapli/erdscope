# Engineering workflows

## Generate a diagram

The normal product workflow is implemented by `src/erdscope/cli.py`:

1. Resolve CLI/config and optional plugins.
2. Fetch zero or one database layer.
3. Parse zero or more model sources in order.
4. apply config schema and manual relation layers.
5. Merge, reconcile, validate, optionally infer, and filter.
6. Write self-contained HTML and optional XLSX.

Useful smoke commands:

```bash
python3 erd.py demo --no-open
python3 erd.py sqlite:///examples/demo_shop.db -o /tmp/shop.html
python3 erd.py --config erdscope.example.yml -o /tmp/config.html
```

The last command uses the example config as a schema source; YAML requires PyYAML.

## Change Python behavior

1. Find the owning split fragment using [Source map](../architecture/source-map.md).
2. Edit only `src/erdscope/`, never `erd.py`.
3. Add the narrowest unit/pipeline test.
4. Run focused tests, then the full suite.
5. Regenerate and verify the committed artifact:

```bash
python3 tools/build_single_file.py
python3 tools/build_single_file.py --check
```

Because fragments form one flat module, verify symbols are defined before use in `tools/build_single_file.py::MODULES` order. Files placed in `db/` or `frameworks/` are auto-included.

## Add or modify a database adapter

Built-in adapters subclass `DBAdapter` and use `@register_adapter` (`src/erdscope/db/base.py`). Define schemes, provider name/label, and `fetch(url) -> tables IR`.

For a built-in engine:

1. Add or edit `src/erdscope/db/<engine>.py`.
2. Normalize engine metadata to the shared IR, preserving deterministic table/column/index ordering.
3. Keep connection locations password-free in ProviderResult metadata.
4. Test URL parsing, catalog edge cases, PK/index/FK extraction, unique-FK `has_one`, and missing dependency behavior.
5. If a driver and CLI fallback exist, prove byte-identical output using real integration coverage.
6. Rebuild `erd.py`.

For an external runtime integration, a trusted plugin can import `DBAdapter`/`register_adapter` from `erd` and register via `--adapter` or config `adapters`. Plugin imports execute arbitrary code with process privileges; do not load untrusted paths.

## Add or modify a framework overlay

Subclass `FrameworkOverlay`, assign a unique `name` and detection `priority`, implement `detect(root)` and `build(root, table_map)`, then decorate with `@register_overlay` (`src/erdscope/frameworks/base.py`).

- Detection is first-match by `(priority, name)`.
- Framework fragments have lower physical authority than DB but higher logical authority.
- Multiple `--models` sources merge in order.
- Static parsing cannot resolve every dynamic framework feature; expose actionable warnings or allow config/table-map correction rather than guessing silently.

Add representative fixture projects and pipeline tests. Confirm framework-only usage works without a DB and that DB physical columns remain authoritative.

## Change merge or config behavior

Read [Schema merge domain](../domain/schema-merge.md) first. Changes usually span:

- contract/normalization: `ir.py` or `header.py`;
- merge identity/authority/reconciliation: `merge.py`;
- config syntax: `config.py`;
- semantic validation/provider construction: `providers.py`;
- output compatibility: `cli.py:serialize_for_viewer()`.

Test both direct merge behavior and the final serialized HTML payload. Preserve first-seen ordering, sparse-field semantics, and two-phase validation.

## Change the viewer or exports

### Viewer

Edit `src/erdscope/viewer.html`, not the inlined copy in `erd.py` or generated `docs/index.html`.

```bash
python3 -m unittest tests.test_e2e -v
python3 tools/build_single_file.py
python3 docs/gen_demo.py
python3 docs/gen_shots.py
```

Then verify `python3 tools/build_single_file.py --check` and inspect `git diff`. `docs/index.html` is deterministic and must be committed when changed. Screenshots are rendering-dependent; CI smoke-tests generation but does not require byte identity. Avoid `"""` in viewer source because the builder embeds it in a raw triple-double-quoted string.

### Excel

Edit `src/erdscope/exporters.py`. Keep stdlib-only generation and the five-cell style-template contract used by `excel-template.xlsx`. Run Excel tests with optional `openpyxl` installed for round-trip validation.

## Change the demo and examples

`erdscope demo` uses embedded SQL from `src/erdscope/demo.py`, creates a temp SQLite database, disables config discovery, and runs the normal pipeline. This exists because pip packages only `erd.py`; installed code cannot rely on `examples/demo_shop.db` being present.

When changing sample schema/content:

1. Update the canonical embedded/sample-building logic as appropriate.
2. Rebuild `examples/demo_shop.db` with `python3 examples/build_demo_db.py`.
3. Run `tests/test_demo.py`.
4. Regenerate `docs/index.html` and screenshots.
5. Confirm demo flags such as filters and Excel still use normal pipeline behavior.

## Documentation workflow

- User command/reference changes belong in `README.md` and both manuals where relevant.
- `docs/index.html` is generated by `docs/gen_demo.py`.
- Keep English/Japanese manuals aligned for user-visible features.
- This wiki is an engineering map; link to manuals rather than cloning their complete CLI reference.

## Release workflow

The current `.github/workflows/release.yml` triggers on `v*`, builds wheel/sdist, and publishes to PyPI through trusted publishing/OIDC. It does not independently run the full CI matrix, so treat successful pre-tag verification as mandatory.

Recommended sequence:

```bash
python3 -m unittest discover -s tests -v
python3 tools/build_single_file.py --check
python3 docs/gen_demo.py
git diff --exit-code docs/index.html
python3 docs/gen_shots.py
python -m build
```

Also check that `pyproject.toml` version, `CHANGELOG.md`, and tag agree, and that generated `erd.py` is committed. See [Operations and testing](../operations-and-testing.md) for optional dependencies and real-DB checks.
