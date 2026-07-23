# ---------------------------------------------------------------------------
# Framework overlays — the Framework layer's pluggable "code -> IR" surface.
#
# A FrameworkOverlay reads association (and, for Prisma/Django, column)
# semantics out of application code and returns a ProviderResult that merge_ir
# folds over the DB layer. The built-in overlays (Rails, Prisma, Django) live in
# the sibling frameworks/*.py fragments — one per framework — and each
# @register_overlay's its class. Adding one is "drop a frameworks/<name>.py that
# subclasses FrameworkOverlay and registers itself"; the build picks the folder
# up automatically, and a --adapter plugin can register the very same way at run
# time (register_overlay is exported alongside register_adapter).
#
# This base file also holds the machinery the overlays share: the inflector
# (pluralize / to_snake / class_to_table, used by Rails and by FK inference) and
# the post-merge *_id FK inference pass. Those stay module-level free functions
# because the test suite calls them by name.
# ---------------------------------------------------------------------------
import abc
import re
import sys


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
# Overlay base class + registry
# ---------------------------------------------------------------------------
FRAMEWORK_OVERLAYS = []   # registered FrameworkOverlay subclasses (see priority)


def register_overlay(cls):
    """Class decorator: register a FrameworkOverlay subclass. Returns the class
    unchanged. Detection consults overlays in `priority` order (see
    framework_overlay_for), so registration/concat order does not matter. A
    later registration with the same `name` replaces the earlier class, matching
    DB adapter plugin overrides and making plugin reload deterministic."""
    if not isinstance(cls, type) or not issubclass(cls, FrameworkOverlay):
        raise TypeError('register_overlay requires a FrameworkOverlay subclass')
    if getattr(cls, '__abstractmethods__', None):
        raise TypeError(f'{cls.__name__} must implement detect(root) and build(root, table_map)')
    if not isinstance(cls.name, str) or not re.fullmatch(r'[a-z][a-z0-9_-]*', cls.name):
        raise ValueError(f'{cls.__name__}.name must be a lower-case source identifier')
    if not isinstance(cls.priority, int):
        raise ValueError(f'{cls.__name__}.priority must be an integer')
    if not isinstance(cls.expects, str) or not cls.expects.strip():
        raise ValueError(f'{cls.__name__}.expects must describe accepted input')
    FRAMEWORK_OVERLAYS[:] = [existing for existing in FRAMEWORK_OVERLAYS
                             if existing.name != cls.name]
    FRAMEWORK_OVERLAYS.append(cls)
    return cls


class FrameworkOverlay(abc.ABC):
    """Abstract base for a framework overlay: recognise an application-code
    project and parse its schema/associations into a ProviderResult.

    To add another framework, subclass this and:
      * set `name` to the short provider id recorded in the IR's Source (§5),
        e.g. 'sequelize';
      * set `priority` if detection ordering matters (lower runs first; the
        first overlay whose detect() is true wins);
      * set `expects` to a short human description of the input layout the
        overlay parses — it is quoted in the "found nothing to parse" error
        a typed `sources[].type` run raises when build() returns no tables;
      * implement detect(root) -> bool over a --models path (a directory, or a
        single schema file);
      * implement build(root, table_map) -> ProviderResult (table_map is the
        Rails table override map; ignore it if not applicable);
      * decorate the class with @register_overlay.

    Untyped ``--models`` calls ``detect`` on every overlay in priority order,
    then calls the winner's ``build``. Typed ``<name>.models`` skips detection
    and calls ``build`` directly. ``build`` must return exactly::

        make_provider_result('framework', self.name, tables,
                             location=str(root), warnings=[])

    ``tables`` is sparse IR: association-only overlays may return
    ``{'users': {'associations': [...]}}`` without columns/indexes/primary_key.
    Results are checked by ``validate_provider_result`` before merge; see that
    function and ``validate_tables_ir`` for the executable output contract.
    Warnings are human-readable strings without a leading ``Warning:``.
    """
    name = ''
    priority = 100
    expects = ''  # human description of the expected input layout (see above)

    @abc.abstractmethod
    def detect(self, root):
        """True if `root` (a --models path) is a project this overlay handles.

        Two legitimate detection strategies exist among the built-in overlays
        (Fable design note, F1/F2): **marker-based** (Rails/Django/Prisma/
        Laravel — a decisive, root-level artifact like `manage.py`/
        `schema.prisma`/an `artisan`-style Eloquent base class exists, so a
        SHALLOW, root-only check is correct; going recursive here would only
        add false-positive risk from an unrelated nested project) vs.
        **content-based** (SQLAlchemy — no marker file or fixed layout exists;
        the models themselves, wherever they live in the tree, are the only
        evidence). A content-based overlay's `detect()` MUST recurse exactly
        as deep as its own `build()` does — a shallow content-based `detect()`
        would create the opposite bug, silently reporting "not detected" for
        a project `build()` could actually parse. When adding a new overlay,
        classify it as one or the other first; that decision determines
        whether `detect()` should recurse, not the other way around."""
        raise NotImplementedError

    @abc.abstractmethod
    def build(self, root, table_map):
        """Return a framework ProviderResult for `root`."""
        raise NotImplementedError


def framework_overlay_for(root):
    """The first registered overlay (in priority order) that recognises `root`,
    or None. Instantiating each is cheap — they hold no state."""
    for cls in sorted(FRAMEWORK_OVERLAYS, key=lambda c: (c.priority, c.name)):
        overlay = cls()
        if overlay.detect(root):
            return overlay
    return None

def framework_overlays_matching(root):
    """Every registered overlay (in priority order) that recognises `root`, not
    just the first. Used only to report ambiguity when an untyped --models/
    config `models` path matches more than one framework (sources.py); the
    winner is always overlays_matching(root)[0], i.e. framework_overlay_for's
    pick — this function changes nothing about detection, only what gets
    reported about it."""
    return [cls for cls in sorted(FRAMEWORK_OVERLAYS, key=lambda c: (c.priority, c.name))
            if cls().detect(root)]

def detect_code_source(root):
    """Classify a --models path: the `name` of the overlay that recognises it
    (a Rails app/models dir, a Prisma schema, or a Django project), or None."""
    overlay = framework_overlay_for(root)
    return overlay.name if overlay else None

def framework_provider(mroot, table_map=None):
    """Framework ProviderResult (§5). Finds the overlay that recognises the
    --models path and delegates to its build(), which resolves the concrete
    input (the Rails app/models dir, the schema.prisma file, or the Django
    root) and parses it."""
    overlay = framework_overlay_for(mroot)
    if overlay is None:
        expected = ', '.join(f'{cls.name}.models' for cls in sorted(
            FRAMEWORK_OVERLAYS, key=lambda c: (c.priority, c.name)))
        sys.exit(f'Error: could not detect the code kind at {mroot} '
                 f'(registered model types: {expected or "none"})')
    result = overlay.build(mroot, table_map)
    try:
        return validate_provider_result(
            result, f'framework overlay {overlay.name!r}',
            expected_kind='framework', expected_provider=overlay.name)
    except ValueError as e:
        sys.exit(f'Error: invalid output from framework overlay {overlay.name!r}: {e}')

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
