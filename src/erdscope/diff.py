# ---------------------------------------------------------------------------
# schema diff / drift (backlog #2) — level1 semantic diff between two
# canonical schemas (emit.py's canonical_schema shape:
# {tables: {...}, notes?: [...], groups?: [...]}), driving the --diff CI
# drift gate in cli.py.
#
# Direction (fixed, documented once here — every helper below honors it):
# `left` is the CURRENT run's canonical_schema; `right` is the BASE snapshot
# being compared against (an --emit-json document's `schema`). At every
# level (tables, columns, indexes, associations, notes, groups):
#   added   = present in `left` (current) only  — new since the snapshot
#   removed = present in `right` (base) only     — gone since the snapshot
# This never inverts at a sub-level: a table's added columns are still
# "present in the current run's version of that table only", etc.
#
# level1 identity (materially-the-same-schema, not byte-identical — the same
# notion --emit-config's docstring in emit.py already defines):
#   - tables/notes/groups: matched by name/id; a matched pair with any field
#     difference is "changed" (differing fields enumerated with old/new).
#   - columns: matched by name; changed = any of type/sql_type/nullable/
#     primary/default/extra/comment differs (differing fields enumerated).
#   - indexes: matched by (tuple(columns), unique) — name is NOT part of
#     identity, so a bare rename is invisible at level1; indexes are pure
#     added/removed, never "changed".
#   - associations: matched by (type, target, name, foreign_key, through,
#     polymorphic) — provenance/sources excluded by default (they describe
#     WHERE an association came from, not what it means at level1); pass
#     include_provenance=True to fold them into identity too. A "changed"
#     association (e.g. retargeted foreign_key) has no separate
#     representation: it is the OLD identity removed + the NEW identity
#     added — exactly emit.py's association-diff wording.
#
# Known level1 limitations (documented, by design — the same "level1 material
# meaning, not byte identity" line --emit-config draws):
#   - an empty-string column default and "no default at all" look identical in
#     every canonical schema (no provider distinguishes the two on read), so
#     this diff cannot detect that specific change.
#   - a pure column REORDER is invisible: columns are matched by name, so the
#     same columns in a different order compare equal. (canonical_schema keeps
#     column order and the fingerprint is order-sensitive, so a reorder still
#     fails the fingerprint fast path and falls through to this order-agnostic
#     deep compare — landing on "no difference". fingerprint = byte identity;
#     diff = level1 meaning.)
# ---------------------------------------------------------------------------

_COLUMN_FIELDS = ('type', 'sql_type', 'nullable', 'primary', 'default', 'extra', 'comment')
_COLUMN_FIELD_FALSY_DEFAULTS = {'nullable': False, 'primary': False}


def _column_field(col, field):
    """A canonical column's value for `field`, defaulting exactly the way
    emit.py's _canonical_column omits it: '' for the falsy-omitted string
    fields (sql_type/default/extra/comment; `type` is always present so this
    default never actually applies to it), False for nullable/primary."""
    return col.get(field, _COLUMN_FIELD_FALSY_DEFAULTS.get(field, ''))


def _diff_columns(left_cols, right_cols):
    left_by_name = {c['name']: c for c in left_cols}
    right_by_name = {c['name']: c for c in right_cols}
    added = sorted(set(left_by_name) - set(right_by_name))
    removed = sorted(set(right_by_name) - set(left_by_name))
    changed = {}
    for name in sorted(set(left_by_name) & set(right_by_name)):
        l, r = left_by_name[name], right_by_name[name]
        fields = {}
        for f in _COLUMN_FIELDS:
            lv, rv = _column_field(l, f), _column_field(r, f)
            if lv != rv:
                fields[f] = {'old': rv, 'new': lv}
        if fields:
            changed[name] = {'fields': fields}
    return {'added': added, 'removed': removed, 'changed': changed}


def _index_identity(ix):
    return (tuple(ix.get('columns', [])), bool(ix.get('unique', False)))


def _index_repr(ix):
    return {'columns': list(ix.get('columns', [])), 'unique': bool(ix.get('unique', False))}


def _diff_indexes(left_idx, right_idx):
    # First-seen-wins on a duplicate identity (shouldn't happen for a
    # genuinely canonical schema, but a hand-built/adversarial right-hand
    # snapshot must not crash the diff over it).
    left_by_id, right_by_id = {}, {}
    for ix in left_idx:
        left_by_id.setdefault(_index_identity(ix), ix)
    for ix in right_idx:
        right_by_id.setdefault(_index_identity(ix), ix)
    added_ids = sorted(set(left_by_id) - set(right_by_id))
    removed_ids = sorted(set(right_by_id) - set(left_by_id))
    added = [_index_repr(left_by_id[i]) for i in added_ids]
    removed = [_index_repr(right_by_id[i]) for i in removed_ids]
    return {'added': added, 'removed': removed}


def _assoc_identity(a, include_provenance):
    identity = (a['type'], a.get('target') or '', a.get('name') or '',
                a.get('foreign_key') or '', a.get('through') or '',
                bool(a.get('polymorphic')))
    if include_provenance:
        sources = tuple(sorted((s['kind'], s['provider']) for s in a.get('sources') or []))
        identity = identity + (a.get('provenance') or '', sources)
    return identity


def _assoc_repr(a, include_provenance):
    out = {'type': a['type'], 'target': a.get('target')}
    for key in ('name', 'foreign_key', 'through'):
        if a.get(key):
            out[key] = a[key]
    if a.get('polymorphic'):
        out['polymorphic'] = True
    if include_provenance:
        if a.get('provenance'):
            out['provenance'] = a['provenance']
        if a.get('sources'):
            out['sources'] = a['sources']
    return out


def _diff_associations(left_assoc, right_assoc, include_provenance):
    left_by_id, right_by_id = {}, {}
    for a in left_assoc:
        left_by_id.setdefault(_assoc_identity(a, include_provenance), a)
    for a in right_assoc:
        right_by_id.setdefault(_assoc_identity(a, include_provenance), a)
    added_ids = sorted(set(left_by_id) - set(right_by_id))
    removed_ids = sorted(set(right_by_id) - set(left_by_id))
    added = [_assoc_repr(left_by_id[i], include_provenance) for i in added_ids]
    removed = [_assoc_repr(right_by_id[i], include_provenance) for i in removed_ids]
    return {'added': added, 'removed': removed}


def _table_comment(t):
    return t.get('comment') or ''


def _diff_table(left_t, right_t, include_provenance):
    """One matched (same-name) table pair -> its diff entry, or {} when the
    two are level1-identical (comment + columns + indexes + associations all
    equal) — the caller uses truthiness to decide whether the table belongs
    in the `changed` map at all."""
    out = {}
    lc, rc = _table_comment(left_t), _table_comment(right_t)
    if lc != rc:
        out['comment'] = {'old': rc, 'new': lc}
    columns = _diff_columns(left_t.get('columns', []), right_t.get('columns', []))
    if columns['added'] or columns['removed'] or columns['changed']:
        out['columns'] = columns
    indexes = _diff_indexes(left_t.get('indexes', []), right_t.get('indexes', []))
    if indexes['added'] or indexes['removed']:
        out['indexes'] = indexes
    associations = _diff_associations(left_t.get('associations', []),
                                      right_t.get('associations', []), include_provenance)
    if associations['added'] or associations['removed']:
        out['associations'] = associations
    return out


def _diff_tables(left_tables, right_tables, include_provenance):
    added = sorted(set(left_tables) - set(right_tables))
    removed = sorted(set(right_tables) - set(left_tables))
    changed = {}
    for name in sorted(set(left_tables) & set(right_tables)):
        d = _diff_table(left_tables[name], right_tables[name], include_provenance)
        if d:
            changed[name] = d
    return {'added': added, 'removed': removed, 'changed': changed}


# A note's full identity envelope beyond id/text/title/links — which of these
# keys are actually present on a given note depends on its `scope` (global/
# table/relation; see providers.resolve_and_validate_notes's shared-contract
# entry shapes), so comparing the union of possibly-present keys covers every
# scope combination (including a note changing scope entirely) without
# special-casing any one of them.
_NOTE_FIELDS = ('scope', 'table', 'source_table', 'target', 'type', 'name',
                'foreign_key', 'through', 'polymorphic', 'title', 'text', 'links')


def _diff_note(l, r):
    fields = {}
    for f in _NOTE_FIELDS:
        lv, rv = l.get(f), r.get(f)
        if lv != rv:
            fields[f] = {'old': rv, 'new': lv}
    return {'fields': fields} if fields else {}


def _diff_notes(left_notes, right_notes):
    left_by_id = {n['id']: n for n in left_notes or []}
    right_by_id = {n['id']: n for n in right_notes or []}
    added = sorted(set(left_by_id) - set(right_by_id))
    removed = sorted(set(right_by_id) - set(left_by_id))
    changed = {}
    for nid in sorted(set(left_by_id) & set(right_by_id)):
        d = _diff_note(left_by_id[nid], right_by_id[nid])
        if d:
            changed[nid] = d
    return {'added': added, 'removed': removed, 'changed': changed}


def _diff_group(l, r):
    out = {}
    lt, rt = set(l.get('tables', [])), set(r.get('tables', []))
    if lt != rt:
        out['tables'] = {'added': sorted(lt - rt), 'removed': sorted(rt - lt)}
    lti, rti = l.get('title') or '', r.get('title') or ''
    if lti != rti:
        out['title'] = {'old': rti, 'new': lti}
    lco, rco = l.get('color') or '', r.get('color') or ''
    if lco != rco:
        out['color'] = {'old': rco, 'new': lco}
    return out


def _diff_groups(left_groups, right_groups):
    left_by_id = {g['id']: g for g in left_groups or []}
    right_by_id = {g['id']: g for g in right_groups or []}
    added = sorted(set(left_by_id) - set(right_by_id))
    removed = sorted(set(right_by_id) - set(left_by_id))
    changed = {}
    for gid in sorted(set(left_by_id) & set(right_by_id)):
        d = _diff_group(left_by_id[gid], right_by_id[gid])
        if d:
            changed[gid] = d
    return {'added': added, 'removed': removed, 'changed': changed}


def schema_diff(left_schema, right_schema, *, include_provenance=False):
    """The level1 diff between two canonical schemas (emit.py's
    canonical_schema shape). Pure: only reads `left_schema`/`right_schema`,
    never mutates either. See the module docstring above for the fixed
    left=current/right=base direction and the per-level identity rules.
    Returns a deterministic dict — every level is {added, removed, changed},
    each already sorted/keyed for a stable render_json()."""
    left_tables = left_schema.get('tables', {})
    right_tables = right_schema.get('tables', {})
    return {
        'tables': _diff_tables(left_tables, right_tables, include_provenance),
        'notes': _diff_notes(left_schema.get('notes'), right_schema.get('notes')),
        'groups': _diff_groups(left_schema.get('groups'), right_schema.get('groups')),
    }


def empty_schema_diff():
    """The all-empty diff shape schema_diff() would return for two
    level1-identical schemas — used by cli.py's fingerprint fast path so a
    fingerprint match can skip the deep compare entirely while still handing
    render_text/render_json/diff_is_empty the exact shape they expect."""
    def _section():
        return {'added': [], 'removed': [], 'changed': {}}
    return {'tables': _section(), 'notes': _section(), 'groups': _section()}


def diff_is_empty(diff):
    """True when `diff` (schema_diff()'s or empty_schema_diff()'s return
    value) carries no difference at all in any of tables/notes/groups."""
    return all(not diff[section]['added'] and not diff[section]['removed']
              and not diff[section]['changed'] for section in ('tables', 'notes', 'groups'))


def render_json(diff) -> str:
    """--diff-format json rendering: same conventions as emit.py's own JSON
    writers — indent=2, sort_keys=True, ensure_ascii=False, trailing
    newline — so the text is diff-friendly and depends only on content."""
    return json.dumps(diff, indent=2, sort_keys=True, ensure_ascii=False) + '\n'


def _fmt_change(old, new):
    return f'{old!r} -> {new!r}'


def _fmt_fields(fields):
    return ', '.join(f'{k}: {_fmt_change(v["old"], v["new"])}' for k, v in sorted(fields.items()))


def _assoc_label(item):
    bits = [item['type'], item.get('target') or '']
    if item.get('name'):
        bits.append(f'name={item["name"]}')
    if item.get('foreign_key'):
        bits.append(f'fk={item["foreign_key"]}')
    if item.get('through'):
        bits.append(f'through={item["through"]}')
    if item.get('polymorphic'):
        bits.append('polymorphic')
    if item.get('provenance'):
        bits.append(f'provenance={item["provenance"]}')
    return ' '.join(bits)


def _index_label(item):
    return f'({", ".join(item["columns"])}) unique={item["unique"]}'


def _render_table_detail(td, indent='      '):
    lines = []
    if 'comment' in td:
        lines.append(f'{indent}comment: {_fmt_change(td["comment"]["old"], td["comment"]["new"])}')
    if 'columns' in td:
        c = td['columns']
        for name in c['added']:
            lines.append(f'{indent}+ column {name}')
        for name in c['removed']:
            lines.append(f'{indent}- column {name}')
        for name in sorted(c['changed']):
            lines.append(f'{indent}~ column {name} ({_fmt_fields(c["changed"][name]["fields"])})')
    if 'indexes' in td:
        ix = td['indexes']
        for item in ix['added']:
            lines.append(f'{indent}+ index {_index_label(item)}')
        for item in ix['removed']:
            lines.append(f'{indent}- index {_index_label(item)}')
    if 'associations' in td:
        asc = td['associations']
        for item in asc['added']:
            lines.append(f'{indent}+ association {_assoc_label(item)}')
        for item in asc['removed']:
            lines.append(f'{indent}- association {_assoc_label(item)}')
    return lines


def render_text(diff) -> str:
    """--diff-format text (default) rendering: a summary line, then one
    section per top-level kind (tables/notes/groups) with +added/-removed/
    ~changed detail lines. No ANSI color (optional per spec; kept plain for
    simplicity and CI-log friendliness)."""
    t, n, g = diff['tables'], diff['notes'], diff['groups']
    total_added = len(t['added']) + len(n['added']) + len(g['added'])
    total_removed = len(t['removed']) + len(n['removed']) + len(g['removed'])
    total_changed = len(t['changed']) + len(n['changed']) + len(g['changed'])
    if not (total_added or total_removed or total_changed):
        return 'No schema differences.\n'

    lines = [f'{total_added} added, {total_removed} removed, {total_changed} changed', '']

    if t['added'] or t['removed'] or t['changed']:
        lines.append('tables:')
        for name in t['added']:
            lines.append(f'  + {name}')
        for name in t['removed']:
            lines.append(f'  - {name}')
        for name in sorted(t['changed']):
            lines.append(f'  ~ {name}')
            lines.extend(_render_table_detail(t['changed'][name]))
        lines.append('')

    if n['added'] or n['removed'] or n['changed']:
        lines.append('notes:')
        for nid in n['added']:
            lines.append(f'  + {nid}')
        for nid in n['removed']:
            lines.append(f'  - {nid}')
        for nid in sorted(n['changed']):
            lines.append(f'  ~ {nid} ({_fmt_fields(n["changed"][nid]["fields"])})')
        lines.append('')

    if g['added'] or g['removed'] or g['changed']:
        lines.append('groups:')
        for gid in g['added']:
            lines.append(f'  + {gid}')
        for gid in g['removed']:
            lines.append(f'  - {gid}')
        for gid in sorted(g['changed']):
            lines.append(f'  ~ {gid}')
            gd = g['changed'][gid]
            if 'tables' in gd:
                for tn in gd['tables']['added']:
                    lines.append(f'      + table {tn}')
                for tn in gd['tables']['removed']:
                    lines.append(f'      - table {tn}')
            if 'title' in gd:
                lines.append(f'      title: {_fmt_change(gd["title"]["old"], gd["title"]["new"])}')
            if 'color' in gd:
                lines.append(f'      color: {_fmt_change(gd["color"]["old"], gd["color"]["new"])}')
        lines.append('')

    return '\n'.join(lines).rstrip('\n') + '\n'
