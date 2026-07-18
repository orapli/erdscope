# ---------------------------------------------------------------------------
# --emit-mermaid — Mermaid erDiagram export of the schema. Reuses emit.py's
# canonical_schema (same allowlist/pruning/deterministic order every other
# --emit-* flag already shares) — render_mermaid never re-derives what
# survives or how tables/columns/associations are ordered, only how the
# result is RENDERED as Mermaid text.
#
# This is a CLI-side, non-interactive counterpart to the viewer's own
# Mermaid Copy/Download buttons (viewer.html's buildMermaidText/
# exportToMermaid/downloadMermaidFile). The viewer's version is intentionally
# left untouched: it renders whatever subset of tables is CURRENTLY ON SCREEN
# (hidden/banned tables excluded, auto-expand/focus state reflected) — a
# legitimate "export what I'm looking at" use case no CLI flag can stand in
# for. This flag instead renders the schema as canonical_schema already
# resolved it (the whole schema, or whatever --only/--exclude narrowed it
# to) — the same non-interactive/scriptable gap --emit-json/--emit-config/
# --emit-digest/--emit-dbml already fill for their formats. The two code
# paths are NOT unified and are not meant to be; see DESIGN_ROADMAP.md.
#
# Semantics are ported from viewer.html's getDisplayEdges()/edgeCard()/
# buildMermaidText() — read there for the original — reconstructed here
# against canonical_schema's clean, allowlisted associations (direct
# foreign_key/type/target/through/polymorphic fields) rather than the
# viewer's DOM/display-state-flavored inputs. `fk_columns` is NOT reused
# (canonical_schema deliberately strips that viewer-only derived field) —
# each table's FK column set is recomputed locally with the exact same rule
# cli.py's _finish uses to compute it in the first place:
# `{a['foreign_key'] for a in table_associations if a.get('foreign_key')}`.
#
# EDGE AGGREGATION: canonical_schema's associations are one-directional and
# table-local — a `belongs_to` on `posts` targeting `users` and a `has_many`
# on `users` targeting `posts` are two separate dicts on two separate
# tables, not one bidirectional edge. Every table (in name order, for
# determinism) contributes its own associations to an unordered-pair map,
# skipping: polymorphic associations (no real target), associations whose
# target isn't a real table in this schema (defensive — canonical_schema
# already prunes non-polymorphic dangling targets, but costs nothing to
# re-check), associations whose `through` names a real table in this schema
# (the join table itself represents the relation directly, so the implied
# many-to-many edge would be redundant), and `has_and_belongs_to_many`
# associations whose sorted-pair join-table name
# (`'_'.join(sorted([this_table, target]))`) is itself a real table (same
# redundancy reasoning). Survivors are grouped by the unordered pair
# `tuple(sorted([this_table, target]))`; every association naming that pair
# (from either side) is folded into one edge record, each tagged with the
# table it came from (mirrors the JS `{from:name, ...a}` shape) since
# cardinality resolution needs to know which side declared what.
#
# CARDINALITY (ported from edgeCard()): `direct` = associations on the edge
# that are neither `through`-based nor `has_and_belongs_to_many` (only these
# carry real cardinality signal). No direct associations -> many-to-many
# (`nn`) — only a join table or HABTM link the pair. Else: a `has_many`
# among them -> one-to-many (`1n`), with `many` = the edge's OTHER table
# (the has_many's own table is the "one" side: "users has_many posts" means
# posts is many). Else a `has_one` -> one-to-one (`11`). Else a `belongs_to`
# -> one-to-many (`1n`), with `many` = the belongs_to's OWN table (opposite
# of has_many: "posts belongs_to user" means posts itself is many — no
# flip). Else (unreachable in practice, matching the JS fallback) -> `1n`
# with `many` = the edge's target-side table.
#
# Deterministic output is a hard requirement, same as every other --emit-*
# flag: edges are rendered in `tuple(sorted(pair))` order, tables in name
# order, columns in their already-canonical (per-table) order.
#
# Mermaid is a lightweight DIAGRAM notation, not a schema-definition
# language: column types render from the column's coarse `type` field
# (`'string'`/`'integer'`/...), never `sql_type` — a deliberate departure
# from dbml.py's fidelity-first `sql_type`-preferred rendering, matching the
# viewer's own `c.type||'string'`. Do not "fix" this to prefer `sql_type`
# to bring it in line with dbml.py; the two exports have different jobs.
#
# Column names failing `^[A-Za-z0-9_]+$` (pseudo-columns synthesized from
# expression indexes) are skipped, same as the viewer. The explicit ASCII
# character class is deliberate, not a style choice: Python's `\w` is
# Unicode-aware by default and would keep letters JS's (effectively ASCII)
# `\w` rejects — see dbml.py's own `_IDENT_RE` for the same precedent.
#
# Notes and groups are accepted (mirrors emit_dbml_document's signature —
# cli.py already has both in hand at the call site, and keeping every
# emit-family builder's signature uniform is simpler than special-casing
# this one) but never rendered: Mermaid erDiagram output has no notion of
# either, and the viewer's own buildMermaidText has never drawn them.
#
# Pure and non-destructive: render_mermaid/emit_mermaid_document never
# mutate their input (canonical_schema already deep-copies; nothing here
# sorts in place).
#
# Self-contained by convention: this module does not import from
# plantuml.py (nor vice versa) even though the two share a structurally
# similar edge-aggregation/cardinality shape — see dbml.py, which similarly
# never imports digest.py despite the structural overlap. Names below are
# prefixed `_mmd_` specifically so they can't collide with (and silently
# shadow) plantuml.py's own copies once build_single_file.py concatenates
# every fragment into one flat module/namespace.
# ---------------------------------------------------------------------------

_MMD_COL_NAME_RE = re.compile(r'[A-Za-z0-9_]+')


def _mmd_display_edges(tables):
    """Aggregate every table's one-directional `associations` into
    unordered-pair edge records: `{'source', 'target', 'assocs': [...]}`,
    one per distinct table pair, `assocs` holding every surviving
    association naming that pair (from either side), each tagged with the
    table it came from. `source`/`target` are whichever table/target first
    created the pair's entry, in table-name order (mirrors the JS map's
    insertion-order semantics) — deterministic since canonical_schema's own
    table dict is iterated in the fixed `sorted(tables)` order here. Returned
    as a list ordered by the pair's own sorted-name key, for a second,
    independent layer of determinism (JS returns Map insertion order; this
    port additionally sorts so the result never depends on iteration
    happenstance)."""
    tset = set(tables)
    edges = {}
    for name in sorted(tables):
        for a in tables[name].get('associations', []):
            if a.get('polymorphic'):
                continue
            target = a.get('target')
            if target not in tset:
                continue
            through = a.get('through')
            if through and through in tset:
                continue
            if a.get('type') == 'has_and_belongs_to_many':
                join_name = '_'.join(sorted([name, target]))
                if join_name in tset:
                    continue
            key = tuple(sorted([name, target]))
            if key not in edges:
                edges[key] = {'source': name, 'target': target, 'assocs': []}
            edges[key]['assocs'].append({'from': name, **a})
    return [edges[key] for key in sorted(edges)]


def _mmd_edge_card(edge):
    """One edge's cardinality: `{'kind': 'nn'}`, `{'kind': '11'}`, or
    `{'kind': '1n', 'many': <table name>}`. See the module docstring's
    CARDINALITY section for the full rule; ported 1:1 from viewer.html's
    edgeCard()."""
    direct = [a for a in edge['assocs']
             if not a.get('through') and a['type'] != 'has_and_belongs_to_many']
    if not direct:
        return {'kind': 'nn'}
    hm = next((a for a in direct if a['type'] == 'has_many'), None)
    if hm:
        many = edge['target'] if hm['from'] == edge['source'] else edge['source']
        return {'kind': '1n', 'many': many}
    if any(a['type'] == 'has_one' for a in direct):
        return {'kind': '11'}
    bt = next((a for a in direct if a['type'] == 'belongs_to'), None)
    if bt:
        return {'kind': '1n', 'many': bt['from']}
    return {'kind': '1n', 'many': edge['target']}


def _mmd_relationship_line(edge):
    """One edge -> its Mermaid relationship line (no leading/trailing
    newline). `nn`/`11` render bare crow's-foot tokens; `1n` renders
    `||--o{` with the endpoints swapped, if needed, so the many side always
    ends up on the right (Mermaid erDiagram reads left-to-right as
    one-to-many)."""
    card = _mmd_edge_card(edge)
    a, b = edge['source'], edge['target']
    if card['kind'] == 'nn':
        rel = '}o--o{'
    elif card['kind'] == '11':
        rel = '||--||'
    else:
        rel = '||--o{'
        if card['many'] == a:
            a, b = b, a
    label = (edge['assocs'][0].get('name') or '').replace('"', "'")
    return f'    {a} {rel} {b} : "{label}"'


def _mmd_render_table_block(name, t):
    """One table -> its `{table} {{ ... }}` column block, as a list of
    lines. Pseudo-columns (expression-index artifacts) are skipped; each
    surviving column renders as `{type} {name}[ PK| FK]`, PK taking
    precedence over FK when (hypothetically) a column were both."""
    fk_cols = {a['foreign_key'] for a in t.get('associations', []) if a.get('foreign_key')}
    lines = [f'    {name} {{']
    for c in t.get('columns', []):
        if not _MMD_COL_NAME_RE.fullmatch(c['name']):
            continue
        marker = ' PK' if c.get('primary') else (' FK' if c['name'] in fk_cols else '')
        ctype = c.get('type') or 'string'
        lines.append(f'        {ctype} {c["name"]}{marker}')
    lines.append('    }')
    return lines


def render_mermaid(schema):
    """Render a canonical `schema` (emit.py's canonical_schema shape:
    {tables, notes?, groups?}) to Mermaid erDiagram text. `notes`/`groups`,
    even when present, are never read (see module docstring). Pure and
    deterministic: the same schema always renders the same text, since
    canonical_schema's tables/columns/associations are already canonically
    ordered and this function sorts tables by name itself (as dbml.py's
    render_dbml also does, since `tables` is a plain name-keyed dict)."""
    tables = schema.get('tables', {})
    names = sorted(tables)
    edges = _mmd_display_edges(tables)

    lines = ['erDiagram']
    for edge in edges:
        lines.append(_mmd_relationship_line(edge))
    for name in names:
        lines.extend(_mmd_render_table_block(name, tables[name]))

    return '\n'.join(lines) + '\n'


def emit_mermaid_document(tables, notes_data, groups_data):
    """Build the --emit-mermaid document: project the final merged IR
    through the SAME canonical_schema --emit-json/--emit-config/--emit-
    digest/--emit-dbml already share, then render it. `notes_data`/
    `groups_data` are accepted (uniform emit-family builder signature) but
    intentionally unused — see module docstring."""
    schema = canonical_schema(tables, notes_data, groups_data)
    return render_mermaid(schema)
