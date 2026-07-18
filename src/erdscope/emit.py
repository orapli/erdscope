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
