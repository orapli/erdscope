# ---------------------------------------------------------------------------
# --emit-plantuml — PlantUML entity-relationship export of the schema.
# Reuses emit.py's canonical_schema (same allowlist/pruning/deterministic
# order every other --emit-* flag already shares) — render_plantuml never
# re-derives what survives or how it's ordered, only how it's RENDERED as
# PlantUML text.
#
# Same CLI-side / non-interactive rationale as mermaid.py — read that
# module's docstring for the full "why this exists alongside the viewer's
# own Copy/Download buttons, and why the two are deliberately not unified"
# reasoning; it applies here unchanged (viewer.html's
# buildPlantUMLText/exportToPlantUML/downloadPlantUMLFile is the untouched
# counterpart this flag does not replace).
#
# Semantics are ported from viewer.html's getDisplayEdges()/edgeCard()/
# buildPlantUMLText() — read there for the original. Edge aggregation and
# cardinality resolution are IDENTICAL in rule to mermaid.py's (same
# getDisplayEdges()/edgeCard() source); see that module's docstring for the
# full EDGE AGGREGATION / CARDINALITY writeup. This module deliberately
# duplicates that ~20-line logic rather than importing mermaid.py — every
# emit-family module in this codebase is self-contained (dbml.py doesn't
# import digest.py despite a similar structural overlap); all names below
# are prefixed `_puml_` specifically so they can't collide with (and
# silently shadow) mermaid.py's own copies once build_single_file.py
# concatenates every fragment into one flat module/namespace.
#
# `fk_columns` is NOT reused (canonical_schema deliberately strips that
# viewer-only derived field) — each table's FK column set is recomputed
# locally with the same rule cli.py's _finish uses:
# `{a['foreign_key'] for a in table_associations if a.get('foreign_key')}`.
#
# ENTITY ALIASING: a PlantUML identifier — used both as an entity's own
# alias and in every relationship line referencing it — must itself match
# `^[A-Za-z_][A-Za-z0-9_]*$`. A table name that already matches is used
# as-is; otherwise every character outside `[A-Za-z0-9_]` is replaced with
# `_` to build the alias. An entity is declared with an explicit
# `"name" as alias` form whenever the alias differs from the real name OR
# the table has a comment; otherwise the bare `entity alias {` form is
# used. A comment (when present) is appended to the display name as
# `（comment）` — FULL-WIDTH parentheses, deliberately, matching the
# viewer's own Japanese-friendly formatting; not a typo. `"` inside a
# comment or a relationship label is replaced with `'`.
#
# Column rendering: primary-key columns first (each `  * {name} : {type}
# <<PK>>`), then — IF both a PK and a non-PK column exist — a `  --`
# separator, then non-PK columns (each `  {mark}{name} : {type}{fk}`, where
# `mark` is `'* '` for a NOT NULL column and empty for nullable, and `fk`
# is ` <<FK>>` when the column is in the table's locally-recomputed FK set).
# Same coarse `type` field as mermaid.py (never `sql_type` — see that
# module's docstring for why this deliberately differs from dbml.py), same
# pseudo-column skip (`^[A-Za-z0-9_]+$`, explicit ASCII class — Python's
# `\w` is Unicode-aware by default, unlike JS's effectively-ASCII `\w`).
#
# Relationship lines reuse the SAME crow's-foot tokens as Mermaid
# (`}o--o{`/`||--||`/`||--o{`) — a coincidence the viewer's own
# buildPlantUMLText comment already calls out, not a deliberate shared
# format — but reference each table by its ALIAS (never its raw name), and
# render the label unquoted (`alias_a rel alias_b : label`) when present,
# with no `:` segment at all when it's empty — unlike Mermaid, which always
# quotes a (possibly empty) label.
#
# Deterministic output is a hard requirement, same as every other --emit-*
# flag: edges are rendered in `tuple(sorted(pair))` order, tables (and
# their entity blocks) in name order, columns in their already-canonical
# (per-table) order.
#
# Notes and groups are accepted (mirrors emit_dbml_document/
# emit_mermaid_document's signature) but never rendered — PlantUML ER
# output has no notion of either, and the viewer's own buildPlantUMLText has
# never drawn them.
#
# Pure and non-destructive: render_plantuml/emit_plantuml_document never
# mutate their input (canonical_schema already deep-copies; nothing here
# sorts in place).
# ---------------------------------------------------------------------------

_PUML_COL_NAME_RE = re.compile(r'[A-Za-z0-9_]+')
_PUML_IDENT_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
_PUML_NONWORD_RE = re.compile(r'[^A-Za-z0-9_]')


def _puml_alias(name):
    """A table name -> its PlantUML identifier: itself when it already
    matches `^[A-Za-z_][A-Za-z0-9_]*$`, else every non-`[A-Za-z0-9_]`
    character replaced with `_`."""
    if _PUML_IDENT_RE.fullmatch(name):
        return name
    return _PUML_NONWORD_RE.sub('_', name)


def _puml_display_edges(tables):
    """Identical rule to mermaid.py's _mmd_display_edges — see that
    module's EDGE AGGREGATION docstring section for the full writeup.
    Duplicated (not imported) per this module's self-containment
    convention."""
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


def _puml_edge_card(edge):
    """Identical rule to mermaid.py's _mmd_edge_card — see that module's
    CARDINALITY docstring section for the full writeup."""
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


def _puml_relationship_line(edge):
    """One edge -> its PlantUML relationship line (no leading/trailing
    newline), referencing each side by its alias. Same crow's-foot tokens
    and many-side-on-the-right swap rule as mermaid.py's
    _mmd_relationship_line, but the label (when present) is unquoted and
    the `:` segment is omitted entirely when there's no label."""
    card = _puml_edge_card(edge)
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
    alias_a, alias_b = _puml_alias(a), _puml_alias(b)
    if label:
        return f'{alias_a} {rel} {alias_b} : {label}'
    return f'{alias_a} {rel} {alias_b}'


def _puml_render_entity_block(name, t):
    """One table -> its `entity ... {{ ... }}` block, as a list of lines
    (the trailing blank-line separator is included, matching the viewer's
    own one-blank-line-per-entity spacing)."""
    alias = _puml_alias(name)
    comment = t.get('comment')
    needs_alias = bool(comment) or alias != name
    if needs_alias:
        suffix = f'（{comment}）'.replace('"', "'") if comment else ''
        display = name.replace('"', "'") + suffix
        lines = [f'entity "{display}" as {alias} {{']
    else:
        lines = [f'entity {alias} {{']

    cols = [c for c in t.get('columns', []) if _PUML_COL_NAME_RE.fullmatch(c['name'])]
    fk_cols = {a['foreign_key'] for a in t.get('associations', []) if a.get('foreign_key')}
    pk_cols = [c for c in cols if c.get('primary')]
    rest_cols = [c for c in cols if not c.get('primary')]

    for c in pk_cols:
        ctype = c.get('type') or 'string'
        lines.append(f'  * {c["name"]} : {ctype} <<PK>>')
    if pk_cols and rest_cols:
        lines.append('  --')
    for c in rest_cols:
        ctype = c.get('type') or 'string'
        mark = '' if c.get('nullable', False) else '* '
        fk = ' <<FK>>' if c['name'] in fk_cols else ''
        lines.append(f'  {mark}{c["name"]} : {ctype}{fk}')

    lines.append('}')
    lines.append('')
    return lines


def render_plantuml(schema):
    """Render a canonical `schema` (emit.py's canonical_schema shape:
    {tables, notes?, groups?}) to PlantUML entity-relationship text.
    `notes`/`groups`, even when present, are never read (see module
    docstring). Pure and deterministic: the same schema always renders the
    same text, since canonical_schema's tables/columns/associations are
    already canonically ordered and this function sorts tables by name
    itself (as dbml.py's/mermaid.py's render functions also do, since
    `tables` is a plain name-keyed dict)."""
    tables = schema.get('tables', {})
    names = sorted(tables)
    edges = _puml_display_edges(tables)

    lines = ['@startuml', 'hide circle', 'skinparam linetype ortho', '']
    for name in names:
        lines.extend(_puml_render_entity_block(name, tables[name]))
    for edge in edges:
        lines.append(_puml_relationship_line(edge))
    lines.append('@enduml')

    return '\n'.join(lines) + '\n'


def emit_plantuml_document(tables, notes_data, groups_data):
    """Build the --emit-plantuml document: project the final merged IR
    through the SAME canonical_schema --emit-json/--emit-config/--emit-
    digest/--emit-dbml/--emit-mermaid already share, then render it.
    `notes_data`/`groups_data` are accepted (uniform emit-family builder
    signature) but intentionally unused — see module docstring."""
    schema = canonical_schema(tables, notes_data, groups_data)
    return render_plantuml(schema)
