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


# ---------------------------------------------------------------------------
# --- DBML INPUT — typed source `dbml` (backlog P4) --------------------------
#
# The reverse half of this file: a static, line-oriented DBML parser (NO
# external dbml-cli/library dependency — dependency-zero is a hard constraint,
# same reasoning as rails_schema.py never executing Ruby). Registers a
# `dbml.schema` was considered but the source-type name is simply `dbml`,
# since unlike Rails there's only one shape of DBML input.
#
# D-1 (DESIGN_ROADMAP.md): DBML input is authority kind='schema' — same rank
# as rails.schema (see merge.py's _PHYSICAL_RANK/_LOGICAL_RANK) — because a
# hand-written or dbdiagram.io-exported .dbml file is, like a schema.rb dump,
# a DECLARED PHYSICAL SCHEMA DOCUMENT: a live DB wins over it when both are
# present, but it outranks framework code's association guesses.
#
# SCOPE — what this parser supports:
#   * `Table name { ... }` (schema-qualified `schema.name` collapses to the
#     last segment; an `as alias` suffix is consumed and discarded — see the
#     alias gap note below; a trailing `[settings]` bracket, e.g.
#     `[headercolor: #3498DB]`, is consumed and discarded, matching
#     rails_schema.py's own precedent of silently ignoring cosmetic/
#     unmodeled create_table options).
#   * Columns: `name type [attrs]` — `pk`/`primary key`, `not null`, `unique`
#     (folded into a synthetic single-column unique index, deduped against
#     an explicit `indexes{}` entry for the same column — the two are the
#     SAME fact represented two ways in DBML, unlike dbml.py's EXPORT side
#     which deliberately shows both because canonical_schema only has one
#     source of truth to read from), `increment` (-> `extra: auto_increment`,
#     read back by `_column_attrs` above), `default: <literal>` (quoted
#     string / backtick expression kept verbatim / bare number / true|false;
#     an unparseable literal warns and is skipped, mirroring
#     rails_schema.py's dynamic-default handling), `note: '...'` (-> column
#     comment), and inline `ref: SYMBOL table.col` (folded into the same
#     Ref-resolution pass as standalone Refs below).
#   * `indexes { ... }` sub-block: `(a, b)` or bare `col`, with `[unique]`/
#     `[name: '...']`. A `[pk]` entry (single- or multi-column) contributes
#     to the table's primary_key candidate list rather than becoming a real
#     index entry — this mirrors dbml.py's OWN EXPORT, which synthesizes
#     exactly this line from column `primary` flags rather than reading it
#     from a real index (`_render_indexes_block`). The primary_key winner is
#     a bare string for a single column, a list for 2+ (§7.3) — merge.py's
#     `_normalize_primary_key` post-merge pass is what actually stamps
#     `primary: True` onto the named columns; this parser never sets that
#     flag directly on a column itself (same division of labor
#     rails_schema.py already established for its own composite-PK case).
#   * `Ref: table.col SYMBOL table.col` (optionally named, `Ref fk_x: ...`)
#     standalone, AND the block form `Ref { table.col SYMBOL table.col ... }`
#     for declaring several at once. All four DBML symbols map onto
#     erdscope's association vocabulary: `>` (many left, one right) and `<`
#     (its mirror image) both become `belongs_to` on whichever side is
#     "many" (the side with the FK column), with `foreign_key` = that
#     column; `-` (one-to-one) becomes `has_one` on the LEFT (FK-column)
#     side — safe here specifically because WE are the ones asserting that
#     side owns the column (contrast dbml.py's EXPORT-side docstring on why
#     a *pre-existing* has_one's `foreign_key` is provider-ambiguous: that
#     concern is about interpreting someone else's already-written has_one,
#     not synthesizing a fresh one from an unambiguous column reference);
#     `<>` (many-to-many) becomes `has_and_belongs_to_many` on the left,
#     `foreign_key` omitted (a plain column-pair Ref doesn't carry enough
#     information for one). Every Ref-derived association is marked
#     `schema_fk: True` — the SAME legacy flag rails_schema.py's own FK scan
#     uses — for a consistent representative-provenance ranking (§9.1) with
#     a live DB's `db_fk` (higher) and framework code's declared
#     associations. A composite Ref (`table.(a, b) > ...`) is out of scope,
#     matching --emit-dbml's own single-column-only Ref contract, and warns
#     rather than silently dropping.
#   * `Enum name { ... }` is recognized and consumed with NO warning — a
#     column typed as an enum name just keeps that name as its raw
#     `type`/`sql_type` string verbatim; there is nowhere in erdscope's IR to
#     hang the enum's member list, so the block's body is intentionally inert
#     (this is a deliberately-silent no-op, not an oversight — contrast the
#     warn-and-skip constructs below, where real information is discarded).
#   * `Project name { ... }` is recognized and consumed with NO warning —
#     nothing downstream reads a source-supplied title suggestion today (see
#     cli.py's `_resolve_title` precedence chain, which providers never
#     participate in), so there is nothing useful to do with it yet.
#   * Table-level `Note: '...'` / `Note: '''...'''` (the latter possibly
#     spanning several physical lines) -> table `comment`, the exact reverse
#     of `_render_table_note` above.
#
# OUT OF SCOPE this round (recognized but WARN-and-skip — never silently
# dropped, matching rails_schema.py's "unknown construct" philosophy):
#   * `TableGroup name { ... }` -> groups. Wiring a provider's own notes/
#     groups contributions into the notes/groups Phase-1 system (which today
#     only ever reads config's top-level `notes:`/`groups:` keys — see
#     cli.py's `_finish`) is a real pipeline extension, not a parsing
#     exercise, and is deferred — symmetric with dbml.py's EXPORT half
#     above, which likewise never emits `TableGroup`/a standalone `Note`
#     object (see that section's own module-docstring: "notes/groups... a
#     later 'extension' phase"). Do not "complete" one side without the
#     other; they were deferred together for the same reason.
#   * A standalone `Note name { ... }` object (DBML's free-floating
#     documentation block, distinct from a Table's own inline `Note:`) —
#     same reasoning as TableGroup above.
#   * A composite Ref (multi-column FK) — see above.
#   * Table aliases (`Table foo as f`) are consumed but NOT tracked, so a
#     Ref written against the alias (`Ref: f.x > ...`) rather than the real
#     table name will fail to resolve (warns "references unknown table
#     'f'"). Real-world hand-written DBML overwhelmingly refers to Refs by
#     the table's real name, not its alias; this is a known, narrow gap.
#   * `/* ... */` block comments are not stripped (only line-trailing `//`
#     is) — a block comment spanning several lines may produce warnings on
#     the lines it covers. `TablePartial`/`~mixin` column templates are not
#     recognized at all (fall through to "unknown top-level construct").
#   * ONE STATEMENT PER PHYSICAL LINE, throughout — same assumption
#     rails_schema.py makes for schema.rb. A block that opens AND closes on
#     the same physical line (`Table t { id integer [pk] }` all on one line)
#     is NOT recognized as that block's header at all; it falls through to
#     "unknown statement" like any other unrecognized line. Every real
#     dbdiagram.io export, and essentially all hand-written DBML in the
#     wild, already uses one-statement-per-line formatting, so this is a
#     negligible practical gap, not a silent-data-loss risk (a compacted
#     file still warns loudly rather than parsing wrongly).
#
# Never silently drop: every unrecognized top-level construct (block or bare
# line) and every unparseable statement inside a Table/indexes/Ref block
# warns with `path:line: ...` and is skipped, never causing the whole parse
# to fail — mirroring rails_schema_provider's `warn` closure exactly. A file
# with zero `Table` blocks parses to an empty `tables` dict, which
# sources.py's typed-dispatch layer turns into the standard "found nothing to
# parse" hard error (same as every other typed source), never a silent
# empty success.
# ---------------------------------------------------------------------------

_DBML_TABLE_NOTE_LINE_RE = re.compile(r"^[Nn]ote:\s*(?P<rest>.+)$")
_DBML_REF_LINE_RE = re.compile(r'^[Rr]ef(?:\s+\S+)?\s*:\s*(?P<body>.+)$')
_DBML_REF_BODY_RE = re.compile(
    r'^(?P<lt>"[^"]+"|[A-Za-z_][\w.]*)\.(?P<lc>\([^)]*\)|"[^"]+"|[A-Za-z_]\w*)'
    r'\s*(?P<sym><>|>|<|-)\s*'
    r'(?P<rt>"[^"]+"|[A-Za-z_][\w.]*)\.(?P<rc>\([^)]*\)|"[^"]+"|[A-Za-z_]\w*)$')
_DBML_INDEXES_HEADER_RE = re.compile(r'^[Ii]ndexes\s*\{$')
_DBML_DEFAULT_NUM_RE = re.compile(r'-?\d+(\.\d+)?')


def _dbml_strip_line_comment(line):
    """Cut `line` at its first top-level `//` — quote-aware (both `'` and
    `"` delimit strings/identifiers in DBML) so a `//` inside one survives.
    Mirrors rails_schema.py's `_strip_comment`, adapted to DBML's quoting
    rules (Ruby only strings with `#`; DBML has no `#`-comment at all, only
    `//`, but both single AND double quotes need protecting here)."""
    quote, i, n = None, 0, len(line)
    while i < n:
        c = line[i]
        if quote:
            if c == '\\' and i + 1 < n:
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in '\'"':
            quote = c
            i += 1
            continue
        if c == '/' and i + 1 < n and line[i + 1] == '/':
            return line[:i]
        i += 1
    return line


def _dbml_ident(tok):
    """A column/index-column/Ref-side identifier token -> its bare string:
    double-quoted forms are unwrapped and unescaped, everything else is
    returned as-is (already bare)."""
    tok = tok.strip()
    if len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"':
        return tok[1:-1].replace('\\"', '"')
    return tok


def _dbml_table_name(tok):
    """A Table-header or Ref-side table-name token -> its resolved name:
    double-quoted forms are unwrapped/unescaped (kept whole, dots and all —
    a quoted name is never schema-qualified); a bare token is split on its
    LAST `.` and only the final segment kept (`public.users` -> `users`) —
    DBML's schema-qualification has no equivalent slot in erdscope's flat,
    single-namespace table dict, so it is deliberately collapsed away rather
    than warned about (the same non-issue --models/config inputs already
    have, since none of them are schema-qualified either)."""
    tok = tok.strip()
    if len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"':
        return tok[1:-1].replace('\\"', '"')
    return tok.rsplit('.', 1)[-1]


def _dbml_strip_trailing_settings(s):
    """Remove a trailing `[...]` settings bracket (and the whitespace before
    it) from a block-header line already known to end with `{` (with that
    `{` itself already sliced off by the caller) — used for Table/Ref/Enum/
    Project/TableGroup/Note headers alike, all of which allow an optional
    trailing settings bracket DBML doesn't require this parser to model."""
    s = s.rstrip()
    if s.endswith(']'):
        idx = s.rfind('[')
        if idx != -1:
            return s[:idx].rstrip()
    return s


def _dbml_parse_table_header(rest):
    """A Table header's content after the `Table` keyword, with any trailing
    `[settings]` already stripped by the caller — handles an optional
    trailing `as alias` (consumed and discarded; see module docstring's
    alias-gap note) and returns the resolved table name."""
    rest = rest.strip()
    m = re.match(r'^(?P<name>"[^"]+"|\S+)\s+as\s+(?:"[^"]+"|\S+)$', rest, re.I)
    name_tok = m.group('name') if m else rest
    return _dbml_table_name(name_tok)


def _dbml_find_block_end(lines, start):
    """`lines[start]` is a header line already known to end with an
    unmatched `{` (depth 1 immediately after it). Returns the index of the
    line that closes it back to depth 0, counting brace characters OUTSIDE
    quotes only (comments are stripped per-line first) — used to skip a
    recognized-but-out-of-scope or genuinely unknown block wholesale without
    misreading its body as top-level statements. Returns the last line index
    if the file ends before the block closes (best-effort; the caller
    doesn't separately warn about this — an unclosed block reaching EOF
    naturally stops any further (mis)parsing)."""
    depth = 1
    i = start + 1
    n = len(lines)
    while i < n:
        quote = None
        for c in _dbml_strip_line_comment(lines[i]):
            if quote:
                if c == quote:
                    quote = None
                continue
            if c in '\'"':
                quote = c
            elif c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return n - 1


def _dbml_read_note_value(lines, i, rest):
    """`lines[i]`'s `Note:` line remainder (comment-stripped, NOT yet
    stripped of surrounding whitespace) -> `(note_text_or_None, last_index)`.
    `last_index` is the index of the LAST physical line this statement
    consumed, so the caller resumes at `last_index + 1`. Supports the
    single-quoted single-line form (`Note: 'text'`, `\\'` unescaped) and the
    triple-single-quoted form (`Note: '''text'''`), which may span several
    physical lines exactly like `_render_table_note` above emits for a
    multi-line comment — the join uses `\\n`, the exact reverse of that
    function's own join. Lines inside a triple-quoted note are read RAW (not
    comment-stripped — `//` is valid note prose, not a comment, once inside
    the quotes)."""
    rest = rest.strip()
    if rest.startswith("'''"):
        body = rest[3:]
        end = body.find("'''")
        if end != -1:
            return body[:end], i
        chunks = [body]
        j = i + 1
        while j < len(lines):
            line = lines[j]
            end = line.find("'''")
            if end != -1:
                chunks.append(line[:end])
                return '\n'.join(chunks), j
            chunks.append(line)
            j += 1
        return '\n'.join(chunks), j - 1
    if len(rest) >= 2 and rest[0] == "'" and rest[-1] == "'":
        return rest[1:-1].replace("\\'", "'"), i
    return None, i


def _dbml_column_types(type_tok):
    """A column's declared type token -> `(coarse_type, sql_type)`. Mirrors
    rails_schema.py's own `SQL_TYPES.get(rails_type, rails_type)` pattern:
    `sql_type` keeps the ORIGINAL token verbatim (case, `(length)` suffix and
    all — e.g. `Varchar(255)`), while the coarse `type` looks up just the
    parenthesis-stripped, lower-cased base name in the shared `SQL_TYPES`
    table, falling back to the verbatim token itself (not the lower-cased
    base) when the type isn't a recognized SQL type name — the common case
    for an Enum name or a custom/domain type, where showing the real
    declared name beats a silently-lowered guess."""
    base = re.sub(r'\(.*\)\s*$', '', type_tok).strip().lower()
    return SQL_TYPES.get(base, type_tok), type_tok


def _dbml_parse_default_literal(tok):
    """One `default:` attr's value token -> its stored string form, `None`
    (`null` — no default), or the shared `_DYNAMIC` sentinel (rails_schema.py
    style) for anything unparseable — the caller warns and skips in that
    case. A single-quoted string is unescaped; a backtick expression
    (`` `now()` ``) is kept verbatim (no attempt to interpret it, matching
    --emit-dbml's own "no SQL-expression special case" simplification on the
    export side); a bare integer/float or `true`/`false` passes through
    as-is (canonical `default` is always a plain string — see emit.py)."""
    tok = tok.strip()
    if not tok or tok.lower() == 'null':
        return None
    if len(tok) >= 2 and tok[0] == "'" and tok[-1] == "'":
        return tok[1:-1].replace("\\'", "'")
    if len(tok) >= 2 and tok[0] == '`' and tok[-1] == '`':
        return tok[1:-1]
    if _DBML_DEFAULT_NUM_RE.fullmatch(tok):
        return tok
    if tok.lower() in ('true', 'false'):
        return tok.lower()
    return _DYNAMIC


def _dbml_parse_column_line(line):
    """One Table-body line, already known not to be a `Note:`/`indexes{`
    line -> `(name, type_token, attrs_str_or_None)`, or `None` if it doesn't
    even have the shape `name type[ [attrs]]` — the caller then warns and
    skips the whole line rather than guessing further."""
    s = line.strip()
    attrs = None
    if s.endswith(']'):
        idx = s.rfind('[')
        if idx == -1:
            return None
        attrs = s[idx + 1:-1]
        s = s[:idx].strip()
    if not s:
        return None
    if s.startswith('"'):
        end = s.find('"', 1)
        if end == -1:
            return None
        name = s[1:end].replace('\\"', '"')
        rest = s[end + 1:].strip()
    else:
        m = re.match(r'^([A-Za-z_]\w*)\s+(.+)$', s)
        if not m:
            return None
        name, rest = m.group(1), m.group(2).strip()
    if not rest:
        return None
    if len(rest) >= 2 and rest[0] == '"' and rest[-1] == '"':
        type_tok = rest[1:-1].replace('\\"', '"')
    else:
        type_tok = rest
    return name, type_tok, attrs


def _dbml_apply_column_attrs(col, attrs_str, table_name, line_no, warn, pending_refs):
    """Mutates `col` (already carrying name/type/sql_type/nullable=True) per
    the comma-split tokens inside a column's trailing `[...]` — see the
    module docstring's column-attrs bullet for the full attr list. Returns
    True when a `pk`/`primary key` flag was present; the caller folds that
    into the table's primary_key candidate list rather than setting
    `col['primary']` directly (merge.py's `_normalize_primary_key` is what
    actually stamps that flag post-merge — see module docstring). An inline
    `ref:` attr appends a pending-ref dict (same shape a standalone/block Ref
    produces) to `pending_refs`, resolved in the same end-of-file pass."""
    is_pk = False
    if attrs_str is None:
        return is_pk
    for tok in _split_top_level(attrs_str):
        t = tok.strip()
        if not t:
            continue
        low = t.lower()
        if low in ('pk', 'primary key'):
            is_pk = True
        elif low == 'not null':
            col['nullable'] = False
        elif low == 'null':
            pass
        elif low == 'unique':
            col['_dbml_unique'] = True
        elif low in ('increment', 'auto_increment', 'autoincrement'):
            col['extra'] = 'auto_increment'
        elif low.startswith('default') and ':' in t:
            val = _dbml_parse_default_literal(t.split(':', 1)[1])
            if val is _DYNAMIC:
                warn(line_no, f"unparseable default ({table_name}.{col['name']}) "
                              '— attribute skipped')
            elif val is not None:
                col['default'] = val
        elif low.startswith('note') and ':' in t:
            note_tok = t.split(':', 1)[1].strip()
            if len(note_tok) >= 2 and note_tok[0] == "'" and note_tok[-1] == "'":
                col['comment'] = note_tok[1:-1].replace("\\'", "'")
        elif low.startswith('ref') and ':' in t:
            m = re.match(r"^(?P<sym><>|>|<|-)\s*"
                        r'(?P<rt>"[^"]+"|[A-Za-z_][\w.]*)\.(?P<rc>"[^"]+"|[A-Za-z_]\w*)$',
                        t.split(':', 1)[1].strip())
            if not m:
                warn(line_no, f"unparseable inline ref ({table_name}.{col['name']}) — skipped")
                continue
            pending_refs.append({'line': line_no, 'lt': table_name, 'lc': col['name'],
                                 'sym': m.group('sym'), 'rt': _dbml_table_name(m.group('rt')),
                                 'rc': m.group('rc')})
        else:
            warn(line_no, f"unknown column setting {t!r} ({table_name}.{col['name']}) — ignored")
    return is_pk


def _dbml_parse_index_line(line):
    """One `indexes{}` body line -> `(columns_list, attrs_str_or_None)`, or
    `None` if unparseable. Accepts both the composite `(a, b)` form and a
    bare single-column form (`col_name`), each with an optional trailing
    `[attrs]`."""
    s = line.strip()
    attrs = None
    if s.endswith(']'):
        idx = s.rfind('[')
        if idx == -1:
            return None
        attrs = s[idx + 1:-1]
        s = s[:idx].strip()
    if not s:
        return None
    if s.startswith('(') and s.endswith(')'):
        cols = [_dbml_ident(c) for c in _split_top_level(s[1:-1])]
    else:
        cols = [_dbml_ident(s)]
    if not cols or any(not c for c in cols):
        return None
    return cols, attrs


def _dbml_index_attrs(attrs_str, line_no, warn, label):
    """An indexes{}-entry's `[...]` attrs -> `(unique, name_or_None,
    is_pk)`. `type: btree|hash` and `note: '...'` are recognized-but-inert
    (no erdscope equivalent); anything else unrecognized warns."""
    unique, name, is_pk = False, None, False
    if attrs_str is None:
        return unique, name, is_pk
    for tok in _split_top_level(attrs_str):
        t = tok.strip()
        if not t:
            continue
        low = t.lower()
        if low in ('pk', 'primary key'):
            is_pk = True
        elif low == 'unique':
            unique = True
        elif low.startswith('name') and ':' in t:
            val = t.split(':', 1)[1].strip()
            if len(val) >= 2 and val[0] == "'" and val[-1] == "'":
                name = val[1:-1].replace("\\'", "'")
        elif low.startswith('type') or low.startswith('note'):
            pass
        else:
            warn(line_no, f'unknown index setting {t!r} ({label}) — ignored')
    return unique, name, is_pk


def _dbml_parse_table_block(lines, start, name, warn):
    """Parse a Table body starting at `lines[start]` (the first body line)
    through its closing `}` (exclusive) — returns `(table_fragment, end,
    pending_refs, closed)`: `end` is the index of the closing `}` (or the
    last line index if never closed), `pending_refs` are this table's inline
    column `ref:` attrs (bubbled up for the caller's shared end-of-file
    resolution pass), `closed` is False when EOF was reached first (the
    caller discards the whole table in that case, warning once — same
    "unterminated block" handling rails_schema_provider uses for an
    unterminated create_table)."""
    table = {'columns': [], 'indexes': [], 'associations': [], 'primary_key': None}
    pk_candidates = []
    pending_refs = []
    i, n = start, len(lines)
    in_indexes = False
    closed = False
    while i < n:
        stripped = _dbml_strip_line_comment(lines[i]).strip()
        if not stripped:
            i += 1
            continue
        if stripped == '}':
            if in_indexes:
                in_indexes = False
                i += 1
                continue
            closed = True
            break
        if in_indexes:
            parsed = _dbml_parse_index_line(stripped)
            if parsed is None:
                warn(i + 1, f'unparseable index entry {stripped!r} — skipped')
                i += 1
                continue
            cols, attrs = parsed
            unique, ix_name, is_pk = _dbml_index_attrs(
                attrs, i + 1, warn, f'{name} index ({", ".join(cols)})')
            if is_pk:
                for c in cols:
                    if c not in pk_candidates:
                        pk_candidates.append(c)
            else:
                ix = {'columns': cols, 'unique': unique}
                if ix_name:
                    ix['name'] = ix_name
                table['indexes'].append(ix)
            i += 1
            continue
        if _DBML_INDEXES_HEADER_RE.match(stripped):
            in_indexes = True
            i += 1
            continue
        m = _DBML_TABLE_NOTE_LINE_RE.match(stripped)
        if m:
            note_text, i = _dbml_read_note_value(lines, i, m.group('rest'))
            if note_text is not None:
                table['comment'] = note_text
            i += 1
            continue
        parsed_col = _dbml_parse_column_line(stripped)
        if parsed_col is None:
            warn(i + 1, f'unknown table statement {stripped!r} — skipped')
            i += 1
            continue
        cname, type_tok, attrs = parsed_col
        cname = _dbml_ident(cname)
        coarse, sql_type = _dbml_column_types(type_tok)
        col = {'name': cname, 'type': coarse, 'sql_type': sql_type, 'nullable': True}
        is_pk = _dbml_apply_column_attrs(col, attrs, name, i + 1, warn, pending_refs)
        if is_pk and cname not in pk_candidates:
            pk_candidates.append(cname)
        table['columns'].append(col)
        i += 1
    if not closed:
        warn(n, f'unterminated Table {name!r} block at end of file — skipped')
    existing_single_cols = {ix['columns'][0] for ix in table['indexes']
                            if len(ix['columns']) == 1}
    for col in table['columns']:
        if col.pop('_dbml_unique', False) and col['name'] not in existing_single_cols:
            table['indexes'].append({'columns': [col['name']], 'unique': True})
            existing_single_cols.add(col['name'])
    if pk_candidates:
        table['primary_key'] = pk_candidates[0] if len(pk_candidates) == 1 else pk_candidates
        # A primary key is never actually nullable in any real engine, regardless
        # of whether `not null` was also spelled out explicitly (dbdiagram.io
        # exports usually do; plenty of hand-written DBML doesn't bother) — forced
        # here rather than left to merge.py's _normalize_primary_key, which only
        # ever sets `primary`, never touches `nullable` (see its own docstring).
        pk_set = set(pk_candidates)
        for c in table['columns']:
            if c['name'] in pk_set:
                c['nullable'] = False
    return table, i, pending_refs, closed


def _dbml_parse_ref_body(body):
    """A Ref statement's right-hand side (the part after any leading
    `Ref:`/`Ref name:` has already been stripped by the caller):
    `table.col SYMBOL table.col` -> `{lt, lc, sym, rt, rc}` with both table
    names resolved (`_dbml_table_name`) but `lc`/`rc` left as RAW tokens —
    a `(...)`-wrapped one signals a composite reference the caller must
    reject (see module docstring: composite FKs are out of scope, matching
    --emit-dbml's own single-column-only contract). `None` if the body
    doesn't parse at all."""
    m = _DBML_REF_BODY_RE.match(body.strip())
    if not m:
        return None
    return {'lt': _dbml_table_name(m.group('lt')), 'lc': m.group('lc'),
           'sym': m.group('sym'), 'rt': _dbml_table_name(m.group('rt')),
           'rc': m.group('rc')}


def _dbml_resolve_pending_ref(pr, tables, warn):
    """One collected pending ref (from a standalone/block Ref statement OR
    an inline column `ref:` attr — same shape either way) -> materialized
    directly onto `tables[declaring]['associations']`, or a warning if it
    can't be (composite columns, an unknown table, or a named column that
    doesn't exist on its table). See the module docstring's Ref-mapping
    bullet for the symbol -> association-type table."""
    line_no = pr['line']
    lt, lc, sym, rt, rc = pr['lt'], pr['lc'], pr['sym'], pr['rt'], pr['rc']
    if lc.startswith('(') or rc.startswith('('):
        warn(line_no, f'composite Ref {lt}.{lc} {sym} {rt}.{rc} — composite foreign '
                      'keys are not supported, skipped')
        return
    lc, rc = _dbml_ident(lc), _dbml_ident(rc)
    if lt not in tables:
        warn(line_no, f'Ref references unknown table {lt!r} — skipped')
        return
    if rt not in tables:
        warn(line_no, f'Ref references unknown table {rt!r} — skipped')
        return
    if not any(c['name'] == lc for c in tables[lt]['columns']):
        warn(line_no, f'Ref names column {lt}.{lc} which does not exist — skipped')
        return
    if not any(c['name'] == rc for c in tables[rt]['columns']):
        warn(line_no, f'Ref names column {rt}.{rc} which does not exist — skipped')
        return
    if sym == '>':
        decl, target, fk, atype = lt, rt, lc, 'belongs_to'
    elif sym == '<':
        decl, target, fk, atype = rt, lt, rc, 'belongs_to'
    elif sym == '-':
        decl, target, fk, atype = lt, rt, lc, 'has_one'
    else:  # '<>'
        decl, target, fk, atype = lt, rt, lc, 'has_and_belongs_to_many'
    name = fk[:-3] if fk.endswith('_id') else fk
    tables[decl].setdefault('associations', []).append(
        {'type': atype, 'name': name, 'target': target, 'foreign_key': fk, 'schema_fk': True})


def dbml_provider(path, given=None):
    """ProviderResult (kind='schema', provider='dbml') for a .dbml file:
    tables/columns/indexes/primary keys/table comments/Enum-typed columns,
    plus Ref-derived associations — parsed by pure text analysis, no DBML
    library dependency (D7-style: dependency-zero is non-negotiable). See
    the module docstring above for the full supported/out-of-scope grammar.

    `path` is always the resolved Path actually read from disk; `given` is
    the (possibly relative, possibly unresolved) path string to DISPLAY in
    warnings/location — sources.py passes the user's own spelling, exactly
    like rails_schema_provider. Defaults to `str(path)` for callers (tests,
    direct use) that don't distinguish the two."""
    display = given if given is not None else str(path)
    text = path.read_text(encoding='utf-8', errors='replace')
    lines = text.splitlines()
    n = len(lines)
    warnings = []

    def warn(line_no, msg):
        warnings.append(f'{display}:{line_no}: {msg}')

    tables = {}
    pending_refs = []
    i = 0
    while i < n:
        stripped = _dbml_strip_line_comment(lines[i]).strip()
        if not stripped:
            i += 1
            continue
        if stripped.endswith('{'):
            head = _dbml_strip_trailing_settings(stripped[:-1]).strip()
            m = re.match(r'^[Tt]able\s+(?P<rest>.+)$', head)
            if m:
                tname = _dbml_parse_table_header(m.group('rest'))
                table, end, refs, closed = _dbml_parse_table_block(lines, i + 1, tname, warn)
                pending_refs.extend(refs)
                if closed:
                    if tname in tables:
                        warn(i + 1, f'duplicate Table {tname!r} — later definition ignored')
                    else:
                        tables[tname] = table
                i = end + 1
                continue
            if re.match(r'^[Rr]ef(?:\s+[A-Za-z_]\w*)?$', head, re.I):
                end = _dbml_find_block_end(lines, i)
                for j in range(i + 1, end):
                    inner = _dbml_strip_line_comment(lines[j]).strip()
                    if not inner:
                        continue
                    if inner.endswith(']'):
                        idx = inner.rfind('[')
                        if idx != -1:
                            inner = inner[:idx].strip()
                    parsed = _dbml_parse_ref_body(inner)
                    if parsed is None:
                        warn(j + 1, f'unparseable Ref entry {inner!r} — skipped')
                        continue
                    parsed['line'] = j + 1
                    pending_refs.append(parsed)
                i = end + 1
                continue
            if re.match(r'^[Ee]num\s+\S+$', head, re.I):
                i = _dbml_find_block_end(lines, i) + 1
                continue
            if re.match(r'^[Pp]roject\s+\S+$', head, re.I):
                i = _dbml_find_block_end(lines, i) + 1
                continue
            m = re.match(r'^[Tt]ableGroup\s+(?P<name>\S+)$', head, re.I)
            if m:
                warn(i + 1, f"TableGroup {_dbml_ident(m.group('name'))!r} is not "
                            'supported as input yet — skipped')
                i = _dbml_find_block_end(lines, i) + 1
                continue
            m = re.match(r'^[Nn]ote\s+(?P<name>\S+)$', head, re.I)
            if m:
                warn(i + 1, f"standalone Note block {_dbml_ident(m.group('name'))!r} is "
                            'not supported as input yet — skipped')
                i = _dbml_find_block_end(lines, i) + 1
                continue
            warn(i + 1, f'unknown top-level construct {stripped!r} — skipped')
            i = _dbml_find_block_end(lines, i) + 1
            continue
        m = _DBML_REF_LINE_RE.match(stripped)
        if m:
            parsed = _dbml_parse_ref_body(m.group('body'))
            if parsed is None:
                warn(i + 1, f'unparseable Ref statement {stripped!r} — skipped')
            else:
                parsed['line'] = i + 1
                pending_refs.append(parsed)
            i += 1
            continue
        warn(i + 1, f'unknown statement {stripped!r} — skipped')
        i += 1

    for pr in pending_refs:
        _dbml_resolve_pending_ref(pr, tables, warn)

    return make_provider_result('schema', 'dbml', tables, location=display, warnings=warnings)
