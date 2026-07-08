#!/usr/bin/env python3
"""
erdscope — interactive ER diagrams (and Excel table definitions) from a live database
Usage:
    python3 erd.py mysql://readonly@host:3306/dbname [-o erd.html]
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
                          "db_fk"?, "inferred"?}],
      }
    }
"""
import ast, getpass, os, subprocess, sys, json, re, argparse
from fnmatch import fnmatch
from pathlib import Path
from urllib.parse import urlparse

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
        conn = pymysql.connect(host=host, port=port, user=u.username,
                               password=u.password or os.environ.get('MYSQL_PWD', ''),
                               database=db, charset='utf8mb4')
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                return [tuple('' if v is None else str(v) for v in r)
                        for r in cur.fetchall()]
        finally:
            conn.close()
    cmd = ['mysql', '--batch', '--raw', '--skip-column-names',
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
    return [tuple('' if v == 'NULL' else v for v in line.split('\t'))
            for line in r.stdout.splitlines()]

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
    for tname, col, ref in fk_rows:
        if tname not in tables or ref not in tables:
            continue
        name = col[:-3] if col.endswith('_id') else col
        tables[tname]['associations'].append(
            {'type': 'belongs_to', 'name': name, 'target': ref,
             'foreign_key': col, 'db_fk': True})
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
    return tables

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

def dedupe_db_fk(tables):
    """After merging code-declared associations, drop DB-FK associations for
    pairs that an explicit association already covers (either direction)."""
    explicit = set()
    for name, t in tables.items():
        for a in t['associations']:
            if not a.get('db_fk') and not a.get('inferred'):
                explicit.add((name, a['target']))
                explicit.add((a['target'], name))
    removed = 0
    for name, t in tables.items():
        kept = []
        for a in t['associations']:
            if a.get('db_fk') and (name, a['target']) in explicit:
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
def parse_models(models_dir, tables):
    if not models_dir.is_dir():
        return
    for path in sorted(models_dir.rglob('*.rb')):
        if 'concerns' in path.parts:  # separator-agnostic (works on Windows too)
            continue
        try:
            content = path.read_text(encoding='utf-8', errors='replace')
        except Exception:
            continue
        if not re.search(r'< (?:ApplicationRecord|ActiveRecord::Base)', content):
            continue
        m = re.search(r'class\s+([\w:]+)\s*<', content)
        if not m:
            continue
        class_name = m.group(1)
        tn_m = re.search(r"self\.table_name\s*=\s*['\"]([^'\"]+)['\"]", content)
        table_name = tn_m.group(1) if tn_m else class_to_table(class_name)
        if table_name not in tables:
            # model exists but the table is not in any schema file
            # (library-managed table, another database, ...)
            tables[table_name] = {'columns': [], 'associations': [],
                                  'primary_key': None, 'schema_missing': True}
        clean = re.sub(r'#[^\n]*', '', content)
        for m2 in re.finditer(
            r'(has_many|has_one|belongs_to|has_and_belongs_to_many)\s+:(\w+)((?:[^#\n]|,[ \t]*\n)*)',
            clean
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
            assoc = {'type': assoc_type, 'name': sym, 'target': target_table}
            if through_m: assoc['through'] = through_m.group(1)
            if fk_m:      assoc['foreign_key'] = fk_m.group(1)
            if poly:      assoc['polymorphic'] = True
            tables[table_name]['associations'].append(assoc)

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
        for line in block.splitlines():
            line = line.strip()
            if not line or line.startswith('@@'):
                continue
            fm = re.match(r'(\w+)\s+(\w+)(\[\])?(\?)?\s*(.*)', line)
            if not fm:
                continue
            fname, ftype, is_list, optional, rest = fm.groups()

            if ftype in blocks:  # relation field
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
                    assocs.append({'type': 'belongs_to', 'name': fname,
                                   'target': target, 'foreign_key': fields.group(1)})
                else:  # 1:1 parent side without the FK
                    assocs.append({'type': 'has_one', 'name': fname, 'target': target})
                continue

            # scalar / enum column
            col = fname
            cm = re.search(r'@map\("([^"]+)"\)', rest)
            if cm:
                col = cm.group(1)
            primary = '@id' in rest
            if primary:
                pk = col
            cols.append({
                'name': col,
                'type': PRISMA_TYPES.get(ftype, ftype if ftype in enums else ftype.lower()),
                'nullable': bool(optional) and not primary,
                'primary': primary,
            })
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
    declared. Pairs already related in either direction are skipped. Marked
    with an `inferred` flag."""
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
            target = pluralize(cn[:-3])
            if (target == name or target not in tables
                    or target in outgoing              # we already reference it
                    or target in incoming.get(name, ())):  # it already references us
                continue
            t['associations'].append({'type': 'belongs_to', 'name': cn[:-3],
                                      'target': target, 'foreign_key': cn,
                                      'inferred': True})
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

def merge_code_semantics(tables, mroot):
    """Overlay associations parsed from application code (Rails / Prisma /
    Django) on top of the database truth. Columns always come from the DB;
    models without a matching DB table are added with schema_missing."""
    kind = detect_code_source(mroot)
    if kind == 'rails':
        mdir = mroot / 'app' / 'models' if (mroot / 'app' / 'models').is_dir() else mroot
        parse_models(mdir, tables)
        return kind
    if kind in ('prisma', 'django'):
        if kind == 'prisma':
            schema = mroot if mroot.is_file() else next(
                c for c in (mroot / 'prisma' / 'schema.prisma',
                            mroot / 'schema.prisma') if c.exists())
            ir = parse_prisma(schema)
        else:
            ir = parse_django(mroot)
        for name, t in ir.items():
            if name in tables:
                tables[name]['associations'].extend(t['associations'])
            else:
                tables[name] = {'columns': [], 'associations': t['associations'],
                                'primary_key': None, 'schema_missing': True}
        return kind
    sys.exit(f'Error: could not detect the code kind at {mroot} '
             '(expected a Rails app/models dir, a schema.prisma, or a Django project)')

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

def write_excel(tables, path, title):
    import zipfile
    HDR = 1  # bold style index
    used = set()
    sheets = []  # (sheet_name, xml)

    # ── overview sheet ──
    used.add('tables')
    names = sorted(tables)
    sheet_of = {n: _sheet_name(n, used) for n in names}
    rows = [[(f'{title} — table definitions', HDR)], [],
            [('#', HDR), ('Table', HDR), ('Comment', HDR),
             ('Columns', HDR), ('Indexes', HDR), ('Missing schema', HDR)]]
    links = []
    for i, n in enumerate(names, 1):
        t = tables[n]
        r = len(rows) + 1
        rows.append([i, n, t.get('comment', ''), len(t['columns']),
                     len(t.get('indexes', [])),
                     'yes' if t.get('schema_missing') else ''])
        links.append((f'B{r}', f"'{sheet_of[n]}'", n))
    overview = _sheet_xml(rows, widths=[5, 32, 50, 10, 10, 14], links=links)

    # ── per-table sheets ──
    for n in names:
        t = tables[n]
        rows = [[('Table', HDR), n],
                [('Comment', HDR), t.get('comment', '')],
                [],
                [('#', HDR), ('Column', HDR), ('Type', HDR), ('Nullable', HDR),
                 ('Default', HDR), ('Key', HDR), ('Extra', HDR), ('Comment', HDR)]]
        fk_cols = {a.get('foreign_key') for a in t['associations']
                   if a.get('foreign_key')}
        for i, c in enumerate(t['columns'], 1):
            key = 'PK' if c.get('primary') else ('FK' if c['name'] in fk_cols
                  or c['name'].endswith('_id') else '')
            rows.append([i, c['name'], c.get('sql_type', c['type']),
                         'YES' if c['nullable'] else 'NO',
                         c.get('default', ''), key, c.get('extra', ''),
                         c.get('comment', '')])
        if t.get('indexes'):
            rows += [[], [('Indexes', HDR)],
                     [('Name', HDR), ('Columns', HDR), ('Unique', HDR)]]
            for ix in t['indexes']:
                rows.append([ix['name'], ', '.join(ix['columns']),
                             'UNIQUE' if ix['unique'] else ''])
        if t['associations']:
            rows += [[], [('Associations', HDR)],
                     [('Type', HDR), ('Name', HDR), ('Target', HDR), ('Via', HDR)]]
            for a in t['associations']:
                via = ('DB FK' if a.get('db_fk') else
                       'inferred' if a.get('inferred') else 'code')
                rows.append([a['type'], a['name'], a['target'], via])
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
    wb_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + ''.join(f'<Relationship Id="rId{i+1}" '
                  'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                  f'Target="worksheets/sheet{i+1}.xml"/>' for i in range(len(sheets)))
        + f'<Relationship Id="rId{len(sheets)+1}" '
          'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
          'Target="styles.xml"/></Relationships>')
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
        '<borders count="1"><border/></borders>'
        '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
        '<cellXfs count="2"><xf xfId="0"/><xf xfId="0" fontId="1" applyFont="1"/></cellXfs>'
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
#depth-ctrl span{color:#64748b;font-size:11px}
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
#search-box{padding:6px 8px;border-bottom:1px solid #f1f5f9;flex-shrink:0}
#search-box input{width:100%;padding:4px 8px;font-size:12px;border:1px solid #e2e8f0;
  border-radius:4px;outline:none;background:#f8fafc}
#search-box input:focus{border-color:#3b82f6;background:#fff}
#table-list{flex:1;overflow-y:auto;padding:3px 0;min-height:0}
.table-item label{display:flex;align-items:center;gap:7px;width:100%;
  padding:4px 10px;cursor:pointer;color:#334155}
.table-item label:hover{background:#f1f5f9}
.table-item.selected label{background:#f0f7ff}
.table-item.focused label{background:#eff6ff;color:#1d4ed8;font-weight:600}
.table-item label input[type=checkbox]{accent-color:#3b82f6;flex-shrink:0}
.tname{flex:1;font-size:11px;font-family:'SF Mono','Fira Code',monospace;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rel-badge{font-size:10px;background:#f1f5f9;color:#94a3b8;padding:1px 4px;border-radius:3px;flex-shrink:0}
.col-hit{font-size:9px;background:#ecfeff;color:#0e7490;padding:1px 4px;border-radius:3px;flex-shrink:0;
  font-family:'SF Mono','Fira Code',monospace}
.table-item.focused .rel-badge{background:#dbeafe;color:#3b82f6}
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

/* ── toolbar ── */
#diagram-toolbar{position:absolute;bottom:12px;right:12px;display:flex;gap:4px;align-items:center}
.diag-btn{height:30px;min-width:30px;padding:0 8px;background:white;border:1px solid #e2e8f0;
  border-radius:6px;display:flex;align-items:center;justify-content:center;cursor:pointer;
  font-size:13px;color:#475569;box-shadow:0 1px 3px rgba(0,0,0,.08);white-space:nowrap}
.diag-btn:hover{background:#f1f5f9}
.diag-btn.active{background:#3b82f6;border-color:#3b82f6;color:#fff}
.diag-btn:disabled{opacity:.4;cursor:not-allowed}
.diag-btn:disabled:hover{background:white}
#colmode-group{display:flex;box-shadow:0 1px 3px rgba(0,0,0,.08);border-radius:6px}
#colmode-group .diag-btn{box-shadow:none;border-radius:0;font-size:11px;margin-left:-1px}
#colmode-group .diag-btn:first-child{border-radius:6px 0 0 6px;margin-left:0}
#colmode-group .diag-btn:last-child{border-radius:0 6px 6px 0}

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
.aname{font-family:monospace;font-size:12px;font-weight:600;color:#1e293b}
.atarget{font-size:11px;color:#64748b;margin-top:1px}
.atarget a{color:#3b82f6;cursor:pointer;text-decoration:none}
.atarget a:hover{text-decoration:underline}
.atarget .not-in-view{color:#94a3b8;font-style:italic}
.athrough{font-size:10px;color:#94a3b8}
.atarget .add-target{color:#64748b;cursor:pointer;text-decoration:none;border-bottom:1px dashed #cbd5e1}
.atarget .add-target:hover{color:#1d4ed8;border-bottom-color:#1d4ed8}

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
.er-node .n-alt{fill:#f8fafc}
.er-node .n-colhit{fill:#fef3c7}
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
#legend .lsvg{flex-shrink:0}

/* ── dark mode ── */
body.dark{background:#0b1220;color:#cbd5e1}
body.dark #left-pane,body.dark #right-pane{background:#0f172a;border-color:#1e293b}
body.dark .pane-title{color:#64748b;border-color:#1e293b}
body.dark #left-controls{border-color:#1e293b}
body.dark #left-controls button{background:#1e293b;border-color:#334155;color:#94a3b8}
body.dark #left-controls button:hover{background:#334155}
body.dark #search-box{border-color:#1e293b}
body.dark #search-box input{background:#1e293b;border-color:#334155;color:#e2e8f0}
body.dark .table-item label{color:#94a3b8}
body.dark .table-item label:hover{background:#1e293b}
body.dark .table-item.selected label{background:#172554}
body.dark .table-item.focused label{background:#1e3a8a;color:#bfdbfe}
body.dark .rel-badge{background:#1e293b;color:#64748b}
body.dark .col-hit{background:#164e63;color:#a5f3fc}
body.dark #center-pane{background:#0b1220;
  background-image:linear-gradient(rgba(148,163,184,.07) 1px,transparent 1px),
    linear-gradient(90deg,rgba(148,163,184,.07) 1px,transparent 1px)}
body.dark #legend,body.dark .diag-btn,body.dark .expand-tab{background:#0f172a;border-color:#334155;color:#94a3b8}
body.dark .diag-btn:hover{background:#1e293b}
body.dark .diag-btn.active{background:#3b82f6;border-color:#3b82f6;color:#fff}
body.dark .diag-btn:disabled:hover{background:#0f172a}
body.dark #max-rows{background:#0f172a;color:#94a3b8}
body.dark #focus-bar{background:#172554;border-color:#3b82f6;color:#bfdbfe}
body.dark #focus-bar .fb-hint{color:#93c5fd}
body.dark #focus-bar button{background:#0f172a;border-color:#1e40af;color:#93c5fd}
body.dark #hidden-bar{background:#450a0a;border-color:#7f1d1d;color:#fca5a5}
body.dark #canvas-empty{color:#64748b}
body.dark .detail-name{color:#e2e8f0;border-color:#e2e8f0}
body.dark .col-entry,body.dark .assoc-entry,body.dark .idx-entry,body.dark .msel-chip{background:#1e293b}
body.dark .idx-name{color:#e2e8f0}
body.dark .col-cn,body.dark .aname,body.dark .msel-cn{color:#e2e8f0}
body.dark .empty-state{color:#64748b}
body.dark .er-node .n-bg{fill:#1e293b;stroke:#334155}
body.dark .er-node .n-alt{fill:#263447}
body.dark .er-node .n-cn{fill:#e2e8f0}
body.dark .er-node .n-ct{fill:#94a3b8}
body.dark .er-node .n-colhit{fill:#713f12}
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
    <span style="margin-left:8px">Direction:</span>
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
    <div id="search-box"><input type="text" id="search" placeholder="Search tables / columns…"></div>
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
          2+ selected: align/distribute buttons appear in the right pane<br>
          List: click = locate in diagram, double-click = focus<br>
          Esc: exit focus</div>
      </div>
    </div>
    <button id="expand-left" class="expand-tab" title="Open left pane">▶</button>
    <button id="expand-right" class="expand-tab" title="Open right pane">◀</button>
    <div id="canvas-empty">No tables are displayed<br>
      Check tables in the list on the left, or press "All"</div>
    <div id="diagram-toolbar">
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
      <button class="diag-btn" id="btn-labels" title="Show/hide join-table labels (⇢)">Labels</button>
      <button class="diag-btn" id="btn-export" title="PNG to clipboard / download">⬇</button>
      <button class="diag-btn" id="btn-export-svg" title="Download SVG (vector, scales without loss)">SVG</button>
      <button class="diag-btn" id="btn-export-mmd" title="Copy Mermaid erDiagram markup to clipboard (paste into READMEs/PRs; covers displayed tables)">MMD</button>
      <button class="diag-btn" id="btn-zoom-in" title="Zoom in">+</button>
      <button class="diag-btn" id="btn-zoom-out" title="Zoom out">−</button>
      <button class="diag-btn" id="btn-zoom-100" title="Zoom to 100% (text at natural size)">1:1</button>
      <button class="diag-btn" id="btn-fit" title="Fit all">⊡</button>
      <button class="diag-btn" id="btn-undo" title="Undo layout change (Ctrl/Cmd+Z)" disabled>↶</button>
      <button class="diag-btn" id="btn-redo" title="Redo layout change (Ctrl/Cmd+Shift+Z)" disabled>↷</button>
      <button class="diag-btn" id="btn-reset" title="Re-layout now (repack to fill the screen)">↺</button>
      <button class="diag-btn" id="btn-autolayout" title="Auto-tidy mode: re-layout and fit whenever the displayed tables change">Auto-tidy</button>
      <button class="diag-btn" id="btn-dark" title="Toggle dark mode (exports always use the light palette)">🌙</button>
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
// LocalStorage keys are namespaced per project so multiple ERD pages
// served from the same origin don't share table selections
const LS = k => `erd:${document.title}:${k}`;

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
let colHighlight   = null;  // {table, q} — column-search hit to highlight in the node
let colMode        = 0;  // 0=all  1=PK/FK  2=header
let colOverride    = {}; // per-table column-mode override (name -> 0|1|2)
let showEdgeLabels = true;

const nodePos  = {};  // active positions
const basePos  = {};  // saved full-view positions
const nodeSize = {};

// ring depth map (set during hub-spoke layout for CSS tinting)
const ringDepth = {}; // tableName -> depth (0=center, 1=ring1, ...)

let vx=0, vy=0, vs=1;
let isPanning=false, panSX, panSY, panVX, panVY;
let isDragging=false, dragName, dragOX, dragOY, dragMoved=false, dragCX=0, dragCY=0;
let dragSet=new Set(), dragGroupStart={}; // nodes moving together in the current drag
let dragUndoSnapshot=null; // nodePos captured at mousedown, committed to undoStack only if the drag actually moved something

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
  localStorage.setItem(LS('excl'), JSON.stringify([...excludedTables]));
  localStorage.setItem(LS('hid'),  JSON.stringify([...hiddenTables]));
  localStorage.setItem(LS('ae'),   String(autoExpand));
  localStorage.setItem(LS('dep'),  String(expandDepth));
  localStorage.setItem(LS('cm'),   String(colMode));
  localStorage.setItem(LS('cov'),  JSON.stringify(colOverride));
  localStorage.setItem(LS('lbl'),  String(showEdgeLabels));
  localStorage.setItem(LS('dir'),  expandDir);
  localStorage.setItem(LS('al'),   String(autoLayout));
}
function loadState() {
  try { excludedTables = new Set(JSON.parse(localStorage.getItem(LS('excl')) || '[]')); } catch{}
  try { hiddenTables   = new Set(JSON.parse(localStorage.getItem(LS('hid'))  || '[]')); } catch{}
  try { colOverride    = JSON.parse(localStorage.getItem(LS('cov')) || '{}') || {}; } catch{}
  autoExpand  = localStorage.getItem(LS('ae'))  === 'true';
  expandDepth = parseInt(localStorage.getItem(LS('dep')) || '1', 10);
  colMode     = parseInt(localStorage.getItem(LS('cm'))  || '0', 10);
  showEdgeLabels = localStorage.getItem(LS('lbl')) !== 'false';
  const mr = parseInt(localStorage.getItem(LS('mr')), 10);
  if (mr > 0) maxRows = mr; // user's choice overrides the CLI default
  expandDir = localStorage.getItem(LS('dir')) || 'both';
  autoLayout = localStorage.getItem(LS('al')) === 'true';
  // guard against corrupted stored values — they fail silently otherwise
  if (![0,1,2,3].includes(expandDepth)) expandDepth = 1;
  if (![0,1,2].includes(colMode)) colMode = 0;
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
const isFkName = n => /(_id|Id)$/.test(n); // Rails snake_case / Prisma camelCase

function visibleCols(name) {
  const all = DATA.tables[name]?.columns || [];
  const m = effColMode(name);
  if (m === 0) return all;
  if (m === 1) return all.filter(c => c.primary || isFkName(c.name));
  return [];
}

// ── Node size ──────────────────────────────────────────────────────────────
const HDR_H=30, ROW_H=20, MIN_W=160, PAD=16;
let maxRows = __MAX_ROWS__; // CLI --max-rows default; adjustable in the toolbar
const colScroll = {}; // name -> first visible column index (for tall tables)

function calcSize(name) {
  const cols    = visibleCols(name);
  const allCnt  = (DATA.tables[name]?.columns || []).length;
  const nameW   = name.length * 8.5 + PAD;
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
// from its highest-degree table (same shape as hubSpokeLayout's rings, just
// laid out as rows instead of circles). Every edge therefore connects the
// same row or an adjacent one — the old width-wrapped rows could put two
// connected tables many rows apart, or split a dense cluster across one row
// with no adjacency, forcing edges into long detour arcs around whatever
// sat between them (see HANDOFF for the concrete example). Components are
// then shelf-packed side by side toward the viewport aspect ratio.
// widest physical row worth laying out: anything wider renders below ~60%
// zoom on a typical viewport. Both the overview's sub-row wrap and the
// incremental group placement break rows up at this width.
const MAX_ROW_W=1700;

function gridLayout(tables) {
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
    const hub=comp.slice().sort((a,b)=>(adj.get(b).size-adj.get(a).size)||a.localeCompare(b))[0];
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
  let cx=0, cy=0, rowH=0, maxX=0;
  boxes.forEach(b=>{
    if(cx>0 && cx+b.w>targetW){ cx=0; cy+=rowH+gap; rowH=0; }
    b.comp.forEach(t=>{
      const p=nodePos[t];
      nodePos[t]={x:cx+(p.x-b.x0), y:cy+(p.y-b.y0)};
    });
    cx+=b.w+gap; rowH=Math.max(rowH,b.h); maxX=Math.max(maxX,cx-gap);
  });

  // isolated tables: plain rows underneath
  if(singles.length){
    const rowW=Math.max(maxX,900);
    let sx=0, sy=cy+rowH+gap, srH=0;
    singles.sort().forEach(t=>{
      const s=nodeSize[t]||calcSize(t);
      if(sx>0 && sx+s.w>rowW){ sx=0; sy+=srH+60; srH=0; }
      nodePos[t]={x:sx+s.w/2, y:sy+s.h/2};
      sx+=s.w+40; srH=Math.max(srH,s.h);
    });
  }
}

// viewport aspect ratio (w/h), clamped — wide screens spread layouts sideways
function viewAspect(){
  const R = svg.getBoundingClientRect();
  if (!R.width || !R.height) return 1.6;
  return Math.max(1, Math.min(2.4, R.width / R.height));
}

// Hub-and-spoke layout: center + concentric rings by BFS depth
function hubSpokeLayout(tables, center) {
  const tset = new Set(tables);
  if (!tset.has(center)) return;

  // BFS from center within the visible table set
  const depMap = new Map([[center, 0]]);
  let frontier = [center];
  while (frontier.length > 0) {
    const next = [];
    for (const name of frontier) {
      (DATA.tables[name]?.associations || []).forEach(a => {
        if (tset.has(a.target) && !depMap.has(a.target)) {
          depMap.set(a.target, (depMap.get(name)||0) + 1); next.push(a.target);
        }
      });
      for (const [n, t] of Object.entries(DATA.tables)) {
        if (tset.has(n) && !depMap.has(n) && t.associations.some(a => a.target === name)) {
          depMap.set(n, (depMap.get(name)||0) + 1); next.push(n);
        }
      }
    }
    frontier = next;
  }
  // Disconnected tables get max depth+1
  tables.forEach(t => { if (!depMap.has(t)) depMap.set(t, 99); });

  // Update ringDepth for CSS tinting
  Object.keys(ringDepth).forEach(k => delete ringDepth[k]);
  depMap.forEach((d, t) => { ringDepth[t] = d; });

  // Group by depth
  const byDepth = {};
  depMap.forEach((d, t) => { (byDepth[d] = byDepth[d] || []).push(t); });

  // Place center
  nodePos[center] = {x:0, y:0};

  // Place each ring — radius adapts to node sizes so large tables
  // (e.g. accounts with 50+ columns) don't cover the center node.
  // Rings are ellipses stretched to the viewport aspect: wide screens
  // spread tables sideways instead of stacking them vertically.
  // Outer-ring members are ordered by the angle of their already-placed
  // neighbors (radial tree): edges stay short and cross the figure less.
  const ringGap = 70, circGap = 30;
  const asp = viewAspect();
  const twoPi = 2*Math.PI;
  const ext = t => { const s = nodeSize[t] || calcSize(t); return Math.hypot(s.w, s.h)/2; };

  // adjacency (both directions, non-polymorphic) for angular alignment
  const adj = new Map();
  const link = (a,b) => { if(!adj.has(a)) adj.set(a, new Set()); adj.get(a).add(b); };
  for (const [n, t] of Object.entries(DATA.tables)) {
    (t.associations||[]).forEach(a => {
      if (a.polymorphic || !DATA.tables[a.target]) return;
      link(n, a.target); link(a.target, n);
    });
  }

  const placedAngle = new Map();
  let prevR = 0, prevExt = ext(center);
  Object.keys(byDepth).map(Number).filter(d => d > 0).sort((a,b) => a-b).forEach(d => {
    const ring = byDepth[d];
    const maxExt = Math.max(...ring.map(ext));
    // circumference needed so ring members don't collide with each other
    const circ = ring.reduce((s,t) => { const sz = nodeSize[t]||calcSize(t); return s + (sz.w+sz.h)/2 + circGap; }, 0);
    const ringR = Math.max(230, prevR + prevExt + maxExt + ringGap, circ/(twoPi*asp));

    let order = ring.slice(), start = 0;
    if (d > 1) {
      // preferred angle = circular mean of placed neighbors' angles
      const pref = new Map(ring.map(t => {
        const ns = [...(adj.get(t)||[])].filter(n => placedAngle.has(n));
        if (!ns.length) return [t, null];
        let sx=0, sy=0;
        ns.forEach(n => { sx+=Math.cos(placedAngle.get(n)); sy+=Math.sin(placedAngle.get(n)); });
        return [t, Math.atan2(sy, sx)];
      }));
      order.sort((a,b) => {
        const pa=pref.get(a), pb=pref.get(b);
        if (pa===null && pb===null) return 0;
        if (pa===null) return 1;
        if (pb===null) return -1;
        return pa-pb;
      });
      start = pref.get(order[0]) ?? 0;
    }
    order.forEach((t, i) => {
      const angle = start + twoPi*i/order.length;
      placedAngle.set(t, angle);
      nodePos[t] = {x: ringR*asp*Math.cos(angle), y: ringR*Math.sin(angle)};
    });
    prevR = ringR; prevExt = maxExt;
  });
}

function layoutAll(tables, edges) {
  tables.forEach(n => { nodeSize[n] = calcSize(n); });
  const newTables = tables.filter(t => !nodePos[t]);
  if (newTables.length === 0) return;

  if (newTables.length === tables.length) {
    if (focusedTable && tables.includes(focusedTable)) {
      hubSpokeLayout(tables, focusedTable);
      // For small focused sets, hub-spoke alone is sufficient;
      // larger ones get refined by the force sim below
      if (tables.length <= 15) return;
      const n = tables.length;
      const K     = n<=35 ? 165 : 220;
      const attrK = n<=35 ? 0.85 : 0.55;
      runForce(tables, edges, Math.max(60, Math.min(220, 280-n)), K, attrK);
      return;
    }
    // overview: rectangle-native shelf-packed rows (connectivity order +
    // barycenter crossing reduction) — fills the viewport without a
    // circular layout's corner waste. No force sim (it inflates the
    // layout into an unreadable circle).
    gridLayout(tables);
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
  let bx0=Infinity, by1=-Infinity;
  tables.forEach(t => {
    const p=nodePos[t]; if(!p) return;
    const s=nodeSize[t]||{w:160,h:100};
    bx0=Math.min(bx0, p.x-s.w/2); by1=Math.max(by1, p.y+s.h/2);
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

  let cx=bx0, cy=by1+110, rowH=0; // fallback cursor, isolated additions only
  groups.forEach(g=>{
    if(g.isolated){
      const t=g.members[0], s=nodeSize[t]||calcSize(t);
      if(cx>bx0 && cx+s.w>bx0+900){ cx=bx0; cy+=rowH+40; rowH=0; }
      nodePos[t]={x:cx+s.w/2, y:cy+s.h/2};
      cx+=s.w+40; rowH=Math.max(rowH,s.h);
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

function runForce(tables, edges, iters, K, attrK) {
  const nodes = tables.map(id => ({id, ...nodePos[id], ...nodeSize[id]}));
  const byId  = Object.fromEntries(nodes.map(n => [n.id, n]));

  for (let it = 0; it < iters; it++) {
    const cool = Math.pow(1 - it/iters, 1.5);
    nodes.forEach(n => { n.fx=0; n.fy=0; });

    for (let i=0; i<nodes.length; i++) {
      for (let j=i+1; j<nodes.length; j++) {
        const a=nodes[i], b=nodes[j];
        let dx=b.x-a.x, dy=b.y-a.y;
        const d2=dx*dx+dy*dy;
        if (d2<.01){dx=Math.random()*2-1;dy=Math.random()*2-1;}
        const d=Math.sqrt(d2+.01);
        const minDist=(a.w+b.w)/2+50;
        const rep=K*K*1.5/d+(d<minDist?(minDist-d)*6:0);
        const fx=rep*dx/d, fy=rep*dy/d;
        a.fx-=fx; a.fy-=fy; b.fx+=fx; b.fy+=fy;
        // rectangles overlapping: push apart along the shallower axis
        // (point-based repulsion lets tall nodes stack vertically)
        const ox=(a.w+b.w)/2+30-Math.abs(dx), oy=(a.h+b.h)/2+30-Math.abs(dy);
        if(ox>0&&oy>0){
          if(ox<oy){ const s=dx>=0?1:-1, f=ox*3; a.fx-=f*s; b.fx+=f*s; }
          else     { const s=dy>=0?1:-1, f=oy*3; a.fy-=f*s; b.fy+=f*s; }
        }
      }
    }
    edges.forEach(e=>{
      const a=byId[e.source], b=byId[e.target];
      if (!a||!b) return;
      const dx=b.x-a.x, dy=b.y-a.y;
      const d=Math.max(1,Math.sqrt(dx*dx+dy*dy));
      const att=d/K*attrK;
      const fx=att*dx/d, fy=att*dy/d;
      a.fx+=fx; a.fy+=fy; b.fx-=fx; b.fy-=fy;
    });
    nodes.forEach(n=>{
      const disp=Math.sqrt(n.fx*n.fx+n.fy*n.fy);
      if(disp>0){const step=Math.min(disp,cool*55+.5);n.x+=n.fx/disp*step;n.y+=n.fy/disp*step;}
    });
  }
  nodes.forEach(n => { nodePos[n.id]={x:n.x,y:n.y}; });
}

// ── Focus mode position management ────────────────────────────────────────
function enterFocusMode() {
  // Save full-view positions
  Object.keys(nodePos).forEach(k => { basePos[k] = {...nodePos[k]}; });
  Object.keys(nodePos).forEach(k => delete nodePos[k]);
}
function switchFocusTable() {
  Object.keys(nodePos).forEach(k => delete nodePos[k]);
  Object.keys(ringDepth).forEach(k => delete ringDepth[k]);
}
function exitFocusMode() {
  Object.keys(nodePos).forEach(k => delete nodePos[k]);
  Object.keys(ringDepth).forEach(k => delete ringDepth[k]);
  Object.keys(basePos).forEach(k => { nodePos[k] = {...basePos[k]}; });
}

// re-render; with auto-tidy on, the overview is re-packed first so the
// layout always tracks the current display set
function refreshView(){
  if(!focusedTable && autoLayout){
    const ts=getDisplayTables();
    ts.forEach(t=>delete nodePos[t]);
    ts.forEach(n=>{ nodeSize[n]=calcSize(n); });
      if(ts.length) gridLayout(ts);
  }
  renderDiagram(); requestAnimationFrame(fitView);
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
  const edgeG=svgEl('g',{id:'edge-layer'});
  const nodeG=svgEl('g',{id:'node-layer'});
  erMain.appendChild(edgeG);
  erMain.appendChild(nodeG);
  edgeObstacles=tables;
  edges.forEach(e => drawEdge(edgeG, e));
  tables.forEach(n => drawNode(nodeG, n));
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
  const g = svgEl('g', {
    class: 'er-node' + (selectedTables.has(name)?' sel':'') + (name===focusedTable?' center':'') + (isAuto?' auto':'') + ringCls,
    transform: `translate(${lx},${ty})`,
    'data-name': name,
  });

  g.appendChild(svgEl('rect',{x:3,y:3,width:sz.w,height:sz.h,rx:5,ry:5,class:'n-shadow'}));
  g.appendChild(svgEl('rect',{width:sz.w,height:sz.h,rx:5,ry:5,class:'n-bg'}));
  g.appendChild(svgEl('rect',{width:sz.w,height:HDR_H,rx:5,ry:5,class:'n-hdr'}));
  g.appendChild(svgEl('rect',{y:HDR_H-4,width:sz.w,height:4,class:'n-hdr'}));

  const nt=svgEl('text',{x:sz.w/2,y:HDR_H/2+1,'text-anchor':'middle','dominant-baseline':'middle',class:'n-title'});
  nt.textContent=name; g.appendChild(nt);

  if(isRoot){
    const rb=svgEl('text',{x:12,y:HDR_H/2+1,'text-anchor':'middle','dominant-baseline':'middle',class:'n-root'});
    rb.textContent='✓';
    const rbTitle=svgEl('title',{});
    rbTitle.textContent='Checked (expansion root)';
    rb.appendChild(rbTitle);
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
    if(colHighlight&&colHighlight.table===name&&col.name.toLowerCase().includes(colHighlight.q))
      g.appendChild(svgEl('rect',{x:1,y:ry,width:sz.w-2,height:ROW_H,class:'n-colhit'}));
    const isPK=col.primary, isFK=!isPK&&isFkName(col.name);
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
  let blocked=0;
  for(const t of edgeObstacles){
    if(t===edge.source||t===edge.target) continue;
    const p=nodePos[t], s=nodeSize[t];
    if(!p||!s) continue;
    const x0=p.x-s.w/2-8, x1=p.x+s.w/2+8, y0=p.y-s.h/2-8, y1=p.y+s.h/2+8;
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
  refreshView();
  showDetails(); renderTableList();
  updateDepthCtrl(); updateFocusUI();
  if(!wasInFocus) showToast(`Focused: ${name} — double-click or Esc to exit`);
}

function clearFocus(){
  if(!focusedTable) return; // no-op outside focus — don't destroy the overview layout
  focusedTable=null; selectedTables=new Set(); selectionAnchor=null;
  manualExpanded.clear();
  exitFocusMode();
  refreshView();
  showDetails(); renderTableList();
  updateDepthCtrl(); updateFocusUI();
  showToast('Focus cleared — back to the overview');
}

// ── Left pane ─────────────────────────────────────────────────────────────
function renderTableList(){
  const list  = document.getElementById('table-list');
  const query = document.getElementById('search').value.toLowerCase();
  const prevScroll = list.scrollTop;
  list.innerHTML='';
  const inView = (focusedTable||autoExpand) ? new Set(getDisplayTables()) : null;
  const colHit = t => query && !t.includes(query)
    ? (DATA.tables[t]?.columns||[]).find(c=>c.name.toLowerCase().includes(query))?.name
    : null;
  allTables()
    .filter(t => !query || t.includes(query)
      || (DATA.tables[t]?.columns||[]).some(c=>c.name.toLowerCase().includes(query)))
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
        +(autoShown?' inview':'');
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
        if(focusedTable){ renderTableList(); return; } // the focus view ignores checkboxes
        refreshView(); renderTableList();
      });
      const nm=document.createElement('span'); nm.className='tname'; nm.textContent=name;
      lbl.appendChild(cb); lbl.appendChild(nm);
      const hit=colHit(name);
      if(hit){const hb2=document.createElement('span');hb2.className='col-hit';hb2.textContent='⌕ '+hit;lbl.appendChild(hb2);}
      const ac=(t?.associations||[]).length;
      if(ac>0){const b=document.createElement('span');b.className='rel-badge';b.textContent=ac;lbl.appendChild(b);}
      const hb=document.createElement('button');
      hb.className='hide-btn'; hb.type='button'; hb.textContent='🚫';
      hb.title=isHidden?'Unban':'Ban completely (never shown, even by auto-expand)';
      hb.addEventListener('click', e => {
        e.stopPropagation(); e.preventDefault();
        if(isHidden){ hiddenTables.delete(name); }
        else {
          hiddenTables.add(name);
          if(focusedTable===name){ focusedTable=null; selectedTables=new Set(); selectionAnchor=null; exitFocusMode(); }
        }
        saveState();
        if(focusedTable) switchFocusTable(); // re-run hub-spoke with new table set
        refreshView(); renderTableList(); updateHiddenBar();
        showDetails();
      });
      lbl.appendChild(hb);
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
  let h=`<div class="detail-name">${esc(name)}</div>`;
  if(t.comment) h+=`<div class="tbl-comment">${esc(t.comment)}</div>`;
  if(t.schema_missing && cols.length===0){
    h+='<div class="sec-title">Columns</div>'
      +'<div class="empty-state" style="margin-top:8px;text-align:left">No column info — this table is not in '
      +'schema.rb / structure.sql (likely managed by a gem or another database).</div>';
  }
  if(cols.length>0){
    h+='<div class="sec-title">Columns</div><div class="col-list">';
    cols.forEach(c=>{
      const isPK=c.primary, isFK=!isPK&&isFkName(c.name);
      const bc=isPK?'badge bdg-pk':isFK?'badge bdg-fk':'badge bdg-mt';
      const bt=isPK?'PK':isFK?'FK':'';
      const nullEl=c.nullable?'<span class="col-null">NULL</span>':'';
      const cmtEl=c.comment?`<div class="col-comment">${esc(c.comment)}</div>`:'';
      h+=`<div class="col-entry"><div class="col-main"><span class="${bc}">${bt}</span><span class="col-cn">${esc(c.name)}</span><span class="col-ct">${esc(c.sql_type||c.type)}</span>${nullEl}</div>${cmtEl}</div>`;
    });
    h+='</div>';
  }
  const idxs=t.indexes||[];
  if(idxs.length>0){
    h+='<div class="sec-title">Indexes</div><div class="idx-list">';
    idxs.forEach(ix=>{
      const uq=ix.unique?'<span class="badge-uq">UNIQUE</span>':'';
      h+=`<div class="idx-entry"><div class="idx-name">${esc(ix.name)}${uq}</div>`
        +`<div class="idx-cols">${ix.columns.map(esc).join(', ')}</div></div>`;
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
      const inView=dispTables.has(a.target);
      const isHidden=hiddenTables.has(a.target);
      const link=a.polymorphic
        ?`<span class="not-in-view" title="Polymorphic association (target table is decided at runtime)">(polymorphic)</span>`
        :inView
          ?`<a data-goto="${esc(a.target)}">${esc(a.target)}</a>`
          :isHidden
            ?`<span class="not-in-view" title="Banned with 🚫">${esc(a.target)} 🚫</span>`
            :`<a class="add-target" data-add="${esc(a.target)}" title="Not displayed — click to add to the diagram">${esc(a.target)} ＋</a>`;
      const thr=a.through?`<div class="athrough">through: :${esc(a.through)}</div>`:'';
      // plain-language description appears as a tooltip on hover
      h+=`<div class="assoc-entry ${cls}" title="${esc(desc)}"><div class="atype">${esc(a.type)}${a.inferred?' <span class="badge-inf">inferred</span>':''}${a.db_fk?' <span class="badge-dbfk">DB FK</span>':''}</div><div class="aname">:${esc(a.name)}</div><div class="atarget">→ ${link}</div>${thr}</div>`;
    });
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
.er-node .n-shadow{fill:rgba(0,0,0,.07)}
.er-node .n-bg{fill:#fff;stroke:#cbd5e1;stroke-width:1}
.er-node.sel .n-bg{stroke:#3b82f6;stroke-width:2}
.er-node .n-hdr{fill:#1e293b}
.er-node.ring-1 .n-hdr{fill:#1e3a5f}
.er-node.ring-2 .n-hdr{fill:#374151}
.er-node.ring-3 .n-hdr{fill:#4b5563}
.er-node .n-title{fill:#f8fafc;font-size:12px;font-weight:bold;font-family:monospace}
.er-node .n-alt{fill:#f8fafc}
.er-node .n-colhit{fill:#fef3c7}
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
  const vw=x1-x0, vh=y1-y0;

  const exportSvg=document.createElementNS(NS,'svg');
  exportSvg.setAttribute('xmlns',NS);
  exportSvg.setAttribute('width',Math.ceil(vw)); exportSvg.setAttribute('height',Math.ceil(vh));
  exportSvg.setAttribute('viewBox',`${x0} ${y0} ${vw} ${vh}`);

  // Embed CSS
  const styleEl=document.createElementNS(NS,'style');
  styleEl.textContent=EXPORT_CSS+(showEdgeLabels?'':'.e-lbg,.e-ltxt{display:none}');
  exportSvg.appendChild(styleEl);

  // Embed arrowhead markers
  const defsClone=document.querySelector('#er-svg defs').cloneNode(true);
  exportSvg.appendChild(defsClone);

  // Embed diagram content (deep clone). The live er-main carries the screen
  // pan/zoom transform — strip it, the viewBox already frames the diagram.
  const mainClone=erMain.cloneNode(true);
  mainClone.removeAttribute('transform');
  // strip transient selection state — the shared image should be neutral
  mainClone.querySelectorAll('.sel,.hl').forEach(el=>el.classList.remove('sel','hl'));
  mainClone.querySelectorAll('path').forEach(p=>{
    ['marker-start','marker-end'].forEach(a=>{
      const v=p.getAttribute(a); if(v) p.setAttribute(a, v.replace('-hl)',')'));
    });
  });
  exportSvg.appendChild(mainClone);

  return {svg:exportSvg, vw, vh};
}

function exportToSVG(){
  const built=buildExportSvg();
  if(!built) return;
  const svgStr=new XMLSerializer().serializeToString(built.svg);
  const url=URL.createObjectURL(new Blob([svgStr],{type:'image/svg+xml;charset=utf-8'}));
  const a=document.createElement('a');
  a.href=url; a.download='erd.svg'; a.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
  showToast('Downloaded erd.svg ✓');
}

// Mermaid erDiagram markup (paste straight into READMEs / PRs)
function exportToMermaid(){
  const tables=getDisplayTables();
  if(!tables.length){showToast('No tables are displayed');return;}
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
      const key=c.primary?' PK':isFkName(c.name)?' FK':'';
      lines.push(`        ${c.type||'string'} ${c.name}${key}`);
    });
    lines.push('    }');
  });
  const text=lines.join('\n');
  const dl=()=>{
    const url=URL.createObjectURL(new Blob([text],{type:'text/plain;charset=utf-8'}));
    const a=document.createElement('a');
    a.href=url; a.download='erd.mmd'; a.click();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
    showToast('Downloaded erd.mmd ✓');
  };
  if(navigator.clipboard?.writeText){
    navigator.clipboard.writeText(text)
      .then(()=>showToast(`Copied Mermaid markup ✓ (${tables.length} tables)`))
      .catch(dl);
  } else dl();
}

async function exportToPNG(){
  const built=buildExportSvg();
  if(!built) return;
  const scale=2, W=Math.ceil(built.vw*scale), H=Math.ceil(built.vh*scale);
  built.svg.setAttribute('width',W); built.svg.setAttribute('height',H); // 2x raster
  const svgStr=new XMLSerializer().serializeToString(built.svg);
  const blob=new Blob([svgStr],{type:'image/svg+xml;charset=utf-8'});
  const url=URL.createObjectURL(blob);

  await new Promise(resolve=>{
    const img=new Image();
    img.onload=()=>{
      const canvas=document.createElement('canvas');
      canvas.width=W; canvas.height=H;
      const ctx=canvas.getContext('2d');
      ctx.drawImage(img,0,0,W,H); // background stays transparent
      URL.revokeObjectURL(url);
      canvas.toBlob(pngBlob=>{
        const pngUrl=URL.createObjectURL(pngBlob);
        if(navigator.clipboard?.write){
          navigator.clipboard.write([new ClipboardItem({'image/png':pngBlob})])
            .then(()=>showToast('Copied to clipboard ✓'))
            .catch(()=>downloadPNG(pngUrl));
        } else { downloadPNG(pngUrl); }
        resolve();
      },'image/png');
    };
    img.onerror=()=>{URL.revokeObjectURL(url);showToast('Export failed');resolve();};
    img.src=url;
  });
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

svg.addEventListener('mousedown', e=>{
  if(e.button!==0||isDragging) return;
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
    if(!e.altKey){
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
    if(eL){eL.innerHTML='';edgeObstacles=getDisplayTables();getDisplayEdges(edgeObstacles).forEach(e2=>drawEdge(eL,e2));updateEdgeHighlight();}
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
    dragSet=new Set(); dragGroupStart={};
    // dragMoved stays set: the click event fires after mouseup and must see it.
    // If no click follows (released outside the node/window), clear it on the
    // next task so it can't swallow a later legitimate click.
    setTimeout(()=>{ dragMoved=false; }, 0);
    svg.classList.remove('node-drag');
    drawSnapGuides([]);
    const eL=document.getElementById('edge-layer');
    if(eL){eL.innerHTML='';edgeObstacles=getDisplayTables();getDisplayEdges(edgeObstacles).forEach(e2=>drawEdge(eL,e2));updateEdgeHighlight();}
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
  if(e.target===svg||e.target===erMain) selectOnly(null);
});

// Escape: search → focus → selection, in that order
window.addEventListener('keydown', e=>{
  if(e.key!=='Escape') return;
  const sb=document.getElementById('search');
  if(document.activeElement===sb && sb.value){ sb.value=''; renderTableList(); return; }
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
document.getElementById('btn-export').addEventListener('click', exportToPNG);
document.getElementById('btn-export-svg').addEventListener('click', exportToSVG);
document.getElementById('btn-export-mmd').addEventListener('click', exportToMermaid);
document.getElementById('btn-dark').addEventListener('click',()=>{
  const on=!document.body.classList.contains('dark');
  document.body.classList.toggle('dark', on);
  localStorage.setItem(LS('dk'), String(on));
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

// Max visible rows per table
document.getElementById('max-rows').addEventListener('change', e=>{
  maxRows=parseInt(e.target.value,10)||15;
  localStorage.setItem(LS('mr'), String(maxRows));
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

// ── Pane resize / collapse ──────────────────────────────────────────────────
(()=>{
  const lp=document.getElementById('left-pane'), rp=document.getElementById('right-pane');
  const lw=parseInt(localStorage.getItem(LS('lw')),10), rw=parseInt(localStorage.getItem(LS('rw')),10);
  if(lw) lp.style.width=lw+'px';
  if(rw) rp.style.width=rw+'px';

  function setCollapsed(pane, tabId, key, val){
    pane.classList.toggle('collapsed', val);
    document.getElementById(tabId).classList.toggle('visible', val);
    localStorage.setItem(key, String(val));
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
      localStorage.setItem(key, String(Math.round(pane.getBoundingClientRect().width)));
    });
  }
  setupDivider('div-l', lp, LS('lw'),  1);
  setupDivider('div-r', rp, LS('rw'), -1);
})();

// ── Named views (save/restore the current view under a name) ─────────────
function loadViews(){ try{ return JSON.parse(localStorage.getItem(LS('views'))||'{}')||{}; }catch{ return {}; } }
function persistViews(v){ localStorage.setItem(LS('views'), JSON.stringify(v)); }

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
    localStorage.setItem(LS('lg'), String(v));
  };
  setLg(localStorage.getItem(LS('lg'))==='true');
  document.getElementById('legend-head').addEventListener('click',()=>setLg(!lg.classList.contains('collapsed')));
})();

// Search: filter the list; Enter jumps to the first match in the diagram
document.getElementById('search').addEventListener('input', e=>{
  if(!e.target.value && colHighlight){ const t=colHighlight.table; colHighlight=null; redrawNode(t); }
  renderTableList();
});
document.getElementById('search').addEventListener('keydown', e=>{
  if(e.key!=='Enter') return;
  const q=e.target.value.toLowerCase();
  if(!q) return;
  const m=allTables().find(t=>t.startsWith(q)) || allTables().find(t=>t.includes(q))
       || allTables().find(t=>(DATA.tables[t]?.columns||[]).some(c=>c.name.toLowerCase().includes(q)));
  if(!m) return;
  if(!hiddenTables.has(m) && !getDisplayTables().includes(m)) addTables([m]); // add hidden targets before locating
  locateTable(m);
  // column hit: make sure the column is visible and highlighted in the node
  const colQ=(DATA.tables[m]?.columns||[]).some(c=>c.name.toLowerCase().includes(q))?q:null;
  colHighlight=colQ?{table:m,q:colQ}:null;
  if(colQ){
    if(!visibleCols(m).some(c=>c.name.toLowerCase().includes(colQ))) colOverride[m]=0; // reveal columns hidden by the mode
    const idx=visibleCols(m).findIndex(c=>c.name.toLowerCase().includes(colQ));
    if(idx>=0) colScroll[m]=Math.max(0, Math.min(idx-2, Math.max(0, visibleCols(m).length-maxRows)));
    delete nodeSize[m];
    renderDiagram();
    flashNode(m);
  }
});

// ── Init ───────────────────────────────────────────────────────────────────
loadState();
document.getElementById('auto-expand').checked=autoExpand;
document.getElementById('ae-label').className=autoExpand?'ae-on':'';
updateDepthCtrl();
updateFocusUI();
updateColModeUI();
updateLabelUI();
document.getElementById('btn-autolayout').classList.toggle('active',autoLayout);
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
renderTableList();
renderDiagram();
requestAnimationFrame(fitView);
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(
        description='Generate an interactive ER diagram from a live database, '
                    'optionally enriched with association semantics parsed from '
                    'application code (Rails / Prisma / Django)')
    p.add_argument('database', metavar='mysql://user@host:port/dbname',
                   help='Database connection URL (required). Use MYSQL_PWD or '
                        '~/.my.cnf for the password; a read-only account is recommended')
    p.add_argument('-o', '--output', default='erd.html',
                   help='Output HTML file (default: erd.html)')
    p.add_argument('--models', metavar='PATH',
                   help='Merge association semantics parsed from application code '
                        '(Rails project/app/models dir, schema.prisma, or Django project)')
    p.add_argument('--excel', metavar='FILE.xlsx',
                   help='Also write a table-definition workbook '
                        '(overview sheet + one sheet per table)')
    p.add_argument('--max-rows', type=int, default=15,
                   help='Max column rows shown per table before scrolling (default: 15)')
    p.add_argument('--only', action='append', metavar='PATTERN',
                   help='Include only tables matching the glob pattern(s). '
                        'Repeatable; comma-separated lists accepted (e.g. --only "user*,post*")')
    p.add_argument('--exclude', action='append', metavar='PATTERN',
                   help='Exclude tables matching the glob pattern(s). Same syntax as --only')
    p.add_argument('--no-infer-fk', action='store_true',
                   help='Disable inferring relations from *_id columns')
    args = p.parse_args()

    url = args.database
    if not url.startswith('mysql://'):
        sys.exit('Error: a database URL is required (currently mysql:// only), '
                 'e.g. mysql://readonly@127.0.0.1:3306/myapp_production')
    tables = parse_mysql(url)
    print(f'Fetched {len(tables)} tables from MySQL', file=sys.stderr)

    if args.models:
        mroot = Path(args.models).expanduser().resolve()
        if not mroot.exists():
            sys.exit(f'Error: {mroot} does not exist')
        kind = merge_code_semantics(tables, mroot)
        removed = dedupe_db_fk(tables)
        print(f'Merged {kind} associations from {mroot}'
              + (f' ({removed} DB FKs covered by explicit associations)' if removed else ''),
              file=sys.stderr)

    _finish(tables, args, urlparse(url).path.lstrip('/'))

def _finish(tables, args, title_name):
    """Shared tail: FK inference, --only/--exclude filtering, HTML generation."""
    if not args.no_infer_fk:
        inferred = infer_fk_associations(tables)
        if inferred:
            print(f'Inferred {inferred} relations from *_id columns', file=sys.stderr)

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

    data_json = json.dumps({'tables': tables}, ensure_ascii=False)
    html = (HTML_TEMPLATE
            .replace('__DATA_JSON__', data_json)
            .replace('__MAX_ROWS__', str(args.max_rows))
            .replace('__TITLE__', f'{title_name} — ERD'))

    out = Path(args.output)
    out.write_text(html, encoding='utf-8')
    print(f'Generated: {out} ({out.stat().st_size // 1024} KB)', file=sys.stderr)

    if getattr(args, 'excel', None):
        write_excel(tables, Path(args.excel), title_name)
        print(f'Generated: {args.excel}', file=sys.stderr)

if __name__ == '__main__':
    main()
