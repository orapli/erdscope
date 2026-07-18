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


# ---------------------------------------------------------------------------
# --- MERMAID INPUT — typed source `mermaid.er` (backlog P5) -----------------
#
# The reverse half of this file: a static, line-oriented Mermaid `erDiagram`
# parser (no mermaid-cli/library dependency — dependency-zero is
# non-negotiable, same reasoning dbml.py's INPUT half and rails_schema.py
# both already document). MVP scope, per DESIGN_ROADMAP.md's P5 task
# breakdown: a standalone .mmd/.mermaid file containing exactly one
# `erDiagram` — no Markdown-fence extraction (pulling an erDiagram out of a
# ```mermaid fenced block inside a larger .md file) is a possible future
# extension, not attempted here.
#
# D-2 (DESIGN_ROADMAP.md): Mermaid input is authority kind='sketch' — a NEW,
# LOWEST rank in BOTH merge.py's _PHYSICAL_RANK and _LOGICAL_RANK, below even
# framework code. A Mermaid column's `type` is free text a human jotted down
# while sketching a diagram, never a precise type the way a live DB, a
# schema.rb/.dbml dump, or framework code's own column parsing (Prisma/
# Django) is — it must never win a physical/logical authority tie against
# any of them. Ref-derived associations (see below) intentionally carry NO
# special legacy flag (bare, so `provenance_of` reads them as 'declared',
# same as a framework association) — unlike dbml.py's INPUT half, which
# marks its Ref-derived associations `schema_fk: True`. This is deliberate,
# not an inconsistency: `_PROV_PRECEDENCE` (§9.1) is a purely cosmetic
# "which representative-provenance badge wins when the SAME edge is asserted
# by 2+ layers" choice — it never affects which layer's actual VALUES win
# (that is `_merge_association_group`'s `ms[-1]` layer-order rule, wholly
# separate from provenance). The real "sketch input must never out-rank a
# more authoritative source" guarantee is what the NEW 'sketch' rank in
# _PHYSICAL_RANK/_LOGICAL_RANK already provides, for columns/pk/indexes/
# comment. Introducing a sixth provenance value purely for a cosmetic badge
# choice would be a format bump (see emit.py's own `_VALID_PROVENANCE`
# comment) for no real semantic gain, so this parser doesn't attempt it.
#
# SCOPE — what this parser supports:
#   * An entity block: `NAME {` ... `}`, each body line
#     `type name [PK|FK|UK[, ...]] ["comment"]`. `type` is kept VERBATIM as
#     both `type` and `sql_type` — unlike dbml.py's INPUT half, there is no
#     SQL_TYPES lookup here, symmetric with render_mermaid's own `c.type||
#     'string'` (Mermaid's type vocabulary already IS erdscope's own coarse
#     vocabulary, not a native DB type system). `PK` folds into the table's
#     primary_key candidate list (string for one column, list for 2+ — same
#     mechanism dbml.py's INPUT half uses, and for the same reason:
#     merge.py's `_normalize_primary_key` is what actually stamps `primary:
#     True` post-merge, never this parser directly). `FK` is a pure display
#     hint in real Mermaid (it doesn't name a target) and carries no
#     actionable IR information on its own — recognized, but a no-op, same
#     category as dbml.py's Enum handling. `UK` becomes a synthetic
#     single-column unique index (mirrors dbml.py's `unique` column attr).
#     A trailing quoted string is the column's `comment`.
#   * A relationship line: `ENTITY1 <card><line><card> ENTITY2[ : label]`,
#     where `<card>` is one of Mermaid's 4 crow's-foot tokens per side
#     (`|o`/`||`/`}o`/`}|` on the left, mirrored `o|`/`||`/`o{`/`|{` on the
#     right) and `<line>` is `--` or `..` (both accepted; Mermaid's dotted
#     form is a "non-identifying" relationship, a distinction erdscope's IR
#     has no slot for, so it collapses to the SAME cardinality reading as
#     the solid form). Cardinality maps onto the association vocabulary
#     just like dbml.py's Ref symbols do, but WITHOUT a `foreign_key` — a
#     relationship line names no column at all, only two entities and a
#     label, so there is nothing to hang a foreign_key on: both-many ->
#     `has_and_belongs_to_many` (declaring side = the LEFT entity, target =
#     right); both-one -> `has_one` (declaring side = LEFT, target =
#     right); one side many / other one -> `belongs_to` on whichever side
#     is the "many" one (target = the "one" side) — mirroring exactly what
#     `belongs_to` already means (the many side is the one that belongs to
#     the one side), regardless of which physical column would implement it
#     in a real schema. The label (if any, quoted or bare) becomes the
#     association's `name`; an empty/absent label omits `name` entirely
#     (same optional-when-falsy convention every other provider follows).
#   * Either side of a relationship line naming an entity with NO `{ }`
#     block anywhere in the file still produces a (columnless) table for
#     it — a Mermaid diagram commonly shows relationships for entities whose
#     attributes weren't detailed.
#   * A leading bare `erDiagram` line is recognized and consumed (optional —
#     nothing downstream needs it, and a file missing it still parses the
#     same way; MVP assumes the file's content is a SINGLE erDiagram, so
#     nothing else meaningfully precedes it anyway).
#   * `%%` line comments (Mermaid's own comment syntax — NOT `//`, unlike
#     DBML) are stripped, quote-aware.
#
# OUT OF SCOPE this round (warn-and-skip, never silently dropped — same
# philosophy as dbml.py's INPUT half and rails_schema.py):
#   * Any Mermaid diagram directive/styling this diagram TYPE doesn't have
#     anyway (erDiagram has no `classDef`/`click`/etc. — those belong to
#     other Mermaid diagram types and would never legitimately appear here).
#   * A quoted entity name (`"My Entity" { ... }` — a newer Mermaid feature)
#     — entity names must be bare identifiers here, matching render_mermaid
#     itself, which never quotes or aliases a table name (contrast
#     plantuml.py's INPUT half, which inherits PlantUML's own alias
#     machinery because PlantUML always needed one).
#   * Markdown-fence extraction (pulling ```mermaid out of a larger .md
#     file) — the MVP takes a standalone .mmd/.mermaid file only.
#   * ONE STATEMENT PER PHYSICAL LINE throughout, same as dbml.py's INPUT
#     half — an entity block that opens and closes on one physical line
#     is not recognized as a block header at all.
#
# Never silently drop: every unrecognized line (top-level or inside an
# entity block) warns with `path:line: ...` and is skipped, never causing
# the whole parse to fail. A file with zero entity blocks AND zero
# relationship lines (so no tables at all) parses to an empty `tables`
# dict, which sources.py's typed-dispatch layer turns into the standard
# "found nothing to parse" hard error, same as every other typed source.
# ---------------------------------------------------------------------------

_MMD_ENTITY_HEADER_RE = re.compile(r'^([A-Za-z_]\w*)\s*\{$')
_MMD_LEFT_CARD = r'\|o|\|\||\}o|\}\|'
_MMD_RIGHT_CARD = r'o\||\|\||o\{|\|\{'
_MMD_REL_RE = re.compile(
    rf'^(?P<e1>[A-Za-z_]\w*)\s+(?P<lc>{_MMD_LEFT_CARD})(?:--|\.\.)(?P<rc>{_MMD_RIGHT_CARD})'
    rf'\s+(?P<e2>[A-Za-z_]\w*)(?:\s*:\s*(?P<label>.+))?$')
_MMD_COLUMN_LINE_RE = re.compile(
    r'^(?P<type>[A-Za-z_][\w-]*(?:\([^)]*\))?)\s+(?P<name>[A-Za-z_]\w*)(?P<rest>.*)$')
_MMD_KEY_TOKENS = ('PK', 'FK', 'UK')


def _mmd_strip_line_comment(line):
    """Cut `line` at its first top-level `%%` (Mermaid's own comment marker
    — NOT `//`, unlike DBML) — quote-aware (a `%%` inside a `"..."` label
    survives)."""
    quote, i, n = None, 0, len(line)
    while i < n:
        c = line[i]
        if quote:
            if c == '\\' and i + 1 < n:
                i += 2
                continue
            if c == '"':
                quote = None
            i += 1
            continue
        if c == '"':
            quote = c
            i += 1
            continue
        if c == '%' and i + 1 < n and line[i + 1] == '%':
            return line[:i]
        i += 1
    return line


def _mmd_unquote_label(raw):
    """A relationship line's captured label text (after the `:`, not yet
    stripped) -> its resolved string, or `None` for an empty/whitespace-only
    label (the caller then omits `name` entirely). A `"..."` wrapped label
    is unescaped (`\\"` -> `"`); a bare (unquoted) label is accepted as-is,
    leniently, for hand-written diagrams that skip the quotes."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        raw = raw[1:-1].replace('\\"', '"')
    return raw or None


def _mmd_parse_entity_column_line(line):
    """One entity-block body line -> `(type_tok, name, keys, comment)`, or
    `None` if it doesn't even have the shape `type name...` — the caller
    then warns and skips the line. `keys` is the list of recognized
    `PK`/`FK`/`UK` tokens found (case-normalized to upper); an unrecognized
    trailing token warns separately but doesn't invalidate the whole line."""
    m = _MMD_COLUMN_LINE_RE.match(line.strip())
    if not m:
        return None
    rest = m.group('rest').strip()
    comment = None
    qm = re.search(r'"((?:[^"\\]|\\.)*)"\s*$', rest)
    if qm:
        comment = qm.group(1).replace('\\"', '"')
        rest = rest[:qm.start()].strip()
    keys = [tok.strip().upper() for tok in re.split(r'[,\s]+', rest) if tok.strip()]
    return m.group('type'), m.group('name'), keys, comment


def _mmd_parse_entity_block(lines, start, name, warn):
    """Parse an entity body starting at `lines[start]` through its closing
    `}` (exclusive) — returns `(table_fragment, end, closed)`, mirroring
    dbml.py's `_dbml_parse_table_block` shape/contract (including the
    unterminated-block discard rule — see that function's docstring)."""
    table = {'columns': [], 'indexes': [], 'associations': [], 'primary_key': None}
    pk_candidates = []
    i, n = start, len(lines)
    closed = False
    while i < n:
        stripped = _mmd_strip_line_comment(lines[i]).strip()
        if not stripped:
            i += 1
            continue
        if stripped == '}':
            closed = True
            break
        parsed = _mmd_parse_entity_column_line(stripped)
        if parsed is None:
            warn(i + 1, f'unknown entity statement {stripped!r} — skipped')
            i += 1
            continue
        type_tok, cname, keys, comment = parsed
        col = {'name': cname, 'type': type_tok, 'sql_type': type_tok, 'nullable': True}
        if comment:
            col['comment'] = comment
        for key in keys:
            if key == 'PK':
                if cname not in pk_candidates:
                    pk_candidates.append(cname)
            elif key == 'UK':
                col['_mmd_unique'] = True
            elif key != 'FK':
                warn(i + 1, f"unknown column key {key!r} ({name}.{cname}) — ignored")
        table['columns'].append(col)
        i += 1
    if not closed:
        warn(n, f'unterminated entity {name!r} block at end of file — skipped')
    existing_single_cols = {ix['columns'][0] for ix in table['indexes']
                            if len(ix['columns']) == 1}
    for col in table['columns']:
        if col.pop('_mmd_unique', False) and col['name'] not in existing_single_cols:
            table['indexes'].append({'columns': [col['name']], 'unique': True})
            existing_single_cols.add(col['name'])
    if pk_candidates:
        table['primary_key'] = pk_candidates[0] if len(pk_candidates) == 1 else pk_candidates
        pk_set = set(pk_candidates)
        for c in table['columns']:
            if c['name'] in pk_set:
                c['nullable'] = False
    return table, i, closed


def _mmd_ensure_table(tables, name):
    """A relationship line may name an entity with no `{ }` block anywhere
    in the file — Mermaid diagrams commonly show relationships without
    attribute detail for some/all entities. Ensures `tables[name]` exists
    (columnless stub) without clobbering an already-parsed entity block."""
    if name not in tables:
        tables[name] = {'columns': [], 'indexes': [], 'associations': [], 'primary_key': None}


def _mmd_resolve_relationship(m, tables):
    """A matched _MMD_REL_RE -> materializes the association directly onto
    the declaring table (creating columnless stubs for either entity if
    needed — see _mmd_ensure_table). See the module docstring's
    relationship-mapping bullet for the full symbol -> association-type
    rule; no `foreign_key` is ever set (a relationship line names no
    column), and no special provenance flag is set (see module docstring's
    D-2 section on why that's a deliberate no-op here)."""
    e1, e2 = m.group('e1'), m.group('e2')
    _mmd_ensure_table(tables, e1)
    _mmd_ensure_table(tables, e2)
    left_many = m.group('lc') in ('}o', '}|')
    right_many = m.group('rc') in ('o{', '|{')
    label = m.group('label')
    name = _mmd_unquote_label(label) if label else None
    if left_many and right_many:
        decl, target, atype = e1, e2, 'has_and_belongs_to_many'
    elif not left_many and not right_many:
        decl, target, atype = e1, e2, 'has_one'
    elif right_many:
        decl, target, atype = e2, e1, 'belongs_to'
    else:
        decl, target, atype = e1, e2, 'belongs_to'
    # `name` is a REQUIRED fragment-level field (header.py's AssociationFragment
    # shape — no `?`), unlike the canonical/output shape where it's dropped
    # when falsy (emit.py's `_canonical_associations`) — an empty string here
    # is exactly what every other provider already does for an unnamed edge.
    assoc = {'type': atype, 'name': name or '', 'target': target}
    tables[decl].setdefault('associations', []).append(assoc)


def mermaid_er_provider(path, given=None):
    """ProviderResult (kind='sketch', provider='mermaid.er') for a Mermaid
    `erDiagram` file: entity blocks (columns, PK/UK) plus relationship-line
    associations — parsed by pure text analysis, no mermaid-cli/library
    dependency. See the module docstring above for the full supported/
    out-of-scope grammar.

    `path` is always the resolved Path actually read from disk; `given` is
    the (possibly relative, possibly unresolved) path string to DISPLAY in
    warnings/location, exactly like rails_schema_provider/dbml_provider."""
    display = given if given is not None else str(path)
    text = path.read_text(encoding='utf-8', errors='replace')
    lines = text.splitlines()
    n = len(lines)
    warnings = []

    def warn(line_no, msg):
        warnings.append(f'{display}:{line_no}: {msg}')

    tables = {}
    pending_rels = []
    i = 0
    while i < n:
        stripped = _mmd_strip_line_comment(lines[i]).strip()
        if not stripped:
            i += 1
            continue
        if stripped == 'erDiagram':
            i += 1
            continue
        m = _MMD_ENTITY_HEADER_RE.match(stripped)
        if m:
            ename = m.group(1)
            table, end, closed = _mmd_parse_entity_block(lines, i + 1, ename, warn)
            if closed:
                if ename in tables:
                    warn(i + 1, f'duplicate entity {ename!r} — later definition ignored')
                else:
                    tables[ename] = table
            i = end + 1
            continue
        m = _MMD_REL_RE.match(stripped)
        if m:
            # Deferred to after the whole file is parsed (not resolved here
            # inline): an entity mentioned only in relationship lines so far
            # might still get its own explicit `{ }` block LATER in the
            # file — resolving now would stub it early and make that later
            # block look like a spurious "duplicate entity".
            pending_rels.append(m)
            i += 1
            continue
        warn(i + 1, f'unknown statement {stripped!r} — skipped')
        i += 1

    for m in pending_rels:
        _mmd_resolve_relationship(m, tables)

    return make_provider_result('sketch', 'mermaid.er', tables,
                                location=display, warnings=warnings)
