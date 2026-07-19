# ---------------------------------------------------------------------------
# Laravel Eloquent model parser (a directory of *.php model files, typically
# app/Models) — regex-based static analysis, same flavor as rails.py.
#
# There is no PHP AST available under the zero-dependency constraint (Django's
# overlay gets ast.parse() for free because Django models ARE Python; Eloquent
# models are PHP), so this parses `$this-><relation>(...)` calls out of
# comment-stripped source with regexes, the same tactic rails_schema.py uses
# for Ruby: never execute the input, and never silently drop a construct the
# regex can't make sense of — an unresolvable relation target is a file:line
# warning and a skipped association, not a quiet omission.
# ---------------------------------------------------------------------------
_LARAVEL_MODEL_BASE_RE = re.compile(r'^(?:\w*Model|Authenticatable|Pivot)$')
_LARAVEL_DETECT_CLASS_RE = re.compile(r'class \w+ extends (\w*Model|Authenticatable|Pivot)')
_LARAVEL_DETECT_ELOQUENT_USE = 'use Illuminate\\Database\\Eloquent\\'
_LARAVEL_CLASS_DEF_RE = re.compile(r'class\s+(\w+)\s+extends\s+([\w\\]+)')
_LARAVEL_TABLE_OVERRIDE_RE = re.compile(r"protected\s+\$table\s*=\s*['\"]([^'\"]+)['\"]")
_LARAVEL_METHOD_DEF_RE = re.compile(r'function\s+(\w+)\s*\([^)]*\)')
_LARAVEL_ASSOC_CALL_RE = re.compile(
    r'\$this->(hasMany|hasOne|belongsTo|belongsToMany|morphTo|morphMany|morphOne|morphToMany)'
    r'\s*\(([^)]*)\)'
)
_LARAVEL_CLASS_CONST_RE = re.compile(r'^\\?([\w\\]+)::class$')

# Eloquent method -> IR association type. morphMany/morphOne/morphToMany share
# the plain relation's type (they draw a real edge to a concrete class, unlike
# morphTo) and are told apart from their non-morph counterparts only by the
# `polymorphic` flag added in _build_association.
_LARAVEL_RELATION_TYPE = {
    'hasMany': 'has_many', 'hasOne': 'has_one', 'belongsTo': 'belongs_to',
    'belongsToMany': 'has_and_belongs_to_many',
    'morphMany': 'has_many', 'morphOne': 'has_one', 'morphToMany': 'has_and_belongs_to_many',
}


def _iter_php_files(root):
    """Every *.php under `root`, sorted, excluding anything under a `vendor`
    directory (composer dependencies — never the app's own models)."""
    for path in sorted(root.rglob('*.php')):
        if 'vendor' in path.relative_to(root).parts:
            continue
        yield path


def _looks_like_laravel_models(root):
    """detect() signal: at least one .php file DIRECTLY in `root` (not a
    recursive descent — same shallow-signal convention Rails' bare-directory
    check and Django's manage.py check use, so pointing detect at a large,
    unrelated directory that merely happens to contain a .php file somewhere
    deep inside it — e.g. a repo's whole test suite — never false-positives)
    either declares a class extending Model/Authenticatable/Pivot, or imports
    the Eloquent namespace (a weaker but still telling signal — e.g. a base
    class defined elsewhere in the same app whose OWN extends clause isn't in
    this file). A Laravel PROJECT ROOT (the directory you'd `--models ./my-app`)
    has no .php files at the top, so the conventional `app/Models` marker
    directory is checked the same shallow way — mirroring how Rails detection
    accepts an app root via its own marker layout. build()'s actual parse
    still descends recursively (see _iter_php_files) once a project is
    already known to be Laravel."""
    candidates = sorted(root.glob('*.php')) + sorted((root / 'app' / 'Models').glob('*.php'))
    for path in candidates:
        try:
            content = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        if (_LARAVEL_DETECT_CLASS_RE.search(content)
                or _LARAVEL_DETECT_ELOQUENT_USE in content):
            return True
    return False


def _strip_php_comments(content):
    """Blank out `//` line comments and `/* ... */` block comments before any
    regex scan — the same comment-stripped-source tactic rails.py uses for
    Ruby's `#` comments, so a relation call or `$table` assignment mentioned
    only in a comment is never mistaken for real code. Newlines inside a block
    comment are preserved (replaced 1:1) so absolute line numbers computed
    later for warnings stay correct."""
    content = re.sub(r'//[^\n]*', '', content)
    return re.sub(r'/\*.*?\*/', lambda m: '\n' * m.group(0).count('\n'), content, flags=re.S)


def _split_top_level(s):
    """Split a PHP call's argument list on commas at bracket/paren/quote depth
    0 — the same shared-tactic call-argument splitter rails_schema.py uses for
    Ruby, reimplemented here for PHP (single/double-quote strings with
    backslash escaping, same shape as Ruby's for this purpose)."""
    parts, depth, cur, quote, i, n = [], 0, [], None, 0, len(s)
    while i < n:
        c = s[i]
        if quote:
            cur.append(c)
            if c == '\\' and i + 1 < n:
                cur.append(s[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in '\'"':
            quote = c
            cur.append(c)
            i += 1
            continue
        if c in '([{':
            depth += 1
            cur.append(c)
            i += 1
            continue
        if c in ')]}':
            depth -= 1
            cur.append(c)
            i += 1
            continue
        if c == ',' and depth == 0:
            parts.append(''.join(cur).strip())
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    tail = ''.join(cur).strip()
    if tail:
        parts.append(tail)
    return parts


def _string_literal(tok):
    """A single/double-quoted PHP string literal's raw contents, or None if
    `tok` isn't one. Deliberately just a quote-strip, not a full PHP-string
    unescaper — table/class names realistically never contain an escape
    sequence, so there is nothing more here worth getting right."""
    tok = tok.strip()
    if len(tok) >= 2 and tok[0] in '\'"' and tok[-1] == tok[0]:
        return tok[1:-1]
    return None


def _target_class_name(tok):
    """The bare class name out of a relation call's related-model argument:
    `Post::class` or a quoted `'App\\Models\\Post'` / `'Post'` string — the
    two ways Eloquent code names a related model. None for anything else (a
    variable, a function call, a config() lookup, ...), which the caller
    treats as unresolvable — warn and skip, never guess."""
    tok = tok.strip()
    m = _LARAVEL_CLASS_CONST_RE.match(tok)
    if m:
        return m.group(1).rsplit('\\', 1)[-1]
    lit = _string_literal(tok)
    if lit is not None:
        return lit.rsplit('\\', 1)[-1]
    return None


def _build_association(method, name, args_str, target_override, path, line_no, warn):
    """One `$this-><method>(...)` call -> an association dict, or None if the
    call is unparseable (warn() is called in that case — never a silent
    drop)."""
    if method == 'morphTo':
        # No related-class argument at all: morphTo's real target is only
        # known at runtime (whichever row the *_type column names), so — like
        # Rails' `belongs_to ..., polymorphic: true` and Django's
        # GenericForeignKey — this is a guessed pseudo-target that surfaces in
        # the details pane only; no edge is ever drawn to it.
        target = pluralize(to_snake(name))
        return {'type': 'belongs_to', 'name': name, 'target': target,
                'foreign_key': f'{to_snake(name)}_id', 'polymorphic': True}

    args = _split_top_level(args_str)
    if not args:
        warn(path, line_no, f'{method}() call for {name!r} has no arguments '
             '— cannot resolve its related model, skipped')
        return None
    cls_name = _target_class_name(args[0])
    if cls_name is None:
        warn(path, line_no, f'{method}() call for {name!r} — unresolvable '
             f'related-model argument {args[0]!r}, skipped')
        return None
    target = target_override.get(class_to_table(cls_name), class_to_table(cls_name))

    if method == 'belongsTo':
        # Eloquent's own convention default, same backfill rails.py does for
        # `belongs_to` — a second positional string argument names an
        # explicit foreign key and wins over the guess.
        foreign_key = f'{to_snake(name)}_id'
        if len(args) > 1:
            override = _string_literal(args[1])
            if override:
                foreign_key = override
        return {'type': 'belongs_to', 'name': name, 'target': target,
                'foreign_key': foreign_key}

    if method == 'belongsToMany':
        assoc = {'type': 'has_and_belongs_to_many', 'name': name, 'target': target}
        if len(args) > 1:
            pivot = _string_literal(args[1])
            if pivot:
                assoc['through'] = pivot
        return assoc

    if method in ('morphMany', 'morphOne', 'morphToMany'):
        return {'type': _LARAVEL_RELATION_TYPE[method], 'name': name,
                'target': target, 'polymorphic': True}

    return {'type': _LARAVEL_RELATION_TYPE[method], 'name': name, 'target': target}


def _parse_class_relations(info, frag, target_override, warn):
    """Scan one model class's body for relation-method definitions and, inside
    each, `$this-><relation>(...)` calls — same "slice the body between this
    declaration and the next" trick rails.py uses for class bodies, applied
    here one level down (method bodies inside an already-isolated class
    body)."""
    body, body_offset, clean, path = (info['body'], info['body_offset'],
                                      info['clean'], info['path'])
    method_matches = list(_LARAVEL_METHOD_DEF_RE.finditer(body))
    for i, mm in enumerate(method_matches):
        method_name = mm.group(1)
        mstart = mm.end()
        mend = method_matches[i + 1].start() if i + 1 < len(method_matches) else len(body)
        method_body = body[mstart:mend]
        method_abs_start = body_offset + mstart
        for call in _LARAVEL_ASSOC_CALL_RE.finditer(method_body):
            eloquent_method, args_str = call.group(1), call.group(2)
            line_no = clean.count('\n', 0, method_abs_start + call.start()) + 1
            assoc = _build_association(eloquent_method, method_name, args_str,
                                       target_override, path, line_no, warn)
            if assoc is not None:
                frag['associations'].append(assoc)


def _parse_laravel_models(models_dir, fragment, table_map, warn):
    """Laravel static analysis, writing association fragments into `fragment`
    (keyed by table_name, each entry `{'associations': [...]}`, no columns).
    See laravel_provider for the contract."""
    # class name -> {base, body, body_offset, clean, path}, one entry per
    # `class X extends Y` found anywhere under models_dir (vendor/ excluded).
    # `body` is the comment-stripped text between this class declaration and
    # the next (or EOF) — same slicing trick rails.py uses, one file at a
    # time here since PHP (unlike Rails' one-file-per-concern convention)
    # doesn't reliably share bodies across files the way `include`d Ruby
    # concerns do.
    class_info = {}
    for path in _iter_php_files(models_dir):
        try:
            content = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        clean = _strip_php_comments(content)
        matches = list(_LARAVEL_CLASS_DEF_RE.finditer(clean))
        for i, cm in enumerate(matches):
            start = cm.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(clean)
            class_info[cm.group(1)] = {
                'base': cm.group(2), 'body': clean[start:end],
                'body_offset': start, 'clean': clean, 'path': path,
            }

    # A class counts as an Eloquent model when its base class (namespace
    # stripped) is Model/*Model/Authenticatable/Pivot — the same signal
    # detect() uses, just applied per-class rather than per-file, and a
    # little more lenient about a namespace-qualified extends clause since
    # here (unlike the fast directory-level detect() scan) we've already
    # committed to parsing this project as Laravel.
    model_names = [name for name, c in class_info.items()
                   if _LARAVEL_MODEL_BASE_RE.match(c['base'].rsplit('\\', 1)[-1])]

    # protected $table = '...' overrides the naming convention for this
    # model's own table, same escape hatch Rails' self.table_name is.
    table_overrides = {}
    for name in model_names:
        m = _LARAVEL_TABLE_OVERRIDE_RE.search(class_info[name]['body'])
        if m:
            table_overrides[name] = m.group(1)

    def resolve_table(name):
        if name in table_map:
            return table_map[name]  # explicit --table-map wins over everything
        if name in table_overrides:
            return table_overrides[name]
        return class_to_table(name)

    # Same "naive-guess -> real-table" redirect rails.py builds: a model whose
    # own table is overridden ($table or table_map) still gets referenced by
    # OTHER models via the naive class_to_table() convention (a bare
    # `Widget::class` never consults $table), so every association target
    # resolved below is passed through this map too.
    target_override = {}
    for name in model_names:
        naive = class_to_table(name)
        real = resolve_table(name)
        if naive != real:
            target_override[naive] = real

    for class_name in sorted(model_names):
        info = class_info[class_name]
        table_name = resolve_table(class_name)
        frag = fragment.setdefault(table_name, {'associations': []})
        _parse_class_relations(info, frag, target_override, warn)


def laravel_provider(models_dir, table_map=None):
    """ProviderResult for a directory of Eloquent model *.php files (REFACTOR
    PLAN §5's ProviderResult contract). Static regex analysis only — no PHP
    parser, no PHP execution; an unparseable relation call is a file:line
    warning and a skipped association, never a silent drop.

    Association-only, exactly like Rails: Eloquent relation methods say
    nothing about column types, so each table Fragment carries just
    `associations` and OMITS the `columns` key entirely (§4: an absent key
    keeps the lower/DB layer's columns, so a framework-only merge never erases
    real schema). `database/migrations/*.php` and mass-assignment
    ($fillable/$casts) are deliberately NOT read to synthesize columns for
    this pass — $fillable is a whitelist, not the full column set, and
    inferring columns from it would misrepresent the schema; a typed
    `laravel.migrations` source is a plausible future addition, not this one.
    """
    models_dir = Path(models_dir)
    # accept a Laravel project root: when the conventional app/Models exists,
    # parse that subtree instead of the whole app (routes/tests/database PHP
    # is never model code) — the same root the detect() marker check accepts
    if (models_dir / 'app' / 'Models').is_dir():
        models_dir = models_dir / 'app' / 'Models'
    fragment = {}
    warnings = []

    def warn(path, line_no, msg):
        try:
            display = path.relative_to(models_dir)
        except ValueError:
            display = path
        warnings.append(f'{display}:{line_no}: {msg}')

    if models_dir.is_dir():
        _parse_laravel_models(models_dir, fragment, table_map or {}, warn)
    return make_provider_result('framework', 'laravel', fragment,
                                location=str(models_dir), warnings=warnings)


@register_overlay
class LaravelOverlay(FrameworkOverlay):
    """A Laravel project: a directory of Eloquent model *.php files (typically
    app/Models). Contributes associations only — no columns (DB-first, the
    same asymmetry as Rails)."""
    name = 'laravel'
    # Must run BEFORE rails (priority 1): on a case-insensitive filesystem
    # (macOS/Windows default) a Laravel root's `app/Models` satisfies Rails'
    # weak `app/models`-directory-exists check, so the weak check would
    # claim the project first. This detect() demands actual Eloquent
    # evidence inside a .php file, so it can never claim a Rails project.
    priority = 0
    expects = ('a directory of Eloquent model *.php files (typically '
               'app/Models) declaring at least one model')

    def detect(self, root):
        return root.is_dir() and _looks_like_laravel_models(root)

    def build(self, root, table_map):
        return laravel_provider(root, table_map)
