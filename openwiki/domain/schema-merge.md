# Schema merge domain

The schema merge is the product’s central business rule: heterogeneous evidence must become one deterministic diagram without letting a weaker source overwrite a stronger kind of fact.

## Sources and ordering

`src/erdscope/cli.py:_run_pipeline()` constructs layers from low to high specification priority:

1. Database provider, if a URL exists.
2. Framework providers for each `--models`/config `models` path, in supplied order.
3. Config `tables:` provider.
4. Config `relations:` provider.

Any of the first three source categories is sufficient by itself. Relations are patches and need a base schema. Later equal-priority framework/plugin layers win ties, with conflict warnings where merge-visible values disagree.

## Sparse fragments

Providers return partial table fragments. Presence is semantic:

- Missing key: this provider has no opinion; preserve lower-layer evidence.
- Explicit config empty/null: clear the lower-layer value where the contract permits it.
- Stable identities: table map key, column name, index name (or lower-level unnamed column tuple), and association identity.

`merge_ir()` deep-copies input and preserves deterministic first-seen ordering. Avoid iterating unordered sets when producing output.

## Authority ladders

Field authority is defined in `src/erdscope/merge.py`.

| Data | Winning order |
|---|---|
| Column `type`, `sql_type`, `nullable`, `primary`, `default`, `extra` | config > database > framework |
| Primary key and indexes | config > database > framework |
| Table/column comments and association names | config > framework > database |
| Equal-rank conflicts | later layer wins; differing top-rank values warn |

The practical intent is that the database owns physical truth, application code owns semantics the database cannot express, and config is an explicit operator correction with final authority.

## Association identity and reconciliation

`association_key(source_table, association)` includes:

- source and target tables;
- normalized FK set;
- semantic role (`owner_fk`, collection, inverse one, or named);
- association name;
- `through` and polymorphic markers when present.

Name is intentionally part of identity so aliases such as Rails `user` and `author` can coexist on the same `user_id`. Conventional DB names can still merge with code declarations; renamed declarations remain distinct through Phase A.

After identity merge, `reconcile_db_fks()` removes raw DB edges already covered by explicit framework/config semantics. Matching is FK-aware where possible. A unique single-column DB FK’s one-to-one signal is preserved by promoting a covering owner association to `has_one` rather than losing cardinality.

Polymorphic and through associations remain semantic constructs; viewer behavior may suppress or render them differently from ordinary direct edges.

## Provenance

Internally, merged associations carry:

- representative `provenance`: `manual`, `declared`, `db_fk`, or `inferred`;
- deduplicated `sources`: contributing `{kind, provider}` entries.

Representative precedence is:

```text
manual > declared > db_fk > inferred
```

Config associations and config relations are manual by definition. `--infer-fk` associations are created after merge with no provider source. At output, `serialize_for_viewer()` removes structured provenance and maps it to legacy flags:

- `manual` → `manual: true`
- `db_fk` → `db_fk: true`
- `inferred` → `inferred: true`
- `declared` → no flag

Changing this boundary requires coordinated viewer, Excel, characterization, and pipeline changes.

## Config schema operations

Config can declare a complete schema or patch lower layers (`src/erdscope/config.py`, `src/erdscope/providers.py`). Supported table-level behavior includes:

- add or override tables, columns, indexes, comments, PKs, and associations;
- drop a whole table;
- drop columns, named indexes, or matching associations;
- set `columns_mode`, `indexes_mode`, or `associations_mode` to `replace` instead of the default merge.

Config primary keys may be composite. Config foreign keys/associations are currently single-column. Config indexes must be named so drops and overrides have stable identity.

### Two-phase validation

1. **Syntactic at load:** top-level and nested allowlists, required keys, scalar/list types, valid modes, duplicate identities, and prohibited password/full-URL fields.
2. **Semantic during pipeline:** drops must identify real lower-layer items; final PK columns, association source FKs, and targets must resolve after all config additions are merged.

This split allows config to refer to items created by another source or by the same final config layer while still rejecting misspellings.

## Config and CLI precedence

Mirrorable argparse defaults are suppressed so the pipeline can distinguish an explicit CLI value from absence. Explicit CLI values beat config; otherwise config beats built-in defaults. Config discovery order is `.erdscope.json`, `.erdscope.yml`, `.erdscope.yaml`. YAML requires optional PyYAML; JSON remains dependency-free.

Connection config accepts separate `engine`, `host`, `port`, `user`, and `database` fields, deliberately not passwords or full URLs. SQLite uses `engine: sqlite` plus a local database path and rejects irrelevant server fields and URL-sensitive path characters.

## FK inference and derived fields

`--infer-fk` is off by default. `infer_fk_associations()` examines non-primary `*_id` columns, tries plural and singular table targets, and skips self, unknown, or already-related pairs. A unique single-column index promotes the inferred relation to `has_one`; otherwise it is `belongs_to`.

After final associations, `fk_columns` is recomputed from actual association `foreign_key` values. The viewer does not infer FK badges from names. Tables with no columns receive `schema_missing: true`, which is common for association-only Rails models without a DB table.

## Invariants to protect

- Merge results are pure, deterministic, and do not retain config operation markers.
- DB physical facts survive framework disagreement unless config explicitly overrides them.
- Alias associations are not accidentally deduplicated.
- One-to-one signals are never downgraded during merge/reconciliation.
- Config typos fail loudly.
- Internal provenance does not leak into current output JSON.
- `fk_columns` reflects final associations only.

## Focused verification

```bash
python3 -m unittest tests.test_merge_ir -v
python3 -m unittest tests.test_config_validation -v
python3 -m unittest tests.test_pipeline -v
python3 -m unittest tests.test_characterization -v
python3 tools/build_single_file.py --check
```

Use `tests/test_erd.py` for parser and legacy behavior coverage. Add end-to-end pipeline cases when a merge rule affects serialized viewer data, not just `merge_ir()` internals.
