# ---------------------------------------------------------------------------
# --emit-dbml — minimal DBML export of the schema (backlog #5). Reuses emit.py's
# canonical_schema (same allowlist/pruning/deterministic order --emit-json/
# --emit-config/--emit-digest already share) — render_dbml never re-derives
# what survives or how it's ordered, only how it's RENDERED as DBML text.
#
# This is deliberately the EXPORT half only (DBML *input* — parsing a .dbml
# file into the IR — is a separate, later piece of work; building export
# first fixes the IR<->DBML mapping so a future importer can reuse it).
# The MINIMAL version covers tables/columns/primary keys/indexes/single-
# column-FK relations (`Ref:`)/table comments ONLY. Notes, groups, and
# DBML's own `Project`/`TableGroup` blocks are explicitly OUT OF SCOPE this
# round (a later "extension" phase) — `notes`/`groups` are present in the
# canonical schema input but intentionally ignored/unused here, mirroring
# how digest.py's emit_digest_document accepts-but-ignores `groups_data`.
#
# THE LOSSY CONTRACT — read this before touching Ref generation:
#
#   Only `belongs_to` associations with a truthy `foreign_key` ever produce a
#   `Ref:` line. This is narrower than it looks, and deliberately so. Across
#   every provider in this codebase (db/base.py, frameworks/rails.py,
#   frameworks/prisma.py, frameworks/django.py), a `belongs_to`'s
#   `foreign_key` ALWAYS names a column on that association's own/declaring
#   table — every provider agrees on this, so it's always safe to render
#   `Ref: <this table>.<fk> > <target>.<target pk>`.
#
#   `has_one`'s `foreign_key` is NOT safe the same way: it is AMBIGUOUS across
#   providers. db/base.py and frameworks/prisma.py both auto-label a
#   same-table unique FK column as `has_one` (for them, `foreign_key` is
#   still a column on the DECLARING table, exactly like belongs_to) — but
#   frameworks/rails.py's hand-written `has_one :profile, foreign_key: :xyz`
#   scan means the OPPOSITE: `xyz` names a column on the OTHER (target)
#   table, not this one. Once an association reaches canonical_schema there
#   is no remaining signal for which provider produced a given `has_one`, so
#   a `has_one`'s `foreign_key` cannot be safely resolved to a `Ref:` without
#   risking a Ref that points at a column that doesn't exist on the source
#   table (silently WRONG DBML, not just incomplete). So `has_one`/
#   `has_many`/`has_and_belongs_to_many` are excluded from Ref generation
#   ENTIRELY — only ever `belongs_to`. Do not "fix" this by adding `has_one`
#   back in; it would ship a wrong Ref for Rails schemas.
#
#   A polymorphic `belongs_to` is skipped silently (no warning) — its
#   `target` is a synthetic, tableless placeholder (canonical_schema keeps it
#   in the association list per its own docstring so the details pane still
#   has something to show), and there is no real table to point a Ref at.
#
#   The Ref's target column is resolved as the target table's own SOLE
#   primary-key column (scanned from the target's canonical `columns`, never
#   a `primary_key` field — canonical_schema's shape has no such field
#   anyway). A target with zero or 2+ primary columns (no PK, or a composite
#   PK) can't be expressed as a single-column Ref in this minimal version —
#   that relation is skipped, WITH a warning printed to stderr (never a hard
#   failure: a skip, not an error).
#
#   Column `default:` values are always rendered as either a bare number or a
#   single-quoted string — there is no SQL-expression / backtick-expr
#   special case. This is a deliberate simplification, not an oversight: a
#   default like `CURRENT_TIMESTAMP` renders as `default: 'CURRENT_TIMESTAMP'`
#   (a quoted string), which is syntactically valid DBML even though it isn't
#   the most idiomatic rendering of a SQL expression default.
#
# Pure and non-destructive: render_dbml/emit_dbml_document never mutate their
# input (canonical_schema already deep-copies; nothing here sorts in place).
# render_dbml may print `Warning: --emit-dbml: ...` lines to stderr as a side
# effect (the Ref-skip case above) — this matches how the rest of the
# pipeline already prints informational lines to stderr (cli.py's
# _run_pipeline/_finish).
# ---------------------------------------------------------------------------

_IDENT_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
_NUMERIC_DEFAULT_RE = re.compile(r'-?\d+(\.\d+)?')


def _ident(name):
    """A table/column/index-column name -> its DBML token: bare when it's a
    simple identifier, else double-quoted with internal `"` escaped."""
    if _IDENT_RE.fullmatch(name):
        return name
    return '"' + name.replace('"', '\\"') + '"'


def _sq(s):
    """Single-quote a string for DBML, escaping internal `'` as `\\'`. Used
    for everything EXCEPT the table Note's triple-quote path, which needs no
    escaping at all (see _render_table_note)."""
    return "'" + s.replace("'", "\\'") + "'"


def _dbml_default(val):
    """A column's `default:` attribute value: bare/unquoted when the raw
    string is purely numeric (`^-?\\d+(\\.\\d+)?$`), else single-quoted with
    `'` escaped and any embedded newline collapsed to a space (defensive —
    reuse digest.py's _oneline). No SQL-expression detection — see the module
    docstring's lossy-contract note on `default:`."""
    s = str(val)
    if _NUMERIC_DEFAULT_RE.fullmatch(s):
        return s
    return _sq(_oneline(s))


def _unique_single_columns(indexes):
    """`{column_name, ...}` for every canonical index that is both unique and
    single-column — the set a column attr's `unique` marker (attr #4) checks
    against. Intentionally NOT deduped against the indexes block: a
    single-column unique index appears both inline on its column and as a
    block entry, by design (see the module docstring / DBML_REVIEW_NOTES)."""
    return {ix['columns'][0] for ix in indexes
            if ix.get('unique') and len(ix.get('columns', [])) == 1}


def _column_attrs(c, is_sole_pk, unique_single_cols):
    """One column's DBML attrs, comma-joined-ready list, in the fixed order:
    pk (sole single-column PK only) / increment / not null / unique /
    default. Composite PKs never contribute a per-column `pk` here — they go
    in the table's indexes block instead (_composite_pk_columns)."""
    attrs = []
    if is_sole_pk:
        attrs.append('pk')
    if 'auto_increment' in (c.get('extra') or '').lower():
        attrs.append('increment')
    if not c.get('nullable', False):
        attrs.append('not null')
    if c['name'] in unique_single_cols:
        attrs.append('unique')
    default = c.get('default')
    if default:
        attrs.append(f'default: {_dbml_default(default)}')
    return attrs


def _render_dbml_column(c, sole_pk_name, unique_single_cols):
    """One canonical column -> its DBML line: `  <ident> <type>[ [attrs]]`.
    `type` prefers `sql_type` (raw DB type, fidelity) and falls back to the
    coarse `type` shorthand — printed bare/unquoted even when it contains an
    internal space (e.g. `int unsigned`), since DBML column types are bare
    tokens, not string literals. Named _render_dbml_column (not
    _render_column) to avoid colliding with digest.py's own _render_column —
    both files are concatenated into one flat module by build_single_file.py,
    so a same-named function here would silently shadow digest.py's."""
    ctype = c.get('sql_type') or c.get('type', '')
    attrs = _column_attrs(c, c['name'] == sole_pk_name, unique_single_cols)
    line = f"  {_ident(c['name'])} {ctype}"
    if attrs:
        line += ' [' + ', '.join(attrs) + ']'
    return line


def _composite_pk_columns(columns):
    """Column names individually flagged `primary: true`, in COLUMN ORDER —
    the SAME composite-PK detection rule emit.py's _config_table already
    established (P1/E-2): read from column flags, never from a `primary_key`
    field (canonical_schema's shape has no such field anyway). Returns the
    full list regardless of length; callers decide what 0/1/2+ means."""
    return [c['name'] for c in columns if c.get('primary')]


def _render_indexes_block(primary_cols, indexes):
    """The table's `indexes { ... }` sub-block as a list of lines (unindented
    at the `indexes {`/`}` level — the caller indents nothing further), or
    None when there's neither a composite PK (2+ primary columns) nor any
    index at all. The composite-PK line (if any) is always FIRST, then every
    entry of `indexes` in its already-canonical (not re-sorted) order."""
    if len(primary_cols) < 2 and not indexes:
        return None
    lines = ['  indexes {']
    if len(primary_cols) >= 2:
        cols = ', '.join(_ident(c) for c in primary_cols)
        lines.append(f'    ({cols}) [pk]')
    for ix in indexes:
        cols = ', '.join(_ident(c) for c in ix.get('columns', []))
        attrs = []
        if ix.get('unique'):
            attrs.append('unique')
        if ix.get('name'):
            attrs.append(f"name: {_sq(ix['name'])}")
        line = f'    ({cols})'
        if attrs:
            line += ' [' + ', '.join(attrs) + ']'
        lines.append(line)
    lines.append('  }')
    return lines


def _render_table_note(comment):
    """The table's `  Note: ...` line, or None when there's no comment.
    A single-line comment is single-quoted with `'` escaped
    (`  Note: 'text'`); a comment containing a newline instead uses DBML's
    triple-quote form (`  Note: '''text'''`, unescaped) — same reasoning
    emit.py's config_yaml_text already applied to multi-line note/comment
    text (literal block style there, triple-quote here), for the same
    "don't mangle a paragraph of free text" reason."""
    if not comment:
        return None
    if '\n' in comment:
        return f"  Note: '''{comment}'''"
    return f'  Note: {_sq(comment)}'


def _render_table(name, t):
    """One canonical table -> its full `Table ... { ... }` block, as a list
    of lines. Blank-line separators appear only before a section that's
    actually present: columns, then (blank +) indexes block if any, then
    (blank +) Note if any."""
    columns = t.get('columns', [])
    primary_cols = _composite_pk_columns(columns)
    sole_pk_name = primary_cols[0] if len(primary_cols) == 1 else None
    indexes = t.get('indexes', [])
    unique_single_cols = _unique_single_columns(indexes)

    lines = [f'Table {_ident(name)} {{']
    for c in columns:
        lines.append(_render_dbml_column(c, sole_pk_name, unique_single_cols))

    idx_block = _render_indexes_block(primary_cols, indexes)
    if idx_block:
        lines.append('')
        lines.extend(idx_block)

    note_line = _render_table_note(t.get('comment'))
    if note_line:
        lines.append('')
        lines.append(note_line)

    lines.append('}')
    return lines


def _collect_refs(tables):
    """Every `Ref:` tuple `(source_table, foreign_key, target_table,
    target_pk_column)` this schema resolves to, sorted by that same tuple for
    deterministic output. See the module docstring for the full lossy-
    contract reasoning; in short: only `belongs_to` + truthy `foreign_key`,
    never `has_one`/`has_many`/`has_and_belongs_to_many`; a polymorphic
    belongs_to is skipped silently; a target with no single-column PK is
    skipped with a stderr warning. Dedups defensively on
    (source_table, foreign_key, target_table), keeping only the first
    occurrence, should a duplicate somehow occur."""
    seen = set()
    kept = []
    for name in sorted(tables):
        t = tables[name]
        for a in t.get('associations', []):
            if a.get('type') != 'belongs_to' or not a.get('foreign_key'):
                continue
            if a.get('polymorphic'):
                continue  # synthetic tableless target — nothing real to reference
            target = a['target']
            fk = a['foreign_key']
            key = (name, fk, target)
            if key in seen:
                continue
            seen.add(key)
            target_table = tables[target]  # guaranteed present: canonical_schema
                                            # already pruned any dangling non-poly target
            target_primary = _composite_pk_columns(target_table.get('columns', []))
            if len(target_primary) != 1:
                print(f'Warning: --emit-dbml: skipping Ref {name}.{fk} -> {target} '
                      '(target has no single-column primary key)', file=sys.stderr)
                continue
            kept.append((name, fk, target, target_primary[0]))
    kept.sort()
    return kept


def render_dbml(schema):
    """Render a canonical `schema` (emit.py's canonical_schema shape:
    {tables, notes?, groups?}) to minimal DBML text. `notes`/`groups`, even
    when present, are never read — out of scope this round (see module
    docstring). Pure and deterministic: the same schema always renders the
    same text, since canonical_schema's tables/columns/indexes/associations
    are already canonically ordered and this function sorts tables by name
    itself (the one thing canonical_schema does not sort, since `tables` is a
    plain name-keyed dict) rather than relying on any caller-side ordering."""
    tables = schema.get('tables', {})
    names = sorted(tables)

    lines = []
    for i, name in enumerate(names):
        if i:
            lines.append('')
        lines.extend(_render_table(name, tables[name]))

    refs = _collect_refs(tables)
    if refs:
        lines.append('')
        for source, fk, target, target_pk in refs:
            lines.append(f'Ref: {_ident(source)}.{_ident(fk)} > {_ident(target)}.{_ident(target_pk)}')

    return '\n'.join(lines) + '\n'


def emit_dbml_document(tables, notes_data, groups_data):
    """Build the --emit-dbml document: project the final merged IR through
    the SAME canonical_schema --emit-json/--emit-config/--emit-digest already
    share, then render it. `notes_data`/`groups_data` are accepted (mirroring
    emit_digest_document's signature — cli.py already has both in hand at the
    same call site, and keeping all four emit-family builders' signatures
    uniform is simpler than special-casing this one) but intentionally
    unused: this export's scope stops at tables/columns/indexes/single-
    column-FK Refs/table comments (see module docstring)."""
    schema = canonical_schema(tables, notes_data, groups_data)
    return render_dbml(schema)
