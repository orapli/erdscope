_PROVENANCE_TO_FLAG = {'db_fk': 'db_fk', 'manual': 'manual', 'inferred': 'inferred',
                       'schema_fk': 'schema_fk'}


def make_provider_result(kind, provider, tables, location=None, warnings=None):
    """Build a ProviderResult dict (§5): a Source describing where this IR came
    from, the parsed `tables` IR itself, and any non-fatal warnings. Pure — it
    only assembles the dict, never mutates `tables`. `location` (a
    password-free URL / directory / config path) is omitted from Source when
    None; `warnings` defaults to an empty list."""
    source = {'kind': kind, 'provider': provider}
    if location is not None:
        source['location'] = location
    return {'source': source, 'tables': tables,
            'warnings': list(warnings) if warnings else []}


def provenance_of(assoc):
    """Representative provenance (§9.1) for an association dict, derived from
    the legacy boolean flags it currently carries. Precedence when several
    coexist: manual > db_fk > schema_fk > inferred; a bare association (no
    flag) is 'declared'. This is the read half of the legacy<->provenance
    seam."""
    if assoc.get('manual'):
        return 'manual'
    if assoc.get('db_fk'):
        return 'db_fk'
    if assoc.get('schema_fk'):
        return 'schema_fk'
    if assoc.get('inferred'):
        return 'inferred'
    return 'declared'


def legacy_flags_for(provenance):
    """The legacy boolean flag dict to serialize for a given representative
    provenance (§9.3): db_fk/manual/inferred -> {<flag>: True}, and 'declared'
    -> {} (no badge). The write half of the seam; round-trips with
    provenance_of for each of the four provenances."""
    flag = _PROVENANCE_TO_FLAG.get(provenance)
    return {flag: True} if flag else {}


def _assoc_provenance(assoc):
    """Provenance of an association regardless of which shape it carries: the
    merged IR stores a structured `provenance` string (Step 10); a legacy-shape
    association (parser output, or a synthetic test fixture) stores boolean
    flags. Prefer the explicit provenance, else derive it from the flags. Lets
    the internals (reconcile_db_fks) treat both shapes uniformly."""
    return assoc.get('provenance') or provenance_of(assoc)

# ---------------------------------------------------------------------------
# SQL type shorthand (information_schema DATA_TYPE -> display type)
# ---------------------------------------------------------------------------
SQL_TYPES = {
    'character varying': 'string', 'varchar': 'string', 'char': 'string',
    'timestamp without time zone': 'datetime', 'timestamp with time zone': 'datetime',
    'timestamp': 'datetime', 'datetime': 'datetime',
    'time without time zone': 'time', 'time': 'time', 'date': 'date',
    'double precision': 'float', 'float': 'float', 'real': 'float',
    'numeric': 'decimal', 'decimal': 'decimal',
    'bigint': 'bigint', 'bigserial': 'bigint', 'integer': 'integer', 'int': 'integer',
    'serial': 'integer', 'smallint': 'integer', 'tinyint': 'integer', 'mediumint': 'integer',
    'text': 'text', 'longtext': 'text', 'mediumtext': 'text',
    'boolean': 'boolean', 'jsonb': 'jsonb', 'json': 'json', 'uuid': 'uuid',
    'bytea': 'binary', 'blob': 'binary', 'longblob': 'binary', 'inet': 'inet',
}
