# ---------------------------------------------------------------------------
# SQLAlchemy models parser (a single .py file, or a directory of .py files;
# AST based, same static-analysis discipline as frameworks/django.py — no
# import, no execution).
# ---------------------------------------------------------------------------
SQLALCHEMY_TYPES = {
    'Integer': 'integer', 'INTEGER': 'integer', 'SmallInteger': 'integer', 'SMALLINT': 'integer',
    'BigInteger': 'bigint', 'BIGINT': 'bigint',
    'String': 'string', 'VARCHAR': 'string', 'Unicode': 'string', 'CHAR': 'string', 'NCHAR': 'string',
    'Text': 'text', 'UnicodeText': 'text', 'TEXT': 'text', 'CLOB': 'text',
    'Boolean': 'boolean', 'BOOLEAN': 'boolean',
    'Float': 'float', 'FLOAT': 'float', 'REAL': 'float', 'DOUBLE': 'float', 'Double': 'float',
    'Numeric': 'decimal', 'DECIMAL': 'decimal', 'NUMERIC': 'decimal',
    'DateTime': 'datetime', 'TIMESTAMP': 'datetime',
    'Date': 'date', 'DATE': 'date', 'Time': 'time', 'TIME': 'time',
    'Interval': 'interval',
    'LargeBinary': 'binary', 'BINARY': 'binary', 'VARBINARY': 'binary', 'BLOB': 'binary',
    'JSON': 'jsonb', 'JSONB': 'jsonb',
    'Uuid': 'uuid', 'UUID': 'uuid',
    'Enum': 'string',
}
# Python type names inside a 2.0-style `Mapped[...]` annotation, mapped to the
# same coarse vocabulary SQLALCHEMY_TYPES produces. SQLAlchemy's own type
# registry resolves these to concrete SQL types (int -> Integer, str ->
# String, ...), which is exactly what an annotation-only `mapped_column()`
# with no explicit type argument relies on.
SQLALCHEMY_ANNOTATION_TYPES = {
    'int': 'integer', 'str': 'string', 'float': 'float', 'bool': 'boolean',
    'bytes': 'binary', 'datetime': 'datetime', 'date': 'date', 'time': 'time',
    'timedelta': 'interval', 'Decimal': 'decimal', 'UUID': 'uuid',
    'dict': 'jsonb', 'list': 'jsonb',
}
# `Mapped[list["Item"]]` and friends — the collection wrappers that make a
# relationship a to-many rather than a scalar
_SQLALCHEMY_COLLECTION_ANNOTATIONS = {'list', 'List', 'set', 'Set', 'frozenset', 'FrozenSet',
                                      'Sequence', 'MutableSequence', 'Collection'}
# the ORM's annotation wrappers, incl. the 2.0 lazy-collection variants
_SQLALCHEMY_MAPPED_ANNOTATIONS = {'Mapped', 'WriteOnlyMapped', 'DynamicMapped'}
_SQLALCHEMY_COLUMN_CALLS = {'Column', 'mapped_column'}
_SQLALCHEMY_SKIP_DIRS = {'venv', '.venv', 'env', 'site-packages', 'node_modules',
                         '.git', 'migrations', 'versions', '__pycache__',
                         'tests', 'test', 'examples', 'docs', 'scripts'}


def _sqlalchemy_files(root):
    if root.is_file():
        return [root] if root.suffix == '.py' else []
    return [p for p in sorted(root.rglob('*.py'))
            if not (set(p.relative_to(root).parts) & _SQLALCHEMY_SKIP_DIRS)]


def _sqlalchemy_call_name(call):
    fn = call.func
    return fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else None


def _annotation_name(node):
    """The bare name an annotation node denotes: `int`, `orm.Mapped` -> Mapped,
    or the forward reference in `Mapped["User"]`. None if it isn't a name."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        # a forward reference, quoted because the class isn't defined yet;
        # only the last dotted segment can match a parsed class name
        return node.value.split('.')[-1].strip('\'" ')
    return None


def _unwrap_mapped_annotation(annotation):
    """Read a SQLAlchemy 2.0 `Mapped[...]` annotation, the style that carries
    information the mapped_column()/relationship() call itself no longer
    repeats. Returns (inner_name, is_collection, is_optional); inner_name is
    None when there is no Mapped[...] to read or it's too dynamic to resolve
    statically (a real union, an unrecognised generic).

    Covers the shapes 2.0 code actually writes:
        Mapped[int]            Mapped[Optional[str]]     Mapped[str | None]
        Mapped["User"]         Mapped[list["Item"]]      Mapped[List[Item]]
        WriteOnlyMapped[...]   DynamicMapped[...]
    """
    if not isinstance(annotation, ast.Subscript):
        return None, False, False
    wrapper = _annotation_name(annotation.value)
    if wrapper not in _SQLALCHEMY_MAPPED_ANNOTATIONS:
        return None, False, False
    # WriteOnlyMapped["Post"] / DynamicMapped["Post"] are to-many collections
    # by definition — they name the element type directly, with no list[...]
    node, collection, optional = annotation.slice, wrapper != 'Mapped', False
    for _ in range(4):  # bounded: real annotations nest Optional/list a couple deep at most
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            # PEP 604 `str | None`; anything else is a genuine union with no
            # single type to report
            left_none = isinstance(node.left, ast.Constant) and node.left.value is None
            right_none = isinstance(node.right, ast.Constant) and node.right.value is None
            if not (left_none or right_none):
                return None, collection, optional
            optional = True
            node = node.left if right_none else node.right
            continue
        if isinstance(node, ast.Subscript):
            outer = _annotation_name(node.value)
            if outer == 'Optional':
                optional = True
            elif outer in _SQLALCHEMY_COLLECTION_ANNOTATIONS:
                collection = True
            else:
                return None, collection, optional  # e.g. dict[str, Any] — not a single type
            node = node.slice
            continue
        break
    return _annotation_name(node), collection, optional


def _looks_like_sqlalchemy(tree):
    # three independent signals (any one is enough), gathered in a single
    # walk: an explicit declarative_base() call, a class subclassing
    # DeclarativeBase (2.0 style), or the combination of a __tablename__
    # assignment together with a Column(...)/mapped_column(...) call —
    # neither alone is distinctive enough (many ORMs use a `Column` name;
    # `__tablename__`-only would also match a hand-rolled dataclass).
    has_tablename = has_column_call = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _sqlalchemy_call_name(node) == 'declarative_base':
            return True
        if isinstance(node, ast.ClassDef):
            for b in node.bases:
                bname = b.attr if isinstance(b, ast.Attribute) else b.id if isinstance(b, ast.Name) else None
                if bname == 'DeclarativeBase':
                    return True
        if isinstance(node, ast.Call) and _sqlalchemy_call_name(node) in _SQLALCHEMY_COLUMN_CALLS:
            has_column_call = True
        elif ((isinstance(node, ast.Assign) and len(node.targets) == 1
               and isinstance(node.targets[0], ast.Name) and node.targets[0].id == '__tablename__')
              or (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                  and node.target.id == '__tablename__')):
            has_tablename = True
    return has_tablename and has_column_call


def parse_sqlalchemy(root):
    """Static AST parse of a --models root recognised as SQLAlchemy: a single
    .py file, or a directory of .py files (venv/site-packages/migrations/etc.
    excluded — see _SQLALCHEMY_SKIP_DIRS). Returns (tables, warnings); a
    concrete model class with no resolvable __tablename__ is kept (not
    silently dropped — D7) under a to_snake(classname) fallback, with a
    file:line warning naming the guess, same philosophy as rails_schema.py.

    Two passes, same shape as parse_django: pass 1 collects every class
    definition (bases, __tablename__/__abstract__, Column/mapped_column/
    relationship field calls) plus every module-level `Table(...)` variable
    (for relationship(secondary=<var>) resolution) and every
    declarative_base() variable (for base-class resolution); pass 2 builds
    each concrete class's columns and associations, needing the completed
    class->tablename map (relationship targets) and each class's own FK
    targets (association dedup, see below)."""
    warnings = []
    classes = {}       # class name -> {bases, tablename, abstract, fields, file, lineno}
    table_vars = {}    # variable name -> table name, from `x = Table('name', ...)`
    root_bases = {'DeclarativeBase'}  # + every declarative_base() variable found below

    def const(v):
        return v.value if isinstance(v, ast.Constant) else None

    def assign_target_value(stmt):
        # a single-name Assign OR AnnAssign with a value — the two shapes a
        # `foo = Column(...)` / `foo: Mapped[int] = mapped_column(...)` /
        # `__tablename__ = "x"` line can take. The AnnAssign's own annotation
        # comes back as the third element: in the 2.0 style SQLAlchemy's docs
        # now lead with, `Mapped[int]` / `Mapped[list["Item"]]` is where the
        # type and the relationship target live, because the call beside it
        # (`mapped_column()`, `relationship()`) often carries neither.
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)):
            return stmt.targets[0].id, stmt.value, None
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
            return stmt.target.id, stmt.value, stmt.annotation
        return None, None, None

    for path in _sqlalchemy_files(root):
        try:
            tree = ast.parse(path.read_text(encoding='utf-8', errors='replace'))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            name, value, _ = assign_target_value(node)
            if name is None or not isinstance(value, ast.Call):
                continue
            fname = _sqlalchemy_call_name(value)
            if fname == 'declarative_base':
                root_bases.add(name)
            elif (fname == 'Table' and value.args and isinstance(value.args[0], ast.Constant)
                  and isinstance(value.args[0].value, str)):
                table_vars[name] = value.args[0].value
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            bases = [b.attr if isinstance(b, ast.Attribute) else
                     b.id if isinstance(b, ast.Name) else '' for b in node.bases]
            tablename, abstract, fields = None, False, []
            for stmt in node.body:
                fname_, value, annotation = assign_target_value(stmt)
                if fname_ is None:
                    continue
                if fname_ == '__tablename__' and isinstance(value, ast.Constant) and isinstance(value.value, str):
                    tablename = value.value
                elif fname_ == '__abstract__' and isinstance(value, ast.Constant):
                    abstract = bool(value.value)
                elif isinstance(value, ast.Call):
                    call_name = _sqlalchemy_call_name(value)
                    if call_name in _SQLALCHEMY_COLUMN_CALLS:
                        fields.append({'kind': 'column', 'name': fname_, 'call': value,
                                       'annotation': annotation, 'lineno': stmt.lineno})
                    elif call_name == 'relationship':
                        fields.append({'kind': 'relationship', 'name': fname_, 'call': value,
                                       'annotation': annotation, 'lineno': stmt.lineno})
            # a class name collision across two files (globally keyed, unlike
            # parse_django's per-app namespacing — SQLAlchemy has no
            # equivalent app boundary) silently keeps the last one parsed;
            # narrow enough not to special-case for a single-project overlay.
            classes[node.name] = {'bases': bases, 'tablename': tablename, 'abstract': abstract,
                                  'fields': fields, 'file': str(path), 'lineno': node.lineno}

    # `class Base(DeclarativeBase): pass` (the 2.0-style app-defined base) is
    # a declarative BASE, not a model: no __tablename__, no columns or
    # relationships of its own. Promote such a class (transitively — a base
    # can subclass another base) into root_bases so its subclasses still
    # resolve as models, instead of letting the D7 fallback below fabricate a
    # phantom to_snake('Base') table for the base itself.
    changed = True
    while changed:
        changed = False
        for name, c in classes.items():
            if (name not in root_bases and not c['tablename'] and not c['fields']
                    and any(b in root_bases for b in c['bases'])):
                root_bases.add(name)
                changed = True

    # a class is a model if any base (transitively, by bare name) resolves to
    # a recognised declarative base — mirrors parse_django's model_keys walk.
    # A class that IS a recognised base (root_bases, incl. the promotion
    # above) is never itself a model.
    model_keys, changed = set(), True
    while changed:
        changed = False
        for name, c in classes.items():
            if name in model_keys or name in root_bases:
                continue
            if (any(b in root_bases for b in c['bases'])
                    or any(b in model_keys for b in c['bases'])):
                model_keys.add(name)
                changed = True

    def merged_fields(name, seen=None):
        # inherit fields from every base class we parsed, whether or not it
        # is itself a recognised model — this covers both an `__abstract__ =
        # True` declarative base AND a plain (non-Base) mixin class, since
        # SQLAlchemy code commonly uses both patterns to share columns
        seen = seen if seen is not None else set()
        if name not in classes or name in seen:
            return []
        seen.add(name)
        out = []
        for b in classes[name]['bases']:
            out.extend(merged_fields(b, seen))
        out.extend(classes[name]['fields'])
        return out

    # concrete = has a real table: `__abstract__` classes never get one (pure
    # column donors); everything else either names __tablename__ or, if it's
    # a recognised model missing one, falls back to to_snake(classname) with
    # a warning (D7 — never silently dropped) — a plain mixin that is
    # neither abstract-flagged nor a recognised model subclass is not a table
    # either (it only ever contributes fields via merged_fields above)
    table_of, concrete = {}, []
    for name, c in classes.items():
        if c['abstract']:
            continue
        if c['tablename']:
            table_of[name] = c['tablename']
            concrete.append(name)
        elif name in model_keys:
            table_of[name] = to_snake(name)
            concrete.append(name)
            warnings.append(f"{c['file']}:{c['lineno']}: model class {name!r} has no "
                            f"__tablename__ — using {table_of[name]!r}")

    def call_type_name(node):
        if isinstance(node, ast.Call):
            return _sqlalchemy_call_name(node)
        if isinstance(node, ast.Attribute):
            return node.attr
        if isinstance(node, ast.Name):
            return node.id
        return None

    def coarse_type(node):
        name = call_type_name(node)
        return None if name is None else SQLALCHEMY_TYPES.get(name, name.lower())

    def parse_col_call(call):
        # Column/mapped_column's positional args: an optional leading string
        # (physical column name override), an optional type (Name/Call/
        # Attribute), and an optional ForeignKey(...) call — any order after
        # the leading name, so classify each by shape rather than position
        physical, type_node, fk_call = None, None, None
        for a in call.args:
            if (isinstance(a, ast.Constant) and isinstance(a.value, str)
                    and physical is None and type_node is None and fk_call is None):
                physical = a.value
            elif isinstance(a, ast.Call) and _sqlalchemy_call_name(a) == 'ForeignKey':
                fk_call = a
            elif type_node is None and isinstance(a, (ast.Call, ast.Name, ast.Attribute)):
                type_node = a
        kw = {k.arg: k.value for k in call.keywords if k.arg}
        return physical, type_node, fk_call, kw

    def fk_target_table(fk_call):
        # ForeignKey('table.col') / ForeignKey('schema.table.col') — always a
        # dotted string in statically-analysable code; the target table is
        # the FIRST segment (no class->table resolution needed, unlike
        # Rails/Django: this already names the physical table)
        if fk_call.args and isinstance(fk_call.args[0], ast.Constant) and isinstance(fk_call.args[0].value, str):
            parts = fk_call.args[0].value.split('.')
            return parts[0] if parts and parts[0] else None
        return None

    def resolve_rel_target(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return table_of.get(node.value.split('.')[-1])
        if isinstance(node, ast.Name):
            return table_of.get(node.id)
        if isinstance(node, ast.Attribute):
            return table_of.get(node.attr)
        return None

    def resolve_secondary(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return table_vars.get(node.id)
        return None

    tables = {}
    for name in concrete:
        tname = table_of[name]
        fields = merged_fields(name)
        cols, assocs, pk, fk_targets = [], [], None, set()
        for f in fields:
            if f['kind'] != 'column':
                continue
            physical, type_node, fk_call, kw = parse_col_call(f['call'])
            col_name = physical or f['name']
            primary = bool(const(kw.get('primary_key')))
            if primary:
                pk = col_name
            ann_name, ann_collection, ann_optional = _unwrap_mapped_annotation(f.get('annotation'))
            # In the 2.0 style the annotation, not a keyword, is what declares
            # nullability: `Mapped[str]` is NOT NULL, `Mapped[Optional[str]]`
            # (or `str | None`) is nullable. An explicit nullable= still wins,
            # and a column with no readable annotation keeps the pre-2.0
            # default of nullable-unless-PK.
            if 'nullable' in kw:
                nullable = bool(const(kw['nullable']))
            elif ann_name is not None:
                nullable = ann_optional
            else:
                nullable = not primary
            if type_node is not None:
                ctype = coarse_type(type_node) or ''
            elif ann_name is not None and not ann_collection:
                # annotation-only mapped_column(): resolve the type from
                # `Mapped[...]`. An unrecognised name (a custom type, an Enum
                # class) passes through lowercased, same as an unrecognised
                # SQLAlchemy type name does in coarse_type. A collection
                # annotation is skipped deliberately — `Mapped[list[str]]` is
                # an ARRAY/JSON column, not a string one.
                ctype = SQLALCHEMY_ANNOTATION_TYPES.get(ann_name, ann_name.lower())
            else:
                # no explicit type: a bare FK column infers its type from the
                # referenced PK, almost always an integer surrogate key —
                # same bigint default parse_django backfills for its FK
                # columns; a genuinely typeless, FK-less column is a static-
                # analysis blind spot (real SQLAlchemy requires one or the
                # other) and gets '' rather than a guess
                ctype = 'bigint' if fk_call is not None else ''
            cols.append({'name': col_name, 'type': ctype, 'nullable': nullable, 'primary': primary})
            if fk_call is not None:
                target = fk_target_table(fk_call)
                if target:
                    fk_targets.add(target)
                    unique = bool(const(kw.get('unique')))
                    # a friendlier name than the raw column attribute (`user`,
                    # not `user_id`) when it ends with the conventional
                    # suffix — same stem infer_fk_associations (base.py) uses
                    assoc_name = f['name'][:-3] if f['name'].endswith('_id') else f['name']
                    assocs.append({'type': 'has_one' if unique else 'belongs_to',
                                   'name': assoc_name, 'target': target, 'foreign_key': col_name})
        for f in fields:
            if f['kind'] != 'relationship':
                continue
            call = f['call']
            kw = {k.arg: k.value for k in call.keywords if k.arg}
            ann_name, ann_collection, _ = _unwrap_mapped_annotation(f.get('annotation'))
            target = resolve_rel_target(call.args[0]) if call.args else None
            if target is None and ann_name is not None:
                # 2.0 style: `items: Mapped[list["Item"]] = relationship()` —
                # the call has no target argument at all, the annotation is
                # the only place the target class appears. Without this the
                # whole association was dropped silently.
                target = table_of.get(ann_name)
            if target is None:
                continue  # unresolvable target (dynamic/external class) — keep silent, same as parse_django
            if 'secondary' in kw:
                a = {'type': 'has_and_belongs_to_many', 'name': f['name'], 'target': target}
                thr = resolve_secondary(kw['secondary'])
                if thr:
                    a['through'] = thr
                assocs.append(a)
                continue
            if 'remote_side' in kw:
                # the many-to-one side of a self-reference, aliasing the FK
                # column's own belongs_to already appended above — creating
                # an edge here too would double it
                continue
            if target != tname and target in fk_targets:
                # this class already declared its own ForeignKey to the same
                # target table (e.g. `user_id = Column(ForeignKey('users.id'))`
                # next to `user = relationship('User')`) — the relationship()
                # call is just a Python-side handle onto that same edge, not a
                # second one (Fable's dedup requirement). Excluded when
                # target == tname: a self-referencing table's OWN fk_targets
                # always contains its own name, which would otherwise also
                # swallow the genuine inverse collection (e.g. `children`)
                continue
            uselist = const(kw.get('uselist'))
            if uselist is not None:
                card = 'has_many' if uselist else 'has_one'
            elif ann_name is not None:
                # the 2.0 annotation states the cardinality outright:
                # `Mapped[list["Item"]]` is to-many, `Mapped["User"]` is scalar
                card = 'has_many' if ann_collection else 'has_one'
            else:
                card = 'has_many'
            assocs.append({'type': card, 'name': f['name'], 'target': target})
        if pk is None:  # SQLAlchemy has no implicit PK; mirror parse_django's own bigint backfill for consistency
            pk = 'id'
            cols.insert(0, {'name': 'id', 'type': 'bigint', 'nullable': False, 'primary': True})
        tables[tname] = {'columns': cols, 'associations': assocs, 'primary_key': pk}
    return tables, warnings


def sqlalchemy_provider(root):
    """ProviderResult for a resolved SQLAlchemy --models root. Retains columns,
    same as django_provider/prisma_provider."""
    tables, warnings = parse_sqlalchemy(root)
    return make_provider_result('framework', 'sqlalchemy', tables,
                                location=str(root), warnings=warnings)

@register_overlay
class SQLAlchemyOverlay(FrameworkOverlay):
    """A SQLAlchemy declarative-models input: a single .py file, or a
    directory of .py files, containing at least one recognisable declarative
    model (see _looks_like_sqlalchemy). Retains columns. Runs after Rails/
    Django/Prisma (priority 4) since its detection signals are the loosest
    of the four (plain .py files, no distinguishing marker file like
    manage.py or schema.prisma)."""
    name = 'sqlalchemy'
    priority = 4
    expects = ('a single .py file or a directory of .py files declaring at least one '
               'SQLAlchemy declarative model (declarative_base()/DeclarativeBase, or '
               '__tablename__ together with Column()/mapped_column())')

    def detect(self, root):
        if root.is_file() and root.suffix != '.py':
            return False
        if not root.is_file() and not root.is_dir():
            return False
        for p in _sqlalchemy_files(root):
            try:
                tree = ast.parse(p.read_text(encoding='utf-8', errors='replace'))
            except SyntaxError:
                continue
            if _looks_like_sqlalchemy(tree):
                return True
        return False

    def build(self, root, table_map):
        return sqlalchemy_provider(root)
