#!/usr/bin/env python3
"""(Re)build the sample SQLite database used in examples/README.md.

Creates examples/demo_shop.db from scratch: the same small e-commerce schema as
the hosted demo (docs/gen_demo.py), but as a REAL SQLite database with actual
foreign keys, unique constraints, and indexes — so `python3 erd.py
sqlite:///examples/demo_shop.db` reads a live schema and reproduces a diagram
just like the online demo. Committed to the repo so you can try it without
building anything; run this script to regenerate it.

    python3 examples/build_demo_db.py

The schema itself (DEMO_SCHEMA_SQL) and the build logic (build_demo_db) live in
erd.py now, not here — they're also what `erdscope demo` uses to generate a
throwaway copy on the fly for a `pip install`ed user with no local checkout
(pyproject.toml ships this project as the single erd.py module, so this
examples/ directory isn't part of the wheel). This script just points that
shared function at examples/demo_shop.db.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = Path(__file__).resolve().parent / 'demo_shop.db'

sys.path.insert(0, str(ROOT))
from erd import DEMO_SCHEMA_SQL, build_demo_db  # noqa: E402,F401 (path setup above;
                                                  # DEMO_SCHEMA_SQL re-exported for
                                                  # anyone importing this module directly)


def build():
    build_demo_db(DB)
    print(f'Wrote {DB}')


if __name__ == '__main__':
    build()
