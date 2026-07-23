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


_ASSOCIATION_TYPES = {'belongs_to', 'has_one', 'has_many',
                      'has_and_belongs_to_many'}


def validate_tables_ir(tables, provider='provider', require_complete=False):
    """Validate the structural provider boundary without rejecting sparse IR.

    Framework overlays may contribute only associations while DB adapters
    normally contribute complete tables, so this checks container shapes and
    identity fields. ``require_complete`` applies the stronger DB-adapter
    contract. Returns ``tables`` unchanged for easy use at dispatch boundaries;
    raises ValueError with a provider-local path.
    """
    if not isinstance(tables, dict):
        raise ValueError(f'{provider}: tables must be a dict')
    for tname, table in tables.items():
        where = f'{provider}: table {tname!r}'
        if not isinstance(tname, str) or not tname:
            raise ValueError(f'{provider}: table names must be non-empty strings')
        if not isinstance(table, dict):
            raise ValueError(f'{where} must be a dict')
        if require_complete:
            missing = {'primary_key', 'columns', 'indexes', 'associations'} - set(table)
            if missing:
                raise ValueError(f'{where} is missing DB fields {sorted(missing)}')
        for field in ('columns', 'indexes', 'associations'):
            if field in table and not isinstance(table[field], list):
                raise ValueError(f'{where}.{field} must be a list')
        if 'comment' in table and table['comment'] is not None \
                and not isinstance(table['comment'], str):
            raise ValueError(f'{where}.comment must be a string or null')
        if 'schema_missing' in table and not isinstance(table['schema_missing'], bool):
            raise ValueError(f'{where}.schema_missing must be a boolean')
        pk = table.get('primary_key')
        if 'primary_key' in table and not (
                pk is None or isinstance(pk, str)
                or (isinstance(pk, list) and all(isinstance(x, str) for x in pk))):
            raise ValueError(f'{where}.primary_key must be a string, list of strings, or null')
        for i, col in enumerate(table.get('columns', [])):
            cwhere = f'{where}.columns[{i}]'
            if not isinstance(col, dict):
                raise ValueError(f'{cwhere} must be a dict')
            if not isinstance(col.get('name'), str) or not col['name']:
                raise ValueError(f'{cwhere}.name must be a non-empty string')
            for field in ('type', 'sql_type', 'default', 'extra'):
                if field in col and not isinstance(col[field], str):
                    raise ValueError(f'{cwhere}.{field} must be a string')
            if 'comment' in col and col['comment'] is not None \
                    and not isinstance(col['comment'], str):
                raise ValueError(f'{cwhere}.comment must be a string or null')
            for field in ('nullable', 'primary'):
                if field in col and not isinstance(col[field], bool):
                    raise ValueError(f'{cwhere}.{field} must be a boolean')
        for i, index in enumerate(table.get('indexes', [])):
            iwhere = f'{where}.indexes[{i}]'
            if not isinstance(index, dict):
                raise ValueError(f'{iwhere} must be a dict')
            if not isinstance(index.get('columns'), list) or not index['columns'] or not all(
                    isinstance(x, str) and x for x in index['columns']):
                raise ValueError(f'{iwhere}.columns must be a list of non-empty strings')
            if 'name' in index and not isinstance(index['name'], str):
                raise ValueError(f'{iwhere}.name must be a string')
            if 'unique' in index and not isinstance(index['unique'], bool):
                raise ValueError(f'{iwhere}.unique must be a boolean')
        for i, assoc in enumerate(table.get('associations', [])):
            awhere = f'{where}.associations[{i}]'
            if not isinstance(assoc, dict):
                raise ValueError(f'{awhere} must be a dict')
            if assoc.get('type') not in _ASSOCIATION_TYPES:
                raise ValueError(f'{awhere}.type must be one of '
                                 f'{sorted(_ASSOCIATION_TYPES)}')
            for field in ('name', 'target'):
                if not isinstance(assoc.get(field), str) or not assoc[field]:
                    raise ValueError(f'{awhere}.{field} must be a non-empty string')
            for field in ('foreign_key', 'through'):
                if field in assoc and not isinstance(assoc[field], str):
                    raise ValueError(f'{awhere}.{field} must be a string')
            for field in ('polymorphic', 'db_fk', 'schema_fk', 'manual', 'inferred'):
                if field in assoc and not isinstance(assoc[field], bool):
                    raise ValueError(f'{awhere}.{field} must be a boolean')
    return tables


def validate_provider_result(result, provider='provider', expected_kind=None,
                             expected_provider=None):
    """Validate a parser's ProviderResult and return it unchanged."""
    if not isinstance(result, dict):
        raise ValueError(f'{provider}: result must be a ProviderResult dict')
    if set(result) != {'source', 'tables', 'warnings'}:
        raise ValueError(f'{provider}: result must contain exactly source, tables, warnings')
    source = result['source']
    if not isinstance(source, dict):
        raise ValueError(f'{provider}: source must be a dict')
    if not isinstance(source.get('kind'), str) or not source['kind']:
        raise ValueError(f'{provider}: source.kind must be a non-empty string')
    if not isinstance(source.get('provider'), str) or not source['provider']:
        raise ValueError(f'{provider}: source.provider must be a non-empty string')
    if 'location' in source and not isinstance(source['location'], str):
        raise ValueError(f'{provider}: source.location must be a string when present')
    if expected_kind is not None and source['kind'] != expected_kind:
        raise ValueError(f'{provider}: source.kind must be {expected_kind!r}')
    if expected_provider is not None and source['provider'] != expected_provider:
        raise ValueError(f'{provider}: source.provider must be {expected_provider!r}')
    if not isinstance(result['warnings'], list) or not all(
            isinstance(x, str) for x in result['warnings']):
        raise ValueError(f'{provider}: warnings must be a list of strings')
    validate_tables_ir(result['tables'], provider)
    return result


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
