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
    tables = adapter.fetch(url)
    try:
        tables = validate_tables_ir(tables, f'database adapter {adapter.name!r}',
                                    require_complete=True)
    except ValueError as e:
        sys.exit(f'Error: invalid output from database adapter {adapter.name!r}: {e}')
    return make_provider_result('db', adapter.name, tables,
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
            # Sol relaxation #3: a polymorphic belongs_to's target is a
            # SYNTHETIC, tableless name (Django's pluralized model name,
            # Rails' association name) — never a real table, by design (see
            # emit.py's _canonical_associations docstring on the same
            # exemption for --emit-json). Only a non-polymorphic association
            # needs its target to actually exist.
            if target and not a.get('polymorphic') and target not in tables:
                sys.exit(f"Error: {label} association {a.get('name')!r} on {tname!r} "
                         f"references unknown target table {target!r}")
            # a declared foreign_key must name a real column on the SOURCE table
            # — except a schema_missing table (Rails-only: no DB columns at
            # all, per merge.py's schema_missing derivation), where a
            # foreign_key is Rails' *convention* column, not a real one this
            # release ever observes (Sol relaxation #4: --emit-config's
            # round trip of a Rails-only belongs_to must not be rejected for
            # naming a column that was never going to appear).
            fk = a.get('foreign_key')
            if fk and fk not in col_names and not tables[tname].get('schema_missing'):
                sys.exit(f"Error: {label} association {a.get('name')!r} on {tname!r} "
                         f"declares foreign_key {fk!r} which does not exist in "
                         f"{tname!r}'s merged columns")

# ---------------------------------------------------------------------------
# `notes:` semantic validation + viewer-ready resolution (notes Phase 1).
#
# Called from cli._finish, AFTER --infer-fk has added its guessed relations to
# `tables` (Sol finding #3: a note can target an inferred relation, since this
# validates against the FINAL final-IR-plus-inferred-relations, not the
# pre-infer merge_ir output) and BEFORE --only/--exclude filtering (so a note
# on an about-to-be-excluded table still gets full semantic validation against
# the complete schema — _finish filters the RESOLVED notes down afterward).
# This is also after the final merge_ir (same final IR validate_config_
# references checks), so a note may target a table/association added by
# config.tables, and a note targeting something config.tables DROPPED is
# correctly an error here even though it was syntactically fine at load time
# (§6.4②-style two stage split, mirrored from validate_config_references
# above).
#
# notes are a pure sidecar: this function only READS `tables` (never mutates
# it, never feeds anything back into layers/merge_ir/ProviderResult/
# provenance/fk_columns) and returns a new, viewer-ready list. Every error
# includes the note's `id`, per the Phase 1 contract.
# ---------------------------------------------------------------------------
def resolve_and_validate_notes(notes, tables, label):
    """Semantic-validate config `notes` against the FINAL merged IR and
    resolve each `relation` note to the one association it identifies, so the
    viewer can match on a fully-resolved identity instead of re-implementing
    relation lookup in JS. Hard error via sys.exit (note id always included)."""
    out = []
    for n in notes:
        note_id = n['id']
        target = n['target']
        ttype = target['type']
        if ttype == 'global':
            entry = {'id': note_id, 'scope': 'global'}
        elif ttype == 'table':
            tname = target['table']
            if tname not in tables:
                sys.exit(f"Error: {label} note {note_id!r}: unknown table {tname!r} "
                         "(not in the final schema)")
            entry = {'id': note_id, 'scope': 'table', 'table': tname}
        else:  # relation
            src, tgt = target['source_table'], target['target_table']
            if src not in tables:
                sys.exit(f"Error: {label} note {note_id!r}: unknown source_table {src!r} "
                         "(not in the final schema)")
            cands = [a for a in tables[src]['associations'] if a['target'] == tgt]
            # Sol relaxation #2: an OMITTED key is a wildcard (don't narrow on
            # it at all); an explicit `null` narrows to "this field is
            # absent on the match" — `a.get(key)` is already None for an
            # association that never carries that optional key, so testing
            # `'key' in target` (not `target.get(key) is not None`) is what
            # makes the two cases distinguishable. This is what lets
            # --emit-config's reverse note mapping (which always emits every
            # one of these keys, explicit-null where the resolved
            # association has no value) reimport back to exactly the one
            # association it came from, instead of the null being silently
            # read as "don't care" and re-widening the match.
            if 'foreign_key' in target:
                cands = [a for a in cands if a.get('foreign_key') == target['foreign_key']]
            if 'name' in target:
                cands = [a for a in cands if a['name'] == target['name']]
            if 'through' in target:
                cands = [a for a in cands if a.get('through') == target['through']]
            # Sol finding #5: narrow by association TYPE (has_many/belongs_to/
            # has_one/has_and_belongs_to_many) — lets a note pick out e.g. a
            # has_many among a has_many/has_one pair that otherwise share name
            # and target. Config key is `assoc_type` (not `type` — that name is
            # already the note's own target-kind discriminator), but it
            # narrows against the association's real `type` field.
            if 'assoc_type' in target:
                cands = [a for a in cands if a['type'] == target['assoc_type']]
            # `polymorphic` is tri-state: absent = don't care, True/False both
            # narrow (previously only `is True` narrowed, so `polymorphic:
            # false` was silently ignored as a filter — Sol finding #5).
            # config.py's syntactic check rejects `polymorphic: null` outright
            # (it must be a real bool when the key is present at all), so
            # `'polymorphic' in target` never sees an explicit null here.
            if 'polymorphic' in target:
                cands = [a for a in cands if bool(a.get('polymorphic')) == target['polymorphic']]
            if not cands:
                sys.exit(f"Error: {label} note {note_id!r}: no relation from {src!r} to "
                         f"{tgt!r} matches (check source_table/target_table/foreign_key/"
                         "name/through/assoc_type/polymorphic)")
            if len(cands) > 1:
                sys.exit(f"Error: {label} note {note_id!r}: ambiguous — {len(cands)} "
                         f"relations from {src!r} to {tgt!r} match; add foreign_key/name/"
                         "through/assoc_type to disambiguate")
            a = cands[0]
            # Resolved relation entry — SHARED CONTRACT with the viewer (do not
            # diverge): id/scope/source_table/target/type/name/foreign_key/
            # through/polymorphic, where every field except id/scope/
            # source_table/target is the RESOLVED association `a`'s real value
            # (not the note's possibly-partial narrowing target). `type` is
            # ALWAYS included now (Sol finding #5) so the viewer can match on
            # role the same way this function just did.
            entry = {'id': note_id, 'scope': 'relation', 'source_table': src,
                     'target': tgt, 'type': a['type'], 'name': a['name'],
                     'foreign_key': a.get('foreign_key'), 'through': a.get('through'),
                     'polymorphic': bool(a.get('polymorphic'))}
        if n.get('title'):
            entry['title'] = n['title']
        entry['text'] = n['text']
        if n.get('links'):
            entry['links'] = n['links']
        out.append(entry)
    return out

# ---------------------------------------------------------------------------
# `groups:` semantic validation + viewer-ready resolution (groups Phase 1).
#
# Called from cli._finish, mirroring resolve_and_validate_notes above: AFTER
# --infer-fk (groups don't care about associations, but validating against the
# same final IR keeps the two sidecars consistent) and BEFORE --only/--exclude
# filtering (so a group is fully semantic-validated against the complete
# schema; _finish filters the RESOLVED groups' membership down afterward).
#
# groups are a pure sidecar: this function only READS `tables` (never mutates
# it, never feeds anything back into layers/merge_ir/ProviderResult/
# provenance/fk_columns) and returns a new, viewer-ready list. Every error
# includes the group's `id`, per the Phase 1 contract.
#
# Phase 1 scope: NO overlapping membership — a table claimed by two groups is
# a hard error (naming both group ids and the table), not a silently-picked
# winner. Layout affinity (placing group members near each other) is
# explicitly out of scope for this PR (DESIGN_ROADMAP §P2 follow-up).
# ---------------------------------------------------------------------------
def resolve_and_validate_groups(groups, tables, label):
    """Semantic-validate config `groups` against the FINAL merged IR: every
    member table must exist, and no table may belong to more than one group.
    Returns a viewer-ready list of {'id', 'tables':[...], 'title'?, 'color'?}
    (title/color present only when configured). Hard error via sys.exit
    (group id always included)."""
    out = []
    owner = {}  # table -> group id that already claimed it
    for g in groups:
        group_id = g['id']
        for t in g['tables']:
            if t not in tables:
                sys.exit(f"Error: {label} group {group_id!r}: unknown table {t!r} "
                         "(not in the final schema)")
            if t in owner:
                sys.exit(f"Error: {label} group {group_id!r}: table {t!r} already "
                         f"belongs to group {owner[t]!r} (a table may belong to only "
                         "one group)")
            owner[t] = group_id
        entry = {'id': group_id, 'tables': list(g['tables'])}
        if g.get('title'):
            entry['title'] = g['title']
        if g.get('color'):
            entry['color'] = g['color']
        out.append(entry)
    return out

# ---------------------------------------------------------------------------
# Excel export (.xlsx via zipfile — no third-party dependency)
# ---------------------------------------------------------------------------
