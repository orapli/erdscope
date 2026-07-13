# ---------------------------------------------------------------------------
# Layered IR merge — Phase A (identity merge) + Phase B (reconcile_db_fks)
# REFACTOR_PLAN.md §7 (field rules), §8 (association identity), §9 (provenance).
#
# The pipeline builds ProviderResult layers (db / framework / config) and folds
# them with merge_ir, whose Phase B reconcile_db_fks subsumes the old
# per-table dedupe pass.
# ---------------------------------------------------------------------------
# Field authority as a numeric rank: among the layers that PROVIDE a field
# (key present — §4: an absent key never participates), pick the value
# maximizing (rank, spec_order); spec_order is the index in `layers`, so a
# later layer wins ties (e.g. multiple frameworks -> last one wins).
_PHYSICAL_RANK = {'config': 3, 'db': 2, 'framework': 1}   # DB is the physical truth
_LOGICAL_RANK = {'config': 3, 'framework': 2, 'db': 1}    # code owns logical names
# Column attributes split by authority kind (§7.2). Everything not physical
# (i.e. `comment`) is logical.
_PHYSICAL_COL_ATTRS = ('type', 'sql_type', 'nullable', 'primary', 'default', 'extra')
# Deterministic column-attribute emit order (str-set iteration is hash-seed
# dependent, so never iterate a set for output order).
_COL_ATTR_ORDER = ('type', 'sql_type', 'nullable', 'primary', 'default', 'extra', 'comment')
# Representative-provenance precedence (§9.1): manual > declared > db_fk > inferred.
_PROV_PRECEDENCE = {'manual': 3, 'declared': 2, 'db_fk': 1, 'inferred': 0}

def _pick_by_authority(contribs):
    """contribs: list of (rank, spec_order, value). Return the value with the
    greatest (rank, spec_order)."""
    return max(contribs, key=lambda c: (c[0], c[1]))[2]

def _assoc_role(a):
    """Identity role for an association (§8.1). owner_fk holds the FK column;
    collection is has_many/habtm without an FK; inverse_one is has_one without
    an FK. A rare belongs_to/other lacking an FK gets its own name-keyed role
    so it's never wrongly merged with a real owner_fk."""
    if a.get('foreign_key'):
        return 'owner_fk'
    if a['type'] in ('has_many', 'has_and_belongs_to_many'):
        return 'collection'
    if a['type'] == 'has_one':
        return 'inverse_one'
    return 'named'

def association_key(source_table, a):
    """Stable identity tuple (§8.1). `name` is part of the identity for EVERY
    role, owner_fk included: the Rails alias pattern (`belongs_to :user` AND
    `belongs_to :author`, both on `user_id`) is two distinct associations that
    must stay separate. A name-blind owner_fk identity would over-merge them.
    The DB-FK-vs-code-name case is handled without it: a DB FK's name is the
    machine-derived column stem (`user_id`->`user`), so a conventional
    `belongs_to :user` matches and merges in Phase A, while a renamed
    `belongs_to :author` stays separate and Phase B reconcile_db_fks drops the
    now-covered DB FK — no double edge either way. A single-column FK is
    normalized to a frozenset, leaving room for future composite FKs (§4.2)."""
    fk = frozenset([a['foreign_key']]) if a.get('foreign_key') else frozenset()
    key = [source_table, a['target'], fk, _assoc_role(a), a['name']]
    if a.get('through'):
        key.append(('through', a['through']))
    if a.get('polymorphic'):
        key.append(('polymorphic', True))
    return tuple(key)

def _merge_column(name, contribs):
    """contribs: list of (kind, spec_order, column_dict) for one column name,
    in layer order. Physical attrs resolve Config>DB>Framework, `comment`
    resolves Config>Framework>DB, present-only (§7.2)."""
    out = {'name': name}
    present = set()
    for _, _, c in contribs:
        present |= set(c)
    present.discard('name')
    present.discard('drop')  # config op marker, never a data attribute
    ordered = [k for k in _COL_ATTR_ORDER if k in present]
    ordered += sorted(present - set(_COL_ATTR_ORDER))
    for attr in ordered:
        rank_map = _PHYSICAL_RANK if attr in _PHYSICAL_COL_ATTRS else _LOGICAL_RANK
        cc = [(rank_map[kind], order, c[attr]) for kind, order, c in contribs if attr in c]
        if cc:
            out[attr] = _pick_by_authority(cc)
    return out

def _assoc_content_differs(a, b):
    """Do two same-identity associations differ in merge-visible content
    (cardinality / target / through / polymorphic)? A name difference is
    expected (declared vs machine-derived) and never counts. Shared by the
    config-override (§8.6) and framework-vs-framework (§10) override warnings."""
    return (a['type'] != b['type'] or a['target'] != b['target']
            or a.get('through') != b.get('through')
            or bool(a.get('polymorphic')) != bool(b.get('polymorphic')))

def _merge_association_group(members, layer_sources):
    """members: list of (spec_order, kind, assoc_dict), same identity, in layer
    order. `layer_sources[spec_order]` is the contributing layer's {kind,
    provider}. Merge into one association per §8.4 + §9. The merged association
    carries a structured `provenance` (representative string) + `sources` (the
    deduplicated union of contributing layers) and NOT the legacy db_fk/manual/
    inferred booleans — those are re-derived only at the serialize boundary."""
    ms = sorted(members, key=lambda m: m[0])
    role = _assoc_role(ms[0][2])
    out = {}
    # cardinality: never lose a 1:1 — an owner_fk group with any has_one is
    # has_one (§8.4); otherwise later layer's type wins.
    types = [a['type'] for _, _, a in ms]
    out['type'] = 'has_one' if (role == 'owner_fk' and 'has_one' in types) else ms[-1][2]['type']
    # name: logical authority (declared/config name beats a DB-derived one)
    out['name'] = _pick_by_authority(
        [(_LOGICAL_RANK[kind], order, a['name']) for order, kind, a in ms])
    out['target'] = ms[-1][2]['target']  # constant within an identity group
    for _, _, a in ms:
        if a.get('foreign_key'):
            out['foreign_key'] = a['foreign_key']
            break
    thr = [(order, a['through']) for order, _, a in ms if a.get('through')]
    if thr:
        out['through'] = max(thr, key=lambda x: x[0])[1]
    if any(a.get('polymorphic') for _, _, a in ms):
        out['polymorphic'] = True
    # §8.6 (P0-3): a config association overrides a same-identity db/framework
    # one. Warn when it overrides *differing* content — a different cardinality,
    # target, through, or polymorphic flag — so a silent semantic change is
    # visible. A name difference is expected (declared vs machine-derived) and
    # never warned. Identical content merges quietly.
    cfg = [a for _, kind, a in ms if kind == 'config']
    if cfg:
        c = cfg[-1]
        for _, kind, a in ms:
            if kind == 'config':
                continue
            if _assoc_content_differs(a, c):
                print(f"Warning: config association {c.get('name')!r} (on {c['target']!r}) "
                      f"overrides a differing {kind} association", file=sys.stderr)
                break
    # §10 (multiple --models): a later framework layer overriding an earlier
    # framework layer's same-identity association with differing content is a
    # multi-framework conflict the user should see. `ms` is already layer-order
    # sorted, so the last framework member is the winner.
    fw_members = [a for _, kind, a in ms if kind == 'framework']
    if len(fw_members) >= 2:
        winner = fw_members[-1]
        if any(_assoc_content_differs(a, winner) for a in fw_members[:-1]):
            print(f"Warning: framework association {winner.get('name')!r} (on "
                  f"{winner['target']!r}) overrides a differing earlier framework "
                  f"association", file=sys.stderr)
    # provenance (§9.1): representative string by precedence (manual > declared
    # > db_fk > inferred). A config-layer association is manual by definition
    # (§9.1) even if its fragment carries no flag; otherwise read the member's
    # own legacy flags. Stored structured on the merged IR — converted back to a
    # legacy boolean only by serialize_for_viewer (§9.3).
    def member_prov(kind, a):
        return 'manual' if kind == 'config' else provenance_of(a)
    out['provenance'] = max((member_prov(kind, a) for _, kind, a in ms),
                            key=lambda p: _PROV_PRECEDENCE[p])
    # sources (§9.1): the deduplicated union of the {kind, provider} of every
    # layer that contributed to this identity, deterministically ordered. A DB
    # FK also declared in Rails ends up with both {db,mysql} and {framework,
    # rails}.
    seen_src, src = set(), []
    for order, _, _ in ms:
        s = layer_sources[order]
        key = (s['kind'], s['provider'])
        if key not in seen_src:
            seen_src.add(key)
            src.append({'kind': s['kind'], 'provider': s['provider']})
    out['sources'] = sorted(src, key=lambda s: (s['kind'], s['provider']))
    return out

def _config_assoc_drop_matches(drop, ident):
    """Does a config association DropOperation match a merged association's
    identity tuple (§4.3/§6.2)? Role + target must match. An owner_fk drop
    matches by (target, foreign_key) and — since the identity now includes
    `name` (6b) — drops EVERY owner_fk association on that column/target when
    no `name` is given (so a wrong FK edge can be removed without naming it),
    or additionally filters by `name` when one is given. A collection/inverse
    drop matches by (type-role, target, name). If the drop pins through/
    polymorphic, those must match too."""
    target, fk, role, name = ident[1], ident[2], ident[3], ident[4]
    if role != _assoc_role(drop) or target != drop.get('target'):
        return False
    extras = dict(ident[5:])
    if drop.get('through') and drop['through'] != extras.get('through'):
        return False
    if drop.get('polymorphic') and not extras.get('polymorphic'):
        return False
    if role == 'owner_fk':
        d_fk = frozenset([drop['foreign_key']]) if drop.get('foreign_key') else frozenset()
        if fk != d_fk:
            return False
        return drop.get('name') is None or drop['name'] == name
    return drop.get('name') == name

def _merge_table(tname, contribs, layer_sources):
    """contribs: list of (kind, spec_order, fragment) for one table, in layer
    order. `layer_sources[spec_order]` is that layer's {kind, provider} source,
    threaded through to the association merge for `sources` (§9.1). Build the
    merged table (columns/indexes/primary_key/comment/associations). Derived
    fields (fk_columns, schema_missing, primary_key normalization) are applied
    later, after Phase B.

    Config-only operation markers (§6.2), present ONLY on config-kind fragments:
      - `columns_mode|indexes_mode|associations_mode: "replace"` (table-scope):
        discard lower-layer (db/framework) contributions for that field before
        applying the config list. Default "merge" = additive/override (today's
        behavior). replace precedes per-item drop (nothing lower to drop).
      - per-item `drop: true`: remove the matching column (by name) / index (by
        name) / association (by identity, see _config_assoc_drop_matches).
    Within a config fragment, a field's list is processed in document order;
    drop entries remove matching lower-layer items, non-drop entries add/override
    (Step-3 load validation forbids a duplicate name/identity within one config
    fragment, so a drop and a re-add of the SAME item can't coexist there). All
    op markers are stripped from the output — the final IR carries data only.
    A drop that matches nothing is a silent no-op here; the hard error for an
    absent drop target is semantic validation, added in Step 7b (§6.4②)."""
    def replace(mode_key):
        return any(f.get(mode_key) == 'replace' for k, _, f in contribs if k == 'config')

    merged = {}
    # ── columns: keyed by name, first-seen order across layers (§7.2) ──
    col_replace = replace('columns_mode')
    col_order, col_seen, col_contribs, col_drops = [], set(), {}, set()
    for kind, order, frag in contribs:
        for c in frag.get('columns', []):
            if kind == 'config' and c.get('drop') is True:
                col_drops.add(c['name'])
                continue
            if col_replace and kind != 'config':
                continue  # replace discards lower-layer columns
            nm = c['name']
            if nm not in col_seen:
                col_seen.add(nm)
                col_order.append(nm)
            col_contribs.setdefault(nm, []).append((kind, order, c))
    merged['columns'] = [_merge_column(nm, col_contribs[nm])
                         for nm in col_order if nm not in col_drops]
    # ── primary_key: physical authority, present-only (§7.3) ──
    pk = [(_PHYSICAL_RANK[kind], order, frag['primary_key'])
          for kind, order, frag in contribs if 'primary_key' in frag]
    merged['primary_key'] = _pick_by_authority(pk) if pk else None
    # ── indexes: keyed by name (unnamed -> tuple(columns), NOT unique — §7.4);
    #    whole-index physical authority; union across keys ──
    ix_replace = replace('indexes_mode')
    ix_order, ix_seen, ix_contribs, ix_drops = [], set(), {}, set()
    for kind, order, frag in contribs:
        for ix in frag.get('indexes', []):
            if kind == 'config' and ix.get('drop') is True:
                ix_drops.add(ix['name'])  # index drop is always by name (§7.4)
                continue
            if ix_replace and kind != 'config':
                continue
            k = ix['name'] if ix.get('name') else tuple(ix.get('columns', []))
            if k not in ix_seen:
                ix_seen.add(k)
                ix_order.append(k)
            ix_contribs.setdefault(k, []).append((_PHYSICAL_RANK[kind], order, ix))
    merged['indexes'] = []
    for k in ix_order:
        if k in ix_drops:
            continue
        ix = copy.deepcopy(_pick_by_authority(ix_contribs[k]))
        ix.pop('drop', None)  # strip any op marker
        merged['indexes'].append(ix)
    # ── comment: logical authority, present-only; null/"" = delete (§7.5) ──
    cm = [(_LOGICAL_RANK[kind], order, frag['comment'])
          for kind, order, frag in contribs if 'comment' in frag]
    if cm:
        val = _pick_by_authority(cm)
        if val:  # a non-empty string; null/"" resolves to "no comment"
            merged['comment'] = val
    # ── associations: Phase A identity merge (§7.6/§8) ──
    a_replace = replace('associations_mode')
    a_order, a_groups, a_drops = [], {}, []
    for kind, order, frag in contribs:
        for a in frag.get('associations', []):
            if kind == 'config' and a.get('drop') is True:
                a_drops.append(a)   # collected separately (may omit `name`)
                continue
            if a_replace and kind != 'config':
                continue
            ident = association_key(tname, a)
            if ident not in a_groups:
                a_groups[ident] = []
                a_order.append(ident)
            a_groups[ident].append((order, kind, a))
    merged['associations'] = [
        _merge_association_group(a_groups[i], layer_sources) for i in a_order
        if not any(_config_assoc_drop_matches(d, i) for d in a_drops)]
    return merged

def _normalize_primary_key(tname, t, warn=True, authoritative=False):
    """§7.3: primary_key is authoritative — ensure every column it names carries
    primary=True (a PK column whose primary flag a lower layer didn't set is
    corrected up). By default columns are NOT flipped to False, so a composite PK
    whose columns were all flagged by the DB (primary_key stores only the first)
    keeps every member primary. A PK naming an absent column is a warning, not
    fatal.

    `authoritative=True` (a config layer supplied primary_key, so it is COMPLETE)
    additionally RESETS every non-PK column's primary flag to False — and clears
    ALL primary flags when the config primary_key is null. This is what makes
    `primary_key: null` (or a config PK narrower than the DB's) take full effect
    instead of leaving stale DB primary flags behind.

    `warn=False` suppresses the missing-column warning for a config-declared
    primary_key: validate_config_references issues an authoritative hard error
    for it, so the non-fatal warning would be a redundant second message."""
    pk = t.get('primary_key')
    pk_names = [] if pk is None else ([pk] if isinstance(pk, str) else list(pk))
    names = {c['name'] for c in t['columns']}
    for c in t['columns']:
        if c['name'] in pk_names:
            c['primary'] = True
        elif authoritative:
            c['primary'] = False
    missing = [n for n in pk_names if n not in names]
    if missing and warn:
        print(f"Warning: table {tname!r} primary_key names column(s) not present: "
              f"{', '.join(missing)}", file=sys.stderr)

def merge_ir(layers):
    """Merge an ordered list of ProviderResults (low→high spec priority — the
    usual order is [db?, framework_1?, ..., config?]) into one IR dict per §7-§9.
    Pure: inputs are deep-copied, never mutated.

    Steps: union tables (first-seen order) → apply config table `drop` → per-
    field authority merge with config `drop`/`*_mode: replace` ops (Phase A,
    incl. association identity merge) → Phase B reconcile_db_fks → derive
    primary_key normalization, fk_columns (§7.7), and schema_missing (§7.8).

    Config operation markers (table `drop`, per-item `drop`, `*_mode: replace`)
    are honored here and stripped from the output IR (they're operations, not
    data). They are only produced on config-kind layers; the current pipeline's
    config layer (relations → associations) carries none, so this is inert for
    it. `config.tables` is wired into main in Step 7b; semantic validation of
    drop targets etc. (§6.4②) also lands there."""
    prepared = [(pr['source']['kind'], copy.deepcopy(pr['tables'])) for pr in layers]
    # per-layer source ({kind, provider}), indexed by spec order — threaded into
    # the association merge so each merged association can record the union of
    # the layers that contributed to it (§9.1 `sources`).
    layer_sources = [pr['source'] for pr in layers]
    order, seen = [], set()
    for _, tbls in prepared:
        for tname in tbls:
            if tname not in seen:
                seen.add(tname)
                order.append(tname)
    # Tables whose primary_key is contributed by a config layer (the KEY is
    # present, even if its value is null). config has the highest physical-field
    # rank, so a config primary_key always wins and is authoritative/COMPLETE:
    # _normalize_primary_key resets non-PK primary flags for these (§7.3, P1-c).
    # It also suppresses the non-fatal missing-column warning (validate_config_
    # references raises an authoritative hard error instead). Non-config PK
    # mismatches still warn and never reset flags (composite-PK safe).
    config_pk_tables = {tname for kind, tbls in prepared if kind == 'config'
                        for tname, frag in tbls.items()
                        if isinstance(frag, dict) and 'primary_key' in frag}
    result = {}
    for tname in order:
        contribs = [(kind, spec, tbls[tname])
                    for spec, (kind, tbls) in enumerate(prepared) if tname in tbls]
        # config table drop (§6.2): `tables: { t: { drop: true } }` removes the
        # table from the result (a no-op if nothing lower provided it).
        if any(kind == 'config' and frag.get('drop') is True for kind, _, frag in contribs):
            continue
        result[tname] = _merge_table(tname, contribs, layer_sources)
    # Phase B: edge-level DB-FK reconciliation (§8.5), same as the legacy path.
    reconcile_db_fks(result)
    # Derived values (§7.3/§7.7/§7.8) — computed, never read from input.
    for tname, t in result.items():
        # Normalize columns to the shape the HTML/Excel consumers expect always
        # present (mysql_ir and the Prisma/Django parsers already set these on
        # every column; a config-only column may omit them). Inert for db/
        # framework columns — pure setdefault, so no existing output changes.
        for c in t['columns']:
            c.setdefault('type', '')
            c.setdefault('nullable', False)
            c.setdefault('primary', False)
        # Normalize indexes to always carry `unique` (mysql_ir/parsers already
        # do; a config index may omit it — Step-3 accepts it as optional). This
        # keeps every downstream ix['unique'] read (_unique_single_col, Excel,
        # HTML) safe. Inert for db/framework indexes, so demo stays byte-equal.
        for ix in t.get('indexes', []):
            ix.setdefault('unique', False)
        authoritative_pk = tname in config_pk_tables
        _normalize_primary_key(tname, t, warn=not authoritative_pk,
                               authoritative=authoritative_pk)
        t['fk_columns'] = sorted({a['foreign_key'] for a in t['associations']
                                  if a.get('foreign_key')})
        if len(t['columns']) == 0:
            t['schema_missing'] = True
    return result

def reconcile_db_fks(tables):
    """Phase B (§8.5) — edge-level DB-FK reconciliation: an explicit (non-db_fk,
    non-inferred) association covering an undirected {source, target} pair drops
    the DB FK for that pair when the explicit side names no column or the same
    column; a dropped has_one DB FK upgrades a lone covering belongs_to to
    has_one in place. belongs_to alone doesn't assert cardinality in Rails, so
    dropping a 1:1 DB FK outright would silently discard the DB's 1:1 signal.
    Mutates `tables` in place and returns the number of DB FKs removed."""
    explicit_by_pair = {}
    for name, t in tables.items():
        for a in t['associations']:
            if _assoc_provenance(a) in ('declared', 'manual'):
                explicit_by_pair.setdefault(frozenset((name, a['target'])), []).append((name, a))
    removed = 0
    for name, t in tables.items():
        kept = []
        for a in t['associations']:
            if _assoc_provenance(a) == 'db_fk':
                candidates = explicit_by_pair.get(frozenset((name, a['target'])), [])
                covering = [(n, ea) for n, ea in candidates
                            if not ea.get('foreign_key') or ea['foreign_key'] == a.get('foreign_key')]
                if covering:
                    has_cardinality = any(ea['type'] in ('has_one', 'has_many') for _, ea in covering)
                    if a['type'] == 'has_one' and not has_cardinality:
                        for other_name, ea in covering:
                            if other_name == name and ea['type'] == 'belongs_to':
                                ea['type'] = 'has_one'
                    removed += 1
                    continue
            kept.append(a)
        t['associations'] = kept
    return removed

# ---------------------------------------------------------------------------
# Pluralizer / inflector
# ---------------------------------------------------------------------------
