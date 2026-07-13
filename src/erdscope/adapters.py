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
