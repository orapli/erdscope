# Review showcase

This directory provides reviewable, committed examples for three input paths
describing the same publishing domain:

- `input/showcase.db`: real SQLite schema;
- `input/schema.json`: config-only schema;
- `input/models.py`: statically parsed SQLAlchemy model code (SQLAlchemy need
  not be installed);
- `input/presentation.json`: shared title, design notes, and visual group for
  the SQLite/model runs.

Provider-native differences remain visible: SQLite/config expose `post_tags`
as a physical join table, while SQLAlchemy represents the same relationship as
many-to-many metadata through `secondary=post_tags`.

Each subdirectory under `output/` contains the actual CLI results:

- self-contained interactive `diagram.html`;
- `definitions.xlsx` with overview, table, Notes, and Groups sheets;
- canonical `schema.json` snapshot;
- agent-readable `digest.md`;
- `schema.dbml`, `schema.mmd`, and `schema.puml` projections.

Regenerate after behavior or output changes:

```bash
python3 tools/build_single_file.py
python3 examples/showcase/generate.py
```

Verify without modifying files:

```bash
python3 examples/showcase/generate.py --check
```

The unittest suite and CI run `--check`; stale or missing outputs fail the
build. Generated outputs are compared byte-for-byte, including deterministic
XLSX ZIP metadata. The SQLite input is compared by its schema signature because
SQLite file-container bytes vary across SQLite versions and operating systems.
