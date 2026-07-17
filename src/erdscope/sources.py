# ---------------------------------------------------------------------------
# Input sources — InputSpec normalization + source-type registry/dispatch.
#
# Every code-source input (legacy --models, config `models`, and the typed
# config `sources` list) normalizes to a common, ordered list of InputSpec
# dicts, which then run through a small source-type registry to produce the
# ProviderResult layers merge_ir folds. A typed source (config `sources[].type`)
# skips detection entirely and calls the named type's builder directly; an
# untyped source (legacy --models / config `models`) keeps today's
# auto-detection behavior, with ambiguity/ note-worthy detections reported to
# stderr instead of resolved silently.
#
# InputSpec = {'id': str, 'type': str|None, 'path': Path}   # type None = auto-detect
# ---------------------------------------------------------------------------

# Static source-type registry: type name -> builder fn(spec, table_map) ->
# ProviderResult. The '<overlay.name>.models' types (rails.models,
# prisma.models, django.models, and any --adapter overlay's own) are NOT
# listed here — they're derived dynamically from FRAMEWORK_OVERLAYS in
# _source_type_builder/known_source_type_names so a newly-registered overlay
# gets a usable `sources[].type` for free, with no registry edit.
def _rails_schema_type_builder(spec, table_map):
    return rails_schema_provider(spec['path'])


SOURCE_TYPES = {
    'rails.schema': _rails_schema_type_builder,
}

# Source types whose path must be an existing FILE, not a directory (D4) —
# checked in _run_typed_spec before the builder runs, with a type-specific
# message (a directory is the mistake someone makes when they meant the
# containing project, or copy-pasted a `rails.models` path by habit).
_FILE_SOURCE_TYPES = {'rails.schema': 'a schema.rb file'}


def _models_type_builder(overlay_cls):
    def build(spec, table_map):
        return overlay_cls().build(spec['path'], table_map)
    return build


def _source_type_builder(type_name):
    """Resolve a sources[].type name to its builder fn(spec, table_map) ->
    ProviderResult, or None if the name isn't (yet) registered."""
    if type_name in SOURCE_TYPES:
        return SOURCE_TYPES[type_name]
    for cls in FRAMEWORK_OVERLAYS:
        if f'{cls.name}.models' == type_name:
            return _models_type_builder(cls)
    return None


def known_source_type_names():
    """Every currently valid sources[].type value, sorted — the static
    registry plus one '<overlay.name>.models' entry per registered
    FrameworkOverlay. Used only to build "unknown type" error messages."""
    dynamic = {f'{cls.name}.models' for cls in FRAMEWORK_OVERLAYS}
    return sorted(set(SOURCE_TYPES) | dynamic)


def normalize_input_specs(models_list, config_sources):
    """Build the deterministic, ordered InputSpec list merge_ir's layers come
    from (D4): config `sources` first, in declared order, then each legacy
    --models / config `models` entry (id `models[<i>]`, type None — auto-
    detected at dispatch), preserving their given order. Later entries win
    same-kind merge ties (existing merge rule); CLI --models sorting after
    config `sources` is consistent with "CLI wins over config"."""
    specs = []
    for s in config_sources:
        specs.append({'id': s['id'], 'type': s['type'],
                      'path': Path(s['path']).expanduser().resolve()})
    for i, m in enumerate(models_list):
        specs.append({'id': f'models[{i}]', 'type': None,
                      'path': Path(m).expanduser().resolve()})
    return specs


def run_input_specs(specs, table_map):
    """Dispatch every InputSpec (in order) to its ProviderResult, printing a
    per-source progress line and forwarding every warning the provider
    returns to stderr (D4 — the first real consumer of ProviderResult
    warnings)."""
    results = []
    for spec in specs:
        result = (_run_typed_spec(spec, table_map) if spec['type'] is not None
                  else _run_untyped_spec(spec, table_map))
        for w in result['warnings']:
            print(f'Warning: {w}', file=sys.stderr)
        results.append(result)
    return results


def _run_typed_spec(spec, table_map):
    sid, stype, path = spec['id'], spec['type'], spec['path']
    builder = _source_type_builder(stype)
    if builder is None:
        sys.exit(f"Error: source {sid!r}: unknown type {stype!r} "
                 f"(known types: {', '.join(known_source_type_names())})")
    if not path.exists():
        sys.exit(f"Error: source {sid!r}: {path} does not exist")
    if stype in _FILE_SOURCE_TYPES and not path.is_file():
        sys.exit(f"Error: source {sid!r}: {stype} expects {_FILE_SOURCE_TYPES[stype]}, "
                 f"got {path}")
    result = builder(spec, table_map)
    print(f'Merged {result["source"]["provider"]} {_progress_noun(result)} from {path}',
          file=sys.stderr)
    return result


def _progress_noun(result):
    """D4's per-source progress line uses 'tables' for a schema-kind layer
    (rails.schema's contribution is columns/indexes/PK, not code semantics)
    and keeps the existing 'associations' wording for every other kind
    (framework layers — tests may match this exact phrase)."""
    return 'tables' if result['source']['kind'] == 'schema' else 'associations'


def _run_untyped_spec(spec, table_map):
    """Legacy --models / config `models` auto-detection (today's behavior),
    plus ambiguity reporting (D4b/c): when more than one FrameworkOverlay
    matches, the winner is unchanged (framework_overlay_for's own priority
    order) but now a stderr note names the runner-up(s) and points at
    `sources[].type` as the way to pin it down explicitly."""
    sid, path = spec['id'], spec['path']
    if not path.exists():
        sys.exit(f'Error: {path} does not exist')
    if path.is_file() and path.name == 'schema.rb':
        print(f'Note: {path} auto-detected as rails.schema (declare it in config '
              'sources to make this explicit)', file=sys.stderr)
        result = rails_schema_provider(path)
        print(f'Merged {result["source"]["provider"]} {_progress_noun(result)} from {path}',
              file=sys.stderr)
        return result
    matches = framework_overlays_matching(path)
    if not matches:
        sys.exit(f'Error: could not detect the code kind at {path} (expected a Rails '
                 'app/models dir, a schema.prisma, a Django project, or a db/schema.rb '
                 'file — declare `sources[].type` in the config to be explicit)')
    winner = matches[0]
    if len(matches) > 1:
        names = ', '.join(c.name for c in matches)
        print(f'Note: {path} matched multiple frameworks ({names}); using {winner.name}. '
              f'Declare sources[].type in the config to override.', file=sys.stderr)
    result = winner().build(path, table_map)
    print(f'Merged {result["source"]["provider"]} {_progress_noun(result)} from {path}',
          file=sys.stderr)
    return result
