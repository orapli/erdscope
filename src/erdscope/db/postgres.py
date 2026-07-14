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
