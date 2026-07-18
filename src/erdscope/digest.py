# ---------------------------------------------------------------------------
# --emit-digest — token-efficient Markdown digest of the schema, WITH design
# notes, for LLM/agent consumption (backlog #3). Reuses emit.py's
# canonical_schema (same allowlist/pruning/deterministic order --emit-json and
# --emit-config already share) — render_digest never re-derives what survives
# or how it's ordered, only how it's RENDERED. Pure and non-destructive:
# canonical_schema already deep-copies, and nothing here mutates its result.
#
# Differentiator (the digest's whole reason to exist, per EMIT_DIGEST_SPEC.md
# §0): design intent a machine cannot re-derive from the raw schema — notes
# (table/relation/global) — survives. provenance/sources, the legacy db_fk/
# inferred/manual/schema_fk flags, and (by default) nullable/default/extra are
# all dropped to keep the token budget on MEANING, not on every column detail
# an LLM can usually infer or doesn't need. `groups` is dropped entirely: it
# is a viewer-cosmetic layout aid (which tables get drawn inside a rounded
# frame together), not schema semantics — nothing an LLM reading this digest
# would need to reason about the data model.
# ---------------------------------------------------------------------------


def _oneline(s):
    """Collapse a free-text field (comment/note text) to one line — a
    literal newline inside a bullet or heading would break the Markdown
    structure (turn one bullet into what looks like two, or one table
    heading into a heading plus stray body text)."""
    return ' '.join(s.split())


def _notes_by_scope(notes_data):
    """Split canonical `notes` (already id-sorted by emit.py's
    _canonical_notes) into (global list, {table -> [note, ...]},
    {relation identity -> [note, ...]}). A relation identity is
    `(source_table, type, name, foreign_key, through, polymorphic)` — exactly
    the fields a resolved relation note carries (providers.py's
    resolve_and_validate_notes) — so a table's Rel: line can look its own
    associations up against this dict in O(1) instead of re-scanning every
    note per association."""
    global_notes = []
    table_notes = {}
    relation_notes = {}
    for n in notes_data or []:
        if n['scope'] == 'global':
            global_notes.append(n)
        elif n['scope'] == 'table':
            table_notes.setdefault(n['table'], []).append(n)
        else:  # relation
            key = (n['source_table'], n['type'], n.get('name'), n.get('foreign_key'),
                   n.get('through'), bool(n.get('polymorphic')))
            relation_notes.setdefault(key, []).append(n)
    return global_notes, table_notes, relation_notes


def _note_text(n):
    """One note -> a short inline rendering: `title: text` when titled, else
    bare `text`. Links are never rendered here (in verbose mode either) — a
    URL is low value per token for an LLM reader and the digest already
    keeps notes themselves regardless of --digest-verbose (G-1's verbosity
    knob is about column metadata density, not about which notes survive)."""
    text = _oneline(n['text'])
    return f'{n["title"]}: {text}' if n.get('title') else text


def _fk_targets(associations):
    """`{foreign_key_column -> target_table}` for every single-column FK
    association on a table, so each column line can show `fk→<target>`
    without re-scanning associations per column. Association `foreign_key`
    is always single-column (AssociationFragment contract, header.py §4.2);
    on a rare duplicate the deterministically-last (canonical order) wins —
    harmless, since this is an informational cross-reference, not identity."""
    return {a['foreign_key']: a['target'] for a in associations if a.get('foreign_key')}


def _render_column(c, fk_targets, verbose):
    """One canonical column -> its digest bullet line:
    `- name: type[, pk][, fk→target][, null][, default=...][, sql_type][, "comment"]`.
    nullable/default/sql_type only show under --digest-verbose (G-1) —
    dropped by default to keep the per-column token cost to what's needed to
    reconstruct the shape of the table, not every DB-level nuance."""
    bits = [c.get('type', '')]
    if c.get('primary'):
        bits.append('pk')
    target = fk_targets.get(c['name'])
    if target:
        bits.append(f'fk→{target}')
    if verbose:
        if c.get('nullable'):
            bits.append('null')
        if c.get('default'):
            bits.append(f'default={c["default"]}')
        if c.get('sql_type'):
            bits.append(c['sql_type'])
    comment = c.get('comment')
    if comment:
        bits.append(f'"{_oneline(comment)}"')
    return f'- {c["name"]}: ' + ', '.join(bits)


def _assoc_token(a):
    """One canonical association -> its compact Rel: token:
    `type target[ as name][ fk=foreign_key][ through X][ (poly)]`. `as name`
    is included only when the association name differs from its target
    (e.g. belongs_to :user on target `users` needs no `as`; a polymorphic
    belongs_to whose target IS the synthetic association name never adds
    one either, since target already equals name in that case)."""
    bits = [a['type'], a['target']]
    name = a.get('name')
    if name and name != a['target']:
        bits.append(f'as {name}')
    if a.get('foreign_key'):
        bits.append(f'fk={a["foreign_key"]}')
    if a.get('through'):
        bits.append(f'through {a["through"]}')
    if a.get('polymorphic'):
        bits.append('(poly)')
    return ' '.join(bits)


def _relation_note_key(a, table_name):
    return (table_name, a['type'], a.get('name'), a.get('foreign_key'),
            a.get('through'), bool(a.get('polymorphic')))


def _render_rel_line(table_name, associations, relation_notes):
    """The table's one-line association summary (spec §2's `Rel:` line),
    each association compressed to _assoc_token and any relation note(s)
    that resolve to it appended as `— "note text"`. Omitted entirely (returns
    None) when the table has no associations — no reason to spend a line on
    an empty summary."""
    if not associations:
        return None
    tokens = []
    for a in associations:
        token = _assoc_token(a)
        notes = relation_notes.get(_relation_note_key(a, table_name))
        if notes:
            token += ' — ' + '; '.join(f'"{_note_text(n)}"' for n in notes)
        tokens.append(token)
    return 'Rel: ' + ', '.join(tokens)


def render_digest(schema, title=None, verbose=False):
    """Render a canonical `schema` (emit.py's canonical_schema shape:
    {tables, notes?, groups?}) to the --emit-digest Markdown. Pure and
    deterministic: the same schema always renders the same text, since every
    input list is already canonically ordered (emit.py) and this function
    adds no further data-dependent ordering of its own — only `sorted(tables)`
    for the one thing canonical_schema does NOT itself sort (its `tables` is
    a dict keyed by name, in whatever order the caller built it; ordering it
    here, rather than relying on json.dumps(sort_keys=True) the way
    --emit-json/--emit-config's file writers do, is what makes THIS text
    itself byte-deterministic, not just a JSON encoding of it)."""
    global_notes, table_notes, relation_notes = _notes_by_scope(schema.get('notes'))
    tables = schema.get('tables', {})
    names = sorted(tables)

    lines = [f'# {title} — schema digest' if title else '# Schema digest', '']
    if global_notes:
        lines.append('\n\n'.join(_note_text(n) for n in global_notes))
        lines.append('')
    lines.append(f'## Tables ({len(names)})')
    lines.append('')

    for name in names:
        t = tables[name]
        heading = f'### {name}'
        comment = t.get('comment')
        if comment:
            heading += f'  — {_oneline(comment)}'
        lines.append(heading)
        for n in table_notes.get(name, []):
            lines.append(f'_{_note_text(n)}_')
        fk_targets = _fk_targets(t.get('associations', []))
        for c in t.get('columns', []):
            lines.append(_render_column(c, fk_targets, verbose))
        rel_line = _render_rel_line(name, t.get('associations', []), relation_notes)
        if rel_line:
            lines.append(rel_line)
        lines.append('')

    return '\n'.join(lines).rstrip('\n') + '\n'


def emit_digest_document(tables, notes_data, groups_data, title=None, verbose=False):
    """Build the --emit-digest document (as a Markdown string): project the
    final merged IR through the SAME canonical_schema --emit-json/--emit-config
    already share, then render it. `groups_data` is accepted (mirroring the
    other two emitters' signatures, and cli.py already has it in hand at the
    same call site) but intentionally unused here — see the module docstring
    on why groups carry no schema meaning for a digest."""
    schema = canonical_schema(tables, notes_data, groups_data)
    return render_digest(schema, title=title, verbose=verbose)
