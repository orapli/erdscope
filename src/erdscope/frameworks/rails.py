# ---------------------------------------------------------------------------
# Rails model parser (app/models/**/*.rb)
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

@register_overlay
class RailsOverlay(FrameworkOverlay):
    """A Rails project: an app/models directory (or a directory of *.rb model
    files). Contributes associations only — no columns."""
    name = 'rails'
    priority = 1
    expects = ('a Rails app/models directory (or a directory of *.rb model '
               'files) declaring at least one model')

    def detect(self, root):
        return root.is_dir() and (
            (root / 'app' / 'models').is_dir() or any(root.glob('*.rb')))

    def build(self, root, table_map):
        mdir = root / 'app' / 'models' if (root / 'app' / 'models').is_dir() else root
        return rails_provider(mdir, table_map)
