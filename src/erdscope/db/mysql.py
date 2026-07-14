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
