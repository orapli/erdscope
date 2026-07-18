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
_PROVENANCE_TO_FLAG = {'db_fk': 'db_fk', 'manual': 'manual', 'inferred': 'inferred',
                       'schema_fk': 'schema_fk'}


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
    coexist: manual > db_fk > schema_fk > inferred; a bare association (no
    flag) is 'declared'. This is the read half of the legacy<->provenance
    seam."""
    if assoc.get('manual'):
        return 'manual'
    if assoc.get('db_fk'):
        return 'db_fk'
    if assoc.get('schema_fk'):
        return 'schema_fk'
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
# Database adapters — the DB layer's pluggable "URL scheme -> IR" surface.
#
# A DBAdapter turns a connection URL into the IR (the `tables` dict every
# downstream step consumes). The built-in engines live in the sibling db/*.py
# fragments — one file per engine — and each @register_adapter's its class under
# the URL scheme(s) it answers to. Adding an engine is "drop a db/<name>.py that
# subclasses DBAdapter and registers itself"; the build picks the folder up
# automatically (tools/build_single_file.py), and users can register the very
# same way at run time from a --adapter plugin file, without editing erdscope.
#
# This base file also holds the machinery the built-in adapters share: the
# information_schema-shaped IR builder (mysql_ir), the "is this FK 1:1" test,
# and the tab-separated-output unescaping the CLI fallbacks need. Those stay
# module-level free functions (not methods) on purpose — the test suite
# monkeypatches them by name, and gen_demo calls erd.mysql_ir directly.
# ---------------------------------------------------------------------------
DB_ADAPTERS = {}   # url scheme (lower-case) -> DBAdapter subclass


def register_adapter(cls):
    """Class decorator: register a DBAdapter subclass under each of its
    `schemes` (case-insensitively). Returns the class unchanged, so it can also
    be used directly. A later registration for a scheme replaces an earlier one,
    which is exactly what lets a --adapter plugin override a built-in engine."""
    for scheme in cls.schemes:
        DB_ADAPTERS[scheme.lower()] = cls
    return cls


class DBAdapter(abc.ABC):
    """Abstract base for a database adapter: read a live schema from a
    connection URL and return the IR (the `tables` dict mysql_ir builds).

    To add another engine, subclass this and:
      * set `schemes` to the URL scheme(s) it answers to, e.g. ('sqlite',);
      * set `name` to the short provider id recorded in the IR's Source (§5),
        e.g. 'sqlite' — it appears in provenance and the output;
      * optionally set `label` to a pretty display name for the progress line
        (defaults to `name`);
      * implement fetch(url) to return the IR;
      * decorate the class with @register_adapter.
    """
    schemes = ()      # URL schemes handled, e.g. ('postgres', 'postgresql')
    name = ''         # provider id for the ProviderResult Source (§5)
    label = ''        # pretty display name for the "Fetched from …" line

    @abc.abstractmethod
    def fetch(self, url):
        """Return the IR (`tables` dict) for `url`. Called once per run."""
        raise NotImplementedError


def db_adapter_for(scheme):
    """The registered DBAdapter subclass for a URL scheme (case-insensitive),
    or None if no adapter handles it."""
    return DB_ADAPTERS.get((scheme or '').lower())


def load_adapter_plugins(paths):
    """Import each user adapter plugin (a --adapter path / config `adapters`
    entry): a plain Python file that defines a DBAdapter subclass and registers
    it with @register_adapter. The plugin can `from erd import DBAdapter,
    register_adapter` — we alias the running erdscope module under both `erd`
    and `erdscope` first, so the plugin registers into the LIVE registry rather
    than a freshly re-imported second copy of it. Registration order is config
    entries then CLI ones, and a later entry overriding a scheme is intentional
    (last one wins), which is how a plugin can replace a built-in engine."""
    import importlib.util, types
    me = sys.modules.get(__name__)
    if me is None:
        # Exec'd without being registered in sys.modules (e.g. a test harness):
        # expose a stand-in whose namespace shares the live globals, so
        # `from erd import register_adapter` hands back the real, live objects
        # (register_adapter still mutates the live DB_ADAPTERS).
        me = types.ModuleType(__name__)
        me.__dict__.update(globals())
        sys.modules[__name__] = me
    for alias in ('erd', 'erdscope'):
        sys.modules.setdefault(alias, me)
    for i, path in enumerate(paths):
        p = Path(path).expanduser().resolve()
        if not p.exists():
            sys.exit(f'Error: --adapter plugin {p} does not exist')
        spec = importlib.util.spec_from_file_location(f'_erdscope_adapter_{i}', p)
        if spec is None or spec.loader is None:
            sys.exit(f'Error: could not load adapter plugin {p}')
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except SystemExit:
            raise
        except Exception as e:
            sys.exit(f'Error: adapter plugin {p} failed to import: {e}')


# --- Tab-separated CLI output unescaping (shared by the CLI fallbacks) ------
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
    # mysql --batch prints a SQL NULL as the bare, unescaped word `NULL`
    # (the \N escape is the SELECT ... INTO OUTFILE / mysqldump spelling,
    # not the batch client's; _unescape_tsv_field above still accepts it
    # harmlessly). Map the bare form to '' so the CLI fallback agrees with
    # the PyMySQL path (which maps None -> ''); without this, a NULL column
    # default surfaces as the literal string 'NULL' in the IR whenever the
    # CLI fallback is used. Deliberate, accepted ambiguity: in --batch
    # output an actual SQL NULL and a field whose text is the 4-letter word
    # NULL are byte-identical, so no unescaper can tell them apart — we
    # side with the (overwhelmingly more common) SQL NULL reading.
    if s == 'NULL':
        return ''
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

@register_adapter
class MySQLAdapter(DBAdapter):
    """MySQL / MariaDB, read from information_schema (PyMySQL, or the mysql CLI
    as a dependency-free fallback)."""
    schemes = ('mysql',)
    name = 'mysql'
    label = 'MySQL'

    def fetch(self, url):
        return parse_mysql(url)
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

@register_adapter
class PostgresAdapter(DBAdapter):
    """PostgreSQL, read from pg_catalog (psycopg/psycopg2, or the psql CLI as a
    dependency-free fallback)."""
    schemes = ('postgres', 'postgresql')
    name = 'postgres'
    label = 'PostgreSQL'

    def fetch(self, url):
        return parse_postgres(url)
# ---------------------------------------------------------------------------
# SQLite adapter (the sqlite3 stdlib module — always available, zero dependency)
# ---------------------------------------------------------------------------
def sqlite_path_from_url(url):
    """The database file path from a sqlite:// URL, SQLAlchemy-style:
    sqlite:///relative.db  -> 'relative.db'  (relative to the cwd)
    sqlite:////abs/path.db -> '/abs/path.db' (absolute)
    i.e. strip exactly one leading slash from the URL path."""
    p = urlparse(url).path
    return p[1:] if p.startswith('/') else p

def parse_sqlite(url):
    """Read the schema from a SQLite database file and build the IR.

    Uses only the sqlite3 stdlib module (no driver to install, no CLI
    fallback), opened read-only. Shapes PRAGMA results into the same
    information_schema-style rows mysql_ir() consumes, so PK detection,
    unique-index 1:1 promotion, and index assembly are shared with the other
    adapters. SQLite has no table/column comments, so those are empty; a
    column declared INTEGER PRIMARY KEY AUTOINCREMENT is marked in `extra`
    (like MySQL's auto_increment). Views and internal sqlite_* tables are
    excluded, matching the BASE TABLE spirit of the MySQL/Postgres adapters."""
    import sqlite3
    path = sqlite_path_from_url(url)
    if not path:
        sys.exit('Error: the sqlite URL must include a file path, '
                 'e.g. sqlite:///path/to/app.db')
    if not os.path.exists(path):
        sys.exit(f'Error: sqlite database file not found: {path}')
    try:
        conn = sqlite3.connect(f'{Path(path).resolve().as_uri()}?mode=ro', uri=True)
    except sqlite3.Error as e:
        sys.exit(f'Error: could not open sqlite database {path}: {e}')
    try:
        cur = conn.cursor()
        try:
            master = cur.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        except sqlite3.DatabaseError as e:
            # a non-SQLite / corrupt file is the most common failure here
            sys.exit(f'Error: {path} is not a readable SQLite database: {e}')
        table_rows = [(name, '') for name, _ in master]
        autoinc = {name: bool(sql) and 'AUTOINCREMENT' in sql.upper()
                   for name, sql in master}
        col_rows, fk_rows, index_rows = [], [], []
        for tname, _ in master:
            q = tname.replace('"', '""')  # identifier-quote for PRAGMA
            for cid, cname, ctype, notnull, dflt, pk in cur.execute(f'PRAGMA table_info("{q}")'):
                base = (ctype or '').split('(')[0].strip().lower()
                extra = 'autoincrement' if (pk and base == 'integer' and autoinc[tname]) else ''
                default = '' if dflt is None else str(dflt)
                if len(default) >= 2 and default[0] == "'" and default[-1] == "'":
                    default = default[1:-1]  # unquote a string-literal default
                # a PRIMARY KEY column is implicitly NOT NULL; SQLite's
                # table_info still reports notnull=0 for the INTEGER-PK rowid
                # alias, so force it for display parity with MySQL/Postgres
                col_rows.append((tname, cname, base, ctype or '',
                                 'NO' if (notnull or pk) else 'YES',
                                 'PRI' if pk else '', default, extra, ''))
            for row in cur.execute(f'PRAGMA foreign_key_list("{q}")'):
                # (id, seq, referenced_table, from_col, to_col, on_update, ...)
                fk_rows.append((tname, row[3], row[2]))
            for irow in cur.execute(f'PRAGMA index_list("{q}")'):
                iname, unique = irow[1], irow[2]
                iq = iname.replace('"', '""')
                for info in cur.execute(f'PRAGMA index_info("{iq}")'):
                    seqno, colname = info[0], info[2]
                    if colname is None:
                        continue  # expression index component — no column name
                    index_rows.append((tname, iname, 0 if unique else 1, seqno, colname))
    finally:
        conn.close()
    return mysql_ir(table_rows, col_rows, fk_rows, index_rows)

@register_adapter
class SQLiteAdapter(DBAdapter):
    """SQLite, read from a database file via the sqlite3 stdlib module
    (always available — nothing to install). URL: sqlite:///path/to/app.db."""
    schemes = ('sqlite',)
    name = 'sqlite'
    label = 'SQLite'

    def fetch(self, url):
        return parse_sqlite(url)
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
# DB is the physical truth; a static rails.schema parse sits below a live DB
# read but above framework code (a schema.rb dump is closer to the real
# database than an association declaration is). Framework code owns logical
# names, so schema.rb ranks below it there but still above the DB (an
# association's declared name beats a machine-derived one, which in turn beats
# a raw DB-FK-derived name).
_PHYSICAL_RANK = {'config': 4, 'db': 3, 'schema': 2, 'framework': 1}
_LOGICAL_RANK = {'config': 4, 'framework': 3, 'schema': 2, 'db': 1}
# Column attributes split by authority kind (§7.2). Everything not physical
# (i.e. `comment`) is logical.
_PHYSICAL_COL_ATTRS = ('type', 'sql_type', 'nullable', 'primary', 'default', 'extra')
# Deterministic column-attribute emit order (str-set iteration is hash-seed
# dependent, so never iterate a set for output order).
_COL_ATTR_ORDER = ('type', 'sql_type', 'nullable', 'primary', 'default', 'extra', 'comment')
# Representative-provenance precedence (§9.1): manual > declared > db_fk >
# schema_fk > inferred — a live DB FK is more authoritative than the same edge
# parsed statically out of schema.rb.
_PROV_PRECEDENCE = {'manual': 4, 'declared': 3, 'db_fk': 2, 'schema_fk': 1, 'inferred': 0}

def _pick_by_authority(contribs):
    """contribs: list of (rank, spec_order, value). Return the value with the
    greatest (rank, spec_order)."""
    return max(contribs, key=lambda c: (c[0], c[1]))[2]

def _pick_by_authority_warned(contribs, label):
    """Like _pick_by_authority, but warn when two SAME-rank layers disagree on
    the value — a silent last-wins among equals (§7/§10). The archetype is two
    --models frameworks (both kind='framework', so equal physical/logical rank)
    that each define `t.col` with a different type: the later one wins by spec
    order, and without this the override is invisible. A lower-rank contributor
    losing to a higher one is the authority ladder working as designed and never
    warns. `label` is a human field id like `table.column.type`."""
    winner = _pick_by_authority(contribs)
    top = max(c[0] for c in contribs)
    if any(c[0] == top and c[2] != winner for c in contribs):
        print(f"Warning: {label} has conflicting values from same-priority "
              f"sources; using {winner!r}", file=sys.stderr)
    return winner

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

def _merge_column(tname, name, contribs):
    """contribs: list of (kind, spec_order, column_dict) for one column name,
    in layer order. Physical attrs resolve Config>DB>Framework, `comment`
    resolves Config>Framework>DB, present-only (§7.2). A same-rank disagreement
    (e.g. two frameworks giving a different `type`) is warned per attribute."""
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
            out[attr] = _pick_by_authority_warned(cc, f'{tname}.{name}.{attr}')
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
    merged['columns'] = [_merge_column(tname, nm, col_contribs[nm])
                         for nm in col_order if nm not in col_drops]
    # ── primary_key: physical authority, present-only (§7.3) ──
    pk = [(_PHYSICAL_RANK[kind], order, frag['primary_key'])
          for kind, order, frag in contribs if 'primary_key' in frag]
    merged['primary_key'] = (_pick_by_authority_warned(pk, f'{tname}.primary_key')
                             if pk else None)
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
        label = f'{tname} index {k if isinstance(k, str) else "(" + ", ".join(k) + ")"}'
        ix = copy.deepcopy(_pick_by_authority_warned(ix_contribs[k], label))
        ix.pop('drop', None)  # strip any op marker
        merged['indexes'].append(ix)
    # ── comment: logical authority, present-only; null/"" = delete (§7.5) ──
    cm = [(_LOGICAL_RANK[kind], order, frag['comment'])
          for kind, order, frag in contribs if 'comment' in frag]
    if cm:
        val = _pick_by_authority_warned(cm, f'{tname}.comment')
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
    """Phase B (§8.5) — edge-level DB/schema-FK reconciliation: an explicit
    (non-db_fk, non-schema_fk, non-inferred) association covering an
    undirected {source, target} pair drops the DB/schema FK for that pair when
    the explicit side names no column or the same column; a dropped has_one
    DB/schema FK upgrades a lone covering belongs_to to has_one in place.
    belongs_to alone doesn't assert cardinality in Rails, so dropping a 1:1
    DB/schema FK outright would silently discard its 1:1 signal. A
    'schema_fk' association (rails.schema's static parse of a foreign-key
    definition) is treated exactly like 'db_fk' here — same covered-by-
    explicit drop, same has_one upgrade — since it is the same kind of
    machine-derived, un-named edge; only its representative-provenance rank
    (§9.1, below db_fk) tells them apart at the field-authority level. Mutates
    `tables` in place and returns the number of DB/schema FKs removed."""
    explicit_by_pair = {}
    for name, t in tables.items():
        for a in t['associations']:
            if _assoc_provenance(a) in ('declared', 'manual'):
                explicit_by_pair.setdefault(frozenset((name, a['target'])), []).append((name, a))
    removed = 0
    for name, t in tables.items():
        kept = []
        for a in t['associations']:
            if _assoc_provenance(a) in ('db_fk', 'schema_fk'):
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
# ---------------------------------------------------------------------------
# Framework overlays — the Framework layer's pluggable "code -> IR" surface.
#
# A FrameworkOverlay reads association (and, for Prisma/Django, column)
# semantics out of application code and returns a ProviderResult that merge_ir
# folds over the DB layer. The built-in overlays (Rails, Prisma, Django) live in
# the sibling frameworks/*.py fragments — one per framework — and each
# @register_overlay's its class. Adding one is "drop a frameworks/<name>.py that
# subclasses FrameworkOverlay and registers itself"; the build picks the folder
# up automatically, and a --adapter plugin can register the very same way at run
# time (register_overlay is exported alongside register_adapter).
#
# This base file also holds the machinery the overlays share: the inflector
# (pluralize / to_snake / class_to_table, used by Rails and by FK inference) and
# the post-merge *_id FK inference pass. Those stay module-level free functions
# because the test suite calls them by name.
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
# Overlay base class + registry
# ---------------------------------------------------------------------------
FRAMEWORK_OVERLAYS = []   # registered FrameworkOverlay subclasses (see priority)


def register_overlay(cls):
    """Class decorator: register a FrameworkOverlay subclass. Returns the class
    unchanged. Detection consults overlays in `priority` order (see
    framework_overlay_for), so registration/concat order does not matter."""
    FRAMEWORK_OVERLAYS.append(cls)
    return cls


class FrameworkOverlay(abc.ABC):
    """Abstract base for a framework overlay: recognise an application-code
    project and parse its schema/associations into a ProviderResult.

    To add another framework, subclass this and:
      * set `name` to the short provider id recorded in the IR's Source (§5),
        e.g. 'sequelize';
      * set `priority` if detection ordering matters (lower runs first; the
        first overlay whose detect() is true wins);
      * set `expects` to a short human description of the input layout the
        overlay parses — it is quoted in the "found nothing to parse" error
        a typed `sources[].type` run raises when build() returns no tables;
      * implement detect(root) -> bool over a --models path (a directory, or a
        single schema file);
      * implement build(root, table_map) -> ProviderResult (table_map is the
        Rails table override map; ignore it if not applicable);
      * decorate the class with @register_overlay.
    """
    name = ''
    priority = 100
    expects = ''  # human description of the expected input layout (see above)

    @abc.abstractmethod
    def detect(self, root):
        """True if `root` (a --models path) is a project this overlay handles."""
        raise NotImplementedError

    @abc.abstractmethod
    def build(self, root, table_map):
        """Return a framework ProviderResult for `root`."""
        raise NotImplementedError


def framework_overlay_for(root):
    """The first registered overlay (in priority order) that recognises `root`,
    or None. Instantiating each is cheap — they hold no state."""
    for cls in sorted(FRAMEWORK_OVERLAYS, key=lambda c: (c.priority, c.name)):
        overlay = cls()
        if overlay.detect(root):
            return overlay
    return None

def framework_overlays_matching(root):
    """Every registered overlay (in priority order) that recognises `root`, not
    just the first. Used only to report ambiguity when an untyped --models/
    config `models` path matches more than one framework (sources.py); the
    winner is always overlays_matching(root)[0], i.e. framework_overlay_for's
    pick — this function changes nothing about detection, only what gets
    reported about it."""
    return [cls for cls in sorted(FRAMEWORK_OVERLAYS, key=lambda c: (c.priority, c.name))
            if cls().detect(root)]

def detect_code_source(root):
    """Classify a --models path: the `name` of the overlay that recognises it
    (a Rails app/models dir, a Prisma schema, or a Django project), or None."""
    overlay = framework_overlay_for(root)
    return overlay.name if overlay else None

def framework_provider(mroot, table_map=None):
    """Framework ProviderResult (§5). Finds the overlay that recognises the
    --models path and delegates to its build(), which resolves the concrete
    input (the Rails app/models dir, the schema.prisma file, or the Django
    root) and parses it."""
    overlay = framework_overlay_for(mroot)
    if overlay is None:
        sys.exit(f'Error: could not detect the code kind at {mroot} '
                 '(expected a Rails app/models dir, a schema.prisma, or a Django project)')
    return overlay.build(mroot, table_map)

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

    # pass 1: collect every class definition. Keyed by (app, class name) —
    # two apps may each define a model with the same class name (e.g. a Tag
    # in both blog and shop), and a name-only dict silently dropped all but
    # the last one parsed. by_name indexes the keys for base/reference
    # resolution, which still happens by bare class name.
    classes = {}  # (app, class name) -> {bases, fields, meta}
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
                    ftype = fn.attr if isinstance(fn, ast.Attribute) else \
                            fn.id if isinstance(fn, ast.Name) else None
                    if ftype and (ftype in DJANGO_TYPES or ftype in DJANGO_REL_FIELDS
                                  or ftype == 'GenericForeignKey'):
                        fields.append({
                            'name': stmt.targets[0].id, 'ftype': ftype,
                            'args': stmt.value.args,
                            'kw': {k.arg: k.value for k in stmt.value.keywords if k.arg},
                        })
            classes[(app, node.name)] = {'bases': bases, 'fields': fields, 'meta': meta}

    by_name = {}  # class name -> [every (app, name) key defining it]
    for key in classes:
        by_name.setdefault(key[1], []).append(key)

    def base_key(key, base_name):
        # a base is referenced by bare name — prefer the same app's
        # definition, else the first app defining it
        cands = by_name.get(base_name, ())
        for k in cands:
            if k[0] == key[0]:
                return k
        return cands[0] if cands else None

    # a class is a model if models.Model is among its ancestors (transitively)
    model_keys, changed = set(), True
    while changed:
        changed = False
        for key, c in classes.items():
            if key not in model_keys and (
                    'Model' in c['bases']
                    or any(base_key(key, b) in model_keys for b in c['bases'])):
                model_keys.add(key)
                changed = True

    def const(v):
        return v.value if isinstance(v, ast.Constant) else None

    def merged_fields(key, seen=None):
        # inherit fields from abstract base classes
        seen = seen or set()
        if key not in classes or key in seen:
            return []
        seen.add(key)
        out = []
        for b in classes[key]['bases']:
            bk = base_key(key, b)
            if bk in model_keys:
                out.extend(merged_fields(bk, seen))
        out.extend(classes[key]['fields'])
        return out

    concrete = [k for k in model_keys
                if not classes[k]['meta'].get('abstract')
                and not classes[k]['meta'].get('proxy')]
    table_of = {k: classes[k]['meta'].get('db_table')
                or f'{k[0]}_{k[1].lower()}' for k in concrete}

    def class_key(name, own_key):
        # bare class reference: the same app's model if it has one, else the
        # single other app defining it; ambiguous (several other apps) -> None
        cands = [k for k in by_name.get(name, ()) if k in table_of]
        for k in cands:
            if k[0] == own_key[0]:
                return k
        return cands[0] if len(cands) == 1 else None

    def resolve(node, own_key):
        # ForeignKey(Author) / ForeignKey('Author') / ForeignKey('blog.Author')
        # / 'self'. Unresolvable references (settings.AUTH_USER_MODEL, an
        # external app's ContentType, ...) return None — the caller keeps the
        # FK column but skips the edge.
        if isinstance(node, ast.Name) or isinstance(node, ast.Attribute):
            k = class_key(node.id if isinstance(node, ast.Name) else node.attr, own_key)
            return table_of[k] if k else None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            s = node.value
            if s == 'self':
                return table_of.get(own_key)
            if '.' in s:
                app, cls = s.split('.', 1)
                return table_of.get((app, cls), f'{app.lower()}_{cls.lower()}')
            k = class_key(s, own_key)
            return table_of[k] if k else f'{own_key[0]}_{s.lower()}'
        return None

    tables = {}
    for n in concrete:
        tname = table_of[n]
        cols, assocs, pk = [], [], None
        for f in merged_fields(n):
            fname, ftype, kw = f['name'], f['ftype'], f['kw']
            if ftype == 'GenericForeignKey':
                # the Django spelling of a polymorphic belongs_to; like the
                # Rails one it never draws an edge (there is no single
                # target), it only surfaces in the details pane. The
                # content_type FK / object_id columns are ordinary fields the
                # model declares itself.
                assocs.append({'type': 'belongs_to', 'name': fname,
                               'target': pluralize(to_snake(fname)),
                               'polymorphic': True})
                continue
            if ftype in DJANGO_REL_FIELDS:
                tnode = kw.get('to') or (f['args'][0] if f['args'] else None)
                target = resolve(tnode, n) if tnode is not None else None
                if ftype == 'ManyToManyField':
                    if not target:
                        continue
                    a = {'type': 'has_and_belongs_to_many', 'name': fname, 'target': target}
                    thr = resolve(kw['through'], n) if 'through' in kw else None
                    if thr:
                        a['through'] = thr
                    assocs.append(a)
                else:
                    # the FK column is real even when the target class can't
                    # be resolved statically (a swappable AUTH_USER_MODEL,
                    # contenttypes' ContentType, ...) — keep the column,
                    # skip only the edge
                    col = const(kw.get('db_column')) or f'{fname}_id'
                    cols.append({'name': col, 'type': 'bigint',
                                 'nullable': bool(const(kw.get('null'))), 'primary': False})
                    if target:
                        # OneToOneField: the declaring side holds the FK, but
                        # we emit has_one so the edge renders as 1:1
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

def django_provider(root):
    """ProviderResult for a resolved Django project root. Retains columns —
    including the synthetic `id` PK Django backfills and the `<name>_id` FK
    columns emitted for ForeignKey/OneToOneField — that the current overlay
    path drops."""
    tables = parse_django(root)
    return make_provider_result('framework', 'django', tables,
                                location=str(root))

@register_overlay
class DjangoOverlay(FrameworkOverlay):
    """A Django project: a directory containing manage.py. Retains columns."""
    name = 'django'
    priority = 2
    expects = ('a Django project whose models.py files declare at least one '
               'concrete model')

    def detect(self, root):
        return root.is_dir() and (root / 'manage.py').exists()

    def build(self, root, table_map):
        return django_provider(root)
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

    def relation_name(rest):
        # @relation("Name", ...) — the disambiguator Prisma requires when two
        # relations link the same pair of models (and for self-relations)
        m = re.search(r'@relation\(\s*"([^"]+)"', rest)
        return m.group(1) if m else None

    # every list field in every model: model -> [(field, target model, relation
    # name)]. Implicit m2m pairing below matches on (target, relation name) —
    # NOT merely "the other model declares some list of us", which misread a
    # self-relation's own back-reference (`replies Post[]` next to `parent
    # Post?`) and any mixed named relations as many-to-many.
    list_fields = {}
    for model, block in blocks.items():
        entries = []
        for line in block.splitlines():
            lm = re.match(r'\s*(\w+)\s+(\w+)\[\]\s*(.*)', line)
            if lm and lm.group(2) in blocks:
                entries.append((lm.group(1), lm.group(2), relation_name(lm.group(3))))
        list_fields[model] = entries

    def paired_list_field(model, fname, other, rel):
        # does `other` declare a list field back at `model` in the SAME
        # relation (matching @relation name, both possibly unnamed)? The field
        # itself is excluded so a self-relation's one list field never pairs
        # with itself.
        for f, t, r in list_fields.get(other, ()):
            if t == model and r == rel and not (other == model and f == fname):
                return True
        return False

    tables = {}
    for model, block in blocks.items():
        cols, assocs, pk = [], [], None
        unique_cols = set()  # scalar fields with @unique — used below to
                              # tell a 1:1 FK-holding side from a plain belongs_to
        lines = [l.strip() for l in block.splitlines() if l.strip() and not l.strip().startswith('@@')]

        # pass 1: scalar/enum columns only — relation fields need unique_cols
        # fully populated first (a relation's `fields: [...]` FK column can be
        # declared on any line in the block, not necessarily before it)
        field_col = {}  # field name -> column name (differs under @map)
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
            field_col[fname] = col
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

        # block-level attributes (the @@ lines pass 1 skips), read from the
        # raw block: @@id([a, b]) is a composite PK (primary_key becomes the
        # IR's list form, same as the DB adapters emit); a single-field
        # @@unique([x]) is the same 1:1 signal as an inline @unique, while a
        # multi-field one is a composite constraint and deliberately not one
        mid = re.search(r'@@id\([^)]*?\[([^\]]+)\]', block)
        if mid:
            pk_cols = [field_col.get(f.strip(), f.strip())
                       for f in mid.group(1).split(',') if f.strip()]
            for c in cols:
                if c['name'] in pk_cols:
                    c['primary'] = True
                    c['nullable'] = False
            if pk_cols:
                pk = pk_cols if len(pk_cols) > 1 else pk_cols[0]
        for mu in re.finditer(r'@@unique\([^)]*?\[([^\]]+)\]', block):
            ufields = [f.strip() for f in mu.group(1).split(',') if f.strip()]
            if len(ufields) == 1:
                unique_cols.add(field_col.get(ufields[0], ufields[0]))

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
                # the SAME relation has a list field on the other side too ->
                # implicit many-to-many (see paired_list_field for why the
                # pairing must match @relation names, not just field types)
                if paired_list_field(model, fname, ftype, relation_name(rest)):
                    assocs.append({'type': 'has_and_belongs_to_many',
                                   'name': fname, 'target': target})
                else:
                    assocs.append({'type': 'has_many', 'name': fname, 'target': target})
            elif fields:  # the side holding the FK
                # `fields:` names the scalar FIELD; the column differs when
                # that field carries @map — resolve so foreign_key (and the
                # unique_cols lookup below) name the real column
                fk_col = field_col.get(fields.group(1), fields.group(1))
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

def prisma_provider(schema_path):
    """ProviderResult for a resolved Prisma schema file. Retains columns
    (with Prisma types, including enum field types as the enum name) so a
    Prisma-only run (Step 8) or the Step-6 merge can use them instead of
    discarding them the way the current association-only overlay does."""
    tables = parse_prisma(schema_path)
    return make_provider_result('framework', 'prisma', tables,
                                location=str(schema_path))

@register_overlay
class PrismaOverlay(FrameworkOverlay):
    """A Prisma schema: a schema.prisma file directly, or a project containing
    prisma/schema.prisma (or schema.prisma at the root). Retains columns."""
    name = 'prisma'
    priority = 3
    expects = ('a schema.prisma file (or a project containing '
               'prisma/schema.prisma) declaring at least one model')

    def _schema(self, root):
        if root.is_file():
            return root
        found = next((c for c in (root / 'prisma' / 'schema.prisma', root / 'schema.prisma')
                      if c.exists()), None)
        if found is None:
            # reachable via a typed prisma.models source, which skips detect()
            # — a raw StopIteration traceback is not an error message
            sys.exit(f'Error: no prisma/schema.prisma or schema.prisma found under {root}')
        return found

    def detect(self, root):
        if root.is_file():
            return root.suffix == '.prisma'
        return any(c.exists() for c in
                   (root / 'prisma' / 'schema.prisma', root / 'schema.prisma'))

    def build(self, root, table_map):
        return prisma_provider(self._schema(root))
# ---------------------------------------------------------------------------
# Rails model parser (app/models/**/*.rb)
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

@register_overlay
class RailsOverlay(FrameworkOverlay):
    """A Rails project: an app/models directory (or a directory of *.rb model
    files). Contributes associations only — no columns."""
    name = 'rails'
    priority = 1
    expects = ('a Rails app/models directory (or a directory of *.rb model '
               'files) declaring at least one model')

    def detect(self, root):
        return root.is_dir() and (
            (root / 'app' / 'models').is_dir() or any(root.glob('*.rb')))

    def build(self, root, table_map):
        mdir = root / 'app' / 'models' if (root / 'app' / 'models').is_dir() else root
        return rails_provider(mdir, table_map)
# ---------------------------------------------------------------------------
# Rails db/schema.rb parser — static text analysis, NO Ruby execution (D7).
#
# schema.rb is Rails' generated, canonical dump of the live database as Ruby
# DSL calls (create_table/t.<type>/add_foreign_key/...). It is always
# machine-generated in a narrow, predictable shape (one statement per
# physical line), so a line-oriented state machine — outside / inside a
# `create_table ... do |t|` block — plus a small Ruby-literal parser for each
# statement's argument list is enough to read it faithfully without ever
# `eval`-ing Ruby. Anything the parser can't identify or parse as a literal
# is a warning, never a silent drop (§ "NEVER silently drop an unrecognized
# construct").
# ---------------------------------------------------------------------------
_SCHEMA_COLUMN_TYPES = {
    'string', 'text', 'integer', 'bigint', 'float', 'decimal', 'numeric',
    'datetime', 'timestamp', 'time', 'date', 'boolean', 'binary', 'blob',
    'json', 'jsonb', 'uuid', 'inet',
}
_COLUMN_RECOGNIZED_OPTS = {'null', 'default', 'comment', 'limit', 'precision', 'scale'}
_CREATE_TABLE_IGNORED_OPTS = {'force', 'charset', 'collation', 'options', 'if_not_exists'}
_CREATE_TABLE_RECOGNIZED_OPTS = {'id', 'primary_key', 'comment'} | _CREATE_TABLE_IGNORED_OPTS
_REFERENCE_RECOGNIZED_OPTS = {'type', 'null', 'polymorphic', 'index', 'foreign_key'}
_INDEX_RECOGNIZED_OPTS = {'name', 'unique'}
_ADD_FK_IGNORED_OPTS = {'primary_key', 'name', 'on_delete', 'on_update', 'validate', 'deferrable'}
_ADD_FK_RECOGNIZED_OPTS = {'column'} | _ADD_FK_IGNORED_OPTS

_SCHEMA_HEADER_RE = re.compile(r'^ActiveRecord::Schema(\[[^\]]*\])?\.define\(.*\)\s*do$')
_CREATE_TABLE_RE = re.compile(r'^create_table\s+(?P<args>.+?)\s+do\s*\|\s*t\s*\|$')
_T_CALL_RE = re.compile(r'^t\.(?P<method>\w+)\b\s*(?P<args>.*)$')
_ADD_INDEX_RE = re.compile(r'^add_index\s+(?P<args>.+)$')
_ADD_FK_RE = re.compile(r'^add_foreign_key\s+(?P<args>.+)$')
_IGNORED_TOP_LEVEL_RE = re.compile(r'^(enable_extension|create_schema)\b')
_OPT_KEY_RE = re.compile(r'^([A-Za-z_]\w*):\s*(.*)$', re.S)

_DYNAMIC = object()  # sentinel: this token couldn't be parsed as a Ruby literal

# plural -> singular, for the FK-column-name inflector below (the inverse of
# frameworks/base.py's IRREGULAR, which is singular -> plural)
_IRREGULAR_SINGULAR = {plural: singular for singular, plural in IRREGULAR.items()}


def _singularize(word):
    """Minimal singularizer, used ONLY to compute add_foreign_key's default
    column name (`<to_table singular>_id` — there is no general singularize
    helper elsewhere in the codebase, pluralize() only goes one way)."""
    if not word:
        return word
    if word in _IRREGULAR_SINGULAR:
        return _IRREGULAR_SINGULAR[word]
    if word.endswith('ies'):
        return word[:-3] + 'y'
    if re.search(r'(s|x|z|ch|sh)es$', word):
        return word[:-2]
    if word.endswith('s'):
        return word[:-1]
    return word


# ---------------------------------------------------------------------------
# Ruby literal / call-argument parsing (D7's "Ruby literal parsing helper")
# ---------------------------------------------------------------------------
def _strip_comment(line):
    """Cut a line at its first `#` that is outside a quoted string — quote-
    aware so a comment/default string containing '#' (e.g. comment: "a #tag")
    is never truncated."""
    quote, i, n = None, 0, len(line)
    while i < n:
        c = line[i]
        if quote:
            if c == '\\' and i + 1 < n:
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in '\'"':
            quote = c
            i += 1
            continue
        if c == '#':
            return line[:i]
        i += 1
    return line


def _split_top_level(s):
    """Split `s` on commas at bracket/paren/brace depth 0 and outside quotes —
    shared by call-argument lists, `[...]` arrays, and `{...}` hashes."""
    parts, depth, cur, quote, i, n = [], 0, [], None, 0, len(s)
    while i < n:
        c = s[i]
        if quote:
            cur.append(c)
            if c == '\\' and i + 1 < n:
                cur.append(s[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in '\'"':
            quote = c
            cur.append(c)
            i += 1
            continue
        if c in '([{':
            depth += 1
            cur.append(c)
            i += 1
            continue
        if c in ')]}':
            depth -= 1
            cur.append(c)
            i += 1
            continue
        if c == ',' and depth == 0:
            parts.append(''.join(cur).strip())
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    tail = ''.join(cur).strip()
    if tail:
        parts.append(tail)
    return parts


def _unescape_ruby_string(inner, quote):
    """Undo Ruby string escaping for the quote character, backslash, \\n, \\t
    — the escapes realistically found in a generated schema.rb's string
    literals (table/column names, comments, string defaults)."""
    out, i, n = [], 0, len(inner)
    while i < n:
        c = inner[i]
        if c == '\\' and i + 1 < n:
            nc = inner[i + 1]
            if nc in (quote, '\\'):
                out.append(nc)
            elif nc == 'n':
                out.append('\n')
            elif nc == 't':
                out.append('\t')
            else:
                out.append(nc)
            i += 2
            continue
        out.append(c)
        i += 1
    return ''.join(out)


def _parse_ruby_value(tok):
    """Parse one Ruby literal token: string, symbol, int, float, true/false/
    nil, a `[...]` array of the same, or a `{...}` hash of known option
    shapes (D7). Returns `_DYNAMIC` for anything else (a method call, a
    lambda, an interpolated string, ...) — the caller decides what to skip."""
    tok = tok.strip()
    if not tok:
        return _DYNAMIC
    if tok in ('true', 'false'):
        return tok == 'true'
    if tok == 'nil':
        return None
    if len(tok) >= 2 and tok[0] in '\'"' and tok[-1] == tok[0]:
        return _unescape_ruby_string(tok[1:-1], tok[0])
    if tok.startswith(':'):
        rest = tok[1:]
        if len(rest) >= 2 and rest[0] in '\'"' and rest[-1] == rest[0]:
            return _unescape_ruby_string(rest[1:-1], rest[0])
        if re.fullmatch(r'\w+', rest):
            return rest
        return _DYNAMIC
    if re.fullmatch(r'-?\d+', tok):
        return int(tok)
    if re.fullmatch(r'-?\d+\.\d+', tok):
        return float(tok)
    if tok.startswith('[') and tok.endswith(']'):
        vals = []
        for it in _split_top_level(tok[1:-1]):
            v = _parse_ruby_value(it)
            if v is _DYNAMIC:
                return _DYNAMIC
            vals.append(v)
        return vals
    if tok.startswith('{') and tok.endswith('}'):
        out = {}
        for it in _split_top_level(tok[1:-1]):
            m = _OPT_KEY_RE.match(it)
            if not m:
                return _DYNAMIC
            v = _parse_ruby_value(m.group(2))
            if v is _DYNAMIC:
                return _DYNAMIC
            out[m.group(1)] = v
        return out
    return _DYNAMIC


def _parse_call_args(args_str):
    """Split a Ruby call's argument list into (positionals, options): a token
    matching `identifier: value` at the top level is an option (key parsed,
    value left as _parse_ruby_value(value) — possibly `_DYNAMIC`); everything
    else is a positional, itself run through _parse_ruby_value."""
    positionals, options = [], {}
    for tok in _split_top_level(args_str):
        # _OPT_KEY_RE anchors on a leading identifier, so a quoted string or
        # symbol token (never starting with a letter/underscore) can't match
        # it and always falls through to positional, even if its contents
        # happen to contain a colon (e.g. a string default "10:30").
        m = _OPT_KEY_RE.match(tok)
        if m:
            options[m.group(1)] = _parse_ruby_value(m.group(2))
        else:
            positionals.append(_parse_ruby_value(tok))
    return positionals, options


def _default_index_name(table, columns):
    return f"index_{table}_on_{'_'.join(columns)}"


def _format_default(value):
    """Lossless, engine-agnostic string form of a parsed Ruby default (D7):
    numbers via str(), strings verbatim, booleans as 'true'/'false' — NOT
    imitating any particular DB engine's default-value spelling."""
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


# ---------------------------------------------------------------------------
# create_table body — column / reference / timestamps / index statements
# ---------------------------------------------------------------------------
def _apply_column_opts(col, opts, ctx, line_no, warn, seen_unknown_opts):
    """Apply the recognized {null, default, comment, limit, precision, scale}
    options onto `col` (mutated), building `sql_type` modifiers as it goes.
    Any other option name warns (deduped per option name per file — D7)."""
    if opts.get('null') is False:
        col['nullable'] = False
    if 'default' in opts:
        val = opts['default']
        if val is _DYNAMIC:
            warn(line_no, f'dynamic default ({ctx}) — attribute skipped')
        elif val is not None:
            col['default'] = _format_default(val)
    if 'comment' in opts and isinstance(opts['comment'], str):
        col['comment'] = opts['comment']
    mods = []
    if 'limit' in opts and isinstance(opts['limit'], int):
        mods.append(str(opts['limit']))
    elif 'precision' in opts and isinstance(opts['precision'], int):
        if 'scale' in opts and isinstance(opts['scale'], int):
            mods.append(f"{opts['precision']},{opts['scale']}")
        else:
            mods.append(str(opts['precision']))
    if mods:
        col['sql_type'] = f"{col['sql_type']}({','.join(mods)})"
    for key in opts:
        if key not in _COLUMN_RECOGNIZED_OPTS and key not in seen_unknown_opts:
            seen_unknown_opts.add(key)
            warn(line_no, f'unknown option {key!r} ({ctx}) — ignored')


def _handle_column_stmt(rails_type, args, line_no, table, warn, seen_unknown_opts):
    positionals, opts = _parse_call_args(args)
    if not positionals or positionals[0] is _DYNAMIC or not isinstance(positionals[0], str):
        warn(line_no, f"unparseable column name for t.{rails_type} — column skipped")
        return
    name = positionals[0]
    nullable = opts.get('null') is not False
    col = {'name': name, 'type': SQL_TYPES.get(rails_type, rails_type),
          'sql_type': rails_type, 'nullable': nullable}
    _apply_column_opts(col, opts, f'{rails_type}: {name!r}', line_no, warn, seen_unknown_opts)
    table['columns'].append(col)


def _handle_reference_stmt(method, args, line_no, table, warn, pending_fks):
    positionals, opts = _parse_call_args(args)
    if not positionals or positionals[0] is _DYNAMIC or not isinstance(positionals[0], str):
        warn(line_no, f"unparseable reference name for t.{method} — skipped")
        return
    name = positionals[0]
    for key in opts:
        if key not in _REFERENCE_RECOGNIZED_OPTS:
            warn(line_no, f'unknown option {key!r} (t.{method}: {name!r}) — ignored')
    rails_type = opts.get('type')
    if not isinstance(rails_type, str):
        rails_type = 'bigint'
    nullable = opts.get('null') is not False
    id_col = f'{name}_id'
    table['columns'].append({'name': id_col, 'type': SQL_TYPES.get(rails_type, rails_type),
                             'sql_type': rails_type, 'nullable': nullable})
    polymorphic = opts.get('polymorphic') is True
    index_cols = [id_col]
    if polymorphic:
        type_col = f'{name}_type'
        table['columns'].append({'name': type_col, 'type': 'string',
                                 'sql_type': 'string', 'nullable': nullable})
        index_cols = [type_col, id_col]
        if opts.get('foreign_key'):
            warn(line_no, f"t.{method} {name!r} is polymorphic — "
                          "foreign_key: is ignored (no single target table)")
    index_opt = opts.get('index', True)
    if index_opt is not False:
        unique = isinstance(index_opt, dict) and index_opt.get('unique') is True
        table['indexes'].append({'name': _default_index_name(table['_name'], index_cols),
                                 'columns': index_cols, 'unique': unique})
    fk_opt = opts.get('foreign_key')
    if fk_opt and not polymorphic:
        target = fk_opt['to_table'] if isinstance(fk_opt, dict) and 'to_table' in fk_opt \
            else pluralize(name)
        pending_fks.append({'table': table['_name'], 'column': id_col,
                            'target': target, 'line': line_no})


def _handle_timestamps_stmt(args, line_no, table, warn):
    _, opts = _parse_call_args(args)
    nullable = opts.get('null') is True  # default null: false — opposite of a plain column
    for key in opts:
        if key != 'null':
            warn(line_no, f'unknown option {key!r} (t.timestamps) — ignored')
    for col_name in ('created_at', 'updated_at'):
        table['columns'].append({'name': col_name, 'type': 'datetime',
                                 'sql_type': 'datetime', 'nullable': nullable})


def _handle_index_stmt(args, line_no, table, warn, top_level_table_name=None):
    """Shared by `t.index` (table param is the enclosing create_table) and
    `add_index` (table param is None; caller resolves `top_level_table_name`
    against the already-parsed tables and passes the fragment, or None if
    unknown)."""
    positionals, opts = _parse_call_args(args)
    if not positionals or positionals[0] is _DYNAMIC:
        warn(line_no, 'unparseable index columns — skipped')
        return
    cols = positionals[0] if isinstance(positionals[0], list) else [positionals[0]]
    if not all(isinstance(c, str) for c in cols):
        warn(line_no, 'unparseable index columns — skipped')
        return
    for key in opts:
        if key not in _INDEX_RECOGNIZED_OPTS:
            warn(line_no, f'unknown option {key!r} (index on {cols}) — ignored')
    tname = top_level_table_name if top_level_table_name is not None else table['_name']
    name = opts.get('name') if isinstance(opts.get('name'), str) else _default_index_name(tname, cols)
    unique = opts.get('unique') is True
    table['indexes'].append({'name': name, 'columns': cols, 'unique': unique})


_TABLE_STATEMENT_HANDLERS = {
    'timestamps': 'timestamps',
    'index': 'index',
    'references': 'references',
    'belongs_to': 'references',
}


def _handle_table_body_line(line, line_no, table, warn, pending_fks, seen_unknown_opts):
    """Dispatch one physical line inside a `create_table ... do |t|` block.
    Returns True if `line` closed the table (a bare `end`)."""
    if line == 'end':
        return True
    m = _T_CALL_RE.match(line)
    if not m:
        warn(line_no, f"unknown statement {line!r} — skipped")
        return False
    method, args = m.group('method'), m.group('args')
    if args.startswith('(') and args.endswith(')'):
        args = args[1:-1]
    kind = _TABLE_STATEMENT_HANDLERS.get(method)
    if method in _SCHEMA_COLUMN_TYPES:
        _handle_column_stmt(method, args, line_no, table, warn, seen_unknown_opts)
    elif kind == 'references':
        _handle_reference_stmt(method, args, line_no, table, warn, pending_fks)
    elif kind == 'timestamps':
        _handle_timestamps_stmt(args, line_no, table, warn)
    elif kind == 'index':
        _handle_index_stmt(args, line_no, table, warn)
    else:
        warn(line_no, f"unsupported column type 't.{method}' — column skipped")
    return False


# ---------------------------------------------------------------------------
# Top-level statements — create_table header, add_index, add_foreign_key
# ---------------------------------------------------------------------------
def _new_table(name):
    return {'_name': name, 'columns': [], 'indexes': [], 'primary_key': None}


def _start_create_table(m, line_no, warn):
    positionals, opts = _parse_call_args(m.group('args'))
    if not positionals or positionals[0] is _DYNAMIC or not isinstance(positionals[0], str):
        warn(line_no, 'unparseable create_table name — table skipped')
        return None
    name = positionals[0]
    table = _new_table(name)
    for key in opts:
        if key not in _CREATE_TABLE_RECOGNIZED_OPTS:
            warn(line_no, f'unknown option {key!r} (create_table {name!r}) — ignored')
    if isinstance(opts.get('comment'), str):
        table['comment'] = opts['comment']
    pk_opt = opts.get('primary_key')
    id_opt = opts.get('id', True)
    if isinstance(pk_opt, list):
        table['primary_key'] = pk_opt   # composite PK — its columns are defined in the body
    elif id_opt is not False:
        pk_name = pk_opt if isinstance(pk_opt, str) else 'id'
        pk_type = id_opt if isinstance(id_opt, str) else 'bigint'
        table['columns'].append({'name': pk_name, 'type': SQL_TYPES.get(pk_type, pk_type),
                                 'sql_type': SQL_TYPES.get(pk_type, pk_type),
                                 'nullable': False, 'primary': True})
        table['primary_key'] = pk_name
    return table


def _handle_top_level_line(line, line_no, tables, pending_fks, warn):
    if line == 'end' or _SCHEMA_HEADER_RE.match(line) or _IGNORED_TOP_LEVEL_RE.match(line):
        return
    m = _ADD_INDEX_RE.match(line)
    if m:
        tokens = _split_top_level(m.group('args'))
        tname = _parse_ruby_value(tokens[0]) if tokens else _DYNAMIC
        if not isinstance(tname, str):
            warn(line_no, 'unparseable add_index target table — skipped')
            return
        if tname not in tables:
            warn(line_no, f'add_index on unknown table {tname!r} — skipped')
            return
        _handle_index_stmt(', '.join(tokens[1:]), line_no, tables[tname], warn,
                           top_level_table_name=tname)
        return
    m = _ADD_FK_RE.match(line)
    if m:
        positionals, opts = _parse_call_args(m.group('args'))
        if (len(positionals) < 2 or positionals[0] is _DYNAMIC or positionals[1] is _DYNAMIC
                or not isinstance(positionals[0], str) or not isinstance(positionals[1], str)):
            warn(line_no, 'unparseable add_foreign_key arguments — skipped')
            return
        table_name, target = positionals[0], positionals[1]
        for key in opts:
            if key not in _ADD_FK_RECOGNIZED_OPTS:
                warn(line_no, f'unknown option {key!r} (add_foreign_key {table_name!r}, '
                              f'{target!r}) — ignored')
        column = opts.get('column') if isinstance(opts.get('column'), str) \
            else f'{_singularize(target)}_id'
        pending_fks.append({'table': table_name, 'column': column, 'target': target,
                            'line': line_no})
        return
    warn(line_no, f"unknown statement {line!r} — skipped")


def _resolve_pending_fks(tables, pending_fks, warn):
    """FK resolution pass (D7), run after the whole file is parsed: turn each
    pending {table, column, target} into a schema_fk association on `table`,
    or warn+skip when the table/target/column doesn't resolve. Dedups
    (table, column, target) so a t.references foreign_key: + a redundant
    add_foreign_key for the same edge produce exactly one association."""
    seen = set()
    for fk in pending_fks:
        tname, col, target, line_no = fk['table'], fk['column'], fk['target'], fk['line']
        if tname not in tables:
            warn(line_no, f'add_foreign_key on unknown table {tname!r} — skipped')
            continue
        if target not in tables:
            warn(line_no, f'foreign key on {tname!r} references unknown table {target!r} '
                          '— skipped')
            continue
        table = tables[tname]
        if not any(c['name'] == col for c in table['columns']):
            warn(line_no, f'foreign key on {tname!r} names column {col!r} which does not '
                          'exist — skipped')
            continue
        key = (tname, col, target)
        if key in seen:
            continue
        seen.add(key)
        assoc_type = 'has_one' if _unique_single_col(table, col) else 'belongs_to'
        name = col[:-3] if col.endswith('_id') else col
        table.setdefault('associations', []).append(
            {'type': assoc_type, 'name': name, 'target': target,
             'foreign_key': col, 'schema_fk': True})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def rails_schema_provider(path, given=None):
    """ProviderResult (kind='schema', provider='rails.schema') for a Rails
    db/schema.rb file: columns, primary keys, indexes, and foreign keys,
    parsed by pure text analysis — schema.rb is NEVER executed as Ruby (D7).
    A parser-derived FK becomes an association carrying `schema_fk: True`
    (D2), the schema layer's legacy-style provenance flag, mirroring how the
    live-DB layer marks its own FKs `db_fk: True`.

    `path` is always the resolved Path actually read from disk; `given` is
    the (possibly relative, possibly unresolved) path string to DISPLAY in
    warnings/location — sources.py passes the user's own spelling so a
    warning reads e.g. `db/schema.rb:12: ...` instead of an absolute path
    the user never typed. Defaults to `str(path)` for callers (tests, direct
    use) that don't distinguish the two."""
    display = given if given is not None else str(path)
    text = path.read_text(encoding='utf-8', errors='replace')
    warnings = []

    def warn(line_no, msg):
        warnings.append(f'{display}:{line_no}: {msg}')

    tables = {}
    pending_fks = []
    seen_unknown_col_opts = set()
    current = None  # the in-progress table dict, or None when outside a create_table block
    lines = text.splitlines()

    for line_no, raw in enumerate(lines, 1):
        line = _strip_comment(raw).strip()
        if not line:
            continue
        if current is not None:
            closed = _handle_table_body_line(line, line_no, current, warn, pending_fks,
                                             seen_unknown_col_opts)
            if closed:
                if not current.get('_discard'):
                    tables[current['_name']] = {k: v for k, v in current.items()
                                                if k not in ('_name', '_discard')}
                current = None
            continue
        m = _CREATE_TABLE_RE.match(line)
        if m:
            current = _start_create_table(m, line_no, warn)
            if current is None:
                # unparseable create_table name — still consume the body so its
                # lines don't get misread as top-level statements, but discard it
                current = _new_table('')
                current['_discard'] = True
            continue
        _handle_top_level_line(line, line_no, tables, pending_fks, warn)

    if current is not None:
        warn(len(lines), 'unterminated create_table block at end of file — skipped')

    _resolve_pending_fks(tables, pending_fks, warn)
    return make_provider_result('schema', 'rails.schema', tables,
                                location=display, warnings=warnings)
# ---------------------------------------------------------------------------
# Input sources — InputSpec normalization + source-type registry/dispatch.
#
# Every code-source input (legacy --models, config `models`, and the typed
# config `sources` list) normalizes to a common, ordered list of InputSpec
# dicts, which then run through a small source-type registry to produce the
# ProviderResult layers merge_ir folds. A typed source (config `sources[].type`)
# skips detection entirely and calls the named type's builder directly; an
# untyped source (legacy --models / config `models`) keeps today's
# auto-detection behavior, with ambiguity/ note-worthy detections reported to
# stderr instead of resolved silently.
#
# InputSpec = {'id': str, 'type': str|None, 'path': Path, 'given': str,
#              'allow_empty': bool}
#   type None = auto-detect. `path` is always resolved (expanduser+resolve) —
#   the one filesystem operations use. `given` is the ORIGINAL path string
#   (relative, unresolved, exactly as the user typed/configured it) — the one
#   every user-facing message (warnings, progress lines, Note lines, error
#   messages naming the source) displays, so a relative `./app/models` in the
#   config still reads that way on stderr instead of some long absolute path
#   the user never wrote. `allow_empty` (config sources[].allow_empty,
#   default false) opts a TYPED source out of the empty-result hard error —
#   see _run_typed_spec; untyped sources never read it.
# ---------------------------------------------------------------------------

# Static source-type registry: type name -> builder fn(spec, table_map) ->
# ProviderResult. The '<overlay.name>.models' types (rails.models,
# prisma.models, django.models, and any --adapter overlay's own) are NOT
# listed here — they're derived dynamically from FRAMEWORK_OVERLAYS in
# _source_type_builder/known_source_type_names so a newly-registered overlay
# gets a usable `sources[].type` for free, with no registry edit.
def _rails_schema_type_builder(spec, table_map):
    return rails_schema_provider(spec['path'], given=spec['given'])


SOURCE_TYPES = {
    'rails.schema': _rails_schema_type_builder,
}

# Source types whose path must be an existing FILE, not a directory (D4) —
# checked in _run_typed_spec before the builder runs, with a type-specific
# message (a directory is the mistake someone makes when they meant the
# containing project, or copy-pasted a `rails.models` path by habit).
_FILE_SOURCE_TYPES = {'rails.schema': 'a schema.rb file'}


def _models_type_builder(overlay_cls):
    def build(spec, table_map):
        return overlay_cls().build(spec['path'], table_map)
    return build


def _source_type_builder(type_name):
    """Resolve a sources[].type name to its builder fn(spec, table_map) ->
    ProviderResult, or None if the name isn't (yet) registered."""
    if type_name in SOURCE_TYPES:
        return SOURCE_TYPES[type_name]
    for cls in FRAMEWORK_OVERLAYS:
        if f'{cls.name}.models' == type_name:
            return _models_type_builder(cls)
    return None


def known_source_type_names():
    """Every currently valid sources[].type value, sorted — the static
    registry, one '<overlay.name>.models' entry per registered
    FrameworkOverlay, and the 'rails.project' macro (D4 — it never reaches
    the dispatch registry itself, since normalize_input_specs expands it
    away first, but it's still a name a user can legitimately declare). Used
    only to build "unknown type" error messages."""
    dynamic = {f'{cls.name}.models' for cls in FRAMEWORK_OVERLAYS}
    return sorted(set(SOURCE_TYPES) | dynamic | {'rails.project'})


def _join_given(root_given, *parts):
    """Join further path segments onto a user-given path STRING for display
    only — never resolved/normalized, so a relative rails.project root like
    `./railsapp` still reads `./railsapp/db/schema.rb` in the expanded
    rails.schema half's own messages, not whatever normalize_input_specs's
    early .resolve() turned the root into."""
    root = root_given.rstrip('/')
    return '/'.join((root, *parts)) if root else '/'.join(parts)


def _expand_rails_project(spec):
    """D4 macro expansion: a `rails.project` source's path is a Rails project
    root, expanded (in place, before dispatch ever sees it) into a
    'rails.schema' spec for root/db/schema.rb and/or a 'rails.models' spec
    for root/app/models — whichever exist. Neither existing is a hard error;
    exactly one existing proceeds with a stderr note naming what was
    skipped (both existing is the common case and stays silent). Messages
    name the paths via the user-given root string (`spec['given']`), not the
    resolved `spec['path']` — see the InputSpec shape note above."""
    sid, root, given_root = spec['id'], spec['path'], spec['given']
    schema_path, models_path = root / 'db' / 'schema.rb', root / 'app' / 'models'
    given_schema = _join_given(given_root, 'db', 'schema.rb')
    given_models = _join_given(given_root, 'app', 'models')
    has_schema, has_models = schema_path.is_file(), models_path.is_dir()
    if not has_schema and not has_models:
        sys.exit(f"Error: source {sid!r}: rails.project found neither {given_schema} "
                 f"nor {given_models} under {given_root}")
    allow_empty = spec.get('allow_empty', False)
    expanded = []
    if has_schema:
        expanded.append({'id': f'{sid}:schema', 'type': 'rails.schema',
                         'path': schema_path, 'given': given_schema,
                         'allow_empty': allow_empty})
    else:
        print(f"Note: source {sid!r}: rails.project found no {given_schema} — "
              "skipping its rails.schema half", file=sys.stderr)
    if has_models:
        expanded.append({'id': f'{sid}:models', 'type': 'rails.models',
                         'path': models_path, 'given': given_models,
                         'allow_empty': allow_empty})
    else:
        print(f"Note: source {sid!r}: rails.project found no {given_models} — "
              "skipping its rails.models half", file=sys.stderr)
    return expanded


def normalize_input_specs(models_list, config_sources):
    """Build the deterministic, ordered InputSpec list merge_ir's layers come
    from (D4): config `sources` first, in declared order (a `rails.project`
    entry expands in place to its rails.schema/rails.models pair — see
    _expand_rails_project — so it never reaches dispatch as its own type),
    then each legacy --models / config `models` entry (id `models[<i>]`,
    type None — auto-detected at dispatch), preserving their given order.
    Later entries win same-kind merge ties (existing merge rule); CLI
    --models sorting after config `sources` is consistent with "CLI wins
    over config". Each spec keeps the user's original path STRING alongside
    the resolved Path (see the InputSpec shape note above)."""
    specs = []
    for s in config_sources:
        spec = {'id': s['id'], 'type': s['type'], 'given': s['path'],
                'path': Path(s['path']).expanduser().resolve(),
                'allow_empty': bool(s.get('allow_empty'))}
        if spec['type'] == 'rails.project':
            specs.extend(_expand_rails_project(spec))
        else:
            specs.append(spec)
    for i, m in enumerate(models_list):
        specs.append({'id': f'models[{i}]', 'type': None, 'given': m,
                      'path': Path(m).expanduser().resolve(),
                      'allow_empty': False})
    return specs


def run_input_specs(specs, table_map):
    """Dispatch every InputSpec (in order) to its ProviderResult, printing a
    per-source progress line and forwarding every warning the provider
    returns to stderr (D4 — the first real consumer of ProviderResult
    warnings)."""
    results = []
    for spec in specs:
        result = (_run_typed_spec(spec, table_map) if spec['type'] is not None
                  else _run_untyped_spec(spec, table_map))
        for w in result['warnings']:
            print(f'Warning: {w}', file=sys.stderr)
        results.append(result)
    return results


def _run_typed_spec(spec, table_map):
    sid, stype, path, given = spec['id'], spec['type'], spec['path'], spec['given']
    builder = _source_type_builder(stype)
    if builder is None:
        sys.exit(f"Error: source {sid!r}: unknown type {stype!r} "
                 f"(known types: {', '.join(known_source_type_names())})")
    if not path.exists():
        sys.exit(f"Error: source {sid!r}: {given} does not exist")
    if stype in _FILE_SOURCE_TYPES and not path.is_file():
        sys.exit(f"Error: source {sid!r}: {stype} expects {_FILE_SOURCE_TYPES[stype]}, "
                 f"got {given}")
    result = builder(spec, table_map)
    # A typed source that parses NOTHING is almost always a path/type mismatch
    # (e.g. a Prisma project declared as rails.models): the path exists, the
    # named parser runs, finds nothing it recognises, and — without this check
    # — the run would "succeed" with a silently empty layer. Same philosophy
    # as `relations`: being silently ignored is the worst failure mode, so
    # fail loud and name the layout the type wanted. `allow_empty: true` on
    # the source is the explicit opt-in for a genuinely empty input.
    if not result['tables'] and not spec.get('allow_empty'):
        sys.exit(f"Error: source {sid!r}: {stype} found nothing to parse at {given} "
                 f"(expected {_source_type_expects(stype)}). If an empty result is "
                 "intended, set `allow_empty: true` on this source.")
    print(f'Merged {result["source"]["provider"]} {_progress_noun(result)} from {given}',
          file=sys.stderr)
    return result


def _source_type_expects(type_name):
    """Human description of the input layout a sources[].type wants, for the
    empty-result error above: the static registry's types describe themselves
    here; a '<overlay>.models' type quotes the overlay's own `expects`."""
    if type_name == 'rails.schema':
        return 'a db/schema.rb with create_table blocks'
    for cls in FRAMEWORK_OVERLAYS:
        if f'{cls.name}.models' == type_name:
            return cls.expects or f'input the {cls.name} overlay can parse'
    return f'input the {type_name} parser can parse'


def _progress_noun(result):
    """D4's per-source progress line uses 'tables' for a schema-kind layer
    (rails.schema's contribution is columns/indexes/PK, not code semantics)
    and keeps the existing 'associations' wording for every other kind
    (framework layers — tests may match this exact phrase)."""
    return 'tables' if result['source']['kind'] == 'schema' else 'associations'


def _run_untyped_spec(spec, table_map):
    """Legacy --models / config `models` auto-detection (today's behavior),
    plus ambiguity reporting (D4b/c): when more than one FrameworkOverlay
    matches, the winner is unchanged (framework_overlay_for's own priority
    order) but now a stderr note names the runner-up(s) and points at
    `sources[].type` as the way to pin it down explicitly."""
    sid, path, given = spec['id'], spec['path'], spec['given']
    if not path.exists():
        sys.exit(f'Error: {given} does not exist')
    if path.is_file() and path.name == 'schema.rb':
        print(f'Note: {given} auto-detected as rails.schema (declare it in config '
              'sources to make this explicit)', file=sys.stderr)
        result = rails_schema_provider(path, given=given)
        print(f'Merged {result["source"]["provider"]} {_progress_noun(result)} from {given}',
              file=sys.stderr)
        return result
    matches = framework_overlays_matching(path)
    if not matches:
        sys.exit(f'Error: could not detect the code kind at {given} (expected a Rails '
                 'app/models dir, a schema.prisma, a Django project, or a db/schema.rb '
                 'file — declare `sources[].type` in the config to be explicit)')
    winner = matches[0]
    if len(matches) > 1:
        names = ', '.join(c.name for c in matches)
        print(f'Note: {given} matched multiple frameworks ({names}); using {winner.name}. '
              f'Declare sources[].type in the config to override.', file=sys.stderr)
    result = winner().build(path, table_map)
    print(f'Merged {result["source"]["provider"]} {_progress_noun(result)} from {given}',
          file=sys.stderr)
    return result
# ---------------------------------------------------------------------------
# Provider dispatchers (DB) + config layer construction/validation.
#
# The framework overlays and their dispatcher (framework_provider /
# detect_code_source) live in the frameworks/ package; db_provider dispatches
# through the db/ adapter registry. What remains here is the DB provider seam
# and everything that builds/validates the config layer.
# ---------------------------------------------------------------------------
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
    """DB ProviderResult (§5). Dispatches on the URL scheme to the registered
    DBAdapter (built-in MySQL/PostgreSQL, or a user adapter loaded via
    --adapter) and packages the IR with a password-free location. The built-in
    adapters delegate to the module-level parse_mysql/parse_postgres, so the
    test harness's monkeypatch of those still applies."""
    scheme = urlparse(url).scheme
    adapter_cls = db_adapter_for(scheme)
    if adapter_cls is None:
        known = ', '.join(sorted(DB_ADAPTERS)) or '(none registered)'
        sys.exit(f'Error: no database adapter for URL scheme {scheme!r} '
                 f'(known schemes: {known})')
    adapter = adapter_cls()
    return make_provider_result('db', adapter.name, adapter.fetch(url),
                                location=_password_free_url(url))

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
            # Sol relaxation #3: a polymorphic belongs_to's target is a
            # SYNTHETIC, tableless name (Django's pluralized model name,
            # Rails' association name) — never a real table, by design (see
            # emit.py's _canonical_associations docstring on the same
            # exemption for --emit-json). Only a non-polymorphic association
            # needs its target to actually exist.
            if target and not a.get('polymorphic') and target not in tables:
                sys.exit(f"Error: {label} association {a.get('name')!r} on {tname!r} "
                         f"references unknown target table {target!r}")
            # a declared foreign_key must name a real column on the SOURCE table
            # — except a schema_missing table (Rails-only: no DB columns at
            # all, per merge.py's schema_missing derivation), where a
            # foreign_key is Rails' *convention* column, not a real one this
            # release ever observes (Sol relaxation #4: --emit-config's
            # round trip of a Rails-only belongs_to must not be rejected for
            # naming a column that was never going to appear).
            fk = a.get('foreign_key')
            if fk and fk not in col_names and not tables[tname].get('schema_missing'):
                sys.exit(f"Error: {label} association {a.get('name')!r} on {tname!r} "
                         f"declares foreign_key {fk!r} which does not exist in "
                         f"{tname!r}'s merged columns")

# ---------------------------------------------------------------------------
# `notes:` semantic validation + viewer-ready resolution (notes Phase 1).
#
# Called from cli._finish, AFTER --infer-fk has added its guessed relations to
# `tables` (Sol finding #3: a note can target an inferred relation, since this
# validates against the FINAL final-IR-plus-inferred-relations, not the
# pre-infer merge_ir output) and BEFORE --only/--exclude filtering (so a note
# on an about-to-be-excluded table still gets full semantic validation against
# the complete schema — _finish filters the RESOLVED notes down afterward).
# This is also after the final merge_ir (same final IR validate_config_
# references checks), so a note may target a table/association added by
# config.tables, and a note targeting something config.tables DROPPED is
# correctly an error here even though it was syntactically fine at load time
# (§6.4②-style two stage split, mirrored from validate_config_references
# above).
#
# notes are a pure sidecar: this function only READS `tables` (never mutates
# it, never feeds anything back into layers/merge_ir/ProviderResult/
# provenance/fk_columns) and returns a new, viewer-ready list. Every error
# includes the note's `id`, per the Phase 1 contract.
# ---------------------------------------------------------------------------
def resolve_and_validate_notes(notes, tables, label):
    """Semantic-validate config `notes` against the FINAL merged IR and
    resolve each `relation` note to the one association it identifies, so the
    viewer can match on a fully-resolved identity instead of re-implementing
    relation lookup in JS. Hard error via sys.exit (note id always included)."""
    out = []
    for n in notes:
        note_id = n['id']
        target = n['target']
        ttype = target['type']
        if ttype == 'global':
            entry = {'id': note_id, 'scope': 'global'}
        elif ttype == 'table':
            tname = target['table']
            if tname not in tables:
                sys.exit(f"Error: {label} note {note_id!r}: unknown table {tname!r} "
                         "(not in the final schema)")
            entry = {'id': note_id, 'scope': 'table', 'table': tname}
        else:  # relation
            src, tgt = target['source_table'], target['target_table']
            if src not in tables:
                sys.exit(f"Error: {label} note {note_id!r}: unknown source_table {src!r} "
                         "(not in the final schema)")
            cands = [a for a in tables[src]['associations'] if a['target'] == tgt]
            # Sol relaxation #2: an OMITTED key is a wildcard (don't narrow on
            # it at all); an explicit `null` narrows to "this field is
            # absent on the match" — `a.get(key)` is already None for an
            # association that never carries that optional key, so testing
            # `'key' in target` (not `target.get(key) is not None`) is what
            # makes the two cases distinguishable. This is what lets
            # --emit-config's reverse note mapping (which always emits every
            # one of these keys, explicit-null where the resolved
            # association has no value) reimport back to exactly the one
            # association it came from, instead of the null being silently
            # read as "don't care" and re-widening the match.
            if 'foreign_key' in target:
                cands = [a for a in cands if a.get('foreign_key') == target['foreign_key']]
            if 'name' in target:
                cands = [a for a in cands if a['name'] == target['name']]
            if 'through' in target:
                cands = [a for a in cands if a.get('through') == target['through']]
            # Sol finding #5: narrow by association TYPE (has_many/belongs_to/
            # has_one/has_and_belongs_to_many) — lets a note pick out e.g. a
            # has_many among a has_many/has_one pair that otherwise share name
            # and target. Config key is `assoc_type` (not `type` — that name is
            # already the note's own target-kind discriminator), but it
            # narrows against the association's real `type` field.
            if 'assoc_type' in target:
                cands = [a for a in cands if a['type'] == target['assoc_type']]
            # `polymorphic` is tri-state: absent = don't care, True/False both
            # narrow (previously only `is True` narrowed, so `polymorphic:
            # false` was silently ignored as a filter — Sol finding #5).
            # config.py's syntactic check rejects `polymorphic: null` outright
            # (it must be a real bool when the key is present at all), so
            # `'polymorphic' in target` never sees an explicit null here.
            if 'polymorphic' in target:
                cands = [a for a in cands if bool(a.get('polymorphic')) == target['polymorphic']]
            if not cands:
                sys.exit(f"Error: {label} note {note_id!r}: no relation from {src!r} to "
                         f"{tgt!r} matches (check source_table/target_table/foreign_key/"
                         "name/through/assoc_type/polymorphic)")
            if len(cands) > 1:
                sys.exit(f"Error: {label} note {note_id!r}: ambiguous — {len(cands)} "
                         f"relations from {src!r} to {tgt!r} match; add foreign_key/name/"
                         "through/assoc_type to disambiguate")
            a = cands[0]
            # Resolved relation entry — SHARED CONTRACT with the viewer (do not
            # diverge): id/scope/source_table/target/type/name/foreign_key/
            # through/polymorphic, where every field except id/scope/
            # source_table/target is the RESOLVED association `a`'s real value
            # (not the note's possibly-partial narrowing target). `type` is
            # ALWAYS included now (Sol finding #5) so the viewer can match on
            # role the same way this function just did.
            entry = {'id': note_id, 'scope': 'relation', 'source_table': src,
                     'target': tgt, 'type': a['type'], 'name': a['name'],
                     'foreign_key': a.get('foreign_key'), 'through': a.get('through'),
                     'polymorphic': bool(a.get('polymorphic'))}
        if n.get('title'):
            entry['title'] = n['title']
        entry['text'] = n['text']
        if n.get('links'):
            entry['links'] = n['links']
        out.append(entry)
    return out

# ---------------------------------------------------------------------------
# `groups:` semantic validation + viewer-ready resolution (groups Phase 1).
#
# Called from cli._finish, mirroring resolve_and_validate_notes above: AFTER
# --infer-fk (groups don't care about associations, but validating against the
# same final IR keeps the two sidecars consistent) and BEFORE --only/--exclude
# filtering (so a group is fully semantic-validated against the complete
# schema; _finish filters the RESOLVED groups' membership down afterward).
#
# groups are a pure sidecar: this function only READS `tables` (never mutates
# it, never feeds anything back into layers/merge_ir/ProviderResult/
# provenance/fk_columns) and returns a new, viewer-ready list. Every error
# includes the group's `id`, per the Phase 1 contract.
#
# Phase 1 scope: NO overlapping membership — a table claimed by two groups is
# a hard error (naming both group ids and the table), not a silently-picked
# winner. Layout affinity (placing group members near each other) is
# explicitly out of scope for this PR (DESIGN_ROADMAP §P2 follow-up).
# ---------------------------------------------------------------------------
def resolve_and_validate_groups(groups, tables, label):
    """Semantic-validate config `groups` against the FINAL merged IR: every
    member table must exist, and no table may belong to more than one group.
    Returns a viewer-ready list of {'id', 'tables':[...], 'title'?, 'color'?}
    (title/color present only when configured). Hard error via sys.exit
    (group id always included)."""
    out = []
    owner = {}  # table -> group id that already claimed it
    for g in groups:
        group_id = g['id']
        for t in g['tables']:
            if t not in tables:
                sys.exit(f"Error: {label} group {group_id!r}: unknown table {t!r} "
                         "(not in the final schema)")
            if t in owner:
                sys.exit(f"Error: {label} group {group_id!r}: table {t!r} already "
                         f"belongs to group {owner[t]!r} (a table may belong to only "
                         "one group)")
            owner[t] = group_id
        entry = {'id': group_id, 'tables': list(g['tables'])}
        if g.get('title'):
            entry['title'] = g['title']
        if g.get('color'):
            entry['color'] = g['color']
        out.append(entry)
    return out

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

def write_excel(tables, path, title, template_path=None, notes=None, groups=None):
    """`notes`/`groups` (backlog #4, activating the Phase 1 wiring): a Notes
    sheet and a Groups sheet are appended when either is non-empty, and the
    overview sheet gains a trailing Group column when `groups` is non-empty.
    Both additions are fully omitted (not just left empty) when there's
    nothing to show, so a run with no notes/groups still produces
    byte-identical output to before this feature existed."""
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
    # table -> its group's display label (title if set, else the group id).
    # Phase 1 groups have non-overlapping membership (resolve_and_validate_
    # groups), so a table maps to at most one label here.
    group_of = {}
    for g in (groups or []):
        label = g.get('title') or g['id']
        for tn in g.get('tables', []):
            group_of[tn] = label
    header = [('#', S_HEADER), ('Table', S_HEADER), ('Comment', S_HEADER),
              ('Columns', S_HEADER), ('Indexes', S_HEADER), ('Missing schema', S_HEADER)]
    widths = [5, 32, 50, 10, 10, 14]
    if groups:  # column omitted entirely (not left blank) when there are no groups
        header.append(('Group', S_HEADER))
        widths.append(20)
    rows = [[(f'{title} — table definitions', S_TITLE)], [], header]
    links = []
    for i, n in enumerate(names, 1):
        t = tables[n]
        r = len(rows) + 1
        s = alt(i - 1)
        row = [(i, s), (n, s), (t.get('comment', ''), s), (len(t['columns']), s),
               (len(t.get('indexes', [])), s),
               ('yes' if t.get('schema_missing') else '', s)]
        if groups:
            row.append((group_of.get(n, ''), s))
        rows.append(row)
        links.append((f'B{r}', f"'{sheet_of[n]}'", n))
    overview = _sheet_xml(rows, widths=widths, links=links)

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
                       'schema FK' if a.get('schema_fk') else
                       'inferred' if a.get('inferred') else
                       'manual' if a.get('manual') else 'code')
                s = alt(i)
                rows.append([(a['type'], s), (a['name'], s), (a['target'], s), (via, s)])
        sheets.append((sheet_of[n],
                       _sheet_xml(rows, widths=[12, 28, 24, 10, 18, 6, 16, 50])))

    # ── notes sheet (backlog #4) — omitted entirely when there are no notes ──
    if notes:
        rows = [[(f'{title} — notes', S_TITLE)], [],
                [('#', S_HEADER), ('ID', S_HEADER), ('Scope', S_HEADER), ('Target', S_HEADER),
                 ('Title', S_HEADER), ('Text', S_HEADER), ('Links', S_HEADER)]]
        for i, n in enumerate(sorted(notes, key=lambda n: n['id']), 1):
            if n['scope'] == 'global':
                target = ''
            elif n['scope'] == 'table':
                target = n['table']
            else:  # relation
                target = f"{n['source_table']} → {n['target']}"
            link_text = '; '.join((f"{l['label']} " if l.get('label') else '') + l['url']
                                  for l in n.get('links') or [])
            s = alt(i - 1)
            rows.append([(i, s), (n['id'], s), (n['scope'], s), (target, s),
                        (n.get('title', ''), s), (n['text'], s), (link_text, s)])
        sheets.append(('Notes', _sheet_xml(rows, widths=[5, 12, 12, 30, 20, 60, 40])))

    # ── groups sheet (backlog #4) — omitted entirely when there are no groups ──
    if groups:
        rows = [[(f'{title} — groups', S_TITLE)], [],
                [('#', S_HEADER), ('Group', S_HEADER), ('Title', S_HEADER),
                 ('Color', S_HEADER), ('Tables', S_HEADER)]]
        for i, g in enumerate(sorted(groups, key=lambda g: g['id']), 1):
            s = alt(i - 1)
            rows.append([(i, s), (g['id'], s), (g.get('title', ''), s), (g.get('color', ''), s),
                        (', '.join(sorted(g.get('tables', []))), s)])
        sheets.append(('Groups', _sheet_xml(rows, widths=[5, 16, 24, 12, 60])))

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
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__TITLE__</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px;
  background:#f1f5f9;color:#1e293b;height:100vh;overflow:hidden;display:flex;flex-direction:column}

/* ── top bar ── */
#topbar{height:44px;background:#1e293b;color:#f8fafc;display:flex;align-items:center;
  padding:0 14px;gap:14px;flex-shrink:0;user-select:none}
#topbar h1{font-size:14px;font-weight:700;letter-spacing:.3px;white-space:nowrap}
#topbar .sep{flex:1}
#topbar label{display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px;color:#94a3b8}
#topbar label input[type=checkbox]{accent-color:#3b82f6;width:14px;height:14px;cursor:pointer}
#topbar label:hover{color:#f8fafc}
#topbar .ae-on{color:#93c5fd}
#depth-ctrl{display:none;align-items:center;gap:4px;font-size:11px;color:#64748b}
#depth-ctrl.visible{display:flex}
#depth-ctrl span,#dir-ctrl span{color:#64748b;font-size:11px}
#dir-ctrl{display:flex;align-items:center;gap:4px;font-size:11px;color:#64748b}
.dep-btn{padding:2px 7px;font-size:11px;border:1px solid #334155;background:transparent;
  color:#94a3b8;border-radius:4px;cursor:pointer}
.dep-btn:hover{border-color:#94a3b8;color:#f8fafc}
.dep-btn.active{background:#3b82f6;border-color:#3b82f6;color:#fff}
#info-bar{font-size:11px;color:#475569;white-space:nowrap}
#view-sel{background:#0f172a;color:#cbd5e1;border:1px solid #334155;border-radius:4px;
  font-size:11px;padding:2px 4px;max-width:150px}
.tb-btn{background:transparent;color:#94a3b8;border:1px solid #334155;border-radius:4px;
  font-size:11px;padding:2px 7px;cursor:pointer;white-space:nowrap}
.tb-btn:hover{color:#f8fafc;border-color:#94a3b8}

/* ── main layout ── */
#main{display:flex;flex:1;overflow:hidden;min-height:0}

/* ── left pane ── */
#left-pane{width:220px;flex-shrink:0;background:#fff;border-right:1px solid #e2e8f0;
  display:flex;flex-direction:column;overflow:hidden;min-height:0}
#left-pane.collapsed,#right-pane.collapsed{display:none}
.pane-title{padding:9px 12px 7px;font-size:10px;font-weight:700;color:#64748b;
  text-transform:uppercase;letter-spacing:.7px;border-bottom:1px solid #f1f5f9;flex-shrink:0;
  display:flex;align-items:center;justify-content:space-between}
.collapse-btn{border:none;background:none;cursor:pointer;color:#94a3b8;font-size:10px;
  padding:0 2px;line-height:1}
.collapse-btn:hover{color:#1e293b}
.divider{width:5px;flex-shrink:0;cursor:col-resize;background:transparent;transition:background .15s}
.divider:hover,.divider.dragging{background:#93c5fd}
.expand-tab{position:absolute;top:50%;transform:translateY(-50%);width:18px;height:52px;
  background:#fff;border:1px solid #e2e8f0;cursor:pointer;display:none;align-items:center;
  justify-content:center;color:#64748b;font-size:9px;box-shadow:0 1px 3px rgba(0,0,0,.1);z-index:5;padding:0}
.expand-tab.visible{display:flex}
.expand-tab:hover{background:#f1f5f9;color:#1e293b}
#expand-left{left:0;border-left:none;border-radius:0 6px 6px 0}
#expand-right{right:0;border-right:none;border-radius:6px 0 0 6px}
#canvas-empty{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
  display:none;color:#94a3b8;font-size:13px;text-align:center;line-height:2;pointer-events:none}
#canvas-empty.visible{display:block}
#left-controls{display:flex;gap:4px;padding:7px 8px;border-bottom:1px solid #f1f5f9;flex-shrink:0;flex-wrap:wrap}
#left-controls button{flex:1;min-width:0;padding:4px 5px;font-size:10px;border:1px solid #e2e8f0;
  background:#f8fafc;border-radius:4px;cursor:pointer;color:#475569;white-space:nowrap}
#left-controls button:hover{background:#e2e8f0}
#search-box{padding:6px 8px;border-bottom:1px solid #f1f5f9;flex-shrink:0;
  display:flex;align-items:center;gap:2px;border:1px solid transparent}
#search-box input{flex:1;min-width:0;padding:4px 8px;font-size:12px;border:1px solid #e2e8f0;
  border-radius:4px;outline:none;background:#f8fafc}
#search-box input:focus{border-color:#3b82f6;background:#fff}
#search-box.bad-re input{border-color:#dc2626}
/* Aa / .* mode toggles — shared by both search boxes (left-pane filter
   and toolbar Highlight). Blue active state, not amber: amber elsewhere
   in word-search means "this has matches," and these buttons must not
   be confused with match-state indicators. */
.srch-tgl{border:1px solid transparent;background:none;cursor:pointer;flex-shrink:0;
  width:20px;height:20px;border-radius:4px;padding:0;line-height:1;
  font:600 10px ui-monospace,'SF Mono','Fira Code',monospace;color:#94a3b8}
.srch-tgl:hover{color:#475569;background:#f1f5f9}
.srch-tgl.active{color:#1d4ed8;background:#dbeafe;border-color:#93c5fd}
body.dark .srch-tgl{color:#64748b}
body.dark .srch-tgl:hover{color:#cbd5e1;background:#334155}
body.dark .srch-tgl.active{color:#93c5fd;background:#1e3a5f;border-color:#3b82f6}
#table-list{flex:1;overflow-y:auto;padding:3px 0;min-height:0}
.table-item label{display:flex;align-items:center;gap:7px;width:100%;
  padding:4px 10px;cursor:pointer;color:#334155}
.table-item label:hover{background:#f1f5f9}
.table-item.selected label{background:#f0f7ff}
.table-item.focused label{background:#eff6ff;color:#1d4ed8;font-weight:600}
.table-item label input[type=checkbox]{accent-color:#3b82f6;flex-shrink:0}
.tname{flex:0 1 auto;min-width:0;font-size:11px;font-family:'SF Mono','Fira Code',monospace;
  white-space:normal;overflow-wrap:anywhere}
.tlogical{flex:1;min-width:0;font-size:10px;color:#94a3b8;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tail{margin-left:auto;display:flex;align-items:center;gap:4px;flex-shrink:0}
.rel-badge{font-size:10px;background:#f1f5f9;color:#94a3b8;padding:1px 4px;border-radius:3px;flex-shrink:0}
.col-hit{font-size:9px;background:#ecfeff;color:#0e7490;padding:1px 4px;border-radius:3px;flex-shrink:0;
  font-family:'SF Mono','Fira Code',monospace}
.table-item.focused .rel-badge{background:#dbeafe;color:#3b82f6}
/* toolbar word-search hit — an inset bar, not another badge next to the
   cyan ⌕ one above (that's the left-pane filter's own column-hit marker;
   two visually-similar badges from two different searches would be
   genuinely ambiguous about which search found what) */
.table-item.word-hit label{box-shadow:inset 3px 0 0 #f59e0b}
body.dark .table-item.word-hit label{box-shadow:inset 3px 0 0 #fbbf24}
/* auto-expanded (in current focus view) indicator */
.table-item.inview .tname::after{content:'●';color:#3b82f6;font-size:7px;margin-left:5px;vertical-align:middle}
/* fully-hidden (banned) tables */
.hide-btn{visibility:hidden;border:none;background:none;cursor:pointer;font-size:10px;
  padding:0 2px;opacity:.45;flex-shrink:0;line-height:1;filter:grayscale(1)}
.table-item label:hover .hide-btn{visibility:visible}
.hide-btn:hover{opacity:1;filter:none}
.table-item.hidden .hide-btn{visibility:visible;opacity:1;filter:none}
.table-item.hidden .tname{color:#cbd5e1;text-decoration:line-through}
.table-item.hidden .rel-badge{opacity:.4}
.table-item.hidden label input[type=checkbox]{opacity:.35}
#hidden-bar{display:none;padding:5px 12px;font-size:11px;color:#b91c1c;background:#fef2f2;
  border-bottom:1px solid #fee2e2;flex-shrink:0;align-items:center;justify-content:space-between}
#hidden-bar.visible{display:flex}
#hidden-bar a{color:#3b82f6;cursor:pointer;text-decoration:none;font-size:10px}
#hidden-bar a:hover{text-decoration:underline}

/* ── center pane ── */
#center-pane{flex:1;position:relative;overflow:hidden;min-height:0;background:#f8fafc;
  background-image:linear-gradient(rgba(148,163,184,.13) 1px,transparent 1px),
    linear-gradient(90deg,rgba(148,163,184,.13) 1px,transparent 1px);
  background-size:24px 24px}
#er-svg{width:100%;height:100%;cursor:grab;display:block}
#er-svg.panning{cursor:grabbing}
#er-svg.node-drag{cursor:grabbing}
.snap-guide{stroke:#ef4444;stroke-width:1;stroke-dasharray:4 3;vector-effect:non-scaling-stroke;pointer-events:none}
body.dark .snap-guide{stroke:#f87171}
.marquee-rect{fill:rgba(59,130,246,.12);stroke:#3b82f6;stroke-width:1;vector-effect:non-scaling-stroke;pointer-events:none}

/* ── toolbar ── */
#diagram-toolbar{position:absolute;bottom:12px;right:12px;left:12px;display:flex;flex-wrap:wrap;
  gap:4px;align-items:center;justify-content:flex-end}
.diag-btn{height:30px;min-width:30px;padding:0 8px;background:white;border:1px solid #e2e8f0;
  border-radius:6px;display:flex;align-items:center;justify-content:center;cursor:pointer;
  font-size:13px;color:#475569;box-shadow:0 1px 3px rgba(0,0,0,.08);white-space:nowrap}
.diag-btn:hover{background:#f1f5f9}
.diag-btn.active{background:#3b82f6;border-color:#3b82f6;color:#fff}
.diag-btn:disabled{opacity:.4;cursor:not-allowed}
.diag-btn:disabled:hover{background:white}
#colmode-group,#namemode-group,.seg-group{display:flex;box-shadow:0 1px 3px rgba(0,0,0,.08);border-radius:6px}
#colmode-group .diag-btn,#namemode-group .diag-btn,.seg-group .diag-btn{box-shadow:none;border-radius:0;font-size:11px;margin-left:-1px}
#colmode-group .diag-btn:first-child,#namemode-group .diag-btn:first-child,.seg-group .diag-btn:first-child{border-radius:6px 0 0 6px;margin-left:0}
#colmode-group .diag-btn:last-child,#namemode-group .diag-btn:last-child,.seg-group .diag-btn:last-child{border-radius:0 6px 6px 0}
.tb-popup .seg-group{box-shadow:none;border:1px solid #e2e8f0}
body.dark .tb-popup .seg-group{border-color:#334155}
.tb-popup .seg-group .diag-btn{width:auto;flex:1;font-size:10px;padding:0 6px}
/* toolbar grouping — clusters of related controls (what tables show /
   where they sit / how you're viewing them / getting output out),
   separated by a thin rule rather than merging into one pill: unlike
   colmode-group these aren't a single radio control, just neighbors */
.tb-group{display:flex;gap:4px;align-items:center;flex-wrap:nowrap}
.tb-sep{width:1px;height:18px;background:#e2e8f0;flex-shrink:0}
body.dark .tb-sep{background:#334155}
#export-group{position:relative}
.tb-popup{position:absolute;bottom:36px;right:0;display:none;flex-direction:column;gap:4px;
  background:white;border:1px solid #e2e8f0;border-radius:8px;padding:6px;
  box-shadow:0 4px 16px rgba(0,0,0,.15);min-width:220px;z-index:7}
.tb-popup.open{display:flex}
.tb-popup .diag-btn{width:100%;justify-content:flex-start;font-size:11px;padding:0 10px}
body.dark .tb-popup{background:#1e293b;border-color:#334155}
.tb-popup-caption{font-size:10px;font-weight:700;color:#94a3b8;text-transform:uppercase;
  letter-spacing:.5px;padding:2px 6px}
.tb-popup-check{display:flex;align-items:center;gap:6px;font-size:11px;color:#334155;
  padding:3px 6px;cursor:pointer;border-radius:4px}
.tb-popup-check:hover{background:#f1f5f9}
.tb-popup-check input{accent-color:#3b82f6}
.tb-popup-sep{height:1px;background:#e2e8f0;margin:2px 4px}
/* one row per export format: a label plus copy/download side by side —
   keeps 4 formats x 2 actions to 4 compact rows instead of 8 stacked
   full-width buttons */
.tb-export-row{display:grid;grid-template-columns:60px 1fr 1fr;gap:4px;align-items:center;padding:0 6px}
.tb-export-fmt{font-size:11px;font-weight:600;color:#475569}
.tb-export-row .diag-btn{width:100%;justify-content:center;font-size:10px;padding:0 4px}
body.dark .tb-popup-check{color:#cbd5e1}
body.dark .tb-popup-check:hover{background:#334155}
body.dark .tb-popup-sep{background:#334155}
body.dark .tb-export-fmt{color:#cbd5e1}
#help-group{position:relative}
#help-menu{min-width:280px}
.help-row{display:grid;grid-template-columns:1fr auto;gap:12px;align-items:baseline;
  font-size:11px;color:#334155;padding:2px 6px}
.help-k{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:10px;color:#475569;white-space:nowrap}
.help-link{font-size:11px;padding:3px 6px;color:#2563eb;text-decoration:none}
.help-link:hover{text-decoration:underline}
body.dark .help-row{color:#cbd5e1}
body.dark .help-k{color:#94a3b8}
body.dark .help-link{color:#60a5fa}

/* ── word-search / highlight (toolbar) ── */
#word-search-box{height:30px;display:flex;align-items:center;gap:4px;padding:0 4px 0 8px;
  background:white;border:1px solid #e2e8f0;border-radius:6px;box-shadow:0 1px 3px rgba(0,0,0,.08);
  margin-right:auto}
#word-search-box.bad-re{border-color:#dc2626}
#word-search{width:90px;border:none;outline:none;font-size:12px;background:transparent;
  transition:width .15s}
#word-search:focus{width:150px}
#word-search-count{font-size:10px;color:#94a3b8;min-width:12px;text-align:center;flex-shrink:0}
#word-search-box.bad-re #word-search-count{color:#dc2626;font-weight:700}
#word-search-clear{border:none;background:none;cursor:pointer;font-size:10px;color:#94a3b8;
  padding:2px 4px;line-height:1;flex-shrink:0;visibility:hidden}
#word-search-box.has-query #word-search-clear{visibility:visible}
#word-search-box.has-query #word-search-count{color:#b45309}
#word-search-clear:hover{color:#475569}
body.dark #word-search-box{background:#1e293b;border-color:#334155}
body.dark #word-search{color:#e2e8f0}
body.dark #word-search-count{color:#64748b}
body.dark #word-search-box.has-query #word-search-count{color:#fbbf24}
body.dark #word-search-clear{color:#64748b}
body.dark #word-search-clear:hover{color:#cbd5e1}

/* ── focus bar (dialog-like header while focused) ── */
#focus-bar{position:absolute;top:0;left:0;right:0;height:36px;z-index:6;
  display:none;align-items:center;gap:10px;padding:0 12px;
  background:#eff6ff;border-bottom:2px solid #3b82f6;font-size:12px;color:#1e3a5f}
body.focus-mode #focus-bar{display:flex}
#focus-bar-label{font-weight:700;white-space:nowrap}
#focus-bar .fb-hint{color:#64748b;font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#focus-bar .fb-sep{flex:1}
#focus-bar button{border:1px solid #bfdbfe;background:#fff;border-radius:5px;
  padding:3px 9px;font-size:11px;color:#1d4ed8;cursor:pointer;white-space:nowrap}
#focus-bar button:hover{background:#dbeafe}
#focus-bar-close{font-weight:600}
body.focus-mode #legend{top:46px}
body.focus-mode #center-pane{box-shadow:inset 0 0 0 2px #3b82f6}
body.focus-mode #table-list input[type=checkbox]{opacity:.35}

/* ── legend ── */
#legend{position:absolute;top:10px;left:10px;background:rgba(255,255,255,.92);
  border:1px solid #e2e8f0;border-radius:6px;padding:5px 10px 7px;font-size:10px;
  color:#64748b}
#legend-head{display:flex;align-items:center;justify-content:space-between;gap:10px;
  font-weight:700;letter-spacing:.4px;color:#94a3b8;cursor:pointer;user-select:none}
#legend-toggle{border:none;background:none;cursor:pointer;color:#94a3b8;font-size:10px;padding:0}
#legend.collapsed #legend-body{display:none}
#legend.collapsed{padding:5px 10px}
#legend-body{margin-top:4px;pointer-events:none}
#legend .lr{display:flex;align-items:center;gap:6px;margin-bottom:2px}
#legend .lhint{margin-top:5px;padding-top:5px;border-top:1px solid #f1f5f9;color:#94a3b8;
  font-size:9px;line-height:1.6}

/* ── right pane ── */
#right-pane{width:280px;flex-shrink:0;background:#fff;border-left:1px solid #e2e8f0;
  display:flex;flex-direction:column;overflow:hidden;min-height:0}
#table-details{flex:1;overflow-y:auto;padding:12px;min-height:0}
.empty-state{color:#94a3b8;font-size:12px;text-align:center;margin-top:40px;line-height:1.8}
.detail-name{font-size:15px;font-weight:700;font-family:'SF Mono','Fira Code',monospace;
  color:#1e293b;margin-bottom:12px;padding-bottom:8px;border-bottom:2px solid #1e293b;word-break:break-all}
.word-mark{background:#fde68a;color:#713f12;border-radius:2px;padding:0 1px}
.sec-title{font-size:10px;font-weight:700;color:#64748b;text-transform:uppercase;
  letter-spacing:.6px;margin:14px 0 5px}
.col-list{display:flex;flex-direction:column;gap:2px}
.col-entry{padding:3px 6px;border-radius:4px;background:#f8fafc}
.badge{font-size:9px;font-weight:700;padding:1px 4px;border-radius:3px;flex-shrink:0;min-width:22px;text-align:center}
.bdg-pk{background:#fef08a;color:#713f12}
.bdg-fk{background:#bfdbfe;color:#1e3a5f}
.bdg-mt{background:transparent;min-width:22px}
.col-cn{font-family:monospace;font-size:11px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.col-ct{color:#64748b;font-size:10px;font-family:monospace;flex-shrink:0}
.col-null{color:#94a3b8;font-size:10px;flex-shrink:0}
.col-main{display:flex;align-items:center;gap:5px}
.col-comment{font-size:10px;color:#64748b;margin:1px 0 0 27px;line-height:1.4}
.tbl-comment{font-size:11px;color:#64748b;margin:-8px 0 10px;line-height:1.5}
.idx-list{display:flex;flex-direction:column;gap:3px}
.idx-entry{padding:4px 8px;border-radius:4px;background:#f8fafc;border-left:3px solid #cbd5e1}
.idx-name{font-family:monospace;font-size:11px;font-weight:600;color:#1e293b;display:flex;align-items:center;gap:6px}
.idx-cols{font-size:10px;color:#64748b;font-family:monospace}
.badge-uq{font-size:8px;background:#dcfce7;color:#166534;padding:0 4px;border-radius:3px;font-family:sans-serif}
.assoc-list{display:flex;flex-direction:column;gap:3px}
.assoc-entry{padding:5px 8px;border-radius:4px;background:#f8fafc;border-left:3px solid #e2e8f0}
.assoc-entry.t-has_many{border-left-color:#6366f1}
.assoc-entry.t-belongs_to{border-left-color:#10b981}
.assoc-entry.t-has_one{border-left-color:#f59e0b}
.assoc-entry.t-habtm,.assoc-entry.t-through{border-left-color:#ec4899}
.atype{font-size:10px;color:#64748b}
.badge-inf{font-size:9px;background:#fef9c3;color:#854d0e;padding:0 4px;border-radius:3px}
.badge-dbfk{font-size:9px;background:#dbeafe;color:#1e3a5f;padding:0 4px;border-radius:3px}
.badge-schemafk{font-size:9px;background:#ccfbf1;color:#0f766e;padding:0 4px;border-radius:3px}
.badge-manual{font-size:9px;background:#ede9fe;color:#5b21b6;padding:0 4px;border-radius:3px}
.aname{font-family:monospace;font-size:12px;font-weight:600;color:#1e293b}
.atarget{font-size:11px;color:#64748b;margin-top:1px}
.atarget a{color:#3b82f6;cursor:pointer;text-decoration:none}
.atarget a:hover{text-decoration:underline}
.atarget .not-in-view{color:#94a3b8;font-style:italic}
.athrough{font-size:10px;color:#94a3b8}
.atarget .add-target{color:#64748b;cursor:pointer;text-decoration:none;border-bottom:1px dashed #cbd5e1}
.atarget .add-target:hover{color:#1d4ed8;border-bottom-color:#1d4ed8}

/* ── notes (notes Phase 1) ── */
.note-list{display:flex;flex-direction:column;gap:4px}
.note-entry{padding:5px 8px;border-radius:4px;background:#f8fafc;border-left:3px solid #0d9488}
.note-title{font-size:11px;font-weight:700;color:#0f766e;margin-bottom:2px}
.note-text{font-size:11px;color:#334155;line-height:1.5;white-space:pre-wrap}
.note-links{margin-top:3px;display:flex;flex-direction:column;gap:1px}
.note-links a{font-size:10px;color:#0d9488;text-decoration:none}
.note-links a:hover{text-decoration:underline}
.assoc-notes{margin-top:4px}
.assoc-notes .note-entry{background:transparent;padding:4px 0 0 6px;border-left-width:2px}
#legend-notes{margin-top:6px;padding-top:6px;border-top:1px solid #f1f5f9}
#legend-notes:empty,#legend-notes[style*="display: none"]{border-top:none;padding-top:0;margin-top:0}
.lgn-title{font-size:9px;font-weight:700;color:#94a3b8;text-transform:uppercase;
  letter-spacing:.6px;margin-bottom:4px}
#legend-notes .note-entry{max-width:230px}
.note-hit{font-size:9px;background:#ccfbf1;color:#0f766e;padding:1px 4px;border-radius:3px;
  flex-shrink:0;margin-left:4px}
.table-item.note-banner label{cursor:pointer;color:#0f766e;font-size:11px;gap:6px}

/* ── multi-select align/distribute panel ── */
.msel-btns{display:grid;grid-template-columns:1fr 1fr;gap:5px}
.msel-btns .diag-btn{width:100%;font-size:11px;justify-content:flex-start;padding:0 10px}
.msel-btns .diag-btn:disabled{opacity:.4;cursor:not-allowed}
.msel-btns .diag-btn:disabled:hover{background:white}
.msel-list{display:flex;flex-direction:column;gap:3px}
.msel-chip{display:flex;align-items:center;justify-content:space-between;gap:6px;
  padding:4px 8px;border-radius:4px;background:#f8fafc}
.msel-cn{font-family:monospace;font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.msel-remove{border:none;background:none;color:#94a3b8;cursor:pointer;font-size:11px;flex-shrink:0;padding:0 2px}
.msel-remove:hover{color:#ef4444}

/* ── toast ── */
#toast{position:fixed;bottom:56px;left:50%;transform:translateX(-50%);
  background:#1e293b;color:#f8fafc;padding:8px 16px;border-radius:8px;
  font-size:12px;pointer-events:none;opacity:0;transition:opacity .25s;z-index:100}
#toast.show{opacity:1}

/* ── groups (groups Phase 1) — group-layer frames, backmost in #er-main ──
   fill/stroke COLOR itself comes from each group's inline style attribute
   (already hex-validated server-side); these classes only set the shape
   properties that don't vary per group. */
#group-layer{pointer-events:none}
.grp-rect{fill-opacity:.06;stroke-width:1.5;stroke-opacity:.55;pointer-events:none}
.grp-chip{cursor:move;pointer-events:auto}
.grp-label-bg{fill-opacity:.16;stroke:none}
.grp-label-text{fill:#1e293b;font-size:11px;font-weight:700;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;user-select:none}
body.dark .grp-rect{fill-opacity:.1;stroke-opacity:.7}
body.dark .grp-label-bg{fill-opacity:.28}
body.dark .grp-label-text{fill:#f1f5f9}

/* ── SVG node ── */
.er-node{cursor:pointer}
.er-node .n-shadow{fill:rgba(0,0,0,.07)}
.er-node .n-bg{fill:#fff;stroke:#cbd5e1;stroke-width:1}
.er-node.sel .n-bg{stroke:#3b82f6;stroke-width:2}
.er-node .n-hdr{fill:#1e293b}
/* auto-expanded (shown by expansion, not by its own checkbox) */
.er-node.auto .n-hdr{fill:#64748b}
.er-node.auto .n-bg{stroke-dasharray:5 3}
/* ✓ badge on expansion roots (checked tables) */
.er-node .n-root{fill:#4ade80;font-size:11px;font-weight:700}
.er-node.sel .n-hdr,.er-node.center .n-hdr{fill:#1d4ed8}
.er-node.flash .n-bg{animation:nodeflash 1.2s ease-out}
@keyframes nodeflash{0%{stroke:#f59e0b;stroke-width:8}100%{stroke:#3b82f6;stroke-width:2}}
.er-node .n-title{fill:#f8fafc;font-size:12px;font-weight:700;font-family:'SF Mono','Fira Code',monospace}
.er-node .n-title .n-logical{font-size:11px;font-weight:400;opacity:.7}
.er-node .n-title .n-namehit{fill:#f59e0b;opacity:1}
.er-node .n-alt{fill:#f8fafc}
.er-node .n-colhit{fill:#fef3c7}
/* toolbar word-search highlight — amber, distinct from the cyan/amber-ish
   colHighlight above so "found via Enter-locate" and "matches the live
   Highlight box" read as related but not identical signals */
.er-node.word-hit .n-bg{stroke:#f59e0b;stroke-width:2.5}
.er-node.word-dim{opacity:.45}
.er-node .n-wordhit{fill:#fde68a}
.er-node .n-cn{fill:#1e293b;font-size:11px;font-family:'SF Mono','Fira Code',monospace}
.er-node .n-ct{fill:#64748b;font-size:10px;font-family:'SF Mono','Fira Code',monospace}
.er-node .n-bpk{fill:#fef08a}
.er-node .n-bfk{fill:#bfdbfe}
.er-node .n-tpk{fill:#713f12;font-size:9px;font-weight:700;font-family:sans-serif}
.er-node .n-tfk{fill:#1e3a5f;font-size:9px;font-weight:700;font-family:sans-serif}
.er-node .n-more{fill:#94a3b8;font-size:10px;font-family:sans-serif}
.er-node .n-mode{fill:#64748b;font-size:11px;cursor:pointer}
.er-node .n-mode:hover{fill:#f8fafc}
.er-node .n-sctrack{fill:#e2e8f0}
.er-node .n-scthumb{fill:#94a3b8}
/* ring depth tint for focused view */
.er-node.ring-1 .n-hdr{fill:#1e3a5f}
.er-node.ring-2 .n-hdr{fill:#374151}
.er-node.ring-3 .n-hdr{fill:#4b5563}

/* ── SVG edge (standard ER notation: cardinality via end markers) ── */
.er-edge{pointer-events:none}
.er-edge path{fill:none;stroke:#64748b;stroke-width:1.5;opacity:.75}
.er-edge.t-nn path{stroke-dasharray:6 3}
/* inferred from FK column names (no explicit association) */
.er-edge.inf path{opacity:.4;stroke-dasharray:2 4}
/* edges touching the selected table */
.er-edge.hl path{stroke:#2563eb;stroke-width:2;opacity:1}
.er-edge.hl .e-ltxt{fill:#2563eb}
.er-edge .e-lbg{fill:white;opacity:.88}
.er-edge .e-ltxt{font-size:10px;font-family:sans-serif;fill:#64748b}
body.no-edge-labels .e-lbg,body.no-edge-labels .e-ltxt{display:none}
/* physical/logical name display mode — the node header always renders
   both tspans (see drawNode); which one(s) show is decided here, purely
   by CSS, so exports can apply a different mode via the same rule shape
   injected into the export stylesheet instead of a second render path */
body.namemode-physical .n-logical,body.namemode-physical .n-paren{display:none}
body.namemode-logical .er-node.has-logical .n-physical,
body.namemode-logical .er-node.has-logical .n-paren{display:none}
#legend .lsvg{flex-shrink:0}

/* ── dark mode ── */
body.dark{background:#0b1220;color:#cbd5e1}
body.dark #left-pane,body.dark #right-pane{background:#0f172a;border-color:#1e293b}
/* secondary/dimmed text in dark mode, one step brighter across the board
   than the first pass (still dimmer than primary text, just legible) */
body.dark .pane-title{color:#94a3b8;border-color:#1e293b}
body.dark #left-controls{border-color:#1e293b}
body.dark #left-controls button{background:#1e293b;border-color:#334155;color:#cbd5e1}
body.dark #left-controls button:hover{background:#334155}
body.dark #search-box{border-color:#1e293b}
body.dark #search-box input{background:#1e293b;border-color:#334155;color:#e2e8f0}
body.dark .table-item label{color:#cbd5e1}
body.dark .table-item label:hover{background:#1e293b}
body.dark .table-item.selected label{background:#172554}
body.dark .table-item.focused label{background:#1e3a8a;color:#bfdbfe}
body.dark .rel-badge{background:#1e293b;color:#94a3b8}
body.dark .col-hit{background:#164e63;color:#a5f3fc}
body.dark #center-pane{background:#0b1220;
  background-image:linear-gradient(rgba(148,163,184,.07) 1px,transparent 1px),
    linear-gradient(90deg,rgba(148,163,184,.07) 1px,transparent 1px)}
body.dark #legend,body.dark .diag-btn,body.dark .expand-tab{background:#0f172a;border-color:#334155;color:#cbd5e1}
body.dark .diag-btn:hover{background:#1e293b}
body.dark .diag-btn.active{background:#3b82f6;border-color:#3b82f6;color:#fff}
body.dark .diag-btn:disabled:hover{background:#0f172a}
body.dark #max-rows{background:#0f172a;color:#cbd5e1}
body.dark #focus-bar{background:#172554;border-color:#3b82f6;color:#bfdbfe}
body.dark #focus-bar .fb-hint{color:#93c5fd}
body.dark #focus-bar button{background:#0f172a;border-color:#1e40af;color:#93c5fd}
body.dark #hidden-bar{background:#450a0a;border-color:#7f1d1d;color:#fca5a5}
body.dark #canvas-empty{color:#94a3b8}
body.dark .detail-name{color:#e2e8f0;border-color:#e2e8f0}
body.dark .word-mark{background:#78350f;color:#fde68a}
body.dark .col-entry,body.dark .assoc-entry,body.dark .idx-entry,body.dark .msel-chip,body.dark .note-entry{background:#1e293b}
body.dark .note-entry{border-left-color:#2dd4bf}
body.dark .assoc-notes .note-entry{background:transparent}
body.dark .note-title{color:#5eead4}
body.dark .note-text{color:#cbd5e1}
body.dark .note-links a{color:#2dd4bf}
body.dark #legend-notes{border-color:#1e293b}
body.dark .note-hit{background:#134e4a;color:#5eead4}
body.dark .table-item.note-banner label{color:#5eead4}
body.dark .idx-name{color:#e2e8f0}
body.dark .col-cn,body.dark .aname,body.dark .msel-cn{color:#e2e8f0}
body.dark .empty-state{color:#94a3b8}
body.dark .er-node .n-bg{fill:#1e293b;stroke:#334155}
body.dark .er-node .n-alt{fill:#263447}
body.dark .er-node .n-cn{fill:#e2e8f0}
body.dark .er-node .n-ct{fill:#aab6c7}
body.dark .er-node .n-colhit{fill:#713f12}
body.dark .er-node.word-hit .n-bg{stroke:#fbbf24}
body.dark .er-node .n-wordhit{fill:#92400e}
/* headers had no dark override at all, so they silently inherited the
   light-mode fill (#1e293b) — identical to the dark-mode body color right
   above, making every table's title bar invisible against its own body.
   Light mode gets its contrast from a dark header on a near-white body
   (max lightness gap); mirroring that in dark mode means a header
   noticeably *lighter* than the body, not just one shade darker-of-dark —
   auto-expanded (dashed, de-emphasized) nodes get the dimmer, closer-to-
   body tone instead, matching their already-subdued border treatment */
body.dark .er-node .n-hdr{fill:#475569}
body.dark .er-node.auto .n-hdr{fill:#334155}
body.dark .er-node.ring-1 .n-hdr{fill:#2952cc}
body.dark .er-node.ring-2 .n-hdr{fill:#4a5f8f}
body.dark .er-node.ring-3 .n-hdr{fill:#525f78}
/* the theme-independent .sel/.center rule (#1d4ed8) has lower specificity
   than body.dark .er-node .n-hdr above (extra "body" type selector), so
   without this it silently lost to the plain dark-mode default and a
   selected/focused node's header looked identical to every other node's */
body.dark .er-node.sel .n-hdr,body.dark .er-node.center .n-hdr{fill:#3b82f6}
body.dark .er-edge .e-lbg{fill:#0b1220}
body.dark .divider:hover,body.dark .divider.dragging{background:#1d4ed8}

/* ── print ── */
@media print{
  #topbar,#left-pane,#right-pane,#diagram-toolbar,.divider,#legend,
  #focus-bar,.expand-tab,#toast{display:none!important}
  #center-pane{box-shadow:none!important;background:#fff!important;background-image:none!important}
  body{background:#fff}
}
</style>
</head>
<body>
<div id="topbar">
  <h1>__TITLE__</h1>
  <span class="sep"></span>
  <select id="view-sel" title="Load a saved view"><option value="">Views...</option></select>
  <button id="view-save" class="tb-btn" title="Save the current view (checks, bans, positions, expansion settings) under a name">💾 Save</button>
  <button id="view-del" class="tb-btn" title="Delete the selected view">🗑</button>
  <button id="view-share" class="tb-btn" title="Copy a share link with the current view embedded (opening it reproduces this view)">🔗</button>
  <span id="info-bar"></span>
  <label id="ae-label">
    <input type="checkbox" id="auto-expand"> Auto-expand
  </label>
  <div id="depth-ctrl">
    <span>Depth:</span>
    <button class="dep-btn active" data-d="1">1</button>
    <button class="dep-btn" data-d="2">2</button>
    <button class="dep-btn" data-d="3">3</button>
    <button class="dep-btn" data-d="0">∞</button>
  </div>
  <div id="dir-ctrl" title="Also used by each table's ⊕ button, regardless of Auto-expand">
    <span>Direction:</span>
    <button class="dep-btn dir-btn active" data-dir="both" title="Follow relations in both directions">Both</button>
    <button class="dep-btn dir-btn" data-dir="out" title="Follow what this table depends on (parents it references via FK)">Deps</button>
    <button class="dep-btn dir-btn" data-dir="in" title="Follow what depends on this table (children referencing it via FK)">Dependents</button>
  </div>
</div>

<div id="main">
  <div id="left-pane">
    <div class="pane-title"><span>Tables</span><button class="collapse-btn" id="collapse-left" title="Collapse left pane">◀</button></div>
    <div id="left-controls">
      <button id="btn-all">All</button>
      <button id="btn-none">None</button>
      <button id="btn-unfocus">Exit focus</button>
    </div>
    <div id="search-box">
      <input type="text" id="search" placeholder="Search tables / columns…">
      <button class="srch-tgl" id="fs-case" title="Match case" aria-pressed="false">Aa</button>
      <button class="srch-tgl" id="fs-regex" title="Use regular expression" aria-pressed="false">.*</button>
    </div>
    <div id="hidden-bar"><span id="hidden-count"></span><a id="hidden-clear">Unban all</a></div>
    <div id="table-list"></div>
  </div>

  <div id="div-l" class="divider"></div>

  <div id="center-pane">
    <div id="focus-bar">
      <span id="focus-bar-label"></span>
      <span class="fb-hint">Checkboxes control the overview only (they do not affect this view)</span>
      <span class="fb-sep"></span>
      <button id="focus-bar-apply" title="Re-check exactly the tables shown here so the overview matches this view">Apply to checks</button>
      <button id="focus-bar-close" title="Exit focus and return to the overview (Esc)">✕ Back to overview</button>
    </div>
    <svg id="er-svg" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <marker id="m-one" markerWidth="14" markerHeight="12" refX="11" refY="6" orient="auto-start-reverse" markerUnits="userSpaceOnUse"><path d="M7 1.5 L7 10.5" stroke="#64748b" stroke-width="1.4" fill="none"/></marker>
        <marker id="m-many" markerWidth="14" markerHeight="12" refX="12" refY="6" orient="auto-start-reverse" markerUnits="userSpaceOnUse"><path d="M2 6 L12 1.5 M2 6 L12 10.5 M2 6 L12 6" stroke="#64748b" stroke-width="1.4" fill="none"/></marker>
        <marker id="m-one-hl" markerWidth="14" markerHeight="12" refX="11" refY="6" orient="auto-start-reverse" markerUnits="userSpaceOnUse"><path d="M7 1.5 L7 10.5" stroke="#2563eb" stroke-width="1.8" fill="none"/></marker>
        <marker id="m-many-hl" markerWidth="14" markerHeight="12" refX="12" refY="6" orient="auto-start-reverse" markerUnits="userSpaceOnUse"><path d="M2 6 L12 1.5 M2 6 L12 10.5 M2 6 L12 6" stroke="#2563eb" stroke-width="1.8" fill="none"/></marker>
      </defs>
      <g id="er-main"></g>
    </svg>
    <div id="legend">
      <div id="legend-head"><span>Legend / Controls</span><button id="legend-toggle" title="Collapse/expand the legend">▾</button></div>
      <div id="legend-body">
        <div class="lr"><svg class="lsvg" width="30" height="12" viewBox="0 0 30 12"><path d="M2 6 H20 M7 2 V10 M28 6 L20 2 M28 6 L20 10 M28 6 L20 6" stroke="#64748b" stroke-width="1.2" fill="none"/></svg>one to many</div>
        <div class="lr"><svg class="lsvg" width="30" height="12" viewBox="0 0 30 12"><path d="M2 6 H28 M7 2 V10 M23 2 V10" stroke="#64748b" stroke-width="1.2" fill="none"/></svg>one to one</div>
        <div class="lr"><svg class="lsvg" width="30" height="12" viewBox="0 0 30 12"><path d="M10 6 H20" stroke="#64748b" stroke-width="1.2" stroke-dasharray="3 2" fill="none"/><path d="M2 6 L10 2 M2 6 L10 10 M2 6 L10 6 M28 6 L20 2 M28 6 L20 10 M28 6 L20 6" stroke="#64748b" stroke-width="1.2" fill="none"/></svg>many to many (via join table)</div>
        <div class="lr" style="color:#94a3b8">⇢name … join-table label (toggle with Labels)</div>
        <div class="lr" style="color:#94a3b8">✓ = expansion root (checked)　dashed frame = shown by auto-expand</div>
        <div class="lr" style="color:#94a3b8">faint dotted = relation inferred from FK column name</div>
        <div class="lhint">Framework association names (has_many etc.) appear in the right pane<br>
          Diagram: click = select, shift/ctrl-click = multi-select, double-click = focus, drag = move (whole selection if multi-selected)<br>
          shift-drag on empty canvas = rubber-band select<br>
          2+ selected: align/distribute buttons appear in the right pane<br>
          List: click = locate in diagram, double-click = focus<br>
          Toolbar "Highlight" box: marks matches everywhere (doesn't filter), survives exports.<br>
          Enter = next match, Shift+Enter = previous match<br>
          Esc: exit focus</div>
        <div id="legend-notes" style="pointer-events:auto;display:none"></div>
      </div>
    </div>
    <button id="expand-left" class="expand-tab" title="Open left pane">▶</button>
    <button id="expand-right" class="expand-tab" title="Open right pane">◀</button>
    <div id="canvas-empty">No tables are displayed<br>
      Check tables in the list on the left, or press "All"</div>
    <div id="diagram-toolbar">
      <div id="word-search-box" title="Highlight matching tables/columns in the diagram (does not filter). Enter = next match, Shift+Enter = previous">
        <input type="text" id="word-search" placeholder="Highlight…">
        <span id="word-search-count"></span>
        <button class="srch-tgl" id="ws-case" title="Match case" aria-pressed="false">Aa</button>
        <button class="srch-tgl" id="ws-regex" title="Use regular expression" aria-pressed="false">.*</button>
        <button id="word-search-clear" title="Clear highlight" aria-label="Clear highlight">✕</button>
      </div>
      <div class="tb-group">
        <select class="diag-btn" id="max-rows" title="Max column rows per table (scroll with the wheel for the rest)">
          <option value="5">5 rows</option>
          <option value="10">10 rows</option>
          <option value="15">15 rows</option>
          <option value="20">20 rows</option>
          <option value="30">30 rows</option>
          <option value="9999">All rows</option>
        </select>
        <div id="colmode-group" title="Column display (all tables)">
          <button class="diag-btn" data-cm="0">All</button>
          <button class="diag-btn" data-cm="1">PK/FK</button>
          <button class="diag-btn" data-cm="2">Name</button>
        </div>
        <div id="namemode-group" title="Table name display (physical vs. logical/comment name)">
          <button class="diag-btn" data-nm="0">Both</button>
          <button class="diag-btn" data-nm="1">Physical</button>
          <button class="diag-btn" data-nm="2">Logical</button>
        </div>
        <button class="diag-btn" id="btn-labels" title="Show/hide join-table labels (⇢) in this view — exports have their own toggle in the Export menu">Labels</button>
        <button class="diag-btn" id="btn-groups" title="Show/hide group frames around related tables">Groups</button>
      </div>
      <div class="tb-sep"></div>
      <div class="tb-group">
        <button class="diag-btn" id="btn-undo" title="Undo layout change (Ctrl/Cmd+Z)" disabled>↶</button>
        <button class="diag-btn" id="btn-redo" title="Redo layout change (Ctrl/Cmd+Shift+Z)" disabled>↷</button>
        <button class="diag-btn" id="btn-reset" title="Re-layout now (repack to fill the screen)">↺</button>
        <button class="diag-btn" id="btn-autolayout" title="Auto-tidy mode: re-layout and fit whenever the displayed tables change">Auto-tidy</button>
      </div>
      <div class="tb-sep"></div>
      <div class="tb-group">
        <button class="diag-btn" id="btn-zoom-in" title="Zoom in">+</button>
        <button class="diag-btn" id="btn-zoom-out" title="Zoom out">−</button>
        <button class="diag-btn" id="btn-zoom-100" title="Zoom to 100% (text at natural size)">1:1</button>
        <button class="diag-btn" id="btn-fit" title="Fit all">⊡</button>
      </div>
      <div class="tb-sep"></div>
      <div class="tb-group" id="export-group">
        <button class="diag-btn" id="btn-export-toggle" title="Export the diagram" aria-haspopup="true">⬇ Export</button>
        <div id="export-menu" class="tb-popup">
          <div class="tb-popup-caption">Image options</div>
          <label class="tb-popup-check"><input type="checkbox" id="export-opt-labels" checked> Join-table labels (⇢)</label>
          <label class="tb-popup-check"><input type="checkbox" id="export-opt-roots"> ✓ root badges</label>
          <div class="tb-export-row" style="grid-template-columns:60px 1fr" title="Independent of the live view's own Both/Physical/Logical toggle in the toolbar">
            <span class="tb-export-fmt">Names</span>
            <div id="export-namemode-group" class="seg-group">
              <button class="diag-btn" data-xnm="0">Both</button>
              <button class="diag-btn" data-xnm="1">Phys.</button>
              <button class="diag-btn" data-xnm="2">Log.</button>
            </div>
          </div>
          <div class="tb-popup-sep"></div>
          <div class="tb-popup-caption">Export — copy or download</div>
          <div class="tb-export-row">
            <span class="tb-export-fmt">PNG</span>
            <button class="diag-btn" id="btn-export" title="Falls back to a file download if the browser can't write images to the clipboard">Copy</button>
            <button class="diag-btn" id="btn-export-download">Download</button>
          </div>
          <div class="tb-export-row">
            <span class="tb-export-fmt">SVG</span>
            <button class="diag-btn" id="btn-export-svg-copy">Copy</button>
            <button class="diag-btn" id="btn-export-svg">Download</button>
          </div>
          <div class="tb-export-row">
            <span class="tb-export-fmt">Mermaid</span>
            <button class="diag-btn" id="btn-export-mmd" title="Covers displayed tables; paste into READMEs/PRs">Copy</button>
            <button class="diag-btn" id="btn-export-mmd-download">Download</button>
          </div>
          <div class="tb-export-row">
            <span class="tb-export-fmt">PlantUML</span>
            <button class="diag-btn" id="btn-export-puml" title="Covers displayed tables; paste into PlantUML renderers">Copy</button>
            <button class="diag-btn" id="btn-export-puml-download">Download</button>
          </div>
        </div>
      </div>
      <div class="tb-sep"></div>
      <button class="diag-btn" id="btn-dark" title="Toggle dark mode (exports always use the light palette)">🌙</button>
      <div class="tb-group" id="help-group">
        <button class="diag-btn" id="btn-help" title="Shortcuts &amp; help" aria-haspopup="true">?</button>
        <div id="help-menu" class="tb-popup">
          <div class="tb-popup-caption">Mouse</div>
          <div class="help-row"><span>Pan / zoom</span><span class="help-k">drag bg / Ctrl+wheel</span></div>
          <div class="help-row"><span>Focus a table (again to exit)</span><span class="help-k">double-click</span></div>
          <div class="help-row"><span>Move (hold Alt: no snap)</span><span class="help-k">drag table</span></div>
          <div class="help-row"><span>Multi-select</span><span class="help-k">Shift+click / Shift+drag</span></div>
          <div class="help-row"><span>Move a whole group</span><span class="help-k">drag its title</span></div>
          <div class="tb-popup-sep"></div>
          <div class="tb-popup-caption">Keyboard</div>
          <div class="help-row"><span>Close menu / clear search / exit focus / deselect</span><span class="help-k">Esc</span></div>
          <div class="help-row"><span>Undo / redo layout</span><span class="help-k">Ctrl/Cmd+Z / +Shift+Z</span></div>
          <div class="help-row"><span>Next / prev search hit</span><span class="help-k">Enter / Shift+Enter</span></div>
          <div class="tb-popup-sep"></div>
          <a class="help-link" href="https://orapli.github.io/erdscope/manual.html" target="_blank" rel="noopener">Full manual ↗</a>
        </div>
      </div>
    </div>
  </div>

  <div id="div-r" class="divider"></div>

  <div id="right-pane">
    <div class="pane-title"><button class="collapse-btn" id="collapse-right" title="Collapse right pane">▶</button><span>Details</span></div>
    <div id="table-details"><div class="empty-state">Click a table<br>to see its details</div></div>
  </div>
</div>

<div id="toast"></div>

<script>
'use strict';
const DATA = __DATA_JSON__;
// notes Phase 1: viewer-ready sidecar, already validated/resolved server-side
// (resolve_and_validate_notes in providers.py) — DATA.notes is absent when no
// notes were configured (demo byte-equality), so default to [] here.
const NOTES = DATA.notes || [];
// groups Phase 1: viewer-ready sidecar, already validated/resolved server-side
// (resolve_and_validate_groups in providers.py) — DATA.groups is absent when
// no groups were configured (demo byte-equality), so default to [] here.
const GROUPS = DATA.groups || [];
// LocalStorage keys are namespaced per project so multiple ERD pages
// served from the same origin don't share table selections
const LS = k => `erd:${document.title}:${k}`;
// localStorage.setItem throws in Safari private browsing and when the quota
// is exceeded — uncaught, that aborts whatever click/drag handler called it
// partway through, silently. Persisting view state is a nice-to-have, not
// something worth breaking the interaction over, so swallow the failure
// (once per session, so a full quota doesn't spam the console on every move).
let lsWriteFailed = false;
function setLS(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch (e) {
    if (!lsWriteFailed) {
      lsWriteFailed = true;
      console.warn('localStorage write failed (private browsing or quota exceeded) '
        + '— view state (selection, layout, panel sizes, ...) will not persist across reloads', e);
    }
  }
}

// ── SVG helper ──────────────────────────────────────────────────────────
const NS = 'http://www.w3.org/2000/svg';
function svgEl(tag, a={}) {
  const el = document.createElementNS(NS, tag);
  for (const [k,v] of Object.entries(a)) el.setAttribute(k, v);
  return el;
}

// ── State ────────────────────────────────────────────────────────────────
let excludedTables = new Set(); // unchecked in list = not shown in overview
let hiddenTables   = new Set(); // fully banned: never shown, even by auto-expand
let focusedTable   = null;
let selectedTables = new Set(); // multi-select for align/distribute; single click = size 1
let selectionAnchor = null;     // last explicit selection target — drives the single-table detail view
let autoExpand     = false;
let expandDepth    = 1;  // 1|2|3|0(=unlimited)
let expandDir      = 'both'; // 'both' | 'out' (depends on) | 'in' (depended by)
let manualExpanded = new Set(); // tables added with ⊕ while focused (not persisted)
// overview ⊕: tables it checks so they're shown, but that shouldn't themselves
// become fresh auto-expand roots (else one ⊕ click cascades past 1 hop
// whenever auto-expand is on, because the newly-checked tables immediately
// expand again on the next getDisplayTables() pass). Not persisted.
let noAutoExpandRoot = new Set();
let autoLayout     = false; // re-layout automatically whenever the display set changes
let colHighlight   = null;  // {table, match} — column-search hit to highlight in the node
// left-pane #search's own regex/case toggles — independent of the toolbar
// Highlight box's wordRegexMode/wordCaseSensitive below (see makeMatcher's
// declaration comment for why the two boxes don't share one mode)
let filterRegexMode = false;
let filterCaseSensitive = false;

// Shared substring/regex matcher used by both search boxes (left-pane
// filter and toolbar Highlight) — centralized after a bug where column
// *comments* were matched at some call sites but not others, because the
// same "toLowerCase().includes()" logic was independently duplicated in
// ~12 places. Every match site now goes through one of two matcher
// instances (wordMatcher for the toolbar box, filterMatcher for the
// left-pane box) instead of reimplementing string matching itself.
//
// makeMatcher(query, {regex, cs}): query='' -> null (callers treat null
// as "no query, matches nothing"). Otherwise:
//   .test(s)   - boolean, the common case
//   .ranges(s) - [[start,end],...] of every match, for escMark()
//   .error     - regex SyntaxError message, or null (drives the red
//                "invalid pattern" state instead of silently matching
//                nothing with no explanation, or silently falling back
//                to substring mode, which would make the same query mean
//                two different things as the user keeps typing)
function makeMatcher(query, {regex=false, cs=false}={}){
  if(!query) return null;
  if(regex){
    let re, reG;
    try{
      re=new RegExp(query, cs?'':'i');    // no 'g' — .test() must stay stateless
      reG=new RegExp(query, cs?'g':'gi'); // 'g' copy used only by matchAll below
    }catch(e){
      return {test:()=>false, ranges:()=>[], error:e.message};
    }
    return {
      test:s=>re.test(String(s)),
      ranges:s=>[...String(s).matchAll(reG)].map(m=>[m.index,m.index+m[0].length]).filter(([a,b])=>b>a),
      error:null,
    };
  }
  const q=cs?query:query.toLowerCase();
  return {
    test:s=>(cs?String(s):String(s).toLowerCase()).includes(q),
    ranges:s=>{
      const str=cs?String(s):String(s).toLowerCase();
      const out=[]; let i=0, idx;
      while((idx=str.indexOf(q,i))>=0){ out.push([idx,idx+q.length]); i=idx+q.length; }
      return out;
    },
    error:null,
  };
}

// toolbar "Highlight" search — separate from #search (which filters the
// left-pane list). Never filters anything; marks matches everywhere
// instead. wordQuery is the raw input value (empty = off, and still used
// for cheap truthy checks); wordMatcher is the compiled matcher derived
// from it plus the regex/case toggles, rebuilt whenever either changes.
// Read on demand by drawNode/renderTableList/showDetails rather than
// pushed into per-table state, so it survives every existing re-render
// path for free.
let wordQuery = '';
let wordMatcher = null;
let wordRegexMode = false;
let wordCaseSensitive = false;
let wordMatchIdx = -1; // rotating cursor for Enter-to-cycle
let wordColHitCache = new Map(); // table -> last-seen wordColHits() signature
function wordColHits(name){
  if(!wordMatcher) return [];
  return (DATA.tables[name]?.columns||[])
    .filter(c=>wordMatcher.test(c.name) || wordMatcher.test(c.comment||''))
    .map(c=>c.name);
}
function wordHit(name){
  if(!wordMatcher) return false;
  return wordMatcher.test(name) || wordColHits(name).length>0
    || wordMatcher.test(DATA.tables[name]?.comment||'')
    // table/relation notes attached to this table count toward Highlight too
    // (a global note has no owning table/node, so it's out of scope here —
    // it's only reachable via the left-pane filter's banner row).
    || notesForTable(name).some(n=>wordMatcher.test(noteText(n)));
}
let colMode        = 0;  // 0=all  1=PK/FK  2=header
let colOverride    = {}; // per-table column-mode override (name -> 0|1|2)
let showEdgeLabels = true;
// groups Phase 1: whether the group-layer frames are drawn at all. Toggle is
// hidden entirely when GROUPS is empty (§ toolbar init below), so this only
// ever matters for a config that actually declared groups.
let showGroups = true;
// export-only options (independent of the live view — the live Labels
// toggle above controls what YOU see while working; these control what
// goes into a PNG/SVG someone else will look at later)
let exportOptLabels = true;
let exportOptRoots  = false;

const nodePos  = {};  // active positions
const basePos  = {};  // saved full-view positions
const nodeSize = {};

// ring depth map (set during hub-spoke layout for CSS tinting)
const ringDepth = {}; // tableName -> depth (0=center, 1=ring1, ...)

let vx=0, vy=0, vs=1;
let isPanning=false, panSX, panSY, panVX, panVY;
let isMarqueeSelecting=false, marqueeStart=null; // world coords; shift-drag on empty canvas
let marqueeStartCX=0, marqueeStartCY=0; // client coords, for the same-units-as-dragMoved 3px threshold
let marqueeJustSelected=false; // suppresses the "click on empty canvas clears selection"
                                // handler for the click event that follows the marquee's
                                // own mouseup — same trick dragMoved uses for node drags
let isDragging=false, dragName, dragOX, dragOY, dragMoved=false, dragCX=0, dragCY=0;
let dragSet=new Set(), dragGroupStart={}; // nodes moving together in the current drag
// groups Phase 1: true while the current drag was started from a group's
// title chip (dragging the whole group by its label) rather than a node —
// suppresses node-snap (a group move has no single "anchor" node to snap)
// and skips per-node selection semantics. dragSet/dragGroupStart/dragName/
// dragUndoSnapshot/dragEdgeCache/isDragging are otherwise fully shared with
// the ordinary multi-node drag machinery above.
let dragIsGroup=false;
let dragUndoSnapshot=null; // nodePos captured at mousedown, committed to undoStack only if the drag actually moved something
// the display set/edge list can't change mid-drag (only checkbox/auto-expand
// changes do that), so it's computed once at mousedown instead of on every
// mousemove — recomputing getDisplayTables() (which can BFS auto-expand
// roots) and getDisplayEdges() per mouse event is the main cost of dragging
// on a large diagram
let dragEdgeCache=null;

// Layout undo/redo: position-only history (not selection/checkbox state —
// changing which tables are displayed is treated as expected, not something
// to undo). Covers drag, align/distribute, and explicit relayouts (↺, and
// colmode/max-rows changes while Auto-tidy is on) — the "I moved things
// carefully, then a misclick wiped it" scenarios.
let undoStack=[], redoStack=[];
const UNDO_LIMIT=30;

const svg    = document.getElementById('er-svg');
const erMain = document.getElementById('er-main');

// ── LocalStorage ──────────────────────────────────────────────────────────
function saveState() {
  setLS(LS('excl'), JSON.stringify([...excludedTables]));
  setLS(LS('hid'),  JSON.stringify([...hiddenTables]));
  setLS(LS('ae'),   String(autoExpand));
  setLS(LS('dep'),  String(expandDepth));
  setLS(LS('cm'),   String(colMode));
  setLS(LS('cov'),  JSON.stringify(colOverride));
  setLS(LS('lbl'),  String(showEdgeLabels));
  setLS(LS('grp'),  String(showGroups));
  setLS(LS('dir'),  expandDir);
  setLS(LS('al'),   String(autoLayout));
  setLS(LS('xlbl'),  String(exportOptLabels));
  setLS(LS('xroot'), String(exportOptRoots));
  setLS(LS('wre'),  String(wordRegexMode));
  setLS(LS('wcs'),  String(wordCaseSensitive));
  setLS(LS('fre'),  String(filterRegexMode));
  setLS(LS('fcs'),  String(filterCaseSensitive));
  setLS(LS('nm'),   String(nameMode));
  setLS(LS('xnm'),  String(exportNameMode));
}
function loadState() {
  try { excludedTables = new Set(JSON.parse(localStorage.getItem(LS('excl')) || '[]')); } catch{}
  try { hiddenTables   = new Set(JSON.parse(localStorage.getItem(LS('hid'))  || '[]')); } catch{}
  try { colOverride    = JSON.parse(localStorage.getItem(LS('cov')) || '{}') || {}; } catch{}
  autoExpand  = localStorage.getItem(LS('ae'))  === 'true';
  expandDepth = parseInt(localStorage.getItem(LS('dep')) || '1', 10);
  colMode     = parseInt(localStorage.getItem(LS('cm'))  || '0', 10);
  showEdgeLabels = localStorage.getItem(LS('lbl')) !== 'false';
  showGroups = localStorage.getItem(LS('grp')) !== 'false';
  const mr = parseInt(localStorage.getItem(LS('mr')), 10);
  if (mr > 0) maxRows = mr; // user's choice overrides the CLI default
  expandDir = localStorage.getItem(LS('dir')) || 'both';
  autoLayout = localStorage.getItem(LS('al')) === 'true';
  exportOptLabels = localStorage.getItem(LS('xlbl'))  !== 'false';
  exportOptRoots  = localStorage.getItem(LS('xroot')) === 'true';
  wordRegexMode      = localStorage.getItem(LS('wre')) === 'true';
  wordCaseSensitive  = localStorage.getItem(LS('wcs')) === 'true';
  filterRegexMode     = localStorage.getItem(LS('fre')) === 'true';
  filterCaseSensitive = localStorage.getItem(LS('fcs')) === 'true';
  nameMode       = parseInt(localStorage.getItem(LS('nm'))  || '0', 10);
  exportNameMode = parseInt(localStorage.getItem(LS('xnm')) || '0', 10);
  // guard against corrupted stored values — they fail silently otherwise
  if (![0,1,2,3].includes(expandDepth)) expandDepth = 1;
  if (![0,1,2].includes(colMode)) colMode = 0;
  if (![0,1,2].includes(nameMode)) nameMode = 0;
  if (![0,1,2].includes(exportNameMode)) exportNameMode = 0;
  if (!['both','out','in'].includes(expandDir)) expandDir = 'both';
  for (const [k,v] of Object.entries(colOverride)) if (![0,1,2].includes(v)) delete colOverride[k];
}

// ── Table helpers ──────────────────────────────────────────────────────────
function allTables() { return Object.keys(DATA.tables).sort(); }

// One expansion step from `name`, honoring the dependency direction:
// 'out' = tables this one depends on (it holds the FK / belongs_to them),
// 'in'  = tables that depend on this one, 'both' = either.
// through/habtm are mutual and follow in every mode. Polymorphic is skipped.
function stepNeighbors(name, dir) {
  const out = new Set();
  (DATA.tables[name]?.associations || []).forEach(a => {
    if (a.polymorphic || !DATA.tables[a.target]) return;
    const mutual = !!a.through || a.type === 'has_and_belongs_to_many';
    const dep = a.type === 'belongs_to'; // name depends on target
    if (mutual || dir === 'both' || (dir === 'out' && dep) || (dir === 'in' && !dep)) out.add(a.target);
  });
  for (const [n, t] of Object.entries(DATA.tables)) {
    if (n === name) continue;
    for (const a of t.associations) {
      if (a.target !== name || a.polymorphic) continue;
      const mutual = !!a.through || a.type === 'has_and_belongs_to_many';
      const nDep = a.type === 'belongs_to'; // n depends on name
      if (mutual || dir === 'both' || (dir === 'out' && !nDep) || (dir === 'in' && nDep)) { out.add(n); break; }
    }
  }
  out.delete(name);
  return out;
}

// BFS up to `depth` hops (depth=0 means unlimited), following expandDir.
// Fully-hidden tables are never visited, so they don't act as bridges either.
function getRelated(rootName, depth) {
  const maxD   = depth === 0 ? 9999 : depth;
  const visited = new Map([[rootName, 0]]); // name -> depth
  let frontier  = [rootName];

  for (let d = 1; d <= maxD && frontier.length > 0; d++) {
    const next = [];
    for (const name of frontier) {
      stepNeighbors(name, expandDir).forEach(t => {
        if (!visited.has(t) && !hiddenTables.has(t)) { visited.set(t, d); next.push(t); }
      });
    }
    frontier = next;
  }
  return visited; // Map<name, depth>
}

// Overview: checkbox selection minus hidden. With auto-expand on, every
// checked table becomes a BFS root and its neighbors are pulled in too.
// Focus: always shows the table + relations up to expandDepth —
// that is the point of focusing, so it does not depend on the auto-expand toggle.
// Checkboxes never limit expansion (uncheck ≠ ban; use 🚫 to ban).
function getDisplayTables() {
  if (!focusedTable) {
    const base = allTables().filter(t => !hiddenTables.has(t) && !excludedTables.has(t));
    if (!autoExpand || base.length === 0) return base;
    const seen = new Set(base);
    const total = allTables().filter(t => !hiddenTables.has(t)).length;
    for (const root of base) {
      if (seen.size >= total) break;
      if (noAutoExpandRoot.has(root)) continue; // ⊕-added: shown, but not a fresh root
      getRelated(root, expandDepth).forEach((d, t) => { if (!hiddenTables.has(t)) seen.add(t); });
    }
    return allTables().filter(t => seen.has(t));
  }
  const rel = getRelated(focusedTable, expandDepth);
  return allTables().filter(t => (rel.has(t) || manualExpanded.has(t)) && !hiddenTables.has(t));
}

// Add tables to the current view: while focused they join manualExpanded
// (transient deep-dive); in the overview their checkboxes get checked, but
// marked noAutoExpandRoot so a single ⊕ click can't cascade past 1 hop when
// auto-expand is on (see the flag's declaration for why).
function addTables(names, label){
  const cur=new Set(getDisplayTables());
  const add=[...names].filter(t=>!cur.has(t)&&!hiddenTables.has(t)&&DATA.tables[t]);
  if(!add.length){ showToast('No related tables to add'); return; }
  if(focusedTable){
    add.forEach(t=>manualExpanded.add(t));
    switchFocusTable(); // re-run hub-spoke including the new tables
  } else {
    add.forEach(t=>{ excludedTables.delete(t); noAutoExpandRoot.add(t); });
    saveState();
  }
  refreshView(); renderTableList();
  showToast(`Added ${add.length} table(s)${label?` related to ${label}`:''}`);
}

function getDisplayEdges(tables) {
  const tset = new Set(tables);
  const map  = new Map();
  tables.forEach(name => {
    (DATA.tables[name]?.associations || []).forEach(a => {
      if (a.polymorphic || !tset.has(a.target)) return;
      // when the join table itself is on screen, the A–join–B chain
      // represents the relation — drop the direct many-to-many edge
      if (a.through && tset.has(a.through)) return;
      if (a.type === 'has_and_belongs_to_many' && tset.has([name, a.target].sort().join('_'))) return;
      const key = [name, a.target].sort().join('\x00');
      if (!map.has(key)) map.set(key, {source:name, target:a.target, assocs:[]});
      map.get(key).assocs.push({from:name, ...a});
    });
  });
  return [...map.values()];
}

// ── Column mode ────────────────────────────────────────────────────────────
const COL_LABELS = ['All', 'PK/FK', 'Name'];

function effColMode(name) { return colOverride[name] ?? colMode; }
// a column counts as FK only when a real association actually backs it
// (declared, DB constraint, or --infer-fk) — fk_columns is computed
// server-side in _finish() from those associations, not guessed from the
// column name here, so the badge can't claim a relation that isn't real
function isFkCol(table, colName) {
  return (DATA.tables[table]?.fk_columns || []).includes(colName);
}

function visibleCols(name) {
  const all = DATA.tables[name]?.columns || [];
  const m = effColMode(name);
  if (m === 0) return all;
  if (m === 1) return all.filter(c => c.primary || isFkCol(name, c.name));
  return [];
}

// ── Node size ──────────────────────────────────────────────────────────────
const HDR_H=30, ROW_H=20, MIN_W=160, PAD=16;
let maxRows = __MAX_ROWS__; // CLI --max-rows default; adjustable in the toolbar
const colScroll = {}; // name -> first visible column index (for tall tables)

// CJK glyphs render roughly 2x as wide as Latin ones in the UI's monospace
// font — treat any codepoint outside Latin-1 as 2 width units, everything
// else as 1, so mixed-script strings (a Japanese logical name next to an
// English physical one) size and truncate correctly instead of assuming
// 1 char == 1 unit like the rest of this file's plain name.length math did
// before logical names existed.
function displayWidthUnits(s){
  let w=0;
  for(const ch of s) w += /[^\x00-\xff]/.test(ch) ? 2 : 1;
  return w;
}

// Table comments are free text (often a long paragraph, not a short
// business name) — cap to a readable prefix: first line only, then a
// fixed *display-width* budget so CJK comments don't balloon whatever
// they're rendered into. Shared by the node header, the left-pane list
// row, and PlantUML's entity alias (see exportToPlantUML).
const LOGICAL_NAME_CAP = 16;
function logicalName(name){
  const c=(DATA.tables[name]?.comment||'').split('\n')[0].trim();
  if(!c) return '';
  let units=0, out='';
  for(const ch of c){
    const w=/[^\x00-\xff]/.test(ch)?2:1;
    if(units+w>LOGICAL_NAME_CAP) return out+'…';
    units+=w; out+=ch;
  }
  return out;
}

// Which of physical/logical name(s) the header shows — a *display*
// setting, not a data one: search/matching always considers both
// regardless of this mode (see wordHit/renderTableList's filter, which
// read DATA.tables[name].comment directly, never headerDisplayString).
// 0 = both (default, physical（logical）) · 1 = physical only ·
// 2 = logical only (falls back to physical when a table has no comment —
// there's nothing else to show). Persisted like colMode/showEdgeLabels.
let nameMode = 0;
// Export gets its own independent copy — same reasoning as
// exportOptLabels/exportOptRoots above: what you're currently looking at
// and what you hand to someone else are different questions, and forcing
// them to match would mean changing your own view just to export a
// specific look.
let exportNameMode = 0;

// The full header string (physical + 「logical」, when present) used both
// to render the node title and to size the node — kept as one function so
// the two can never drift apart (a header wider than the box it sized).
// Reflects the *live* nameMode (export's own mode only affects which
// pre-rendered tspan CSS hides — see buildExportSvg — not node sizing).
function headerDisplayString(name){
  const lg=logicalName(name);
  if(!lg || nameMode===1) return name;
  if(nameMode===2) return lg;
  return `${name}（${lg}）`;
}

function calcSize(name) {
  const cols    = visibleCols(name);
  const allCnt  = (DATA.tables[name]?.columns || []).length;
  // header icon buttons (⊖/⊕/▤) live in a ~56px-wide zone on the right of
  // the header, unrelated to the centered title text's own width — a
  // header showing only the physical name was rarely wide enough to reach
  // them, but a header that's grown with a logical name commonly is, so
  // reserve headroom for the icon cluster whenever the *displayed* header
  // (not just the physical name) pushes the header wider
  const nameW   = displayWidthUnits(headerDisplayString(name)) * 8.5 + PAD
    + (headerDisplayString(name)!==name ? 56 : 0);
  const colW    = cols.map(c => (c.name.length + c.type.length + 2) * 7.2 + 52);
  const shown   = Math.min(cols.length, maxRows);
  const noSchema = allCnt === 0 && !!DATA.tables[name]?.schema_missing;
  const footer  = (cols.length > maxRows || (effColMode(name) > 0 && allCnt > cols.length) || noSchema) ? 1 : 0;
  // scrollable nodes get extra width so the scrollbar doesn't touch the type column
  const w = Math.max(MIN_W, nameW, ...colW, 0) + (cols.length > maxRows ? 10 : 0);
  const h = HDR_H + shown * ROW_H + footer * ROW_H + (shown + footer > 0 ? 6 : 4);
  return {w, h};
}

// ── Layout ──────────────────────────────────────────────────────────────────
// Shelf packing: rows of real-size nodes wrapped to the viewport shape.
// Alphabetical order is kept so the eye can scan A→Z; no overlaps.
// Order tables so connected ones sit next to each other in the shelf
// (DFS over the visible subgraph, starting from the alphabetical first).
// keeps chains like account – list_accounts – lists adjacent.
// Overview layout: each connected component is split into rows by BFS depth
// from its highest-degree table (concentric rings, laid out as rows instead
// of circles — a circular ring wastes the four corners of a rectangular
// viewport). Every edge therefore connects the
// same row or an adjacent one — the old width-wrapped rows could put two
// connected tables many rows apart, or split a dense cluster across one row
// with no adjacency, forcing edges into long detour arcs around whatever
// sat between them (see HANDOFF for the concrete example). Components are
// then shelf-packed side by side toward the viewport aspect ratio.
// widest physical row worth laying out: anything wider renders below ~60%
// zoom on a typical viewport. Both the overview's sub-row wrap and the
// incremental group placement break rows up at this width.
const MAX_ROW_W=1700;

function gridLayout(tables, preferredHub) {
  const gapX=40, gapY=60, gap=90;
  const tset=new Set(tables);
  // small views: order rows by neighbor position for short, near-vertical
  // edges; large views: skip it (alphabetical) to stay cheap
  const small=tables.length<=40;

  const adj=new Map(tables.map(t=>[t,new Set()]));
  for(const [n,t] of Object.entries(DATA.tables)){
    if(!tset.has(n)) continue;
    (t.associations||[]).forEach(a=>{
      if(a.polymorphic||a.target===n||!tset.has(a.target)) return;
      adj.get(n).add(a.target); adj.get(a.target).add(n);
    });
  }

  // connected components (alphabetical seed order keeps runs stable); size-1
  // components are isolated tables, laid out separately as plain rows below
  const seen=new Set(), comps=[], singles=[];
  for(const t of tables.slice().sort()){
    if(seen.has(t)) continue;
    const comp=[], stack=[t];
    while(stack.length){
      const c=stack.pop();
      if(seen.has(c)) continue;
      seen.add(c); comp.push(c);
      adj.get(c).forEach(n=>{ if(!seen.has(n)) stack.push(n); });
    }
    if(comp.length>1) comps.push(comp); else singles.push(comp[0]);
  }

  function layoutComponent(comp){
    // focus mode wants the focused table itself at the center, not
    // whichever node happens to have the highest degree within it
    const hub=(preferredHub && comp.includes(preferredHub)) ? preferredHub
      : comp.slice().sort((a,b)=>(adj.get(b).size-adj.get(a).size)||a.localeCompare(b))[0];
    const depMap=new Map([[hub,0]]);
    let frontier=[hub];
    while(frontier.length){
      const next=[];
      frontier.forEach(name=>{
        adj.get(name).forEach(n=>{
          if(!depMap.has(n)){ depMap.set(n, depMap.get(name)+1); next.push(n); }
        });
      });
      frontier=next;
    }
    // feeds the ring-1/2/3 header tint in focus mode (how far a table is
    // from the focused one) — only meaningful for the focused component,
    // not the general overview
    if(hub===preferredHub) depMap.forEach((d,t)=>{ ringDepth[t]=d; });
    const byDepth={};
    depMap.forEach((d,t)=>{ (byDepth[d]=byDepth[d]||[]).push(t); });

    // target row width: a hub with many direct children makes one BFS depth
    // much wider than the rest ("wedge" shape, mostly empty bounding box) —
    // wrap any row wider than this back into multiple physical sub-rows.
    // Target from the component's expected height (depth count × a typical
    // row band) × the viewport aspect, not from total node area — an
    // area-based guess doesn't know a BFS layout already has one mandatory
    // row per depth, so it under-targets width and over-wraps, leaving the
    // component too tall instead of too wide.
    const depths=Object.keys(byDepth).length;
    const avgRowH=comp.reduce((s,t)=>s+(nodeSize[t]||calcSize(t)).h,0)/comp.length;
    const estHeight=depths*(avgRowH+gapY);
    const rowTargetW=Math.max(700, estHeight*viewAspect()*1.15);

    const xc=new Map(); // placed x-centers, feeds the next row's ordering
    const placeRow=(sr, rowY)=>{
      const rw=sr.reduce((s,t)=>s+(nodeSize[t]||calcSize(t)).w+gapX,0)-gapX;
      // shift the row so members sit under their already-placed neighbors
      // (mean offset) instead of centering every row on the component axis:
      // a centered row puts e.g. two grandchildren in the middle while
      // their parents sit at the far end of a wide wrapped depth-1 row,
      // forcing long swooping arcs under the whole component. Symmetric
      // rows (a star's children around the hub) get shift ≈ 0, so the
      // common case stays visually centered.
      let shift=0;
      if(small){
        const deltas=[]; let x=-rw/2;
        sr.forEach(t=>{
          const s=nodeSize[t]||calcSize(t);
          const ns=[...adj.get(t)].filter(n=>xc.has(n));
          if(ns.length) deltas.push(ns.reduce((a,n)=>a+xc.get(n),0)/ns.length-(x+s.w/2));
          x+=s.w+gapX;
        });
        if(deltas.length) shift=deltas.reduce((a,b)=>a+b,0)/deltas.length;
      }
      let x=shift-rw/2;
      sr.forEach(t=>{
        const s=nodeSize[t]||calcSize(t);
        const cx=x+s.w/2;
        nodePos[t]={x:cx, y:rowY};
        xc.set(t,cx);
        x+=s.w+gapX;
      });
    };
    let y=0, hubH=0;
    Object.keys(byDepth).map(Number).sort((a,b)=>a-b).forEach(d=>{
      let row=byDepth[d];
      if(small && d>0){
        // preferred x = mean x of this row's already-placed (row d-1) neighbors
        // — keeps a table under its parent, minimizing zigzag and edge length.
        // Siblings that share a parent all get the same preference (it's the
        // parent's x), so on its own this can't separate a busy hub's many
        // direct children — cluster same-row tables that are *also* directly
        // connected to each other (e.g. two children of the same hub that
        // reference one another) so they land adjacent instead of at
        // opposite ends of the row, which is what forces a long detour arc.
        const rowSet=new Set(row);
        const pref=t=>{
          const ns=[...adj.get(t)].filter(n=>xc.has(n));
          return ns.length ? ns.reduce((s,n)=>s+xc.get(n),0)/ns.length : 0;
        };
        const seenRow=new Set(), clusters=[];
        row.slice().sort((a,b)=>pref(a)-pref(b)).forEach(start=>{
          if(seenRow.has(start)) return;
          const members=new Set(), stack=[start];
          while(stack.length){
            const t=stack.pop();
            if(members.has(t)) continue;
            members.add(t);
            [...adj.get(t)].filter(n=>rowSet.has(n)&&!members.has(n)).forEach(n=>stack.push(n));
          }
          // a same-row "hub" (e.g. two children of the parent that also
          // reference each other) needs to sit *between* its row-siblings,
          // not at one end — plain discovery order only guarantees adjacency
          // on one side. Sort by in-cluster degree and place highest first,
          // then alternate left/right so each subsequent (lower-degree) node
          // lands next to what's already placed.
          const list=[...members];
          const inDeg=t=>[...adj.get(t)].filter(n=>members.has(n)).length;
          list.sort((a,b)=>(inDeg(b)-inDeg(a))||(pref(a)-pref(b)));
          const seq=[list[0]];
          for(let i=1;i<list.length;i++) (i%2) ? seq.push(list[i]) : seq.unshift(list[i]);
          seq.forEach(t=>seenRow.add(t));
          clusters.push(seq);
        });
        clusters.sort((a,b)=>{
          const pa=a.reduce((s,t)=>s+pref(t),0)/a.length;
          const pb=b.reduce((s,t)=>s+pref(t),0)/b.length;
          return pa-pb;
        });
        row=clusters.flat();
      } else {
        row=row.slice().sort();
      }

      // wrap into at most 2 physical sub-rows when this depth is too wide —
      // capped at 2 (not "however many fit at rowTargetW") because a hub
      // with many direct children is still only *one* level deep, and
      // wrapping it into 3+ stacked rows reads as a much taller/deeper tree
      // than the graph actually is. Split by item count (not a width
      // target) so the two halves come out roughly balanced.
      const naturalW=row.reduce((s,t)=>s+(nodeSize[t]||calcSize(t)).w+gapX,0)-gapX;
      // normally at most 2 sub-rows (3+ stacked rows read as a much deeper
      // tree than the graph actually is) — but a really big fan-out (20+
      // direct children) makes each half wider than any viewport can show
      // at readable zoom, so the cap grows just enough to keep each
      // physical sub-row under MAX_ROW_W
      const cap=Math.max(2, Math.ceil(naturalW/MAX_ROW_W));
      const chunks=Math.max(1, Math.min(cap, Math.ceil(naturalW/rowTargetW)));
      const subRows=[];
      if(chunks<=1){
        subRows.push(row);
      } else {
        const per=Math.ceil(row.length/chunks);
        for(let i=0;i<row.length;i+=per) subRows.push(row.slice(i,i+per));
      }

      if(d===1 && subRows.length>1){
        // the row directly under the hub overflowed into sub-rows —
        // alternate them below/above the hub instead of stacking them all
        // below the first. Depth 0 has nothing above it by construction,
        // so that is free space; stacking every sub-row below instead
        // forces every edge from the later sub-rows up to the hub to pass
        // behind/through the first sub-row's nodes to get there.
        let yUp=-(hubH/2+gapY);
        subRows.forEach((sr,i)=>{
          const rh=Math.max(...sr.map(t=>(nodeSize[t]||calcSize(t)).h));
          if(i%2===0){ placeRow(sr, y+rh/2); y+=rh+gapY; }
          else       { placeRow(sr, yUp-rh/2); yUp-=rh+gapY; }
        });
        return;
      }
      subRows.forEach(sr=>{
        const rh=Math.max(...sr.map(t=>(nodeSize[t]||calcSize(t)).h));
        placeRow(sr, y+rh/2);
        if(d===0) hubH=rh;
        y+=rh+gapY;
      });
    });

    let x0=Infinity,y0=Infinity,x1=-Infinity,y1=-Infinity;
    comp.forEach(t=>{
      const p=nodePos[t], s=nodeSize[t]||calcSize(t);
      x0=Math.min(x0,p.x-s.w/2); y0=Math.min(y0,p.y-s.h/2);
      x1=Math.max(x1,p.x+s.w/2); y1=Math.max(y1,p.y+s.h/2);
    });
    return {comp, x0, y0, w:x1-x0, h:y1-y0};
  }

  // shelf-pack component boxes (largest first) toward the viewport shape
  const boxes=comps.map(layoutComponent);
  const area=boxes.reduce((s,b)=>s+(b.w+gap)*(b.h+gap),0);
  const targetW=Math.max(900, Math.sqrt(area*viewAspect()));
  boxes.sort((a,b)=>b.w*b.h-a.w*a.h);
  let cx=0, cy=0, rowH=0;
  boxes.forEach(b=>{
    if(cx>0 && cx+b.w>targetW){ cx=0; cy+=rowH+gap; rowH=0; }
    b.comp.forEach(t=>{
      const p=nodePos[t];
      nodePos[t]={x:cx+(p.x-b.x0), y:cy+(p.y-b.y0)};
    });
    cx+=b.w+gap; rowH=Math.max(rowH,b.h);
  });

  // isolated tables (no relation to anything, or to each other): stacked
  // in a column beside the connected components, top to bottom, rather
  // than appended as more rows underneath everything else — the connected
  // components above already grow tallest via BFS-depth rows, so piling
  // isolated tables on as further rows compounded that same vertical
  // growth. Vertically centered on the components' bounding box, not
  // top-aligned to it: the hub of a component isn't necessarily near its
  // own top edge (e.g. a hub with children below it but none above), so
  // anchoring at the raw top edge could park the column beside whichever
  // child happened to end up topmost, reading as "floating near a random
  // table" instead of "beside the group."
  if(singles.length){
    let ix0=Infinity, iy0=Infinity, ix1=-Infinity, iy1=-Infinity;
    comps.forEach(comp=>comp.forEach(t=>{
      const p=nodePos[t], s=nodeSize[t]||calcSize(t);
      ix0=Math.min(ix0,p.x-s.w/2); iy0=Math.min(iy0,p.y-s.h/2);
      ix1=Math.max(ix1,p.x+s.w/2); iy1=Math.max(iy1,p.y+s.h/2);
    }));
    if(!isFinite(ix1)){ ix1=0; iy0=0; iy1=0; } // every table is isolated
    singles.sort();
    const totalH=singles.reduce((s,t)=>s+(nodeSize[t]||calcSize(t)).h+gapY,0)-gapY;
    const sx=ix1+gap;
    let sy=(iy0+iy1)/2-totalH/2;
    singles.forEach(t=>{
      const s=nodeSize[t]||calcSize(t);
      nodePos[t]={x:sx+s.w/2, y:sy+s.h/2};
      sy+=s.h+gapY;
    });
  }
}

// viewport aspect ratio (w/h), clamped — wide screens spread layouts sideways
function viewAspect(){
  const R = svg.getBoundingClientRect();
  if (!R.width || !R.height) return 1.6;
  return Math.max(1, Math.min(2.4, R.width / R.height));
}

function layoutAll(tables, edges) {
  tables.forEach(n => { nodeSize[n] = calcSize(n); });
  const newTables = tables.filter(t => !nodePos[t]);
  if (newTables.length === 0) return;

  if (newTables.length === tables.length) {
    // same rectangle-native shelf-packed layout as the overview (BFS depth
    // rows + barycenter crossing reduction) — a circular hub-spoke ring
    // wastes the four corners of a rectangular viewport, which used to
    // make the "focused" view noticeably more zoomed-out than the plain
    // overview of the same tables, exactly backwards from the point of
    // focusing. Passing focusedTable as the preferred hub keeps it front
    // and center the way a dedicated hub-spoke layout did.
    gridLayout(tables, focusedTable);
    return;
  }

  // incremental additions (checkbox re-check, auto-expand pulling in new
  // tables, etc.): new tables land near the already-placed neighbors that
  // pulled them in, instead of a disconnected row appended far below the
  // whole diagram. Tables that share the same anchor (e.g. several direct
  // children of one hub, all arriving together via auto-expand) are grouped
  // and packed as a single row below that anchor — placing each one via an
  // independent nearest-free-slot search instead scatters same-depth
  // siblings across several rows purely by search order, which reads as a
  // much deeper tree than the graph actually is. Isolated additions (no
  // already-placed neighbor at all) fall back to the old append-below row.
  let bx0=Infinity, by0=Infinity, bx1=-Infinity, by1=-Infinity;
  tables.forEach(t => {
    const p=nodePos[t]; if(!p) return;
    const s=nodeSize[t]||{w:160,h:100};
    bx0=Math.min(bx0, p.x-s.w/2); by0=Math.min(by0, p.y-s.h/2);
    bx1=Math.max(bx1, p.x+s.w/2); by1=Math.max(by1, p.y+s.h/2);
  });
  if(!isFinite(bx0)){ gridLayout(tables); return; }
  const overlapsPlaced=(x,y,s)=>{
    for(const t of tables){
      const p=nodePos[t]; if(!p) continue;
      const os=nodeSize[t]||{w:160,h:100};
      if(Math.abs(x-p.x)<(s.w+os.w)/2+20 && Math.abs(y-p.y)<(s.h+os.h)/2+20) return true;
    }
    return false;
  };

  const groups=new Map(); // anchor bucket key -> {ax,ay,members[]} | {isolated,members:[t]}
  const anchorX=new Map(); // new table -> its group's anchor x (pre-placement estimate)
  newTables.forEach(t=>{
    const neighborPos=edges
      .filter(e=>e.source===t||e.target===t)
      .map(e=>e.source===t?e.target:e.source)
      .filter(n=>nodePos[n]&&!newTables.includes(n))
      .map(n=>nodePos[n]);
    if(!neighborPos.length){ groups.set('iso:'+t, {isolated:true, members:[t]}); return; }
    const ax=neighborPos.reduce((s,p)=>s+p.x,0)/neighborPos.length;
    const ay=neighborPos.reduce((s,p)=>s+p.y,0)/neighborPos.length;
    anchorX.set(t, ax);
    const key=Math.round(ax/60)+','+Math.round(ay/60); // nearby anchors share a group
    if(!groups.has(key)) groups.set(key, {ax, ay, members:[]});
    groups.get(key).members.push(t);
  });

  // Isolated additions (no already-placed neighbor at all) stack in a
  // single column along the right edge, top to bottom — appending them as
  // more rows below (the old behavior) meant every unrelated table
  // checked in made the whole diagram grow taller, which compounds fast
  // since the diagram already tends to grow vertically from BFS-depth
  // rows. If an isolated column already exists (from an earlier checkbox
  // pass), continue stacking below its last member at the *same* x —
  // recomputing the anchor from the whole diagram's right edge every time
  // would push each new addition further right than the last, since the
  // previous isolated table is now itself part of "the whole diagram".
  const tset=new Set(tables), edgedTables=new Set();
  edges.forEach(e=>{
    if(tset.has(e.source)&&tset.has(e.target)&&e.source!==e.target){
      edgedTables.add(e.source); edgedTables.add(e.target);
    }
  });
  const placedIsolated=tables.filter(t=>nodePos[t]&&!newTables.includes(t)&&!edgedTables.has(t));
  let ix, iy;
  if(placedIsolated.length){
    let cix0=Infinity, ciy1=-Infinity;
    placedIsolated.forEach(t=>{
      const p=nodePos[t], s=nodeSize[t]||calcSize(t);
      cix0=Math.min(cix0, p.x-s.w/2); ciy1=Math.max(ciy1, p.y+s.h/2);
    });
    ix=cix0; iy=ciy1+40;
  } else {
    // starting a fresh column: center this pass's isolated additions on
    // the rest of the diagram's vertical midpoint (not its top edge) —
    // see the matching comment in gridLayout's singles placement for why
    ix=bx1+110;
    const isolatedThisPass=[...groups.values()].filter(g=>g.isolated).map(g=>g.members[0]);
    const totalH=isolatedThisPass.reduce((s,t)=>s+(nodeSize[t]||calcSize(t)).h+40,0)-40;
    iy=(by0+by1)/2-totalH/2;
  }
  groups.forEach(g=>{
    if(g.isolated){
      const t=g.members[0], s=nodeSize[t]||calcSize(t);
      nodePos[t]={x:ix+s.w/2, y:iy+s.h/2};
      iy+=s.h+40;
      return;
    }
    // pack this group as one centered row below its shared anchor; when that
    // spot is taken, scan sideways within the same row band first, then the
    // bands further down — not straight down only. A pure vertical descent
    // stacks *repeated* additions (several rounds of checkbox clicks, each
    // its own 1-table pass anchored near the same hub) into a single 1-wide
    // column: every new table lands below the previous one, and after a few
    // rounds the diagram is a tall snake full of long detour edges.
    // order the row by where each member's connections sit — already-placed
    // tables by real position, still-unplaced new tables by their group's
    // anchor — so tables that reference each other land adjacent instead of
    // at opposite ends of the band (which forces a detour arc under
    // everything in between)
    if(g.members.length>1){
      const prefX=t=>{
        const xs=[];
        edges.forEach(e=>{
          if(e.source!==t&&e.target!==t) return;
          const n=e.source===t?e.target:e.source;
          if(nodePos[n]) xs.push(nodePos[n].x);
          else if(anchorX.has(n)) xs.push(anchorX.get(n));
        });
        return xs.length?xs.reduce((a,b)=>a+b,0)/xs.length:g.ax;
      };
      g.members.sort((a,b)=>(prefX(a)-prefX(b))||a.localeCompare(b));
    }
    // a huge group (e.g. auto-expand toggled on next to a 15-child hub)
    // would make one unreadably wide row — break it at MAX_ROW_W; the
    // pref-sorted order keeps each chunk internally coherent, and chunks
    // placed later collide with earlier ones, landing on the next band
    const bands=[];
    { let cur=[], w=0;
      g.members.forEach(t=>{
        const s=nodeSize[t]||calcSize(t);
        if(cur.length && w+s.w>MAX_ROW_W){ bands.push(cur); cur=[]; w=0; }
        cur.push(t); w+=s.w+40;
      });
      if(cur.length) bands.push(cur);
    }
    bands.forEach(members=>{
      const rw=members.reduce((s,t)=>s+(nodeSize[t]||calcSize(t)).w+40,0)-40;
      const rh=Math.max(...members.map(t=>(nodeSize[t]||calcSize(t)).h));
      const fits=(ax,ry)=>{
        let x=ax-rw/2;
        for(const t of members){
          const s=nodeSize[t]||calcSize(t);
          if(overlapsPlaced(x+s.w/2, ry, s)) return false;
          x+=s.w+40;
        }
        return true;
      };
      let ax=g.ax, ry=g.ay+rh/2+110, found=false;
      // multi-band (oversized) groups always stack below the anchor —
      // scanning sideways would lay the bands out end to end, recreating
      // exactly the over-wide row the banding is meant to prevent
      const offs=bands.length>1?[0]:[0,1,-1,2,-2];
      for(let level=0; level<12 && !found; level++){
        const y=g.ay+rh/2+110+level*(rh+40);
        for(const m of offs){               // nearest horizontal slot wins
          if(fits(g.ax+m*(rw+40), y)){ ax=g.ax+m*(rw+40); ry=y; found=true; break; }
        }
      }
      if(!found){ // extremely crowded: old straight-down descent as a backstop
        ax=g.ax; ry=g.ay+rh/2+110;
        for(let tries=0; tries<30 && !fits(ax,ry); tries++) ry+=rh+40;
      }
      let x=ax-rw/2;
      members.forEach(t=>{
        const s=nodeSize[t]||calcSize(t);
        nodePos[t]={x:x+s.w/2, y:ry};
        x+=s.w+40;
      });
    });
  });
}

// ── Focus mode position management ────────────────────────────────────────
function enterFocusMode() {
  // Save full-view positions
  Object.keys(nodePos).forEach(k => { basePos[k] = {...nodePos[k]}; });
  Object.keys(nodePos).forEach(k => delete nodePos[k]);
  clearUndoStacks(); // undo history belongs to the overview's nodePos, not focus's
}
function switchFocusTable() {
  Object.keys(nodePos).forEach(k => delete nodePos[k]);
  Object.keys(ringDepth).forEach(k => delete ringDepth[k]);
  clearUndoStacks(); // new focus root -> different table set, unrelated history
}
function exitFocusMode() {
  Object.keys(nodePos).forEach(k => delete nodePos[k]);
  Object.keys(ringDepth).forEach(k => delete ringDepth[k]);
  Object.keys(basePos).forEach(k => { nodePos[k] = {...basePos[k]}; });
  clearUndoStacks(); // back to the overview's nodePos; focus-mode history doesn't apply
}

// re-render; with auto-tidy on, the overview is re-packed first so the
// layout always tracks the current display set
function refreshView(forceFit){
  // snapshot which tables already had a position *before* this render —
  // used below to scope the in-view check to only what's newly appearing,
  // not the whole display set
  const hadPos=new Set(Object.keys(nodePos));
  if(!focusedTable && autoLayout){
    const ts=getDisplayTables();
    ts.forEach(t=>delete nodePos[t]);
    ts.forEach(n=>{ nodeSize[n]=calcSize(n); });
      if(ts.length) gridLayout(ts);
  }
  renderDiagram();
  // don't yank the viewport back to fit-all for a change that didn't
  // actually move it out of view — checking one more table in the list, or
  // any change with Auto-tidy off (existing positions are untouched, new
  // ones incrementally placed near their connections), usually leaves the
  // diagram right where the user was already looking. Entering/leaving
  // focus is a deliberate context switch, not an incidental display-set
  // tweak, so it always fits — otherwise the old viewport can happen to
  // already "contain" the new, smaller focused layout and never zoom in.
  //
  // Scoped to newly-placed tables only (not the full display set): once a
  // user has manually zoomed in past "everything fits," the full display
  // set's bbox essentially never fits the viewport by definition — so
  // checking the whole set meant *any* refreshView call (even unchecking
  // a table, which places nothing new) silently zoomed back out to fit-
  // all on the very next interaction. A removal has nothing new to bring
  // into view, so it must never force a fit; auto-tidy wipes and re-lays-
  // out every table, so everything counts as "newly placed" there and the
  // full-set check still applies as before.
  const newlyPlaced=getDisplayTables().filter(t=>!hadPos.has(t));
  if(forceFit || !isDisplayInView(newlyPlaced)) requestAnimationFrame(fitView);
}

// is the given set of tables' bounding box (already) inside the viewport,
// i.e. would fitView() actually bring anything new into what the user can
// see? Defaults to the full display set (used by callers other than
// refreshView, and by refreshView itself when nothing has changed yet).
function isDisplayInView(tables){
  tables = tables===undefined ? getDisplayTables() : tables;
  if(!tables.length) return true;
  const R=svg.getBoundingClientRect();
  if(!R.width||!R.height) return false;
  let x0=Infinity,y0=Infinity,x1=-Infinity,y1=-Infinity;
  tables.forEach(name=>{
    const p=nodePos[name], s=nodeSize[name]||calcSize(name);
    if(!p) return;
    x0=Math.min(x0,p.x-s.w/2); y0=Math.min(y0,p.y-s.h/2);
    x1=Math.max(x1,p.x+s.w/2); y1=Math.max(y1,p.y+s.h/2);
  });
  if(!isFinite(x0)) return true;
  const sx0=x0*vs+vx, sy0=y0*vs+vy, sx1=x1*vs+vx, sy1=y1*vs+vy;
  return sx0>=-20 && sy0>=-20 && sx1<=R.width+20 && sy1<=R.height+20;
}

// ── fitView ────────────────────────────────────────────────────────────────
function fitView() {
  const tables = getDisplayTables();
  if (!tables.length) return;
  const R = svg.getBoundingClientRect();
  if (!R.width || !R.height) return;
  let x0=Infinity, y0=Infinity, x1=-Infinity, y1=-Infinity;
  tables.forEach(name => {
    const p=nodePos[name], s=nodeSize[name]||calcSize(name);
    if (!p) return;
    x0=Math.min(x0,p.x-s.w/2-30); y0=Math.min(y0,p.y-s.h/2-30);
    x1=Math.max(x1,p.x+s.w/2+30); y1=Math.max(y1,p.y+s.h/2+30);
  });
  if (!isFinite(x0)) return;
  const gW=x1-x0, gH=y1-y0;
  // Allow more zoom for small focused views
  const maxZoom = tables.length<=3 ? 4.0 : tables.length<=8 ? 2.5 : tables.length<=20 ? 1.8 : 1.4;
  vs = Math.min(maxZoom, Math.min(R.width/gW, R.height/gH)) * 0.92;
  vx = (R.width  - gW*vs)/2 - x0*vs;
  vy = (R.height - gH*vs)/2 - y0*vs;
  setTransform();
}

function setTransform() {
  erMain.setAttribute('transform', `translate(${vx},${vy}) scale(${vs})`);
}
function svgPt(cx, cy) {
  const r=svg.getBoundingClientRect();
  return {x:(cx-r.left-vx)/vs, y:(cy-r.top-vy)/vs};
}

// ── Rendering ──────────────────────────────────────────────────────────────
function renderDiagram() {
  erMain.innerHTML='';
  const tables = getDisplayTables();
  const edges  = getDisplayEdges(tables);
  layoutAll(tables, edges);
  // group-layer goes in first — it's the backmost layer (§5.2), so group
  // frames never sit on top of edges/nodes and never intercept clicks meant
  // for them (rect pointer-events:none, see CSS).
  const groupG=svgEl('g',{id:'group-layer'});
  const edgeG=svgEl('g',{id:'edge-layer'});
  const nodeG=svgEl('g',{id:'node-layer'});
  erMain.appendChild(groupG);
  erMain.appendChild(edgeG);
  erMain.appendChild(nodeG);
  edgeObstacles=tables;
  edges.forEach(e => drawEdge(edgeG, e));
  tables.forEach(n => drawNode(nodeG, n));
  // group frames are computed from nodePos/nodeSize, which layoutAll() and
  // drawNode() above just finalized for this render — draw after both.
  drawGroups(groupG, new Set(tables));
  updateEdgeHighlight();
  updateInfoBar(tables.length);
  const ce=document.getElementById('canvas-empty');
  ce.classList.toggle('visible', tables.length===0);
  if(tables.length===0){
    ce.innerHTML='No tables are displayed<br>Check tables in the list on the left, or press "All"'
      +(hiddenTables.size?`<br>(${hiddenTables.size} table(s) banned with 🚫 stay hidden)`:'');
  }
}

function updateInfoBar(shown) {
  const total = allTables().filter(t => !hiddenTables.has(t)).length;
  const el = document.getElementById('info-bar');
  const cnt = shown === total ? `${total} tables` : `${shown} / ${total} tables`;
  el.textContent = focusedTable ? `Focused: ${focusedTable} · ${cnt}` : cnt;
}

// ── Groups (groups Phase 1) — visual table grouping ─────────────────────────
// A group frame is a rounded rect drawn behind whichever of the group's
// members are CURRENTLY displayed, sized to their live nodePos/nodeSize —
// this is layered on top of whatever layout already placed the nodes
// (DESIGN_ROADMAP §P2: no layout affinity in Phase 1), so a frame just
// tracks its members wherever they already are, and disappears the moment
// zero members remain on screen (e.g. --only/--exclude, hidden tables, or
// focus mode having pulled the display set down to something disjoint from
// this group).
const GROUP_PAD = 16;
const GROUP_DEFAULT_COLOR = '#64748b'; // slate — used when a group has no configured color
// Compute the padded bounding box of a group's currently-displayed members,
// or null if none of them are on screen right now. `displayTables` is a Set
// (the same shape getDisplayTables() returns via new Set(...) at call sites)
// so membership tests below stay O(1).
function groupFrameBBox(members, displayTables){
  let x0=Infinity,y0=Infinity,x1=-Infinity,y1=-Infinity, any=false;
  members.forEach(name=>{
    if(!displayTables.has(name)) return;
    const p=nodePos[name], s=nodeSize[name];
    if(!p||!s) return;
    any=true;
    x0=Math.min(x0,p.x-s.w/2); y0=Math.min(y0,p.y-s.h/2);
    x1=Math.max(x1,p.x+s.w/2); y1=Math.max(y1,p.y+s.h/2);
  });
  if(!any) return null;
  return {x0:x0-GROUP_PAD, y0:y0-GROUP_PAD, x1:x1+GROUP_PAD, y1:y1+GROUP_PAD};
}
// Draws every group's frame + title chip into `parent` (the group-layer <g>).
// `displayTables` is a Set of the currently-shown table names. Colors are
// SVG style/attribute values built ONLY from `g.color`, which config.py
// already restricted to `^#[0-9a-fA-F]{3,8}$` at load time (never a raw,
// unvalidated string) — see _check_config_groups. Titles go through
// .textContent (like every other node/edge label in this file), which never
// interprets its argument as markup, so no separate esc() call is needed
// here the way the innerHTML-built notes panels need one.
function drawGroups(parent, displayTables){
  parent.innerHTML='';
  if(!showGroups) return;
  GROUPS.forEach(g=>{
    const bbox=groupFrameBBox(g.tables, displayTables);
    if(!bbox) return; // every member currently hidden — nothing to frame
    const {x0,y0,x1,y1}=bbox;
    const color=g.color || GROUP_DEFAULT_COLOR;
    const frame=svgEl('g',{class:'grp-frame','data-group':g.id});
    frame.appendChild(svgEl('rect',{
      x:x0, y:y0, width:x1-x0, height:y1-y0, rx:10, ry:10,
      class:'grp-rect', style:`fill:${color};stroke:${color}`,
    }));
    parent.appendChild(frame);

    // title chip: small background pill + label, sitting as a TAB just ABOVE
    // the frame's top-left edge — not inside it. A member node's top is only
    // GROUP_PAD below the frame top, and nodes render in the layer above this
    // one, so a chip drawn inside the frame gets covered by the topmost member
    // (reported on the live demo). Placing it above the top edge keeps it
    // clear of every member node. Only the chip has pointer-events (CSS:
    // .grp-rect is none, .grp-chip is auto) — it's the drag handle for moving
    // the whole group (§5.4); the frame body must never steal a click meant
    // for the node/pan below.
    const label = g.title || g.id;
    const chip = svgEl('g', {class:'grp-chip', 'data-group':g.id});
    const chipText = svgEl('text', {x:x0+9, y:y0-6, class:'grp-label-text'});
    chipText.textContent = label;
    chip.appendChild(chipText);
    frame.appendChild(chip);
    // measure real glyph width only after the text node is attached to the
    // live DOM (getBBox needs layout), then insert the background pill
    // behind it sized to fit
    const tw = chipText.getBBox().width;
    const chipBg = svgEl('rect', {
      x:x0, y:y0-19, width:tw+18, height:18, rx:6, ry:6,
      class:'grp-label-bg', style:`fill:${color};stroke:${color}`,
    });
    chip.insertBefore(chipBg, chipText);
    const chipTitle = svgEl('title', {});
    chipTitle.textContent = label;
    chip.appendChild(chipTitle);
    chip.addEventListener('mousedown', e => startGroupDrag(e, g));
  });
}
// Lightweight re-layout of the group-layer alone, called after ordinary node
// drags/moves so frames track their members without a full renderDiagram()
// (which would also rebuild edges/nodes needlessly mid-drag). group counts
// are small, so redrawing all of them each time is simpler and cheap enough
// — no need to track "only the groups touching this drag."
function updateGroupFrames(){
  const g=document.getElementById('group-layer');
  if(!g) return;
  drawGroups(g, new Set(getDisplayTables()));
}
// Start dragging an entire group by its title chip — reuses the SAME
// dragSet/dragGroupStart/dragName/dragUndoSnapshot/dragEdgeCache/isDragging
// machinery the ordinary multi-node drag (drawNode's mousedown, below) sets
// up, just seeded with the group's own currently-displayed members instead
// of the clicked node/selection. dragIsGroup=true tells the shared mousemove
// handler to skip node-snap (a group move has no single "anchor" to snap;
// same effect as holding Alt during a normal drag).
function startGroupDrag(e, g){
  e.stopPropagation();
  if(e.button!==0) return;
  const display=new Set(getDisplayTables());
  const members=g.tables.filter(t=>display.has(t));
  if(!members.length) return;
  dragSet = new Set(members);
  dragGroupStart = {};
  dragSet.forEach(t => { dragGroupStart[t] = {...(nodePos[t]||{x:0,y:0})}; });
  dragUndoSnapshot = snapshotPos();
  dragEdgeCache = getDisplayEdges(getDisplayTables());
  dragName = members[0]; // representative member — anchors the delta the same way a plain node drag does
  const pt=svgPt(e.clientX,e.clientY);
  dragIsGroup=true; isDragging=true; dragMoved=false;
  dragCX=e.clientX; dragCY=e.clientY;
  dragOX=pt.x-(nodePos[dragName]?.x||0); dragOY=pt.y-(nodePos[dragName]?.y||0);
  svg.classList.add('node-drag');
}

function drawNode(parent, name) {
  const t     = DATA.tables[name];
  const pos   = nodePos[name] || {x:0,y:0};
  const sz    = nodeSize[name] || calcSize(name);
  const cols  = visibleCols(name);
  const allCols = t?.columns || [];
  const hidden  = allCols.length - cols.length;
  const lx = pos.x - sz.w/2, ty = pos.y - sz.h/2;

  const depth = ringDepth[name] ?? -1;
  const ringCls = depth===0?'':depth===1?' ring-1':depth===2?' ring-2':depth>=3?' ring-3':'';
  // overview + auto-expand: roots (checked) get a ✓ badge,
  // expansion-pulled tables get the dashed 'auto' style
  const isOverviewAuto = !focusedTable && autoExpand;
  const isRoot = isOverviewAuto && !excludedTables.has(name);
  const isAuto = isOverviewAuto && excludedTables.has(name);
  const isWordHit = wordHit(name);
  const hasLogical = !!logicalName(name);
  const g = svgEl('g', {
    class: 'er-node' + (selectedTables.has(name)?' sel':'') + (name===focusedTable?' center':'') + (isAuto?' auto':'') + ringCls
      + (isWordHit?' word-hit':'') + (wordMatcher&&!isWordHit?' word-dim':'') + (hasLogical?' has-logical':''),
    transform: `translate(${lx},${ty})`,
    'data-name': name,
  });

  g.appendChild(svgEl('rect',{x:3,y:3,width:sz.w,height:sz.h,rx:5,ry:5,class:'n-shadow'}));
  g.appendChild(svgEl('rect',{width:sz.w,height:sz.h,rx:5,ry:5,class:'n-bg'}));
  g.appendChild(svgEl('rect',{width:sz.w,height:HDR_H,rx:5,ry:5,class:'n-hdr'}));
  g.appendChild(svgEl('rect',{y:HDR_H-4,width:sz.w,height:4,class:'n-hdr'}));

  const nt=svgEl('text',{x:sz.w/2,y:HDR_H/2+1,'text-anchor':'middle','dominant-baseline':'middle',class:'n-title'});
  const lg=logicalName(name);
  // Physical and logical spans are ALWAYS both rendered here (mode-
  // independent DOM) — which one(s) actually show is a pure CSS decision
  // (body.namemode-* below), so the exact same node markup works for the
  // live view (toggled by a body class) and for an export (toggled by a
  // rule buildExportSvg injects into the cloned SVG's own stylesheet,
  // exactly like exportOptLabels/exportOptRoots already do) without
  // needing two different render paths.
  //
  // The whole node already gets an amber border on any match (name,
  // column, or comment) via word-hit above — but a match that's ONLY in
  // the comment had no visible sign at all on the text itself, unlike a
  // matching column (which gets its own highlighted row). Mark whichever
  // part of the header text actually matched, the same amber as elsewhere
  // in the word-search color language.
  const nameSpan=svgEl('tspan',{class:'n-physical'+(wordMatcher&&wordMatcher.test(name)?' n-namehit':'')});
  nameSpan.textContent=name;
  nt.appendChild(nameSpan);
  if(lg){
    const lgHit=wordMatcher&&wordMatcher.test(t?.comment||'');
    const openParen=svgEl('tspan',{class:'n-paren'}); openParen.textContent='（';
    nt.appendChild(openParen);
    const lgSpan=svgEl('tspan',{class:'n-logical'+(lgHit?' n-namehit':'')});
    lgSpan.textContent=lg;
    nt.appendChild(lgSpan);
    const closeParen=svgEl('tspan',{class:'n-paren'}); closeParen.textContent='）';
    nt.appendChild(closeParen);
  }
  if(t?.comment){
    const ntTitle=svgEl('title',{});
    ntTitle.textContent=t.comment;
    nt.appendChild(ntTitle);
  }
  g.appendChild(nt);

  if(isRoot){
    const rb=svgEl('text',{x:12,y:HDR_H/2+1,'text-anchor':'middle','dominant-baseline':'middle',class:'n-root'});
    rb.textContent='✓';
    const rbTitle=svgEl('title',{});
    rbTitle.textContent='Checked (expansion root)';
    rb.appendChild(rbTitle);
    g.appendChild(rb);
  }

  // remove this table from the diagram — the lightweight equivalent of
  // unchecking its list checkbox (not the list's separate 🚫 full ban:
  // that hides it everywhere including auto-expand/focus, which is a much
  // bigger commitment than "get this one table off my screen right now").
  // Only shown when unchecking would actually do something: while
  // focused, checkboxes are ignored entirely, and with auto-expand on, a
  // table can still be pulled back in as another root's neighbor even
  // after being excluded — showing a button that visibly does nothing
  // when clicked is worse than not showing it.
  if(canExclude(name)){
    const rb=svgEl('text',{x:sz.w-46,y:HDR_H/2+1,'text-anchor':'middle','dominant-baseline':'middle',class:'n-mode'});
    rb.textContent='⊖';
    const rbTitle=svgEl('title',{});
    rbTitle.textContent='Remove this table from the diagram';
    rb.appendChild(rbTitle);
    rb.addEventListener('mousedown', e=>e.stopPropagation());
    rb.addEventListener('dblclick', e=>e.stopPropagation());
    rb.addEventListener('click', e=>{
      e.stopPropagation();
      excludeTable(name);
    });
    g.appendChild(rb);
  }

  // manual expansion: pull this table's direct relations into the view
  const eb=svgEl('text',{x:sz.w-28,y:HDR_H/2+1,'text-anchor':'middle','dominant-baseline':'middle',class:'n-mode'});
  eb.textContent='⊕';
  const ebTitle=svgEl('title',{});
  ebTitle.textContent='Add this table\u2019s related tables to the view (deep dive)';
  eb.appendChild(ebTitle);
  eb.addEventListener('mousedown', e=>e.stopPropagation());
  eb.addEventListener('dblclick', e=>e.stopPropagation());
  eb.addEventListener('click', e=>{
    e.stopPropagation();
    addTables(stepNeighbors(name, expandDir), name);
  });
  g.appendChild(eb);

  // per-table column-mode toggle (top-right of header)
  const mb=svgEl('text',{x:sz.w-10,y:HDR_H/2+1,'text-anchor':'middle','dominant-baseline':'middle',class:'n-mode'});
  mb.textContent='▤';
  const mbTitle=svgEl('title',{});
  mbTitle.textContent=`Columns: ${COL_LABELS[effColMode(name)]} (click to cycle for this table only)`;
  mb.appendChild(mbTitle);
  mb.addEventListener('mousedown', e=>e.stopPropagation());
  mb.addEventListener('dblclick', e=>e.stopPropagation());
  mb.addEventListener('click', e=>{
    e.stopPropagation();
    const next=(effColMode(name)+1)%3;
    if(next===colMode) delete colOverride[name]; else colOverride[name]=next;
    delete nodeSize[name];
    saveState();
    renderDiagram(); // keep positions: node resizes in place
    showToast(`${name}: ${COL_LABELS[next]}`);
  });
  g.appendChild(mb);

  // tall tables show a maxRows window; mouse wheel scrolls it
  const scrollable=cols.length>maxRows;
  const maxOff=Math.max(0,cols.length-maxRows);
  const off=Math.min(colScroll[name]||0, maxOff);
  const view=cols.slice(off, off+maxRows);

  view.forEach((col,vi) => {
    const i=off+vi; // absolute index keeps stripes stable while scrolling
    const ry=HDR_H+vi*ROW_H+3;
    if(i%2===1) g.appendChild(svgEl('rect',{x:1,y:ry,width:sz.w-2,height:ROW_H,class:'n-alt'})); // inset: keep off the 1px border
    if(colHighlight&&colHighlight.table===name&&colHighlight.match.test(col.name))
      g.appendChild(svgEl('rect',{x:1,y:ry,width:sz.w-2,height:ROW_H,class:'n-colhit'}));
    if(wordMatcher&&(wordMatcher.test(col.name)||wordMatcher.test(col.comment||'')))
      g.appendChild(svgEl('rect',{x:1,y:ry,width:sz.w-2,height:ROW_H,class:'n-wordhit'}));
    const isPK=col.primary, isFK=!isPK&&isFkCol(name, col.name);
    if(isPK){
      g.appendChild(svgEl('rect',{x:4,y:ry+3,width:20,height:14,rx:2,class:'n-bpk'}));
      const bt=svgEl('text',{x:14,y:ry+ROW_H/2+1,'text-anchor':'middle','dominant-baseline':'middle',class:'n-tpk'});
      bt.textContent='PK'; g.appendChild(bt);
    } else if(isFK){
      g.appendChild(svgEl('rect',{x:4,y:ry+3,width:20,height:14,rx:2,class:'n-bfk'}));
      const bt=svgEl('text',{x:14,y:ry+ROW_H/2+1,'text-anchor':'middle','dominant-baseline':'middle',class:'n-tfk'});
      bt.textContent='FK'; g.appendChild(bt);
    }
    const cn=svgEl('text',{x:28,y:ry+ROW_H/2+1,'dominant-baseline':'middle',class:'n-cn'});
    cn.textContent=col.name;
    if(col.comment){
      const ct2=svgEl('title',{}); ct2.textContent=col.comment; cn.appendChild(ct2);
    }
    g.appendChild(cn);
    const ct=svgEl('text',{x:sz.w-(scrollable?10:4),y:ry+ROW_H/2+1,'text-anchor':'end','dominant-baseline':'middle',class:'n-ct'});
    ct.textContent=col.type; g.appendChild(ct);
  });

  if(scrollable){
    // scrollbar thumb along the right edge of the rows area
    const trackY=HDR_H+3, trackH=maxRows*ROW_H-6;
    const thumbH=Math.max(14, trackH*maxRows/cols.length);
    const thumbY=trackY+(trackH-thumbH)*(maxOff?off/maxOff:0);
    g.appendChild(svgEl('rect',{x:sz.w-3.5,y:trackY,width:2.5,height:trackH,rx:1.25,class:'n-sctrack'}));
    g.appendChild(svgEl('rect',{x:sz.w-3.5,y:thumbY,width:2.5,height:thumbH,rx:1.25,class:'n-scthumb'}));
  }
  const footY=HDR_H+view.length*ROW_H+3;
  if(scrollable){
    const mt=svgEl('text',{x:sz.w/2,y:footY+ROW_H/2+1,'text-anchor':'middle','dominant-baseline':'middle',class:'n-more'});
    mt.textContent=`⇅ ${off+1}–${off+view.length} / ${cols.length} cols`+(hidden>0?` (+${hidden})`:'');
    g.appendChild(mt);
  } else if(hidden>0){
    const mt=svgEl('text',{x:sz.w/2,y:footY+ROW_H/2+1,'text-anchor':'middle','dominant-baseline':'middle',class:'n-more'});
    mt.textContent=`+${hidden} cols`; g.appendChild(mt);
  } else if(allCols.length===0 && t?.schema_missing){
    const mt=svgEl('text',{x:sz.w/2,y:footY+ROW_H/2+1,'text-anchor':'middle','dominant-baseline':'middle',class:'n-more'});
    mt.textContent='(no schema info)'; g.appendChild(mt);
  }

  if(scrollable){
    g.addEventListener('wheel', e => {
      if(e.ctrlKey||e.metaKey) return; // pinch zoom stays global
      e.preventDefault(); e.stopPropagation();
      const cur=Math.min(colScroll[name]||0, maxOff);
      const nxt=Math.max(0, Math.min(maxOff, cur+(e.deltaY>0?3:-3)));
      if(nxt===cur) return;
      colScroll[name]=nxt;
      redrawNode(name);
    }, {passive:false});
  }

  g.addEventListener('mousedown', e => {
    e.stopPropagation();
    if(e.button!==0) return;
    // clicking an unselected node with no modifier replaces the selection
    // immediately (Figma convention) so the drag below acts on just it
    if(!(e.shiftKey||e.ctrlKey||e.metaKey) && !selectedTables.has(name)) selectOnly(name);
    // drag the whole selection together when grabbing a member of it;
    // otherwise (e.g. shift-mousedown on an unselected node) drag just this one
    dragSet = selectedTables.has(name) ? new Set(selectedTables) : new Set([name]);
    dragGroupStart = {};
    dragSet.forEach(t => { dragGroupStart[t] = {...(nodePos[t]||{x:0,y:0})}; });
    dragUndoSnapshot = snapshotPos(); // committed on mouseup only if the drag actually moved something
    dragEdgeCache = getDisplayEdges(getDisplayTables());
    const pt=svgPt(e.clientX,e.clientY);
    isDragging=true; dragMoved=false; dragName=name;
    dragCX=e.clientX; dragCY=e.clientY;
    dragOX=pt.x-(nodePos[name]?.x||0); dragOY=pt.y-(nodePos[name]?.y||0);
    svg.classList.add('node-drag');
  });
  g.addEventListener('click', e => {
    e.stopPropagation();
    if(dragMoved){ dragMoved=false; return; } // end of a drag, not a click
    if(e.shiftKey||e.ctrlKey||e.metaKey) toggleSelect(name);
    else selectOnly(name);
  });
  g.addEventListener('dblclick', e => {
    e.stopPropagation();
    focusedTable===name ? clearFocus() : focusTable(name);
  });
  parent.appendChild(g);
}

// redraw a single node in place (column scroll) — edges are untouched
function redrawNode(name){
  const old=document.querySelector(`#node-layer .er-node[data-name="${CSS.escape(name)}"]`);
  if(!old) return;
  const parent=old.parentNode;
  old.remove();
  drawNode(parent, name);
}

// Standard ER cardinality for an edge. has_many / belongs_to are the same
// 1-to-many relation seen from either side — collapse them into one notation.
// Rails association names stay visible in the right pane.
function edgeCard(edge){
  // direct (FK-backed) associations decide the cardinality; an edge is
  // many-to-many only when nothing but through/habtm links the pair
  const direct=edge.assocs.filter(x=>!x.through&&x.type!=='has_and_belongs_to_many');
  if(direct.length===0) return {kind:'nn'};
  const hm=direct.find(x=>x.type==='has_many');
  if(hm) return {kind:'1n', many:hm.from===edge.source?edge.target:edge.source};
  if(direct.some(x=>x.type==='has_one')) return {kind:'11'};
  const bt=direct.find(x=>x.type==='belongs_to');
  if(bt) return {kind:'1n', many:bt.from};
  return {kind:'1n', many:edge.target};
}

let edgeObstacles=[]; // display tables, set right before edges are drawn

// control points sit at this fraction along the baseline (both ends), offset
// by the full bend amount. Pulling them in toward the endpoints (vs. e.g.
// .35) flattens the curve's mid-section — for a wide obstacle sitting between
// two nodes, the curve needs to hold most of its bend across that whole
// width, not just peak briefly at the very center — so a wide obstacle
// clears at a noticeably smaller (less sweeping) bend.
const CURVE_T=0.2;

// count how many non-endpoint nodes the sampled curve passes through
function bendBlocked(edge, src, tgt, nx, ny, bend){
  const c1={x:src.x+(tgt.x-src.x)*CURVE_T+nx*bend, y:src.y+(tgt.y-src.y)*CURVE_T+ny*bend};
  const c2={x:tgt.x-(tgt.x-src.x)*CURVE_T+nx*bend, y:tgt.y-(tgt.y-src.y)*CURVE_T+ny*bend};
  // a cubic Bezier always lies within the bounding box of its 4 control
  // points (convex hull property) — an obstacle whose box doesn't overlap
  // this one *cannot* be crossed by the curve, so it's safe to skip the
  // expensive 13-point sampling below for it. Cheap early-out that matters
  // most on large diagrams, where pickBend calls this ~25x per edge.
  const cbx0=Math.min(src.x,c1.x,c2.x,tgt.x), cbx1=Math.max(src.x,c1.x,c2.x,tgt.x);
  const cby0=Math.min(src.y,c1.y,c2.y,tgt.y), cby1=Math.max(src.y,c1.y,c2.y,tgt.y);
  let blocked=0;
  for(const t of edgeObstacles){
    if(t===edge.source||t===edge.target) continue;
    const p=nodePos[t], s=nodeSize[t];
    if(!p||!s) continue;
    const x0=p.x-s.w/2-8, x1=p.x+s.w/2+8, y0=p.y-s.h/2-8, y1=p.y+s.h/2+8;
    if(x1<cbx0||x0>cbx1||y1<cby0||y0>cby1) continue;
    for(let i=1;i<14;i++){
      const q=i/14, u=1-q;
      const px=u*u*u*src.x+3*u*u*q*c1.x+3*u*q*q*c2.x+q*q*q*tgt.x;
      const py=u*u*u*src.y+3*u*u*q*c1.y+3*u*q*q*c2.y+q*q*q*tgt.y;
      if(px>x0&&px<x1&&py>y0&&py<y1){ blocked++; break; }
    }
  }
  return blocked;
}

function pickBend(edge, src, tgt, nx, ny, base){
  // escalate in fine (20px) steps rather than big jumps (the old 60/110/170/240
  // ladder could overshoot a lot — e.g. a same-row "skip one node" edge only
  // needing ~140px of clearance would jump straight to 240, producing a much
  // wider arc than necessary) — take the smallest detour that actually clears
  const cands=[base];
  for(let m=20; m<=240; m+=20) cands.push(m,-m);
  let best=base, bestN=Infinity;
  for(const b of cands){
    const n=bendBlocked(edge, src, tgt, nx, ny, b);
    if(n===0) return b;
    if(n<bestN){ bestN=n; best=b; }
  }
  return best;
}

function drawEdge(parent, edge) {
  const sp=nodePos[edge.source], tp=nodePos[edge.target];
  if(!sp||!tp) return;
  const card=edgeCard(edge);
  const mk=side=>
    card.kind==='nn' ? 'm-many' :
    card.kind==='11' ? 'm-one'  :
    side===card.many ? 'm-many' : 'm-one';
  const g=svgEl('g',{class:'er-edge'+(card.kind==='nn'?' t-nn':'')
    +(edge.assocs.every(a=>a.inferred)?' inf':''),
    'data-source':edge.source, 'data-target':edge.target});

  // self-referential association: small loop at the node's top-right corner
  if(edge.source===edge.target){
    const s=nodeSize[edge.source]||{w:160,h:100};
    const rx=sp.x+s.w/2, ty=sp.y-s.h/2;
    g.appendChild(svgEl('path',{
      d:`M ${rx} ${ty+34} C ${rx+58} ${ty+34}, ${rx+58} ${ty-20}, ${rx} ${ty+6}`,
      'marker-start':`url(#${mk(edge.source)})`,
      'marker-end':`url(#${mk(edge.target)})`,
    }));
    parent.appendChild(g);
    return;
  }

  const ss=nodeSize[edge.source]||{w:160,h:100};
  const ts=nodeSize[edge.target]||{w:160,h:100};
  const src=borderPt(sp.x,sp.y,ss.w,ss.h,tp.x,tp.y);
  const tgt=borderPt(tp.x,tp.y,ts.w,ts.h,sp.x,sp.y);
  const dx=tgt.x-src.x, dy=tgt.y-src.y;
  const dist=Math.sqrt(dx*dx+dy*dy)||1;
  const nx=-dy/dist, ny=dx/dist;
  // obstacle avoidance: try increasing perpendicular bends until the curve
  // stops passing under other nodes (so relations stay visible)
  const bend=pickBend(edge, src, tgt, nx, ny, edge.assocs.length>1?24:0);
  const cx1=src.x+dx*CURVE_T+nx*bend, cy1=src.y+dy*CURVE_T+ny*bend;
  const cx2=tgt.x-dx*CURVE_T+nx*bend, cy2=tgt.y-dy*CURVE_T+ny*bend;
  g.appendChild(svgEl('path',{
    d:`M ${src.x} ${src.y} C ${cx1} ${cy1},${cx2} ${cy2},${tgt.x} ${tgt.y}`,
    'marker-start':`url(#${mk(edge.source)})`,
    'marker-end':`url(#${mk(edge.target)})`,
  }));
  // label: intermediate (through) table names only, capped to avoid clutter
  const thrAll=[...new Set(edge.assocs.filter(x=>x.through).map(x=>`⇢${x.through}`))];
  const thr=(thrAll.length>2?thrAll.slice(0,2).concat(`+${thrAll.length-2}`):thrAll).join(' ');
  if(thr){
    const mx=(src.x+tgt.x)/2+nx*bend*.5, my=(src.y+tgt.y)/2+ny*bend*.5;
    const lw=thr.length*6+6;
    g.appendChild(svgEl('rect',{x:mx-lw/2,y:my-8,width:lw,height:14,rx:3,class:'e-lbg'}));
    const lt=svgEl('text',{x:mx,y:my+1,'text-anchor':'middle','dominant-baseline':'middle',class:'e-ltxt'});
    lt.textContent=thr; g.appendChild(lt);
  }
  parent.appendChild(g);
}

function borderPt(cx,cy,w,h,tx,ty){
  const dx=tx-cx, dy=ty-cy;
  if(Math.abs(dx)<.001&&Math.abs(dy)<.001) return {x:cx,y:cy};
  const hw=w/2+2, hh=h/2+2;
  const s=Math.min(dx?hw/Math.abs(dx):Infinity, dy?hh/Math.abs(dy):Infinity);
  return {x:cx+dx*s, y:cy+dy*s};
}

// ── Select / focus ────────────────────────────────────────────────────────
// selectOnly: highlight + details, no relayout (diagram click / list click). null clears.
function selectOnly(name){
  selectedTables=new Set(name?[name]:[]);
  selectionAnchor=name||null;
  refreshSelectionUI();
}

// toggleSelect: shift/ctrl-click — add or remove one table from the selection
function toggleSelect(name){
  if(selectedTables.has(name)){
    selectedTables.delete(name);
    if(selectionAnchor===name) selectionAnchor=[...selectedTables].pop()??null;
  } else {
    selectedTables.add(name);
    selectionAnchor=name;
  }
  refreshSelectionUI();
}

function refreshSelectionUI(){
  document.querySelectorAll('.er-node').forEach(el =>
    el.classList.toggle('sel', selectedTables.has(el.getAttribute('data-name'))));
  updateEdgeHighlight();
  document.querySelectorAll('#table-list .table-item').forEach(el=>{
    const nm=el.querySelector('.tname')?.textContent;
    el.classList.toggle('selected', selectedTables.has(nm) && !el.classList.contains('focused'));
  });
  showDetails();
}

// color the edges (and their end markers) that touch any selected table
function updateEdgeHighlight(){
  document.querySelectorAll('.er-edge').forEach(el=>{
    const on = selectedTables.size>0 &&
      (selectedTables.has(el.getAttribute('data-source')) || selectedTables.has(el.getAttribute('data-target')));
    el.classList.toggle('hl', on);
    el.querySelectorAll('path').forEach(p=>{
      ['marker-start','marker-end'].forEach(attr=>{
        const v=p.getAttribute(attr);
        if(!v) return;
        p.setAttribute(attr, v.replace(/#m-(one|many)(-hl)?\)/, on?'#m-$1-hl)':'#m-$1)'));
      });
    });
  });
}

// locateTable: find the table in the current diagram — pan to it, select
// and flash. No relayout. (list click / search Enter)
function locateTable(name){
  if(hiddenTables.has(name)){ showToast(`${name} is banned (click 🚫 to unban)`); return; }
  if(!getDisplayTables().includes(name)){
    showToast(`${name} is not displayed (check it in the list to show)`);
    return;
  }
  const p=nodePos[name];
  if(!p) return;
  const R=svg.getBoundingClientRect();
  if(vs<0.75) vs=1; // zoomed way out → jump to readable size
  vx=R.width/2-p.x*vs; vy=R.height/2-p.y*vs;
  setTransform();
  selectOnly(name);
  renderTableList();
  flashNode(name);
}

function flashNode(name){
  const el=document.querySelector(`.er-node[data-name="${CSS.escape(name)}"]`);
  if(!el) return;
  el.classList.remove('flash'); void el.getBoundingClientRect(); // restart animation
  el.classList.add('flash');
  setTimeout(()=>el.classList.remove('flash'), 1300);
}

// focusTable: filtered view with hub-spoke relayout
// (list / diagram double-click, detail-pane link)
function focusTable(name) {
  if(hiddenTables.has(name)){
    showToast(`${name} is banned (click 🚫 to unban)`);
    return;
  }
  if(focusedTable===name){
    clearFocus();
    return;
  }
  const wasInFocus=!!focusedTable;
  focusedTable=name; selectedTables=new Set([name]); selectionAnchor=name;
  manualExpanded.clear(); // ⊕ deep-dives reset when the focus target changes
  if(!wasInFocus) enterFocusMode();
  else switchFocusTable();
  refreshView(true);
  showDetails(); renderTableList();
  updateDepthCtrl(); updateFocusUI();
  if(!wasInFocus) showToast(`Focused: ${name} — double-click or Esc to exit`);
}

function clearFocus(){
  if(!focusedTable) return; // no-op outside focus — don't destroy the overview layout
  focusedTable=null; selectedTables=new Set(); selectionAnchor=null;
  manualExpanded.clear();
  exitFocusMode();
  refreshView(true);
  showDetails(); renderTableList();
  updateDepthCtrl(); updateFocusUI();
  showToast('Focus cleared — back to the overview');
}

// Ban/unban a table — fully hidden, even from auto-expand and focus views,
// until unbanned (via this same toggle, the list's 🚫 button, or the
// "banned: N table(s)" bar's clear-all link). Used by the list's per-row
// 🚫 button only — the diagram node's own ⊖ button is the much lighter
// excludeTable() below, not this.
function toggleBan(name){
  if(hiddenTables.has(name)){ hiddenTables.delete(name); }
  else {
    hiddenTables.add(name);
    if(focusedTable===name){ focusedTable=null; selectedTables=new Set(); selectionAnchor=null; exitFocusMode(); }
  }
  saveState();
  if(focusedTable) switchFocusTable(); // re-run hub-spoke with new table set
  refreshView(); renderTableList(); updateHiddenBar();
  showDetails();
}

// Exclude a table from the current view — the same effect as unchecking
// its list checkbox, kept separate from toggleBan() above because it's a
// much smaller commitment (easy to bring back by re-checking the box or
// via ⊕ on a related table; doesn't survive auto-expand pulling it back
// in as someone else's neighbor, unlike a ban).
function excludeTable(name){
  excludedTables.add(name);
  noAutoExpandRoot.delete(name); // matches the checkbox handler's own intent: explicit removal, not a stale auto-expand root
  saveState();
  refreshView(); renderTableList();
}

// Would excludeTable(name) actually remove it from what's currently
// visible? False while focused (checkboxes/exclusion are ignored by the
// focus view entirely) or when auto-expand would just pull the table back
// in as another root's neighbor — drawNode() uses this to hide the ⊖
// button rather than show one that visibly does nothing when clicked.
function canExclude(name){
  if(focusedTable) return false;
  if(!autoExpand) return true;
  const already=excludedTables.has(name);
  excludedTables.add(name);
  const stillShown=getDisplayTables().includes(name);
  if(!already) excludedTables.delete(name);
  return !stillShown;
}

// ── Left pane ─────────────────────────────────────────────────────────────
function renderTableList(){
  const list  = document.getElementById('table-list');
  const filterMatcher = makeMatcher(document.getElementById('search').value,
    {regex:filterRegexMode, cs:filterCaseSensitive});
  const searchBox = document.getElementById('search-box');
  searchBox.classList.toggle('bad-re', !!filterMatcher?.error);
  const prevScroll = list.scrollTop;
  list.innerHTML='';
  const inView = (focusedTable||autoExpand) ? new Set(getDisplayTables()) : null;
  const colHit = t => filterMatcher && !filterMatcher.test(t)
    ? (DATA.tables[t]?.columns||[]).find(c=>filterMatcher.test(c.name) || filterMatcher.test(c.comment||''))?.name
    : null;
  const commentHit = t => filterMatcher && filterMatcher.test(DATA.tables[t]?.comment||'');
  // notesForTable() is shared with wordHit() (toolbar Highlight) — see its
  // definition in the Notes section below.
  const noteHit = t => filterMatcher
    ? notesForTable(t).find(n=>filterMatcher.test(noteText(n)))
    : null;
  // Global notes have no owning table, so a match surfaces as a synthetic
  // banner row at the top of the list instead (clicking scrolls/expands the
  // legend, where the global note actually renders).
  if(filterMatcher){
    const globalHits=NOTES.filter(n=>n.scope==='global' && filterMatcher.test(noteText(n)));
    if(globalHits.length){
      const gitem=document.createElement('div');
      gitem.className='table-item note-banner';
      const glbl=document.createElement('label');
      glbl.innerHTML=`🌐 <span class="note-hit">📝 global note: ${escMark(globalHits[0].title||globalHits[0].text)}</span>`;
      glbl.title='Click to open the legend and view the global note(s)';
      glbl.addEventListener('click', ()=>{
        document.getElementById('legend').classList.remove('collapsed');
      });
      gitem.appendChild(glbl);
      list.appendChild(gitem);
    }
  }
  allTables()
    .filter(t => !filterMatcher || filterMatcher.test(t)
      || (DATA.tables[t]?.columns||[]).some(c=>filterMatcher.test(c.name) || filterMatcher.test(c.comment||''))
      || commentHit(t)
      || noteHit(t))
    .forEach(name => {
      const t=DATA.tables[name];
      const isHidden=hiddenTables.has(name);
      // shown because auto-expansion pulled it in (not by its own checkbox)
      const autoShown=!isHidden && !!inView && inView.has(name)
        && (focusedTable ? focusedTable!==name : excludedTables.has(name));
      const item=document.createElement('div');
      item.className='table-item'
        +(focusedTable===name?' focused':'')
        +(selectedTables.has(name)&&focusedTable!==name?' selected':'')
        +(isHidden?' hidden':'')
        +(autoShown?' inview':'')
        +(wordHit(name)?' word-hit':'');
      const lbl=document.createElement('label');
      lbl.addEventListener('click', e => {
        if(e.target.tagName==='INPUT'||e.target.tagName==='BUTTON') return;
        e.preventDefault();
        locateTable(name); // single click = find it in the diagram
      });
      lbl.addEventListener('dblclick', e => {
        if(e.target.tagName==='INPUT'||e.target.tagName==='BUTTON') return;
        e.preventDefault();
        focusedTable===name ? clearFocus() : focusTable(name); // double click = filter
      });
      const cb=document.createElement('input');
      cb.type='checkbox'; cb.checked=!excludedTables.has(name);
      cb.disabled=isHidden||autoShown; // auto-expanded tables: checkbox locked
      if(autoShown) cb.title='Shown by auto-expansion (checkbox locked)';
      cb.addEventListener('change', e => {
        e.stopPropagation();
        if(cb.checked){ excludedTables.delete(name); }
        else { excludedTables.add(name); }
        noAutoExpandRoot.delete(name); // a direct checkbox click is explicit intent — full root again
        saveState();
        // showDetails() so a selected table's note sections re-render against
        // its new visibility right away — gating notes inside showDetails only
        // takes effect when showDetails is actually re-invoked (Sol re-review
        // #1: unchecking a selected table used to leave its note stale until
        // the next unrelated redraw). Mirrors toggleBan()'s tail.
        if(focusedTable){ renderTableList(); showDetails(); return; } // the focus view ignores checkboxes
        refreshView(); renderTableList(); showDetails();
      });
      const nm=document.createElement('span'); nm.className='tname'; nm.textContent=name;
      lbl.appendChild(cb); lbl.appendChild(nm);
      const lg=logicalName(name);
      if(lg){const lgEl=document.createElement('span');lgEl.className='tlogical';lgEl.textContent=lg;lbl.appendChild(lgEl);}
      const hit=colHit(name);
      if(hit){const hb2=document.createElement('span');hb2.className='col-hit';hb2.textContent='⌕ '+hit;lbl.appendChild(hb2);}
      const nhit=noteHit(name);
      if(nhit){
        const nb=document.createElement('span'); nb.className='note-hit';
        nb.textContent='📝 note: '+(nhit.title||nhit.text).slice(0,40);
        lbl.appendChild(nb);
      }
      // Relation count + ban button live in a `.tail` wrapper pinned to the
      // row's right edge (margin-left:auto) so their position doesn't drift
      // with the table name's length or whether a logical name is present.
      const tail=document.createElement('span'); tail.className='tail';
      const ac=(t?.associations||[]).length;
      if(ac>0){const b=document.createElement('span');b.className='rel-badge';b.textContent=ac;tail.appendChild(b);}
      const hb=document.createElement('button');
      hb.className='hide-btn'; hb.type='button'; hb.textContent='🚫';
      hb.title=isHidden?'Unban':'Ban completely (never shown, even by auto-expand)';
      hb.addEventListener('click', e => {
        e.stopPropagation(); e.preventDefault();
        toggleBan(name);
      });
      tail.appendChild(hb);
      lbl.appendChild(tail);
      item.appendChild(lbl);
      list.appendChild(item);
    });
  // Scroll to the focused item only when the focus target changed;
  // otherwise keep the user's scroll position
  if(focusedTable && focusedTable!==lastFocusScrolled){
    const el=list.querySelector('.table-item.focused');
    if(el) el.scrollIntoView({block:'nearest',behavior:'smooth'});
  } else {
    list.scrollTop=prevScroll;
  }
  lastFocusScrolled=focusedTable;
}
let lastFocusScrolled=null;

// Apply the current wordQuery everywhere: table-list bar (via
// renderTableList, called above/below already), diagram node borders/dim
// (class-toggle, no rebuild), diagram column rows (redrawNode, only for
// tables whose match set actually changed), the counter, and the right
// pane if a single table is selected. Called after every wordQuery change.
function updateWordHighlight(){
  wordMatcher=makeMatcher(wordQuery, {regex:wordRegexMode, cs:wordCaseSensitive});
  const box=document.getElementById('word-search-box');
  box.classList.toggle('has-query', !!wordQuery);
  box.classList.toggle('bad-re', !!wordMatcher?.error);
  const tables=getDisplayTables();
  const hitTables=new Set(tables.filter(wordHit));
  document.querySelectorAll('#node-layer .er-node').forEach(el=>{
    const name=el.getAttribute('data-name');
    const isHit=hitTables.has(name);
    el.classList.toggle('word-hit', isHit);
    el.classList.toggle('word-dim', !!wordMatcher && !isHit);
  });
  tables.forEach(name=>{
    const sig=wordColHits(name).join('\x00');
    if(wordColHitCache.get(name)!==sig){
      wordColHitCache.set(name, sig);
      redrawNode(name);
    }
  });
  const countEl=document.getElementById('word-search-count');
  if(wordMatcher?.error){ countEl.textContent='!'; countEl.title=wordMatcher.error; }
  else { countEl.textContent = wordQuery ? String(hitTables.size) : ''; countEl.title=''; }
  wordMatchIdx=-1;
  renderTableList();
  if(selectedTables.size===1) showDetails(); // re-marks the currently-shown table
}

// ── Hidden-tables bar ───────────────────────────────────────────────────────
function updateHiddenBar(){
  const bar=document.getElementById('hidden-bar');
  const n=hiddenTables.size;
  bar.classList.toggle('visible', n>0);
  document.getElementById('hidden-count').textContent=`🚫 banned: ${n} table(s)`;
}
document.getElementById('hidden-clear').addEventListener('click', () => {
  hiddenTables.clear(); saveState();
  if(focusedTable) switchFocusTable(); // re-run hub-spoke with new table set
  refreshView(); renderTableList(); updateHiddenBar();
  showDetails();
});

// ── Right pane ─────────────────────────────────────────────────────────────
function showDetails(){
  const el=document.getElementById('table-details');
  if(selectedTables.size>=2){ renderMultiSelectDetails(el); return; }
  const name = selectionAnchor && selectedTables.has(selectionAnchor)
    ? selectionAnchor : [...selectedTables][0];
  if(!name||!DATA.tables[name]){el.innerHTML='<div class="empty-state">Click a table<br>to see its details</div>';return;}
  const t=DATA.tables[name];
  const cols=t.columns||[], assocs=t.associations||[];
  // notes gating (Sol review finding 2): a stale selection can outlive its
  // table being unchecked/banned from the current view (selectedTables isn't
  // cleared by excludeTable()/toggleBan()). Columns/indexes/associations keep
  // rendering regardless — only table/relation notes are gated on the anchor
  // table still being part of the displayed set, per NOTES_PHASE1_SPEC's
  // "hidden table's note doesn't leak into the right pane" contract. A
  // global note has no owning table, so it's unaffected and always shown
  // via the legend.
  const anchorVisible = getDisplayTables().includes(name);
  let h=`<div class="detail-name">${escMark(name)}</div>`;
  if(t.comment) h+=`<div class="tbl-comment">${esc(t.comment)}</div>`;
  if(t.schema_missing && cols.length===0){
    h+='<div class="sec-title">Columns</div>'
      +'<div class="empty-state" style="margin-top:8px;text-align:left">No column info — this table is not in '
      +'schema.rb / structure.sql (likely managed by a gem or another database).</div>';
  }
  if(cols.length>0){
    h+='<div class="sec-title">Columns</div><div class="col-list">';
    cols.forEach(c=>{
      const isPK=c.primary, isFK=!isPK&&isFkCol(name, c.name);
      const bc=isPK?'badge bdg-pk':isFK?'badge bdg-fk':'badge bdg-mt';
      const bt=isPK?'PK':isFK?'FK':'';
      const nullEl=c.nullable?'<span class="col-null">NULL</span>':'';
      const cmtEl=c.comment?`<div class="col-comment">${esc(c.comment)}</div>`:'';
      h+=`<div class="col-entry"><div class="col-main"><span class="${bc}">${bt}</span><span class="col-cn">${escMark(c.name)}</span><span class="col-ct">${esc(c.sql_type||c.type)}</span>${nullEl}</div>${cmtEl}</div>`;
    });
    h+='</div>';
  }
  const idxs=t.indexes||[];
  if(idxs.length>0){
    h+='<div class="sec-title">Indexes</div><div class="idx-list">';
    idxs.forEach(ix=>{
      const uq=ix.unique?'<span class="badge-uq">UNIQUE</span>':'';
      h+=`<div class="idx-entry"><div class="idx-name">${esc(ix.name)}${uq}</div>`
        +`<div class="idx-cols">${ix.columns.map(escMark).join(', ')}</div></div>`;
    });
    h+='</div>';
  }
  if(assocs.length>0){
    const dispTables=new Set(getDisplayTables());
    // plain-language descriptions for members not familiar with Rails
    const DESC={
      has_many:'one-to-many — one row here owns many rows there',
      belongs_to:'many-to-one — belongs to one row there (this side holds the FK)',
      has_one:'one-to-one — one row here owns exactly one row there',
      has_and_belongs_to_many:'many-to-many — both sides own many, via a join table',
    };
    h+='<div class="sec-title">Associations</div><div class="assoc-list">';
    assocs.forEach(a=>{
      const cls='t-'+(a.type==='has_and_belongs_to_many'?'habtm':a.through?'through':a.type);
      let desc=a.through
        ?`many-to-many (through) — reached via the join table "${a.through}"`
        :(DESC[a.type]||'');
      if(a.inferred) desc+=' (inferred from the FK column name; no association is declared)';
      if(a.db_fk) desc+=' (from a database foreign-key constraint)';
      if(a.schema_fk) desc+=' (from a foreign-key definition in db/schema.rb)';
      if(a.manual) desc+=' (manually declared in the config file)';
      const inView=dispTables.has(a.target);
      const isHidden=hiddenTables.has(a.target);
      const link=a.polymorphic
        ?`<span class="not-in-view" title="Polymorphic association (target table is decided at runtime)">(polymorphic)</span>`
        :inView
          ?`<a data-goto="${esc(a.target)}">${escMark(a.target)}</a>`
          :isHidden
            ?`<span class="not-in-view" title="Banned with 🚫">${escMark(a.target)} 🚫</span>`
            :`<a class="add-target" data-add="${esc(a.target)}" title="Not displayed — click to add to the diagram">${escMark(a.target)} ＋</a>`;
      const thr=a.through?`<div class="athrough">through: :${esc(a.through)}</div>`:'';
      const relNotes=anchorVisible ? NOTES.filter(n=>relNoteMatches(n, name, a)) : [];
      const relNotesHtml=relNotes.length
        ? '<div class="assoc-notes">'+relNotes.map(noteBlockHtml).join('')+'</div>' : '';
      // plain-language description appears as a tooltip on hover
      h+=`<div class="assoc-entry ${cls}" title="${esc(desc)}"><div class="atype">${esc(a.type)}${a.inferred?' <span class="badge-inf">inferred</span>':''}${a.db_fk?' <span class="badge-dbfk">DB FK</span>':''}${a.schema_fk?' <span class="badge-schemafk">schema FK</span>':''}${a.manual?' <span class="badge-manual">manual</span>':''}</div><div class="aname">:${escMark(a.name)}</div><div class="atarget">→ ${link}</div>${thr}${relNotesHtml}</div>`;
    });
    h+='</div>';
  }
  const tableNotes=anchorVisible ? NOTES.filter(n=>n.scope==='table' && n.table===name) : [];
  if(tableNotes.length){
    h+='<div class="sec-title">Notes</div><div class="note-list">';
    tableNotes.forEach(n=> h+=noteBlockHtml(n));
    h+='</div>';
  }
  el.innerHTML=h;
  // single click = locate (pan+flash), consistent with the list; double-click to focus
  el.querySelectorAll('[data-goto]').forEach(a=>{
    a.addEventListener('click',()=>locateTable(a.dataset.goto));
    a.addEventListener('dblclick',()=>focusTable(a.dataset.goto));
  });
  // not-in-view targets: click to pull them into the diagram
  el.querySelectorAll('[data-add]').forEach(a=>a.addEventListener('click',()=>addTables([a.dataset.add])));
}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
// esc(), plus wrapping wordMatcher matches in a <mark> — get every match
// range first (works for both substring and regex mode, including
// variable-length regex matches), then esc() each fragment individually
// and rejoin. Never regex-replace inside already-escaped HTML (a query
// like "quot" or "amp" would then match inside an entity).
function escMark(s){
  s=String(s);
  if(!wordMatcher) return esc(s);
  const ranges=wordMatcher.ranges(s);
  if(!ranges.length) return esc(s);
  let out='', i=0;
  ranges.forEach(([a,b])=>{
    out+=esc(s.slice(i,a))+'<mark class="word-mark">'+esc(s.slice(a,b))+'</mark>';
    i=b;
  });
  return out+esc(s.slice(i));
}

// ── Notes (notes Phase 1) ────────────────────────────────────────────────
// Every note field is plain text — rendered via innerHTML but always passed
// through esc()/escMark(), same discipline as every other DATA field above.
// Link URLs are validated to http(s)-only at config-load time (config.py); no
// Markdown/raw-HTML rendering is ever done here.
function noteLinksHtml(links){
  if(!links || !links.length) return '';
  return '<div class="note-links">' + links.map(l=>
    `<a href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">${escMark(l.label||l.url)}</a>`
  ).join('') + '</div>';
}
function noteBlockHtml(n){
  const ttl = n.title ? `<div class="note-title">${escMark(n.title)}</div>` : '';
  return `<div class="note-entry">${ttl}<div class="note-text">${escMark(n.text)}</div>${noteLinksHtml(n.links)}</div>`;
}
// Mirrors the Python resolve_and_validate_notes identity fields exactly —
// never re-derive relation identity from scratch in JS (association_key is
// the single source of truth; Python already resolved each relation note to
// one specific association before it ever reached DATA).
function relNoteMatches(n, srcTable, a){
  return n.scope==='relation' && n.source_table===srcTable && n.target===a.target
      && n.type===a.type && n.name===a.name
      && (n.foreign_key ?? null) === (a.foreign_key ?? null)
      && (n.through ?? null) === (a.through ?? null)
      && !!n.polymorphic === !!a.polymorphic;
}
// Aggregate a note's searchable text (title/text/link labels/target names) so
// the left-pane filter and the global-note search banner can match on it.
function noteText(n){
  return [n.title, n.text, ...(n.links||[]).map(l=>l.label), n.table, n.target, n.source_table]
    .filter(Boolean).join(' ');
}
// Notes attached to table `t`: its own table notes, plus relation notes whose
// source_table is `t` (the side the note's association lives on — mirrors the
// right-pane Associations list, which is keyed the same way). Shared by
// renderTableList (left-pane note-hit badge) and wordHit (toolbar Highlight),
// so both search boxes agree on which notes belong to which table.
function notesForTable(t){
  return NOTES.filter(n =>
    (n.scope==='table' && n.table===t) || (n.scope==='relation' && n.source_table===t));
}
function renderGlobalNotes(){
  const box=document.getElementById('legend-notes');
  if(!box) return;
  const gs=NOTES.filter(n=>n.scope==='global');
  if(!gs.length){ box.innerHTML=''; box.style.display='none'; return; }
  box.style.display='';
  box.innerHTML='<div class="lgn-title">Design notes</div>'+gs.map(noteBlockHtml).join('');
}

// ── Multi-select: align / distribute panel ─────────────────────────────────
function renderMultiSelectDetails(el){
  const names=[...selectedTables].filter(t=>DATA.tables[t]).sort();
  const canAlign=selectedPositioned().length>=2;
  const canDist=selectedPositioned().length>=3;
  let h=`<div class="detail-name">${names.length} tables selected</div>`;
  h+='<div class="sec-title">Align</div><div class="msel-btns">'
    +`<button class="diag-btn" data-align="left" ${canAlign?'':'disabled'} title="Align left edges">⇤ Left</button>`
    +`<button class="diag-btn" data-align="top" ${canAlign?'':'disabled'} title="Align top edges">⇡ Top</button>`
    +`<button class="diag-btn" data-align="hcenter" ${canAlign?'':'disabled'} title="Align horizontal centers">↔ Center</button>`
    +`<button class="diag-btn" data-align="vcenter" ${canAlign?'':'disabled'} title="Align vertical centers">↕ Middle</button>`
    +'</div>';
  h+='<div class="sec-title">Distribute</div><div class="msel-btns">'
    +`<button class="diag-btn" data-dist="h" ${canDist?'':'disabled'} title="Distribute horizontally — equalize the gaps">⇔ Horiz.</button>`
    +`<button class="diag-btn" data-dist="v" ${canDist?'':'disabled'} title="Distribute vertically — equalize the gaps">⇕ Vert.</button>`
    +'</div>';
  h+='<div class="sec-title">Selected</div><div class="msel-list">';
  names.forEach(t=>{
    h+=`<div class="msel-chip"><span class="msel-cn">${esc(t)}</span>`
      +`<button class="msel-remove" data-remove="${esc(t)}" title="Remove from selection">✕</button></div>`;
  });
  h+='</div>';
  el.innerHTML=h;
  el.querySelectorAll('[data-align]').forEach(b=>b.addEventListener('click',()=>alignSelection(b.dataset.align)));
  el.querySelectorAll('[data-dist]').forEach(b=>b.addEventListener('click',()=>distributeSelection(b.dataset.dist)));
  el.querySelectorAll('[data-remove]').forEach(b=>b.addEventListener('click',()=>{
    const t=b.dataset.remove;
    selectedTables.delete(t);
    if(selectionAnchor===t) selectionAnchor=[...selectedTables].pop()??null;
    refreshSelectionUI();
  }));
}

// members of the current selection that are actually on the canvas (have a laid-out position)
function selectedPositioned(){
  return [...selectedTables].filter(t=>nodePos[t]&&(nodeSize[t]||calcSize(t)));
}

// ── Layout undo/redo ─────────────────────────────────────────────────────
function snapshotPos(){
  const snap={};
  Object.keys(nodePos).forEach(k=>{ snap[k]={...nodePos[k]}; });
  return snap;
}
function restorePos(snap){
  Object.keys(nodePos).forEach(k=>delete nodePos[k]);
  Object.keys(snap).forEach(k=>{ nodePos[k]={...snap[k]}; });
}
// call *before* mutating nodePos
function pushUndoSnapshot(){
  undoStack.push(snapshotPos());
  if(undoStack.length>UNDO_LIMIT) undoStack.shift();
  redoStack=[]; // a fresh action invalidates the redo branch
  updateUndoRedoUI();
}
function doUndo(){
  if(!undoStack.length){ showToast('Nothing to undo'); return; }
  redoStack.push(snapshotPos());
  restorePos(undoStack.pop());
  updateUndoRedoUI();
  renderDiagram(); // redraw from the restored positions; keep pan/zoom as-is
  showToast('Undid layout change');
}
function doRedo(){
  if(!redoStack.length){ showToast('Nothing to redo'); return; }
  undoStack.push(snapshotPos());
  restorePos(redoStack.pop());
  updateUndoRedoUI();
  renderDiagram();
  showToast('Redid layout change');
}
function updateUndoRedoUI(){
  const u=document.getElementById('btn-undo'), r=document.getElementById('btn-redo');
  if(u) u.disabled=!undoStack.length;
  if(r) r.disabled=!redoStack.length;
}
// a snapshot's nodePos only makes sense for the mode/table-set it was taken
// in — entering/leaving/switching focus (and loading a saved view) all
// wholesale-replace nodePos with an unrelated coordinate space, so an undo
// stack built up in one of them is meaningless (and actively corrupting:
// restoring it drops nodes or scrambles positions) in another
function clearUndoStacks(){
  undoStack=[]; redoStack=[];
  updateUndoRedoUI();
}

// redraw from the mutated nodePos without relaying out or moving the viewport
// (same "just redraw" spirit as the drag-mouseup path — no auto-tidy relayout)
function afterManualReposition(){
  renderDiagram();
  updateEdgeHighlight();
}

function alignSelection(mode){
  const ts=selectedPositioned();
  if(ts.length<2) return;
  pushUndoSnapshot();
  const boxes=ts.map(t=>{
    const p=nodePos[t], s=nodeSize[t]||calcSize(t);
    return {t, x0:p.x-s.w/2, y0:p.y-s.h/2, x1:p.x+s.w/2, y1:p.y+s.h/2};
  });
  if(mode==='left'){
    const x0=Math.min(...boxes.map(b=>b.x0));
    boxes.forEach(b=>{ nodePos[b.t].x=x0+(b.x1-b.x0)/2; });
  } else if(mode==='top'){
    const y0=Math.min(...boxes.map(b=>b.y0));
    boxes.forEach(b=>{ nodePos[b.t].y=y0+(b.y1-b.y0)/2; });
  } else if(mode==='hcenter'){
    const cx=(Math.min(...boxes.map(b=>b.x0))+Math.max(...boxes.map(b=>b.x1)))/2;
    boxes.forEach(b=>{ nodePos[b.t].x=cx; });
  } else if(mode==='vcenter'){
    const cy=(Math.min(...boxes.map(b=>b.y0))+Math.max(...boxes.map(b=>b.y1)))/2;
    boxes.forEach(b=>{ nodePos[b.t].y=cy; });
  }
  afterManualReposition();
  showToast(`Aligned ${ts.length} tables`);
}

// keeps the two outermost members fixed, equalizes the edge-to-edge gaps
// between the rest (equal centers would look wrong given varying node sizes)
function distributeSelection(axis){
  const ts=selectedPositioned();
  if(ts.length<3) return;
  pushUndoSnapshot();
  const key=axis==='h'?'x':'y', dim=axis==='h'?'w':'h';
  const items=ts.map(t=>{
    const s=nodeSize[t]||calcSize(t);
    return {t, half:s[dim]/2, c:nodePos[t][key]};
  }).sort((a,b)=>a.c-b.c);
  const first=items[0], last=items[items.length-1];
  const totalSpan=(last.c+last.half)-(first.c-first.half);
  const totalSize=items.reduce((s,it)=>s+it.half*2,0);
  const gap=Math.max(0,(totalSpan-totalSize))/(items.length-1);
  let edge=first.c-first.half;
  items.forEach(it=>{
    nodePos[it.t][key]=edge+it.half;
    edge+=it.half*2+gap;
  });
  afterManualReposition();
  showToast(`Distributed ${ts.length} tables`);
}

// ── Toast ─────────────────────────────────────────────────────────────────
let toastTimer;
function showToast(msg){
  const el=document.getElementById('toast');
  el.textContent=msg; el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer=setTimeout(()=>el.classList.remove('show'), 2400);
}

// ── PNG Export ─────────────────────────────────────────────────────────────
// Inline CSS for SVG export (classes won't resolve in offscreen canvas)
const EXPORT_CSS = `
.grp-rect{fill-opacity:.06;stroke-width:1.5;stroke-opacity:.55;pointer-events:none}
.grp-label-bg{fill-opacity:.16;stroke:none}
.grp-label-text{fill:#1e293b;font-size:11px;font-weight:700;font-family:sans-serif}
.er-node .n-shadow{fill:rgba(0,0,0,.07)}
.er-node .n-bg{fill:#fff;stroke:#cbd5e1;stroke-width:1}
.er-node.sel .n-bg{stroke:#3b82f6;stroke-width:2}
.er-node .n-hdr{fill:#1e293b}
.er-node.ring-1 .n-hdr{fill:#1e3a5f}
.er-node.ring-2 .n-hdr{fill:#374151}
.er-node.ring-3 .n-hdr{fill:#4b5563}
.er-node .n-title{fill:#f8fafc;font-size:12px;font-weight:bold;font-family:monospace}
.er-node .n-title .n-logical{font-size:11px;font-weight:normal;opacity:.7}
.er-node .n-title .n-namehit{fill:#f59e0b;opacity:1}
.er-node .n-alt{fill:#f8fafc}
.er-node .n-colhit{fill:#fef3c7}
.er-node.word-hit .n-bg{stroke:#f59e0b;stroke-width:2.5}
.er-node.word-dim{opacity:.45}
.er-node .n-wordhit{fill:#fde68a}
.er-node .n-cn{fill:#1e293b;font-size:11px;font-family:monospace}
.er-node .n-ct{fill:#64748b;font-size:10px;font-family:monospace}
.er-node .n-bpk{fill:#fef08a}.er-node .n-bfk{fill:#bfdbfe}
.er-node .n-tpk{fill:#713f12;font-size:9px;font-weight:bold}
.er-node .n-tfk{fill:#1e3a5f;font-size:9px;font-weight:bold}
.er-node .n-more{fill:#94a3b8;font-size:10px}
.er-node .n-mode{display:none}
.er-node.auto .n-hdr{fill:#64748b}
.er-node.auto .n-bg{stroke-dasharray:5 3}
.er-node .n-root{fill:#4ade80;font-size:11px;font-weight:bold}
.er-node .n-sctrack{fill:#e2e8f0}
.er-node .n-scthumb{fill:#94a3b8}
.er-edge path{fill:none;stroke:#64748b;stroke-width:1.5;opacity:.75}
.er-edge.t-nn path{stroke-dasharray:6 3}
.er-edge.inf path{opacity:.4;stroke-dasharray:2 4}
.er-edge.hl path{stroke:#2563eb;stroke-width:2;opacity:1}
.er-edge.hl .e-ltxt{fill:#2563eb}
.er-edge .e-lbg{fill:white;opacity:.88}
.er-edge .e-ltxt{font-size:10px;font-family:sans-serif;fill:#64748b}
`;

function buildExportSvg(){
  const tables=getDisplayTables();
  if(!tables.length){showToast('No tables are displayed');return null;}

  let x0=Infinity,y0=Infinity,x1=-Infinity,y1=-Infinity;
  tables.forEach(name=>{
    const p=nodePos[name],s=nodeSize[name];
    if(!p||!s) return;
    x0=Math.min(x0,p.x-s.w/2-24); y0=Math.min(y0,p.y-s.h/2-24);
    x1=Math.max(x1,p.x+s.w/2+24); y1=Math.max(y1,p.y+s.h/2+24);
  });
  if(!isFinite(x0)){showToast('Export failed');return null;}
  // group frames (groups Phase 1) extend past their member nodes' own bbox —
  // by GROUP_PAD for the rounded frame, and by an unbounded amount for a title
  // chip whose text is wider than its members. Measuring the live group-layer's
  // own getBBox() captures BOTH exactly (frames + chips as actually rendered),
  // where groupFrameBBox() alone knows only the member nodes and would clip a
  // long title (Codex re-review #1). Skipped when groups are hidden — the layer
  // is then empty, matching its empty clone in the export.
  if(showGroups){
    const gl=document.getElementById('group-layer');
    if(gl && gl.childNodes.length){
      const b=gl.getBBox();
      x0=Math.min(x0,b.x); y0=Math.min(y0,b.y);
      x1=Math.max(x1,b.x+b.width); y1=Math.max(y1,b.y+b.height);
    }
  }
  const vw=x1-x0, vh=y1-y0;

  const exportSvg=document.createElementNS(NS,'svg');
  exportSvg.setAttribute('xmlns',NS);
  exportSvg.setAttribute('width',Math.ceil(vw)); exportSvg.setAttribute('height',Math.ceil(vh));
  exportSvg.setAttribute('viewBox',`${x0} ${y0} ${vw} ${vh}`);

  // Embed CSS. Deliberately reads the export-only checkboxes/mode, not
  // the live showEdgeLabels/nameMode — independent by design (see
  // exportOptLabels' declaration comment). Same shape as the live view's
  // body.namemode-* rules, just scoped to this export's own <style> since
  // there's no <body> in an exported SVG to hang a class off of.
  const styleEl=document.createElementNS(NS,'style');
  styleEl.textContent=EXPORT_CSS
    +(exportOptLabels?'':'.e-lbg,.e-ltxt{display:none}')
    +(exportOptRoots?'':'.n-root{display:none}')
    +(exportNameMode===1?'.n-logical,.n-paren{display:none}':
      exportNameMode===2?'.er-node.has-logical .n-physical,.er-node.has-logical .n-paren{display:none}':'');
  exportSvg.appendChild(styleEl);

  // Embed arrowhead markers
  const defsClone=document.querySelector('#er-svg defs').cloneNode(true);
  exportSvg.appendChild(defsClone);

  // Embed diagram content (deep clone). The live er-main carries the screen
  // pan/zoom transform — strip it, the viewBox already frames the diagram.
  const mainClone=erMain.cloneNode(true);
  mainClone.removeAttribute('transform');
  // strip transient selection state — the shared image should be neutral.
  // Deliberately NOT stripping word-hit/word-dim/n-wordhit here — the
  // toolbar Highlight search is meant to survive into exports (the whole
  // point is pasting a highlighted diagram into a doc), unlike selection.
  mainClone.querySelectorAll('.sel,.hl').forEach(el=>el.classList.remove('sel','hl'));
  mainClone.querySelectorAll('path').forEach(p=>{
    ['marker-start','marker-end'].forEach(a=>{
      const v=p.getAttribute(a); if(v) p.setAttribute(a, v.replace('-hl)',')'));
    });
  });
  exportSvg.appendChild(mainClone);

  return {svg:exportSvg, vw, vh};
}

// Shared by every text-markup export (SVG source, Mermaid, PlantUML) — each
// format below is a small "build the text" function plus a copy/download
// pair built on these, the same copy-vs-download split PNG already has
// (exportToPNG copies, downloadPNGFile downloads). Copying still falls back
// to a download on failure (clipboard unsupported/denied) — same as PNG —
// but on success it does NOT also download, now that there's a dedicated
// button for that.
function downloadTextFile(text, filename, mimetype, successMsg){
  const url=URL.createObjectURL(new Blob([text],{type:`${mimetype};charset=utf-8`}));
  const a=document.createElement('a');
  a.href=url; a.download=filename; a.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
  showToast(successMsg);
}
function copyTextToClipboard(text, successMsg, onFail){
  if(navigator.clipboard?.writeText){
    navigator.clipboard.writeText(text).then(()=>showToast(successMsg)).catch(onFail);
  } else onFail();
}

function buildSVGText(){
  const built=buildExportSvg();
  return built ? new XMLSerializer().serializeToString(built.svg) : null;
}
function exportToSVG(){
  const text=buildSVGText();
  if(text==null) return;
  downloadTextFile(text, 'erd.svg', 'image/svg+xml', 'Downloaded erd.svg ✓');
}
function copySVGToClipboard(){
  const text=buildSVGText();
  if(text==null) return;
  copyTextToClipboard(text, 'Copied SVG markup ✓',
    ()=>downloadTextFile(text, 'erd.svg', 'image/svg+xml', 'Downloaded erd.svg ✓'));
}

// Mermaid erDiagram markup (paste straight into READMEs / PRs)
function buildMermaidText(){
  const tables=getDisplayTables();
  if(!tables.length){showToast('No tables are displayed');return null;}
  const lines=['erDiagram'];
  getDisplayEdges(tables).forEach(e=>{
    const card=edgeCard(e);
    let a=e.source, b=e.target, rel;
    if(card.kind==='nn'){ rel='}o--o{'; }
    else if(card.kind==='11'){ rel='||--||'; }
    else { rel='||--o{'; if(card.many===e.source){ a=e.target; b=e.source; } }
    const label=(e.assocs[0]?.name||'').replace(/"/g,"'");
    lines.push(`    ${a} ${rel} ${b} : "${label}"`);
  });
  tables.forEach(t=>{
    lines.push(`    ${t} {`);
    (DATA.tables[t]?.columns||[]).forEach(c=>{
      if(!/^\w+$/.test(c.name)) return; // skip pseudo-columns from expression indexes
      const key=c.primary?' PK':isFkCol(t, c.name)?' FK':'';
      lines.push(`        ${c.type||'string'} ${c.name}${key}`);
    });
    lines.push('    }');
  });
  return lines.join('\n');
}
function exportToMermaid(){
  const text=buildMermaidText();
  if(text==null) return;
  copyTextToClipboard(text, `Copied Mermaid markup ✓ (${getDisplayTables().length} tables)`,
    ()=>downloadTextFile(text, 'erd.mmd', 'text/plain', 'Downloaded erd.mmd ✓'));
}
function downloadMermaidFile(){
  const text=buildMermaidText();
  if(text==null) return;
  downloadTextFile(text, 'erd.mmd', 'text/plain', 'Downloaded erd.mmd ✓');
}

// PlantUML entity-relationship markup (paste into any PlantUML renderer).
// Same shape as buildMermaidText above — edgeCard()'s three kinds map 1:1
// onto PlantUML's own crow's-foot tokens, so the branch-and-swap logic is
// identical; only the token spellings and entity-block syntax differ.
function buildPlantUMLText(){
  const tables=getDisplayTables();
  if(!tables.length){showToast('No tables are displayed');return null;}
  const lines=['@startuml','hide circle','skinparam linetype ortho',''];
  // PlantUML identifiers (used both as the entity's own alias and in every
  // relationship line referencing it) must themselves be \w+ — a table
  // name that already fails that test can't just declare itself as its
  // own alias (still broken), and relationship lines must use the SAME
  // sanitized identifier, not the raw name, or they refer to an entity
  // that was never declared
  const aliasOf=t=>/^\w+$/.test(t)?t:t.replace(/\W/g,'_');
  tables.forEach(t=>{
    const lg=logicalName(t).replace(/"/g,"'"); // comments are free text; " would break the quoted display name
    const alias=aliasOf(t);
    const needsAlias=lg || alias!==t;
    lines.push(needsAlias ? `entity "${t}${lg?`（${lg}）`:''}" as ${alias} {` : `entity ${alias} {`);
    const cols=(DATA.tables[t]?.columns||[]).filter(c=>/^\w+$/.test(c.name)); // skip pseudo-columns from expression indexes
    const pk=cols.filter(c=>c.primary), rest=cols.filter(c=>!c.primary);
    pk.forEach(c=>lines.push(`  * ${c.name} : ${c.type||'string'} <<PK>>`));
    if(pk.length && rest.length) lines.push('  --');
    rest.forEach(c=>{
      const mark=c.nullable?'':'* ';
      const fk=isFkCol(t, c.name)?' <<FK>>':'';
      lines.push(`  ${mark}${c.name} : ${c.type||'string'}${fk}`);
    });
    lines.push('}', '');
  });
  getDisplayEdges(tables).forEach(e=>{
    const card=edgeCard(e);
    let a=e.source, b=e.target, rel;
    if(card.kind==='nn'){ rel='}o--o{'; }
    else if(card.kind==='11'){ rel='||--||'; }
    else { rel='||--o{'; if(card.many===e.source){ a=e.target; b=e.source; } }
    const label=(e.assocs[0]?.name||'').replace(/"/g,"'");
    lines.push(label ? `${aliasOf(a)} ${rel} ${aliasOf(b)} : ${label}` : `${aliasOf(a)} ${rel} ${aliasOf(b)}`);
  });
  lines.push('@enduml');
  return lines.join('\n');
}
function exportToPlantUML(){
  const text=buildPlantUMLText();
  if(text==null) return;
  copyTextToClipboard(text, `Copied PlantUML markup ✓ (${getDisplayTables().length} tables)`,
    ()=>downloadTextFile(text, 'erd.puml', 'text/plain', 'Downloaded erd.puml ✓'));
}
function downloadPlantUMLFile(){
  const text=buildPlantUMLText();
  if(text==null) return;
  downloadTextFile(text, 'erd.puml', 'text/plain', 'Downloaded erd.puml ✓');
}

// Rasterizes the current diagram to a PNG Blob (or null on failure, having
// already shown the relevant toast) — shared by the clipboard-copy and
// explicit-download export actions below.
async function rasterizePNGBlob(){
  const built=buildExportSvg();
  if(!built) return null;
  // browsers cap canvas dimensions (commonly ~16384px/side, tighter on
  // Safari/mobile) — an oversized canvas makes toBlob() silently yield
  // null with no error of its own. Scale down from the ideal 2x rather
  // than fail outright on a large diagram; MAX_DIM is a conservative
  // floor that should be safe everywhere.
  const MAX_DIM=8000;
  const scale=Math.max(0.1, Math.min(2, MAX_DIM/built.vw, MAX_DIM/built.vh));
  const W=Math.ceil(built.vw*scale), H=Math.ceil(built.vh*scale);
  built.svg.setAttribute('width',W); built.svg.setAttribute('height',H);
  const svgStr=new XMLSerializer().serializeToString(built.svg);
  const blob=new Blob([svgStr],{type:'image/svg+xml;charset=utf-8'});
  const url=URL.createObjectURL(blob);

  return new Promise(resolve=>{
    const img=new Image();
    img.onload=()=>{
      try{
        const canvas=document.createElement('canvas');
        canvas.width=W; canvas.height=H;
        const ctx=canvas.getContext('2d');
        ctx.drawImage(img,0,0,W,H); // background stays transparent
        URL.revokeObjectURL(url);
        canvas.toBlob(pngBlob=>{
          if(!pngBlob) showToast('Export failed: diagram too large to rasterize — try SVG export instead');
          resolve(pngBlob);
        },'image/png');
      }catch(err){
        URL.revokeObjectURL(url);
        showToast('Export failed: diagram too large to rasterize — try SVG export instead');
        resolve(null);
      }
    };
    img.onerror=()=>{URL.revokeObjectURL(url);showToast('Export failed');resolve(null);};
    img.src=url;
  });
}

// "PNG — copy to clipboard" menu action: clipboard is the primary intent,
// falling back to a download only when the browser can't write images to
// the clipboard at all (or the user denies the permission prompt) — not a
// substitute for the explicit "download file" action below, which a user
// who *wants* a file (not clipboard) should be able to reach directly
// rather than only by clipboard failing.
async function exportToPNG(){
  const pngBlob=await rasterizePNGBlob();
  if(!pngBlob) return;
  const pngUrl=URL.createObjectURL(pngBlob);
  if(navigator.clipboard?.write){
    navigator.clipboard.write([new ClipboardItem({'image/png':pngBlob})])
      .then(()=>showToast('Copied to clipboard ✓'))
      .catch(()=>downloadPNG(pngUrl));
  } else { downloadPNG(pngUrl); }
}

async function downloadPNGFile(){
  const pngBlob=await rasterizePNGBlob();
  if(!pngBlob) return;
  downloadPNG(URL.createObjectURL(pngBlob));
}

function downloadPNG(url){
  const a=document.createElement('a');
  a.href=url; a.download='erd.png'; a.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
  showToast('Downloaded erd.png ✓');
}

// ── Pan / zoom ─────────────────────────────────────────────────────────────
// ── Drag snapping ──────────────────────────────────────────────────────────
// While dragging, the node's left/center/right (and top/center/bottom) snap
// to the matching lines of other visible nodes, with a Figma-style guide
// line at the snapped coordinate. Threshold is in screen px so zoom doesn't
// change the feel. Hold Alt to disable.
const SNAP_PX=6;
function snapToNodes(name, x, y, guides, exclude){
  exclude = exclude || new Set([name]);
  const sz=nodeSize[name]||{w:160,h:100};
  const th=SNAP_PX/vs;
  const myX=[x-sz.w/2, x, x+sz.w/2], myY=[y-sz.h/2, y, y+sz.h/2];
  let dx=th, dy=th, bx=null, by=null, gx=0, gy=0, tx=null, ty=null;
  getDisplayTables().forEach(t=>{
    if(exclude.has(t)) return;
    const p=nodePos[t]; if(!p) return;
    const s=nodeSize[t]||{w:160,h:100};
    const cxs=[p.x-s.w/2, p.x, p.x+s.w/2], cys=[p.y-s.h/2, p.y, p.y+s.h/2];
    for(const c of cxs) for(const m of myX){
      const d=Math.abs(c-m);
      if(d<dx){ dx=d; bx=x+(c-m); gx=c; tx=t; }
    }
    for(const c of cys) for(const m of myY){
      const d=Math.abs(c-m);
      if(d<dy){ dy=d; by=y+(c-m); gy=c; ty=t; }
    }
  });
  if(bx!==null) guides.push({dir:'v', at:gx, a:name, b:tx});
  if(by!==null) guides.push({dir:'h', at:gy, a:name, b:ty});
  return {x:bx??x, y:by??y};
}

function drawSnapGuides(guides){
  let gl=document.getElementById('guide-layer');
  if(!guides.length){ if(gl) gl.innerHTML=''; return; }
  if(!gl){ gl=svgEl('g',{id:'guide-layer'}); erMain.appendChild(gl); }
  gl.innerHTML='';
  const box=n=>{
    const p=nodePos[n], s=nodeSize[n]||{w:160,h:100};
    return p ? {x0:p.x-s.w/2, y0:p.y-s.h/2, x1:p.x+s.w/2, y1:p.y+s.h/2} : null;
  };
  guides.forEach(g=>{
    const A=box(g.a), B=box(g.b);
    if(!A||!B) return;
    const at=g.dir==='v'
      ? {x1:g.at, x2:g.at, y1:Math.min(A.y0,B.y0)-20, y2:Math.max(A.y1,B.y1)+20}
      : {y1:g.at, y2:g.at, x1:Math.min(A.x0,B.x0)-20, x2:Math.max(A.x1,B.x1)+20};
    gl.appendChild(svgEl('line', {...at, class:'snap-guide'}));
  });
}

function drawMarquee(a,b){
  let gl=document.getElementById('marquee-layer');
  if(!a||!b){ if(gl) gl.innerHTML=''; return; }
  if(!gl){ gl=svgEl('g',{id:'marquee-layer'}); erMain.appendChild(gl); }
  gl.innerHTML='';
  const x=Math.min(a.x,b.x), y=Math.min(a.y,b.y), w=Math.abs(b.x-a.x), h=Math.abs(b.y-a.y);
  gl.appendChild(svgEl('rect',{x,y,width:w,height:h,class:'marquee-rect'}));
}

svg.addEventListener('mousedown', e=>{
  if(e.button!==0||isDragging) return;
  // shift-drag on empty canvas = rubber-band select, same modifier as
  // shift-click's "add to selection" on a node — plain drag still pans
  if(e.shiftKey){
    isMarqueeSelecting=true;
    marqueeStart=svgPt(e.clientX,e.clientY);
    marqueeStartCX=e.clientX; marqueeStartCY=e.clientY;
    return;
  }
  isPanning=true; panSX=e.clientX; panSY=e.clientY; panVX=vx; panVY=vy;
  svg.classList.add('panning');
});
window.addEventListener('mousemove', e=>{
  if(isDragging&&dragName){
    // sub-3px jitter is a click, not a drag
    if(!dragMoved && Math.hypot(e.clientX-dragCX, e.clientY-dragCY) < 3) return;
    const pt=svgPt(e.clientX,e.clientY);
    let nx=pt.x-dragOX, ny=pt.y-dragOY;
    const guides=[];
    // a whole-group drag (§5.4) has no single "anchor" node to snap against
    // — same no-snap treatment as holding Alt during an ordinary node drag
    if(!e.altKey && !dragIsGroup){
      const sp=snapToNodes(dragName, nx, ny, guides, dragSet);
      nx=sp.x; ny=sp.y;
    }
    drawSnapGuides(guides);
    const start=dragGroupStart[dragName]||{x:nx,y:ny};
    const ddx=nx-start.x, ddy=ny-start.y;
    dragSet.forEach(t=>{
      const s0=dragGroupStart[t]; if(!s0) return;
      nodePos[t]={x:s0.x+ddx, y:s0.y+ddy};
    });
    dragMoved=true;
    dragSet.forEach(t=>{
      document.querySelectorAll(`.er-node[data-name="${CSS.escape(t)}"]`).forEach(el=>{
        const sz=nodeSize[t]||{w:160,h:100};
        const p=nodePos[t];
        el.setAttribute('transform',`translate(${p.x-sz.w/2},${p.y-sz.h/2})`);
      });
    });
    const eL=document.getElementById('edge-layer');
    // only re-route edges whose endpoint actually moved — the rest of the
    // diagram's edges are untouched by this drag, and re-running pickBend's
    // obstacle search for all of them on every mousemove is the main cost
    // of dragging on a diagram with many edges. A full, guaranteed-correct
    // redraw still happens once on mouseup.
    if(eL && dragEdgeCache){
      dragEdgeCache.forEach(e2=>{
        if(!dragSet.has(e2.source) && !dragSet.has(e2.target)) return;
        eL.querySelectorAll(`[data-source="${CSS.escape(e2.source)}"][data-target="${CSS.escape(e2.target)}"]`)
          .forEach(el=>el.remove());
        drawEdge(eL, e2);
      });
      updateEdgeHighlight();
    }
    updateGroupFrames(); // group frames track their members live, same as edges above
    return;
  }
  if(isMarqueeSelecting){
    drawMarquee(marqueeStart, svgPt(e.clientX,e.clientY));
    return;
  }
  if(!isPanning) return;
  vx=panVX+(e.clientX-panSX); vy=panVY+(e.clientY-panSY);
  setTransform();
});
window.addEventListener('mouseup', e=>{
  if(isDragging){
    isDragging=false; dragName=null;
    if(dragMoved && dragUndoSnapshot){
      undoStack.push(dragUndoSnapshot);
      if(undoStack.length>UNDO_LIMIT) undoStack.shift();
      redoStack=[];
      updateUndoRedoUI();
    }
    dragUndoSnapshot=null;
    dragSet=new Set(); dragGroupStart={}; dragEdgeCache=null; dragIsGroup=false;
    // dragMoved stays set: the click event fires after mouseup and must see it.
    // If no click follows (released outside the node/window), clear it on the
    // next task so it can't swallow a later legitimate click.
    setTimeout(()=>{ dragMoved=false; }, 0);
    svg.classList.remove('node-drag');
    drawSnapGuides([]);
    const eL=document.getElementById('edge-layer');
    if(eL){eL.innerHTML='';edgeObstacles=getDisplayTables();getDisplayEdges(edgeObstacles).forEach(e2=>drawEdge(eL,e2));updateEdgeHighlight();}
    updateGroupFrames(); // full, guaranteed-correct redraw once, same as edges above
    return;
  }
  if(isMarqueeSelecting){
    isMarqueeSelecting=false;
    const cur=svgPt(e.clientX,e.clientY);
    const x0=Math.min(marqueeStart.x,cur.x), x1=Math.max(marqueeStart.x,cur.x);
    const y0=Math.min(marqueeStart.y,cur.y), y1=Math.max(marqueeStart.y,cur.y);
    // require an actual drag (not just a shift-click) before touching the
    // selection — same 3px-of-client-pixels threshold dragMoved uses
    // (checking world-space extent instead would make the threshold
    // zoom-dependent: too twitchy zoomed out, too strict zoomed in)
    if(Math.hypot(e.clientX-marqueeStartCX, e.clientY-marqueeStartCY)>=3){
      getDisplayTables().forEach(t=>{
        const p=nodePos[t], s=nodeSize[t]||calcSize(t);
        if(!p) return;
        const nx0=p.x-s.w/2, nx1=p.x+s.w/2, ny0=p.y-s.h/2, ny1=p.y+s.h/2;
        if(nx1>=x0 && nx0<=x1 && ny1>=y0 && ny0<=y1){ // intersects the marquee
          selectedTables.add(t);
          selectionAnchor=t;
        }
      });
      refreshSelectionUI();
      marqueeJustSelected=true;
      setTimeout(()=>{ marqueeJustSelected=false; }, 0);
    }
    marqueeStart=null;
    drawMarquee(null,null);
    return;
  }
  if(isPanning){isPanning=false;svg.classList.remove('panning');}
});

// Trackpad: two-finger scroll = pan, pinch / Ctrl+scroll = zoom
svg.addEventListener('wheel', e=>{
  e.preventDefault();
  if(e.ctrlKey||e.metaKey){
    const r=svg.getBoundingClientRect();
    const mx=e.clientX-r.left, my=e.clientY-r.top;
    const factor=e.deltaY>0?.88:1.14;
    const nv=Math.max(.06,Math.min(6,vs*factor));
    vx=mx-(mx-vx)*(nv/vs); vy=my-(my-vy)*(nv/vs); vs=nv;
  } else {
    const s=e.deltaMode===1?20:1;
    vx-=e.deltaX*s; vy-=e.deltaY*s;
  }
  setTransform();
},{passive:false});

svg.addEventListener('click', e=>{
  if(marqueeJustSelected){ marqueeJustSelected=false; return; } // end of a marquee, not a click
  // shift is "additive" everywhere else (shift-click a node adds it rather
  // than replacing the selection) — a stray shift-click on empty canvas
  // (below the marquee's drag threshold) should be a no-op the same way,
  // not a destructive clear
  if(e.shiftKey) return;
  if(e.target===svg||e.target===erMain) selectOnly(null);
});

// Escape: open toolbar menu (export/help) → search → highlight → focus → selection, in that
// order. Only clears the highlight box when it's actually focused
// (matching the left-pane search's own behavior) — Esc with the canvas
// focused keeps meaning "exit focus / deselect", not a surprise
// highlight-clear; the ✕ button is there for that.
window.addEventListener('keydown', e=>{
  if(e.key!=='Escape') return;
  if(document.getElementById('export-menu').classList.contains('open')){ closeExportMenu(); return; }
  if(document.getElementById('help-menu').classList.contains('open')){ closeHelpMenu(); return; }
  const sb=document.getElementById('search');
  if(document.activeElement===sb && sb.value){ sb.value=''; renderTableList(); return; }
  const wb=document.getElementById('word-search');
  if(document.activeElement===wb && wb.value){ wb.value=''; wordQuery=''; updateWordHighlight(); return; }
  if(focusedTable){ clearFocus(); return; }
  if(selectedTables.size) selectOnly(null);
});

// Layout undo/redo: Ctrl/Cmd+Z, Ctrl/Cmd+Shift+Z (or Ctrl+Y). Skipped while
// typing in a text field so it doesn't fight the browser's native undo there.
window.addEventListener('keydown', e=>{
  if(!(e.ctrlKey||e.metaKey)) return;
  const tag=document.activeElement?.tagName;
  if(tag==='INPUT'||tag==='TEXTAREA') return;
  if(e.key==='z'||e.key==='Z'){
    e.preventDefault();
    e.shiftKey ? doRedo() : doUndo();
  } else if(e.key==='y'||e.key==='Y'){
    e.preventDefault();
    doRedo();
  }
});

// ── Toolbar buttons ─────────────────────────────────────────────────────────
function applyZoom(f){
  const r=svg.getBoundingClientRect();
  const mx=r.width/2, my=r.height/2;
  const nv=Math.max(.06,Math.min(6,vs*f));
  vx=mx-(mx-vx)*(nv/vs); vy=my-(my-vy)*(nv/vs); vs=nv;
  setTransform();
}
document.getElementById('btn-zoom-in') .addEventListener('click',()=>applyZoom(1.25));
document.getElementById('btn-zoom-out').addEventListener('click',()=>applyZoom(.8));
document.getElementById('btn-zoom-100').addEventListener('click',()=>{
  // 100% zoom (text at natural size), keeping the current view center
  const r=svg.getBoundingClientRect();
  const cx=(r.width/2-vx)/vs, cy=(r.height/2-vy)/vs;
  vs=1; vx=r.width/2-cx; vy=r.height/2-cy;
  setTransform();
});
document.getElementById('btn-fit')     .addEventListener('click',fitView);
document.getElementById('btn-undo')    .addEventListener('click',doUndo);
document.getElementById('btn-redo')    .addEventListener('click',doRedo);
document.getElementById('btn-reset')   .addEventListener('click',()=>{
  pushUndoSnapshot();
  const ts=getDisplayTables();
  ts.forEach(t=>delete nodePos[t]);
  Object.keys(ringDepth).forEach(k=>delete ringDepth[k]);
  // overview: shelf-packed rows via gridLayout;
  // focus view: layoutAll re-runs the (elliptical) hub-spoke
  if(!focusedTable){
    ts.forEach(n=>{ nodeSize[n]=calcSize(n); });
    gridLayout(ts);
  }
  refreshView();
});
// PNG/SVG/Mermaid used to be three separate always-visible buttons; now
// tucked behind one "Export" toggle since they're each used ~once per
// session, unlike the always-visible zoom/layout controls
function closeExportMenu(){ document.getElementById('export-menu').classList.remove('open'); }
document.getElementById('btn-export-toggle').addEventListener('click', e=>{
  e.stopPropagation();
  document.getElementById('export-menu').classList.toggle('open');
});
document.addEventListener('click', e=>{
  if(!document.getElementById('export-group').contains(e.target)) closeExportMenu();
});
function closeHelpMenu(){ document.getElementById('help-menu').classList.remove('open'); }
document.getElementById('btn-help').addEventListener('click', e=>{
  e.stopPropagation();
  document.getElementById('help-menu').classList.toggle('open');
});
document.addEventListener('click', e=>{
  if(!document.getElementById('help-group').contains(e.target)) closeHelpMenu();
});
document.getElementById('export-opt-labels').addEventListener('change', e=>{
  exportOptLabels=e.target.checked; saveState();
});
document.getElementById('export-opt-roots').addEventListener('change', e=>{
  exportOptRoots=e.target.checked; saveState();
});
function updateExportNameModeUI(){
  document.querySelectorAll('#export-namemode-group .diag-btn').forEach(b=>
    b.classList.toggle('active', parseInt(b.dataset.xnm,10)===exportNameMode));
}
document.querySelectorAll('#export-namemode-group .diag-btn').forEach(btn=>{
  btn.addEventListener('click', e=>{
    e.stopPropagation(); // stay inside the still-open popup, like the checkboxes above
    exportNameMode=parseInt(btn.dataset.xnm,10);
    saveState(); updateExportNameModeUI();
  });
});
document.getElementById('btn-export').addEventListener('click', ()=>{ exportToPNG(); closeExportMenu(); });
document.getElementById('btn-export-download').addEventListener('click', ()=>{ downloadPNGFile(); closeExportMenu(); });
document.getElementById('btn-export-svg-copy').addEventListener('click', ()=>{ copySVGToClipboard(); closeExportMenu(); });
document.getElementById('btn-export-svg').addEventListener('click', ()=>{ exportToSVG(); closeExportMenu(); });
document.getElementById('btn-export-mmd').addEventListener('click', ()=>{ exportToMermaid(); closeExportMenu(); });
document.getElementById('btn-export-mmd-download').addEventListener('click', ()=>{ downloadMermaidFile(); closeExportMenu(); });
document.getElementById('btn-export-puml').addEventListener('click', ()=>{ exportToPlantUML(); closeExportMenu(); });
document.getElementById('btn-export-puml-download').addEventListener('click', ()=>{ downloadPlantUMLFile(); closeExportMenu(); });
document.getElementById('btn-dark').addEventListener('click',()=>{
  const on=!document.body.classList.contains('dark');
  document.body.classList.toggle('dark', on);
  setLS(LS('dk'), String(on));
});
document.getElementById('btn-autolayout').addEventListener('click',()=>{
  autoLayout=!autoLayout;
  document.getElementById('btn-autolayout').classList.toggle('active',autoLayout);
document.body.classList.toggle('dark', localStorage.getItem(LS('dk'))==='true');
  saveState();
  if(autoLayout) refreshView(); // apply immediately
  showToast(autoLayout?'Auto-tidy ON — the layout follows display changes':'Auto-tidy OFF');
});
document.getElementById('btn-all').addEventListener('click',()=>{
  excludedTables.clear(); saveState();
  refreshView(); renderTableList();
});
document.getElementById('btn-none').addEventListener('click',()=>{
  allTables().forEach(t=>excludedTables.add(t));
  if(focusedTable){ focusedTable=null; exitFocusMode(); updateDepthCtrl(); updateFocusUI(); }
  selectedTables=new Set(); selectionAnchor=null;
  saveState(); refreshView(); renderTableList(); showDetails();
});
document.getElementById('btn-unfocus').addEventListener('click',clearFocus);
document.getElementById('focus-bar-close').addEventListener('click',clearFocus);
// align the overview checkboxes with what the focus view is showing, then exit
document.getElementById('focus-bar-apply').addEventListener('click',()=>{
  if(!focusedTable) return;
  const shown=new Set(getDisplayTables());
  allTables().forEach(t=>{ if(shown.has(t)) excludedTables.delete(t); else excludedTables.add(t); });
  saveState();
  clearFocus();
  showToast('Applied the focused view to the checkboxes');
});

// depth control is relevant while focused or overview auto-expand is active
function updateDepthCtrl(){
  document.getElementById('depth-ctrl').className=(autoExpand||focusedTable)?'visible':'';
}

// dialog-like focus bar + related styling
function updateFocusUI(){
  document.body.classList.toggle('focus-mode', !!focusedTable);
  if(focusedTable){
    const d=expandDepth===0?'∞':expandDepth;
    const dir=expandDir==='both'?'both':expandDir==='out'?'deps':'dependents';
    document.getElementById('focus-bar-label').textContent=`🔍 Focused: ${focusedTable} (depth ${d}, ${dir})`;
  }
}

// Auto-expand toggle — expands the overview from all checked tables
// (focus always expands regardless of this toggle)
document.getElementById('auto-expand').addEventListener('change', e=>{
  autoExpand=e.target.checked;
  document.getElementById('ae-label').className=autoExpand?'ae-on':'';
  updateDepthCtrl();
  saveState();
  if(focusedTable) return; // the focus view is not driven by this toggle
  refreshView(); renderTableList();
});

// Depth buttons
document.querySelectorAll('.dep-btn:not(.dir-btn)').forEach(btn=>{
  btn.addEventListener('click',()=>{
    expandDepth=parseInt(btn.dataset.d, 10);
    document.querySelectorAll('.dep-btn:not(.dir-btn)').forEach(b=>b.classList.toggle('active',b===btn));
    saveState();
    if(focusedTable) switchFocusTable();
    refreshView(); renderTableList();
    updateFocusUI();
  });
});

// Dependency direction buttons (both / deps / dependents)
document.querySelectorAll('.dir-btn').forEach(btn=>{
  btn.addEventListener('click',()=>{
    expandDir=btn.dataset.dir;
    document.querySelectorAll('.dir-btn').forEach(b=>b.classList.toggle('active',b===btn));
    saveState();
    if(focusedTable) switchFocusTable();
    refreshView(); renderTableList();
    updateFocusUI();
  });
});

// Column display mode (segmented buttons, whole diagram)
function updateColModeUI(){
  document.querySelectorAll('#colmode-group .diag-btn').forEach(b=>
    b.classList.toggle('active', parseInt(b.dataset.cm,10)===colMode));
}
document.querySelectorAll('#colmode-group .diag-btn').forEach(btn=>{
  btn.addEventListener('click',()=>{
    const m=parseInt(btn.dataset.cm,10);
    if(m===colMode && Object.keys(colOverride).length===0) return;
    colMode=m; colOverride={}; // global change resets per-table overrides
    saveState(); updateColModeUI();
    Object.keys(nodeSize).forEach(k=>delete nodeSize[k]);
    // Auto-tidy ON: node sizes change drastically, so re-layout.
    // OFF: nodes resize in place — the display set didn't change, so
    // positions (incl. manual arrangement) are kept.
    if(autoLayout){
      pushUndoSnapshot();
      Object.keys(nodePos).forEach(k=>delete nodePos[k]);
      Object.keys(basePos).forEach(k=>delete basePos[k]);
      Object.keys(ringDepth).forEach(k=>delete ringDepth[k]);
    }
    refreshView();
  });
});

// Physical/logical name display mode (segmented buttons, whole diagram) —
// same node-size-invalidation shape as colMode above, since the header
// text width changes with the mode. The body class is what actually
// hides/shows the pre-rendered tspans (see the CSS + drawNode comments);
// updateNameModeUI() below just keeps the buttons' active state in sync.
function updateNameModeUI(){
  document.querySelectorAll('#namemode-group .diag-btn').forEach(b=>
    b.classList.toggle('active', parseInt(b.dataset.nm,10)===nameMode));
  document.body.classList.remove('namemode-physical','namemode-logical');
  if(nameMode===1) document.body.classList.add('namemode-physical');
  if(nameMode===2) document.body.classList.add('namemode-logical');
}
document.querySelectorAll('#namemode-group .diag-btn').forEach(btn=>{
  btn.addEventListener('click',()=>{
    const m=parseInt(btn.dataset.nm,10);
    if(m===nameMode) return;
    nameMode=m;
    saveState(); updateNameModeUI();
    Object.keys(nodeSize).forEach(k=>delete nodeSize[k]);
    if(autoLayout){
      pushUndoSnapshot();
      Object.keys(nodePos).forEach(k=>delete nodePos[k]);
      Object.keys(basePos).forEach(k=>delete basePos[k]);
      Object.keys(ringDepth).forEach(k=>delete ringDepth[k]);
    }
    refreshView();
  });
});

// Max visible rows per table
document.getElementById('max-rows').addEventListener('change', e=>{
  maxRows=parseInt(e.target.value,10)||15;
  setLS(LS('mr'), String(maxRows));
  Object.keys(nodeSize).forEach(k=>delete nodeSize[k]);
  // Auto-tidy ON: node heights change drastically — re-layout to keep the
  // no-overlap guarantee. OFF: resize in place, keep positions.
  if(autoLayout){
    pushUndoSnapshot();
    getDisplayTables().forEach(t=>delete nodePos[t]);
    if(focusedTable) switchFocusTable();
  }
  refreshView();
});

// Edge label (⇢ through-table names) visibility toggle
function updateLabelUI(){
  document.body.classList.toggle('no-edge-labels', !showEdgeLabels);
  document.getElementById('btn-labels').classList.toggle('active', showEdgeLabels);
}
document.getElementById('btn-labels').addEventListener('click',()=>{
  showEdgeLabels=!showEdgeLabels; saveState(); updateLabelUI();
});

// Group-frame visibility toggle (groups Phase 1). The button itself is
// hidden entirely when no groups are configured (init, below) — a config
// with no `groups:` must look exactly like it did before this feature
// existed, toolbar included.
function updateGroupsUI(){
  const btn=document.getElementById('btn-groups');
  if(!btn) return;
  btn.classList.toggle('active', showGroups);
}
document.getElementById('btn-groups').addEventListener('click',()=>{
  showGroups=!showGroups; saveState(); updateGroupsUI(); updateGroupFrames();
});

// ── Pane resize / collapse ──────────────────────────────────────────────────
(()=>{
  const lp=document.getElementById('left-pane'), rp=document.getElementById('right-pane');
  const lw=parseInt(localStorage.getItem(LS('lw')),10), rw=parseInt(localStorage.getItem(LS('rw')),10);
  if(lw) lp.style.width=lw+'px';
  if(rw) rp.style.width=rw+'px';

  function setCollapsed(pane, tabId, key, val){
    pane.classList.toggle('collapsed', val);
    document.getElementById(tabId).classList.toggle('visible', val);
    setLS(key, String(val));
  }
  setCollapsed(lp,'expand-left',LS('lc'),localStorage.getItem(LS('lc'))==='true');
  setCollapsed(rp,'expand-right',LS('rc'),localStorage.getItem(LS('rc'))==='true');

  document.getElementById('collapse-left') .addEventListener('click',()=>setCollapsed(lp,'expand-left',LS('lc'),true));
  document.getElementById('expand-left')   .addEventListener('click',()=>setCollapsed(lp,'expand-left',LS('lc'),false));
  document.getElementById('collapse-right').addEventListener('click',()=>setCollapsed(rp,'expand-right',LS('rc'),true));
  document.getElementById('expand-right')  .addEventListener('click',()=>setCollapsed(rp,'expand-right',LS('rc'),false));

  function setupDivider(divId, pane, key, dir){
    const div=document.getElementById(divId);
    let startX=0, startW=0, resizing=false;
    div.addEventListener('mousedown', e=>{
      if(pane.classList.contains('collapsed')) return;
      resizing=true; startX=e.clientX; startW=pane.getBoundingClientRect().width;
      div.classList.add('dragging');
      e.preventDefault();
    });
    window.addEventListener('mousemove', e=>{
      if(!resizing) return;
      pane.style.width=Math.max(140, Math.min(560, startW + dir*(e.clientX-startX)))+'px';
    });
    window.addEventListener('mouseup', ()=>{
      if(!resizing) return;
      resizing=false; div.classList.remove('dragging');
      setLS(key, String(Math.round(pane.getBoundingClientRect().width)));
    });
  }
  setupDivider('div-l', lp, LS('lw'),  1);
  setupDivider('div-r', rp, LS('rw'), -1);
})();

// ── Named views (save/restore the current view under a name) ─────────────
function loadViews(){ try{ return JSON.parse(localStorage.getItem(LS('views'))||'{}')||{}; }catch{ return {}; } }
function persistViews(v){ setLS(LS('views'), JSON.stringify(v)); }

function snapshotView(){
  const pos={};
  getDisplayTables().forEach(t=>{ const p=nodePos[t]; if(p) pos[t]={x:Math.round(p.x),y:Math.round(p.y)}; });
  return {excl:[...excludedTables], hid:[...hiddenTables],
          ae:autoExpand, dep:expandDepth, dir:expandDir, cm:colMode, pos};
}

function applyView(v){
  excludedTables=new Set(v.excl||[]); hiddenTables=new Set(v.hid||[]);
  autoExpand=!!v.ae; expandDepth=v.dep??1; expandDir=v.dir||'both';
  colMode=v.cm??0; colOverride={}; manualExpanded.clear();
  if(focusedTable){ focusedTable=null; selectedTables=new Set(); selectionAnchor=null; exitFocusMode(); updateFocusUI(); }
  Object.keys(nodePos).forEach(k=>delete nodePos[k]);
  Object.keys(basePos).forEach(k=>delete basePos[k]);
  Object.keys(nodeSize).forEach(k=>delete nodeSize[k]);
  Object.keys(ringDepth).forEach(k=>delete ringDepth[k]);
  Object.entries(v.pos||{}).forEach(([t,p])=>{ if(DATA.tables[t]) nodePos[t]={...p}; });
  clearUndoStacks(); // a saved view's nodePos is unrelated to whatever was being edited before
  saveState(); syncControlsUI();
  renderDiagram(); requestAnimationFrame(fitView);
  renderTableList(); updateHiddenBar(); updateDepthCtrl(); showDetails();
}

// reflect state variables into the topbar / toolbar controls
function syncControlsUI(){
  document.getElementById('auto-expand').checked=autoExpand;
  document.getElementById('ae-label').className=autoExpand?'ae-on':'';
  document.querySelectorAll('.dep-btn:not(.dir-btn)').forEach(b=>b.classList.toggle('active',parseInt(b.dataset.d,10)===expandDepth));
  document.querySelectorAll('.dir-btn').forEach(b=>b.classList.toggle('active',b.dataset.dir===expandDir));
  updateColModeUI(); updateDepthCtrl();
}

function refreshViewSel(){
  const sel=document.getElementById('view-sel');
  const cur=sel.value;
  sel.innerHTML='<option value="">Views...</option>'+
    Object.keys(loadViews()).sort().map(n=>`<option>${esc(n)}</option>`).join('');
  if([...sel.options].some(o=>o.value===cur)) sel.value=cur;
}
document.getElementById('view-save').addEventListener('click',()=>{
  const cur=document.getElementById('view-sel').value;
  const name=prompt('View name (existing name overwrites)', cur||'My view');
  if(!name) return;
  const views=loadViews();
  views[name]=snapshotView();
  persistViews(views); refreshViewSel();
  document.getElementById('view-sel').value=name;
  showToast(`Saved view "${name}"`);
});
document.getElementById('view-sel').addEventListener('change',e=>{
  const v=loadViews()[e.target.value];
  if(v){ applyView(v); showToast(`Applied view "${e.target.value}"`); }
});
document.getElementById('view-del').addEventListener('click',()=>{
  const sel=document.getElementById('view-sel');
  if(!sel.value) return;
  const views=loadViews();
  delete views[sel.value];
  persistViews(views);
  showToast(`Deleted view "${sel.value}"`);
  sel.value=''; refreshViewSel();
});

// Share link: current view state in the URL hash
document.getElementById('view-share').addEventListener('click',()=>{
  const hash='#v='+encodeURIComponent(JSON.stringify(snapshotView()));
  const full=location.href.split('#')[0]+hash;
  const fallback=()=>{ location.hash=hash.slice(1); showToast('State embedded in the URL (copy it from the address bar)'); };
  if(navigator.clipboard?.writeText){
    navigator.clipboard.writeText(full)
      .then(()=>showToast('Copied share link ✓'))
      .catch(fallback);
  } else fallback();
});

// Legend collapse (persisted)
(()=>{
  const lg=document.getElementById('legend');
  const setLg=v=>{
    lg.classList.toggle('collapsed', v);
    document.getElementById('legend-toggle').textContent=v?'▸':'▾';
    setLS(LS('lg'), String(v));
  };
  setLg(localStorage.getItem(LS('lg'))==='true');
  document.getElementById('legend-head').addEventListener('click',()=>setLg(!lg.classList.contains('collapsed')));
})();

// Aa / .* mode toggles — each button flips one boolean, refocuses its
// input (matching the existing clear-button behavior), and re-runs
// whichever render the box drives. sync() (reflecting persisted state
// into the button's visual .active state) is called separately from
// Init, below, once loadState() has actually run — binding this early
// (search boxes are wired well before the loadState() call at the
// bottom of this script) would otherwise always sync to each flag's
// pre-load default (false), never the restored value.
function bindSearchToggle(btnId, get, set, onChange){
  const btn=document.getElementById(btnId);
  const sync=()=>{ const on=get(); btn.classList.toggle('active',on); btn.setAttribute('aria-pressed',String(on)); };
  btn.addEventListener('click', ()=>{ set(!get()); sync(); saveState(); onChange(); });
  return sync;
}
const syncFsCase=bindSearchToggle('fs-case', ()=>filterCaseSensitive, v=>filterCaseSensitive=v,
  ()=>{ renderTableList(); document.getElementById('search').focus(); });
const syncFsRegex=bindSearchToggle('fs-regex', ()=>filterRegexMode, v=>filterRegexMode=v,
  ()=>{ renderTableList(); document.getElementById('search').focus(); });
const syncWsCase=bindSearchToggle('ws-case', ()=>wordCaseSensitive, v=>wordCaseSensitive=v,
  ()=>{ updateWordHighlight(); document.getElementById('word-search').focus(); });
const syncWsRegex=bindSearchToggle('ws-regex', ()=>wordRegexMode, v=>wordRegexMode=v,
  ()=>{ updateWordHighlight(); document.getElementById('word-search').focus(); });

// Search: filter the list; Enter jumps to the first match in the diagram
document.getElementById('search').addEventListener('input', e=>{
  if(!e.target.value && colHighlight){ const t=colHighlight.table; colHighlight=null; redrawNode(t); }
  renderTableList();
});
document.getElementById('search').addEventListener('keydown', e=>{
  if(e.key!=='Enter') return;
  const q=e.target.value;
  const matcher=makeMatcher(q, {regex:filterRegexMode, cs:filterCaseSensitive});
  if(!matcher) return;
  const colTest=t=>(DATA.tables[t]?.columns||[]).some(c=>matcher.test(c.name)||matcher.test(c.comment||''));
  // prefer a table whose match starts at position 0 — generalizes the old
  // "startsWith" preference (which has no regex analog) to "earliest match
  // begins at the very first character," identical behavior in substring mode
  const startsAtZero=t=>matcher.ranges(t)[0]?.[0]===0;
  const m=allTables().find(t=>startsAtZero(t)) || allTables().find(t=>matcher.test(t))
       || allTables().find(t=>colTest(t));
  if(!m) return;
  if(!hiddenTables.has(m) && !getDisplayTables().includes(m)) addTables([m]); // add hidden targets before locating
  locateTable(m);
  // column hit: make sure the column is visible and highlighted in the node
  const colHit=colTest(m);
  colHighlight=colHit?{table:m,match:matcher}:null;
  if(colHit){
    const colPred=c=>matcher.test(c.name)||matcher.test(c.comment||'');
    if(!visibleCols(m).some(colPred)) colOverride[m]=0; // reveal columns hidden by the mode
    const idx=visibleCols(m).findIndex(colPred);
    if(idx>=0) colScroll[m]=Math.max(0, Math.min(idx-2, Math.max(0, visibleCols(m).length-maxRows)));
    delete nodeSize[m];
    renderDiagram();
    flashNode(m);
  }
});

// ── Toolbar word-search (highlight, does not filter) ────────────────────────
let wordSearchDebounce=null;
document.getElementById('word-search').addEventListener('input', e=>{
  clearTimeout(wordSearchDebounce);
  const val=e.target.value;
  wordSearchDebounce=setTimeout(()=>{
    wordQuery=val.trim(); // NOT lowercased here — makeMatcher() needs the raw case to support case-sensitive mode
    updateWordHighlight();
  }, 150);
});
document.getElementById('word-search-clear').addEventListener('click', ()=>{
  const wb=document.getElementById('word-search');
  wb.value=''; wordQuery='';
  clearTimeout(wordSearchDebounce);
  updateWordHighlight();
  wb.focus();
});
document.getElementById('word-search').addEventListener('keydown', e=>{
  if(e.key!=='Enter') return;
  const matches=allTables().filter(wordHit);
  if(!matches.length) return;
  const dir=e.shiftKey?-1:1; // Enter = next match, Shift+Enter = previous
  wordMatchIdx=(wordMatchIdx+dir+matches.length)%matches.length;
  const m=matches[wordMatchIdx];
  if(!hiddenTables.has(m) && !getDisplayTables().includes(m)) addTables([m]); // add hidden targets before locating
  locateTable(m);
});

// ── Init ───────────────────────────────────────────────────────────────────
loadState();
document.getElementById('auto-expand').checked=autoExpand;
document.getElementById('ae-label').className=autoExpand?'ae-on':'';
updateDepthCtrl();
updateFocusUI();
updateColModeUI();
updateNameModeUI();
updateLabelUI();
// groups Phase 1: no groups configured -> hide the toggle entirely, keeping
// the toolbar byte-for-byte the same as before this feature for every
// pre-existing config/demo.
if(!GROUPS.length){
  const gbtn=document.getElementById('btn-groups');
  if(gbtn) gbtn.style.display='none';
} else {
  updateGroupsUI();
}
document.getElementById('btn-autolayout').classList.toggle('active',autoLayout);
document.getElementById('export-opt-labels').checked=exportOptLabels;
document.getElementById('export-opt-roots').checked=exportOptRoots;
updateExportNameModeUI();
syncFsCase(); syncFsRegex(); syncWsCase(); syncWsRegex();
document.body.classList.toggle('dark', localStorage.getItem(LS('dk'))==='true');
(()=>{ // max-rows selector: reflect current value, adding it if non-standard
  const sel=document.getElementById('max-rows');
  if(![...sel.options].some(o=>parseInt(o.value,10)===maxRows)){
    const o=document.createElement('option');
    o.value=String(maxRows); o.textContent=`${maxRows} rows`;
    sel.insertBefore(o, sel.options[0]);
  }
  sel.value=String(maxRows);
})();
document.querySelectorAll('.dep-btn:not(.dir-btn)').forEach(b=>b.classList.toggle('active',parseInt(b.dataset.d,10)===expandDepth));
document.querySelectorAll('.dir-btn').forEach(b=>b.classList.toggle('active',b.dataset.dir===expandDir));
updateHiddenBar();
refreshViewSel();
if(location.hash.startsWith('#v=')){
  try{
    applyView(JSON.parse(decodeURIComponent(location.hash.slice(3))));
    showToast('Applied the shared view');
  }catch(e){ console.warn('Failed to load the shared view:', e); }
}
renderGlobalNotes();
renderTableList();
renderDiagram();
requestAnimationFrame(fitView);
</script>
</body>
</html>
"""

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
    unknown = (set(config) - set(CONFIG_DEFAULTS) - {'relations', 'adapters', 'sources', 'version', 'notes', 'groups'}
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
    # version: purely a documented marker for the config *shape* users are
    # shown (e.g. in erdscope.example.yml) — no runtime behavior hangs off it
    # yet, so the only valid value is the literal int 1. Same bool-vs-int
    # discipline as above: `version: true` must not slip through as 1.
    if 'version' in config:
        v = config['version']
        if not (isinstance(v, int) and not isinstance(v, bool) and v == 1):
            sys.exit(f'Error: {path} `version` must be 1 (the only supported '
                     f'config version), got {v!r}')
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
    # adapters: a single path (str) or a list of paths (str) — custom DB
    # adapter plugin files, same str-or-list shape as `models`
    if 'adapters' in config:
        a = config['adapters']
        if isinstance(a, str):
            pass
        elif isinstance(a, list):
            for i, item in enumerate(a):
                if not isinstance(item, str):
                    sys.exit(f'Error: {path} `adapters[{i}]` must be a string, got {item!r}')
        else:
            sys.exit(f'Error: {path} `adapters` must be a string or a list of strings, '
                     f'got {a!r}')
    if 'table_map' in config and any(not isinstance(v, str) for v in config['table_map'].values()):
        sys.exit(f'Error: {path} `table_map` values must all be strings')
    if 'relations' in config and any(not isinstance(r, dict) for r in config['relations']):
        sys.exit(f'Error: {path} `relations` must be a list of objects '
                 '({table, column, references, ...})')
    if 'tables' in config:
        _check_config_tables(config['tables'], path)
    if 'sources' in config:
        _check_config_sources(config['sources'], path)
    if 'notes' in config:
        _check_config_notes(config['notes'], path)
    if 'groups' in config:
        _check_config_groups(config['groups'], path)

# ---------------------------------------------------------------------------
# `sources:` — typed code-source declarations (D5). Purely syntactic here:
# shape, required fields, allow-listed keys, Config-internal duplicate `id`.
# Whether `type` names a REGISTERED source type is a dispatch-time (sources.py
# run_input_specs) concern, not a load-time one — an --adapter plugin loaded
# later in the pipeline can still register its own overlay/type in time.
# ---------------------------------------------------------------------------
_CONFIG_SOURCE_KEYS = {'id', 'type', 'path', 'allow_empty'}

def _check_config_sources(sources, path):
    if not isinstance(sources, list):
        sys.exit(f'Error: {path} `sources` must be a list of objects '
                 '({id, type, path})')
    seen = set()
    for i, s in enumerate(sources):
        sw = f'sources[{i}]'
        if not isinstance(s, dict):
            sys.exit(f'Error: {path} `{sw}` must be an object')
        _reject_unknown_keys(s, _CONFIG_SOURCE_KEYS, path, sw)
        for key in ('id', 'type', 'path'):
            val = s.get(key)
            if not isinstance(val, str) or not val:
                sys.exit(f'Error: {path} `{sw}` needs a non-empty string `{key}`')
        _check_bool(s.get('allow_empty'), 'allow_empty' in s, path, f'{sw}.allow_empty')
        if s['id'] in seen:
            sys.exit(f'Error: {path} `sources` has a duplicate id {s["id"]!r}')
        seen.add(s['id'])

# ---------------------------------------------------------------------------
# `notes:` — design-documentation sidecar (notes Phase 1). Purely syntactic
# here: shape, required fields, allow-listed keys, Config-internal duplicate
# `id`, and link URL scheme (http/https only — the first line of XSS defense,
# since a note's links render as real <a href> in the viewer). Whether the
# note's TARGET actually exists (a table/relation naming something real) is a
# semantic, final-IR-after-merge concern — see resolve_and_validate_notes in
# providers.py, not here. Mirrors the tables §6.4①/② two-stage split.
# ---------------------------------------------------------------------------
_CONFIG_NOTE_KEYS = {'id', 'target', 'title', 'text', 'links'}
_CONFIG_NOTE_LINK_KEYS = {'label', 'url'}
_CONFIG_NOTE_TARGET_TYPES = {'global', 'table', 'relation'}
_CONFIG_NOTE_TARGET_KEYS = {
    'global': {'type'},
    'table': {'type', 'table'},
    # NOTE: `type` here is the target-KIND discriminator (already required to
    # be 'relation') — an association-type narrowing key (has_many/belongs_to/
    # has_one/has_and_belongs_to_many, mirroring _CONFIG_ASSOC_TYPES) can't
    # reuse that same name without colliding with it, so it's `assoc_type`
    # (Sol finding #5: narrow an ambiguous relation note by role, matching the
    # resolved association's `type`, which the OUTPUT entry surfaces as `type`
    # per the viewer contract — only the config INPUT key differs).
    'relation': {'type', 'source_table', 'target_table', 'foreign_key', 'name',
                 'through', 'polymorphic', 'assoc_type'},
}

def _check_config_notes(notes, path):
    if not isinstance(notes, list):
        sys.exit(f'Error: {path} `notes` must be a list of objects '
                 '({id, target, text, ...})')
    seen = set()
    for i, n in enumerate(notes):
        nw = f'notes[{i}]'
        if not isinstance(n, dict):
            sys.exit(f'Error: {path} `{nw}` must be an object')
        _reject_unknown_keys(n, _CONFIG_NOTE_KEYS, path, nw)
        note_id = n.get('id')
        if not isinstance(note_id, str) or not note_id:
            sys.exit(f'Error: {path} `{nw}` needs a non-empty string `id`')
        if note_id in seen:
            sys.exit(f'Error: {path} `notes` has a duplicate id {note_id!r}')
        seen.add(note_id)
        text = n.get('text')
        if not isinstance(text, str) or not text:
            sys.exit(f'Error: {path} note {note_id!r} needs a non-empty string `text`')
        if 'title' in n and n['title'] is not None and not isinstance(n['title'], str):
            sys.exit(f'Error: {path} note {note_id!r} `title` must be a string')
        if 'links' in n:
            _check_config_note_links(n['links'], path, note_id)
        target = n.get('target')
        if not isinstance(target, dict):
            sys.exit(f'Error: {path} note {note_id!r} needs an object `target`')
        ttype = target.get('type')
        if ttype not in _CONFIG_NOTE_TARGET_TYPES:
            sys.exit(f'Error: {path} note {note_id!r} `target.type` must be one of '
                     f'{", ".join(sorted(_CONFIG_NOTE_TARGET_TYPES))}, got {ttype!r}')
        _reject_unknown_keys(target, _CONFIG_NOTE_TARGET_KEYS[ttype], path,
                             f'{nw}.target')
        if ttype == 'table':
            tbl = target.get('table')
            if not isinstance(tbl, str) or not tbl:
                sys.exit(f'Error: {path} note {note_id!r} needs a non-empty string '
                         '`target.table`')
        elif ttype == 'relation':
            for key in ('source_table', 'target_table'):
                val = target.get(key)
                if not isinstance(val, str) or not val:
                    sys.exit(f'Error: {path} note {note_id!r} needs a non-empty string '
                             f'`target.{key}`')
            # foreign_key / through: explicit null IS meaningful ("match an
            # association where this field is absent" — Sol relaxation #2 in
            # providers.py's resolve_and_validate_notes), so it stays allowed
            # here; only a non-null value is type/shape-checked.
            for key in ('foreign_key', 'through'):
                if key in target and target[key] is not None:
                    val = target[key]
                    if isinstance(val, list):
                        sys.exit(f'Error: {path} note {note_id!r} `target.{key}` is a list '
                                 '— composite foreign keys are not supported; use a single '
                                 'column name')
                    if not isinstance(val, str) or not val:
                        sys.exit(f'Error: {path} note {note_id!r} `target.{key}` must be a '
                                 'non-empty string')
            # name / assoc_type: UNLIKE foreign_key/through above, an explicit
            # null here has no real meaning — every producer (DB/framework/
            # config) always assigns an association a name and a type, so
            # "match an association where name/type is absent" would forever
            # match zero associations and silently confuse whoever wrote it.
            # Reject the explicit null outright; the wildcard ("don't narrow
            # on this field at all") is spelled by omitting the key, not by
            # nulling it.
            if 'name' in target:
                if target['name'] is None:
                    sys.exit(f'Error: {path} note {note_id!r} target.name must not be null '
                             '— omit the key to match any name')
                if not isinstance(target['name'], str) or not target['name']:
                    sys.exit(f'Error: {path} note {note_id!r} `target.name` must be a '
                             'non-empty string')
            _check_bool(target.get('polymorphic'), 'polymorphic' in target, path,
                       f'{nw}.target.polymorphic')
            if 'assoc_type' in target:
                if target['assoc_type'] is None:
                    sys.exit(f'Error: {path} note {note_id!r} target.assoc_type must not be '
                             'null — omit the key to match any type')
                at = target['assoc_type']
                if at not in _CONFIG_ASSOC_TYPES:
                    sys.exit(f'Error: {path} note {note_id!r} `target.assoc_type` must be '
                             f'one of {", ".join(sorted(_CONFIG_ASSOC_TYPES))}, got {at!r}')

def _check_config_note_links(links, path, note_id):
    if not isinstance(links, list):
        sys.exit(f'Error: {path} note {note_id!r} `links` must be a list')
    for j, link in enumerate(links):
        lw = f'links[{j}]'
        if not isinstance(link, dict):
            sys.exit(f'Error: {path} note {note_id!r} `{lw}` must be an object')
        _reject_unknown_keys(link, _CONFIG_NOTE_LINK_KEYS, path, f'notes.{lw}')
        if 'label' in link and link['label'] is not None and not isinstance(link['label'], str):
            sys.exit(f'Error: {path} note {note_id!r} `{lw}.label` must be a string')
        url = link.get('url')
        if not isinstance(url, str) or not url:
            sys.exit(f'Error: {path} note {note_id!r} `{lw}` needs a non-empty string `url`')
        if not url.lower().startswith(('http://', 'https://')):
            sys.exit(f'Error: {path} note {note_id!r} `{lw}.url` must start with http:// '
                     f'or https:// (got {url!r})')

# ---------------------------------------------------------------------------
# `groups:` — visual table grouping sidecar (groups Phase 1, DESIGN_ROADMAP §P2).
# Purely syntactic here: shape, required fields, allow-listed keys,
# Config-internal duplicate `id`, and `color` restricted to a hex string (the
# first line of XSS/attribute-injection defense, since a group's color renders
# as a real SVG fill/stroke attribute in the viewer). Whether every member
# TABLE actually exists, and whether any table is claimed by more than one
# group, is a semantic, final-IR-after-merge concern — see
# resolve_and_validate_groups in providers.py, not here. Mirrors the notes
# two-stage split above.
# ---------------------------------------------------------------------------
_CONFIG_GROUP_KEYS = {'id', 'title', 'tables', 'color'}
# Only the valid CSS/SVG hex-color lengths: #rgb, #rgba, #rrggbb, #rrggbbaa.
# A 5- or 7-digit value is not a real hex color — the browser would drop it and
# silently fall back to the default frame color, so reject it at load instead
# (Codex re-review #2).
_CONFIG_GROUP_COLOR_RE = re.compile(r'#([0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})')

def _check_config_groups(groups, path):
    if not isinstance(groups, list):
        sys.exit(f'Error: {path} `groups` must be a list of objects '
                 '({id, tables, title?, color?})')
    seen = set()
    for i, g in enumerate(groups):
        gw = f'groups[{i}]'
        if not isinstance(g, dict):
            sys.exit(f'Error: {path} `{gw}` must be an object')
        _reject_unknown_keys(g, _CONFIG_GROUP_KEYS, path, gw)
        group_id = g.get('id')
        if not isinstance(group_id, str) or not group_id:
            sys.exit(f'Error: {path} `{gw}` needs a non-empty string `id`')
        if group_id in seen:
            sys.exit(f'Error: {path} `groups` has a duplicate id {group_id!r}')
        seen.add(group_id)
        tables = g.get('tables')
        if not isinstance(tables, list) or not tables:
            sys.exit(f'Error: {path} group {group_id!r} needs a non-empty list `tables`')
        seen_tables = set()
        for j, t in enumerate(tables):
            if not isinstance(t, str) or not t:
                sys.exit(f'Error: {path} group {group_id!r} `tables[{j}]` must be a '
                         f'non-empty string, got {t!r}')
            # A table listed twice in ONE group is a config mistake — reject it
            # here at load (like duplicate column/index names), rather than let
            # it reach the cross-group overlap check, which would then blame the
            # group for overlapping with itself (Codex re-review #3).
            if t in seen_tables:
                sys.exit(f'Error: {path} group {group_id!r} lists table {t!r} more than once')
            seen_tables.add(t)
        if 'title' in g and g['title'] is not None and not isinstance(g['title'], str):
            sys.exit(f'Error: {path} group {group_id!r} `title` must be a string')
        if 'color' in g and g['color'] is not None:
            color = g['color']
            if not isinstance(color, str) or not _CONFIG_GROUP_COLOR_RE.fullmatch(color):
                sys.exit(f'Error: {path} group {group_id!r} `color` must be a hex color '
                         f'like "#0d9488", got {color!r}')

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
        name = ix.get('name')
        if 'name' in ix and (not isinstance(name, str) or not name):
            sys.exit(f'Error: {path} `{iw}.name` must be a non-empty string when given')
        if ix.get('drop') is True:
            # IndexDrop = { name, drop: true } — a drop is still name-
            # mandatory (§7.4): merge.py matches drops by name only, and an
            # unnamed index has no stable cross-layer identity to drop by.
            if not name:
                sys.exit(f'Error: {path} `{iw}` needs a non-empty string `name` '
                         '(an index drop must be named)')
            if name in seen:
                sys.exit(f'Error: {path} `{where}.indexes` has a duplicate index name {name!r}')
            seen.add(name)
            continue
        cols = ix.get('columns')
        if (not isinstance(cols, list) or not cols
                or any(not isinstance(c, str) for c in cols)):
            sys.exit(f'Error: {path} `{iw}.columns` must be a non-empty list of strings')
        _check_bool(ix.get('unique'), 'unique' in ix, path, f'{iw}.unique')
        # A non-drop (add/override) index is name-OPTIONAL (Sol relaxation
        # #1 / full-fidelity --emit-config round trip: a DB-sourced unnamed
        # index has no name to re-emit, and merge.py already supports
        # unnamed-index identity by column tuple). Identity for THIS
        # config-internal duplicate check is the given name when present,
        # else the column tuple ALONE — matching merge.py's own unnamed-index
        # key (`tuple(columns)`, deliberately NOT including `unique`, §7.4).
        # Keying on (columns, unique) here would let a unique + non-unique
        # unnamed pair on the same columns pass load only to be silently
        # collapsed into one at merge (conflicting-value warning, arbitrary
        # winner) — so we reject that pair up front instead.
        identity = name if name else tuple(cols)
        if identity in seen:
            sys.exit(f'Error: {path} `{where}.indexes` has a duplicate index '
                     f'{"name" if name else "columns"} {identity!r}')
        seen.add(identity)

def _config_assoc_identity(a):
    """Stable identity for an association fragment/drop, used only for
    Config-internal duplicate detection. Mirrors the runtime association_key
    (§8.1) so the two never disagree about what counts as "the same" edge:
    role (owner_fk / collection / inverse_one / named) + target + FK column +
    name, PLUS `through` and `polymorphic` when present. Including the last two
    is what lets two associations that share type/name/target but differ only in
    `through` (e.g. `through: orders` vs `through: archived_orders`) coexist —
    the runtime treats them as distinct, so the syntactic check must too. `name`
    is part of the identity for every role, so the Rails alias pattern (`user`
    AND `author`, both on `user_id` -> `users`) is not a duplicate; an exact
    duplicate is still caught. Uses .get() throughout so a DropOperation (which
    may omit name/type) is handled by the same rule."""
    role = ('owner_fk' if a.get('foreign_key')
            else 'collection' if a.get('type') in ('has_many', 'has_and_belongs_to_many')
            else 'inverse_one' if a.get('type') == 'has_one'
            else 'named')
    fk = frozenset([a['foreign_key']]) if a.get('foreign_key') else frozenset()
    ident = [role, a.get('target'), fk, a.get('name')]
    if a.get('through'):
        ident.append(('through', a['through']))
    if a.get('polymorphic'):
        ident.append(('polymorphic', True))
    return tuple(ident)

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
    """Build a mysql://, postgres://, or sqlite:// URL (per the config's
    `engine`, default mysql) from the config's connection fields, or None if
    `database` wasn't given (no connection info in the config at all).

    For mysql/postgres, each of host/port/user/database is validated against
    a safe charset before being pasted into the URL string — host/user
    containing `/`, `@`, or `:` would silently shift what urlparse reads as
    the host/port/path when the assembled string is re-parsed downstream
    (verified empirically: a host of "x@evil" produces a URL whose username
    becomes "x" and whose actual host becomes "evil"), and there is no
    decoding step anywhere downstream to undo percent-encoding, so quoting
    isn't a fix either.

    For sqlite, `database` is a local file path, not a database name —
    host/port/user don't apply (rejected outright if present) and `database`
    is pasted as-is after `sqlite:///`, which round-trips exactly with
    sqlite_path_from_url()'s "strip one leading slash" rule: a relative path
    `rel.db` becomes `sqlite:///rel.db`, an absolute path `/abs/app.db`
    becomes `sqlite:////abs/app.db` (four slashes)."""
    engine = config.get('engine', 'mysql')
    if engine not in ('mysql', 'postgres', 'postgresql', 'sqlite'):
        sys.exit(f'Error: config `engine` must be "mysql", "postgres", or "sqlite", '
                 f'got {engine!r}')
    if engine == 'sqlite':
        for k in ('host', 'port', 'user'):
            if k in config:
                sys.exit(f'Error: config {k!r} does not apply when engine is "sqlite" '
                         f'(it reads a local file)')
        db = config.get('database')
        if db is None:  # absent, or explicitly blank (e.g. a bare `database:` in YAML)
            return None
        db = str(db)
        if not db:
            sys.exit('Error: config `database` must not be blank when engine is "sqlite"')
        if '?' in db or '#' in db:
            sys.exit(f'Error: config `database` {db!r} must not contain "?" or "#" — '
                     f'those would be misread as a URL query/fragment when the sqlite:// '
                     f'URL is assembled')
        if any(ord(ch) < 0x20 or ch == '\x7f' for ch in db):
            sys.exit(f'Error: config `database` {db!r} contains control characters, '
                     f'which are not valid in a file path')
        return 'sqlite:///' + db
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
    """A meaningful project name for the first normalized InputSpec's path (§10
    title fallback / D6.3). Walk from a Rails app/models dir up to the project
    root, from a prisma/schema.prisma up to the project, from a Rails
    db/schema.rb up to ITS project root (file -> parent `db` -> its parent),
    and from any other schema file up to its directory, then use the
    basename."""
    p = mroot
    if p.is_file():                                    # e.g. .../schema.prisma
        if p.name == 'schema.rb' and p.parent.name == 'db':  # .../<proj>/db/schema.rb
            p = p.parent.parent
        else:
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
        u = urlparse(url)
        if u.scheme == 'sqlite':
            stem = Path(u.path).stem   # sqlite:///path/to/shop.db -> "shop"
            if stem:
                return stem
        else:
            db = u.path.lstrip('/')
            if db:
                return db
    if fw_root is not None:
        return _framework_project_name(fw_root)
    if output:
        stem = Path(output).stem
        if stem:
            return stem
    return 'schema'

# ---------------------------------------------------------------------------
# `erdscope demo` — try the tool with zero setup, no database of your own.
#
# `pyproject.toml` ships this project as a single module (`py-modules =
# ["erd"]`), so examples/demo_shop.db is NOT part of the wheel — a `pip
# install`ed user has no file to point `erdscope sqlite:///...` at. Embedding
# the demo schema DDL here (it ends up inlined in erd.py by
# tools/build_single_file.py) sidesteps that entirely: `erdscope demo` builds
# a throwaway copy of the same sample database in a temp directory and runs
# it through the normal sqlite adapter pipeline, so it works identically
# whether you grabbed erd.py or did `pip install erdscope`.
#
# DEMO_SCHEMA_SQL is the same e-commerce schema as examples/build_demo_db.py
# (which now imports it from here — see that file) and, in spirit, the hosted
# demo (docs/gen_demo.py); kept byte-identical to the committed
# examples/demo_shop.db's schema so the two never drift apart.
# ---------------------------------------------------------------------------
DEMO_SCHEMA_SQL = """
CREATE TABLE users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email      VARCHAR(255) NOT NULL UNIQUE,
    name       VARCHAR(100) NOT NULL,
    status     INTEGER NOT NULL DEFAULT 1,   -- 1: active, 2: suspended
    created_at DATETIME NOT NULL
);

CREATE TABLE addresses (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    kind    VARCHAR(20) NOT NULL DEFAULT 'shipping',  -- shipping | billing
    line1   VARCHAR(200) NOT NULL,
    city    VARCHAR(100) NOT NULL,
    country CHAR(2) NOT NULL DEFAULT 'JP'
);
CREATE INDEX idx_addresses_user_kind ON addresses (user_id, kind);

CREATE TABLE products (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sku          VARCHAR(40) NOT NULL UNIQUE,
    title        VARCHAR(200) NOT NULL,
    price_cents  INTEGER NOT NULL DEFAULT 0,
    stock        INTEGER NOT NULL DEFAULT 0,
    discontinued INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE categories (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER REFERENCES categories(id),  -- self-reference
    name      VARCHAR(100) NOT NULL
);

CREATE TABLE product_categories (
    product_id  INTEGER NOT NULL REFERENCES products(id),
    category_id INTEGER NOT NULL REFERENCES categories(id),
    PRIMARY KEY (product_id, category_id)
);

CREATE TABLE orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    address_id  INTEGER NOT NULL REFERENCES addresses(id),
    state       VARCHAR(20) NOT NULL DEFAULT 'cart',  -- cart | placed | paid | shipped
    total_cents INTEGER NOT NULL DEFAULT 0,
    placed_at   DATETIME
);
CREATE INDEX idx_orders_user_state ON orders (user_id, state);

CREATE TABLE order_items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id         INTEGER NOT NULL REFERENCES orders(id),
    product_id       INTEGER NOT NULL REFERENCES products(id),
    quantity         INTEGER NOT NULL DEFAULT 1,
    unit_price_cents INTEGER NOT NULL
);
CREATE INDEX idx_order_items_order ON order_items (order_id);

CREATE TABLE payments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     INTEGER NOT NULL REFERENCES orders(id),
    provider     VARCHAR(30) NOT NULL,   -- stripe | paypal | ...
    amount_cents INTEGER NOT NULL,
    captured_at  DATETIME
);

CREATE TABLE shipments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL UNIQUE REFERENCES orders(id),  -- 1:1 with orders
    carrier     VARCHAR(30) NOT NULL,
    tracking_no VARCHAR(60),
    shipped_at  DATETIME
);

CREATE TABLE reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    user_id    INTEGER NOT NULL REFERENCES users(id),
    rating     INTEGER NOT NULL,   -- 1-5 stars
    body       TEXT,
    UNIQUE (product_id, user_id)   -- one review per product per user
);

CREATE TABLE coupons (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    code           VARCHAR(30) NOT NULL UNIQUE,
    discount_cents INTEGER NOT NULL,
    expires_at     DATETIME
);

CREATE TABLE order_coupons (
    order_id  INTEGER NOT NULL REFERENCES orders(id),
    coupon_id INTEGER NOT NULL REFERENCES coupons(id),
    PRIMARY KEY (order_id, coupon_id)
);

-- append-only audit trail: *_id columns on purpose have NO foreign keys, so
-- `--infer-fk` can demonstrate guessing edges from column names alone
CREATE TABLE activity_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    order_id   INTEGER,
    action     VARCHAR(50) NOT NULL,
    created_at DATETIME NOT NULL
);
CREATE INDEX idx_logs_created ON activity_logs (created_at);
"""


def build_demo_db(path):
    """Create a fresh SQLite database at `path` from DEMO_SCHEMA_SQL (any
    existing file there is replaced). Shared by `erdscope demo` (built into a
    throwaway temp directory on every run) and examples/build_demo_db.py
    (regenerating the committed examples/demo_shop.db). Returns `path`."""
    import sqlite3
    path = Path(path)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(DEMO_SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()
    return path


def run_demo(args):
    """Handle `erdscope demo`: the CLI's `database` positional was the literal
    string "demo" (see the sentinel check in main()). Build a throwaway copy
    of the sample e-commerce database in a temp directory, point the normal
    sqlite adapter pipeline at it (as a sqlite:///<absolute path> URL, so
    every other flag — --only/--excel/--max-rows/... — still applies
    unmodified), then open the result in a browser.

    Config auto-discovery is force-disabled: a stray .erdscope.json in the
    cwd should not silently change what the demo looks like. An explicit
    --config is not an error either — it's ignored with a warning, since the
    whole point of `demo` is that it "just works" with no setup."""
    import tempfile
    import webbrowser

    if getattr(args, 'config', None):
        print('Warning: --config is ignored by `erdscope demo`', file=sys.stderr)
        args.config = None
    args.no_config = True  # also skips .erdscope.* auto-discovery (load_config)

    if not hasattr(args, 'output'):  # -o not given on the CLI — SUPPRESS default
        args.output = 'erd_demo.html'  # don't clobber a plain `erd.html` from a real run

    with tempfile.TemporaryDirectory() as tmp:
        db_path = build_demo_db(Path(tmp) / 'demo_shop.db')
        # 4-slash sqlite:// form: 3 literal slashes + the absolute path's own
        # leading slash (sqlite_path_from_url strips exactly one of them).
        args.database = 'sqlite:///' + str(db_path.resolve())
        _run_pipeline(args)  # builds, filters, and writes args.output — all
                             # while the temp db file above still exists

    if not getattr(args, 'no_open', False):
        try:
            webbrowser.open(Path(args.output).resolve().as_uri())
        except Exception:
            pass  # best-effort convenience only — never fail the run over it
# ---------------------------------------------------------------------------
# --emit-json — canonical JSON snapshot + content fingerprint (backlog #0)
#
# A separate, machine-readable projection of the final merged IR (post --only/
# --exclude, post notes/groups resolution), independent of the HTML/Excel
# outputs: a fixed allowlist of table keys, deterministic ordering everywhere
# order isn't already meaningful, and provenance normalized to the 5-value set
# instead of the legacy db_fk/inferred/manual/schema_fk booleans the HTML/
# Excel path still uses (serialize_for_viewer, cli.py). Pure and non-
# destructive throughout: every function here deep-copies its input before
# touching it, and never sorts in place.
# ---------------------------------------------------------------------------

# The only 5 provenance values a canonical association may carry (§9.1 in
# ir.py's docstring). Anything else is a bug upstream (merge_ir, or a plugin
# writing a bogus `provenance`) and must fail loudly here rather than emit a
# snapshot silently claiming a fabricated value.
_VALID_PROVENANCE = {'declared', 'manual', 'db_fk', 'schema_fk', 'inferred'}

# Optional column keys, omitted from the canonical column when falsy (empty
# string, None, or False) — mirrors the IR docstring's ColumnIR optionals
# minus `primary` (which has its own True-only rule below).
_OPTIONAL_COLUMN_KEYS = ('sql_type', 'default', 'extra', 'comment')


def _canonical_column(c):
    """One column -> its canonical projection. Always carries name/type/
    nullable; the rest are included only when truthy. `primary` is included
    only when True (never `"primary": false`)."""
    out = {'name': c['name'], 'type': c.get('type', ''),
           'nullable': bool(c.get('nullable', False))}
    for key in _OPTIONAL_COLUMN_KEYS:
        val = c.get(key)
        if val:
            out[key] = val
    if c.get('primary'):
        out['primary'] = True
    return out


def _canonical_indexes(indexes):
    """Indexes, each projected to name?/columns/unique, sorted by
    `(name or "", tuple(columns), unique)` for a deterministic snapshot
    regardless of the IR's original (first-seen-across-layers) order."""
    out = []
    for ix in indexes or []:
        item = {}
        if ix.get('name'):
            item['name'] = ix['name']
        item['columns'] = list(ix.get('columns', []))
        item['unique'] = bool(ix.get('unique', False))
        out.append(item)
    out.sort(key=lambda item: (item.get('name') or '', tuple(item['columns']), item['unique']))
    return out


def _association_provenance(a):
    """The association's provenance, from either IR shape (mirrors
    _assoc_provenance in ir.py, but validated against the 5-value set — a
    canonical snapshot never emits a value outside it)."""
    prov = a['provenance'] if 'provenance' in a else provenance_of(a)
    if prov not in _VALID_PROVENANCE:
        raise ValueError(f'unknown association provenance {prov!r}')
    return prov


def _association_sources(a):
    """Deduplicated, ascending-(kind, provider) `sources` for a merged-IR
    association; omitted entirely (returns None) for a legacy-shape
    association, which never carries `sources`."""
    if 'provenance' not in a:
        return None
    seen, out = set(), []
    for s in a.get('sources') or []:
        key = (s['kind'], s['provider'])
        if key not in seen:
            seen.add(key)
            out.append({'kind': s['kind'], 'provider': s['provider']})
    out.sort(key=lambda s: (s['kind'], s['provider']))
    return out


def _assoc_sort_key(item):
    """Deterministic association ordering: `(type, name or "", foreign_key or
    "", target or "", through or "", polymorphic-bit, provenance)`, with the
    canonical association's own JSON string as the final tie-breaker.
    `polymorphic` is normalized to '' / '1' (rather than left as False/True)
    so the key never mixes bool and str at the same tuple position across
    associations — Python's tuple comparison would raise TypeError comparing
    `True` to `''` the moment two associations tie on every earlier field."""
    poly_bit = '1' if item.get('polymorphic') else ''
    tie = json.dumps(item, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return (item['type'], item.get('name') or '', item.get('foreign_key') or '',
            item.get('target') or '', item.get('through') or '', poly_bit,
            item['provenance'], tie)


def _canonical_associations(associations, survivors):
    """Associations -> canonical projection: allowlisted keys only
    (type/target/name?/foreign_key?/through?/polymorphic?/provenance/
    sources?), legacy boolean flags (db_fk/inferred/manual/schema_fk) always
    dropped, dangling associations (target not in `survivors`) pruned, then
    deterministically sorted."""
    out = []
    for a in associations or []:
        target = a.get('target')
        # Prune only a *resolvable* association whose target isn't in the set
        # (e.g. --only/--exclude dropped the target table). A polymorphic
        # belongs_to carries a synthetic, tableless target (django's
        # `pluralize(to_snake(name))`, rails' association name) that is never a
        # real table — it must be KEPT, exactly as the HTML/Excel path keeps it
        # (details-pane, no edge). Pruning it on `target not in survivors` would
        # silently drop every polymorphic relation from the snapshot.
        if not a.get('polymorphic') and target not in survivors:
            continue  # dangling: --only/--exclude (or a stale fixture) left the target out
        item = {'type': a['type'], 'target': target}
        if a.get('name'):
            item['name'] = a['name']
        if a.get('foreign_key'):
            item['foreign_key'] = a['foreign_key']
        if a.get('through'):
            item['through'] = a['through']
        if a.get('polymorphic'):
            item['polymorphic'] = True
        item['provenance'] = _association_provenance(a)
        sources = _association_sources(a)
        if sources:
            item['sources'] = sources
        out.append(item)
    out.sort(key=_assoc_sort_key)
    return out


def _canonical_table(t, survivors):
    """One merged-IR table -> its canonical projection: ONLY comment?/
    columns/indexes/associations survive (fk_columns, schema_missing, and any
    other internal/plugin key are never emitted). `comment` is omitted when
    empty/None."""
    out = {}
    comment = t.get('comment')
    if comment:
        out['comment'] = comment
    out['columns'] = [_canonical_column(c) for c in t.get('columns', [])]
    out['indexes'] = _canonical_indexes(t.get('indexes'))
    out['associations'] = _canonical_associations(t.get('associations'), survivors)
    return out


def _canonical_notes(notes_data):
    """Notes, each passed through unchanged (already the viewer-resolved
    shape, `links` order already meaningful and preserved), sorted by `id`
    ascending."""
    return sorted(copy.deepcopy(notes_data), key=lambda n: n['id'])


def _canonical_groups(groups_data):
    """Groups, each with `tables` re-sorted by name ascending, the whole list
    sorted by `id` ascending."""
    out = []
    for g in groups_data:
        g = copy.deepcopy(g)
        g['tables'] = sorted(g['tables'])
        out.append(g)
    out.sort(key=lambda g: g['id'])
    return out


def canonical_schema(tables, notes_data, groups_data):
    """Project the final merged IR (+ resolved notes/groups) to the
    canonical, allowlisted, deterministically-ordered shape backing
    --emit-json. Pure: deep-copies its inputs, never mutates them, and never
    sorts in place — every list above is built fresh. `notes`/`groups` are
    omitted from the result entirely when empty/None (mirrors the DATA_JSON
    `notes`/`groups` key-omission rule in cli.py's _finish)."""
    tables = copy.deepcopy(tables)
    survivors = set(tables)
    out_tables = {name: _canonical_table(t, survivors) for name, t in tables.items()}
    schema = {'tables': out_tables}
    if notes_data:
        schema['notes'] = _canonical_notes(notes_data)
    if groups_data:
        schema['groups'] = _canonical_groups(groups_data)
    return schema


def snapshot_fingerprint(schema):
    """sha256 content fingerprint of a canonical `schema` dict, prefixed
    `sha256:`. Hashes the UTF-8 bytes of
    `json.dumps({"format": 1, "schema": schema}, sort_keys=True,
    ensure_ascii=False, separators=(",", ":"), allow_nan=False)` — sorted keys
    and a compact separator make the digest depend only on content, never on
    dict insertion order or incidental whitespace; `allow_nan=False` rejects a
    NaN/Infinity that snuck into a comment or default and would otherwise
    serialize as non-portable, non-standard JSON. `format` is folded into the
    hashed payload so a future format bump is a hard fingerprint break, never
    a silent collision with format 1."""
    payload = json.dumps({'format': 1, 'schema': schema}, sort_keys=True,
                         ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    digest = hashlib.sha256(payload.encode('utf-8')).hexdigest()
    return f'sha256:{digest}'


def emit_json_document(tables, notes_data, groups_data):
    """Build the full --emit-json document (as a string, trailing newline
    included): `{"format": 1, "fingerprint": "sha256:...", "schema": {...}}`,
    pretty-printed (`indent=2, sort_keys=True`) for human readability. No
    `</` escaping (unlike the HTML DATA_JSON payload) — this file is never
    embedded in a `<script>` tag. No version field by design: the snapshot's
    own fingerprint is the stable identity, not a tool version string."""
    schema = canonical_schema(tables, notes_data, groups_data)
    document = {'format': 1, 'fingerprint': snapshot_fingerprint(schema), 'schema': schema}
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True,
                      allow_nan=False) + '\n'


# ---------------------------------------------------------------------------
# --emit-config — config-authoring (YAML/JSON) projection of the final merged
# IR (backlog #1). Reuses every canonical_schema helper above for identical
# dangling-pruning, deterministic sort, and falsy-omission rules — the two
# emitters diverge only in OUTPUT SHAPE, not in which data survives:
#   - columns/indexes: byte-identical projection to --emit-json's own
#     (_canonical_column / _canonical_indexes, reused verbatim).
#   - associations: same pruning + sort as --emit-json's
#     (_canonical_associations, reused), minus provenance/sources — a
#     config-only reimport can never carry either (merge_ir always assigns
#     config-kind associations 'manual' provenance; the config association
#     input format has no field for provenance/sources at all).
#   - composite primary keys (P1/E-2): re-derived here from column.primary
#     IN COLUMN ORDER, never from the IR's own `primary_key` field — a
#     DB-sourced composite PK's `primary_key` names only its FIRST column
#     (merge.py:312's _normalize_primary_key docstring), so reading it here
#     would silently truncate the PK on reimport. A single-column PK stays a
#     plain column `primary: true`; only 2+ primary columns promote to a
#     table-level `primary_key: [...]`.
#
# This is intentionally NOT a full round trip: provenance, `sources`, the
# legacy db_fk/inferred/manual/schema_fk flags, and the config drop/*_mode
# OPERATIONS are all gone after one pass through the merged IR — a
# config-only reimport of this file reaches "level1" (materially the same
# schema — same tables/columns/types/nullability/defaults, same primary-key
# COLUMN SETS, same indexes as (columns, unique) sets, same associations as
# (type, target, foreign_key, through, polymorphic) tuples, same
# comments/notes/groups), not a byte-identical config source.
# ---------------------------------------------------------------------------

def _config_associations(associations, survivors):
    """Config-authoring projection of one table's associations: identical
    dangling-pruning and deterministic sort to _canonical_associations
    (reused directly), but WITHOUT provenance/sources — the config
    association shape (type/target/name?/foreign_key?/through?/
    polymorphic?) has no field for either, and a config-only reimport
    resolves every association to 'manual' provenance regardless of what it
    originally was."""
    out = []
    for item in _canonical_associations(associations, survivors):
        entry = {'type': item['type'], 'target': item['target']}
        for key in ('name', 'foreign_key', 'through', 'polymorphic'):
            if key in item:
                entry[key] = item[key]
        out.append(entry)
    return out


def _config_table(t, survivors):
    """One merged-IR table -> its config-authoring TableFragment (the shape
    config.py's _CONFIG_TABLE_KEYS allow-list accepts on load): comment
    (falsy-omitted, same rule as _canonical_table), primary_key (ONLY for a
    genuine composite PK — 2+ columns flagged primary; a single-column PK
    stays a plain column `primary: true`, per P1/E-2), columns/indexes
    (byte-identical to --emit-json's own projection), associations (see
    _config_associations above)."""
    out = {}
    comment = t.get('comment')
    if comment:
        out['comment'] = comment
    columns = t.get('columns', [])
    # Composite-PK detection is column-primary-FLAG based, NOT primary_key-
    # FIELD based (P1/E-2) — see the module-level comment above.
    primary_cols = [c['name'] for c in columns if c.get('primary')]
    if len(primary_cols) >= 2:
        out['primary_key'] = primary_cols
    out['columns'] = [_canonical_column(c) for c in columns]
    out['indexes'] = _canonical_indexes(t.get('indexes'))
    out['associations'] = _config_associations(t.get('associations'), survivors)
    return out


def _config_note(n):
    """One resolved (viewer-shape) note -> its config `notes:` INPUT
    projection — the inverse of providers.resolve_and_validate_notes.
    global/table targets pass through almost as-is. A relation target
    re-derives the FULL narrowing key (name/foreign_key/through/assoc_type/
    polymorphic ALL present, explicit `null` wherever the resolved
    association carries no value for that field — never simply omitted) so
    that reimporting resolves back to the exact one association this note
    came from: resolve_and_validate_notes's null-vs-absent narrowing
    (providers.py, Sol relaxation #2) treats an explicit `null` here as "the
    field must be absent on the match" and an OMITTED key as "don't care" —
    only the former is unambiguous enough for a lossless reimport."""
    scope = n['scope']
    if scope == 'global':
        target = {'type': 'global'}
    elif scope == 'table':
        target = {'type': 'table', 'table': n['table']}
    else:  # relation
        target = {
            'type': 'relation',
            'source_table': n['source_table'],
            'target_table': n['target'],
            'name': n['name'],
            'foreign_key': n.get('foreign_key'),
            'through': n.get('through'),
            'assoc_type': n['type'],
            'polymorphic': bool(n.get('polymorphic')),
        }
    entry = {'id': n['id'], 'target': target}
    if n.get('title'):
        entry['title'] = n['title']
    entry['text'] = n['text']
    if n.get('links'):
        entry['links'] = n['links']
    return entry


def config_document(tables, notes_data, groups_data, title=None):
    """Build the --emit-config document: the final merged IR (+ resolved
    notes/groups), projected to the CONFIG AUTHORING shape (config.py's
    accepted top-level keys — version/title?/tables/notes?/groups?) instead
    of --emit-json's read-only snapshot shape. Pure: deep-copies its inputs,
    never mutates them. `title` is omitted when falsy; `notes`/`groups` are
    omitted entirely when empty/None, mirroring canonical_schema."""
    tables = copy.deepcopy(tables)
    survivors = set(tables)
    out_tables = {name: _config_table(t, survivors) for name, t in tables.items()}
    doc = {'version': 1, 'tables': out_tables}
    if title:
        doc['title'] = title
    if notes_data:
        doc['notes'] = [_config_note(n) for n in _canonical_notes(notes_data)]
    if groups_data:
        doc['groups'] = _canonical_groups(groups_data)
    return doc


def config_json_text(document):
    """--emit-config FILE.json (and `-` stdout, JSON by default) text: same
    conventions as emit_json_document — indent=2, sort_keys=True,
    ensure_ascii=False, trailing newline — for a diff-friendly file whose
    bytes depend only on content, never dict insertion order."""
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True,
                      allow_nan=False) + '\n'


def config_yaml_text(document):
    """--emit-config FILE.yml/.yaml text (Sol relaxation #6). Deterministic
    (`default_flow_style=False`, `allow_unicode=True`, `sort_keys=True` — the
    same "stable regardless of dict insertion order" guarantee as the JSON
    path's `sort_keys=True`) YAML dump, with a custom string representer
    that:
      - forces literal block style (`|`) for any string containing a
        newline, so long note/comment text stays human-readable instead of
        PyYAML's default folded-quote style (one value, blank-line-joined);
      - explicitly quotes any OTHER plain scalar that YAML's own
        implicit-typing resolver would resolve to a non-string tag — the
        "Norway problem" (bare no/yes/on/off/true/false -> bool), a
        leading-zero digit string misread as octal, or any bare
        numeric-looking string misread as int/float/timestamp — so a config
        string value is guaranteed, not just incidentally, to round-trip as
        the SAME string the next time `--config` loads this file (belt and
        suspenders: PyYAML's own default emitter already avoids most of
        these by checking the identical resolver before choosing plain
        style, but doing it here explicitly makes the guarantee independent
        of that emitter-internal behavior).
    Requires PyYAML to be importable — the caller (cli.py) checks that up
    front and exits with a clear, no-fallback error before ever reaching
    here, per Sol relaxation #6 ("no JSON fallback" for a requested .yml/
    .yaml path)."""
    import yaml

    class _ConfigDumper(yaml.SafeDumper):
        pass

    resolver = yaml.resolver.Resolver()

    def _represent_str(dumper, value):
        if '\n' in value:
            return dumper.represent_scalar('tag:yaml.org,2002:str', value, style='|')
        tag = resolver.resolve(yaml.ScalarNode, value, (True, False))
        style = None if tag == 'tag:yaml.org,2002:str' else "'"
        return dumper.represent_scalar('tag:yaml.org,2002:str', value, style=style)

    _ConfigDumper.add_representer(str, _represent_str)
    return yaml.dump(document, Dumper=_ConfigDumper, default_flow_style=False,
                     allow_unicode=True, sort_keys=True, width=1 << 20)
# ---------------------------------------------------------------------------
# schema diff / drift (backlog #2) — level1 semantic diff between two
# canonical schemas (emit.py's canonical_schema shape:
# {tables: {...}, notes?: [...], groups?: [...]}), driving the --diff CI
# drift gate in cli.py.
#
# Direction (fixed, documented once here — every helper below honors it):
# `left` is the CURRENT run's canonical_schema; `right` is the BASE snapshot
# being compared against (an --emit-json document's `schema`). At every
# level (tables, columns, indexes, associations, notes, groups):
#   added   = present in `left` (current) only  — new since the snapshot
#   removed = present in `right` (base) only     — gone since the snapshot
# This never inverts at a sub-level: a table's added columns are still
# "present in the current run's version of that table only", etc.
#
# level1 identity (materially-the-same-schema, not byte-identical — the same
# notion --emit-config's docstring in emit.py already defines):
#   - tables/notes/groups: matched by name/id; a matched pair with any field
#     difference is "changed" (differing fields enumerated with old/new).
#   - columns: matched by name; changed = any of type/sql_type/nullable/
#     primary/default/extra/comment differs (differing fields enumerated).
#   - indexes: matched by (tuple(columns), unique) — name is NOT part of
#     identity, so a bare rename is invisible at level1; indexes are pure
#     added/removed, never "changed".
#   - associations: matched by (type, target, name, foreign_key, through,
#     polymorphic) — provenance/sources excluded by default (they describe
#     WHERE an association came from, not what it means at level1); pass
#     include_provenance=True to fold them into identity too. A "changed"
#     association (e.g. retargeted foreign_key) has no separate
#     representation: it is the OLD identity removed + the NEW identity
#     added — exactly emit.py's association-diff wording.
#
# Known level1 limitations (documented, by design — the same "level1 material
# meaning, not byte identity" line --emit-config draws):
#   - an empty-string column default and "no default at all" look identical in
#     every canonical schema (no provider distinguishes the two on read), so
#     this diff cannot detect that specific change.
#   - a pure column REORDER is invisible: columns are matched by name, so the
#     same columns in a different order compare equal. (canonical_schema keeps
#     column order and the fingerprint is order-sensitive, so a reorder still
#     fails the fingerprint fast path and falls through to this order-agnostic
#     deep compare — landing on "no difference". fingerprint = byte identity;
#     diff = level1 meaning.)
# ---------------------------------------------------------------------------

_COLUMN_FIELDS = ('type', 'sql_type', 'nullable', 'primary', 'default', 'extra', 'comment')
_COLUMN_FIELD_FALSY_DEFAULTS = {'nullable': False, 'primary': False}


def _column_field(col, field):
    """A canonical column's value for `field`, defaulting exactly the way
    emit.py's _canonical_column omits it: '' for the falsy-omitted string
    fields (sql_type/default/extra/comment; `type` is always present so this
    default never actually applies to it), False for nullable/primary."""
    return col.get(field, _COLUMN_FIELD_FALSY_DEFAULTS.get(field, ''))


def _diff_columns(left_cols, right_cols):
    left_by_name = {c['name']: c for c in left_cols}
    right_by_name = {c['name']: c for c in right_cols}
    added = sorted(set(left_by_name) - set(right_by_name))
    removed = sorted(set(right_by_name) - set(left_by_name))
    changed = {}
    for name in sorted(set(left_by_name) & set(right_by_name)):
        l, r = left_by_name[name], right_by_name[name]
        fields = {}
        for f in _COLUMN_FIELDS:
            lv, rv = _column_field(l, f), _column_field(r, f)
            if lv != rv:
                fields[f] = {'old': rv, 'new': lv}
        if fields:
            changed[name] = {'fields': fields}
    return {'added': added, 'removed': removed, 'changed': changed}


def _index_identity(ix):
    return (tuple(ix.get('columns', [])), bool(ix.get('unique', False)))


def _index_repr(ix):
    return {'columns': list(ix.get('columns', [])), 'unique': bool(ix.get('unique', False))}


def _diff_indexes(left_idx, right_idx):
    # First-seen-wins on a duplicate identity (shouldn't happen for a
    # genuinely canonical schema, but a hand-built/adversarial right-hand
    # snapshot must not crash the diff over it).
    left_by_id, right_by_id = {}, {}
    for ix in left_idx:
        left_by_id.setdefault(_index_identity(ix), ix)
    for ix in right_idx:
        right_by_id.setdefault(_index_identity(ix), ix)
    added_ids = sorted(set(left_by_id) - set(right_by_id))
    removed_ids = sorted(set(right_by_id) - set(left_by_id))
    added = [_index_repr(left_by_id[i]) for i in added_ids]
    removed = [_index_repr(right_by_id[i]) for i in removed_ids]
    return {'added': added, 'removed': removed}


def _assoc_identity(a, include_provenance):
    identity = (a['type'], a.get('target') or '', a.get('name') or '',
                a.get('foreign_key') or '', a.get('through') or '',
                bool(a.get('polymorphic')))
    if include_provenance:
        sources = tuple(sorted((s['kind'], s['provider']) for s in a.get('sources') or []))
        identity = identity + (a.get('provenance') or '', sources)
    return identity


def _assoc_repr(a, include_provenance):
    out = {'type': a['type'], 'target': a.get('target')}
    for key in ('name', 'foreign_key', 'through'):
        if a.get(key):
            out[key] = a[key]
    if a.get('polymorphic'):
        out['polymorphic'] = True
    if include_provenance:
        if a.get('provenance'):
            out['provenance'] = a['provenance']
        if a.get('sources'):
            out['sources'] = a['sources']
    return out


def _diff_associations(left_assoc, right_assoc, include_provenance):
    left_by_id, right_by_id = {}, {}
    for a in left_assoc:
        left_by_id.setdefault(_assoc_identity(a, include_provenance), a)
    for a in right_assoc:
        right_by_id.setdefault(_assoc_identity(a, include_provenance), a)
    added_ids = sorted(set(left_by_id) - set(right_by_id))
    removed_ids = sorted(set(right_by_id) - set(left_by_id))
    added = [_assoc_repr(left_by_id[i], include_provenance) for i in added_ids]
    removed = [_assoc_repr(right_by_id[i], include_provenance) for i in removed_ids]
    return {'added': added, 'removed': removed}


def _table_comment(t):
    return t.get('comment') or ''


def _diff_table(left_t, right_t, include_provenance):
    """One matched (same-name) table pair -> its diff entry, or {} when the
    two are level1-identical (comment + columns + indexes + associations all
    equal) — the caller uses truthiness to decide whether the table belongs
    in the `changed` map at all."""
    out = {}
    lc, rc = _table_comment(left_t), _table_comment(right_t)
    if lc != rc:
        out['comment'] = {'old': rc, 'new': lc}
    columns = _diff_columns(left_t.get('columns', []), right_t.get('columns', []))
    if columns['added'] or columns['removed'] or columns['changed']:
        out['columns'] = columns
    indexes = _diff_indexes(left_t.get('indexes', []), right_t.get('indexes', []))
    if indexes['added'] or indexes['removed']:
        out['indexes'] = indexes
    associations = _diff_associations(left_t.get('associations', []),
                                      right_t.get('associations', []), include_provenance)
    if associations['added'] or associations['removed']:
        out['associations'] = associations
    return out


def _diff_tables(left_tables, right_tables, include_provenance):
    added = sorted(set(left_tables) - set(right_tables))
    removed = sorted(set(right_tables) - set(left_tables))
    changed = {}
    for name in sorted(set(left_tables) & set(right_tables)):
        d = _diff_table(left_tables[name], right_tables[name], include_provenance)
        if d:
            changed[name] = d
    return {'added': added, 'removed': removed, 'changed': changed}


# A note's full identity envelope beyond id/text/title/links — which of these
# keys are actually present on a given note depends on its `scope` (global/
# table/relation; see providers.resolve_and_validate_notes's shared-contract
# entry shapes), so comparing the union of possibly-present keys covers every
# scope combination (including a note changing scope entirely) without
# special-casing any one of them.
_NOTE_FIELDS = ('scope', 'table', 'source_table', 'target', 'type', 'name',
                'foreign_key', 'through', 'polymorphic', 'title', 'text', 'links')


def _diff_note(l, r):
    fields = {}
    for f in _NOTE_FIELDS:
        lv, rv = l.get(f), r.get(f)
        if lv != rv:
            fields[f] = {'old': rv, 'new': lv}
    return {'fields': fields} if fields else {}


def _diff_notes(left_notes, right_notes):
    left_by_id = {n['id']: n for n in left_notes or []}
    right_by_id = {n['id']: n for n in right_notes or []}
    added = sorted(set(left_by_id) - set(right_by_id))
    removed = sorted(set(right_by_id) - set(left_by_id))
    changed = {}
    for nid in sorted(set(left_by_id) & set(right_by_id)):
        d = _diff_note(left_by_id[nid], right_by_id[nid])
        if d:
            changed[nid] = d
    return {'added': added, 'removed': removed, 'changed': changed}


def _diff_group(l, r):
    out = {}
    lt, rt = set(l.get('tables', [])), set(r.get('tables', []))
    if lt != rt:
        out['tables'] = {'added': sorted(lt - rt), 'removed': sorted(rt - lt)}
    lti, rti = l.get('title') or '', r.get('title') or ''
    if lti != rti:
        out['title'] = {'old': rti, 'new': lti}
    lco, rco = l.get('color') or '', r.get('color') or ''
    if lco != rco:
        out['color'] = {'old': rco, 'new': lco}
    return out


def _diff_groups(left_groups, right_groups):
    left_by_id = {g['id']: g for g in left_groups or []}
    right_by_id = {g['id']: g for g in right_groups or []}
    added = sorted(set(left_by_id) - set(right_by_id))
    removed = sorted(set(right_by_id) - set(left_by_id))
    changed = {}
    for gid in sorted(set(left_by_id) & set(right_by_id)):
        d = _diff_group(left_by_id[gid], right_by_id[gid])
        if d:
            changed[gid] = d
    return {'added': added, 'removed': removed, 'changed': changed}


def schema_diff(left_schema, right_schema, *, include_provenance=False):
    """The level1 diff between two canonical schemas (emit.py's
    canonical_schema shape). Pure: only reads `left_schema`/`right_schema`,
    never mutates either. See the module docstring above for the fixed
    left=current/right=base direction and the per-level identity rules.
    Returns a deterministic dict — every level is {added, removed, changed},
    each already sorted/keyed for a stable render_json()."""
    left_tables = left_schema.get('tables', {})
    right_tables = right_schema.get('tables', {})
    return {
        'tables': _diff_tables(left_tables, right_tables, include_provenance),
        'notes': _diff_notes(left_schema.get('notes'), right_schema.get('notes')),
        'groups': _diff_groups(left_schema.get('groups'), right_schema.get('groups')),
    }


def empty_schema_diff():
    """The all-empty diff shape schema_diff() would return for two
    level1-identical schemas — used by cli.py's fingerprint fast path so a
    fingerprint match can skip the deep compare entirely while still handing
    render_text/render_json/diff_is_empty the exact shape they expect."""
    def _section():
        return {'added': [], 'removed': [], 'changed': {}}
    return {'tables': _section(), 'notes': _section(), 'groups': _section()}


def diff_is_empty(diff):
    """True when `diff` (schema_diff()'s or empty_schema_diff()'s return
    value) carries no difference at all in any of tables/notes/groups."""
    return all(not diff[section]['added'] and not diff[section]['removed']
              and not diff[section]['changed'] for section in ('tables', 'notes', 'groups'))


def render_json(diff) -> str:
    """--diff-format json rendering: same conventions as emit.py's own JSON
    writers — indent=2, sort_keys=True, ensure_ascii=False, trailing
    newline — so the text is diff-friendly and depends only on content."""
    return json.dumps(diff, indent=2, sort_keys=True, ensure_ascii=False) + '\n'


def _fmt_change(old, new):
    return f'{old!r} -> {new!r}'


def _fmt_fields(fields):
    return ', '.join(f'{k}: {_fmt_change(v["old"], v["new"])}' for k, v in sorted(fields.items()))


def _assoc_label(item):
    bits = [item['type'], item.get('target') or '']
    if item.get('name'):
        bits.append(f'name={item["name"]}')
    if item.get('foreign_key'):
        bits.append(f'fk={item["foreign_key"]}')
    if item.get('through'):
        bits.append(f'through={item["through"]}')
    if item.get('polymorphic'):
        bits.append('polymorphic')
    if item.get('provenance'):
        bits.append(f'provenance={item["provenance"]}')
    return ' '.join(bits)


def _index_label(item):
    return f'({", ".join(item["columns"])}) unique={item["unique"]}'


def _render_table_detail(td, indent='      '):
    lines = []
    if 'comment' in td:
        lines.append(f'{indent}comment: {_fmt_change(td["comment"]["old"], td["comment"]["new"])}')
    if 'columns' in td:
        c = td['columns']
        for name in c['added']:
            lines.append(f'{indent}+ column {name}')
        for name in c['removed']:
            lines.append(f'{indent}- column {name}')
        for name in sorted(c['changed']):
            lines.append(f'{indent}~ column {name} ({_fmt_fields(c["changed"][name]["fields"])})')
    if 'indexes' in td:
        ix = td['indexes']
        for item in ix['added']:
            lines.append(f'{indent}+ index {_index_label(item)}')
        for item in ix['removed']:
            lines.append(f'{indent}- index {_index_label(item)}')
    if 'associations' in td:
        asc = td['associations']
        for item in asc['added']:
            lines.append(f'{indent}+ association {_assoc_label(item)}')
        for item in asc['removed']:
            lines.append(f'{indent}- association {_assoc_label(item)}')
    return lines


def render_text(diff) -> str:
    """--diff-format text (default) rendering: a summary line, then one
    section per top-level kind (tables/notes/groups) with +added/-removed/
    ~changed detail lines. No ANSI color (optional per spec; kept plain for
    simplicity and CI-log friendliness)."""
    t, n, g = diff['tables'], diff['notes'], diff['groups']
    total_added = len(t['added']) + len(n['added']) + len(g['added'])
    total_removed = len(t['removed']) + len(n['removed']) + len(g['removed'])
    total_changed = len(t['changed']) + len(n['changed']) + len(g['changed'])
    if not (total_added or total_removed or total_changed):
        return 'No schema differences.\n'

    lines = [f'{total_added} added, {total_removed} removed, {total_changed} changed', '']

    if t['added'] or t['removed'] or t['changed']:
        lines.append('tables:')
        for name in t['added']:
            lines.append(f'  + {name}')
        for name in t['removed']:
            lines.append(f'  - {name}')
        for name in sorted(t['changed']):
            lines.append(f'  ~ {name}')
            lines.extend(_render_table_detail(t['changed'][name]))
        lines.append('')

    if n['added'] or n['removed'] or n['changed']:
        lines.append('notes:')
        for nid in n['added']:
            lines.append(f'  + {nid}')
        for nid in n['removed']:
            lines.append(f'  - {nid}')
        for nid in sorted(n['changed']):
            lines.append(f'  ~ {nid} ({_fmt_fields(n["changed"][nid]["fields"])})')
        lines.append('')

    if g['added'] or g['removed'] or g['changed']:
        lines.append('groups:')
        for gid in g['added']:
            lines.append(f'  + {gid}')
        for gid in g['removed']:
            lines.append(f'  - {gid}')
        for gid in sorted(g['changed']):
            lines.append(f'  ~ {gid}')
            gd = g['changed'][gid]
            if 'tables' in gd:
                for tn in gd['tables']['added']:
                    lines.append(f'      + table {tn}')
                for tn in gd['tables']['removed']:
                    lines.append(f'      - table {tn}')
            if 'title' in gd:
                lines.append(f'      title: {_fmt_change(gd["title"]["old"], gd["title"]["new"])}')
            if 'color' in gd:
                lines.append(f'      color: {_fmt_change(gd["color"]["old"], gd["color"]["new"])}')
        lines.append('')

    return '\n'.join(lines).rstrip('\n') + '\n'
# ---------------------------------------------------------------------------
# --emit-digest — token-efficient Markdown digest of the schema, WITH design
# notes, for LLM/agent consumption (backlog #3). Reuses emit.py's
# canonical_schema (same allowlist/pruning/deterministic order --emit-json and
# --emit-config already share) — render_digest never re-derives what survives
# or how it's ordered, only how it's RENDERED. Pure and non-destructive:
# canonical_schema already deep-copies, and nothing here mutates its result.
#
# Differentiator (the digest's whole reason to exist, per EMIT_DIGEST_SPEC.md
# §0): design intent a machine cannot re-derive from the raw schema — notes
# (table/relation/global) — survives. provenance/sources, the legacy db_fk/
# inferred/manual/schema_fk flags, and (by default) nullable/default/extra are
# all dropped to keep the token budget on MEANING, not on every column detail
# an LLM can usually infer or doesn't need. `groups` is dropped entirely: it
# is a viewer-cosmetic layout aid (which tables get drawn inside a rounded
# frame together), not schema semantics — nothing an LLM reading this digest
# would need to reason about the data model.
# ---------------------------------------------------------------------------


def _oneline(s):
    """Collapse a free-text field (comment/note text) to one line — a
    literal newline inside a bullet or heading would break the Markdown
    structure (turn one bullet into what looks like two, or one table
    heading into a heading plus stray body text)."""
    return ' '.join(s.split())


def _notes_by_scope(notes_data):
    """Split canonical `notes` (already id-sorted by emit.py's
    _canonical_notes) into (global list, {table -> [note, ...]},
    {relation identity -> [note, ...]}). A relation identity is
    `(source_table, type, name, foreign_key, through, polymorphic)` — exactly
    the fields a resolved relation note carries (providers.py's
    resolve_and_validate_notes) — so a table's Rel: line can look its own
    associations up against this dict in O(1) instead of re-scanning every
    note per association."""
    global_notes = []
    table_notes = {}
    relation_notes = {}
    for n in notes_data or []:
        if n['scope'] == 'global':
            global_notes.append(n)
        elif n['scope'] == 'table':
            table_notes.setdefault(n['table'], []).append(n)
        else:  # relation
            key = (n['source_table'], n['type'], n.get('name'), n.get('foreign_key'),
                   n.get('through'), bool(n.get('polymorphic')))
            relation_notes.setdefault(key, []).append(n)
    return global_notes, table_notes, relation_notes


def _note_text(n):
    """One note -> a short inline rendering: `title: text` when titled, else
    bare `text`. Links are never rendered here (in verbose mode either) — a
    URL is low value per token for an LLM reader and the digest already
    keeps notes themselves regardless of --digest-verbose (G-1's verbosity
    knob is about column metadata density, not about which notes survive)."""
    text = _oneline(n['text'])
    return f'{n["title"]}: {text}' if n.get('title') else text


def _fk_targets(associations):
    """`{foreign_key_column -> target_table}` for every single-column FK
    association on a table, so each column line can show `fk→<target>`
    without re-scanning associations per column. Association `foreign_key`
    is always single-column (AssociationFragment contract, header.py §4.2);
    on a rare duplicate the deterministically-last (canonical order) wins —
    harmless, since this is an informational cross-reference, not identity."""
    return {a['foreign_key']: a['target'] for a in associations if a.get('foreign_key')}


def _render_column(c, fk_targets, verbose):
    """One canonical column -> its digest bullet line:
    `- name: type[, pk][, fk→target][, null][, default=...][, sql_type][, "comment"]`.
    nullable/default/sql_type only show under --digest-verbose (G-1) —
    dropped by default to keep the per-column token cost to what's needed to
    reconstruct the shape of the table, not every DB-level nuance."""
    bits = [c.get('type', '')]
    if c.get('primary'):
        bits.append('pk')
    target = fk_targets.get(c['name'])
    if target:
        bits.append(f'fk→{target}')
    if verbose:
        if c.get('nullable'):
            bits.append('null')
        if c.get('default'):
            bits.append(f'default={c["default"]}')
        if c.get('sql_type'):
            bits.append(c['sql_type'])
    comment = c.get('comment')
    if comment:
        bits.append(f'"{_oneline(comment)}"')
    return f'- {c["name"]}: ' + ', '.join(bits)


def _assoc_token(a):
    """One canonical association -> its compact Rel: token:
    `type target[ as name][ fk=foreign_key][ through X][ (poly)]`. `as name`
    is included only when the association name differs from its target
    (e.g. belongs_to :user on target `users` needs no `as`; a polymorphic
    belongs_to whose target IS the synthetic association name never adds
    one either, since target already equals name in that case)."""
    bits = [a['type'], a['target']]
    name = a.get('name')
    if name and name != a['target']:
        bits.append(f'as {name}')
    if a.get('foreign_key'):
        bits.append(f'fk={a["foreign_key"]}')
    if a.get('through'):
        bits.append(f'through {a["through"]}')
    if a.get('polymorphic'):
        bits.append('(poly)')
    return ' '.join(bits)


def _relation_note_key(a, table_name):
    return (table_name, a['type'], a.get('name'), a.get('foreign_key'),
            a.get('through'), bool(a.get('polymorphic')))


def _render_rel_line(table_name, associations, relation_notes):
    """The table's one-line association summary (spec §2's `Rel:` line),
    each association compressed to _assoc_token and any relation note(s)
    that resolve to it appended as `— "note text"`. Omitted entirely (returns
    None) when the table has no associations — no reason to spend a line on
    an empty summary."""
    if not associations:
        return None
    tokens = []
    for a in associations:
        token = _assoc_token(a)
        notes = relation_notes.get(_relation_note_key(a, table_name))
        if notes:
            token += ' — ' + '; '.join(f'"{_note_text(n)}"' for n in notes)
        tokens.append(token)
    return 'Rel: ' + ', '.join(tokens)


def render_digest(schema, title=None, verbose=False):
    """Render a canonical `schema` (emit.py's canonical_schema shape:
    {tables, notes?, groups?}) to the --emit-digest Markdown. Pure and
    deterministic: the same schema always renders the same text, since every
    input list is already canonically ordered (emit.py) and this function
    adds no further data-dependent ordering of its own — only `sorted(tables)`
    for the one thing canonical_schema does NOT itself sort (its `tables` is
    a dict keyed by name, in whatever order the caller built it; ordering it
    here, rather than relying on json.dumps(sort_keys=True) the way
    --emit-json/--emit-config's file writers do, is what makes THIS text
    itself byte-deterministic, not just a JSON encoding of it)."""
    global_notes, table_notes, relation_notes = _notes_by_scope(schema.get('notes'))
    tables = schema.get('tables', {})
    names = sorted(tables)

    lines = [f'# {title} — schema digest' if title else '# Schema digest', '']
    if global_notes:
        lines.append('\n\n'.join(_note_text(n) for n in global_notes))
        lines.append('')
    lines.append(f'## Tables ({len(names)})')
    lines.append('')

    for name in names:
        t = tables[name]
        heading = f'### {name}'
        comment = t.get('comment')
        if comment:
            heading += f'  — {_oneline(comment)}'
        lines.append(heading)
        for n in table_notes.get(name, []):
            lines.append(f'_{_note_text(n)}_')
        fk_targets = _fk_targets(t.get('associations', []))
        for c in t.get('columns', []):
            lines.append(_render_column(c, fk_targets, verbose))
        rel_line = _render_rel_line(name, t.get('associations', []), relation_notes)
        if rel_line:
            lines.append(rel_line)
        lines.append('')

    return '\n'.join(lines).rstrip('\n') + '\n'


def emit_digest_document(tables, notes_data, groups_data, title=None, verbose=False):
    """Build the --emit-digest document (as a Markdown string): project the
    final merged IR through the SAME canonical_schema --emit-json/--emit-config
    already share, then render it. `groups_data` is accepted (mirroring the
    other two emitters' signatures, and cli.py already has it in hand at the
    same call site) but intentionally unused here — see the module docstring
    on why groups carry no schema meaning for a digest."""
    schema = canonical_schema(tables, notes_data, groups_data)
    return render_digest(schema, title=title, verbose=verbose)
# ---------------------------------------------------------------------------
# --emit-dbml — minimal DBML export of the schema (backlog #5). Reuses emit.py's
# canonical_schema (same allowlist/pruning/deterministic order --emit-json/
# --emit-config/--emit-digest already share) — render_dbml never re-derives
# what survives or how it's ordered, only how it's RENDERED as DBML text.
#
# This is deliberately the EXPORT half only (DBML *input* — parsing a .dbml
# file into the IR — is a separate, later piece of work; building export
# first fixes the IR<->DBML mapping so a future importer can reuse it).
# The MINIMAL version covers tables/columns/primary keys/indexes/single-
# column-FK relations (`Ref:`)/table comments ONLY. Notes, groups, and
# DBML's own `Project`/`TableGroup` blocks are explicitly OUT OF SCOPE this
# round (a later "extension" phase) — `notes`/`groups` are present in the
# canonical schema input but intentionally ignored/unused here, mirroring
# how digest.py's emit_digest_document accepts-but-ignores `groups_data`.
#
# THE LOSSY CONTRACT — read this before touching Ref generation:
#
#   Only `belongs_to` associations with a truthy `foreign_key` ever produce a
#   `Ref:` line. This is narrower than it looks, and deliberately so. Across
#   every provider in this codebase (db/base.py, frameworks/rails.py,
#   frameworks/prisma.py, frameworks/django.py), a `belongs_to`'s
#   `foreign_key` ALWAYS names a column on that association's own/declaring
#   table — every provider agrees on this, so it's always safe to render
#   `Ref: <this table>.<fk> > <target>.<target pk>`.
#
#   `has_one`'s `foreign_key` is NOT safe the same way: it is AMBIGUOUS across
#   providers. db/base.py and frameworks/prisma.py both auto-label a
#   same-table unique FK column as `has_one` (for them, `foreign_key` is
#   still a column on the DECLARING table, exactly like belongs_to) — but
#   frameworks/rails.py's hand-written `has_one :profile, foreign_key: :xyz`
#   scan means the OPPOSITE: `xyz` names a column on the OTHER (target)
#   table, not this one. Once an association reaches canonical_schema there
#   is no remaining signal for which provider produced a given `has_one`, so
#   a `has_one`'s `foreign_key` cannot be safely resolved to a `Ref:` without
#   risking a Ref that points at a column that doesn't exist on the source
#   table (silently WRONG DBML, not just incomplete). So `has_one`/
#   `has_many`/`has_and_belongs_to_many` are excluded from Ref generation
#   ENTIRELY — only ever `belongs_to`. Do not "fix" this by adding `has_one`
#   back in; it would ship a wrong Ref for Rails schemas.
#
#   A polymorphic `belongs_to` is skipped silently (no warning) — its
#   `target` is a synthetic, tableless placeholder (canonical_schema keeps it
#   in the association list per its own docstring so the details pane still
#   has something to show), and there is no real table to point a Ref at.
#
#   The Ref's target column is resolved as the target table's own SOLE
#   primary-key column (scanned from the target's canonical `columns`, never
#   a `primary_key` field — canonical_schema's shape has no such field
#   anyway). A target with zero or 2+ primary columns (no PK, or a composite
#   PK) can't be expressed as a single-column Ref in this minimal version —
#   that relation is skipped, WITH a warning printed to stderr (never a hard
#   failure: a skip, not an error).
#
#   Column `default:` values are always rendered as either a bare number or a
#   single-quoted string — there is no SQL-expression / backtick-expr
#   special case. This is a deliberate simplification, not an oversight: a
#   default like `CURRENT_TIMESTAMP` renders as `default: 'CURRENT_TIMESTAMP'`
#   (a quoted string), which is syntactically valid DBML even though it isn't
#   the most idiomatic rendering of a SQL expression default.
#
# Pure and non-destructive: render_dbml/emit_dbml_document never mutate their
# input (canonical_schema already deep-copies; nothing here sorts in place).
# render_dbml may print `Warning: --emit-dbml: ...` lines to stderr as a side
# effect (the Ref-skip case above) — this matches how the rest of the
# pipeline already prints informational lines to stderr (cli.py's
# _run_pipeline/_finish).
# ---------------------------------------------------------------------------

_IDENT_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
_NUMERIC_DEFAULT_RE = re.compile(r'-?\d+(\.\d+)?')


def _ident(name):
    """A table/column/index-column name -> its DBML token: bare when it's a
    simple identifier, else double-quoted with internal `"` escaped."""
    if _IDENT_RE.fullmatch(name):
        return name
    return '"' + name.replace('"', '\\"') + '"'


def _sq(s):
    """Single-quote a string for DBML, escaping internal `'` as `\\'`. Used
    for everything EXCEPT the table Note's triple-quote path, which needs no
    escaping at all (see _render_table_note)."""
    return "'" + s.replace("'", "\\'") + "'"


def _dbml_default(val):
    """A column's `default:` attribute value: bare/unquoted when the raw
    string is purely numeric (`^-?\\d+(\\.\\d+)?$`), else single-quoted with
    `'` escaped and any embedded newline collapsed to a space (defensive —
    reuse digest.py's _oneline). No SQL-expression detection — see the module
    docstring's lossy-contract note on `default:`."""
    s = str(val)
    if _NUMERIC_DEFAULT_RE.fullmatch(s):
        return s
    return _sq(_oneline(s))


def _unique_single_columns(indexes):
    """`{column_name, ...}` for every canonical index that is both unique and
    single-column — the set a column attr's `unique` marker (attr #4) checks
    against. Intentionally NOT deduped against the indexes block: a
    single-column unique index appears both inline on its column and as a
    block entry, by design (see the module docstring / DBML_REVIEW_NOTES)."""
    return {ix['columns'][0] for ix in indexes
            if ix.get('unique') and len(ix.get('columns', [])) == 1}


def _column_attrs(c, is_sole_pk, unique_single_cols):
    """One column's DBML attrs, comma-joined-ready list, in the fixed order:
    pk (sole single-column PK only) / increment / not null / unique /
    default. Composite PKs never contribute a per-column `pk` here — they go
    in the table's indexes block instead (_composite_pk_columns)."""
    attrs = []
    if is_sole_pk:
        attrs.append('pk')
    if 'auto_increment' in (c.get('extra') or '').lower():
        attrs.append('increment')
    if not c.get('nullable', False):
        attrs.append('not null')
    if c['name'] in unique_single_cols:
        attrs.append('unique')
    default = c.get('default')
    if default:
        attrs.append(f'default: {_dbml_default(default)}')
    return attrs


def _render_dbml_column(c, sole_pk_name, unique_single_cols):
    """One canonical column -> its DBML line: `  <ident> <type>[ [attrs]]`.
    `type` prefers `sql_type` (raw DB type, fidelity) and falls back to the
    coarse `type` shorthand — printed bare/unquoted even when it contains an
    internal space (e.g. `int unsigned`), since DBML column types are bare
    tokens, not string literals. Named _render_dbml_column (not
    _render_column) to avoid colliding with digest.py's own _render_column —
    both files are concatenated into one flat module by build_single_file.py,
    so a same-named function here would silently shadow digest.py's."""
    ctype = c.get('sql_type') or c.get('type', '')
    attrs = _column_attrs(c, c['name'] == sole_pk_name, unique_single_cols)
    line = f"  {_ident(c['name'])} {ctype}"
    if attrs:
        line += ' [' + ', '.join(attrs) + ']'
    return line


def _composite_pk_columns(columns):
    """Column names individually flagged `primary: true`, in COLUMN ORDER —
    the SAME composite-PK detection rule emit.py's _config_table already
    established (P1/E-2): read from column flags, never from a `primary_key`
    field (canonical_schema's shape has no such field anyway). Returns the
    full list regardless of length; callers decide what 0/1/2+ means."""
    return [c['name'] for c in columns if c.get('primary')]


def _render_indexes_block(primary_cols, indexes):
    """The table's `indexes { ... }` sub-block as a list of lines (unindented
    at the `indexes {`/`}` level — the caller indents nothing further), or
    None when there's neither a composite PK (2+ primary columns) nor any
    index at all. The composite-PK line (if any) is always FIRST, then every
    entry of `indexes` in its already-canonical (not re-sorted) order."""
    if len(primary_cols) < 2 and not indexes:
        return None
    lines = ['  indexes {']
    if len(primary_cols) >= 2:
        cols = ', '.join(_ident(c) for c in primary_cols)
        lines.append(f'    ({cols}) [pk]')
    for ix in indexes:
        cols = ', '.join(_ident(c) for c in ix.get('columns', []))
        attrs = []
        if ix.get('unique'):
            attrs.append('unique')
        if ix.get('name'):
            attrs.append(f"name: {_sq(ix['name'])}")
        line = f'    ({cols})'
        if attrs:
            line += ' [' + ', '.join(attrs) + ']'
        lines.append(line)
    lines.append('  }')
    return lines


def _render_table_note(comment):
    """The table's `  Note: ...` line, or None when there's no comment.
    A single-line comment is single-quoted with `'` escaped
    (`  Note: 'text'`); a comment containing a newline instead uses DBML's
    triple-quote form (`  Note: '''text'''`, unescaped) — same reasoning
    emit.py's config_yaml_text already applied to multi-line note/comment
    text (literal block style there, triple-quote here), for the same
    "don't mangle a paragraph of free text" reason."""
    if not comment:
        return None
    if '\n' in comment:
        return f"  Note: '''{comment}'''"
    return f'  Note: {_sq(comment)}'


def _render_table(name, t):
    """One canonical table -> its full `Table ... { ... }` block, as a list
    of lines. Blank-line separators appear only before a section that's
    actually present: columns, then (blank +) indexes block if any, then
    (blank +) Note if any."""
    columns = t.get('columns', [])
    primary_cols = _composite_pk_columns(columns)
    sole_pk_name = primary_cols[0] if len(primary_cols) == 1 else None
    indexes = t.get('indexes', [])
    unique_single_cols = _unique_single_columns(indexes)

    lines = [f'Table {_ident(name)} {{']
    for c in columns:
        lines.append(_render_dbml_column(c, sole_pk_name, unique_single_cols))

    idx_block = _render_indexes_block(primary_cols, indexes)
    if idx_block:
        lines.append('')
        lines.extend(idx_block)

    note_line = _render_table_note(t.get('comment'))
    if note_line:
        lines.append('')
        lines.append(note_line)

    lines.append('}')
    return lines


def _collect_refs(tables):
    """Every `Ref:` tuple `(source_table, foreign_key, target_table,
    target_pk_column)` this schema resolves to, sorted by that same tuple for
    deterministic output. See the module docstring for the full lossy-
    contract reasoning; in short: only `belongs_to` + truthy `foreign_key`,
    never `has_one`/`has_many`/`has_and_belongs_to_many`; a polymorphic
    belongs_to is skipped silently; a target with no single-column PK is
    skipped with a stderr warning. Dedups defensively on
    (source_table, foreign_key, target_table), keeping only the first
    occurrence, should a duplicate somehow occur."""
    seen = set()
    kept = []
    for name in sorted(tables):
        t = tables[name]
        for a in t.get('associations', []):
            if a.get('type') != 'belongs_to' or not a.get('foreign_key'):
                continue
            if a.get('polymorphic'):
                continue  # synthetic tableless target — nothing real to reference
            target = a['target']
            fk = a['foreign_key']
            key = (name, fk, target)
            if key in seen:
                continue
            seen.add(key)
            target_table = tables[target]  # guaranteed present: canonical_schema
                                            # already pruned any dangling non-poly target
            target_primary = _composite_pk_columns(target_table.get('columns', []))
            if len(target_primary) != 1:
                print(f'Warning: --emit-dbml: skipping Ref {name}.{fk} -> {target} '
                      '(target has no single-column primary key)', file=sys.stderr)
                continue
            kept.append((name, fk, target, target_primary[0]))
    kept.sort()
    return kept


def render_dbml(schema):
    """Render a canonical `schema` (emit.py's canonical_schema shape:
    {tables, notes?, groups?}) to minimal DBML text. `notes`/`groups`, even
    when present, are never read — out of scope this round (see module
    docstring). Pure and deterministic: the same schema always renders the
    same text, since canonical_schema's tables/columns/indexes/associations
    are already canonically ordered and this function sorts tables by name
    itself (the one thing canonical_schema does not sort, since `tables` is a
    plain name-keyed dict) rather than relying on any caller-side ordering."""
    tables = schema.get('tables', {})
    names = sorted(tables)

    lines = []
    for i, name in enumerate(names):
        if i:
            lines.append('')
        lines.extend(_render_table(name, tables[name]))

    refs = _collect_refs(tables)
    if refs:
        lines.append('')
        for source, fk, target, target_pk in refs:
            lines.append(f'Ref: {_ident(source)}.{_ident(fk)} > {_ident(target)}.{_ident(target_pk)}')

    return '\n'.join(lines) + '\n'


def emit_dbml_document(tables, notes_data, groups_data):
    """Build the --emit-dbml document: project the final merged IR through
    the SAME canonical_schema --emit-json/--emit-config/--emit-digest already
    share, then render it. `notes_data`/`groups_data` are accepted (mirroring
    emit_digest_document's signature — cli.py already has both in hand at the
    same call site, and keeping all four emit-family builders' signatures
    uniform is simpler than special-casing this one) but intentionally
    unused: this export's scope stops at tables/columns/indexes/single-
    column-FK Refs/table comments (see module docstring)."""
    schema = canonical_schema(tables, notes_data, groups_data)
    return render_dbml(schema)
def main():
    p = argparse.ArgumentParser(
        description='Generate an interactive ER diagram (and optional Excel table definitions) '
                    'from a MySQL / PostgreSQL / SQLite database, application code '
                    '(Rails / Prisma / Django), and/or a config schema — any one source is enough')
    p.add_argument('database',
                   metavar='mysql://user@host/db | postgres://user@host/db | sqlite:///file.db',
                   nargs='?',
                   help='Database connection URL. postgres:// takes an optional '
                        '?schema=name (default public); sqlite:///path/to/app.db reads a '
                        'local file (no server, nothing to install). MySQL/Postgres can '
                        'also be assembled from `engine`/`host`/`port`/`user`/`database` in '
                        'the config file (no password field there — use MYSQL_PWD/PGPASSWORD, '
                        '~/.my.cnf/~/.pgpass, or the interactive prompt). A read-only '
                        'account is recommended. Or pass the literal word "demo" to try '
                        'erdscope instantly against a bundled sample database — no database '
                        'of your own needed')
    # SUPPRESS on every config-mirrorable flag so we can tell "explicitly
    # passed on the CLI" (attribute present) from "left to the config file /
    # built-in default" (attribute absent) — see the merge loop below.
    p.add_argument('-o', '--output', default=argparse.SUPPRESS,
                   help='Output HTML file (default: erd.html)')
    p.add_argument('--models', metavar='PATH', action='append', default=argparse.SUPPRESS,
                   help='Merge association semantics parsed from application code '
                        '(Rails project/app/models dir, schema.prisma, or Django project). '
                        'Repeatable to merge several frameworks; later ones win on ties')
    p.add_argument('--adapter', metavar='PATH', action='append', default=argparse.SUPPRESS,
                   help='Load a Python plugin file that registers a custom database '
                        'adapter (subclass DBAdapter + @register_adapter) and/or a '
                        'framework overlay (subclass FrameworkOverlay + @register_overlay). '
                        'The new URL scheme / --models project kind then works like the '
                        'built-ins. Repeatable; also settable as config `adapters`')
    p.add_argument('--excel', metavar='FILE.xlsx', default=argparse.SUPPRESS,
                   help='Also write a table-definition workbook '
                        '(overview sheet + one sheet per table)')
    p.add_argument('--emit-json', metavar='FILE.json', default=argparse.SUPPRESS,
                   help='Also write a canonical JSON schema snapshot (with provenance and '
                        'a content fingerprint) alongside the HTML; use - for stdout. The '
                        'HTML is still generated')
    p.add_argument('--emit-config', metavar='FILE.(yml|yaml|json)', default=argparse.SUPPRESS,
                   help='Also write the final merged schema as a config-authoring file, '
                        're-importable via --config for a semantically-equivalent (not '
                        'byte-identical) round trip; dispatches on extension — .yml/.yaml '
                        'for YAML (needs PyYAML installed) or .json for JSON; use - for '
                        'stdout, which is always JSON. The HTML is still generated')
    p.add_argument('--emit-digest', metavar='FILE.md', default=argparse.SUPPRESS,
                   help='Also write a token-efficient Markdown digest of the schema '
                        '(with design notes) for LLMs/agents, alongside the HTML; use '
                        '- for stdout. Drops provenance and (by default) nullable/'
                        'default/sql_type — see --digest-verbose')
    p.add_argument('--digest-verbose', action='store_true',
                   help='With --emit-digest, also include nullable/default/sql_type '
                        'per column (omitted by default to keep the digest small)')
    p.add_argument('--emit-dbml', metavar='FILE.dbml', default=argparse.SUPPRESS,
                   help='Also write a minimal DBML export of the schema '
                        '(tables/columns/indexes/single-column-FK relations/table '
                        'comments) alongside the HTML; use - for stdout. Does not '
                        'include notes/groups/TableGroup (deferred)')
    p.add_argument('--excel-template', metavar='FILE.xlsx', default=argparse.SUPPRESS,
                   help="Override the workbook's colors/fonts/borders from a template "
                        '.xlsx — see excel-template.xlsx and its Styles sheet for the '
                        '5-cell contract (default: built-in styling)')
    p.add_argument('--diff', metavar='SNAPSHOT.json', default=None,
                   help='Compare this run against a previously-saved --emit-json snapshot '
                        'instead of generating any output (CLI-only, not a config key). '
                        'level1 comparison — materially the same schema, not byte-identical '
                        '(see --emit-config). added = present in this run only; removed = '
                        'present in the snapshot only. Exits 0 if identical, 1 if different '
                        '(see --diff-exit-zero), 2 on a usage/snapshot-load error, or when '
                        'combined with --emit-json/--emit-config/--excel. Note: an empty-'
                        'string default and no default at all are never distinguished by '
                        'any provider, so this cannot detect that specific change (a known '
                        'level1 limit)')
    p.add_argument('--diff-provenance', action='store_true',
                   help='With --diff, also compare association provenance/sources '
                        '(ignored by default)')
    p.add_argument('--diff-exit-zero', action='store_true',
                   help='With --diff, exit 0 even when a difference is found (a load/usage '
                        'error still exits 2)')
    p.add_argument('--diff-format', choices=('text', 'json'), default='text',
                   help='With --diff, render the difference as human-readable text '
                        '(default) or as deterministic JSON')
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
    p.add_argument('--no-open', action='store_true',
                   help='Skip automatically opening a browser after generating. Only '
                        'relevant to `erdscope demo` (which opens one by default); '
                        'accepted but has no effect on a normal run')
    args = p.parse_args()

    if args.database == 'demo':
        run_demo(args)
        return

    _run_pipeline(args)

def _emit_config_format(val):
    """Extension dispatch for --emit-config: '-' (stdout) and a `.json` path
    -> 'json'; `.yml`/`.yaml` -> 'yaml' (PyYAML required — checked here too,
    hard error, no silent JSON fallback, since a script asking for YAML would
    otherwise get a different format with no indication). Anything else is a
    fatal usage error (an unrecognized/typo'd extension).

    Single source of truth for this dispatch, called from TWO places: once at
    the very top of _run_pipeline (fail-fast, before the DB layer is even
    built — a typo'd extension or a missing PyYAML shouldn't cost the user a
    slow DB introspection just to find out) and again from _finish (which
    actually needs the format value to build the document). Calling it twice
    is cheap and keeps the two call sites from ever disagreeing about what
    counts as valid."""
    if val == '-':
        return 'json'
    suffix = Path(val).suffix.lower()
    if suffix in ('.yml', '.yaml'):
        fmt = 'yaml'
    elif suffix == '.json':
        fmt = 'json'
    else:
        sys.exit(f'Error: --emit-config {val!r} must end in .yml, '
                 '.yaml, or .json (or be "-" for stdout, which is always JSON)')
    if fmt == 'yaml':
        try:
            import yaml  # noqa: F401 — existence check only; config_yaml_text imports it again
        except ImportError:
            sys.exit(f'Error: --emit-config {val} is YAML but PyYAML is '
                     'not installed (pip install pyyaml, or use a .json extension '
                     'instead)')
    return fmt

def _diff_fail(message):
    """--diff's usage/snapshot-load error path: prints `Error: {message}` to
    stderr and exits 2 — distinct from every other CLI error in this file
    (which use `sys.exit(f'Error: ...')`, always exit code 1) because the
    --diff spec reserves 1 for "ran fine, found a difference" and 2 for "the
    comparison itself couldn't run". `sys.exit(str)` can't produce a
    non-1 code, hence the explicit print + sys.exit(2) instead of that
    shorthand."""
    print(f'Error: {message}', file=sys.stderr)
    sys.exit(2)

def _run_pipeline(args):
    """The actual generate pipeline, shared by a normal run and `erdscope
    demo` (run_demo, in demo.py, rewrites args.database to a temp sqlite URL
    and forces config off, then calls straight in here)."""
    # Fail-fast (backlog #1 follow-up): validate --emit-config's extension/
    # PyYAML-availability up front, before the config file is even loaded or
    # the DB layer is built — a typo'd extension or missing PyYAML used to
    # only surface in _finish, AFTER a possibly-slow DB introspection. Both
    # `erdscope demo` (run_demo rewrites args.database and calls straight into
    # here) and a normal run funnel through this one function, so this single
    # check covers both. _finish reuses the SAME helper (not a second copy of
    # this logic) when it actually builds the document.
    if getattr(args, 'emit_config', None) is not None:
        _emit_config_format(args.emit_config)
    config = load_config(args)
    # Load any custom DB adapters (--adapter / config `adapters`) before the URL
    # is classified, so their schemes are registered in time. Config entries
    # first, then CLI ones — a later entry overriding a scheme wins (§ plugins).
    cfg_adapters = config.get('adapters') or []
    if isinstance(cfg_adapters, str):
        cfg_adapters = [cfg_adapters]
    adapter_paths = list(cfg_adapters) + list(getattr(args, 'adapter', []) or [])
    if adapter_paths:
        load_adapter_plugins(adapter_paths)
    url = args.database or assemble_config_url(config)
    # DB is optional now (§10): a schema can also come from --models and/or
    # config.tables. Only a NON-EMPTY url with an unrecognized scheme is an
    # error (a mistyped/wrong argument); a missing url just skips the DB layer.
    engine_name = None
    if url:
        scheme = url.split('://', 1)[0]
        adapter_cls = db_adapter_for(scheme)
        if adapter_cls is None:
            known = ', '.join(f'{s}://' for s in sorted(DB_ADAPTERS))
            sys.exit(f'Error: unrecognized database URL scheme {scheme!r} (known: {known}). '
                     'Pass it as the CLI argument, or set `database` (and optionally engine/'
                     'host/user/port) in the config file, e.g. mysql://readonly@127.0.0.1:3306/'
                     'myapp, postgres://readonly@127.0.0.1:5432/myapp, or sqlite:///./app.db. '
                     'A custom scheme needs its --adapter plugin. Or run with no database at '
                     'all by supplying --models and/or a config file with a `tables:` section')
        engine_name = adapter_cls.label or adapter_cls.name or scheme

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
    config_notes = config.get('notes')       # shape already validated by load_config()
    config_groups = config.get('groups')     # shape already validated by load_config()
    cfg_label = str(args.config) if getattr(args, 'config', None) else 'config'
    cfg_location = str(args.config) if getattr(args, 'config', None) else None

    config_sources = config.get('sources') or []  # shape already validated by load_config()

    # ── valid-input check (§10): at least one SCHEMA source (DB / Framework /
    #    config.tables / config.sources). relations alone is not a source — it
    #    needs a base. ──
    if not (url or models_list or config_tables or config_sources):
        sys.exit('Error: no schema input. Provide at least one of: a database URL '
                 '(mysql:// or postgres://) as the argument or config `database`; '
                 '--models pointing at a Rails/Prisma/Django project; a config `sources` '
                 'entry; or a config file with a `tables:` section.')

    # ── collect provider layers, low→high spec priority, then merge (§3) ──
    layers = []
    db_result = None
    if url:  # only build/connect the DB layer when a url is present (no
             # connection and no password prompt otherwise — §10)
        db_result = db_provider(url)
        print(f'Fetched {len(db_result["tables"])} tables from {engine_name}', file=sys.stderr)
        layers.append(db_result)

    # Code-source inputs (framework `--models`/config `models`, and config
    # `sources` — rails.schema, `<overlay>.models`, the rails.project macro)
    # normalize to one deterministic InputSpec order — config `sources`
    # (declared order, macros expanded) then legacy `models` entries (D3/D4) —
    # then dispatch through the source-type registry. Cross-kind priority still
    # comes from _PHYSICAL_RANK/_LOGICAL_RANK, not list order, so a schema
    # layer listed before a framework layer still resolves ties correctly.
    specs = normalize_input_specs(models_list, config_sources)
    layers += run_input_specs(specs, args.table_map)
    fw_root = specs[0]['path'] if specs else None  # first spec drives the title fallback (§10)

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

    # notes Phase 1 (Sol findings #1/#3): resolution/semantic-validation now
    # happens INSIDE _finish, after its --infer-fk step has added any inferred
    # relations to `tables` — so a note can target one (finding #3) — and its
    # result is filtered down to the tables that survive --only/--exclude
    # (finding #1). Pass the RAW config `notes:` (unresolved) plus a label;
    # `_finish` resolves them itself against its own final IR.
    _finish(tables, args, _resolve_title(config, url, fw_root, getattr(args, 'output', None)),
            notes=config_notes, notes_label=cfg_label,
            groups=config_groups, groups_label=cfg_label)

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

def _finish(tables, args, title_name, notes=None, notes_label='config',
            groups=None, groups_label='config'):
    """Shared tail: FK inference, notes resolution, --only/--exclude filtering,
    HTML generation.

    `notes` (notes Phase 1) is now the RAW config `notes:` list (or None/empty)
    — resolved/semantically-validated HERE (Sol finding #3: AFTER --infer-fk,
    so a note may target a relation --infer-fk adds) and then filtered down to
    the tables that survive --only/--exclude (Sol finding #1: an excluded
    table's design notes must not leak into the HTML). `notes_label` names the
    config source for error messages (mirrors _run_pipeline's cfg_label; the
    demo passes 'demo'). Still never None-but-empty-list vs absent-key
    ambiguity in the output: an empty/None result omits the DATA_JSON `notes`
    key entirely, keeping the demo and every pre-Phase-1 config byte-identical
    to today's output.

    `groups` (groups Phase 1) is the RAW config `groups:` list (or None/empty),
    mirroring `notes` end to end: resolved/validated here against the same
    final IR, then its members filtered down to the tables that survive
    --only/--exclude (a group that loses every member that way is dropped
    entirely, rather than shipping an empty frame). `groups_label` mirrors
    `notes_label`. An empty/None result omits the DATA_JSON `groups` key
    entirely — same byte-equality guarantee as notes."""
    if getattr(args, 'infer_fk', False):
        inferred = infer_fk_associations(tables)
        if inferred:
            print(f'Inferred {inferred} relations from *_id columns', file=sys.stderr)

    # notes Phase 1: semantic validation + viewer resolution against the FINAL
    # IR — deliberately AFTER infer_fk (Sol finding #3) so a note can target an
    # inferred relation, and deliberately BEFORE --only/--exclude filtering
    # below so validation always sees the complete final IR (an excluded
    # table's note is still validated, just filtered out of the output after).
    notes_data = resolve_and_validate_notes(notes, tables, notes_label) if notes else None

    # groups Phase 1: semantic validation + viewer resolution against the
    # SAME final IR notes just validated against — before --only/--exclude,
    # for the same reason (validation always sees the complete schema; the
    # filtering below trims membership down after).
    groups_data = resolve_and_validate_groups(groups, tables, groups_label) if groups else None

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

    # Sol finding #1: drop table/relation notes whose table(s) didn't survive
    # --only/--exclude — their design info must not leak into the HTML for a
    # table that's no longer in it. A `relation` note ships only when BOTH its
    # endpoint tables survive (Sol re-review #2): filtering on `source_table`
    # alone would keep an `orders -> users` note — note body and `target:
    # users` and all — in an HTML that `--only orders` excluded `users` from.
    # `global` notes are diagram-wide (legend/overview), not tied to any one
    # table, so they always survive. A no-op when --only/--exclude weren't
    # passed: `tables` is then the unfiltered set, so every endpoint is present.
    #
    # Sol relaxation #3 (second half): a polymorphic relation note's `target`
    # is a SYNTHETIC, tableless name (see validate_config_references above) —
    # it can never be "in tables" the way a real target can, so requiring
    # that for a polymorphic note would silently drop every one of them.
    # Source survival is still required (the note is meaningless with no
    # source table left to attach to); the target check is skipped instead.
    if notes_data:
        notes_data = [n for n in notes_data
                      if n['scope'] == 'global'
                      or (n['scope'] == 'table' and n['table'] in tables)
                      or (n['scope'] == 'relation' and n['source_table'] in tables
                          and (n.get('polymorphic') or n['target'] in tables))]

    # groups Phase 1: narrow each group's membership to the tables that
    # survived --only/--exclude, then drop any group left with zero members —
    # a group frame drawn around nothing would be a bug in the viewer, not a
    # feature. A no-op when --only/--exclude weren't passed, same as notes.
    if groups_data:
        groups_data = [{**g, 'tables': [t for t in g['tables'] if t in tables]}
                       for g in groups_data]
        groups_data = [g for g in groups_data if g['tables']]

    # --diff (backlog #2): the CI drift gate. Same emit point as --emit-json/
    # --emit-config below (after notes/groups resolution and --only/--exclude
    # filtering, before the §9.3 serialize boundary that would replace
    # provenance with legacy flags), but a diff run is a COMPARISON MODE: it
    # never reaches HTML/Excel/emit-json/emit-config generation at all — it
    # renders the difference against a previously-saved --emit-json snapshot
    # and exits here. See diff.py's module docstring for the fixed
    # left=this-run/right=snapshot direction and the level1 identity rules.
    if getattr(args, 'diff', None) is not None:
        for _flag, _val in (('--emit-json', getattr(args, 'emit_json', None)),
                            ('--emit-config', getattr(args, 'emit_config', None)),
                            ('--emit-digest', getattr(args, 'emit_digest', None)),
                            ('--emit-dbml', getattr(args, 'emit_dbml', None)),
                            ('--excel', getattr(args, 'excel', None))):
            if _val:
                _diff_fail(f'--diff cannot be combined with {_flag} '
                          '(diff is a comparison mode — it never generates output)')
        diff_path = args.diff
        try:
            diff_raw = Path(diff_path).read_text(encoding='utf-8')
        except OSError as e:
            _diff_fail(f'--diff {diff_path!r}: {e.strerror or e}')
        try:
            diff_doc = json.loads(diff_raw)
        except json.JSONDecodeError as e:
            _diff_fail(f'--diff {diff_path!r}: not valid JSON ({e})')
        if not (isinstance(diff_doc, dict) and diff_doc.get('format') == 1
                and isinstance(diff_doc.get('schema'), dict)
                and isinstance(diff_doc['schema'].get('tables'), dict)):
            _diff_fail(f'--diff {diff_path!r} is not a valid --emit-json snapshot '
                      '(expected {"format": 1, "fingerprint": ..., "schema": {"tables": {...}}})')
        left_schema = canonical_schema(tables, notes_data, groups_data)
        right_schema = diff_doc['schema']
        # A shape-valid but internally-malformed snapshot (an association with
        # no `type`, a note that isn't an object, ...) would raise deep inside
        # snapshot_fingerprint/schema_diff/render. Catch it and exit 2 ("the
        # comparison couldn't run") rather than let the traceback surface as
        # exit 1, which a CI gate would misread as "drift detected".
        try:
            # Fast path: recompute the snapshot's OWN fingerprint from its
            # schema — never trust the stored `fingerprint` field, since a
            # snapshot hand-edited with a now-stale fingerprint must not report
            # a false "identical". An exact match means both sides serialize to
            # the same canonical payload, so the deep compare can be skipped.
            if snapshot_fingerprint(right_schema) == snapshot_fingerprint(left_schema):
                diff_result = empty_schema_diff()
            else:
                diff_result = schema_diff(left_schema, right_schema,
                                          include_provenance=getattr(args, 'diff_provenance', False))
            rendered = (render_json(diff_result) if args.diff_format == 'json'
                       else render_text(diff_result))
        except Exception as e:
            _diff_fail(f'--diff {diff_path!r}: snapshot is malformed ({e!r})')
        sys.stdout.write(rendered)
        if diff_is_empty(diff_result):
            sys.exit(0)
        sys.exit(0 if getattr(args, 'diff_exit_zero', False) else 1)

    # Guard: two file outputs must not resolve to the same path, or the second
    # write silently clobbers the first — `-o x.json --emit-json x.json` would
    # overwrite the HTML with the JSON, and `--emit-json y.xlsx --excel y.xlsx`
    # would overwrite the JSON with the workbook. stdout ('-') never collides.
    _seen_out = {}
    for _flag, _val in (('-o/--output', getattr(args, 'output', None)),
                        ('--emit-json', getattr(args, 'emit_json', None)),
                        ('--emit-config', getattr(args, 'emit_config', None)),
                        ('--emit-digest', getattr(args, 'emit_digest', None)),
                        ('--emit-dbml', getattr(args, 'emit_dbml', None)),
                        ('--excel', getattr(args, 'excel', None))):
        if not _val or _val == '-':
            continue
        _rp = Path(_val).resolve()
        if _rp in _seen_out:
            sys.exit(f'Error: {_seen_out[_rp]} and {_flag} both write to {_val!r}; '
                     'use distinct output paths')
        _seen_out[_rp] = _flag

    # --emit-json (backlog #0): built HERE, before the §9.3 serialize boundary
    # below replaces `tables`' structured provenance/sources with the legacy
    # boolean flags — canonical_schema wants the provenance-preserving shape,
    # already narrowed by --only/--exclude (same `tables` the HTML/Excel
    # outputs below are about to consume). Non-destructive: emit_json_document
    # deep-copies internally, so this never affects the HTML/Excel below.
    emit_json_doc = (emit_json_document(tables, notes_data, groups_data)
                     if getattr(args, 'emit_json', None) is not None else None)

    # --emit-config (backlog #1): same provenance-preserving-IR timing as
    # --emit-json above (built before serialize_for_viewer). Extension
    # dispatch + PyYAML-availability (Sol relaxation #6) is delegated to
    # _emit_config_format — the SAME helper _run_pipeline already called
    # fail-fast, before the DB layer was even built, so a bad extension or
    # missing PyYAML never reaches this far; this call is just getting the
    # dispatch result back, not re-deciding anything.
    emit_config_val = getattr(args, 'emit_config', None)
    emit_config_fmt = _emit_config_format(emit_config_val) if emit_config_val is not None else None
    emit_config_text = None
    if emit_config_val is not None:
        emit_config_doc = config_document(tables, notes_data, groups_data, title=title_name)
        emit_config_text = (config_yaml_text(emit_config_doc) if emit_config_fmt == 'yaml'
                            else config_json_text(emit_config_doc))

    # --emit-digest (backlog #3): same provenance-preserving-IR timing as
    # --emit-json/--emit-config above (built before serialize_for_viewer) —
    # render_digest reads notes/associations straight off canonical_schema,
    # same as the other two emitters.
    emit_digest_val = getattr(args, 'emit_digest', None)
    emit_digest_text = (emit_digest_document(tables, notes_data, groups_data, title=title_name,
                                             verbose=getattr(args, 'digest_verbose', False))
                        if emit_digest_val is not None else None)

    # --emit-dbml (backlog #5): same provenance-preserving-IR timing as the
    # other three emit-family builders above (built before serialize_for_viewer)
    # — render_dbml reads columns/indexes/associations straight off
    # canonical_schema, same as --emit-json/--emit-config/--emit-digest.
    emit_dbml_val = getattr(args, 'emit_dbml', None)
    emit_dbml_text = (emit_dbml_document(tables, notes_data, groups_data)
                      if emit_dbml_val is not None else None)

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
    payload = {'tables': tables}
    if notes_data:  # omit the key entirely when empty/None — demo byte-equality (§10.1)
        payload['notes'] = notes_data
    if groups_data:  # same byte-equality guarantee as notes
        payload['groups'] = groups_data
    data_json = json.dumps(payload, ensure_ascii=False).replace('</', '<\\/')
    html = (HTML_TEMPLATE
            .replace('__MAX_ROWS__', str(args.max_rows))
            .replace('__TITLE__', f'{title_name} — ERD')
            .replace('__DATA_JSON__', data_json))

    out = Path(args.output)
    out.write_text(html, encoding='utf-8')
    print(f'Generated: {out} ({out.stat().st_size // 1024} KB)', file=sys.stderr)

    if emit_json_doc is not None:
        if args.emit_json == '-':
            sys.stdout.write(emit_json_doc)
        else:
            Path(args.emit_json).write_text(emit_json_doc, encoding='utf-8')
            print(f'Generated: {args.emit_json}', file=sys.stderr)

    if emit_config_text is not None:
        if emit_config_val == '-':
            sys.stdout.write(emit_config_text)
        else:
            Path(emit_config_val).write_text(emit_config_text, encoding='utf-8')
            print(f'Generated: {emit_config_val}', file=sys.stderr)

    if emit_digest_text is not None:
        if emit_digest_val == '-':
            sys.stdout.write(emit_digest_text)
        else:
            Path(emit_digest_val).write_text(emit_digest_text, encoding='utf-8')
            print(f'Generated: {emit_digest_val}', file=sys.stderr)

    if emit_dbml_text is not None:
        if emit_dbml_val == '-':
            sys.stdout.write(emit_dbml_text)
        else:
            Path(emit_dbml_val).write_text(emit_dbml_text, encoding='utf-8')
            print(f'Generated: {emit_dbml_val}', file=sys.stderr)

    if getattr(args, 'excel', None):
        write_excel(tables, Path(args.excel), title_name,
                    template_path=getattr(args, 'excel_template', None), notes=notes_data,
                    groups=groups_data)
        print(f'Generated: {args.excel}', file=sys.stderr)
    elif getattr(args, 'excel_template', None):
        print('Warning: --excel-template has no effect without --excel', file=sys.stderr)

if __name__ == '__main__':
    main()
