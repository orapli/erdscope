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
