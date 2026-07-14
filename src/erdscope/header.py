#!/usr/bin/env python3
"""
erdscope — interactive ER diagrams (and Excel table definitions) from a live database
Usage:
    python3 erd.py mysql://readonly@host:3306/dbname [-o erd.html]
    python3 erd.py postgres://readonly@host:5432/dbname[?schema=name]
                   [--models /path/to/app] [--excel defs.xlsx]

The database is the required source of truth; --models optionally overlays
association semantics parsed from application code (Rails / Prisma / Django,
auto-detected).

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
                          "db_fk"?, "inferred"?, "manual"?}],
      }
    }

Refactor contracts (REFACTOR_PLAN.md §4/§5/§9) — being introduced incrementally.
The types below are the TARGET shape the providers and merge step are moving
toward. NOTE: as of this step the parsers still return the plain `tables` IR
above (not ProviderResult), the pipeline still carries the legacy boolean
provenance flags on each association, and the serialized DATA_JSON is unchanged.
`make_provider_result`, `provenance_of`, and `legacy_flags_for` (below the
imports) are the pure scaffolding/seam for the later steps that wire this in;
they are not yet used by the main path.

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
    Source         = {"kind": "db"|"framework"|"config",
                      "provider": "mysql"|"postgres"|"rails"|"prisma"|"django"|"config",
                      "location"?: str}            # url (no password) / dir / config path
    Warning        = {"code": str, "message": str, "table"?: str}
    ProviderResult = {"source": Source, "tables": IR, "warnings": list[Warning]}

    # Association provenance (§9). Internally the target is a representative
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
import abc, ast, copy, getpass, os, subprocess, sys, json, re, argparse
from fnmatch import fnmatch
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Provider / provenance contracts (REFACTOR_PLAN.md §5/§9)
# ---------------------------------------------------------------------------
# Pure scaffolding + conversion seam for the staged refactor. These are the
# contract in code form; they have no side effects and are NOT yet used by the
# main pipeline (which still returns plain IR and carries the legacy boolean
# provenance flags). Later steps (§15 Step 4-6, 9-10) route parsers and merge
# through them. Kept small and dependency-free — no dataclasses, just dicts —
# so the single-file, zero-dependency shape is preserved.

# Representative provenance -> the legacy boolean flag(s) the HTML/Excel
# serializers already read. 'declared' carries no flag (bare = code-declared).
