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
