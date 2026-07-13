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
