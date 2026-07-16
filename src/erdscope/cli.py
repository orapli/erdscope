def main():
    p = argparse.ArgumentParser(
        description='Generate an interactive ER diagram (and optional Excel table definitions) '
                    'from a MySQL / PostgreSQL / SQLite database, application code '
                    '(Rails / Prisma / Django), and/or a config schema — any one source is enough')
    p.add_argument('database',
                   metavar='mysql://user@host/db | postgres://user@host/db | sqlite:///file.db',
                   nargs='?',
                   help='Database connection URL. postgres:// takes an optional '
                        '?schema=name (default public); sqlite:///path/to/app.db reads a '
                        'local file (no server, nothing to install). MySQL/Postgres can '
                        'also be assembled from `engine`/`host`/`port`/`user`/`database` in '
                        'the config file (no password field there — use MYSQL_PWD/PGPASSWORD, '
                        '~/.my.cnf/~/.pgpass, or the interactive prompt). A read-only '
                        'account is recommended. Or pass the literal word "demo" to try '
                        'erdscope instantly against a bundled sample database — no database '
                        'of your own needed')
    # SUPPRESS on every config-mirrorable flag so we can tell "explicitly
    # passed on the CLI" (attribute present) from "left to the config file /
    # built-in default" (attribute absent) — see the merge loop below.
    p.add_argument('-o', '--output', default=argparse.SUPPRESS,
                   help='Output HTML file (default: erd.html)')
    p.add_argument('--models', metavar='PATH', action='append', default=argparse.SUPPRESS,
                   help='Merge association semantics parsed from application code '
                        '(Rails project/app/models dir, schema.prisma, or Django project). '
                        'Repeatable to merge several frameworks; later ones win on ties')
    p.add_argument('--adapter', metavar='PATH', action='append', default=argparse.SUPPRESS,
                   help='Load a Python plugin file that registers a custom database '
                        'adapter (subclass DBAdapter + @register_adapter) and/or a '
                        'framework overlay (subclass FrameworkOverlay + @register_overlay). '
                        'The new URL scheme / --models project kind then works like the '
                        'built-ins. Repeatable; also settable as config `adapters`')
    p.add_argument('--excel', metavar='FILE.xlsx', default=argparse.SUPPRESS,
                   help='Also write a table-definition workbook '
                        '(overview sheet + one sheet per table)')
    p.add_argument('--excel-template', metavar='FILE.xlsx', default=argparse.SUPPRESS,
                   help="Override the workbook's colors/fonts/borders from a template "
                        '.xlsx — see excel-template.xlsx and its Styles sheet for the '
                        '5-cell contract (default: built-in styling)')
    p.add_argument('--max-rows', type=int, default=argparse.SUPPRESS,
                   help='Max column rows shown per table before scrolling (default: 15)')
    p.add_argument('--only', action='append', metavar='PATTERN', default=argparse.SUPPRESS,
                   help='Include only tables matching the glob pattern(s). '
                        'Repeatable; comma-separated lists accepted (e.g. --only "user*,post*")')
    p.add_argument('--exclude', action='append', metavar='PATTERN', default=argparse.SUPPRESS,
                   help='Exclude tables matching the glob pattern(s). Same syntax as --only')
    p.add_argument('--infer-fk', action='store_true', default=argparse.SUPPRESS,
                   help='Guess relations from *_id column names when no real '
                        'association/FK backs them (off by default: unbacked guesses '
                        'can be wrong, and both the FK badge and the "PK/FK" column '
                        'view only ever show columns from real associations)')
    p.add_argument('--table-map', action='append', metavar='Class=table', default=argparse.SUPPRESS,
                   help="Rails only: override a model's table when static analysis "
                        "can't determine it (e.g. table_name set inside a concern "
                        "that lives in a gem). Repeatable; comma-separated lists "
                        "accepted, e.g. --table-map 'Widget=crm_widgets,Foo=bar_table'")
    p.add_argument('--config', metavar='PATH',
                   help='Config file (JSON, or YAML if PyYAML is installed) providing '
                        'defaults for the options above, plus the DB connection as '
                        'host/port/user/database (no password field — see README) and '
                        '`relations` (manual FK declarations). An explicit CLI flag or '
                        'argument always wins over the same key in the config. '
                        'Auto-discovered as .erdscope.json/.yml/.yaml in the current '
                        'directory if not given')
    p.add_argument('--no-config', action='store_true',
                   help='Skip config auto-discovery even if .erdscope.* exists in the cwd')
    p.add_argument('--no-open', action='store_true',
                   help='Skip automatically opening a browser after generating. Only '
                        'relevant to `erdscope demo` (which opens one by default); '
                        'accepted but has no effect on a normal run')
    args = p.parse_args()

    if args.database == 'demo':
        run_demo(args)
        return

    _run_pipeline(args)

def _run_pipeline(args):
    """The actual generate pipeline, shared by a normal run and `erdscope
    demo` (run_demo, in demo.py, rewrites args.database to a temp sqlite URL
    and forces config off, then calls straight in here)."""
    config = load_config(args)
    # Load any custom DB adapters (--adapter / config `adapters`) before the URL
    # is classified, so their schemes are registered in time. Config entries
    # first, then CLI ones — a later entry overriding a scheme wins (§ plugins).
    cfg_adapters = config.get('adapters') or []
    if isinstance(cfg_adapters, str):
        cfg_adapters = [cfg_adapters]
    adapter_paths = list(cfg_adapters) + list(getattr(args, 'adapter', []) or [])
    if adapter_paths:
        load_adapter_plugins(adapter_paths)
    url = args.database or assemble_config_url(config)
    # DB is optional now (§10): a schema can also come from --models and/or
    # config.tables. Only a NON-EMPTY url with an unrecognized scheme is an
    # error (a mistyped/wrong argument); a missing url just skips the DB layer.
    engine_name = None
    if url:
        scheme = url.split('://', 1)[0]
        adapter_cls = db_adapter_for(scheme)
        if adapter_cls is None:
            known = ', '.join(f'{s}://' for s in sorted(DB_ADAPTERS))
            sys.exit(f'Error: unrecognized database URL scheme {scheme!r} (known: {known}). '
                     'Pass it as the CLI argument, or set `database` (and optionally engine/'
                     'host/user/port) in the config file, e.g. mysql://readonly@127.0.0.1:3306/'
                     'myapp, postgres://readonly@127.0.0.1:5432/myapp, or sqlite:///./app.db. '
                     'A custom scheme needs its --adapter plugin. Or run with no database at '
                     'all by supplying --models and/or a config file with a `tables:` section')
        engine_name = adapter_cls.label or adapter_cls.name or scheme

    if hasattr(args, 'table_map'):
        tm = {}
        for arg in args.table_map:
            for pair in arg.split(','):
                if not pair:
                    continue
                if '=' not in pair:
                    sys.exit(f"Error: --table-map expects Class=table, got {pair!r}")
                cls, tbl = pair.split('=', 1)
                tm[cls] = tbl
        args.table_map = tm
    for key, default in CONFIG_DEFAULTS.items():
        if not hasattr(args, key):  # not explicitly passed on the CLI
            setattr(args, key, config.get(key, default))
    # --models is repeatable (append) and config `models` may be str or list;
    # normalize both to a list of paths, in given order (§10: later wins on ties)
    if args.models is None:
        models_list = []
    elif isinstance(args.models, str):
        models_list = [args.models]
    else:
        models_list = list(args.models)
    relations = config.get('relations', [])  # shape already validated by load_config()
    config_tables = config.get('tables')     # shape already validated by load_config()
    cfg_label = str(args.config) if getattr(args, 'config', None) else 'config'
    cfg_location = str(args.config) if getattr(args, 'config', None) else None

    # ── valid-input check (§10): at least one SCHEMA source (DB / Framework /
    #    config.tables). relations alone is not a source — it needs a base. ──
    if not (url or models_list or config_tables):
        sys.exit('Error: no schema input. Provide at least one of: a database URL '
                 '(mysql:// or postgres://) as the argument or config `database`; '
                 '--models pointing at a Rails/Prisma/Django project; or a config file '
                 'with a `tables:` section.')

    # ── collect provider layers, low→high spec priority, then merge (§3) ──
    layers = []
    db_result = None
    if url:  # only build/connect the DB layer when a url is present (no
             # connection and no password prompt otherwise — §10)
        db_result = db_provider(url)
        print(f'Fetched {len(db_result["tables"])} tables from {engine_name}', file=sys.stderr)
        layers.append(db_result)

    fw_root = None
    for m in models_list:  # each --models / config `models` entry, in order
        mroot = Path(m).expanduser().resolve()
        if not mroot.exists():
            sys.exit(f'Error: {mroot} does not exist')
        fw = framework_provider(mroot, args.table_map)
        layers.append(fw)
        if fw_root is None:
            fw_root = mroot  # the first framework drives the title fallback (§10)
        print(f'Merged {fw["source"]["provider"]} associations from {mroot}', file=sys.stderr)

    # config.tables join as a top-priority config layer (add/override/drop/
    # replace — §6.2/§7). Its DROP ops are semantic-validated against the merged
    # db+framework base first (§6.4②: a drop must target a real lower-layer
    # item), then the layer is merged so its additions are visible to relations.
    if config_tables:
        base = merge_ir(layers)
        validate_config_drops(config_tables, base, cfg_label)
        layers = layers + [config_provider(config, location=cfg_location)]
        print(f'Applied config schema ({len(config_tables)} table entr'
              f'{"y" if len(config_tables) == 1 else "ies"})', file=sys.stderr)

    # config `relations` join as a further config layer (§8.6/P0-3: override,
    # not skip). They validate against — and detect single-column-unique FKs
    # (1:1) in — the merged base INCLUDING config.tables, so a relation may
    # reference a config-added table/column.
    if relations:
        base2 = merge_ir(layers)
        layers = layers + [relations_to_config_layer(relations, base2)]
        print(f'Applied {len(relations)} manual relation(s) from config', file=sys.stderr)

    # merge_ir runs Phase A (identity merge) + Phase B reconcile_db_fks and
    # derives fk_columns / schema_missing; the DB-FK "covered" count is the
    # drop in db_fk-flagged associations from the raw DB layer to the result.
    tables = merge_ir(layers)
    if db_result:  # count db_fk edges dropped from the raw DB layer to the merge.
        # The raw DB layer carries legacy db_fk booleans; the merged IR carries
        # provenance — _assoc_provenance reads either shape.
        covered = (sum(1 for t in db_result['tables'].values()
                       for a in t['associations'] if _assoc_provenance(a) == 'db_fk')
                   - sum(1 for t in tables.values()
                         for a in t['associations'] if _assoc_provenance(a) == 'db_fk'))
        if covered:
            print(f'{covered} DB FKs covered by explicit associations', file=sys.stderr)

    # §6.4②: config.tables references (association targets, primary_key columns)
    # must resolve in the FINAL merged IR (config-added tables/columns count).
    if config_tables:
        validate_config_references(config_tables, tables, cfg_label)

    _finish(tables, args, _resolve_title(config, url, fw_root, getattr(args, 'output', None)))

def serialize_for_viewer(tables):
    """Convert the internal merged IR to the shape the HTML viewer JSON and the
    Excel export consume (§9.3): each association's structured `provenance` /
    `sources` is replaced by the legacy boolean flag it maps to (db_fk / manual /
    inferred, or NO flag for 'declared'), and both internal keys are dropped, so
    the output carries EXACTLY today's fields — provenance/sources never leak
    into the viewer JSON.

    Handles BOTH IR shapes, so it is safe to call unconditionally:
      - merged IR (main pipeline): an association has `provenance` -> convert it.
      - legacy IR (the demo: gen_demo.py feeds mysql_ir output straight into
        _finish; parser output carries db_fk/inferred booleans and no
        provenance) -> pass the existing flags through unchanged.
    This dual handling is what keeps the demo byte-identical. Pure: returns a
    deep copy, never mutates the input."""
    out = copy.deepcopy(tables)
    for t in out.values():
        for a in t.get('associations', []):
            if 'provenance' in a:
                prov = a.pop('provenance')
                a.pop('sources', None)
                a.update(legacy_flags_for(prov))
    return out

def _finish(tables, args, title_name):
    """Shared tail: FK inference, --only/--exclude filtering, HTML generation."""
    if getattr(args, 'infer_fk', False):
        inferred = infer_fk_associations(tables)
        if inferred:
            print(f'Inferred {inferred} relations from *_id columns', file=sys.stderr)

    # single source of truth for "is this column really a foreign key" —
    # the FK badge and the PK/FK column view both read this instead of
    # guessing from the column name, so they can only ever show a column
    # that's backed by a real (declared, DB, or --infer-fk) association
    for t in tables.values():
        t['fk_columns'] = sorted({a['foreign_key'] for a in t['associations']
                                  if a.get('foreign_key')})

    def patterns(args_list):
        return [pat for arg in args_list for pat in arg.split(',') if pat]

    if args.only:
        pats = patterns(args.only)
        tables = {k: v for k, v in tables.items() if any(fnmatch(k, p) for p in pats)}
    if args.exclude:
        pats = patterns(args.exclude)
        tables = {k: v for k, v in tables.items() if not any(fnmatch(k, p) for p in pats)}
    if args.only or args.exclude:
        if not tables:
            sys.exit('Error: no tables left after --only/--exclude filtering')
        print(f'Filtered: {len(tables)} tables', file=sys.stderr)

    # §9.3 serialize boundary: convert the internal provenance/sources IR to
    # today's legacy-flag shape (a no-op pass-through for the already-legacy demo
    # IR), so BOTH the HTML DATA_JSON and the Excel export below see exactly the
    # fields they do today. This is the single point where provenance is undone.
    tables = serialize_for_viewer(tables)

    # Substitute the other placeholders BEFORE inserting DATA_JSON, and
    # escape `</` in the JSON — otherwise a table/column comment containing
    # a literal "__TITLE__"/"__MAX_ROWS__" would get rewritten by the later
    # .replace() calls, and one containing "</script>" would prematurely
    # close the script tag and blank the whole page. Both are realistic:
    # comments are free-text and come straight from the database.
    data_json = json.dumps({'tables': tables}, ensure_ascii=False).replace('</', '<\\/')
    html = (HTML_TEMPLATE
            .replace('__MAX_ROWS__', str(args.max_rows))
            .replace('__TITLE__', f'{title_name} — ERD')
            .replace('__DATA_JSON__', data_json))

    out = Path(args.output)
    out.write_text(html, encoding='utf-8')
    print(f'Generated: {out} ({out.stat().st_size // 1024} KB)', file=sys.stderr)

    if getattr(args, 'excel', None):
        write_excel(tables, Path(args.excel), title_name,
                    template_path=getattr(args, 'excel_template', None))
        print(f'Generated: {args.excel}', file=sys.stderr)
    elif getattr(args, 'excel_template', None):
        print('Warning: --excel-template has no effect without --excel', file=sys.stderr)

if __name__ == '__main__':
    main()
