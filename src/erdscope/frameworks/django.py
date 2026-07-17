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
