# ---------------------------------------------------------------------------
# Provider dispatchers (DB) + config layer construction/validation.
#
# The framework overlays and their dispatcher (framework_provider /
# detect_code_source) live in the frameworks/ package; db_provider dispatches
# through the db/ adapter registry. What remains here is the DB provider seam
# and everything that builds/validates the config layer.
# ---------------------------------------------------------------------------
def _password_free_url(url):
    """Rebuild a connection URL without its password, for a ProviderResult's
    Source.location (§5: location is password-free). Keeps user@host:port/db
    and any ?query (e.g. postgres ?schema=name)."""
    u = urlparse(url)
    netloc = u.hostname or ''
    if u.username:
        netloc = f'{u.username}@{netloc}'
    if u.port:
        netloc = f'{netloc}:{u.port}'
    out = f'{u.scheme}://{netloc}{u.path}'
    return f'{out}?{u.query}' if u.query else out

def db_provider(url):
    """DB ProviderResult (§5). Dispatches on the URL scheme to the registered
    DBAdapter (built-in MySQL/PostgreSQL, or a user adapter loaded via
    --adapter) and packages the IR with a password-free location. The built-in
    adapters delegate to the module-level parse_mysql/parse_postgres, so the
    test harness's monkeypatch of those still applies."""
    scheme = urlparse(url).scheme
    adapter_cls = db_adapter_for(scheme)
    if adapter_cls is None:
        known = ', '.join(sorted(DB_ADAPTERS)) or '(none registered)'
        sys.exit(f'Error: no database adapter for URL scheme {scheme!r} '
                 f'(known schemes: {known})')
    adapter = adapter_cls()
    return make_provider_result('db', adapter.name, adapter.fetch(url),
                                location=_password_free_url(url))

def relations_to_config_layer(relations, base_tables):
    """Convert the config `relations` list into a config-kind ProviderResult of
    association fragments (§8.6/P0-3). Validates hard (unknown table/column/
    target are the user's typo), and cardinality is has_one when one_to_one is
    set OR the FK column is single-column-unique in the merged base, else
    belongs_to.

    No `manual` flag is written into the fragment — merge_ir forces config-kind
    associations to manual provenance (§9.1). And no "skip if already covered":
    a config relation OVERRIDES a db/framework association of the same identity
    (the intentional §12/P0-3 behavior), which merge_ir's Phase A handles."""
    tables = {}
    for i, r in enumerate(relations):
        where = f'relations[{i}]'
        for key in ('table', 'column', 'references'):
            if not r.get(key):
                sys.exit(f'Error: {where} is missing required key {key!r}')
        table, col, target = r['table'], r['column'], r['references']
        if table not in base_tables:
            sys.exit(f'Error: {where}: unknown table {table!r}')
        if not any(c['name'] == col for c in base_tables[table]['columns']):
            sys.exit(f'Error: {where}: {table!r} has no column {col!r}')
        if target not in base_tables:
            sys.exit(f'Error: {where}: unknown target table {target!r}')
        if r.get('one_to_one'):
            assoc_type = 'has_one'
        else:
            assoc_type = 'has_one' if _unique_single_col(base_tables[table], col) else 'belongs_to'
        name = r.get('name') or (col[:-3] if col.endswith('_id') else col)
        tables.setdefault(table, {'associations': []})['associations'].append(
            {'type': assoc_type, 'name': name, 'target': target, 'foreign_key': col})
    return make_provider_result('config', 'config', tables)

def config_provider(config, location=None):
    """Config ProviderResult (§5) from config['tables'] — the table fragments
    WITH their `drop`/`*_mode` operation markers intact (merge_ir consumes and
    strips them). Associations carry no `manual` flag; merge_ir forces
    config-kind associations to manual provenance (§9.1). Semantic validation
    of the ops/refs (§6.4②) is done by the caller against the merged base."""
    return make_provider_result('config', 'config', config.get('tables', {}) or {},
                                location=location)

def validate_config_drops(config_tables, base, label):
    """Semantic validation (§6.4②) of config.tables DropOperations against the
    merged db+framework `base`: a drop must target something that actually
    exists in a lower layer (dropping a nonexistent table/column/index/
    association — or one only config itself adds — is the user's mistake). Runs
    BEFORE the config layer is merged. Hard error via sys.exit; `label` is the
    config path (or 'config') for the message."""
    for tname, frag in config_tables.items():
        if not isinstance(frag, dict):
            continue  # shape already checked at load time
        if frag.get('drop') is True:
            if tname not in base:
                sys.exit(f"Error: {label} drops table {tname!r} but no such table exists")
            continue
        for c in frag.get('columns', []):
            if c.get('drop') is True and not (
                    tname in base and any(x['name'] == c['name'] for x in base[tname]['columns'])):
                sys.exit(f"Error: {label} drops column {tname}.{c['name']} but no such column exists")
        for ix in frag.get('indexes', []):
            if ix.get('drop') is True and not (
                    tname in base and any(i.get('name') == ix['name']
                                          for i in base[tname].get('indexes', []))):
                sys.exit(f"Error: {label} drops index {ix['name']!r} on {tname!r} "
                         "but no such index exists")
        for a in frag.get('associations', []):
            if a.get('drop') is True:
                idents = [association_key(tname, x)
                          for x in base.get(tname, {}).get('associations', [])]
                if not any(_config_assoc_drop_matches(a, i) for i in idents):
                    sys.exit(f"Error: {label} drops an association on {tname!r} "
                             "but no matching association exists")

def validate_config_references(config_tables, tables, label):
    """Semantic validation (§6.4②) of config.tables references against the FINAL
    merged IR: every config-declared association `target` must be an existing
    table, and every config-declared `primary_key` column must exist in that
    table's final columns. A config-ADDED table/column is already merged in, so
    referencing it is valid (self- and cross-references included). Runs AFTER
    the final merge. Hard error via sys.exit."""
    for tname, frag in config_tables.items():
        if not isinstance(frag, dict) or frag.get('drop') is True or tname not in tables:
            continue
        col_names = {c['name'] for c in tables[tname]['columns']}
        pk = frag.get('primary_key')
        if pk is not None:
            for n in ([pk] if isinstance(pk, str) else pk):
                if n not in col_names:
                    sys.exit(f"Error: {label} table {tname!r} primary_key names column {n!r} "
                             "which does not exist in the merged schema")
        for a in frag.get('associations', []):
            if a.get('drop') is True:
                continue
            target = a.get('target')
            if target and target not in tables:
                sys.exit(f"Error: {label} association {a.get('name')!r} on {tname!r} "
                         f"references unknown target table {target!r}")
            # a declared foreign_key must name a real column on the SOURCE table
            fk = a.get('foreign_key')
            if fk and fk not in col_names:
                sys.exit(f"Error: {label} association {a.get('name')!r} on {tname!r} "
                         f"declares foreign_key {fk!r} which does not exist in "
                         f"{tname!r}'s merged columns")

# ---------------------------------------------------------------------------
# Excel export (.xlsx via zipfile — no third-party dependency)
# ---------------------------------------------------------------------------
