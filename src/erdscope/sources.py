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
# InputSpec = {'id': str, 'type': str|None, 'path': Path, 'given': str}
#   type None = auto-detect. `path` is always resolved (expanduser+resolve) —
#   the one filesystem operations use. `given` is the ORIGINAL path string
#   (relative, unresolved, exactly as the user typed/configured it) — the one
#   every user-facing message (warnings, progress lines, Note lines, error
#   messages naming the source) displays, so a relative `./app/models` in the
#   config still reads that way on stderr instead of some long absolute path
#   the user never wrote.
# ---------------------------------------------------------------------------

# Static source-type registry: type name -> builder fn(spec, table_map) ->
# ProviderResult. The '<overlay.name>.models' types (rails.models,
# prisma.models, django.models, and any --adapter overlay's own) are NOT
# listed here — they're derived dynamically from FRAMEWORK_OVERLAYS in
# _source_type_builder/known_source_type_names so a newly-registered overlay
# gets a usable `sources[].type` for free, with no registry edit.
def _rails_schema_type_builder(spec, table_map):
    return rails_schema_provider(spec['path'], given=spec['given'])


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
    registry, one '<overlay.name>.models' entry per registered
    FrameworkOverlay, and the 'rails.project' macro (D4 — it never reaches
    the dispatch registry itself, since normalize_input_specs expands it
    away first, but it's still a name a user can legitimately declare). Used
    only to build "unknown type" error messages."""
    dynamic = {f'{cls.name}.models' for cls in FRAMEWORK_OVERLAYS}
    return sorted(set(SOURCE_TYPES) | dynamic | {'rails.project'})


def _join_given(root_given, *parts):
    """Join further path segments onto a user-given path STRING for display
    only — never resolved/normalized, so a relative rails.project root like
    `./railsapp` still reads `./railsapp/db/schema.rb` in the expanded
    rails.schema half's own messages, not whatever normalize_input_specs's
    early .resolve() turned the root into."""
    root = root_given.rstrip('/')
    return '/'.join((root, *parts)) if root else '/'.join(parts)


def _expand_rails_project(spec):
    """D4 macro expansion: a `rails.project` source's path is a Rails project
    root, expanded (in place, before dispatch ever sees it) into a
    'rails.schema' spec for root/db/schema.rb and/or a 'rails.models' spec
    for root/app/models — whichever exist. Neither existing is a hard error;
    exactly one existing proceeds with a stderr note naming what was
    skipped (both existing is the common case and stays silent). Messages
    name the paths via the user-given root string (`spec['given']`), not the
    resolved `spec['path']` — see the InputSpec shape note above."""
    sid, root, given_root = spec['id'], spec['path'], spec['given']
    schema_path, models_path = root / 'db' / 'schema.rb', root / 'app' / 'models'
    given_schema = _join_given(given_root, 'db', 'schema.rb')
    given_models = _join_given(given_root, 'app', 'models')
    has_schema, has_models = schema_path.is_file(), models_path.is_dir()
    if not has_schema and not has_models:
        sys.exit(f"Error: source {sid!r}: rails.project found neither {given_schema} "
                 f"nor {given_models} under {given_root}")
    expanded = []
    if has_schema:
        expanded.append({'id': f'{sid}:schema', 'type': 'rails.schema',
                         'path': schema_path, 'given': given_schema})
    else:
        print(f"Note: source {sid!r}: rails.project found no {given_schema} — "
              "skipping its rails.schema half", file=sys.stderr)
    if has_models:
        expanded.append({'id': f'{sid}:models', 'type': 'rails.models',
                         'path': models_path, 'given': given_models})
    else:
        print(f"Note: source {sid!r}: rails.project found no {given_models} — "
              "skipping its rails.models half", file=sys.stderr)
    return expanded


def normalize_input_specs(models_list, config_sources):
    """Build the deterministic, ordered InputSpec list merge_ir's layers come
    from (D4): config `sources` first, in declared order (a `rails.project`
    entry expands in place to its rails.schema/rails.models pair — see
    _expand_rails_project — so it never reaches dispatch as its own type),
    then each legacy --models / config `models` entry (id `models[<i>]`,
    type None — auto-detected at dispatch), preserving their given order.
    Later entries win same-kind merge ties (existing merge rule); CLI
    --models sorting after config `sources` is consistent with "CLI wins
    over config". Each spec keeps the user's original path STRING alongside
    the resolved Path (see the InputSpec shape note above)."""
    specs = []
    for s in config_sources:
        spec = {'id': s['id'], 'type': s['type'], 'given': s['path'],
               'path': Path(s['path']).expanduser().resolve()}
        if spec['type'] == 'rails.project':
            specs.extend(_expand_rails_project(spec))
        else:
            specs.append(spec)
    for i, m in enumerate(models_list):
        specs.append({'id': f'models[{i}]', 'type': None, 'given': m,
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
    sid, stype, path, given = spec['id'], spec['type'], spec['path'], spec['given']
    builder = _source_type_builder(stype)
    if builder is None:
        sys.exit(f"Error: source {sid!r}: unknown type {stype!r} "
                 f"(known types: {', '.join(known_source_type_names())})")
    if not path.exists():
        sys.exit(f"Error: source {sid!r}: {given} does not exist")
    if stype in _FILE_SOURCE_TYPES and not path.is_file():
        sys.exit(f"Error: source {sid!r}: {stype} expects {_FILE_SOURCE_TYPES[stype]}, "
                 f"got {given}")
    result = builder(spec, table_map)
    print(f'Merged {result["source"]["provider"]} {_progress_noun(result)} from {given}',
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
    sid, path, given = spec['id'], spec['path'], spec['given']
    if not path.exists():
        sys.exit(f'Error: {given} does not exist')
    if path.is_file() and path.name == 'schema.rb':
        print(f'Note: {given} auto-detected as rails.schema (declare it in config '
              'sources to make this explicit)', file=sys.stderr)
        result = rails_schema_provider(path, given=given)
        print(f'Merged {result["source"]["provider"]} {_progress_noun(result)} from {given}',
              file=sys.stderr)
        return result
    matches = framework_overlays_matching(path)
    if not matches:
        sys.exit(f'Error: could not detect the code kind at {given} (expected a Rails '
                 'app/models dir, a schema.prisma, a Django project, or a db/schema.rb '
                 'file — declare `sources[].type` in the config to be explicit)')
    winner = matches[0]
    if len(matches) > 1:
        names = ', '.join(c.name for c in matches)
        print(f'Note: {given} matched multiple frameworks ({names}); using {winner.name}. '
              f'Declare sources[].type in the config to override.', file=sys.stderr)
    result = winner().build(path, table_map)
    print(f'Merged {result["source"]["provider"]} {_progress_noun(result)} from {given}',
          file=sys.stderr)
    return result
