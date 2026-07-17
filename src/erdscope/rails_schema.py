# ---------------------------------------------------------------------------
# Rails db/schema.rb parser — static text analysis, NO Ruby execution (D7).
#
# schema.rb is Rails' generated, canonical dump of the live database as Ruby
# DSL calls (create_table/t.<type>/add_foreign_key/...). It is always
# machine-generated in a narrow, predictable shape (one statement per
# physical line), so a line-oriented state machine — outside / inside a
# `create_table ... do |t|` block — plus a small Ruby-literal parser for each
# statement's argument list is enough to read it faithfully without ever
# `eval`-ing Ruby. Anything the parser can't identify or parse as a literal
# is a warning, never a silent drop (§ "NEVER silently drop an unrecognized
# construct").
# ---------------------------------------------------------------------------
_SCHEMA_COLUMN_TYPES = {
    'string', 'text', 'integer', 'bigint', 'float', 'decimal', 'numeric',
    'datetime', 'timestamp', 'time', 'date', 'boolean', 'binary', 'blob',
    'json', 'jsonb', 'uuid', 'inet',
}
_COLUMN_RECOGNIZED_OPTS = {'null', 'default', 'comment', 'limit', 'precision', 'scale'}
_CREATE_TABLE_IGNORED_OPTS = {'force', 'charset', 'collation', 'options', 'if_not_exists'}
_CREATE_TABLE_RECOGNIZED_OPTS = {'id', 'primary_key', 'comment'} | _CREATE_TABLE_IGNORED_OPTS
_REFERENCE_RECOGNIZED_OPTS = {'type', 'null', 'polymorphic', 'index', 'foreign_key'}
_INDEX_RECOGNIZED_OPTS = {'name', 'unique'}
_ADD_FK_IGNORED_OPTS = {'primary_key', 'name', 'on_delete', 'on_update', 'validate', 'deferrable'}
_ADD_FK_RECOGNIZED_OPTS = {'column'} | _ADD_FK_IGNORED_OPTS

_SCHEMA_HEADER_RE = re.compile(r'^ActiveRecord::Schema(\[[^\]]*\])?\.define\(.*\)\s*do$')
_CREATE_TABLE_RE = re.compile(r'^create_table\s+(?P<args>.+?)\s+do\s*\|\s*t\s*\|$')
_T_CALL_RE = re.compile(r'^t\.(?P<method>\w+)\b\s*(?P<args>.*)$')
_ADD_INDEX_RE = re.compile(r'^add_index\s+(?P<args>.+)$')
_ADD_FK_RE = re.compile(r'^add_foreign_key\s+(?P<args>.+)$')
_IGNORED_TOP_LEVEL_RE = re.compile(r'^(enable_extension|create_schema)\b')
_OPT_KEY_RE = re.compile(r'^([A-Za-z_]\w*):\s*(.*)$', re.S)

_DYNAMIC = object()  # sentinel: this token couldn't be parsed as a Ruby literal

# plural -> singular, for the FK-column-name inflector below (the inverse of
# frameworks/base.py's IRREGULAR, which is singular -> plural)
_IRREGULAR_SINGULAR = {plural: singular for singular, plural in IRREGULAR.items()}


def _singularize(word):
    """Minimal singularizer, used ONLY to compute add_foreign_key's default
    column name (`<to_table singular>_id` — there is no general singularize
    helper elsewhere in the codebase, pluralize() only goes one way)."""
    if not word:
        return word
    if word in _IRREGULAR_SINGULAR:
        return _IRREGULAR_SINGULAR[word]
    if word.endswith('ies'):
        return word[:-3] + 'y'
    if re.search(r'(s|x|z|ch|sh)es$', word):
        return word[:-2]
    if word.endswith('s'):
        return word[:-1]
    return word


# ---------------------------------------------------------------------------
# Ruby literal / call-argument parsing (D7's "Ruby literal parsing helper")
# ---------------------------------------------------------------------------
def _strip_comment(line):
    """Cut a line at its first `#` that is outside a quoted string — quote-
    aware so a comment/default string containing '#' (e.g. comment: "a #tag")
    is never truncated."""
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
        if c == '#':
            return line[:i]
        i += 1
    return line


def _split_top_level(s):
    """Split `s` on commas at bracket/paren/brace depth 0 and outside quotes —
    shared by call-argument lists, `[...]` arrays, and `{...}` hashes."""
    parts, depth, cur, quote, i, n = [], 0, [], None, 0, len(s)
    while i < n:
        c = s[i]
        if quote:
            cur.append(c)
            if c == '\\' and i + 1 < n:
                cur.append(s[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in '\'"':
            quote = c
            cur.append(c)
            i += 1
            continue
        if c in '([{':
            depth += 1
            cur.append(c)
            i += 1
            continue
        if c in ')]}':
            depth -= 1
            cur.append(c)
            i += 1
            continue
        if c == ',' and depth == 0:
            parts.append(''.join(cur).strip())
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    tail = ''.join(cur).strip()
    if tail:
        parts.append(tail)
    return parts


def _unescape_ruby_string(inner, quote):
    """Undo Ruby string escaping for the quote character, backslash, \\n, \\t
    — the escapes realistically found in a generated schema.rb's string
    literals (table/column names, comments, string defaults)."""
    out, i, n = [], 0, len(inner)
    while i < n:
        c = inner[i]
        if c == '\\' and i + 1 < n:
            nc = inner[i + 1]
            if nc in (quote, '\\'):
                out.append(nc)
            elif nc == 'n':
                out.append('\n')
            elif nc == 't':
                out.append('\t')
            else:
                out.append(nc)
            i += 2
            continue
        out.append(c)
        i += 1
    return ''.join(out)


def _parse_ruby_value(tok):
    """Parse one Ruby literal token: string, symbol, int, float, true/false/
    nil, a `[...]` array of the same, or a `{...}` hash of known option
    shapes (D7). Returns `_DYNAMIC` for anything else (a method call, a
    lambda, an interpolated string, ...) — the caller decides what to skip."""
    tok = tok.strip()
    if not tok:
        return _DYNAMIC
    if tok in ('true', 'false'):
        return tok == 'true'
    if tok == 'nil':
        return None
    if len(tok) >= 2 and tok[0] in '\'"' and tok[-1] == tok[0]:
        return _unescape_ruby_string(tok[1:-1], tok[0])
    if tok.startswith(':'):
        rest = tok[1:]
        if len(rest) >= 2 and rest[0] in '\'"' and rest[-1] == rest[0]:
            return _unescape_ruby_string(rest[1:-1], rest[0])
        if re.fullmatch(r'\w+', rest):
            return rest
        return _DYNAMIC
    if re.fullmatch(r'-?\d+', tok):
        return int(tok)
    if re.fullmatch(r'-?\d+\.\d+', tok):
        return float(tok)
    if tok.startswith('[') and tok.endswith(']'):
        vals = []
        for it in _split_top_level(tok[1:-1]):
            v = _parse_ruby_value(it)
            if v is _DYNAMIC:
                return _DYNAMIC
            vals.append(v)
        return vals
    if tok.startswith('{') and tok.endswith('}'):
        out = {}
        for it in _split_top_level(tok[1:-1]):
            m = _OPT_KEY_RE.match(it)
            if not m:
                return _DYNAMIC
            v = _parse_ruby_value(m.group(2))
            if v is _DYNAMIC:
                return _DYNAMIC
            out[m.group(1)] = v
        return out
    return _DYNAMIC


def _parse_call_args(args_str):
    """Split a Ruby call's argument list into (positionals, options): a token
    matching `identifier: value` at the top level is an option (key parsed,
    value left as _parse_ruby_value(value) — possibly `_DYNAMIC`); everything
    else is a positional, itself run through _parse_ruby_value."""
    positionals, options = [], {}
    for tok in _split_top_level(args_str):
        # _OPT_KEY_RE anchors on a leading identifier, so a quoted string or
        # symbol token (never starting with a letter/underscore) can't match
        # it and always falls through to positional, even if its contents
        # happen to contain a colon (e.g. a string default "10:30").
        m = _OPT_KEY_RE.match(tok)
        if m:
            options[m.group(1)] = _parse_ruby_value(m.group(2))
        else:
            positionals.append(_parse_ruby_value(tok))
    return positionals, options


def _default_index_name(table, columns):
    return f"index_{table}_on_{'_'.join(columns)}"


def _format_default(value):
    """Lossless, engine-agnostic string form of a parsed Ruby default (D7):
    numbers via str(), strings verbatim, booleans as 'true'/'false' — NOT
    imitating any particular DB engine's default-value spelling."""
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


# ---------------------------------------------------------------------------
# create_table body — column / reference / timestamps / index statements
# ---------------------------------------------------------------------------
def _apply_column_opts(col, opts, ctx, line_no, warn, seen_unknown_opts):
    """Apply the recognized {null, default, comment, limit, precision, scale}
    options onto `col` (mutated), building `sql_type` modifiers as it goes.
    Any other option name warns (deduped per option name per file — D7)."""
    if opts.get('null') is False:
        col['nullable'] = False
    if 'default' in opts:
        val = opts['default']
        if val is _DYNAMIC:
            warn(line_no, f'dynamic default ({ctx}) — attribute skipped')
        elif val is not None:
            col['default'] = _format_default(val)
    if 'comment' in opts and isinstance(opts['comment'], str):
        col['comment'] = opts['comment']
    mods = []
    if 'limit' in opts and isinstance(opts['limit'], int):
        mods.append(str(opts['limit']))
    elif 'precision' in opts and isinstance(opts['precision'], int):
        if 'scale' in opts and isinstance(opts['scale'], int):
            mods.append(f"{opts['precision']},{opts['scale']}")
        else:
            mods.append(str(opts['precision']))
    if mods:
        col['sql_type'] = f"{col['sql_type']}({','.join(mods)})"
    for key in opts:
        if key not in _COLUMN_RECOGNIZED_OPTS and key not in seen_unknown_opts:
            seen_unknown_opts.add(key)
            warn(line_no, f'unknown option {key!r} ({ctx}) — ignored')


def _handle_column_stmt(rails_type, args, line_no, table, warn, seen_unknown_opts):
    positionals, opts = _parse_call_args(args)
    if not positionals or positionals[0] is _DYNAMIC or not isinstance(positionals[0], str):
        warn(line_no, f"unparseable column name for t.{rails_type} — column skipped")
        return
    name = positionals[0]
    nullable = opts.get('null') is not False
    col = {'name': name, 'type': SQL_TYPES.get(rails_type, rails_type),
          'sql_type': rails_type, 'nullable': nullable}
    _apply_column_opts(col, opts, f'{rails_type}: {name!r}', line_no, warn, seen_unknown_opts)
    table['columns'].append(col)


def _handle_reference_stmt(method, args, line_no, table, warn, pending_fks):
    positionals, opts = _parse_call_args(args)
    if not positionals or positionals[0] is _DYNAMIC or not isinstance(positionals[0], str):
        warn(line_no, f"unparseable reference name for t.{method} — skipped")
        return
    name = positionals[0]
    for key in opts:
        if key not in _REFERENCE_RECOGNIZED_OPTS:
            warn(line_no, f'unknown option {key!r} (t.{method}: {name!r}) — ignored')
    rails_type = opts.get('type')
    if not isinstance(rails_type, str):
        rails_type = 'bigint'
    nullable = opts.get('null') is not False
    id_col = f'{name}_id'
    table['columns'].append({'name': id_col, 'type': SQL_TYPES.get(rails_type, rails_type),
                             'sql_type': rails_type, 'nullable': nullable})
    polymorphic = opts.get('polymorphic') is True
    index_cols = [id_col]
    if polymorphic:
        type_col = f'{name}_type'
        table['columns'].append({'name': type_col, 'type': 'string',
                                 'sql_type': 'string', 'nullable': nullable})
        index_cols = [type_col, id_col]
        if opts.get('foreign_key'):
            warn(line_no, f"t.{method} {name!r} is polymorphic — "
                          "foreign_key: is ignored (no single target table)")
    index_opt = opts.get('index', True)
    if index_opt is not False:
        unique = isinstance(index_opt, dict) and index_opt.get('unique') is True
        table['indexes'].append({'name': _default_index_name(table['_name'], index_cols),
                                 'columns': index_cols, 'unique': unique})
    fk_opt = opts.get('foreign_key')
    if fk_opt and not polymorphic:
        target = fk_opt['to_table'] if isinstance(fk_opt, dict) and 'to_table' in fk_opt \
            else pluralize(name)
        pending_fks.append({'table': table['_name'], 'column': id_col,
                            'target': target, 'line': line_no})


def _handle_timestamps_stmt(args, line_no, table, warn):
    _, opts = _parse_call_args(args)
    nullable = opts.get('null') is True  # default null: false — opposite of a plain column
    for key in opts:
        if key != 'null':
            warn(line_no, f'unknown option {key!r} (t.timestamps) — ignored')
    for col_name in ('created_at', 'updated_at'):
        table['columns'].append({'name': col_name, 'type': 'datetime',
                                 'sql_type': 'datetime', 'nullable': nullable})


def _handle_index_stmt(args, line_no, table, warn, top_level_table_name=None):
    """Shared by `t.index` (table param is the enclosing create_table) and
    `add_index` (table param is None; caller resolves `top_level_table_name`
    against the already-parsed tables and passes the fragment, or None if
    unknown)."""
    positionals, opts = _parse_call_args(args)
    if not positionals or positionals[0] is _DYNAMIC:
        warn(line_no, 'unparseable index columns — skipped')
        return
    cols = positionals[0] if isinstance(positionals[0], list) else [positionals[0]]
    if not all(isinstance(c, str) for c in cols):
        warn(line_no, 'unparseable index columns — skipped')
        return
    for key in opts:
        if key not in _INDEX_RECOGNIZED_OPTS:
            warn(line_no, f'unknown option {key!r} (index on {cols}) — ignored')
    tname = top_level_table_name if top_level_table_name is not None else table['_name']
    name = opts.get('name') if isinstance(opts.get('name'), str) else _default_index_name(tname, cols)
    unique = opts.get('unique') is True
    table['indexes'].append({'name': name, 'columns': cols, 'unique': unique})


_TABLE_STATEMENT_HANDLERS = {
    'timestamps': 'timestamps',
    'index': 'index',
    'references': 'references',
    'belongs_to': 'references',
}


def _handle_table_body_line(line, line_no, table, warn, pending_fks, seen_unknown_opts):
    """Dispatch one physical line inside a `create_table ... do |t|` block.
    Returns True if `line` closed the table (a bare `end`)."""
    if line == 'end':
        return True
    m = _T_CALL_RE.match(line)
    if not m:
        warn(line_no, f"unknown statement {line!r} — skipped")
        return False
    method, args = m.group('method'), m.group('args')
    if args.startswith('(') and args.endswith(')'):
        args = args[1:-1]
    kind = _TABLE_STATEMENT_HANDLERS.get(method)
    if method in _SCHEMA_COLUMN_TYPES:
        _handle_column_stmt(method, args, line_no, table, warn, seen_unknown_opts)
    elif kind == 'references':
        _handle_reference_stmt(method, args, line_no, table, warn, pending_fks)
    elif kind == 'timestamps':
        _handle_timestamps_stmt(args, line_no, table, warn)
    elif kind == 'index':
        _handle_index_stmt(args, line_no, table, warn)
    else:
        warn(line_no, f"unsupported column type 't.{method}' — column skipped")
    return False


# ---------------------------------------------------------------------------
# Top-level statements — create_table header, add_index, add_foreign_key
# ---------------------------------------------------------------------------
def _new_table(name):
    return {'_name': name, 'columns': [], 'indexes': [], 'primary_key': None}


def _start_create_table(m, line_no, warn):
    positionals, opts = _parse_call_args(m.group('args'))
    if not positionals or positionals[0] is _DYNAMIC or not isinstance(positionals[0], str):
        warn(line_no, 'unparseable create_table name — table skipped')
        return None
    name = positionals[0]
    table = _new_table(name)
    for key in opts:
        if key not in _CREATE_TABLE_RECOGNIZED_OPTS:
            warn(line_no, f'unknown option {key!r} (create_table {name!r}) — ignored')
    if isinstance(opts.get('comment'), str):
        table['comment'] = opts['comment']
    pk_opt = opts.get('primary_key')
    id_opt = opts.get('id', True)
    if isinstance(pk_opt, list):
        table['primary_key'] = pk_opt   # composite PK — its columns are defined in the body
    elif id_opt is not False:
        pk_name = pk_opt if isinstance(pk_opt, str) else 'id'
        pk_type = id_opt if isinstance(id_opt, str) else 'bigint'
        table['columns'].append({'name': pk_name, 'type': SQL_TYPES.get(pk_type, pk_type),
                                 'sql_type': SQL_TYPES.get(pk_type, pk_type),
                                 'nullable': False, 'primary': True})
        table['primary_key'] = pk_name
    return table


def _handle_top_level_line(line, line_no, tables, pending_fks, warn):
    if line == 'end' or _SCHEMA_HEADER_RE.match(line) or _IGNORED_TOP_LEVEL_RE.match(line):
        return
    m = _ADD_INDEX_RE.match(line)
    if m:
        tokens = _split_top_level(m.group('args'))
        tname = _parse_ruby_value(tokens[0]) if tokens else _DYNAMIC
        if not isinstance(tname, str):
            warn(line_no, 'unparseable add_index target table — skipped')
            return
        if tname not in tables:
            warn(line_no, f'add_index on unknown table {tname!r} — skipped')
            return
        _handle_index_stmt(', '.join(tokens[1:]), line_no, tables[tname], warn,
                           top_level_table_name=tname)
        return
    m = _ADD_FK_RE.match(line)
    if m:
        positionals, opts = _parse_call_args(m.group('args'))
        if (len(positionals) < 2 or positionals[0] is _DYNAMIC or positionals[1] is _DYNAMIC
                or not isinstance(positionals[0], str) or not isinstance(positionals[1], str)):
            warn(line_no, 'unparseable add_foreign_key arguments — skipped')
            return
        table_name, target = positionals[0], positionals[1]
        for key in opts:
            if key not in _ADD_FK_RECOGNIZED_OPTS:
                warn(line_no, f'unknown option {key!r} (add_foreign_key {table_name!r}, '
                              f'{target!r}) — ignored')
        column = opts.get('column') if isinstance(opts.get('column'), str) \
            else f'{_singularize(target)}_id'
        pending_fks.append({'table': table_name, 'column': column, 'target': target,
                            'line': line_no})
        return
    warn(line_no, f"unknown statement {line!r} — skipped")


def _resolve_pending_fks(tables, pending_fks, warn):
    """FK resolution pass (D7), run after the whole file is parsed: turn each
    pending {table, column, target} into a schema_fk association on `table`,
    or warn+skip when the table/target/column doesn't resolve. Dedups
    (table, column, target) so a t.references foreign_key: + a redundant
    add_foreign_key for the same edge produce exactly one association."""
    seen = set()
    for fk in pending_fks:
        tname, col, target, line_no = fk['table'], fk['column'], fk['target'], fk['line']
        if tname not in tables:
            warn(line_no, f'add_foreign_key on unknown table {tname!r} — skipped')
            continue
        if target not in tables:
            warn(line_no, f'foreign key on {tname!r} references unknown table {target!r} '
                          '— skipped')
            continue
        table = tables[tname]
        if not any(c['name'] == col for c in table['columns']):
            warn(line_no, f'foreign key on {tname!r} names column {col!r} which does not '
                          'exist — skipped')
            continue
        key = (tname, col, target)
        if key in seen:
            continue
        seen.add(key)
        assoc_type = 'has_one' if _unique_single_col(table, col) else 'belongs_to'
        name = col[:-3] if col.endswith('_id') else col
        table.setdefault('associations', []).append(
            {'type': assoc_type, 'name': name, 'target': target,
             'foreign_key': col, 'schema_fk': True})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def rails_schema_provider(path, given=None):
    """ProviderResult (kind='schema', provider='rails.schema') for a Rails
    db/schema.rb file: columns, primary keys, indexes, and foreign keys,
    parsed by pure text analysis — schema.rb is NEVER executed as Ruby (D7).
    A parser-derived FK becomes an association carrying `schema_fk: True`
    (D2), the schema layer's legacy-style provenance flag, mirroring how the
    live-DB layer marks its own FKs `db_fk: True`.

    `path` is always the resolved Path actually read from disk; `given` is
    the (possibly relative, possibly unresolved) path string to DISPLAY in
    warnings/location — sources.py passes the user's own spelling so a
    warning reads e.g. `db/schema.rb:12: ...` instead of an absolute path
    the user never typed. Defaults to `str(path)` for callers (tests, direct
    use) that don't distinguish the two."""
    display = given if given is not None else str(path)
    text = path.read_text(encoding='utf-8', errors='replace')
    warnings = []

    def warn(line_no, msg):
        warnings.append(f'{display}:{line_no}: {msg}')

    tables = {}
    pending_fks = []
    seen_unknown_col_opts = set()
    current = None  # the in-progress table dict, or None when outside a create_table block
    lines = text.splitlines()

    for line_no, raw in enumerate(lines, 1):
        line = _strip_comment(raw).strip()
        if not line:
            continue
        if current is not None:
            closed = _handle_table_body_line(line, line_no, current, warn, pending_fks,
                                             seen_unknown_col_opts)
            if closed:
                if not current.get('_discard'):
                    tables[current['_name']] = {k: v for k, v in current.items()
                                                if k not in ('_name', '_discard')}
                current = None
            continue
        m = _CREATE_TABLE_RE.match(line)
        if m:
            current = _start_create_table(m, line_no, warn)
            if current is None:
                # unparseable create_table name — still consume the body so its
                # lines don't get misread as top-level statements, but discard it
                current = _new_table('')
                current['_discard'] = True
            continue
        _handle_top_level_line(line, line_no, tables, pending_fks, warn)

    if current is not None:
        warn(len(lines), 'unterminated create_table block at end of file — skipped')

    _resolve_pending_fks(tables, pending_fks, warn)
    return make_provider_result('schema', 'rails.schema', tables,
                                location=display, warnings=warnings)
