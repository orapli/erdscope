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
import ast, copy, getpass, os, subprocess, sys, json, re, argparse
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
_PROVENANCE_TO_FLAG = {'db_fk': 'db_fk', 'manual': 'manual', 'inferred': 'inferred'}


def make_provider_result(kind, provider, tables, location=None, warnings=None):
    """Build a ProviderResult dict (§5): a Source describing where this IR came
    from, the parsed `tables` IR itself, and any non-fatal warnings. Pure — it
    only assembles the dict, never mutates `tables`. `location` (a
    password-free URL / directory / config path) is omitted from Source when
    None; `warnings` defaults to an empty list."""
    source = {'kind': kind, 'provider': provider}
    if location is not None:
        source['location'] = location
    return {'source': source, 'tables': tables,
            'warnings': list(warnings) if warnings else []}


def provenance_of(assoc):
    """Representative provenance (§9.1) for an association dict, derived from
    the legacy boolean flags it currently carries. Precedence when several
    coexist: manual > db_fk > inferred; a bare association (no flag) is
    'declared'. This is the read half of the legacy<->provenance seam."""
    if assoc.get('manual'):
        return 'manual'
    if assoc.get('db_fk'):
        return 'db_fk'
    if assoc.get('inferred'):
        return 'inferred'
    return 'declared'


def legacy_flags_for(provenance):
    """The legacy boolean flag dict to serialize for a given representative
    provenance (§9.3): db_fk/manual/inferred -> {<flag>: True}, and 'declared'
    -> {} (no badge). The write half of the seam; round-trips with
    provenance_of for each of the four provenances."""
    flag = _PROVENANCE_TO_FLAG.get(provenance)
    return {flag: True} if flag else {}


def _assoc_provenance(assoc):
    """Provenance of an association regardless of which shape it carries: the
    merged IR stores a structured `provenance` string (Step 10); a legacy-shape
    association (parser output, or a synthetic test fixture) stores boolean
    flags. Prefer the explicit provenance, else derive it from the flags. Lets
    the internals (reconcile_db_fks) treat both shapes uniformly."""
    return assoc.get('provenance') or provenance_of(assoc)

# ---------------------------------------------------------------------------
# SQL type shorthand (information_schema DATA_TYPE -> display type)
# ---------------------------------------------------------------------------
SQL_TYPES = {
    'character varying': 'string', 'varchar': 'string', 'char': 'string',
    'timestamp without time zone': 'datetime', 'timestamp with time zone': 'datetime',
    'timestamp': 'datetime', 'datetime': 'datetime',
    'time without time zone': 'time', 'time': 'time', 'date': 'date',
    'double precision': 'float', 'float': 'float', 'real': 'float',
    'numeric': 'decimal', 'decimal': 'decimal',
    'bigint': 'bigint', 'bigserial': 'bigint', 'integer': 'integer', 'int': 'integer',
    'serial': 'integer', 'smallint': 'integer', 'tinyint': 'integer', 'mediumint': 'integer',
    'text': 'text', 'longtext': 'text', 'mediumtext': 'text',
    'boolean': 'boolean', 'jsonb': 'jsonb', 'json': 'json', 'uuid': 'uuid',
    'bytea': 'binary', 'blob': 'binary', 'longblob': 'binary', 'inet': 'inet',
}

# ---------------------------------------------------------------------------
# MySQL adapter (information_schema via PyMySQL or the mysql CLI)
# ---------------------------------------------------------------------------
def mysql_query_rows(url, sql):
    """Run a query and return rows as tuples of strings ('' for NULL).
    Prefers PyMySQL when installed; otherwise shells out to the mysql CLI,
    so the tool itself stays dependency-free."""
    u = urlparse(url)
    host, port, db = u.hostname or '127.0.0.1', u.port or 3306, u.path.lstrip('/')
    try:
        import pymysql
    except ImportError:
        pymysql = None
    if pymysql:
        try:
            conn = pymysql.connect(host=host, port=port, user=u.username,
                                   password=u.password or os.environ.get('MYSQL_PWD', ''),
                                   database=db, charset='utf8mb4')
        except pymysql.err.MySQLError as e:
            # wrong host/password/db is the single most common daily failure
            # here — a raw pymysql traceback is far less useful than the
            # clean one-liner the mysql-CLI path below already gives
            sys.exit(f'Error: could not connect to MySQL at {host}:{port}: {e}')
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                return [tuple('' if v is None else str(v) for v in r)
                        for r in cur.fetchall()]
        except pymysql.err.MySQLError as e:
            sys.exit(f'Error: mysql query failed: {e}')
        finally:
            conn.close()
    # No --raw: table/column comments are free-text and commonly contain
    # tabs or newlines, which --raw's disabled escaping would otherwise
    # leave as literal bytes in the tab-separated stream — corrupting the
    # field count on split('\t') and splitting records early on splitlines().
    # Without --raw, mysql escapes \0 \b \n \r \t \\ and represents NULL as
    # the literal two-char marker \N, all unescaped below.
    cmd = ['mysql', '--batch', '--skip-column-names',
           '--default-character-set=utf8mb4', '-h', host, '-P', str(port)]
    if u.username:
        cmd += ['-u', u.username]
    cmd += [db, '-e', sql]
    env = dict(os.environ)
    if u.password:
        env['MYSQL_PWD'] = u.password  # keep the password off the argv
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    except FileNotFoundError:
        sys.exit('Error: neither PyMySQL nor the mysql CLI is available '
                 '(pip install pymysql, or install a MySQL client)')
    if r.returncode != 0:
        sys.exit(f'Error: mysql query failed: {r.stderr.strip()}')
    return [tuple(_unescape_mysql_field(v) for v in line.split('\t'))
            for line in r.stdout.splitlines()]

_MYSQL_TSV_ESCAPES = {'0': '\0', 'b': '\b', 'n': '\n', 'r': '\r', 't': '\t', '\\': '\\'}
# COPY TO STDOUT (the psql CLI path) escapes the same characters plus \f \v
_COPY_TSV_ESCAPES = {**_MYSQL_TSV_ESCAPES, 'f': '\f', 'v': '\v'}

def _unescape_tsv_field(s, escapes):
    """Undo tab-separated-output escaping (mysql --batch, or COPY TO STDOUT
    text format — they share the same core contract). \\N alone means SQL
    NULL (mapped to '' — same convention the driver paths already use for
    None); elsewhere the entries of `escapes` decode to their literal
    characters. A single left-to-right pass (not chained .replace() calls)
    is required: independent replacements can misfire on adjacent escapes,
    e.g. an escaped backslash immediately followed by an escaped tab."""
    if s == '\\N':
        return ''
    if '\\' not in s:
        return s
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c == '\\' and i + 1 < n and s[i + 1] in escapes:
            out.append(escapes[s[i + 1]])
            i += 2
        else:
            out.append(c)
            i += 1
    return ''.join(out)

def _unescape_mysql_field(s):
    return _unescape_tsv_field(s, _MYSQL_TSV_ESCAPES)

def _unescape_copy_field(s):
    return _unescape_tsv_field(s, _COPY_TSV_ESCAPES)

def mysql_ir(table_rows, col_rows, fk_rows, index_rows):
    """Build the IR from information_schema rows (pure; unit-testable).

    table_rows: (TABLE_NAME, TABLE_COMMENT)
    col_rows:   (TABLE_NAME, COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, IS_NULLABLE,
                 COLUMN_KEY, COLUMN_DEFAULT, EXTRA, COLUMN_COMMENT)
    fk_rows:    (TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME)
    index_rows: (TABLE_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME)
    """
    tables = {}
    for tname, tcomment in table_rows:
        tables[tname] = {'columns': [], 'associations': [], 'indexes': [],
                         'primary_key': None}
        if tcomment:
            tables[tname]['comment'] = tcomment
    for tname, col, dtype, ctype, is_null, key, default, extra, comment in col_rows:
        t = tables.get(tname)
        if t is None:
            continue  # views etc. — not in table_rows
        c = {'name': col, 'type': SQL_TYPES.get(dtype.lower(), dtype.lower()),
             'sql_type': ctype, 'nullable': is_null.upper() == 'YES',
             'primary': key == 'PRI'}
        if default:
            c['default'] = default
        if extra:
            c['extra'] = extra
        if comment:
            c['comment'] = comment
        if key == 'PRI' and t['primary_key'] is None:
            t['primary_key'] = col
        t['columns'].append(c)
    idx = {}
    for tname, iname, non_unique, seq, col in index_rows:
        if tname not in tables:
            continue
        key = (tname, iname)
        e = idx.setdefault(key, {'name': iname, 'columns': [],
                                 'unique': str(non_unique) in ('0', 'False')})
        e['columns'].append((int(seq), col))
    for (tname, _), e in sorted(idx.items()):
        e['columns'] = [c for _, c in sorted(e['columns'])]
        tables[tname]['indexes'].append(e)
    # built before fk_rows so DB FKs can tell 1:1 (FK column alone under a
    # unique index) from the default many:1 apart — a naked FK column can
    # repeat, but a uniquely-constrained one can't, by definition
    for tname, col, ref in fk_rows:
        if tname not in tables or ref not in tables:
            continue
        name = col[:-3] if col.endswith('_id') else col
        # has_one here means "this side's column is the FK, but it's 1:1" —
        # same convention parse_django uses for OneToOneField
        assoc_type = 'has_one' if _unique_single_col(tables[tname], col) else 'belongs_to'
        tables[tname]['associations'].append(
            {'type': assoc_type, 'name': name, 'target': ref,
             'foreign_key': col, 'db_fk': True})
    return tables

def _unique_single_col(table, col):
    """True if `col` is, by itself, the entire column list of some unique
    index on `table` — the DB-level signal for "this FK is really 1:1"."""
    return any(ix['unique'] and ix['columns'] == [col] for ix in table['indexes'])

def parse_mysql(url):
    db = urlparse(url).path.lstrip('/')
    if not re.fullmatch(r'\w+', db or ''):
        sys.exit('Error: the mysql URL must include a database name, '
                 'e.g. mysql://readonly@127.0.0.1:3307/myapp_production')
    u = urlparse(url)
    # No password in the URL (which would land in shell history) and none
    # set via MYSQL_PWD: prompt for it instead of silently trying blank auth.
    # Skipped when not interactive (e.g. under a test harness or CI) so the
    # process never blocks waiting on stdin.
    if u.password is None and not os.environ.get('MYSQL_PWD') and sys.stdin.isatty():
        os.environ['MYSQL_PWD'] = getpass.getpass(
            f"MySQL password for {u.username or 'root'}@{u.hostname or '127.0.0.1'}: ")
    table_rows = mysql_query_rows(url,
        f"SELECT TABLE_NAME, TABLE_COMMENT FROM information_schema.TABLES "
        f"WHERE TABLE_SCHEMA='{db}' AND TABLE_TYPE='BASE TABLE'")
    col_rows = mysql_query_rows(url,
        f"SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, IS_NULLABLE, "
        f"COLUMN_KEY, COLUMN_DEFAULT, EXTRA, COLUMN_COMMENT "
        f"FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='{db}' "
        f"ORDER BY TABLE_NAME, ORDINAL_POSITION")
    fk_rows = mysql_query_rows(url,
        f"SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME "
        f"FROM information_schema.KEY_COLUMN_USAGE "
        f"WHERE TABLE_SCHEMA='{db}' AND REFERENCED_TABLE_NAME IS NOT NULL")
    index_rows = mysql_query_rows(url,
        f"SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME "
        f"FROM information_schema.STATISTICS WHERE TABLE_SCHEMA='{db}' "
        f"ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX")
    return mysql_ir(table_rows, col_rows, fk_rows, index_rows)

# ---------------------------------------------------------------------------
# PostgreSQL adapter (pg_catalog via psycopg/psycopg2 or the psql CLI)
# ---------------------------------------------------------------------------
def postgres_query_rows(url, sql):
    """Run a query and return rows as tuples of strings ('' for NULL).
    Prefers psycopg (v3), then psycopg2, when installed; otherwise shells
    out to the psql CLI — same dependency-free spirit as mysql_query_rows.
    The CLI path wraps the query in COPY (...) TO STDOUT, whose text format
    escapes tabs/newlines/backslashes and spells NULL as \\N — the same
    framing contract mysql --batch provides, so free-text comments can't
    corrupt the field/record structure."""
    u = urlparse(url)
    host, port, db = u.hostname or '127.0.0.1', u.port or 5432, u.path.lstrip('/')
    password = u.password or os.environ.get('PGPASSWORD', '')
    driver = None
    try:
        import psycopg as driver  # psycopg 3
    except ImportError:
        try:
            import psycopg2 as driver
        except ImportError:
            pass
    if driver:
        try:
            conn = driver.connect(host=host, port=port, user=u.username,
                                  password=password, dbname=db)
        except driver.Error as e:
            sys.exit(f'Error: could not connect to PostgreSQL at {host}:{port}: {e}')
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                return [tuple('' if v is None else str(v) for v in r)
                        for r in cur.fetchall()]
        except driver.Error as e:
            sys.exit(f'Error: postgres query failed: {e}')
        finally:
            conn.close()
    cmd = ['psql', '-X', '-q', '-v', 'ON_ERROR_STOP=1', '-h', host, '-p', str(port)]
    if u.username:
        cmd += ['-U', u.username]
    cmd += ['-d', db, '-c', f'COPY ({sql}) TO STDOUT']
    env = dict(os.environ)
    if password:
        env['PGPASSWORD'] = password  # keep the password off the argv
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    except FileNotFoundError:
        sys.exit('Error: neither psycopg/psycopg2 nor the psql CLI is available '
                 '(pip install "psycopg[binary]", or install a PostgreSQL client)')
    if r.returncode != 0:
        sys.exit(f'Error: postgres query failed: {r.stderr.strip()}')
    return [tuple(_unescape_copy_field(v) for v in line.split('\t'))
            for line in r.stdout.splitlines()]

def parse_postgres(url):
    """Read the schema from a live PostgreSQL database and build the IR.

    Shapes pg_catalog query results into the exact information_schema row
    layout mysql_ir() consumes, so the (engine-agnostic) IR builder — PK
    detection, unique-index 1:1 promotion, index assembly — is shared, not
    duplicated. Notes vs. MySQL:
    - schema defaults to 'public'; override with ?schema=name in the URL
    - partitioned parents (relkind 'p') are included, their partitions and
      views are not — matching MySQL's BASE TABLE filter in spirit
    - identity/serial columns land in `extra` (like auto_increment), and a
      serial's noisy nextval(...) default is blanked for display parity
    """
    u = urlparse(url)
    db = u.path.lstrip('/')
    if not re.fullmatch(r'\w+', db or ''):
        sys.exit('Error: the postgres URL must include a database name, '
                 'e.g. postgres://readonly@127.0.0.1:5432/myapp_production')
    schema = 'public'
    for part in u.query.split('&'):
        if part.startswith('schema='):
            schema = part[len('schema='):]
    if not re.fullmatch(r'\w+', schema):
        sys.exit(f'Error: invalid schema name {schema!r} in the postgres URL')
    # No password in the URL and none set via PGPASSWORD: prompt (interactive
    # only, same as the MySQL path). An empty answer leaves PGPASSWORD unset
    # so libpq's own ~/.pgpass lookup still applies.
    if u.password is None and not os.environ.get('PGPASSWORD') and sys.stdin.isatty():
        pw = getpass.getpass(
            f"PostgreSQL password for {u.username or 'postgres'}@{u.hostname or '127.0.0.1'}: ")
        if pw:
            os.environ['PGPASSWORD'] = pw
    base = (f"FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE n.nspname = '{schema}' AND c.relkind IN ('r', 'p') "
            f"AND NOT c.relispartition")
    table_rows = postgres_query_rows(url,
        f"SELECT c.relname, COALESCE(obj_description(c.oid, 'pg_class'), '') "
        f"{base} ORDER BY c.relname")
    col_rows = postgres_query_rows(url,
        f"SELECT c.relname, a.attname, "
        f"format_type(a.atttypid, NULL), "
        f"format_type(a.atttypid, a.atttypmod), "
        f"CASE WHEN a.attnotnull THEN 'NO' ELSE 'YES' END, "
        f"CASE WHEN EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid = c.oid "
        f"AND i.indisprimary AND a.attnum = ANY(i.indkey)) THEN 'PRI' ELSE '' END, "
        f"CASE WHEN pg_get_expr(ad.adbin, ad.adrelid) LIKE 'nextval(%' THEN '' "
        f"ELSE COALESCE(pg_get_expr(ad.adbin, ad.adrelid), '') END, "
        f"CASE WHEN a.attidentity <> '' THEN 'identity' "
        f"WHEN pg_get_expr(ad.adbin, ad.adrelid) LIKE 'nextval(%' THEN 'serial' "
        f"ELSE '' END, "
        f"COALESCE(col_description(c.oid, a.attnum), '') "
        f"FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
        f"JOIN pg_namespace n ON n.oid = c.relnamespace "
        f"LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum "
        f"WHERE n.nspname = '{schema}' AND c.relkind IN ('r', 'p') "
        f"AND NOT c.relispartition AND a.attnum > 0 AND NOT a.attisdropped "
        f"ORDER BY c.relname, a.attnum")
    fk_rows = postgres_query_rows(url,
        f"SELECT conrel.relname, a.attname, confrel.relname "
        f"FROM pg_constraint con "
        f"JOIN pg_class conrel ON conrel.oid = con.conrelid "
        f"JOIN pg_class confrel ON confrel.oid = con.confrelid "
        f"JOIN pg_namespace n ON n.oid = conrel.relnamespace "
        f"CROSS JOIN LATERAL unnest(con.conkey) AS k(attnum) "
        f"JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum "
        f"WHERE con.contype = 'f' AND n.nspname = '{schema}' "
        f"ORDER BY conrel.relname, a.attname")
    index_rows = postgres_query_rows(url,
        f"SELECT c.relname, i.relname, "
        f"CASE WHEN ix.indisunique THEN 0 ELSE 1 END, k.ord, "
        f"COALESCE(a.attname, pg_get_indexdef(ix.indexrelid, k.ord::int, true)) "
        f"FROM pg_index ix "
        f"JOIN pg_class c ON c.oid = ix.indrelid "
        f"JOIN pg_class i ON i.oid = ix.indexrelid "
        f"JOIN pg_namespace n ON n.oid = c.relnamespace "
        f"CROSS JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ord) "
        f"LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum "
        f"AND k.attnum > 0 "
        f"WHERE n.nspname = '{schema}' AND c.relkind IN ('r', 'p') "
        f"AND NOT c.relispartition "
        f"ORDER BY c.relname, i.relname, k.ord")
    return mysql_ir(table_rows, col_rows, fk_rows, index_rows)

# ---------------------------------------------------------------------------
# Layered IR merge — Phase A (identity merge) + Phase B (reconcile_db_fks)
# REFACTOR_PLAN.md §7 (field rules), §8 (association identity), §9 (provenance).
#
# The pipeline builds ProviderResult layers (db / framework / config) and folds
# them with merge_ir, whose Phase B reconcile_db_fks subsumes the old
# per-table dedupe pass.
# ---------------------------------------------------------------------------
# Field authority as a numeric rank: among the layers that PROVIDE a field
# (key present — §4: an absent key never participates), pick the value
# maximizing (rank, spec_order); spec_order is the index in `layers`, so a
# later layer wins ties (e.g. multiple frameworks -> last one wins).
_PHYSICAL_RANK = {'config': 3, 'db': 2, 'framework': 1}   # DB is the physical truth
_LOGICAL_RANK = {'config': 3, 'framework': 2, 'db': 1}    # code owns logical names
# Column attributes split by authority kind (§7.2). Everything not physical
# (i.e. `comment`) is logical.
_PHYSICAL_COL_ATTRS = ('type', 'sql_type', 'nullable', 'primary', 'default', 'extra')
# Deterministic column-attribute emit order (str-set iteration is hash-seed
# dependent, so never iterate a set for output order).
_COL_ATTR_ORDER = ('type', 'sql_type', 'nullable', 'primary', 'default', 'extra', 'comment')
# Representative-provenance precedence (§9.1): manual > declared > db_fk > inferred.
_PROV_PRECEDENCE = {'manual': 3, 'declared': 2, 'db_fk': 1, 'inferred': 0}

def _pick_by_authority(contribs):
    """contribs: list of (rank, spec_order, value). Return the value with the
    greatest (rank, spec_order)."""
    return max(contribs, key=lambda c: (c[0], c[1]))[2]

def _assoc_role(a):
    """Identity role for an association (§8.1). owner_fk holds the FK column;
    collection is has_many/habtm without an FK; inverse_one is has_one without
    an FK. A rare belongs_to/other lacking an FK gets its own name-keyed role
    so it's never wrongly merged with a real owner_fk."""
    if a.get('foreign_key'):
        return 'owner_fk'
    if a['type'] in ('has_many', 'has_and_belongs_to_many'):
        return 'collection'
    if a['type'] == 'has_one':
        return 'inverse_one'
    return 'named'

def association_key(source_table, a):
    """Stable identity tuple (§8.1). `name` is part of the identity for EVERY
    role, owner_fk included: the Rails alias pattern (`belongs_to :user` AND
    `belongs_to :author`, both on `user_id`) is two distinct associations that
    must stay separate. A name-blind owner_fk identity would over-merge them.
    The DB-FK-vs-code-name case is handled without it: a DB FK's name is the
    machine-derived column stem (`user_id`->`user`), so a conventional
    `belongs_to :user` matches and merges in Phase A, while a renamed
    `belongs_to :author` stays separate and Phase B reconcile_db_fks drops the
    now-covered DB FK — no double edge either way. A single-column FK is
    normalized to a frozenset, leaving room for future composite FKs (§4.2)."""
    fk = frozenset([a['foreign_key']]) if a.get('foreign_key') else frozenset()
    key = [source_table, a['target'], fk, _assoc_role(a), a['name']]
    if a.get('through'):
        key.append(('through', a['through']))
    if a.get('polymorphic'):
        key.append(('polymorphic', True))
    return tuple(key)

def _merge_column(name, contribs):
    """contribs: list of (kind, spec_order, column_dict) for one column name,
    in layer order. Physical attrs resolve Config>DB>Framework, `comment`
    resolves Config>Framework>DB, present-only (§7.2)."""
    out = {'name': name}
    present = set()
    for _, _, c in contribs:
        present |= set(c)
    present.discard('name')
    present.discard('drop')  # config op marker, never a data attribute
    ordered = [k for k in _COL_ATTR_ORDER if k in present]
    ordered += sorted(present - set(_COL_ATTR_ORDER))
    for attr in ordered:
        rank_map = _PHYSICAL_RANK if attr in _PHYSICAL_COL_ATTRS else _LOGICAL_RANK
        cc = [(rank_map[kind], order, c[attr]) for kind, order, c in contribs if attr in c]
        if cc:
            out[attr] = _pick_by_authority(cc)
    return out

def _assoc_content_differs(a, b):
    """Do two same-identity associations differ in merge-visible content
    (cardinality / target / through / polymorphic)? A name difference is
    expected (declared vs machine-derived) and never counts. Shared by the
    config-override (§8.6) and framework-vs-framework (§10) override warnings."""
    return (a['type'] != b['type'] or a['target'] != b['target']
            or a.get('through') != b.get('through')
            or bool(a.get('polymorphic')) != bool(b.get('polymorphic')))

def _merge_association_group(members, layer_sources):
    """members: list of (spec_order, kind, assoc_dict), same identity, in layer
    order. `layer_sources[spec_order]` is the contributing layer's {kind,
    provider}. Merge into one association per §8.4 + §9. The merged association
    carries a structured `provenance` (representative string) + `sources` (the
    deduplicated union of contributing layers) and NOT the legacy db_fk/manual/
    inferred booleans — those are re-derived only at the serialize boundary."""
    ms = sorted(members, key=lambda m: m[0])
    role = _assoc_role(ms[0][2])
    out = {}
    # cardinality: never lose a 1:1 — an owner_fk group with any has_one is
    # has_one (§8.4); otherwise later layer's type wins.
    types = [a['type'] for _, _, a in ms]
    out['type'] = 'has_one' if (role == 'owner_fk' and 'has_one' in types) else ms[-1][2]['type']
    # name: logical authority (declared/config name beats a DB-derived one)
    out['name'] = _pick_by_authority(
        [(_LOGICAL_RANK[kind], order, a['name']) for order, kind, a in ms])
    out['target'] = ms[-1][2]['target']  # constant within an identity group
    for _, _, a in ms:
        if a.get('foreign_key'):
            out['foreign_key'] = a['foreign_key']
            break
    thr = [(order, a['through']) for order, _, a in ms if a.get('through')]
    if thr:
        out['through'] = max(thr, key=lambda x: x[0])[1]
    if any(a.get('polymorphic') for _, _, a in ms):
        out['polymorphic'] = True
    # §8.6 (P0-3): a config association overrides a same-identity db/framework
    # one. Warn when it overrides *differing* content — a different cardinality,
    # target, through, or polymorphic flag — so a silent semantic change is
    # visible. A name difference is expected (declared vs machine-derived) and
    # never warned. Identical content merges quietly.
    cfg = [a for _, kind, a in ms if kind == 'config']
    if cfg:
        c = cfg[-1]
        for _, kind, a in ms:
            if kind == 'config':
                continue
            if _assoc_content_differs(a, c):
                print(f"Warning: config association {c.get('name')!r} (on {c['target']!r}) "
                      f"overrides a differing {kind} association", file=sys.stderr)
                break
    # §10 (multiple --models): a later framework layer overriding an earlier
    # framework layer's same-identity association with differing content is a
    # multi-framework conflict the user should see. `ms` is already layer-order
    # sorted, so the last framework member is the winner.
    fw_members = [a for _, kind, a in ms if kind == 'framework']
    if len(fw_members) >= 2:
        winner = fw_members[-1]
        if any(_assoc_content_differs(a, winner) for a in fw_members[:-1]):
            print(f"Warning: framework association {winner.get('name')!r} (on "
                  f"{winner['target']!r}) overrides a differing earlier framework "
                  f"association", file=sys.stderr)
    # provenance (§9.1): representative string by precedence (manual > declared
    # > db_fk > inferred). A config-layer association is manual by definition
    # (§9.1) even if its fragment carries no flag; otherwise read the member's
    # own legacy flags. Stored structured on the merged IR — converted back to a
    # legacy boolean only by serialize_for_viewer (§9.3).
    def member_prov(kind, a):
        return 'manual' if kind == 'config' else provenance_of(a)
    out['provenance'] = max((member_prov(kind, a) for _, kind, a in ms),
                            key=lambda p: _PROV_PRECEDENCE[p])
    # sources (§9.1): the deduplicated union of the {kind, provider} of every
    # layer that contributed to this identity, deterministically ordered. A DB
    # FK also declared in Rails ends up with both {db,mysql} and {framework,
    # rails}.
    seen_src, src = set(), []
    for order, _, _ in ms:
        s = layer_sources[order]
        key = (s['kind'], s['provider'])
        if key not in seen_src:
            seen_src.add(key)
            src.append({'kind': s['kind'], 'provider': s['provider']})
    out['sources'] = sorted(src, key=lambda s: (s['kind'], s['provider']))
    return out

def _config_assoc_drop_matches(drop, ident):
    """Does a config association DropOperation match a merged association's
    identity tuple (§4.3/§6.2)? Role + target must match. An owner_fk drop
    matches by (target, foreign_key) and — since the identity now includes
    `name` (6b) — drops EVERY owner_fk association on that column/target when
    no `name` is given (so a wrong FK edge can be removed without naming it),
    or additionally filters by `name` when one is given. A collection/inverse
    drop matches by (type-role, target, name). If the drop pins through/
    polymorphic, those must match too."""
    target, fk, role, name = ident[1], ident[2], ident[3], ident[4]
    if role != _assoc_role(drop) or target != drop.get('target'):
        return False
    extras = dict(ident[5:])
    if drop.get('through') and drop['through'] != extras.get('through'):
        return False
    if drop.get('polymorphic') and not extras.get('polymorphic'):
        return False
    if role == 'owner_fk':
        d_fk = frozenset([drop['foreign_key']]) if drop.get('foreign_key') else frozenset()
        if fk != d_fk:
            return False
        return drop.get('name') is None or drop['name'] == name
    return drop.get('name') == name

def _merge_table(tname, contribs, layer_sources):
    """contribs: list of (kind, spec_order, fragment) for one table, in layer
    order. `layer_sources[spec_order]` is that layer's {kind, provider} source,
    threaded through to the association merge for `sources` (§9.1). Build the
    merged table (columns/indexes/primary_key/comment/associations). Derived
    fields (fk_columns, schema_missing, primary_key normalization) are applied
    later, after Phase B.

    Config-only operation markers (§6.2), present ONLY on config-kind fragments:
      - `columns_mode|indexes_mode|associations_mode: "replace"` (table-scope):
        discard lower-layer (db/framework) contributions for that field before
        applying the config list. Default "merge" = additive/override (today's
        behavior). replace precedes per-item drop (nothing lower to drop).
      - per-item `drop: true`: remove the matching column (by name) / index (by
        name) / association (by identity, see _config_assoc_drop_matches).
    Within a config fragment, a field's list is processed in document order;
    drop entries remove matching lower-layer items, non-drop entries add/override
    (Step-3 load validation forbids a duplicate name/identity within one config
    fragment, so a drop and a re-add of the SAME item can't coexist there). All
    op markers are stripped from the output — the final IR carries data only.
    A drop that matches nothing is a silent no-op here; the hard error for an
    absent drop target is semantic validation, added in Step 7b (§6.4②)."""
    def replace(mode_key):
        return any(f.get(mode_key) == 'replace' for k, _, f in contribs if k == 'config')

    merged = {}
    # ── columns: keyed by name, first-seen order across layers (§7.2) ──
    col_replace = replace('columns_mode')
    col_order, col_seen, col_contribs, col_drops = [], set(), {}, set()
    for kind, order, frag in contribs:
        for c in frag.get('columns', []):
            if kind == 'config' and c.get('drop') is True:
                col_drops.add(c['name'])
                continue
            if col_replace and kind != 'config':
                continue  # replace discards lower-layer columns
            nm = c['name']
            if nm not in col_seen:
                col_seen.add(nm)
                col_order.append(nm)
            col_contribs.setdefault(nm, []).append((kind, order, c))
    merged['columns'] = [_merge_column(nm, col_contribs[nm])
                         for nm in col_order if nm not in col_drops]
    # ── primary_key: physical authority, present-only (§7.3) ──
    pk = [(_PHYSICAL_RANK[kind], order, frag['primary_key'])
          for kind, order, frag in contribs if 'primary_key' in frag]
    merged['primary_key'] = _pick_by_authority(pk) if pk else None
    # ── indexes: keyed by name (unnamed -> tuple(columns), NOT unique — §7.4);
    #    whole-index physical authority; union across keys ──
    ix_replace = replace('indexes_mode')
    ix_order, ix_seen, ix_contribs, ix_drops = [], set(), {}, set()
    for kind, order, frag in contribs:
        for ix in frag.get('indexes', []):
            if kind == 'config' and ix.get('drop') is True:
                ix_drops.add(ix['name'])  # index drop is always by name (§7.4)
                continue
            if ix_replace and kind != 'config':
                continue
            k = ix['name'] if ix.get('name') else tuple(ix.get('columns', []))
            if k not in ix_seen:
                ix_seen.add(k)
                ix_order.append(k)
            ix_contribs.setdefault(k, []).append((_PHYSICAL_RANK[kind], order, ix))
    merged['indexes'] = []
    for k in ix_order:
        if k in ix_drops:
            continue
        ix = copy.deepcopy(_pick_by_authority(ix_contribs[k]))
        ix.pop('drop', None)  # strip any op marker
        merged['indexes'].append(ix)
    # ── comment: logical authority, present-only; null/"" = delete (§7.5) ──
    cm = [(_LOGICAL_RANK[kind], order, frag['comment'])
          for kind, order, frag in contribs if 'comment' in frag]
    if cm:
        val = _pick_by_authority(cm)
        if val:  # a non-empty string; null/"" resolves to "no comment"
            merged['comment'] = val
    # ── associations: Phase A identity merge (§7.6/§8) ──
    a_replace = replace('associations_mode')
    a_order, a_groups, a_drops = [], {}, []
    for kind, order, frag in contribs:
        for a in frag.get('associations', []):
            if kind == 'config' and a.get('drop') is True:
                a_drops.append(a)   # collected separately (may omit `name`)
                continue
            if a_replace and kind != 'config':
                continue
            ident = association_key(tname, a)
            if ident not in a_groups:
                a_groups[ident] = []
                a_order.append(ident)
            a_groups[ident].append((order, kind, a))
    merged['associations'] = [
        _merge_association_group(a_groups[i], layer_sources) for i in a_order
        if not any(_config_assoc_drop_matches(d, i) for d in a_drops)]
    return merged

def _normalize_primary_key(tname, t, warn=True, authoritative=False):
    """§7.3: primary_key is authoritative — ensure every column it names carries
    primary=True (a PK column whose primary flag a lower layer didn't set is
    corrected up). By default columns are NOT flipped to False, so a composite PK
    whose columns were all flagged by the DB (primary_key stores only the first)
    keeps every member primary. A PK naming an absent column is a warning, not
    fatal.

    `authoritative=True` (a config layer supplied primary_key, so it is COMPLETE)
    additionally RESETS every non-PK column's primary flag to False — and clears
    ALL primary flags when the config primary_key is null. This is what makes
    `primary_key: null` (or a config PK narrower than the DB's) take full effect
    instead of leaving stale DB primary flags behind.

    `warn=False` suppresses the missing-column warning for a config-declared
    primary_key: validate_config_references issues an authoritative hard error
    for it, so the non-fatal warning would be a redundant second message."""
    pk = t.get('primary_key')
    pk_names = [] if pk is None else ([pk] if isinstance(pk, str) else list(pk))
    names = {c['name'] for c in t['columns']}
    for c in t['columns']:
        if c['name'] in pk_names:
            c['primary'] = True
        elif authoritative:
            c['primary'] = False
    missing = [n for n in pk_names if n not in names]
    if missing and warn:
        print(f"Warning: table {tname!r} primary_key names column(s) not present: "
              f"{', '.join(missing)}", file=sys.stderr)

def merge_ir(layers):
    """Merge an ordered list of ProviderResults (low→high spec priority — the
    usual order is [db?, framework_1?, ..., config?]) into one IR dict per §7-§9.
    Pure: inputs are deep-copied, never mutated.

    Steps: union tables (first-seen order) → apply config table `drop` → per-
    field authority merge with config `drop`/`*_mode: replace` ops (Phase A,
    incl. association identity merge) → Phase B reconcile_db_fks → derive
    primary_key normalization, fk_columns (§7.7), and schema_missing (§7.8).

    Config operation markers (table `drop`, per-item `drop`, `*_mode: replace`)
    are honored here and stripped from the output IR (they're operations, not
    data). They are only produced on config-kind layers; the current pipeline's
    config layer (relations → associations) carries none, so this is inert for
    it. `config.tables` is wired into main in Step 7b; semantic validation of
    drop targets etc. (§6.4②) also lands there."""
    prepared = [(pr['source']['kind'], copy.deepcopy(pr['tables'])) for pr in layers]
    # per-layer source ({kind, provider}), indexed by spec order — threaded into
    # the association merge so each merged association can record the union of
    # the layers that contributed to it (§9.1 `sources`).
    layer_sources = [pr['source'] for pr in layers]
    order, seen = [], set()
    for _, tbls in prepared:
        for tname in tbls:
            if tname not in seen:
                seen.add(tname)
                order.append(tname)
    # Tables whose primary_key is contributed by a config layer (the KEY is
    # present, even if its value is null). config has the highest physical-field
    # rank, so a config primary_key always wins and is authoritative/COMPLETE:
    # _normalize_primary_key resets non-PK primary flags for these (§7.3, P1-c).
    # It also suppresses the non-fatal missing-column warning (validate_config_
    # references raises an authoritative hard error instead). Non-config PK
    # mismatches still warn and never reset flags (composite-PK safe).
    config_pk_tables = {tname for kind, tbls in prepared if kind == 'config'
                        for tname, frag in tbls.items()
                        if isinstance(frag, dict) and 'primary_key' in frag}
    result = {}
    for tname in order:
        contribs = [(kind, spec, tbls[tname])
                    for spec, (kind, tbls) in enumerate(prepared) if tname in tbls]
        # config table drop (§6.2): `tables: { t: { drop: true } }` removes the
        # table from the result (a no-op if nothing lower provided it).
        if any(kind == 'config' and frag.get('drop') is True for kind, _, frag in contribs):
            continue
        result[tname] = _merge_table(tname, contribs, layer_sources)
    # Phase B: edge-level DB-FK reconciliation (§8.5), same as the legacy path.
    reconcile_db_fks(result)
    # Derived values (§7.3/§7.7/§7.8) — computed, never read from input.
    for tname, t in result.items():
        # Normalize columns to the shape the HTML/Excel consumers expect always
        # present (mysql_ir and the Prisma/Django parsers already set these on
        # every column; a config-only column may omit them). Inert for db/
        # framework columns — pure setdefault, so no existing output changes.
        for c in t['columns']:
            c.setdefault('type', '')
            c.setdefault('nullable', False)
            c.setdefault('primary', False)
        # Normalize indexes to always carry `unique` (mysql_ir/parsers already
        # do; a config index may omit it — Step-3 accepts it as optional). This
        # keeps every downstream ix['unique'] read (_unique_single_col, Excel,
        # HTML) safe. Inert for db/framework indexes, so demo stays byte-equal.
        for ix in t.get('indexes', []):
            ix.setdefault('unique', False)
        authoritative_pk = tname in config_pk_tables
        _normalize_primary_key(tname, t, warn=not authoritative_pk,
                               authoritative=authoritative_pk)
        t['fk_columns'] = sorted({a['foreign_key'] for a in t['associations']
                                  if a.get('foreign_key')})
        if len(t['columns']) == 0:
            t['schema_missing'] = True
    return result

def reconcile_db_fks(tables):
    """Phase B (§8.5) — edge-level DB-FK reconciliation: an explicit (non-db_fk,
    non-inferred) association covering an undirected {source, target} pair drops
    the DB FK for that pair when the explicit side names no column or the same
    column; a dropped has_one DB FK upgrades a lone covering belongs_to to
    has_one in place. belongs_to alone doesn't assert cardinality in Rails, so
    dropping a 1:1 DB FK outright would silently discard the DB's 1:1 signal.
    Mutates `tables` in place and returns the number of DB FKs removed."""
    explicit_by_pair = {}
    for name, t in tables.items():
        for a in t['associations']:
            if _assoc_provenance(a) in ('declared', 'manual'):
                explicit_by_pair.setdefault(frozenset((name, a['target'])), []).append((name, a))
    removed = 0
    for name, t in tables.items():
        kept = []
        for a in t['associations']:
            if _assoc_provenance(a) == 'db_fk':
                candidates = explicit_by_pair.get(frozenset((name, a['target'])), [])
                covering = [(n, ea) for n, ea in candidates
                            if not ea.get('foreign_key') or ea['foreign_key'] == a.get('foreign_key')]
                if covering:
                    has_cardinality = any(ea['type'] in ('has_one', 'has_many') for _, ea in covering)
                    if a['type'] == 'has_one' and not has_cardinality:
                        for other_name, ea in covering:
                            if other_name == name and ea['type'] == 'belongs_to':
                                ea['type'] = 'has_one'
                    removed += 1
                    continue
            kept.append(a)
        t['associations'] = kept
    return removed

# ---------------------------------------------------------------------------
# Pluralizer / inflector
# ---------------------------------------------------------------------------
IRREGULAR = {
    'person':'people','child':'children','mouse':'mice','datum':'data',
    'medium':'media','analysis':'analyses','criterion':'criteria',
    'tooth':'teeth','foot':'feet','goose':'geese','ox':'oxen',
    'leaf':'leaves','life':'lives','knife':'knives','wife':'wives',
}

def pluralize(word):
    if not word: return word
    if word in IRREGULAR: return IRREGULAR[word]
    if re.search(r'[^aeiou]y$', word): return word[:-1] + 'ies'
    if re.search(r'(s|x|z|ch|sh)$', word): return word + 'es'
    return word + 's'

def to_snake(name):
    s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', name)
    return re.sub(r'([a-z\d])([A-Z])', r'\1_\2', s).lower()

def class_to_table(name):
    return pluralize(to_snake(name.split('::')[-1]))

# ---------------------------------------------------------------------------
# Model parser (app/models/**/*.rb)
# ---------------------------------------------------------------------------
def rails_provider(models_dir, table_map=None):
    """ProviderResult for a Rails app/models directory (REFACTOR_PLAN.md §5).

    Runs the full Rails static analysis — STI table sharing, concern-
    resolved self.table_name, transitively-resolved custom base classes,
    per-class-scoped abstract detection, the target_override redirect for
    associations pointing at a renamed table, belongs_to FK backfill, through,
    polymorphic, and commented-out table_name handling — but builds a FRESH
    fragment IR instead of mutating a caller's dict.

    Rails contributes associations ONLY: it has no column information, so each
    table Fragment carries just `associations` and OMITS the `columns` key
    entirely (§4: an absent key means "not supplied — keep the lower layer's
    value", so a later merge over a DB IR never erases the DB's columns).
    schema_missing is NOT set here — it's a derived/merge concern (§7.8); a
    framework-only merge_ir run derives it for tables with no columns.

    Association append order is preserved exactly: models are iterated in the
    same sorted order and appended into their table's fragment, so STI (several
    models -> one table) accumulates in the same order as before."""
    fragment = {}
    if models_dir.is_dir():
        _parse_rails_models(models_dir, fragment, table_map or {})
    return make_provider_result('framework', 'rails', fragment,
                                location=str(models_dir))

def _parse_rails_models(models_dir, fragment, table_map):
    """Rails static analysis, writing association fragments into `fragment`
    (keyed by table_name, each entry `{'associations': [...]}`, no columns).
    See rails_provider for the contract."""
    # module_name -> file content, for resolving self.table_name when it's
    # set inside an `include`d concern rather than the model body itself
    # (a common way to share a "points at a legacy/renamed table" mixin
    # across models). Only catches a literal assignment somewhere in the
    # module's file, `included do ... end` or not — anything computed
    # dynamically is genuinely out of reach for regex-based static analysis.
    module_src = {}
    # class_name -> {base, content, clean} for every `class X < Y` found
    # anywhere under models_dir (concerns excluded, same as before) —
    # collected in one pass so a custom base class (`class Widget <
    # BaseRecord`, common in mature Rails apps once they've grown a base
    # model of their own) can be resolved transitively below, the same way
    # parse_django resolves models.Model through abstract base classes.
    class_info = {}
    for path in models_dir.rglob('*.rb'):
        try:
            content = path.read_text(encoding='utf-8', errors='replace')
        except Exception:
            continue
        for mm in re.finditer(r'module\s+(\w+)\b', content):
            module_src.setdefault(mm.group(1), content)
        if 'concerns' in path.parts:  # separator-agnostic (works on Windows too)
            continue
        clean = re.sub(r'#[^\n]*', '', content)
        # a file can define more than one class (e.g. an abstract BaseRecord
        # alongside a concrete model using it) — scope each match's "body"
        # to the text between it and the next class declaration (or EOF) so
        # self.abstract_class/table_name/associations aren't accidentally
        # read from a sibling class earlier or later in the same file
        matches = list(re.finditer(r'class\s+([\w:]+)\s*<\s*([\w:]+)', clean))
        for i, cm in enumerate(matches):
            body = clean[cm.end():matches[i+1].start() if i+1 < len(matches) else len(clean)]
            # self.abstract_class = true (e.g. a shared BaseRecord) still
            # counts as a valid base for the transitive check below, but
            # isn't itself a real, queryable model with its own table
            abstract = re.search(r'self\.abstract_class\s*=\s*true', body) is not None
            class_info[cm.group(1)] = {'base': cm.group(2), 'content': content,
                                       'body': body, 'abstract': abstract}

    # a class counts as a real model if it (transitively) inherits from
    # ApplicationRecord/ActiveRecord::Base — not just a literal, direct
    # `< ApplicationRecord`, which silently dropped every model built on a
    # shared custom base class with no warning
    model_names, changed = set(), True
    while changed:
        changed = False
        for name, c in class_info.items():
            if name not in model_names and (
                    c['base'] in ('ApplicationRecord', 'ActiveRecord::Base')
                    or c['base'] in model_names):
                model_names.add(name)
                changed = True

    def sti_root(name):
        # Rails STI: a class whose base is a *concrete* (non-abstract) model
        # shares that model's table rather than getting its own — walk up
        # while the base is itself a known, concrete model; stop at the
        # first abstract base (a real "custom base class", not STI) or at
        # a base outside class_info (ApplicationRecord/ActiveRecord::Base)
        cur, seen = name, set()
        while cur not in seen:
            seen.add(cur)
            base = class_info[cur]['base']
            if base not in class_info or class_info[base]['abstract']:
                return cur
            cur = base
        return cur

    def resolve_table_name(class_name):
        if class_name in table_map:
            # explicit override wins over everything — the escape hatch for
            # cases static analysis genuinely can't reach, e.g. table_name
            # set inside a concern that itself lives in a gem, not the app
            return table_map[class_name]
        # comment-stripped `body`, not raw `content` — a commented-out
        # `# self.table_name = 'old'` must not win over (or stand in for)
        # the real, active assignment
        body = class_info[class_name]['body']
        tn_m = re.search(r"self\.table_name\s*=\s*['\"]([^'\"]+)['\"]", body)
        if not tn_m:
            for inc_m in re.finditer(r'include\s+([\w:]+)', body):
                mod_content = module_src.get(inc_m.group(1).rsplit('::', 1)[-1])
                if mod_content:
                    mod_clean = re.sub(r'#[^\n]*', '', mod_content)
                    tn_m = re.search(r"self\.table_name\s*=\s*['\"]([^'\"]+)['\"]", mod_clean)
                    if tn_m:
                        break
        return tn_m.group(1) if tn_m else class_to_table(class_name)

    # An association's target is resolved from a class/symbol name via the
    # same naive class_to_table() convention used everywhere below — which
    # never consults table_map/self.table_name. So a model whose *own*
    # table_name is overridden (e.g. Project -> real table aaa_projects)
    # still gets every *reference* to it pointed at the naive guess
    # ('projects', a table that doesn't exist) — the right-pane link for
    # that association shows a target that can never be found. Build a
    # naive-guess -> real-table redirect from every model whose resolved
    # table differs from its naive one, then apply it to every computed
    # target_table below, regardless of how that name was derived
    # (explicit class_name:, implicit belongs_to/has_one, or has_many).
    target_override = {}
    for name in model_names:
        if class_info[name]['abstract']:
            continue
        naive = class_to_table(name)
        real = resolve_table_name(name if name in table_map else sti_root(name))
        if naive != real:
            target_override[naive] = real

    for class_name in sorted(model_names):
        if class_info[class_name]['abstract']:
            continue
        body = class_info[class_name]['body']
        # an STI subclass (base is a concrete model) shares its root
        # ancestor's table — it must not get a phantom table of its own.
        # A table_map entry on the subclass itself still wins, though —
        # that's the explicit, deliberate override escape hatch, and it
        # should stay the one thing that always takes precedence
        table_name = resolve_table_name(
            class_name if class_name in table_map else sti_root(class_name))
        # fresh fragment entry per table (STI: several models share one table,
        # so setdefault keeps the first and accumulates associations). No
        # columns key — Rails supplies none (§4/§7.8) — and no schema_missing
        # (a derived value merge_ir computes for tables with no columns).
        frag = fragment.setdefault(table_name, {'associations': []})
        for m2 in re.finditer(
            r'(has_many|has_one|belongs_to|has_and_belongs_to_many)\s+:(\w+)((?:[^#\n]|,[ \t]*\n)*)',
            body
        ):
            assoc_type, sym, opts = m2.group(1), m2.group(2), m2.group(3)
            through_m = re.search(r'through:\s*:(\w+)', opts)
            class_m   = re.search(r"class_name:\s*['\"]([^'\"]+)['\"]", opts)
            fk_m      = re.search(r"foreign_key:\s*['\"]([^'\"]+)['\"]", opts)
            poly      = re.search(r'polymorphic:\s*true', opts) is not None
            if class_m:
                target_table = class_to_table(class_m.group(1))
            elif assoc_type in ('belongs_to', 'has_one'):
                target_table = pluralize(to_snake(sym))
            else:
                target_table = sym
            target_table = target_override.get(target_table, target_table)
            assoc = {'type': assoc_type, 'name': sym, 'target': target_table}
            if through_m: assoc['through'] = through_m.group(1)
            if fk_m:      assoc['foreign_key'] = fk_m.group(1)
            # belongs_to holds the FK on *this* table; without an explicit
            # foreign_key: option Rails defaults to the convention column —
            # backfill it so FK badges/inference have a real column to point
            # to instead of only working when the option happens to be given
            elif assoc_type == 'belongs_to':
                assoc['foreign_key'] = f'{to_snake(sym)}_id'
            if poly:      assoc['polymorphic'] = True
            frag['associations'].append(assoc)

# ---------------------------------------------------------------------------
# Prisma schema parser (schema.prisma)
# ---------------------------------------------------------------------------
PRISMA_TYPES = {
    'Int': 'integer', 'BigInt': 'bigint', 'String': 'string',
    'Boolean': 'boolean', 'DateTime': 'datetime', 'Json': 'jsonb',
    'Float': 'float', 'Decimal': 'decimal', 'Bytes': 'binary',
}

def parse_prisma(schema_path):
    text = schema_path.read_text(encoding='utf-8', errors='replace')
    text = re.sub(r'//[^\n]*', '', text)  # strip comments

    blocks = {m.group(1): m.group(2)
              for m in re.finditer(r'model\s+(\w+)\s*\{([^}]*)\}', text)}
    enums = set(re.findall(r'enum\s+(\w+)\s*\{', text))

    def table_of(model):
        mm = re.search(r'@@map\("([^"]+)"\)', blocks[model])
        return mm.group(1) if mm else model

    def is_list_of(model, other):
        # does `model` declare an `xxx Other[]` field? (implicit m2m check)
        return re.search(r'\w+\s+%s\[\]' % re.escape(other), blocks[model]) is not None

    tables = {}
    for model, block in blocks.items():
        cols, assocs, pk = [], [], None
        unique_cols = set()  # scalar fields with @unique — used below to
                              # tell a 1:1 FK-holding side from a plain belongs_to
        lines = [l.strip() for l in block.splitlines() if l.strip() and not l.strip().startswith('@@')]

        # pass 1: scalar/enum columns only — relation fields need unique_cols
        # fully populated first (a relation's `fields: [...]` FK column can be
        # declared on any line in the block, not necessarily before it)
        for line in lines:
            fm = re.match(r'(\w+)\s+(\w+)(\[\])?(\?)?\s*(.*)', line)
            if not fm:
                continue
            fname, ftype, is_list, optional, rest = fm.groups()
            if ftype in blocks:
                continue  # relation field, handled in pass 2
            col = fname
            cm = re.search(r'@map\("([^"]+)"\)', rest)
            if cm:
                col = cm.group(1)
            primary = '@id' in rest
            if primary:
                pk = col
            if '@unique' in rest:
                unique_cols.add(col)
            cols.append({
                'name': col,
                'type': PRISMA_TYPES.get(ftype, ftype if ftype in enums else ftype.lower()),
                'nullable': bool(optional) and not primary,
                'primary': primary,
            })

        # pass 2: relation fields
        for line in lines:
            fm = re.match(r'(\w+)\s+(\w+)(\[\])?(\?)?\s*(.*)', line)
            if not fm:
                continue
            fname, ftype, is_list, optional, rest = fm.groups()
            if ftype not in blocks:
                continue
            target = table_of(ftype)
            fields = re.search(r'fields:\s*\[\s*(\w+)', rest)
            if is_list:
                # the other side lists this model too -> implicit many-to-many
                if is_list_of(ftype, model):
                    assocs.append({'type': 'has_and_belongs_to_many',
                                   'name': fname, 'target': target})
                else:
                    assocs.append({'type': 'has_many', 'name': fname, 'target': target})
            elif fields:  # the side holding the FK
                fk_col = fields.group(1)
                # @unique on the scalar FK field means each value can only
                # appear once — a real 1:1, not the default many:1 a bare FK
                # column implies. Same has_one convention parse_django uses.
                assoc_type = 'has_one' if fk_col in unique_cols else 'belongs_to'
                assocs.append({'type': assoc_type, 'name': fname,
                               'target': target, 'foreign_key': fk_col})
            else:  # 1:1 parent side without the FK
                assocs.append({'type': 'has_one', 'name': fname, 'target': target})

        tables[table_of(model)] = {'columns': cols, 'associations': assocs,
                                   'primary_key': pk}
    return tables

# ---------------------------------------------------------------------------
# Django models parser (**/models.py, AST based)
# ---------------------------------------------------------------------------
DJANGO_TYPES = {
    'CharField': 'string', 'TextField': 'text', 'SlugField': 'string',
    'EmailField': 'string', 'URLField': 'string', 'FileField': 'string',
    'ImageField': 'string', 'FilePathField': 'string',
    'IntegerField': 'integer', 'SmallIntegerField': 'integer',
    'PositiveIntegerField': 'integer', 'PositiveSmallIntegerField': 'integer',
    'BigIntegerField': 'bigint', 'PositiveBigIntegerField': 'bigint',
    'AutoField': 'integer', 'BigAutoField': 'bigint', 'SmallAutoField': 'integer',
    'FloatField': 'float', 'DecimalField': 'decimal', 'BooleanField': 'boolean',
    'DateTimeField': 'datetime', 'DateField': 'date', 'TimeField': 'time',
    'DurationField': 'interval', 'UUIDField': 'uuid', 'JSONField': 'jsonb',
    'BinaryField': 'binary', 'GenericIPAddressField': 'inet', 'IPAddressField': 'inet',
}
DJANGO_REL_FIELDS = {'ForeignKey', 'OneToOneField', 'ManyToManyField'}
_DJANGO_SKIP_DIRS = {'venv', '.venv', 'env', 'site-packages', 'node_modules',
                     'migrations', '.git', 'tests', 'staticfiles'}

def parse_django(root):
    # collect model files: <app>/models.py and <app>/models/*.py
    files = []
    for p in sorted(root.rglob('*.py')):
        if set(p.relative_to(root).parts) & _DJANGO_SKIP_DIRS:
            continue
        if p.name == 'models.py':
            files.append((p.parent.name, p))
        elif p.parent.name == 'models':
            files.append((p.parent.parent.name, p))

    # pass 1: collect every class definition
    classes = {}  # class name -> {app, bases, fields, meta}
    for app, path in files:
        try:
            tree = ast.parse(path.read_text(encoding='utf-8', errors='replace'))
        except SyntaxError:
            continue
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            bases = [b.attr if isinstance(b, ast.Attribute) else
                     b.id if isinstance(b, ast.Name) else '' for b in node.bases]
            meta, fields = {}, []
            for stmt in node.body:
                if isinstance(stmt, ast.ClassDef) and stmt.name == 'Meta':
                    for ms in stmt.body:
                        if (isinstance(ms, ast.Assign) and len(ms.targets) == 1
                                and isinstance(ms.targets[0], ast.Name)
                                and isinstance(ms.value, ast.Constant)):
                            meta[ms.targets[0].id] = ms.value.value
                elif (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                        and isinstance(stmt.targets[0], ast.Name)
                        and isinstance(stmt.value, ast.Call)):
                    fn = stmt.value.func
                    ftype = fn.attr if isinstance(fn, ast.Attribute) else                             fn.id if isinstance(fn, ast.Name) else None
                    if ftype and (ftype in DJANGO_TYPES or ftype in DJANGO_REL_FIELDS):
                        fields.append({
                            'name': stmt.targets[0].id, 'ftype': ftype,
                            'args': stmt.value.args,
                            'kw': {k.arg: k.value for k in stmt.value.keywords if k.arg},
                        })
            classes[node.name] = {'app': app, 'bases': bases,
                                  'fields': fields, 'meta': meta}

    # a class is a model if models.Model is among its ancestors (transitively)
    model_names, changed = set(), True
    while changed:
        changed = False
        for name, c in classes.items():
            if name not in model_names and (
                    'Model' in c['bases']
                    or any(b in model_names for b in c['bases'])):
                model_names.add(name)
                changed = True

    def const(v):
        return v.value if isinstance(v, ast.Constant) else None

    def merged_fields(name, seen=None):
        # inherit fields from abstract base classes
        seen = seen or set()
        if name not in classes or name in seen:
            return []
        seen.add(name)
        out = []
        for b in classes[name]['bases']:
            if b in model_names:
                out.extend(merged_fields(b, seen))
        out.extend(classes[name]['fields'])
        return out

    concrete = [n for n in model_names if n in classes
                and not classes[n]['meta'].get('abstract')
                and not classes[n]['meta'].get('proxy')]
    table_of = {n: classes[n]['meta'].get('db_table')
                or f"{classes[n]['app']}_{n.lower()}" for n in concrete}

    def resolve(node, own):
        # ForeignKey(Author) / ForeignKey('Author') / ForeignKey('blog.Author') / 'self'
        if isinstance(node, ast.Name) and node.id in table_of:
            return table_of[node.id]
        if isinstance(node, ast.Attribute) and node.attr in table_of:
            return table_of[node.attr]
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            s = node.value
            if s == 'self':
                return table_of.get(own)
            cls = s.split('.')[-1]
            if cls in table_of:
                return table_of[cls]
            app = s.split('.')[0].lower() if '.' in s else classes[own]['app']
            return f'{app}_{cls.lower()}'
        return None

    tables = {}
    for n in concrete:
        tname = table_of[n]
        cols, assocs, pk = [], [], None
        for f in merged_fields(n):
            fname, ftype, kw = f['name'], f['ftype'], f['kw']
            if ftype in DJANGO_REL_FIELDS:
                tnode = kw.get('to') or (f['args'][0] if f['args'] else None)
                target = resolve(tnode, n) if tnode is not None else None
                if not target:
                    continue
                if ftype == 'ManyToManyField':
                    a = {'type': 'has_and_belongs_to_many', 'name': fname, 'target': target}
                    thr = resolve(kw['through'], n) if 'through' in kw else None
                    if thr:
                        a['through'] = thr
                    assocs.append(a)
                else:
                    col = const(kw.get('db_column')) or f'{fname}_id'
                    cols.append({'name': col, 'type': 'bigint',
                                 'nullable': bool(const(kw.get('null'))), 'primary': False})
                    # OneToOneField: the declaring side holds the FK, but we emit
                    # has_one so the edge renders as 1:1
                    assocs.append({'type': 'has_one' if ftype == 'OneToOneField' else 'belongs_to',
                                   'name': fname, 'target': target, 'foreign_key': col})
                continue
            col = const(kw.get('db_column')) or fname
            primary = bool(const(kw.get('primary_key')))
            if primary:
                pk = col
            cols.append({'name': col, 'type': DJANGO_TYPES[ftype],
                         'nullable': bool(const(kw.get('null'))) and not primary,
                         'primary': primary})
        if pk is None:  # Django creates an implicit id (BigAutoField)
            pk = 'id'
            cols.insert(0, {'name': 'id', 'type': 'bigint',
                            'nullable': False, 'primary': True})
        tables[tname] = {'columns': cols, 'associations': assocs, 'primary_key': pk}
    return tables

# ---------------------------------------------------------------------------
# FK column inference — guess relations from `xxx_id` columns (IR post-pass)
# ---------------------------------------------------------------------------
def infer_fk_associations(tables):
    """Infer edges from FK-looking column names even when no association is
    declared. Pairs already related in either direction are skipped. Marked with
    provenance 'inferred' and an empty `sources` (no provider layer contributed
    it — it is a post-merge heuristic derived from *_id column names, not a
    merge of source layers). Tries the pluralized table name first (Rails
    convention) and falls back to the singular stem as-is (common with
    Prisma/other schemas that don't pluralize table names) — whichever
    actually exists. A column alone under a single-column unique index is
    inferred as has_one (1:1) instead of belongs_to (default many:1), same
    signal the real-DB-FK path uses."""
    added = 0
    incoming = {}  # table -> set(tables that reference it)
    for name, t in tables.items():
        for a in t['associations']:
            incoming.setdefault(a['target'], set()).add(name)
    for name, t in tables.items():
        outgoing = {a['target'] for a in t['associations']}
        for c in t['columns']:
            cn = c['name']
            if not cn.endswith('_id') or c.get('primary'):
                continue
            stem = cn[:-3]
            plural = pluralize(stem)
            target = plural if plural in tables else stem if stem in tables else None
            if (target is None or target == name
                    or target in outgoing              # we already reference it
                    or target in incoming.get(name, ())):  # it already references us
                continue
            assoc_type = 'has_one' if _unique_single_col(t, cn) else 'belongs_to'
            t['associations'].append({'type': assoc_type, 'name': stem,
                                      'target': target, 'foreign_key': cn,
                                      'provenance': 'inferred', 'sources': []})
            outgoing.add(target)
            added += 1
    return added

# ---------------------------------------------------------------------------
# Source detection
# ---------------------------------------------------------------------------
def detect_code_source(root):
    """Classify a --models path: a Rails app/models dir, a Prisma schema,
    or a Django project."""
    if root.is_file():
        return 'prisma' if root.suffix == '.prisma' else None
    if (root / 'app' / 'models').is_dir() or any(root.glob('*.rb')):
        return 'rails'
    if (root / 'manage.py').exists():
        return 'django'
    for cand in (root / 'prisma' / 'schema.prisma', root / 'schema.prisma'):
        if cand.exists():
            return 'prisma'
    return None

# ---------------------------------------------------------------------------
# Framework leaf providers (REFACTOR_PLAN.md §5) — ProviderResult wrappers.
#
# Each wraps its existing parser (unchanged) and packages the FULL IR —
# crucially the columns — as a ProviderResult, which merge_ir folds against the
# DB layer. The `framework_provider` dispatcher (detect + resolve path) routes
# the merge through these. These leaf providers take an already-resolved input
# (a .prisma file path / a Django project root); detection and path resolution
# live in detect_code_source / framework_provider.
# ---------------------------------------------------------------------------
def prisma_provider(schema_path):
    """ProviderResult for a resolved Prisma schema file. Retains columns
    (with Prisma types, including enum field types as the enum name) so a
    Prisma-only run (Step 8) or the Step-6 merge can use them instead of
    discarding them the way the current association-only overlay does."""
    tables = parse_prisma(schema_path)
    return make_provider_result('framework', 'prisma', tables,
                                location=str(schema_path))

def django_provider(root):
    """ProviderResult for a resolved Django project root. Retains columns —
    including the synthetic `id` PK Django backfills and the `<name>_id` FK
    columns emitted for ForeignKey/OneToOneField — that the current overlay
    path drops."""
    tables = parse_django(root)
    return make_provider_result('framework', 'django', tables,
                                location=str(root))

def _password_free_url(url):
    """Rebuild a connection URL without its password, for a ProviderResult's
    Source.location (§5: location is password-free). Keeps user@host:port/db
    and any ?query (e.g. postgres ?schema=name)."""
    u = urlparse(url)
    netloc = u.hostname or ''
    if u.username:
        netloc = f'{u.username}@{netloc}'
    if u.port:
        netloc = f'{netloc}:{u.port}'
    out = f'{u.scheme}://{netloc}{u.path}'
    return f'{out}?{u.query}' if u.query else out

def db_provider(url):
    """DB ProviderResult (§5). Dispatches on the URL scheme to the existing
    parse_mysql/parse_postgres (unchanged) and packages the IR with a
    password-free location. Referenced as module globals so the test harness's
    parse_mysql monkeypatch still applies."""
    scheme = urlparse(url).scheme
    if scheme == 'mysql':
        return make_provider_result('db', 'mysql', parse_mysql(url),
                                    location=_password_free_url(url))
    if scheme in ('postgres', 'postgresql'):
        return make_provider_result('db', 'postgres', parse_postgres(url),
                                    location=_password_free_url(url))
    sys.exit('Error: a database URL is required (mysql:// or postgres://)')

def framework_provider(mroot, table_map=None):
    """Framework ProviderResult (§5). Detects the code kind and resolves the
    concrete input path (the Rails app/models dir, the schema.prisma file, or
    the Django root), then dispatches to the matching leaf provider."""
    kind = detect_code_source(mroot)
    if kind == 'rails':
        mdir = mroot / 'app' / 'models' if (mroot / 'app' / 'models').is_dir() else mroot
        return rails_provider(mdir, table_map)
    if kind == 'prisma':
        schema = mroot if mroot.is_file() else next(
            c for c in (mroot / 'prisma' / 'schema.prisma', mroot / 'schema.prisma') if c.exists())
        return prisma_provider(schema)
    if kind == 'django':
        return django_provider(mroot)
    sys.exit(f'Error: could not detect the code kind at {mroot} '
             '(expected a Rails app/models dir, a schema.prisma, or a Django project)')

def relations_to_config_layer(relations, base_tables):
    """Convert the config `relations` list into a config-kind ProviderResult of
    association fragments (§8.6/P0-3). Validates hard (unknown table/column/
    target are the user's typo), and cardinality is has_one when one_to_one is
    set OR the FK column is single-column-unique in the merged base, else
    belongs_to.

    No `manual` flag is written into the fragment — merge_ir forces config-kind
    associations to manual provenance (§9.1). And no "skip if already covered":
    a config relation OVERRIDES a db/framework association of the same identity
    (the intentional §12/P0-3 behavior), which merge_ir's Phase A handles."""
    tables = {}
    for i, r in enumerate(relations):
        where = f'relations[{i}]'
        for key in ('table', 'column', 'references'):
            if not r.get(key):
                sys.exit(f'Error: {where} is missing required key {key!r}')
        table, col, target = r['table'], r['column'], r['references']
        if table not in base_tables:
            sys.exit(f'Error: {where}: unknown table {table!r}')
        if not any(c['name'] == col for c in base_tables[table]['columns']):
            sys.exit(f'Error: {where}: {table!r} has no column {col!r}')
        if target not in base_tables:
            sys.exit(f'Error: {where}: unknown target table {target!r}')
        if r.get('one_to_one'):
            assoc_type = 'has_one'
        else:
            assoc_type = 'has_one' if _unique_single_col(base_tables[table], col) else 'belongs_to'
        name = r.get('name') or (col[:-3] if col.endswith('_id') else col)
        tables.setdefault(table, {'associations': []})['associations'].append(
            {'type': assoc_type, 'name': name, 'target': target, 'foreign_key': col})
    return make_provider_result('config', 'config', tables)

def config_provider(config, location=None):
    """Config ProviderResult (§5) from config['tables'] — the table fragments
    WITH their `drop`/`*_mode` operation markers intact (merge_ir consumes and
    strips them). Associations carry no `manual` flag; merge_ir forces
    config-kind associations to manual provenance (§9.1). Semantic validation
    of the ops/refs (§6.4②) is done by the caller against the merged base."""
    return make_provider_result('config', 'config', config.get('tables', {}) or {},
                                location=location)

def validate_config_drops(config_tables, base, label):
    """Semantic validation (§6.4②) of config.tables DropOperations against the
    merged db+framework `base`: a drop must target something that actually
    exists in a lower layer (dropping a nonexistent table/column/index/
    association — or one only config itself adds — is the user's mistake). Runs
    BEFORE the config layer is merged. Hard error via sys.exit; `label` is the
    config path (or 'config') for the message."""
    for tname, frag in config_tables.items():
        if not isinstance(frag, dict):
            continue  # shape already checked at load time
        if frag.get('drop') is True:
            if tname not in base:
                sys.exit(f"Error: {label} drops table {tname!r} but no such table exists")
            continue
        for c in frag.get('columns', []):
            if c.get('drop') is True and not (
                    tname in base and any(x['name'] == c['name'] for x in base[tname]['columns'])):
                sys.exit(f"Error: {label} drops column {tname}.{c['name']} but no such column exists")
        for ix in frag.get('indexes', []):
            if ix.get('drop') is True and not (
                    tname in base and any(i.get('name') == ix['name']
                                          for i in base[tname].get('indexes', []))):
                sys.exit(f"Error: {label} drops index {ix['name']!r} on {tname!r} "
                         "but no such index exists")
        for a in frag.get('associations', []):
            if a.get('drop') is True:
                idents = [association_key(tname, x)
                          for x in base.get(tname, {}).get('associations', [])]
                if not any(_config_assoc_drop_matches(a, i) for i in idents):
                    sys.exit(f"Error: {label} drops an association on {tname!r} "
                             "but no matching association exists")

def validate_config_references(config_tables, tables, label):
    """Semantic validation (§6.4②) of config.tables references against the FINAL
    merged IR: every config-declared association `target` must be an existing
    table, and every config-declared `primary_key` column must exist in that
    table's final columns. A config-ADDED table/column is already merged in, so
    referencing it is valid (self- and cross-references included). Runs AFTER
    the final merge. Hard error via sys.exit."""
    for tname, frag in config_tables.items():
        if not isinstance(frag, dict) or frag.get('drop') is True or tname not in tables:
            continue
        col_names = {c['name'] for c in tables[tname]['columns']}
        pk = frag.get('primary_key')
        if pk is not None:
            for n in ([pk] if isinstance(pk, str) else pk):
                if n not in col_names:
                    sys.exit(f"Error: {label} table {tname!r} primary_key names column {n!r} "
                             "which does not exist in the merged schema")
        for a in frag.get('associations', []):
            if a.get('drop') is True:
                continue
            target = a.get('target')
            if target and target not in tables:
                sys.exit(f"Error: {label} association {a.get('name')!r} on {tname!r} "
                         f"references unknown target table {target!r}")
            # a declared foreign_key must name a real column on the SOURCE table
            fk = a.get('foreign_key')
            if fk and fk not in col_names:
                sys.exit(f"Error: {label} association {a.get('name')!r} on {tname!r} "
                         f"declares foreign_key {fk!r} which does not exist in "
                         f"{tname!r}'s merged columns")

# ---------------------------------------------------------------------------
# Excel export (.xlsx via zipfile — no third-party dependency)
# ---------------------------------------------------------------------------
def _xml(s):
    return (str(s).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))

def _sheet_xml(rows, widths=None, links=None):
    """rows: list of rows; each cell is a value or (value, style_idx).
    links: [(cell_ref, target_sheet)] internal hyperlinks."""
    cols = ''
    if widths:
        cols = '<cols>' + ''.join(
            f'<col min="{i+1}" max="{i+1}" width="{w}" customWidth="1"/>'
            for i, w in enumerate(widths)) + '</cols>'
    body = []
    for r, row in enumerate(rows, 1):
        cells = []
        for c, cell in enumerate(row):
            val, style = cell if isinstance(cell, tuple) else (cell, 0)
            if val is None or val == '':
                # an empty value with a style (border/zebra-fill role) still
                # needs to exist as a cell, or the styling — the whole point
                # of a 5-role stylesheet — has visible holes wherever a
                # column happens to be blank (very common: Default/Extra/
                # Comment are empty far more often than not)
                if style:
                    ref = f'{_col_letter(c)}{r}'
                    cells.append(f'<c r="{ref}" s="{style}"/>')
                continue
            ref = f'{_col_letter(c)}{r}'
            s = f' s="{style}"' if style else ''
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                cells.append(f'<c r="{ref}"{s}><v>{val}</v></c>')
            else:
                cells.append(f'<c r="{ref}"{s} t="inlineStr"><is><t xml:space="preserve">{_xml(val)}</t></is></c>')
        body.append(f'<row r="{r}">' + ''.join(cells) + '</row>')
    hl = ''
    if links:
        hl = '<hyperlinks>' + ''.join(
            f'<hyperlink ref="{ref}" location="{_xml(loc)}!A1" display="{_xml(disp)}"/>'
            for ref, loc, disp in links) + '</hyperlinks>'
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            + cols + '<sheetData>' + ''.join(body) + '</sheetData>' + hl + '</worksheet>')

def _col_letter(idx):
    s = ''
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s

def _sheet_name(name, used):
    clean = re.sub(r"[\[\]:*?/\\']", '_', name)[:31] or 'sheet'
    base, n = clean, 2
    while clean.lower() in used:
        suffix = f'~{n}'
        clean = base[:31 - len(suffix)] + suffix
        n += 1
    used.add(clean.lower())
    return clean


# Style role indices used throughout write_excel's row-building — kept as
# named constants (rather than the old bare HDR=1) because there are now
# six distinct looks instead of two, and "3" or "4" alone reads as noise
# at every call site. Role 0 ("default") is never assigned explicitly;
# it's simply what a cell looks like with no style index at all.
S_TITLE, S_HEADER, S_DATA, S_DATA_ALT, S_SECTION = 1, 2, 3, 4, 5
_ROLES = (S_TITLE, S_HEADER, S_DATA, S_DATA_ALT, S_SECTION)

def _default_role_styles():
    """role (1-5) -> (font, fill, border) XML fragment dict for the
    built-in look. Colors mirror the HTML UI's own slate palette
    (#1e293b header/title, #cbd5e1 borders, #f8fafc/#f1f5f9 light fills)
    for a family resemblance between the diagram and the workbook."""
    plain_font = '<font><sz val="11"/><name val="Calibri"/></font>'
    none_fill = '<fill><patternFill patternType="none"/></fill>'
    no_border = '<border><left/><right/><top/><bottom/><diagonal/></border>'
    thin = '<color rgb="FFCBD5E1"/>'
    thin_border = (f'<border><left style="thin">{thin}</left><right style="thin">{thin}</right>'
                    f'<top style="thin">{thin}</top><bottom style="thin">{thin}</bottom><diagonal/></border>')
    header_fill = ('<fill><patternFill patternType="solid"><fgColor rgb="FF1E293B"/>'
                    '<bgColor indexed="64"/></patternFill></fill>')
    alt_fill = ('<fill><patternFill patternType="solid"><fgColor rgb="FFF8FAFC"/>'
                '<bgColor indexed="64"/></patternFill></fill>')
    section_fill = ('<fill><patternFill patternType="solid"><fgColor rgb="FFF1F5F9"/>'
                     '<bgColor indexed="64"/></patternFill></fill>')
    return {
        S_TITLE:    ('<font><b/><sz val="14"/><color rgb="FF1E293B"/><name val="Calibri"/></font>',
                      none_fill, no_border),
        S_HEADER:   ('<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>',
                      header_fill, thin_border),
        S_DATA:     (plain_font, none_fill, thin_border),
        S_DATA_ALT: (plain_font, alt_fill, thin_border),
        S_SECTION:  ('<font><b/><sz val="11"/><color rgb="FF1E293B"/><name val="Calibri"/></font>',
                      section_fill, no_border),
    }

def _strip_ns_tags(elem):
    """In-place: rewrite every tag in the subtree from '{uri}local' to
    'local'. ElementTree's built-in namespace handling is otherwise all
    prefix-preserving noise once serialized back out — this makes the
    extracted fragments splice cleanly into our own hand-written XML."""
    for e in elem.iter():
        e.tag = e.tag.rsplit('}', 1)[-1]
    return elem

def _extract_template_role_styles(template_path):
    """role (1-5) -> (font, fill, border) XML fragments, extracted from a
    user-supplied .xlsx template. Contract: the template's FIRST
    worksheet, column A, rows 1-5, hold cells styled as
    title/header/data/data-alt/section respectively (column B is free for
    a human-readable label, so the template self-documents when opened in
    Excel). A role whose contract cell is missing falls back to the
    built-in style for that one role, with a warning on stderr — only a
    template that can't be read as a .xlsx at all is a hard error, since
    the user asked for it explicitly and a silent fallback would hide
    their mistake."""
    import zipfile
    import xml.etree.ElementTree as ET
    try:
        zf = zipfile.ZipFile(template_path)
    except (FileNotFoundError, zipfile.BadZipFile, IsADirectoryError, PermissionError) as e:
        sys.exit(f'Error: --excel-template {template_path!s} is not a readable .xlsx file ({e})')
    try:
        wb_root = _strip_ns_tags(ET.fromstring(zf.read('xl/workbook.xml')))
        rels_root = _strip_ns_tags(ET.fromstring(zf.read('xl/_rels/workbook.xml.rels')))
    except KeyError as e:
        sys.exit(f'Error: --excel-template {template_path!s} is missing {e} — not a valid .xlsx')
    first_sheet = wb_root.find('./sheets/sheet')
    if first_sheet is None:
        sys.exit(f'Error: --excel-template {template_path!s} has no worksheets')
    rid = next(v for k, v in first_sheet.attrib.items() if k.rsplit('}', 1)[-1] == 'id')
    rel = next((r for r in rels_root.findall('Relationship') if r.get('Id') == rid), None)
    if rel is None:
        sys.exit(f'Error: --excel-template {template_path!s} has a broken worksheet relationship')
    # a rels Target is either package-absolute ("/xl/worksheets/sheet1.xml",
    # openpyxl's own convention) or relative to xl/ ("worksheets/sheet1.xml",
    # the more common convention, and what this file's own writer emits) —
    # both are valid per the OOXML spec, so accept either
    target = rel.get('Target')
    sheet_path = target.lstrip('/') if target.startswith('/') else 'xl/' + target

    sheet_root = _strip_ns_tags(ET.fromstring(zf.read(sheet_path)))
    styles_root = _strip_ns_tags(ET.fromstring(zf.read('xl/styles.xml')))
    tpl_cellxfs = styles_root.findall('./cellXfs/xf')
    tpl_fonts = styles_root.findall('./fonts/font')
    tpl_fills = styles_root.findall('./fills/fill')
    tpl_borders = styles_root.findall('./borders/border')

    def _at(elems, idx):  # bounds-checked list lookup — a hand-edited or
        return elems[idx] if 0 <= idx < len(elems) else None  # corrupted template can reference an id that doesn't exist

    defaults = _default_role_styles()
    out = {}
    for row_n, role in enumerate(_ROLES, 1):
        cell = sheet_root.find(f'.//c[@r="A{row_n}"]')
        xf_idx = int(cell.get('s', '0')) if cell is not None else None
        if xf_idx is None or xf_idx >= len(tpl_cellxfs):
            if cell is None:
                print(f'Warning: --excel-template has no cell A{row_n} — role {role} '
                      'falls back to the built-in style', file=sys.stderr)
            elif xf_idx is not None:
                print(f'Warning: --excel-template cell A{row_n} references a style that '
                      f"doesn't exist in the template — role {role} falls back to the "
                      'built-in style', file=sys.stderr)
            out[role] = defaults[role]
            continue
        xf = tpl_cellxfs[xf_idx]
        font = _at(tpl_fonts, int(xf.get('fontId', 0)))
        fill = _at(tpl_fills, int(xf.get('fillId', 0)))
        border = _at(tpl_borders, int(xf.get('borderId', 0)))
        d_font, d_fill, d_border = defaults[role]
        out[role] = (
            ET.tostring(font, encoding='unicode') if font is not None else d_font,
            ET.tostring(fill, encoding='unicode') if fill is not None else d_fill,
            ET.tostring(border, encoding='unicode') if border is not None else d_border,
        )
    theme = zf.read('xl/theme/theme1.xml').decode('utf-8') if 'xl/theme/theme1.xml' in zf.namelist() else None
    return out, theme

def _build_stylesheet_parts(role_styles):
    """role (1-5) -> (font,fill,border) dict -> (fonts,fills,borders,
    cellxfs) XML fragment lists, in the fixed layout every generated
    workbook uses: fills[0]/[1] reserved as none/gray125 (Excel treats
    their absence as a corrupt file, regardless of whether any cell
    references them), font/fill/border index 0 is the plain unstyled
    default, then one fresh slot per role in _ROLES order — so role N's
    cellxfs entry is always at index N, matching the S_* constants.
    Returned as lists (not joined strings) so the caller can both count
    and concatenate them for the count="N" attributes OOXML requires."""
    fonts = ['<font><sz val="11"/><name val="Calibri"/></font>']
    fills = ['<fill><patternFill patternType="none"/></fill>',
             '<fill><patternFill patternType="gray125"/></fill>']
    borders = ['<border><left/><right/><top/><bottom/><diagonal/></border>']
    cellxfs = ['<xf xfId="0"/>']
    for role in _ROLES:
        font, fill, border = role_styles[role]
        fonts.append(font); fills.append(fill); borders.append(border)
        fi, fli, bi = len(fonts)-1, len(fills)-1, len(borders)-1
        cellxfs.append(f'<xf xfId="0" fontId="{fi}" fillId="{fli}" borderId="{bi}" '
                        'applyFont="1" applyFill="1" applyBorder="1"/>')
    return fonts, fills, borders, cellxfs

def write_excel(tables, path, title, template_path=None):
    import zipfile
    used = set()
    sheets = []  # (sheet_name, xml)
    role_styles, theme_xml = (
        _extract_template_role_styles(template_path) if template_path
        else (_default_role_styles(), None))
    fonts, fills, borders, cellxfs = _build_stylesheet_parts(role_styles)

    def alt(i):  # zebra stripe: 0-indexed data row -> data/data-alt role
        return S_DATA_ALT if i % 2 == 1 else S_DATA

    # ── overview sheet ──
    used.add('tables')
    names = sorted(tables)
    sheet_of = {n: _sheet_name(n, used) for n in names}
    rows = [[(f'{title} — table definitions', S_TITLE)], [],
            [('#', S_HEADER), ('Table', S_HEADER), ('Comment', S_HEADER),
             ('Columns', S_HEADER), ('Indexes', S_HEADER), ('Missing schema', S_HEADER)]]
    links = []
    for i, n in enumerate(names, 1):
        t = tables[n]
        r = len(rows) + 1
        s = alt(i - 1)
        rows.append([(i, s), (n, s), (t.get('comment', ''), s), (len(t['columns']), s),
                     (len(t.get('indexes', [])), s),
                     ('yes' if t.get('schema_missing') else '', s)])
        links.append((f'B{r}', f"'{sheet_of[n]}'", n))
    overview = _sheet_xml(rows, widths=[5, 32, 50, 10, 10, 14], links=links)

    # ── per-table sheets ──
    for n in names:
        t = tables[n]
        rows = [[('Table', S_HEADER), n],
                [('Comment', S_HEADER), t.get('comment', '')],
                [],
                [('#', S_HEADER), ('Column', S_HEADER), ('Type', S_HEADER), ('Nullable', S_HEADER),
                 ('Default', S_HEADER), ('Key', S_HEADER), ('Extra', S_HEADER), ('Comment', S_HEADER)]]
        fk_cols = set(t.get('fk_columns') or
                      {a.get('foreign_key') for a in t['associations'] if a.get('foreign_key')})
        for i, c in enumerate(t['columns'], 1):
            key = 'PK' if c.get('primary') else ('FK' if c['name'] in fk_cols else '')
            s = alt(i - 1)
            rows.append([(i, s), (c['name'], s), (c.get('sql_type', c['type']), s),
                         ('YES' if c['nullable'] else 'NO', s),
                         (c.get('default', ''), s), (key, s), (c.get('extra', ''), s),
                         (c.get('comment', ''), s)])
        if t.get('indexes'):
            rows += [[], [('Indexes', S_SECTION)],
                     [('Name', S_HEADER), ('Columns', S_HEADER), ('Unique', S_HEADER)]]
            for i, ix in enumerate(t['indexes']):
                s = alt(i)
                rows.append([(ix['name'], s), (', '.join(ix['columns']), s),
                             ('UNIQUE' if ix['unique'] else '', s)])
        if t['associations']:
            rows += [[], [('Associations', S_SECTION)],
                     [('Type', S_HEADER), ('Name', S_HEADER), ('Target', S_HEADER), ('Via', S_HEADER)]]
            for i, a in enumerate(t['associations']):
                via = ('DB FK' if a.get('db_fk') else
                       'inferred' if a.get('inferred') else
                       'manual' if a.get('manual') else 'code')
                s = alt(i)
                rows.append([(a['type'], s), (a['name'], s), (a['target'], s), (via, s)])
        sheets.append((sheet_of[n],
                       _sheet_xml(rows, widths=[12, 28, 24, 10, 18, 6, 16, 50])))
    sheets.insert(0, ('Tables', overview))

    # ── workbook plumbing ──
    sheet_entries = ''.join(
        f'<sheet name="{_xml(nm)}" sheetId="{i+1}" r:id="rId{i+1}"/>'
        for i, (nm, _) in enumerate(sheets))
    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets>{sheet_entries}</sheets></workbook>')
    styles_rid = f'rId{len(sheets)+1}'
    theme_rid = f'rId{len(sheets)+2}'
    wb_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + ''.join(f'<Relationship Id="rId{i+1}" '
                  'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                  f'Target="worksheets/sheet{i+1}.xml"/>' for i in range(len(sheets)))
        + f'<Relationship Id="{styles_rid}" '
          'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
          'Target="styles.xml"/>'
        + (f'<Relationship Id="{theme_rid}" '
           'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" '
           'Target="theme/theme1.xml"/>' if theme_xml else '')
        + '</Relationships>')
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<fonts count="{len(fonts)}">{"".join(fonts)}</fonts>'
        f'<fills count="{len(fills)}">{"".join(fills)}</fills>'
        f'<borders count="{len(borders)}">{"".join(borders)}</borders>'
        '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
        f'<cellXfs count="{len(cellxfs)}">{"".join(cellxfs)}</cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '</styleSheet>')
    content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        + ''.join(f'<Override PartName="/xl/worksheets/sheet{i+1}.xml" '
                  'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                  for i in range(len(sheets)))
        + ('<Override PartName="/xl/theme/theme1.xml" '
           'ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>' if theme_xml else '')
        + '</Types>')
    root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>')

    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', content_types)
        z.writestr('_rels/.rels', root_rels)
        z.writestr('xl/workbook.xml', workbook)
        if theme_xml:
            z.writestr('xl/theme/theme1.xml', theme_xml)
        z.writestr('xl/_rels/workbook.xml.rels', wb_rels)
        z.writestr('xl/styles.xml', styles)
        for i, (_, xml) in enumerate(sheets):
            z.writestr(f'xl/worksheets/sheet{i+1}.xml', xml)

# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------
HTML_TEMPLATE = r"""__ERDSCOPE_VIEWER_TEMPLATE__"""

# ---------------------------------------------------------------------------
# Config file
# ---------------------------------------------------------------------------
# CLI flags mirrored by config keys of the same name -> (default value if
# neither config nor CLI supplies one). `relations` is config-only (no CLI
# equivalent — a list of individual FK declarations doesn't fit a flag).
CONFIG_DEFAULTS = {
    'output': 'erd.html', 'models': None, 'excel': None, 'excel_template': None,
    'max_rows': 15, 'only': None, 'exclude': None, 'infer_fk': False, 'table_map': {},
}
# Connection fields, broken apart rather than one mysql:// URL string —
# there's deliberately no password/url field: a single URL is one string
# away from someone pasting in a password, but there's no literal field to
# accidentally fill in when the pieces are separate. One config file per
# database (e.g. erdscope.staging.json / erdscope.prod.json) is the
# intended way to point --config at different targets.
CONFIG_CONNECTION_KEYS = {'engine', 'host', 'port', 'user', 'database'}
CONFIG_PASSWORD_KEYS = {'password', 'passwd', 'pwd', 'url', 'database_url'}
# Schema-input keys (REFACTOR_PLAN.md §6.2): `tables` (a map table_name ->
# TableFragment) and `title`. `name` is deliberately NOT accepted at the top
# level (§6.2/§18) — it's overloaded for tables/columns/associations, so a
# stray top-level `name` is far more likely a mistake than a title. These are
# validated syntactically at load time but NOT yet wired into the pipeline
# (REFACTOR_PLAN.md §15 Step 3 is validation-only; construction/merge is Step 7).
CONFIG_SCHEMA_KEYS = {'tables', 'title'}
# The four association kinds and the two list-merge modes, reused by the
# recursive Fragment/DropOperation validators below.
_CONFIG_ASSOC_TYPES = {'has_many', 'belongs_to', 'has_one', 'has_and_belongs_to_many'}
_CONFIG_MODE_KEYS = ('columns_mode', 'indexes_mode', 'associations_mode')
# Fixed per-structure allow-lists for nested keys (typo protection, §6.4 "typo
# を黙って無視しない"). A misspelled nested key (`primary_ky`, `nulable`) is
# silently ignored otherwise. Deliberately spelled out here — NOT derived from
# the Step-2 contract types — so the accepted surface is explicit and stable
# regardless of how the internal IR shape evolves.
_CONFIG_TABLE_KEYS = {'comment', 'primary_key', 'columns', 'indexes', 'associations',
                      'drop', 'columns_mode', 'indexes_mode', 'associations_mode'}
_CONFIG_COLUMN_KEYS = {'name', 'type', 'sql_type', 'nullable', 'primary',
                       'default', 'extra', 'comment', 'drop'}
_CONFIG_INDEX_KEYS = {'name', 'columns', 'unique', 'drop'}
_CONFIG_ASSOC_KEYS = {'type', 'name', 'target', 'foreign_key', 'through',
                      'polymorphic', 'drop'}

def _reject_unknown_keys(obj, allowed, path, where):
    unknown = set(obj) - allowed
    if unknown:
        sys.exit(f"Error: {path} `{where}`: unknown key(s): "
                 f"{', '.join(repr(k) for k in sorted(unknown))}")
# Expected type per key, checked at load time — YAML/JSON scalars are
# exactly where a config typo goes undetected otherwise: max_rows as the
# string "fifteen" would reach the JS as a bare identifier (ReferenceError,
# dead viewer); `only` as a bare string instead of a list gets iterated
# character-by-character by fnmatch (silently matches everything, filter
# does nothing); infer_fk as the string "false" is truthy. host/port/user/
# database get their own, more detailed checks in assemble_config_url().
# `models` is validated separately (below) because it accepts str OR list[str]
# — multiple frameworks (§18 #3 / §10), e.g. a Rails app AND a schema.prisma.
CONFIG_TYPES = {
    'output': str, 'excel': str, 'excel_template': str, 'max_rows': int,
    'only': list, 'exclude': list, 'infer_fk': bool, 'table_map': dict,
    'relations': list, 'title': str,
}

def load_config(args):
    """Load the config file: --config PATH if given, else auto-discovered
    .erdscope.json / .erdscope.yml / .erdscope.yaml in the cwd (in that
    order), unless --no-config. YAML needs PyYAML installed — JSON always
    works with no dependency, same "works out of the box, YAML if you
    already have it" spirit as the PyMySQL/mysql-CLI fallback above."""
    if getattr(args, 'no_config', False):
        if getattr(args, 'config', None):
            sys.exit('Error: --config and --no-config are mutually exclusive')
        return {}
    path = None
    if getattr(args, 'config', None):
        path = Path(args.config).expanduser().resolve()
        if not path.exists():
            sys.exit(f'Error: config file {path} does not exist')
    else:
        for candidate in ('.erdscope.json', '.erdscope.yml', '.erdscope.yaml'):
            c = Path.cwd() / candidate
            if c.exists():
                path = c
                break
    if path is None:
        return {}
    print(f'Using config: {path}', file=sys.stderr)
    text = path.read_text(encoding='utf-8')
    if path.suffix == '.json':
        try:
            config = json.loads(text)
        except json.JSONDecodeError as e:
            sys.exit(f'Error: failed to parse {path} as JSON: {e}')
    else:
        try:
            import yaml
        except ImportError:
            sys.exit(f'Error: {path} is YAML but PyYAML is not installed '
                      f'(pip install pyyaml, or use a .json config instead)')
        try:
            config = yaml.safe_load(text) or {}
        except yaml.YAMLError as e:
            sys.exit(f'Error: failed to parse {path} as YAML: {e}')
    if not isinstance(config, dict):
        sys.exit(f'Error: {path} must contain a JSON/YAML object at the top level')
    password_keys = CONFIG_PASSWORD_KEYS & set(config)
    if password_keys:
        sys.exit(f'Error: {path} has {", ".join(sorted(password_keys))} — passwords '
                 f'(or a full connection URL, which could carry one) are not supported '
                 f'in the config file. Use `host`/`port`/`user`/`database` instead, and '
                 f'MYSQL_PWD, ~/.my.cnf, or the interactive prompt for the password')
    unknown = (set(config) - set(CONFIG_DEFAULTS) - {'relations'}
               - CONFIG_CONNECTION_KEYS - CONFIG_SCHEMA_KEYS)
    if unknown:
        sys.exit(f'Error: {path} has unknown key(s): {", ".join(sorted(unknown))}')
    _check_config_types(config, path)
    return config

def _check_config_types(config, path):
    for key, expected in CONFIG_TYPES.items():
        if key not in config:
            continue
        val = config[key]
        # bool is a subclass of int in Python, so an explicit isinstance(int)
        # check would let `max_rows: true` through as 1 — and the reverse,
        # `infer_fk: 1`, needs the same explicit guard to be rejected
        ok = (isinstance(val, bool) if expected is bool else
              isinstance(val, int) and not isinstance(val, bool) if expected is int else
              isinstance(val, expected))
        if not ok:
            sys.exit(f'Error: {path} `{key}` must be a {expected.__name__}, got {val!r}')
    # models: a single path (str) or a list of paths (str) — multiple frameworks
    if 'models' in config:
        m = config['models']
        if isinstance(m, str):
            pass
        elif isinstance(m, list):
            for i, item in enumerate(m):
                if not isinstance(item, str):
                    sys.exit(f'Error: {path} `models[{i}]` must be a string, got {item!r}')
        else:
            sys.exit(f'Error: {path} `models` must be a string or a list of strings, '
                     f'got {m!r}')
    for key in ('only', 'exclude'):
        if key in config and any(not isinstance(x, str) for x in config[key]):
            sys.exit(f'Error: {path} `{key}` must be a list of strings')
    if 'table_map' in config and any(not isinstance(v, str) for v in config['table_map'].values()):
        sys.exit(f'Error: {path} `table_map` values must all be strings')
    if 'relations' in config and any(not isinstance(r, dict) for r in config['relations']):
        sys.exit(f'Error: {path} `relations` must be a list of objects '
                 '({table, column, references, ...})')
    if 'tables' in config:
        _check_config_tables(config['tables'], path)

# ---------------------------------------------------------------------------
# `tables:` schema-input syntactic validation (REFACTOR_PLAN.md §4.3 / §6.4 ①)
#
# STRICTLY SYNTACTIC (P0-1): these checks need no DB/Framework IR — they verify
# shape, required fields, types, *_mode values, DropOperation identity, and
# Config-internal duplicates only. They must NOT check whether a referenced
# table/column/target actually exists anywhere — that is *semantic* validation
# (§6.4 ②) and runs at apply time (Step 7), once every provider's IR is
# collected. A config that drops or references a not-yet-known table/column is
# valid here on purpose.
# ---------------------------------------------------------------------------
def _check_config_tables(tables, path):
    if not isinstance(tables, dict):
        sys.exit(f'Error: {path} `tables` must be a map of table_name -> table '
                 'definition (an object), not a list or scalar')
    for tname, tdef in tables.items():
        where = f'tables.{tname}'
        if not isinstance(tdef, dict):
            sys.exit(f'Error: {path} `{where}` must be an object')
        _reject_unknown_keys(tdef, _CONFIG_TABLE_KEYS, path, where)
        _check_bool(tdef.get('drop'), 'drop' in tdef, path, f'{where}.drop')
        # comment: str | null (null/"" = explicit delete, a valid Config op)
        if 'comment' in tdef and tdef['comment'] is not None and not isinstance(tdef['comment'], str):
            sys.exit(f'Error: {path} `{where}.comment` must be a string or null')
        # primary_key: str | list[str] | null (list = composite PK, §4.2/§6.9)
        if 'primary_key' in tdef:
            pk = tdef['primary_key']
            if not (pk is None or isinstance(pk, str)
                    or (isinstance(pk, list) and all(isinstance(x, str) for x in pk))):
                sys.exit(f'Error: {path} `{where}.primary_key` must be a string, a list of '
                         'strings (composite PK), or null')
        for mk in _CONFIG_MODE_KEYS:
            if mk in tdef and tdef[mk] not in ('merge', 'replace'):
                sys.exit(f'Error: {path} `{where}.{mk}` must be "merge" or "replace", '
                         f'got {tdef[mk]!r}')
        if 'columns' in tdef:
            _check_config_columns(tdef['columns'], path, where)
        if 'indexes' in tdef:
            _check_config_indexes(tdef['indexes'], path, where)
        if 'associations' in tdef:
            _check_config_associations(tdef['associations'], path, where)

def _check_bool(val, present, path, where):
    if present and not isinstance(val, bool):
        sys.exit(f'Error: {path} `{where}` must be true or false, got {val!r}')

def _check_config_columns(columns, path, where):
    if not isinstance(columns, list):
        sys.exit(f'Error: {path} `{where}.columns` must be a list')
    seen = set()
    for i, col in enumerate(columns):
        cw = f'{where}.columns[{i}]'
        if not isinstance(col, dict):
            sys.exit(f'Error: {path} `{cw}` must be an object')
        _reject_unknown_keys(col, _CONFIG_COLUMN_KEYS, path, cw)
        _check_bool(col.get('drop'), 'drop' in col, path, f'{cw}.drop')
        # `name` identifies the column for BOTH a fragment (add/override) and a
        # drop, so it's required either way (§4.3: ColumnFragment / ColumnDrop)
        name = col.get('name')
        if not isinstance(name, str) or not name:
            sys.exit(f'Error: {path} `{cw}` needs a non-empty string `name` '
                     f'({"to identify the column to drop" if col.get("drop") is True else "for the column"})')
        if name in seen:
            sys.exit(f'Error: {path} `{where}.columns` has a duplicate column name {name!r}')
        seen.add(name)
        if col.get('drop') is True:
            continue  # ColumnDrop = { name, drop: true }; no other fields required
        for k in ('type', 'sql_type', 'default', 'extra'):
            if k in col and not isinstance(col[k], str):
                sys.exit(f'Error: {path} `{cw}.{k}` must be a string')
        for k in ('nullable', 'primary'):
            _check_bool(col.get(k), k in col, path, f'{cw}.{k}')
        if 'comment' in col and col['comment'] is not None and not isinstance(col['comment'], str):
            sys.exit(f'Error: {path} `{cw}.comment` must be a string or null')

def _check_config_indexes(indexes, path, where):
    if not isinstance(indexes, list):
        sys.exit(f'Error: {path} `{where}.indexes` must be a list')
    seen = set()
    for i, ix in enumerate(indexes):
        iw = f'{where}.indexes[{i}]'
        if not isinstance(ix, dict):
            sys.exit(f'Error: {path} `{iw}` must be an object')
        _reject_unknown_keys(ix, _CONFIG_INDEX_KEYS, path, iw)
        _check_bool(ix.get('drop'), 'drop' in ix, path, f'{iw}.drop')
        # Config indexes are name-mandatory (§7.4) — the name is the identity
        # key for both fragment and drop (§4.3: IndexFragment / IndexDrop)
        name = ix.get('name')
        if not isinstance(name, str) or not name:
            sys.exit(f'Error: {path} `{iw}` needs a non-empty string `name` '
                     '(config indexes must be named)')
        if name in seen:
            sys.exit(f'Error: {path} `{where}.indexes` has a duplicate index name {name!r}')
        seen.add(name)
        if ix.get('drop') is True:
            continue  # IndexDrop = { name, drop: true }
        cols = ix.get('columns')
        if (not isinstance(cols, list) or not cols
                or any(not isinstance(c, str) for c in cols)):
            sys.exit(f'Error: {path} `{iw}.columns` must be a non-empty list of strings')
        _check_bool(ix.get('unique'), 'unique' in ix, path, f'{iw}.unique')

def _config_assoc_identity(a):
    """Stable identity for an association fragment/drop (§8.1), used only for
    Config-internal duplicate detection. FK-holding side is keyed by its FK
    column + target + name; a collection/inverse (no FK) by type + target + name.
    `name` is part of BOTH so it aligns with the runtime association_key (6b):
    the Rails alias pattern (`user` AND `author`, both on `user_id` -> `users`)
    is two distinct associations and must not be rejected as a duplicate. An
    exact duplicate (same name+fk+target) is still caught."""
    if a.get('foreign_key'):
        return ('fk', a.get('target'), a.get('foreign_key'), a.get('name'))
    return ('rel', a.get('type'), a.get('target'), a.get('name'))

def _check_config_associations(assocs, path, where):
    if not isinstance(assocs, list):
        sys.exit(f'Error: {path} `{where}.associations` must be a list')
    seen = set()
    for i, a in enumerate(assocs):
        aw = f'{where}.associations[{i}]'
        if not isinstance(a, dict):
            sys.exit(f'Error: {path} `{aw}` must be an object')
        _reject_unknown_keys(a, _CONFIG_ASSOC_KEYS, path, aw)
        _check_bool(a.get('drop'), 'drop' in a, path, f'{aw}.drop')
        # foreign_key is single-column only this release (§4.2/§18): a list is
        # a composite FK — reject it explicitly rather than silently mishandle
        if 'foreign_key' in a and a['foreign_key'] is not None:
            fk = a['foreign_key']
            if isinstance(fk, list):
                sys.exit(f'Error: {path} `{aw}.foreign_key` is a list — composite foreign '
                         'keys are not supported in this release; use a single column name')
            if not isinstance(fk, str) or not fk:
                sys.exit(f'Error: {path} `{aw}.foreign_key` must be a single column name (string)')
        if a.get('type') is not None and a.get('type') not in _CONFIG_ASSOC_TYPES:
            sys.exit(f'Error: {path} `{aw}.type` must be one of '
                     f'{", ".join(sorted(_CONFIG_ASSOC_TYPES))}, got {a.get("type")!r}')
        for k in ('name', 'target', 'through'):
            if k in a and a[k] is not None and not isinstance(a[k], str):
                sys.exit(f'Error: {path} `{aw}.{k}` must be a string')
        _check_bool(a.get('polymorphic'), 'polymorphic' in a, path, f'{aw}.polymorphic')

        if a.get('drop') is True:
            # AssociationDrop identity is role-dependent (§4.3): the FK-holding
            # side needs target+foreign_key; a collection/inverse needs
            # type+target+name. `name` is NOT unconditionally required here (it
            # is for a fragment) — that's the Fragment-vs-Drop required-field split.
            if a.get('foreign_key'):
                if not a.get('target'):
                    sys.exit(f'Error: {path} `{aw}` is an FK-holding association drop but is '
                             'missing `target` — need `target`+`foreign_key` to identify it')
            else:
                missing = [k for k in ('type', 'target', 'name') if not a.get(k)]
                if missing:
                    sys.exit(f'Error: {path} `{aw}` cannot identify the association to drop: '
                             'give `target`+`foreign_key` (FK-holding side) or '
                             '`type`+`target`+`name` (collection/inverse); '
                             f'missing {", ".join(missing)}')
        else:
            # AssociationFragment (add/override): type, name, target all required
            missing = [k for k in ('type', 'name', 'target') if not a.get(k)]
            if missing:
                sys.exit(f'Error: {path} `{aw}` is missing required field(s): '
                         f'{", ".join(missing)} (an association needs type, name, target)')
        identity = _config_assoc_identity(a)
        if identity in seen:
            sys.exit(f'Error: {path} `{where}.associations` has a duplicate association '
                     f'(same identity {identity!r})')
        seen.add(identity)

_SAFE_HOST_OR_USER = re.compile(r'[\w.\-]+')

def assemble_config_url(config):
    """Build a mysql:// or postgres:// URL (per the config's `engine`, default
    mysql) from the config's host/port/user/database fields, or None if
    `database` wasn't given (no connection info in the config at all).
    Each part is validated against a safe charset before being pasted
    into the URL string — host/user containing `/`, `@`, or `:` would
    silently shift what urlparse reads as the host/port/path when the
    assembled string is re-parsed downstream (verified empirically: a host
    of "x@evil" produces a URL whose username becomes "x" and whose actual
    host becomes "evil"), and there is no decoding step anywhere downstream
    to undo percent-encoding, so quoting isn't a fix either."""
    engine = config.get('engine', 'mysql')
    if engine not in ('mysql', 'postgres', 'postgresql'):
        sys.exit(f'Error: config `engine` must be "mysql" or "postgres", got {engine!r}')
    db = config.get('database')
    if db is None:  # absent, or explicitly blank (e.g. a bare `database:` in YAML)
        return None
    if not re.fullmatch(r'\w+', str(db)):
        sys.exit(f'Error: config `database` {db!r} is not a valid database name')
    host = config.get('host') or '127.0.0.1'
    if not _SAFE_HOST_OR_USER.fullmatch(str(host)):
        sys.exit(f'Error: config `host` {host!r} has unsupported characters (letters/'
                 f'digits/./- only here — IPv6 and other exotic hosts need the CLI '
                 f'argument instead, not the config file)')
    port = config.get('port', 3306 if engine == 'mysql' else 5432)
    if isinstance(port, bool) or (isinstance(port, float) and not port.is_integer()):
        sys.exit(f'Error: config `port` {port!r} is not a valid port number')
    try:
        port = int(port)
        if not (1 <= port <= 65535):
            raise ValueError
    except (TypeError, ValueError):
        sys.exit(f'Error: config `port` {port!r} is not a valid port number')
    auth = ''
    if config.get('user') is not None:
        user = str(config['user'])
        if ':' in user:
            sys.exit('Error: config `user` must not contain a password (no "user:pass" '
                     'syntax) — passwords are not supported in the config file')
        if not _SAFE_HOST_OR_USER.fullmatch(user):
            sys.exit(f'Error: config `user` {user!r} has unsupported characters '
                     f'(letters/digits/./- only)')
        auth = f'{user}@'
    scheme = 'mysql' if engine == 'mysql' else 'postgres'
    return f'{scheme}://{auth}{host}:{port}/{db}'

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _framework_project_name(mroot):
    """A meaningful project name for a --models path (§10 title fallback).
    Walk from a Rails app/models dir up to the project root, from a
    prisma/schema.prisma up to the project, and from a schema file up to its
    directory, then use the basename."""
    p = mroot
    if p.is_file():                                    # e.g. .../schema.prisma
        p = p.parent
    if p.name == 'models' and p.parent.name == 'app':  # Rails app/models
        p = p.parent.parent
    elif p.name == 'prisma':                           # .../<proj>/prisma
        p = p.parent
    return p.name or 'schema'

def _resolve_title(config, url, fw_root, output):
    """Workbook/HTML title precedence (§10): config.title > DB name >
    framework project name > output filename stem > "schema"."""
    if config.get('title'):
        return config['title']
    if url:
        db = urlparse(url).path.lstrip('/')
        if db:
            return db
    if fw_root is not None:
        return _framework_project_name(fw_root)
    if output:
        stem = Path(output).stem
        if stem:
            return stem
    return 'schema'

def main():
    p = argparse.ArgumentParser(
        description='Generate an interactive ER diagram from a live database, '
                    'optionally enriched with association semantics parsed from '
                    'application code (Rails / Prisma / Django)')
    p.add_argument('database', metavar='mysql://user@host/db | postgres://user@host/db',
                   nargs='?',
                   help='Database connection URL (postgres:// takes an optional '
                        '?schema=name, default public). Can also be assembled from '
                        '`engine`/`host`/`port`/`user`/`database` in the config file (no '
                        'password field there — use MYSQL_PWD/PGPASSWORD, '
                        '~/.my.cnf/~/.pgpass, or the interactive prompt). A read-only '
                        'account is recommended')
    # SUPPRESS on every config-mirrorable flag so we can tell "explicitly
    # passed on the CLI" (attribute present) from "left to the config file /
    # built-in default" (attribute absent) — see the merge loop below.
    p.add_argument('-o', '--output', default=argparse.SUPPRESS,
                   help='Output HTML file (default: erd.html)')
    p.add_argument('--models', metavar='PATH', action='append', default=argparse.SUPPRESS,
                   help='Merge association semantics parsed from application code '
                        '(Rails project/app/models dir, schema.prisma, or Django project). '
                        'Repeatable to merge several frameworks; later ones win on ties')
    p.add_argument('--excel', metavar='FILE.xlsx', default=argparse.SUPPRESS,
                   help='Also write a table-definition workbook '
                        '(overview sheet + one sheet per table)')
    p.add_argument('--excel-template', metavar='FILE.xlsx', default=argparse.SUPPRESS,
                   help="Override the workbook's colors/fonts/borders from a template "
                        '.xlsx — see excel-template.xlsx and its Styles sheet for the '
                        '5-cell contract (default: built-in styling)')
    p.add_argument('--max-rows', type=int, default=argparse.SUPPRESS,
                   help='Max column rows shown per table before scrolling (default: 15)')
    p.add_argument('--only', action='append', metavar='PATTERN', default=argparse.SUPPRESS,
                   help='Include only tables matching the glob pattern(s). '
                        'Repeatable; comma-separated lists accepted (e.g. --only "user*,post*")')
    p.add_argument('--exclude', action='append', metavar='PATTERN', default=argparse.SUPPRESS,
                   help='Exclude tables matching the glob pattern(s). Same syntax as --only')
    p.add_argument('--infer-fk', action='store_true', default=argparse.SUPPRESS,
                   help='Guess relations from *_id column names when no real '
                        'association/FK backs them (off by default: unbacked guesses '
                        'can be wrong, and both the FK badge and the "PK/FK" column '
                        'view only ever show columns from real associations)')
    p.add_argument('--table-map', action='append', metavar='Class=table', default=argparse.SUPPRESS,
                   help="Rails only: override a model's table when static analysis "
                        "can't determine it (e.g. table_name set inside a concern "
                        "that lives in a gem). Repeatable; comma-separated lists "
                        "accepted, e.g. --table-map 'Widget=crm_widgets,Foo=bar_table'")
    p.add_argument('--config', metavar='PATH',
                   help='Config file (JSON, or YAML if PyYAML is installed) providing '
                        'defaults for the options above, plus the DB connection as '
                        'host/port/user/database (no password field — see README) and '
                        '`relations` (manual FK declarations). An explicit CLI flag or '
                        'argument always wins over the same key in the config. '
                        'Auto-discovered as .erdscope.json/.yml/.yaml in the current '
                        'directory if not given')
    p.add_argument('--no-config', action='store_true',
                   help='Skip config auto-discovery even if .erdscope.* exists in the cwd')
    args = p.parse_args()

    config = load_config(args)
    url = args.database or assemble_config_url(config)
    # DB is optional now (§10): a schema can also come from --models and/or
    # config.tables. Only a NON-EMPTY url with an unrecognized scheme is an
    # error (a mistyped/wrong argument); a missing url just skips the DB layer.
    engine_name = None
    if url:
        scheme = url.split('://', 1)[0]
        if scheme == 'mysql':
            engine_name = 'MySQL'
        elif scheme in ('postgres', 'postgresql'):
            engine_name = 'PostgreSQL'
        else:
            sys.exit('Error: a database URL is required (mysql:// or postgres://) — pass it '
                     'as the CLI argument, or set `database` (and optionally engine/host/user/'
                     'port) in the config file, e.g. mysql://readonly@127.0.0.1:3306/myapp or '
                     'postgres://readonly@127.0.0.1:5432/myapp. Or run with no database at '
                     'all by supplying --models and/or a config file with a `tables:` section')

    if hasattr(args, 'table_map'):
        tm = {}
        for arg in args.table_map:
            for pair in arg.split(','):
                if not pair:
                    continue
                if '=' not in pair:
                    sys.exit(f"Error: --table-map expects Class=table, got {pair!r}")
                cls, tbl = pair.split('=', 1)
                tm[cls] = tbl
        args.table_map = tm
    for key, default in CONFIG_DEFAULTS.items():
        if not hasattr(args, key):  # not explicitly passed on the CLI
            setattr(args, key, config.get(key, default))
    # --models is repeatable (append) and config `models` may be str or list;
    # normalize both to a list of paths, in given order (§10: later wins on ties)
    if args.models is None:
        models_list = []
    elif isinstance(args.models, str):
        models_list = [args.models]
    else:
        models_list = list(args.models)
    relations = config.get('relations', [])  # shape already validated by load_config()
    config_tables = config.get('tables')     # shape already validated by load_config()
    cfg_label = str(args.config) if getattr(args, 'config', None) else 'config'
    cfg_location = str(args.config) if getattr(args, 'config', None) else None

    # ── valid-input check (§10): at least one SCHEMA source (DB / Framework /
    #    config.tables). relations alone is not a source — it needs a base. ──
    if not (url or models_list or config_tables):
        sys.exit('Error: no schema input. Provide at least one of: a database URL '
                 '(mysql:// or postgres://) as the argument or config `database`; '
                 '--models pointing at a Rails/Prisma/Django project; or a config file '
                 'with a `tables:` section.')

    # ── collect provider layers, low→high spec priority, then merge (§3) ──
    layers = []
    db_result = None
    if url:  # only build/connect the DB layer when a url is present (no
             # connection and no password prompt otherwise — §10)
        db_result = db_provider(url)
        print(f'Fetched {len(db_result["tables"])} tables from {engine_name}', file=sys.stderr)
        layers.append(db_result)

    fw_root = None
    for m in models_list:  # each --models / config `models` entry, in order
        mroot = Path(m).expanduser().resolve()
        if not mroot.exists():
            sys.exit(f'Error: {mroot} does not exist')
        fw = framework_provider(mroot, args.table_map)
        layers.append(fw)
        if fw_root is None:
            fw_root = mroot  # the first framework drives the title fallback (§10)
        print(f'Merged {fw["source"]["provider"]} associations from {mroot}', file=sys.stderr)

    # config.tables join as a top-priority config layer (add/override/drop/
    # replace — §6.2/§7). Its DROP ops are semantic-validated against the merged
    # db+framework base first (§6.4②: a drop must target a real lower-layer
    # item), then the layer is merged so its additions are visible to relations.
    if config_tables:
        base = merge_ir(layers)
        validate_config_drops(config_tables, base, cfg_label)
        layers = layers + [config_provider(config, location=cfg_location)]
        print(f'Applied config schema ({len(config_tables)} table entr'
              f'{"y" if len(config_tables) == 1 else "ies"})', file=sys.stderr)

    # config `relations` join as a further config layer (§8.6/P0-3: override,
    # not skip). They validate against — and detect single-column-unique FKs
    # (1:1) in — the merged base INCLUDING config.tables, so a relation may
    # reference a config-added table/column.
    if relations:
        base2 = merge_ir(layers)
        layers = layers + [relations_to_config_layer(relations, base2)]
        print(f'Applied {len(relations)} manual relation(s) from config', file=sys.stderr)

    # merge_ir runs Phase A (identity merge) + Phase B reconcile_db_fks and
    # derives fk_columns / schema_missing; the DB-FK "covered" count is the
    # drop in db_fk-flagged associations from the raw DB layer to the result.
    tables = merge_ir(layers)
    if db_result:  # count db_fk edges dropped from the raw DB layer to the merge.
        # The raw DB layer carries legacy db_fk booleans; the merged IR carries
        # provenance — _assoc_provenance reads either shape.
        covered = (sum(1 for t in db_result['tables'].values()
                       for a in t['associations'] if _assoc_provenance(a) == 'db_fk')
                   - sum(1 for t in tables.values()
                         for a in t['associations'] if _assoc_provenance(a) == 'db_fk'))
        if covered:
            print(f'{covered} DB FKs covered by explicit associations', file=sys.stderr)

    # §6.4②: config.tables references (association targets, primary_key columns)
    # must resolve in the FINAL merged IR (config-added tables/columns count).
    if config_tables:
        validate_config_references(config_tables, tables, cfg_label)

    _finish(tables, args, _resolve_title(config, url, fw_root, getattr(args, 'output', None)))

def serialize_for_viewer(tables):
    """Convert the internal merged IR to the shape the HTML viewer JSON and the
    Excel export consume (§9.3): each association's structured `provenance` /
    `sources` is replaced by the legacy boolean flag it maps to (db_fk / manual /
    inferred, or NO flag for 'declared'), and both internal keys are dropped, so
    the output carries EXACTLY today's fields — provenance/sources never leak
    into the viewer JSON.

    Handles BOTH IR shapes, so it is safe to call unconditionally:
      - merged IR (main pipeline): an association has `provenance` -> convert it.
      - legacy IR (the demo: gen_demo.py feeds mysql_ir output straight into
        _finish; parser output carries db_fk/inferred booleans and no
        provenance) -> pass the existing flags through unchanged.
    This dual handling is what keeps the demo byte-identical. Pure: returns a
    deep copy, never mutates the input."""
    out = copy.deepcopy(tables)
    for t in out.values():
        for a in t.get('associations', []):
            if 'provenance' in a:
                prov = a.pop('provenance')
                a.pop('sources', None)
                a.update(legacy_flags_for(prov))
    return out

def _finish(tables, args, title_name):
    """Shared tail: FK inference, --only/--exclude filtering, HTML generation."""
    if getattr(args, 'infer_fk', False):
        inferred = infer_fk_associations(tables)
        if inferred:
            print(f'Inferred {inferred} relations from *_id columns', file=sys.stderr)

    # single source of truth for "is this column really a foreign key" —
    # the FK badge and the PK/FK column view both read this instead of
    # guessing from the column name, so they can only ever show a column
    # that's backed by a real (declared, DB, or --infer-fk) association
    for t in tables.values():
        t['fk_columns'] = sorted({a['foreign_key'] for a in t['associations']
                                  if a.get('foreign_key')})

    def patterns(args_list):
        return [pat for arg in args_list for pat in arg.split(',') if pat]

    if args.only:
        pats = patterns(args.only)
        tables = {k: v for k, v in tables.items() if any(fnmatch(k, p) for p in pats)}
    if args.exclude:
        pats = patterns(args.exclude)
        tables = {k: v for k, v in tables.items() if not any(fnmatch(k, p) for p in pats)}
    if args.only or args.exclude:
        if not tables:
            sys.exit('Error: no tables left after --only/--exclude filtering')
        print(f'Filtered: {len(tables)} tables', file=sys.stderr)

    # §9.3 serialize boundary: convert the internal provenance/sources IR to
    # today's legacy-flag shape (a no-op pass-through for the already-legacy demo
    # IR), so BOTH the HTML DATA_JSON and the Excel export below see exactly the
    # fields they do today. This is the single point where provenance is undone.
    tables = serialize_for_viewer(tables)

    # Substitute the other placeholders BEFORE inserting DATA_JSON, and
    # escape `</` in the JSON — otherwise a table/column comment containing
    # a literal "__TITLE__"/"__MAX_ROWS__" would get rewritten by the later
    # .replace() calls, and one containing "</script>" would prematurely
    # close the script tag and blank the whole page. Both are realistic:
    # comments are free-text and come straight from the database.
    data_json = json.dumps({'tables': tables}, ensure_ascii=False).replace('</', '<\\/')
    html = (HTML_TEMPLATE
            .replace('__MAX_ROWS__', str(args.max_rows))
            .replace('__TITLE__', f'{title_name} — ERD')
            .replace('__DATA_JSON__', data_json))

    out = Path(args.output)
    out.write_text(html, encoding='utf-8')
    print(f'Generated: {out} ({out.stat().st_size // 1024} KB)', file=sys.stderr)

    if getattr(args, 'excel', None):
        write_excel(tables, Path(args.excel), title_name,
                    template_path=getattr(args, 'excel_template', None))
        print(f'Generated: {args.excel}', file=sys.stderr)
    elif getattr(args, 'excel_template', None):
        print('Warning: --excel-template has no effect without --excel', file=sys.stderr)

if __name__ == '__main__':
    main()
