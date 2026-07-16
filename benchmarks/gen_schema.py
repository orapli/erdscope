#!/usr/bin/env python3
"""Generate a synthetic SQLite database for erdscope performance benchmarking.

Produces `--tables N` tables named table_0001..table_NNNN, each with a
primary key, 6 data columns, and a handful of foreign keys to earlier
tables (so the graph is a DAG — no cycles, nothing to untangle at parse
time that a real schema wouldn't also have).

Only the schema is written — no rows are inserted. erdscope reads catalog
metadata (PRAGMA table_info / foreign_key_list / index_list), never table
contents, so row data would add generation cost without exercising
anything the benchmark cares about.

Usage:
    python3 benchmarks/gen_schema.py --tables 300 --out /tmp/bench_300.db

Deterministic: the FK graph is built from a `random.seed(42)`-seeded
generator, so the same --tables value always produces the same schema
(same table/column/FK count), which keeps benchmark runs comparable.
"""
import argparse
import random
import sqlite3
from pathlib import Path

DATA_COLUMNS = [
    ('name', 'TEXT'),
    ('status', 'INTEGER'),
    ('amount', 'REAL'),
    ('note', 'TEXT'),
    ('created_at', 'TEXT'),
    ('updated_at', 'TEXT'),
]


def table_name(i, width):
    return f'table_{i:0{width}d}'


def build_schema(n_tables, seed=42):
    """Return (table_names, ddl_statements, edge_count) for an n_tables
    schema. Pure/deterministic given (n_tables, seed) — no I/O.

    FK density: table i (1-indexed, i >= 2) picks 1-3 distinct targets at
    random from tables 1..i-1 and gets one `<target>_id` FK column per
    target, each backed by a real `FOREIGN KEY (...) REFERENCES ...`
    constraint. Table 1 has no FKs (nothing earlier to reference). This
    averages ~2 FK edges per table (mean of randint(1,3) is 2), matching
    the "tables * ~2 edges" target used to size the benchmark schemas.
    """
    rng = random.Random(seed)
    width = max(4, len(str(n_tables)))
    names = [table_name(i, width) for i in range(1, n_tables + 1)]
    ddl = []
    edge_count = 0
    for i, name in enumerate(names, start=1):
        cols = ['    id INTEGER PRIMARY KEY']
        cols += [f'    {cname} {ctype}' for cname, ctype in DATA_COLUMNS]
        fk_constraints = []
        if i >= 2:
            k = rng.randint(1, min(3, i - 1))
            targets = rng.sample(names[:i - 1], k)
            for target in targets:
                fk_col = f'{target}_id'
                cols.append(f'    {fk_col} INTEGER')
                fk_constraints.append(
                    f'    FOREIGN KEY ({fk_col}) REFERENCES {target}(id)')
                edge_count += 1
        body = ',\n'.join(cols + fk_constraints)
        ddl.append(f'CREATE TABLE {name} (\n{body}\n);')
    return names, ddl, edge_count


def generate(n_tables, out_path, seed=42):
    """Write the generated schema to a fresh SQLite file at out_path.
    Overwrites any existing file. Returns a small stats dict."""
    out_path = Path(out_path)
    if out_path.exists():
        out_path.unlink()
    names, ddl, edge_count = build_schema(n_tables, seed=seed)
    conn = sqlite3.connect(str(out_path))
    try:
        with conn:
            for stmt in ddl:
                conn.execute(stmt)
    finally:
        conn.close()
    return {'tables': len(names), 'edges': edge_count, 'path': str(out_path)}


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('--tables', type=int, required=True,
                    help='Number of tables to generate')
    p.add_argument('--out', required=True,
                    help='Output SQLite file path (overwritten if it exists)')
    p.add_argument('--seed', type=int, default=42,
                    help='Random seed for the FK graph (default: 42, fixed '
                         'for reproducibility — only override to explore '
                         'other densities)')
    args = p.parse_args()
    stats = generate(args.tables, args.out, seed=args.seed)
    print(f"Generated {stats['tables']} tables, {stats['edges']} FK edges "
          f"-> {stats['path']}")


if __name__ == '__main__':
    main()
