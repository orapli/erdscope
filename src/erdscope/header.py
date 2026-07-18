#!/usr/bin/env python3
"""
erdscope — interactive ER diagrams (and Excel table definitions) from a
MySQL / PostgreSQL / SQLite database, application code, or a config schema
Usage:
    python3 erd.py mysql://readonly@host:3306/dbname [-o erd.html]
    python3 erd.py postgres://readonly@host:5432/dbname[?schema=name]
    python3 erd.py sqlite:///path/to/app.db [--excel defs.xlsx]
    python3 erd.py --models /path/to/app     # code only — no database needed
    python3 erd.py demo                      # bundled sample, no DB of your own

Any one of three sources is enough: a database, application code
(--models: Rails / Prisma / Django, auto-detected), or a config `tables:`
schema. They merge as layered providers — database -> code -> config.

Intermediate representation (IR) — everything downstream (HTML/JS, exports)
consumes this shape:
    tables = {
      "table_name": {
        "primary_key": "id" | None,
        "comment"?: str,
        "schema_missing"?: bool,        # model exists but no DB table
        "columns": [{"name","type","nullable","primary",
                     "sql_type"?, "default"?, "extra"?, "comment"?}],
        "indexes": [{"name","columns":[...],"unique":bool}],
        "associations": [{"type": has_many|belongs_to|has_one|has_and_belongs_to_many,
                          "name", "target",
                          "through"?, "foreign_key"?, "polymorphic"?,
                          "db_fk"?, "inferred"?, "manual"?, "schema_fk"?}],
      }
    }

Provider / provenance contracts — live in the pipeline. Parsers return
ProviderResult; the merge step folds layered providers (db -> framework ->
config); association provenance is a representative `provenance` string plus
a `sources` set, converted back to the legacy boolean flags only at
serialization time, so the DATA_JSON output stays byte-identical.

    # A per-table "fragment": every field optional. An absent key means "this
    # provider didn't supply it — keep the lower layer's value"; an explicit
    # null/[]/"" means "overwrite with empty" (Config only). Table name is the
    # map key, so it isn't repeated inside the fragment.
    ColumnIR = {"name": str,                        # required — identity key
                "type"?: str, "sql_type"?: str, "nullable"?: bool,
                "primary"?: bool,                   # composite PKs: several cols true
                "default"?: str, "extra"?: str, "comment"?: str|None}
    IndexIR  = {"name"?: str, "columns": list[str], "unique"?: bool}
    AssociationFragment = {"type": ..., "name": str, "target": str,
                           "foreign_key"?: str,     # single-column FK only (§4.2)
                           "through"?: str, "polymorphic"?: bool}
    TableFragment = {"comment"?: str|None,
                     "primary_key"?: str|list[str]|None,   # list = composite PK
                     "columns"?: [ColumnIR], "indexes"?: [IndexIR],
                     "associations"?: [AssociationFragment]}
    IR = dict[table_name, TableFragment]

    # Each parser is (moving toward) "parse only, no merging" and returns:
    Source         = {"kind": "db"|"framework"|"config"|"schema",
                      "provider": "mysql"|"postgres"|"rails"|"prisma"|"django"|"config"
                                 |"rails.schema",
                      "location"?: str}            # url (no password) / dir / config path
    Warning        = {"code": str, "message": str, "table"?: str}
    ProviderResult = {"source": Source, "tables": IR, "warnings": list[Warning]}

    # Association provenance (§9). Internally it is a representative
    # `provenance` string plus a `sources` set; on the way to HTML/Excel it is
    # converted back to the legacy booleans the viewer/Excel already read, so
    # the serialized output is byte-identical. The representative precedence at
    # merge time is manual > declared > db_fk > inferred (hand-declared beats
    # code, code beats a raw DB constraint, all beat a guess).
    provenance = "declared" | "db_fk" | "manual" | "inferred"
    #   DB FK constraint -> db_fk ; framework declaration -> declared ;
    #   config relations/associations -> manual ; --infer-fk post-pass -> inferred
    #   legacy flag mapping: db_fk/inferred/manual -> {<flag>: True}; declared -> {}
"""
import abc, ast, copy, getpass, hashlib, os, subprocess, sys, json, re, argparse
from fnmatch import fnmatch
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Provider / provenance contracts
# ---------------------------------------------------------------------------
# Parsers and merge route through these (providers.py / merge.py); serialization
# converts provenance back to legacy flags in cli.py. No side effects; dicts
# only — the single-file, zero-dependency shape is preserved.

# Representative provenance -> the legacy boolean flag(s) the HTML/Excel
# serializers already read. 'declared' carries no flag (bare = code-declared).
