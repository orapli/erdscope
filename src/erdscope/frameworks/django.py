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
                    ftype = fn.attr if isinstance(fn, ast.Attribute) else \
                            fn.id if isinstance(fn, ast.Name) else None
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

    def detect(self, root):
        return root.is_dir() and (root / 'manage.py').exists()

    def build(self, root, table_map):
        return django_provider(root)
