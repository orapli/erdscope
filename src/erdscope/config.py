CONFIG_DEFAULTS = {
    'output': 'erd.html', 'models': None, 'excel': None, 'excel_template': None,
    'max_rows': 15, 'only': None, 'exclude': None, 'infer_fk': False, 'table_map': {},
}
# Connection fields, broken apart rather than one mysql:// URL string —
# there's deliberately no password/url field: a single URL is one string
# away from someone pasting in a password, but there's no literal field to
# accidentally fill in when the pieces are separate. One config file per
# database (e.g. erdscope.staging.json / erdscope.prod.json) is the
# intended way to point --config at different targets.
CONFIG_CONNECTION_KEYS = {'engine', 'host', 'port', 'user', 'database'}
CONFIG_PASSWORD_KEYS = {'password', 'passwd', 'pwd', 'url', 'database_url'}
# Schema-input keys (REFACTOR_PLAN.md §6.2): `tables` (a map table_name ->
# TableFragment) and `title`. `name` is deliberately NOT accepted at the top
# level (§6.2/§18) — it's overloaded for tables/columns/associations, so a
# stray top-level `name` is far more likely a mistake than a title. These are
# validated syntactically at load time but NOT yet wired into the pipeline
# (REFACTOR_PLAN.md §15 Step 3 is validation-only; construction/merge is Step 7).
CONFIG_SCHEMA_KEYS = {'tables', 'title'}
# The four association kinds and the two list-merge modes, reused by the
# recursive Fragment/DropOperation validators below.
_CONFIG_ASSOC_TYPES = {'has_many', 'belongs_to', 'has_one', 'has_and_belongs_to_many'}
_CONFIG_MODE_KEYS = ('columns_mode', 'indexes_mode', 'associations_mode')
# Fixed per-structure allow-lists for nested keys (typo protection, §6.4 "typo
# を黙って無視しない"). A misspelled nested key (`primary_ky`, `nulable`) is
# silently ignored otherwise. Deliberately spelled out here — NOT derived from
# the Step-2 contract types — so the accepted surface is explicit and stable
# regardless of how the internal IR shape evolves.
_CONFIG_TABLE_KEYS = {'comment', 'primary_key', 'columns', 'indexes', 'associations',
                      'drop', 'columns_mode', 'indexes_mode', 'associations_mode'}
_CONFIG_COLUMN_KEYS = {'name', 'type', 'sql_type', 'nullable', 'primary',
                       'default', 'extra', 'comment', 'drop'}
_CONFIG_INDEX_KEYS = {'name', 'columns', 'unique', 'drop'}
_CONFIG_ASSOC_KEYS = {'type', 'name', 'target', 'foreign_key', 'through',
                      'polymorphic', 'drop'}

def _reject_unknown_keys(obj, allowed, path, where):
    unknown = set(obj) - allowed
    if unknown:
        sys.exit(f"Error: {path} `{where}`: unknown key(s): "
                 f"{', '.join(repr(k) for k in sorted(unknown))}")
# Expected type per key, checked at load time — YAML/JSON scalars are
# exactly where a config typo goes undetected otherwise: max_rows as the
# string "fifteen" would reach the JS as a bare identifier (ReferenceError,
# dead viewer); `only` as a bare string instead of a list gets iterated
# character-by-character by fnmatch (silently matches everything, filter
# does nothing); infer_fk as the string "false" is truthy. host/port/user/
# database get their own, more detailed checks in assemble_config_url().
# `models` is validated separately (below) because it accepts str OR list[str]
# — multiple frameworks (§18 #3 / §10), e.g. a Rails app AND a schema.prisma.
CONFIG_TYPES = {
    'output': str, 'excel': str, 'excel_template': str, 'max_rows': int,
    'only': list, 'exclude': list, 'infer_fk': bool, 'table_map': dict,
    'relations': list, 'title': str,
}

def load_config(args):
    """Load the config file: --config PATH if given, else auto-discovered
    .erdscope.json / .erdscope.yml / .erdscope.yaml in the cwd (in that
    order), unless --no-config. YAML needs PyYAML installed — JSON always
    works with no dependency, same "works out of the box, YAML if you
    already have it" spirit as the PyMySQL/mysql-CLI fallback above."""
    if getattr(args, 'no_config', False):
        if getattr(args, 'config', None):
            sys.exit('Error: --config and --no-config are mutually exclusive')
        return {}
    path = None
    if getattr(args, 'config', None):
        path = Path(args.config).expanduser().resolve()
        if not path.exists():
            sys.exit(f'Error: config file {path} does not exist')
    else:
        for candidate in ('.erdscope.json', '.erdscope.yml', '.erdscope.yaml'):
            c = Path.cwd() / candidate
            if c.exists():
                path = c
                break
    if path is None:
        return {}
    print(f'Using config: {path}', file=sys.stderr)
    text = path.read_text(encoding='utf-8')
    if path.suffix == '.json':
        try:
            config = json.loads(text)
        except json.JSONDecodeError as e:
            sys.exit(f'Error: failed to parse {path} as JSON: {e}')
    else:
        try:
            import yaml
        except ImportError:
            sys.exit(f'Error: {path} is YAML but PyYAML is not installed '
                      f'(pip install pyyaml, or use a .json config instead)')
        try:
            config = yaml.safe_load(text) or {}
        except yaml.YAMLError as e:
            sys.exit(f'Error: failed to parse {path} as YAML: {e}')
    if not isinstance(config, dict):
        sys.exit(f'Error: {path} must contain a JSON/YAML object at the top level')
    password_keys = CONFIG_PASSWORD_KEYS & set(config)
    if password_keys:
        sys.exit(f'Error: {path} has {", ".join(sorted(password_keys))} — passwords '
                 f'(or a full connection URL, which could carry one) are not supported '
                 f'in the config file. Use `host`/`port`/`user`/`database` instead, and '
                 f'MYSQL_PWD, ~/.my.cnf, or the interactive prompt for the password')
    unknown = (set(config) - set(CONFIG_DEFAULTS) - {'relations', 'adapters', 'sources', 'version', 'notes', 'groups'}
               - CONFIG_CONNECTION_KEYS - CONFIG_SCHEMA_KEYS)
    if unknown:
        sys.exit(f'Error: {path} has unknown key(s): {", ".join(sorted(unknown))}')
    _check_config_types(config, path)
    return config

def _check_config_types(config, path):
    for key, expected in CONFIG_TYPES.items():
        if key not in config:
            continue
        val = config[key]
        # bool is a subclass of int in Python, so an explicit isinstance(int)
        # check would let `max_rows: true` through as 1 — and the reverse,
        # `infer_fk: 1`, needs the same explicit guard to be rejected
        ok = (isinstance(val, bool) if expected is bool else
              isinstance(val, int) and not isinstance(val, bool) if expected is int else
              isinstance(val, expected))
        if not ok:
            sys.exit(f'Error: {path} `{key}` must be a {expected.__name__}, got {val!r}')
    # version: purely a documented marker for the config *shape* users are
    # shown (e.g. in erdscope.example.yml) — no runtime behavior hangs off it
    # yet, so the only valid value is the literal int 1. Same bool-vs-int
    # discipline as above: `version: true` must not slip through as 1.
    if 'version' in config:
        v = config['version']
        if not (isinstance(v, int) and not isinstance(v, bool) and v == 1):
            sys.exit(f'Error: {path} `version` must be 1 (the only supported '
                     f'config version), got {v!r}')
    # models: a single path (str) or a list of paths (str) — multiple frameworks
    if 'models' in config:
        m = config['models']
        if isinstance(m, str):
            pass
        elif isinstance(m, list):
            for i, item in enumerate(m):
                if not isinstance(item, str):
                    sys.exit(f'Error: {path} `models[{i}]` must be a string, got {item!r}')
        else:
            sys.exit(f'Error: {path} `models` must be a string or a list of strings, '
                     f'got {m!r}')
    for key in ('only', 'exclude'):
        if key in config and any(not isinstance(x, str) for x in config[key]):
            sys.exit(f'Error: {path} `{key}` must be a list of strings')
    # adapters: a single path (str) or a list of paths (str) — custom DB
    # adapter plugin files, same str-or-list shape as `models`
    if 'adapters' in config:
        a = config['adapters']
        if isinstance(a, str):
            pass
        elif isinstance(a, list):
            for i, item in enumerate(a):
                if not isinstance(item, str):
                    sys.exit(f'Error: {path} `adapters[{i}]` must be a string, got {item!r}')
        else:
            sys.exit(f'Error: {path} `adapters` must be a string or a list of strings, '
                     f'got {a!r}')
    if 'table_map' in config and any(not isinstance(v, str) for v in config['table_map'].values()):
        sys.exit(f'Error: {path} `table_map` values must all be strings')
    if 'relations' in config and any(not isinstance(r, dict) for r in config['relations']):
        sys.exit(f'Error: {path} `relations` must be a list of objects '
                 '({table, column, references, ...})')
    if 'tables' in config:
        _check_config_tables(config['tables'], path)
    if 'sources' in config:
        _check_config_sources(config['sources'], path)
    if 'notes' in config:
        _check_config_notes(config['notes'], path)
    if 'groups' in config:
        _check_config_groups(config['groups'], path)

# ---------------------------------------------------------------------------
# `sources:` — typed code-source declarations (D5). Purely syntactic here:
# shape, required fields, allow-listed keys, Config-internal duplicate `id`.
# Whether `type` names a REGISTERED source type is a dispatch-time (sources.py
# run_input_specs) concern, not a load-time one — an --adapter plugin loaded
# later in the pipeline can still register its own overlay/type in time.
# ---------------------------------------------------------------------------
_CONFIG_SOURCE_KEYS = {'id', 'type', 'path', 'allow_empty'}

def _check_config_sources(sources, path):
    if not isinstance(sources, list):
        sys.exit(f'Error: {path} `sources` must be a list of objects '
                 '({id, type, path})')
    seen = set()
    for i, s in enumerate(sources):
        sw = f'sources[{i}]'
        if not isinstance(s, dict):
            sys.exit(f'Error: {path} `{sw}` must be an object')
        _reject_unknown_keys(s, _CONFIG_SOURCE_KEYS, path, sw)
        for key in ('id', 'type', 'path'):
            val = s.get(key)
            if not isinstance(val, str) or not val:
                sys.exit(f'Error: {path} `{sw}` needs a non-empty string `{key}`')
        _check_bool(s.get('allow_empty'), 'allow_empty' in s, path, f'{sw}.allow_empty')
        if s['id'] in seen:
            sys.exit(f'Error: {path} `sources` has a duplicate id {s["id"]!r}')
        seen.add(s['id'])

# ---------------------------------------------------------------------------
# `notes:` — design-documentation sidecar (notes Phase 1). Purely syntactic
# here: shape, required fields, allow-listed keys, Config-internal duplicate
# `id`, and link URL scheme (http/https only — the first line of XSS defense,
# since a note's links render as real <a href> in the viewer). Whether the
# note's TARGET actually exists (a table/relation naming something real) is a
# semantic, final-IR-after-merge concern — see resolve_and_validate_notes in
# providers.py, not here. Mirrors the tables §6.4①/② two-stage split.
# ---------------------------------------------------------------------------
_CONFIG_NOTE_KEYS = {'id', 'target', 'title', 'text', 'links'}
_CONFIG_NOTE_LINK_KEYS = {'label', 'url'}
_CONFIG_NOTE_TARGET_TYPES = {'global', 'table', 'relation'}
_CONFIG_NOTE_TARGET_KEYS = {
    'global': {'type'},
    'table': {'type', 'table'},
    # NOTE: `type` here is the target-KIND discriminator (already required to
    # be 'relation') — an association-type narrowing key (has_many/belongs_to/
    # has_one/has_and_belongs_to_many, mirroring _CONFIG_ASSOC_TYPES) can't
    # reuse that same name without colliding with it, so it's `assoc_type`
    # (Sol finding #5: narrow an ambiguous relation note by role, matching the
    # resolved association's `type`, which the OUTPUT entry surfaces as `type`
    # per the viewer contract — only the config INPUT key differs).
    'relation': {'type', 'source_table', 'target_table', 'foreign_key', 'name',
                 'through', 'polymorphic', 'assoc_type'},
}

def _check_config_notes(notes, path):
    if not isinstance(notes, list):
        sys.exit(f'Error: {path} `notes` must be a list of objects '
                 '({id, target, text, ...})')
    seen = set()
    for i, n in enumerate(notes):
        nw = f'notes[{i}]'
        if not isinstance(n, dict):
            sys.exit(f'Error: {path} `{nw}` must be an object')
        _reject_unknown_keys(n, _CONFIG_NOTE_KEYS, path, nw)
        note_id = n.get('id')
        if not isinstance(note_id, str) or not note_id:
            sys.exit(f'Error: {path} `{nw}` needs a non-empty string `id`')
        if note_id in seen:
            sys.exit(f'Error: {path} `notes` has a duplicate id {note_id!r}')
        seen.add(note_id)
        text = n.get('text')
        if not isinstance(text, str) or not text:
            sys.exit(f'Error: {path} note {note_id!r} needs a non-empty string `text`')
        if 'title' in n and n['title'] is not None and not isinstance(n['title'], str):
            sys.exit(f'Error: {path} note {note_id!r} `title` must be a string')
        if 'links' in n:
            _check_config_note_links(n['links'], path, note_id)
        target = n.get('target')
        if not isinstance(target, dict):
            sys.exit(f'Error: {path} note {note_id!r} needs an object `target`')
        ttype = target.get('type')
        if ttype not in _CONFIG_NOTE_TARGET_TYPES:
            sys.exit(f'Error: {path} note {note_id!r} `target.type` must be one of '
                     f'{", ".join(sorted(_CONFIG_NOTE_TARGET_TYPES))}, got {ttype!r}')
        _reject_unknown_keys(target, _CONFIG_NOTE_TARGET_KEYS[ttype], path,
                             f'{nw}.target')
        if ttype == 'table':
            tbl = target.get('table')
            if not isinstance(tbl, str) or not tbl:
                sys.exit(f'Error: {path} note {note_id!r} needs a non-empty string '
                         '`target.table`')
        elif ttype == 'relation':
            for key in ('source_table', 'target_table'):
                val = target.get(key)
                if not isinstance(val, str) or not val:
                    sys.exit(f'Error: {path} note {note_id!r} needs a non-empty string '
                             f'`target.{key}`')
            for key in ('foreign_key', 'name', 'through'):
                if key in target and target[key] is not None:
                    val = target[key]
                    if isinstance(val, list):
                        sys.exit(f'Error: {path} note {note_id!r} `target.{key}` is a list '
                                 '— composite foreign keys are not supported; use a single '
                                 'column name')
                    if not isinstance(val, str) or not val:
                        sys.exit(f'Error: {path} note {note_id!r} `target.{key}` must be a '
                                 'non-empty string')
            _check_bool(target.get('polymorphic'), 'polymorphic' in target, path,
                       f'{nw}.target.polymorphic')
            if 'assoc_type' in target and target['assoc_type'] is not None:
                at = target['assoc_type']
                if at not in _CONFIG_ASSOC_TYPES:
                    sys.exit(f'Error: {path} note {note_id!r} `target.assoc_type` must be '
                             f'one of {", ".join(sorted(_CONFIG_ASSOC_TYPES))}, got {at!r}')

def _check_config_note_links(links, path, note_id):
    if not isinstance(links, list):
        sys.exit(f'Error: {path} note {note_id!r} `links` must be a list')
    for j, link in enumerate(links):
        lw = f'links[{j}]'
        if not isinstance(link, dict):
            sys.exit(f'Error: {path} note {note_id!r} `{lw}` must be an object')
        _reject_unknown_keys(link, _CONFIG_NOTE_LINK_KEYS, path, f'notes.{lw}')
        if 'label' in link and link['label'] is not None and not isinstance(link['label'], str):
            sys.exit(f'Error: {path} note {note_id!r} `{lw}.label` must be a string')
        url = link.get('url')
        if not isinstance(url, str) or not url:
            sys.exit(f'Error: {path} note {note_id!r} `{lw}` needs a non-empty string `url`')
        if not url.lower().startswith(('http://', 'https://')):
            sys.exit(f'Error: {path} note {note_id!r} `{lw}.url` must start with http:// '
                     f'or https:// (got {url!r})')

# ---------------------------------------------------------------------------
# `groups:` — visual table grouping sidecar (groups Phase 1, DESIGN_ROADMAP §P2).
# Purely syntactic here: shape, required fields, allow-listed keys,
# Config-internal duplicate `id`, and `color` restricted to a hex string (the
# first line of XSS/attribute-injection defense, since a group's color renders
# as a real SVG fill/stroke attribute in the viewer). Whether every member
# TABLE actually exists, and whether any table is claimed by more than one
# group, is a semantic, final-IR-after-merge concern — see
# resolve_and_validate_groups in providers.py, not here. Mirrors the notes
# two-stage split above.
# ---------------------------------------------------------------------------
_CONFIG_GROUP_KEYS = {'id', 'title', 'tables', 'color'}
# Only the valid CSS/SVG hex-color lengths: #rgb, #rgba, #rrggbb, #rrggbbaa.
# A 5- or 7-digit value is not a real hex color — the browser would drop it and
# silently fall back to the default frame color, so reject it at load instead
# (Codex re-review #2).
_CONFIG_GROUP_COLOR_RE = re.compile(r'#([0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})')

def _check_config_groups(groups, path):
    if not isinstance(groups, list):
        sys.exit(f'Error: {path} `groups` must be a list of objects '
                 '({id, tables, title?, color?})')
    seen = set()
    for i, g in enumerate(groups):
        gw = f'groups[{i}]'
        if not isinstance(g, dict):
            sys.exit(f'Error: {path} `{gw}` must be an object')
        _reject_unknown_keys(g, _CONFIG_GROUP_KEYS, path, gw)
        group_id = g.get('id')
        if not isinstance(group_id, str) or not group_id:
            sys.exit(f'Error: {path} `{gw}` needs a non-empty string `id`')
        if group_id in seen:
            sys.exit(f'Error: {path} `groups` has a duplicate id {group_id!r}')
        seen.add(group_id)
        tables = g.get('tables')
        if not isinstance(tables, list) or not tables:
            sys.exit(f'Error: {path} group {group_id!r} needs a non-empty list `tables`')
        seen_tables = set()
        for j, t in enumerate(tables):
            if not isinstance(t, str) or not t:
                sys.exit(f'Error: {path} group {group_id!r} `tables[{j}]` must be a '
                         f'non-empty string, got {t!r}')
            # A table listed twice in ONE group is a config mistake — reject it
            # here at load (like duplicate column/index names), rather than let
            # it reach the cross-group overlap check, which would then blame the
            # group for overlapping with itself (Codex re-review #3).
            if t in seen_tables:
                sys.exit(f'Error: {path} group {group_id!r} lists table {t!r} more than once')
            seen_tables.add(t)
        if 'title' in g and g['title'] is not None and not isinstance(g['title'], str):
            sys.exit(f'Error: {path} group {group_id!r} `title` must be a string')
        if 'color' in g and g['color'] is not None:
            color = g['color']
            if not isinstance(color, str) or not _CONFIG_GROUP_COLOR_RE.fullmatch(color):
                sys.exit(f'Error: {path} group {group_id!r} `color` must be a hex color '
                         f'like "#0d9488", got {color!r}')

# ---------------------------------------------------------------------------
# `tables:` schema-input syntactic validation (REFACTOR_PLAN.md §4.3 / §6.4 ①)
#
# STRICTLY SYNTACTIC (P0-1): these checks need no DB/Framework IR — they verify
# shape, required fields, types, *_mode values, DropOperation identity, and
# Config-internal duplicates only. They must NOT check whether a referenced
# table/column/target actually exists anywhere — that is *semantic* validation
# (§6.4 ②) and runs at apply time (Step 7), once every provider's IR is
# collected. A config that drops or references a not-yet-known table/column is
# valid here on purpose.
# ---------------------------------------------------------------------------
def _check_config_tables(tables, path):
    if not isinstance(tables, dict):
        sys.exit(f'Error: {path} `tables` must be a map of table_name -> table '
                 'definition (an object), not a list or scalar')
    for tname, tdef in tables.items():
        where = f'tables.{tname}'
        if not isinstance(tdef, dict):
            sys.exit(f'Error: {path} `{where}` must be an object')
        _reject_unknown_keys(tdef, _CONFIG_TABLE_KEYS, path, where)
        _check_bool(tdef.get('drop'), 'drop' in tdef, path, f'{where}.drop')
        # comment: str | null (null/"" = explicit delete, a valid Config op)
        if 'comment' in tdef and tdef['comment'] is not None and not isinstance(tdef['comment'], str):
            sys.exit(f'Error: {path} `{where}.comment` must be a string or null')
        # primary_key: str | list[str] | null (list = composite PK, §4.2/§6.9)
        if 'primary_key' in tdef:
            pk = tdef['primary_key']
            if not (pk is None or isinstance(pk, str)
                    or (isinstance(pk, list) and all(isinstance(x, str) for x in pk))):
                sys.exit(f'Error: {path} `{where}.primary_key` must be a string, a list of '
                         'strings (composite PK), or null')
        for mk in _CONFIG_MODE_KEYS:
            if mk in tdef and tdef[mk] not in ('merge', 'replace'):
                sys.exit(f'Error: {path} `{where}.{mk}` must be "merge" or "replace", '
                         f'got {tdef[mk]!r}')
        if 'columns' in tdef:
            _check_config_columns(tdef['columns'], path, where)
        if 'indexes' in tdef:
            _check_config_indexes(tdef['indexes'], path, where)
        if 'associations' in tdef:
            _check_config_associations(tdef['associations'], path, where)

def _check_bool(val, present, path, where):
    if present and not isinstance(val, bool):
        sys.exit(f'Error: {path} `{where}` must be true or false, got {val!r}')

def _check_config_columns(columns, path, where):
    if not isinstance(columns, list):
        sys.exit(f'Error: {path} `{where}.columns` must be a list')
    seen = set()
    for i, col in enumerate(columns):
        cw = f'{where}.columns[{i}]'
        if not isinstance(col, dict):
            sys.exit(f'Error: {path} `{cw}` must be an object')
        _reject_unknown_keys(col, _CONFIG_COLUMN_KEYS, path, cw)
        _check_bool(col.get('drop'), 'drop' in col, path, f'{cw}.drop')
        # `name` identifies the column for BOTH a fragment (add/override) and a
        # drop, so it's required either way (§4.3: ColumnFragment / ColumnDrop)
        name = col.get('name')
        if not isinstance(name, str) or not name:
            sys.exit(f'Error: {path} `{cw}` needs a non-empty string `name` '
                     f'({"to identify the column to drop" if col.get("drop") is True else "for the column"})')
        if name in seen:
            sys.exit(f'Error: {path} `{where}.columns` has a duplicate column name {name!r}')
        seen.add(name)
        if col.get('drop') is True:
            continue  # ColumnDrop = { name, drop: true }; no other fields required
        for k in ('type', 'sql_type', 'default', 'extra'):
            if k in col and not isinstance(col[k], str):
                sys.exit(f'Error: {path} `{cw}.{k}` must be a string')
        for k in ('nullable', 'primary'):
            _check_bool(col.get(k), k in col, path, f'{cw}.{k}')
        if 'comment' in col and col['comment'] is not None and not isinstance(col['comment'], str):
            sys.exit(f'Error: {path} `{cw}.comment` must be a string or null')

def _check_config_indexes(indexes, path, where):
    if not isinstance(indexes, list):
        sys.exit(f'Error: {path} `{where}.indexes` must be a list')
    seen = set()
    for i, ix in enumerate(indexes):
        iw = f'{where}.indexes[{i}]'
        if not isinstance(ix, dict):
            sys.exit(f'Error: {path} `{iw}` must be an object')
        _reject_unknown_keys(ix, _CONFIG_INDEX_KEYS, path, iw)
        _check_bool(ix.get('drop'), 'drop' in ix, path, f'{iw}.drop')
        name = ix.get('name')
        if 'name' in ix and (not isinstance(name, str) or not name):
            sys.exit(f'Error: {path} `{iw}.name` must be a non-empty string when given')
        if ix.get('drop') is True:
            # IndexDrop = { name, drop: true } — a drop is still name-
            # mandatory (§7.4): merge.py matches drops by name only, and an
            # unnamed index has no stable cross-layer identity to drop by.
            if not name:
                sys.exit(f'Error: {path} `{iw}` needs a non-empty string `name` '
                         '(an index drop must be named)')
            if name in seen:
                sys.exit(f'Error: {path} `{where}.indexes` has a duplicate index name {name!r}')
            seen.add(name)
            continue
        cols = ix.get('columns')
        if (not isinstance(cols, list) or not cols
                or any(not isinstance(c, str) for c in cols)):
            sys.exit(f'Error: {path} `{iw}.columns` must be a non-empty list of strings')
        _check_bool(ix.get('unique'), 'unique' in ix, path, f'{iw}.unique')
        # A non-drop (add/override) index is name-OPTIONAL (Sol relaxation
        # #1 / full-fidelity --emit-config round trip: a DB-sourced unnamed
        # index has no name to re-emit, and merge.py already supports
        # unnamed-index identity by column tuple). Identity for THIS
        # config-internal duplicate check is the given name when present,
        # else the column tuple ALONE — matching merge.py's own unnamed-index
        # key (`tuple(columns)`, deliberately NOT including `unique`, §7.4).
        # Keying on (columns, unique) here would let a unique + non-unique
        # unnamed pair on the same columns pass load only to be silently
        # collapsed into one at merge (conflicting-value warning, arbitrary
        # winner) — so we reject that pair up front instead.
        identity = name if name else tuple(cols)
        if identity in seen:
            sys.exit(f'Error: {path} `{where}.indexes` has a duplicate index '
                     f'{"name" if name else "columns"} {identity!r}')
        seen.add(identity)

def _config_assoc_identity(a):
    """Stable identity for an association fragment/drop, used only for
    Config-internal duplicate detection. Mirrors the runtime association_key
    (§8.1) so the two never disagree about what counts as "the same" edge:
    role (owner_fk / collection / inverse_one / named) + target + FK column +
    name, PLUS `through` and `polymorphic` when present. Including the last two
    is what lets two associations that share type/name/target but differ only in
    `through` (e.g. `through: orders` vs `through: archived_orders`) coexist —
    the runtime treats them as distinct, so the syntactic check must too. `name`
    is part of the identity for every role, so the Rails alias pattern (`user`
    AND `author`, both on `user_id` -> `users`) is not a duplicate; an exact
    duplicate is still caught. Uses .get() throughout so a DropOperation (which
    may omit name/type) is handled by the same rule."""
    role = ('owner_fk' if a.get('foreign_key')
            else 'collection' if a.get('type') in ('has_many', 'has_and_belongs_to_many')
            else 'inverse_one' if a.get('type') == 'has_one'
            else 'named')
    fk = frozenset([a['foreign_key']]) if a.get('foreign_key') else frozenset()
    ident = [role, a.get('target'), fk, a.get('name')]
    if a.get('through'):
        ident.append(('through', a['through']))
    if a.get('polymorphic'):
        ident.append(('polymorphic', True))
    return tuple(ident)

def _check_config_associations(assocs, path, where):
    if not isinstance(assocs, list):
        sys.exit(f'Error: {path} `{where}.associations` must be a list')
    seen = set()
    for i, a in enumerate(assocs):
        aw = f'{where}.associations[{i}]'
        if not isinstance(a, dict):
            sys.exit(f'Error: {path} `{aw}` must be an object')
        _reject_unknown_keys(a, _CONFIG_ASSOC_KEYS, path, aw)
        _check_bool(a.get('drop'), 'drop' in a, path, f'{aw}.drop')
        # foreign_key is single-column only this release (§4.2/§18): a list is
        # a composite FK — reject it explicitly rather than silently mishandle
        if 'foreign_key' in a and a['foreign_key'] is not None:
            fk = a['foreign_key']
            if isinstance(fk, list):
                sys.exit(f'Error: {path} `{aw}.foreign_key` is a list — composite foreign '
                         'keys are not supported in this release; use a single column name')
            if not isinstance(fk, str) or not fk:
                sys.exit(f'Error: {path} `{aw}.foreign_key` must be a single column name (string)')
        if a.get('type') is not None and a.get('type') not in _CONFIG_ASSOC_TYPES:
            sys.exit(f'Error: {path} `{aw}.type` must be one of '
                     f'{", ".join(sorted(_CONFIG_ASSOC_TYPES))}, got {a.get("type")!r}')
        for k in ('name', 'target', 'through'):
            if k in a and a[k] is not None and not isinstance(a[k], str):
                sys.exit(f'Error: {path} `{aw}.{k}` must be a string')
        _check_bool(a.get('polymorphic'), 'polymorphic' in a, path, f'{aw}.polymorphic')

        if a.get('drop') is True:
            # AssociationDrop identity is role-dependent (§4.3): the FK-holding
            # side needs target+foreign_key; a collection/inverse needs
            # type+target+name. `name` is NOT unconditionally required here (it
            # is for a fragment) — that's the Fragment-vs-Drop required-field split.
            if a.get('foreign_key'):
                if not a.get('target'):
                    sys.exit(f'Error: {path} `{aw}` is an FK-holding association drop but is '
                             'missing `target` — need `target`+`foreign_key` to identify it')
            else:
                missing = [k for k in ('type', 'target', 'name') if not a.get(k)]
                if missing:
                    sys.exit(f'Error: {path} `{aw}` cannot identify the association to drop: '
                             'give `target`+`foreign_key` (FK-holding side) or '
                             '`type`+`target`+`name` (collection/inverse); '
                             f'missing {", ".join(missing)}')
        else:
            # AssociationFragment (add/override): type, name, target all required
            missing = [k for k in ('type', 'name', 'target') if not a.get(k)]
            if missing:
                sys.exit(f'Error: {path} `{aw}` is missing required field(s): '
                         f'{", ".join(missing)} (an association needs type, name, target)')
        identity = _config_assoc_identity(a)
        if identity in seen:
            sys.exit(f'Error: {path} `{where}.associations` has a duplicate association '
                     f'(same identity {identity!r})')
        seen.add(identity)

_SAFE_HOST_OR_USER = re.compile(r'[\w.\-]+')

def assemble_config_url(config):
    """Build a mysql://, postgres://, or sqlite:// URL (per the config's
    `engine`, default mysql) from the config's connection fields, or None if
    `database` wasn't given (no connection info in the config at all).

    For mysql/postgres, each of host/port/user/database is validated against
    a safe charset before being pasted into the URL string — host/user
    containing `/`, `@`, or `:` would silently shift what urlparse reads as
    the host/port/path when the assembled string is re-parsed downstream
    (verified empirically: a host of "x@evil" produces a URL whose username
    becomes "x" and whose actual host becomes "evil"), and there is no
    decoding step anywhere downstream to undo percent-encoding, so quoting
    isn't a fix either.

    For sqlite, `database` is a local file path, not a database name —
    host/port/user don't apply (rejected outright if present) and `database`
    is pasted as-is after `sqlite:///`, which round-trips exactly with
    sqlite_path_from_url()'s "strip one leading slash" rule: a relative path
    `rel.db` becomes `sqlite:///rel.db`, an absolute path `/abs/app.db`
    becomes `sqlite:////abs/app.db` (four slashes)."""
    engine = config.get('engine', 'mysql')
    if engine not in ('mysql', 'postgres', 'postgresql', 'sqlite'):
        sys.exit(f'Error: config `engine` must be "mysql", "postgres", or "sqlite", '
                 f'got {engine!r}')
    if engine == 'sqlite':
        for k in ('host', 'port', 'user'):
            if k in config:
                sys.exit(f'Error: config {k!r} does not apply when engine is "sqlite" '
                         f'(it reads a local file)')
        db = config.get('database')
        if db is None:  # absent, or explicitly blank (e.g. a bare `database:` in YAML)
            return None
        db = str(db)
        if not db:
            sys.exit('Error: config `database` must not be blank when engine is "sqlite"')
        if '?' in db or '#' in db:
            sys.exit(f'Error: config `database` {db!r} must not contain "?" or "#" — '
                     f'those would be misread as a URL query/fragment when the sqlite:// '
                     f'URL is assembled')
        if any(ord(ch) < 0x20 or ch == '\x7f' for ch in db):
            sys.exit(f'Error: config `database` {db!r} contains control characters, '
                     f'which are not valid in a file path')
        return 'sqlite:///' + db
    db = config.get('database')
    if db is None:  # absent, or explicitly blank (e.g. a bare `database:` in YAML)
        return None
    if not re.fullmatch(r'\w+', str(db)):
        sys.exit(f'Error: config `database` {db!r} is not a valid database name')
    host = config.get('host') or '127.0.0.1'
    if not _SAFE_HOST_OR_USER.fullmatch(str(host)):
        sys.exit(f'Error: config `host` {host!r} has unsupported characters (letters/'
                 f'digits/./- only here — IPv6 and other exotic hosts need the CLI '
                 f'argument instead, not the config file)')
    port = config.get('port', 3306 if engine == 'mysql' else 5432)
    if isinstance(port, bool) or (isinstance(port, float) and not port.is_integer()):
        sys.exit(f'Error: config `port` {port!r} is not a valid port number')
    try:
        port = int(port)
        if not (1 <= port <= 65535):
            raise ValueError
    except (TypeError, ValueError):
        sys.exit(f'Error: config `port` {port!r} is not a valid port number')
    auth = ''
    if config.get('user') is not None:
        user = str(config['user'])
        if ':' in user:
            sys.exit('Error: config `user` must not contain a password (no "user:pass" '
                     'syntax) — passwords are not supported in the config file')
        if not _SAFE_HOST_OR_USER.fullmatch(user):
            sys.exit(f'Error: config `user` {user!r} has unsupported characters '
                     f'(letters/digits/./- only)')
        auth = f'{user}@'
    scheme = 'mysql' if engine == 'mysql' else 'postgres'
    return f'{scheme}://{auth}{host}:{port}/{db}'

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _framework_project_name(mroot):
    """A meaningful project name for the first normalized InputSpec's path (§10
    title fallback / D6.3). Walk from a Rails app/models dir up to the project
    root, from a prisma/schema.prisma up to the project, from a Rails
    db/schema.rb up to ITS project root (file -> parent `db` -> its parent),
    and from any other schema file up to its directory, then use the
    basename."""
    p = mroot
    if p.is_file():                                    # e.g. .../schema.prisma
        if p.name == 'schema.rb' and p.parent.name == 'db':  # .../<proj>/db/schema.rb
            p = p.parent.parent
        else:
            p = p.parent
    if p.name == 'models' and p.parent.name == 'app':  # Rails app/models
        p = p.parent.parent
    elif p.name == 'prisma':                           # .../<proj>/prisma
        p = p.parent
    return p.name or 'schema'

def _resolve_title(config, url, fw_root, output):
    """Workbook/HTML title precedence (§10): config.title > DB name >
    framework project name > output filename stem > "schema"."""
    if config.get('title'):
        return config['title']
    if url:
        u = urlparse(url)
        if u.scheme == 'sqlite':
            stem = Path(u.path).stem   # sqlite:///path/to/shop.db -> "shop"
            if stem:
                return stem
        else:
            db = u.path.lstrip('/')
            if db:
                return db
    if fw_root is not None:
        return _framework_project_name(fw_root)
    if output:
        stem = Path(output).stem
        if stem:
            return stem
    return 'schema'

