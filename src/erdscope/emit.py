# ---------------------------------------------------------------------------
# --emit-json — canonical JSON snapshot + content fingerprint (backlog #0)
#
# A separate, machine-readable projection of the final merged IR (post --only/
# --exclude, post notes/groups resolution), independent of the HTML/Excel
# outputs: a fixed allowlist of table keys, deterministic ordering everywhere
# order isn't already meaningful, and provenance normalized to the 5-value set
# instead of the legacy db_fk/inferred/manual/schema_fk booleans the HTML/
# Excel path still uses (serialize_for_viewer, cli.py). Pure and non-
# destructive throughout: every function here deep-copies its input before
# touching it, and never sorts in place.
# ---------------------------------------------------------------------------

# The only 5 provenance values a canonical association may carry (§9.1 in
# ir.py's docstring). Anything else is a bug upstream (merge_ir, or a plugin
# writing a bogus `provenance`) and must fail loudly here rather than emit a
# snapshot silently claiming a fabricated value.
_VALID_PROVENANCE = {'declared', 'manual', 'db_fk', 'schema_fk', 'inferred'}

# Optional column keys, omitted from the canonical column when falsy (empty
# string, None, or False) — mirrors the IR docstring's ColumnIR optionals
# minus `primary` (which has its own True-only rule below).
_OPTIONAL_COLUMN_KEYS = ('sql_type', 'default', 'extra', 'comment')


def _canonical_column(c):
    """One column -> its canonical projection. Always carries name/type/
    nullable; the rest are included only when truthy. `primary` is included
    only when True (never `"primary": false`)."""
    out = {'name': c['name'], 'type': c.get('type', ''),
           'nullable': bool(c.get('nullable', False))}
    for key in _OPTIONAL_COLUMN_KEYS:
        val = c.get(key)
        if val:
            out[key] = val
    if c.get('primary'):
        out['primary'] = True
    return out


def _canonical_indexes(indexes):
    """Indexes, each projected to name?/columns/unique, sorted by
    `(name or "", tuple(columns), unique)` for a deterministic snapshot
    regardless of the IR's original (first-seen-across-layers) order."""
    out = []
    for ix in indexes or []:
        item = {}
        if ix.get('name'):
            item['name'] = ix['name']
        item['columns'] = list(ix.get('columns', []))
        item['unique'] = bool(ix.get('unique', False))
        out.append(item)
    out.sort(key=lambda item: (item.get('name') or '', tuple(item['columns']), item['unique']))
    return out


def _association_provenance(a):
    """The association's provenance, from either IR shape (mirrors
    _assoc_provenance in ir.py, but validated against the 5-value set — a
    canonical snapshot never emits a value outside it)."""
    prov = a['provenance'] if 'provenance' in a else provenance_of(a)
    if prov not in _VALID_PROVENANCE:
        raise ValueError(f'unknown association provenance {prov!r}')
    return prov


def _association_sources(a):
    """Deduplicated, ascending-(kind, provider) `sources` for a merged-IR
    association; omitted entirely (returns None) for a legacy-shape
    association, which never carries `sources`."""
    if 'provenance' not in a:
        return None
    seen, out = set(), []
    for s in a.get('sources') or []:
        key = (s['kind'], s['provider'])
        if key not in seen:
            seen.add(key)
            out.append({'kind': s['kind'], 'provider': s['provider']})
    out.sort(key=lambda s: (s['kind'], s['provider']))
    return out


def _assoc_sort_key(item):
    """Deterministic association ordering: `(type, name or "", foreign_key or
    "", target or "", through or "", polymorphic-bit, provenance)`, with the
    canonical association's own JSON string as the final tie-breaker.
    `polymorphic` is normalized to '' / '1' (rather than left as False/True)
    so the key never mixes bool and str at the same tuple position across
    associations — Python's tuple comparison would raise TypeError comparing
    `True` to `''` the moment two associations tie on every earlier field."""
    poly_bit = '1' if item.get('polymorphic') else ''
    tie = json.dumps(item, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return (item['type'], item.get('name') or '', item.get('foreign_key') or '',
            item.get('target') or '', item.get('through') or '', poly_bit,
            item['provenance'], tie)


def _canonical_associations(associations, survivors):
    """Associations -> canonical projection: allowlisted keys only
    (type/target/name?/foreign_key?/through?/polymorphic?/provenance/
    sources?), legacy boolean flags (db_fk/inferred/manual/schema_fk) always
    dropped, dangling associations (target not in `survivors`) pruned, then
    deterministically sorted."""
    out = []
    for a in associations or []:
        target = a.get('target')
        # Prune only a *resolvable* association whose target isn't in the set
        # (e.g. --only/--exclude dropped the target table). A polymorphic
        # belongs_to carries a synthetic, tableless target (django's
        # `pluralize(to_snake(name))`, rails' association name) that is never a
        # real table — it must be KEPT, exactly as the HTML/Excel path keeps it
        # (details-pane, no edge). Pruning it on `target not in survivors` would
        # silently drop every polymorphic relation from the snapshot.
        if not a.get('polymorphic') and target not in survivors:
            continue  # dangling: --only/--exclude (or a stale fixture) left the target out
        item = {'type': a['type'], 'target': target}
        if a.get('name'):
            item['name'] = a['name']
        if a.get('foreign_key'):
            item['foreign_key'] = a['foreign_key']
        if a.get('through'):
            item['through'] = a['through']
        if a.get('polymorphic'):
            item['polymorphic'] = True
        item['provenance'] = _association_provenance(a)
        sources = _association_sources(a)
        if sources:
            item['sources'] = sources
        out.append(item)
    out.sort(key=_assoc_sort_key)
    return out


def _canonical_table(t, survivors):
    """One merged-IR table -> its canonical projection: ONLY comment?/
    columns/indexes/associations survive (fk_columns, schema_missing, and any
    other internal/plugin key are never emitted). `comment` is omitted when
    empty/None."""
    out = {}
    comment = t.get('comment')
    if comment:
        out['comment'] = comment
    out['columns'] = [_canonical_column(c) for c in t.get('columns', [])]
    out['indexes'] = _canonical_indexes(t.get('indexes'))
    out['associations'] = _canonical_associations(t.get('associations'), survivors)
    return out


def _canonical_notes(notes_data):
    """Notes, each passed through unchanged (already the viewer-resolved
    shape, `links` order already meaningful and preserved), sorted by `id`
    ascending."""
    return sorted(copy.deepcopy(notes_data), key=lambda n: n['id'])


def _canonical_groups(groups_data):
    """Groups, each with `tables` re-sorted by name ascending, the whole list
    sorted by `id` ascending."""
    out = []
    for g in groups_data:
        g = copy.deepcopy(g)
        g['tables'] = sorted(g['tables'])
        out.append(g)
    out.sort(key=lambda g: g['id'])
    return out


def canonical_schema(tables, notes_data, groups_data):
    """Project the final merged IR (+ resolved notes/groups) to the
    canonical, allowlisted, deterministically-ordered shape backing
    --emit-json. Pure: deep-copies its inputs, never mutates them, and never
    sorts in place — every list above is built fresh. `notes`/`groups` are
    omitted from the result entirely when empty/None (mirrors the DATA_JSON
    `notes`/`groups` key-omission rule in cli.py's _finish)."""
    tables = copy.deepcopy(tables)
    survivors = set(tables)
    out_tables = {name: _canonical_table(t, survivors) for name, t in tables.items()}
    schema = {'tables': out_tables}
    if notes_data:
        schema['notes'] = _canonical_notes(notes_data)
    if groups_data:
        schema['groups'] = _canonical_groups(groups_data)
    return schema


def snapshot_fingerprint(schema):
    """sha256 content fingerprint of a canonical `schema` dict, prefixed
    `sha256:`. Hashes the UTF-8 bytes of
    `json.dumps({"format": 1, "schema": schema}, sort_keys=True,
    ensure_ascii=False, separators=(",", ":"), allow_nan=False)` — sorted keys
    and a compact separator make the digest depend only on content, never on
    dict insertion order or incidental whitespace; `allow_nan=False` rejects a
    NaN/Infinity that snuck into a comment or default and would otherwise
    serialize as non-portable, non-standard JSON. `format` is folded into the
    hashed payload so a future format bump is a hard fingerprint break, never
    a silent collision with format 1."""
    payload = json.dumps({'format': 1, 'schema': schema}, sort_keys=True,
                         ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    digest = hashlib.sha256(payload.encode('utf-8')).hexdigest()
    return f'sha256:{digest}'


def emit_json_document(tables, notes_data, groups_data):
    """Build the full --emit-json document (as a string, trailing newline
    included): `{"format": 1, "fingerprint": "sha256:...", "schema": {...}}`,
    pretty-printed (`indent=2, sort_keys=True`) for human readability. No
    `</` escaping (unlike the HTML DATA_JSON payload) — this file is never
    embedded in a `<script>` tag. No version field by design: the snapshot's
    own fingerprint is the stable identity, not a tool version string."""
    schema = canonical_schema(tables, notes_data, groups_data)
    document = {'format': 1, 'fingerprint': snapshot_fingerprint(schema), 'schema': schema}
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True,
                      allow_nan=False) + '\n'


# ---------------------------------------------------------------------------
# --emit-config — config-authoring (YAML/JSON) projection of the final merged
# IR (backlog #1). Reuses every canonical_schema helper above for identical
# dangling-pruning, deterministic sort, and falsy-omission rules — the two
# emitters diverge only in OUTPUT SHAPE, not in which data survives:
#   - columns/indexes: byte-identical projection to --emit-json's own
#     (_canonical_column / _canonical_indexes, reused verbatim).
#   - associations: same pruning + sort as --emit-json's
#     (_canonical_associations, reused), minus provenance/sources — a
#     config-only reimport can never carry either (merge_ir always assigns
#     config-kind associations 'manual' provenance; the config association
#     input format has no field for provenance/sources at all).
#   - composite primary keys (P1/E-2): re-derived here from column.primary
#     IN COLUMN ORDER, never from the IR's own `primary_key` field — a
#     DB-sourced composite PK's `primary_key` names only its FIRST column
#     (merge.py:312's _normalize_primary_key docstring), so reading it here
#     would silently truncate the PK on reimport. A single-column PK stays a
#     plain column `primary: true`; only 2+ primary columns promote to a
#     table-level `primary_key: [...]`.
#
# This is intentionally NOT a full round trip: provenance, `sources`, the
# legacy db_fk/inferred/manual/schema_fk flags, and the config drop/*_mode
# OPERATIONS are all gone after one pass through the merged IR — a
# config-only reimport of this file reaches "level1" (materially the same
# schema — same tables/columns/types/nullability/defaults, same primary-key
# COLUMN SETS, same indexes as (columns, unique) sets, same associations as
# (type, target, foreign_key, through, polymorphic) tuples, same
# comments/notes/groups), not a byte-identical config source.
# ---------------------------------------------------------------------------

def _config_associations(associations, survivors):
    """Config-authoring projection of one table's associations: identical
    dangling-pruning and deterministic sort to _canonical_associations
    (reused directly), but WITHOUT provenance/sources — the config
    association shape (type/target/name?/foreign_key?/through?/
    polymorphic?) has no field for either, and a config-only reimport
    resolves every association to 'manual' provenance regardless of what it
    originally was."""
    out = []
    for item in _canonical_associations(associations, survivors):
        entry = {'type': item['type'], 'target': item['target']}
        for key in ('name', 'foreign_key', 'through', 'polymorphic'):
            if key in item:
                entry[key] = item[key]
        out.append(entry)
    return out


def _config_table(t, survivors):
    """One merged-IR table -> its config-authoring TableFragment (the shape
    config.py's _CONFIG_TABLE_KEYS allow-list accepts on load): comment
    (falsy-omitted, same rule as _canonical_table), primary_key (ONLY for a
    genuine composite PK — 2+ columns flagged primary; a single-column PK
    stays a plain column `primary: true`, per P1/E-2), columns/indexes
    (byte-identical to --emit-json's own projection), associations (see
    _config_associations above)."""
    out = {}
    comment = t.get('comment')
    if comment:
        out['comment'] = comment
    columns = t.get('columns', [])
    # Composite-PK detection is column-primary-FLAG based, NOT primary_key-
    # FIELD based (P1/E-2) — see the module-level comment above.
    primary_cols = [c['name'] for c in columns if c.get('primary')]
    if len(primary_cols) >= 2:
        out['primary_key'] = primary_cols
    out['columns'] = [_canonical_column(c) for c in columns]
    out['indexes'] = _canonical_indexes(t.get('indexes'))
    out['associations'] = _config_associations(t.get('associations'), survivors)
    return out


def _config_note(n):
    """One resolved (viewer-shape) note -> its config `notes:` INPUT
    projection — the inverse of providers.resolve_and_validate_notes.
    global/table targets pass through almost as-is. A relation target
    re-derives the FULL narrowing key (name/foreign_key/through/assoc_type/
    polymorphic ALL present, explicit `null` wherever the resolved
    association carries no value for that field — never simply omitted) so
    that reimporting resolves back to the exact one association this note
    came from: resolve_and_validate_notes's null-vs-absent narrowing
    (providers.py, Sol relaxation #2) treats an explicit `null` here as "the
    field must be absent on the match" and an OMITTED key as "don't care" —
    only the former is unambiguous enough for a lossless reimport."""
    scope = n['scope']
    if scope == 'global':
        target = {'type': 'global'}
    elif scope == 'table':
        target = {'type': 'table', 'table': n['table']}
    else:  # relation
        target = {
            'type': 'relation',
            'source_table': n['source_table'],
            'target_table': n['target'],
            'name': n['name'],
            'foreign_key': n.get('foreign_key'),
            'through': n.get('through'),
            'assoc_type': n['type'],
            'polymorphic': bool(n.get('polymorphic')),
        }
    entry = {'id': n['id'], 'target': target}
    if n.get('title'):
        entry['title'] = n['title']
    entry['text'] = n['text']
    if n.get('links'):
        entry['links'] = n['links']
    return entry


def config_document(tables, notes_data, groups_data, title=None):
    """Build the --emit-config document: the final merged IR (+ resolved
    notes/groups), projected to the CONFIG AUTHORING shape (config.py's
    accepted top-level keys — version/title?/tables/notes?/groups?) instead
    of --emit-json's read-only snapshot shape. Pure: deep-copies its inputs,
    never mutates them. `title` is omitted when falsy; `notes`/`groups` are
    omitted entirely when empty/None, mirroring canonical_schema."""
    tables = copy.deepcopy(tables)
    survivors = set(tables)
    out_tables = {name: _config_table(t, survivors) for name, t in tables.items()}
    doc = {'version': 1, 'tables': out_tables}
    if title:
        doc['title'] = title
    if notes_data:
        doc['notes'] = [_config_note(n) for n in _canonical_notes(notes_data)]
    if groups_data:
        doc['groups'] = _canonical_groups(groups_data)
    return doc


def config_json_text(document):
    """--emit-config FILE.json (and `-` stdout, JSON by default) text: same
    conventions as emit_json_document — indent=2, sort_keys=True,
    ensure_ascii=False, trailing newline — for a diff-friendly file whose
    bytes depend only on content, never dict insertion order."""
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True,
                      allow_nan=False) + '\n'


def config_yaml_text(document):
    """--emit-config FILE.yml/.yaml text (Sol relaxation #6). Deterministic
    (`default_flow_style=False`, `allow_unicode=True`, `sort_keys=True` — the
    same "stable regardless of dict insertion order" guarantee as the JSON
    path's `sort_keys=True`) YAML dump, with a custom string representer
    that:
      - forces literal block style (`|`) for any string containing a
        newline, so long note/comment text stays human-readable instead of
        PyYAML's default folded-quote style (one value, blank-line-joined);
      - explicitly quotes any OTHER plain scalar that YAML's own
        implicit-typing resolver would resolve to a non-string tag — the
        "Norway problem" (bare no/yes/on/off/true/false -> bool), a
        leading-zero digit string misread as octal, or any bare
        numeric-looking string misread as int/float/timestamp — so a config
        string value is guaranteed, not just incidentally, to round-trip as
        the SAME string the next time `--config` loads this file (belt and
        suspenders: PyYAML's own default emitter already avoids most of
        these by checking the identical resolver before choosing plain
        style, but doing it here explicitly makes the guarantee independent
        of that emitter-internal behavior).
    Requires PyYAML to be importable — the caller (cli.py) checks that up
    front and exits with a clear, no-fallback error before ever reaching
    here, per Sol relaxation #6 ("no JSON fallback" for a requested .yml/
    .yaml path)."""
    import yaml

    class _ConfigDumper(yaml.SafeDumper):
        pass

    resolver = yaml.resolver.Resolver()

    def _represent_str(dumper, value):
        if '\n' in value:
            return dumper.represent_scalar('tag:yaml.org,2002:str', value, style='|')
        tag = resolver.resolve(yaml.ScalarNode, value, (True, False))
        style = None if tag == 'tag:yaml.org,2002:str' else "'"
        return dumper.represent_scalar('tag:yaml.org,2002:str', value, style=style)

    _ConfigDumper.add_representer(str, _represent_str)
    return yaml.dump(document, Dumper=_ConfigDumper, default_flow_style=False,
                     allow_unicode=True, sort_keys=True, width=1 << 20)
