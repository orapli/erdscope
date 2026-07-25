"""notes Phase 1 — the config `notes:` design-documentation sidecar.

Covers what tests/test_config_validation.py deliberately does NOT (purely
syntactic checks live there): semantic validation + viewer resolution
(providers.resolve_and_validate_notes) against a final merged IR, the
config->CLI wiring (two-stage validation against config.tables add/drop,
DATA_JSON serialization), the demo/back-compat byte-shape guarantee (no
`notes` key when there are no notes), and the DATA_JSON-level half of the
XSS defense (the viewer-JS half — esc()/escMark() actually neutralizing
`<b>`/`"onx` at render time — is exercised for real in tests/test_e2e.py's
TestNotes, which drives a real headless browser).

Run from the repository root:
    python3 -m unittest tests.test_notes -v
"""
import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_RAILS = Path(__file__).resolve().parent / 'fixture_app'

spec = importlib.util.spec_from_file_location('erd', ROOT / 'erd.py')
erd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(erd)


def _tables(*, posts_assocs=None, extra=None):
    """A minimal hand-built final-IR shape (users, posts belongs_to users on
    user_id) — resolve_and_validate_notes only reads `tables[t]['columns'/
    'associations']`, so there's no need to route this through merge_ir for
    tests that are purely about note resolution."""
    t = {
        'users': {'columns': [{'name': 'id'}], 'associations': []},
        'posts': {'columns': [{'name': 'id'}, {'name': 'user_id'}],
                  'associations': posts_assocs if posts_assocs is not None else [
                      {'type': 'belongs_to', 'name': 'user',
                       'target': 'users', 'foreign_key': 'user_id'}]},
    }
    if extra:
        t.update(extra)
    return t


# ---------------------------------------------------------------------------
# resolve_and_validate_notes — direct unit tests (no CLI/pipeline involved)
# ---------------------------------------------------------------------------
class TestResolveGlobalNotes(unittest.TestCase):
    def test_global_note_always_allowed(self):
        notes = [{'id': 'g1', 'target': {'type': 'global'}, 'text': 'hello'}]
        out = erd.resolve_and_validate_notes(notes, _tables(), 'cfg')
        self.assertEqual(out, [{'id': 'g1', 'scope': 'global', 'text': 'hello'}])

    def test_global_note_with_title_and_links_passthrough(self):
        notes = [{'id': 'g1', 'target': {'type': 'global'}, 'title': 'T', 'text': 'hi',
                  'links': [{'label': 'L', 'url': 'https://x'}]}]
        out = erd.resolve_and_validate_notes(notes, _tables(), 'cfg')
        self.assertEqual(out[0]['title'], 'T')
        self.assertEqual(out[0]['links'], [{'label': 'L', 'url': 'https://x'}])

    def test_global_note_never_needs_a_matching_table(self):
        # empty schema — global is still fine, unlike table/relation
        out = erd.resolve_and_validate_notes(
            [{'id': 'g1', 'target': {'type': 'global'}, 'text': 'hi'}], {}, 'cfg')
        self.assertEqual(out[0]['scope'], 'global')


class TestResolveTableNotes(unittest.TestCase):
    def test_existing_table_resolves(self):
        notes = [{'id': 't1', 'target': {'type': 'table', 'table': 'users'}, 'text': 'hi'}]
        out = erd.resolve_and_validate_notes(notes, _tables(), 'cfg')
        self.assertEqual(out, [{'id': 't1', 'scope': 'table', 'table': 'users', 'text': 'hi'}])

    def test_unknown_table_errors_and_includes_note_id(self):
        notes = [{'id': 't1', 'target': {'type': 'table', 'table': 'ghosts'}, 'text': 'hi'}]
        with self.assertRaises(SystemExit) as cm:
            erd.resolve_and_validate_notes(notes, _tables(), 'cfg')
        msg = str(cm.exception)
        self.assertIn('t1', msg)
        self.assertIn('ghosts', msg)


class TestResolveRelationNotes(unittest.TestCase):
    def _note(self, **target_overrides):
        target = {'type': 'relation', 'source_table': 'posts', 'target_table': 'users'}
        target.update(target_overrides)
        return {'id': 'r1', 'target': target, 'text': 'hi'}

    def test_unique_match_resolves_with_full_resolved_identity(self):
        out = erd.resolve_and_validate_notes([self._note()], _tables(), 'cfg')
        self.assertEqual(out[0], {
            'id': 'r1', 'scope': 'relation', 'source_table': 'posts', 'target': 'users',
            'type': 'belongs_to', 'name': 'user', 'foreign_key': 'user_id', 'through': None,
            'polymorphic': False, 'text': 'hi'})

    def test_unknown_source_table_errors_and_includes_note_id(self):
        with self.assertRaises(SystemExit) as cm:
            erd.resolve_and_validate_notes([self._note(source_table='ghosts')], _tables(), 'cfg')
        self.assertIn('r1', str(cm.exception))

    def test_no_matching_relation_errors_and_includes_note_id(self):
        with self.assertRaises(SystemExit) as cm:
            erd.resolve_and_validate_notes([self._note(target_table='ghosts')], _tables(), 'cfg')
        msg = str(cm.exception)
        self.assertIn('r1', msg)
        self.assertIn('no relation', msg)

    def _two_aliases(self):
        # the Rails alias pattern: two belongs_to from posts to users, on
        # different FK columns — foreign_key/name-blind narrowing is ambiguous
        return _tables(posts_assocs=[
            {'type': 'belongs_to', 'name': 'user', 'target': 'users', 'foreign_key': 'user_id'},
            {'type': 'belongs_to', 'name': 'author', 'target': 'users', 'foreign_key': 'author_id'},
        ])

    def test_ambiguous_relation_errors_and_includes_note_id(self):
        with self.assertRaises(SystemExit) as cm:
            erd.resolve_and_validate_notes([self._note()], self._two_aliases(), 'cfg')
        msg = str(cm.exception)
        self.assertIn('r1', msg)
        self.assertIn('ambiguous', msg)

    def test_foreign_key_narrows_an_ambiguous_match(self):
        out = erd.resolve_and_validate_notes(
            [self._note(foreign_key='author_id')], self._two_aliases(), 'cfg')
        self.assertEqual(out[0]['name'], 'author')

    def test_name_narrows_an_ambiguous_match(self):
        out = erd.resolve_and_validate_notes(
            [self._note(name='author')], self._two_aliases(), 'cfg')
        self.assertEqual(out[0]['foreign_key'], 'author_id')

    def test_through_narrows_an_ambiguous_through_match(self):
        # Fable review point 1: through/polymorphic as narrowing keys —
        # two has_many :commenters, through different join tables
        tables = _tables(posts_assocs=[
            {'type': 'has_many', 'name': 'commenters', 'target': 'users', 'through': 'comments'},
            {'type': 'has_many', 'name': 'commenters', 'target': 'users', 'through': 'reviews'},
        ])
        out = erd.resolve_and_validate_notes(
            [self._note(name='commenters', through='comments')], tables, 'cfg')
        self.assertEqual(out[0]['through'], 'comments')

    def test_polymorphic_true_narrows_to_the_polymorphic_association(self):
        tables = _tables(posts_assocs=[
            {'type': 'belongs_to', 'name': 'owner', 'target': 'users', 'foreign_key': 'owner_id'},
            {'type': 'belongs_to', 'name': 'owner', 'target': 'users', 'foreign_key': 'owner_id',
             'polymorphic': True},
        ])
        out = erd.resolve_and_validate_notes(
            [self._note(name='owner', foreign_key='owner_id', polymorphic=True)], tables, 'cfg')
        self.assertTrue(out[0]['polymorphic'])

    def test_ambiguous_still_errors_when_narrowing_keys_are_absent(self):
        # sanity: without foreign_key/name, the two aliases above stay ambiguous
        # even though the *target* is identical — this is the "曖昧ならエラー"
        # contract requirement, not a bug in the two-alias fixture
        with self.assertRaises(SystemExit):
            erd.resolve_and_validate_notes([self._note()], self._two_aliases(), 'cfg')

    # -- Sol finding #5: association_key parity (assoc_type + tri-state
    #    polymorphic narrowing), and `type` always present on the resolved
    #    relation entry (the viewer contract) --------------------------------

    def _same_name_has_many_and_has_one(self):
        # same name + same target, only the association TYPE differs — role
        # (has_many vs has_one) is the only thing that can disambiguate this,
        # since foreign_key/name/through/polymorphic are all identical (and,
        # for has_many/has_one, absent)
        return _tables(posts_assocs=[
            {'type': 'has_many', 'name': 'comments', 'target': 'users'},
            {'type': 'has_one', 'name': 'comments', 'target': 'users'},
        ])

    def test_same_name_and_target_has_many_vs_has_one_is_ambiguous_without_assoc_type(self):
        with self.assertRaises(SystemExit) as cm:
            erd.resolve_and_validate_notes(
                [self._note(name='comments')], self._same_name_has_many_and_has_one(), 'cfg')
        self.assertIn('ambiguous', str(cm.exception))

    def test_assoc_type_narrows_has_many_vs_has_one(self):
        tables = self._same_name_has_many_and_has_one()
        out = erd.resolve_and_validate_notes(
            [self._note(name='comments', assoc_type='has_many')], tables, 'cfg')
        self.assertEqual(out[0]['type'], 'has_many')
        out = erd.resolve_and_validate_notes(
            [self._note(name='comments', assoc_type='has_one')], tables, 'cfg')
        self.assertEqual(out[0]['type'], 'has_one')

    def _polymorphic_pair(self):
        return _tables(posts_assocs=[
            {'type': 'belongs_to', 'name': 'owner', 'target': 'users', 'foreign_key': 'owner_id'},
            {'type': 'belongs_to', 'name': 'owner', 'target': 'users', 'foreign_key': 'owner_id',
             'polymorphic': True},
        ])

    def test_polymorphic_false_narrows_to_the_non_polymorphic_association(self):
        # Previously `polymorphic` only narrowed when explicitly True — a
        # `false` in config was silently ignored as a filter, so this case
        # stayed ambiguous even with the disambiguating key present.
        out = erd.resolve_and_validate_notes(
            [self._note(name='owner', foreign_key='owner_id', polymorphic=False)],
            self._polymorphic_pair(), 'cfg')
        self.assertFalse(out[0]['polymorphic'])

    def test_polymorphic_true_still_narrows_to_the_polymorphic_association(self):
        # regression guard alongside the new tri-state behavior above
        out = erd.resolve_and_validate_notes(
            [self._note(name='owner', foreign_key='owner_id', polymorphic=True)],
            self._polymorphic_pair(), 'cfg')
        self.assertTrue(out[0]['polymorphic'])

    def test_ambiguous_without_polymorphic_narrowing_key(self):
        # sanity: the pair above IS genuinely ambiguous without `polymorphic`
        with self.assertRaises(SystemExit):
            erd.resolve_and_validate_notes(
                [self._note(name='owner', foreign_key='owner_id')],
                self._polymorphic_pair(), 'cfg')

    def test_resolved_relation_entry_always_includes_type(self):
        # the viewer shared contract: `type` is the resolved association's
        # REAL type, always present — not just when used to narrow
        out = erd.resolve_and_validate_notes([self._note()], _tables(), 'cfg')
        self.assertEqual(out[0]['type'], 'belongs_to')


# ---------------------------------------------------------------------------
# Full CLI pipeline wiring: two-stage validation, serialize, XSS, demo parity
# ---------------------------------------------------------------------------
class _NoDBDriver(unittest.TestCase):
    """Drive main() with NO database url — same technique as
    test_pipeline._NoDBDriver — so notes-only / config+framework runs never
    try to open a real connection."""
    def setUp(self):
        def _no_db(url):
            raise AssertionError('DB should not be contacted in a notes-only/config test')
        self._orig = (erd.parse_mysql, erd.parse_postgres)
        erd.parse_mysql = _no_db
        erd.parse_postgres = _no_db
        self._argv = sys.argv
        import os
        self._cwd = os.getcwd()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: setattr(sys, 'argv', self._argv))
        self.addCleanup(os.chdir, self._cwd)
        self.addCleanup(lambda: (setattr(erd, 'parse_mysql', self._orig[0]),
                                 setattr(erd, 'parse_postgres', self._orig[1])))
        os.chdir(self.tmp.name)  # empty cwd -> no .erdscope auto-discovery

    def _p(self, name):
        return str(Path(self.tmp.name) / name)

    def _run(self, *argv):
        sys.argv = ['erd.py', *argv]
        erd.main()

    def _data(self, path):
        return json.loads(re.search(r'const DATA = (\{.*?\});\s*\n',
                                    Path(path).read_text()).group(1))


class TestNotesTwoStageValidation(_NoDBDriver):
    """§6.4①/② mirrored for notes: syntax at load time (test_config_validation),
    semantic validation against the FINAL merged IR — after config.tables
    add/drop — at apply time."""

    def test_note_on_a_table_added_by_config_tables_resolves(self):
        # the note's target table doesn't exist anywhere except config.tables
        # itself — must still resolve, since validation runs on the FINAL IR
        cfg = {'tables': {'settings': {'columns': [{'name': 'id', 'primary': True}]}},
               'notes': [{'id': 'n1', 'target': {'type': 'table', 'table': 'settings'},
                         'text': 'hi'}]}
        path = self._p('c.json')
        Path(path).write_text(json.dumps(cfg))
        out = self._p('out.html')
        self._run('--config', path, '-o', out)
        data = self._data(out)
        self.assertEqual(data['notes'], [{'id': 'n1', 'scope': 'table',
                                          'table': 'settings', 'text': 'hi'}])

    def test_note_on_a_relation_added_by_config_tables_resolves(self):
        cfg = {'tables': {
                   'orders': {'columns': [{'name': 'id', 'primary': True}, {'name': 'user_id'}],
                              'associations': [{'type': 'belongs_to', 'name': 'user',
                                                'target': 'users', 'foreign_key': 'user_id'}]},
                   'users': {'columns': [{'name': 'id', 'primary': True}]}},
               'notes': [{'id': 'n1', 'target': {
                             'type': 'relation', 'source_table': 'orders',
                             'target_table': 'users', 'foreign_key': 'user_id'},
                         'text': 'hi'}]}
        path = self._p('c.json')
        Path(path).write_text(json.dumps(cfg))
        out = self._p('out.html')
        self._run('--config', path, '-o', out)
        data = self._data(out)
        self.assertEqual(data['notes'][0]['name'], 'user')

    def test_note_targeting_a_table_dropped_by_config_errors_against_final_ir(self):
        # 'webhooks' comes from the Rails fixture (a real lower layer);
        # config.tables drops it, so the note's target no longer exists in
        # the FINAL merged IR even though it existed in a raw layer
        cfg = {'models': str(FIXTURE_RAILS),
               'tables': {'webhooks': {'drop': True}},
               'notes': [{'id': 'n1', 'target': {'type': 'table', 'table': 'webhooks'},
                         'text': 'hi'}]}
        path = self._p('c.json')
        Path(path).write_text(json.dumps(cfg))
        out = self._p('out.html')
        with self.assertRaises(SystemExit) as cm:
            self._run('--config', path, '-o', out)
        msg = str(cm.exception)
        self.assertIn('n1', msg)
        self.assertIn('webhooks', msg)

    def test_note_on_a_table_config_leaves_untouched_still_resolves_alongside_a_drop(self):
        # regression guard: a drop elsewhere in the SAME config.tables run
        # must not affect a note on an unrelated, still-present table
        cfg = {'models': str(FIXTURE_RAILS),
               'tables': {'webhooks': {'drop': True}},
               'notes': [{'id': 'n1', 'target': {'type': 'table', 'table': 'users'},
                         'text': 'hi'}]}
        path = self._p('c.json')
        Path(path).write_text(json.dumps(cfg))
        out = self._p('out.html')
        self._run('--config', path, '-o', out)
        data = self._data(out)
        self.assertNotIn('webhooks', data['tables'])
        self.assertEqual(data['notes'][0]['table'], 'users')


class TestNotesOnlyExcludeFiltering(_NoDBDriver):
    """Sol finding #1: --only/--exclude must not leak an excluded table's
    design notes into the HTML. table/relation notes whose target didn't
    survive filtering are dropped from DATA.notes; global notes always
    survive (they're diagram-wide, not tied to any one table)."""

    def _cfg(self):
        return {'tables': {
                    'users': {'columns': [{'name': 'id', 'primary': True}]},
                    'secret': {'columns': [{'name': 'id', 'primary': True}]},
                    'orders': {'columns': [{'name': 'id', 'primary': True},
                                           {'name': 'user_id'}],
                               'associations': [{'type': 'belongs_to', 'name': 'user',
                                                 'target': 'users', 'foreign_key': 'user_id'}]}},
                'notes': [
                    {'id': 'g1', 'target': {'type': 'global'}, 'text': 'global note'},
                    {'id': 't-secret', 'target': {'type': 'table', 'table': 'secret'},
                     'text': 'secret table note'},
                    {'id': 't-users', 'target': {'type': 'table', 'table': 'users'},
                     'text': 'users table note'},
                    {'id': 'r-orders-users', 'target': {
                        'type': 'relation', 'source_table': 'orders',
                        'target_table': 'users', 'foreign_key': 'user_id'},
                     'text': 'orders->users note'}]}

    def test_exclude_drops_table_and_relation_notes_for_the_excluded_table(self):
        path = self._p('c.json')
        Path(path).write_text(json.dumps(self._cfg()))
        out = self._p('out.html')
        self._run('--config', path, '-o', out, '--exclude', 'secret', '--exclude', 'orders')
        data = self._data(out)
        self.assertNotIn('secret', data['tables'])
        self.assertNotIn('orders', data['tables'])
        ids = {n['id'] for n in data['notes']}
        # global always survives; the secret table note and the orders->users
        # relation note (source_table excluded) must both be gone
        self.assertEqual(ids, {'g1', 't-users'})

    def test_only_keeps_notes_for_surviving_tables_alone(self):
        path = self._p('c.json')
        Path(path).write_text(json.dumps(self._cfg()))
        out = self._p('out.html')
        self._run('--config', path, '-o', out, '--only', 'users')
        data = self._data(out)
        self.assertEqual(set(data['tables']), {'users'})
        ids = {n['id'] for n in data['notes']}
        self.assertEqual(ids, {'g1', 't-users'})

    def test_relation_note_dropped_when_only_its_target_endpoint_is_excluded(self):
        # Sol re-review #2: a relation note ships only when BOTH endpoints
        # survive. `--only orders` keeps the source_table (orders) but drops
        # the target (users), so the orders->users note — whose body and
        # `target: users` would otherwise leak into a users-less HTML — must
        # be dropped too. (Filtering on source_table alone used to keep it.)
        path = self._p('c.json')
        Path(path).write_text(json.dumps(self._cfg()))
        out = self._p('out.html')
        self._run('--config', path, '-o', out, '--only', 'orders')
        data = self._data(out)
        self.assertEqual(set(data['tables']), {'orders'})
        ids = {n['id'] for n in data['notes']}
        self.assertNotIn('r-orders-users', ids)   # target 'users' excluded
        self.assertEqual(ids, {'g1'})             # only the global note remains

    def test_global_note_survives_even_when_no_table_note_does(self):
        cfg = {'tables': {'users': {'columns': [{'name': 'id', 'primary': True}]},
                          'secret': {'columns': [{'name': 'id', 'primary': True}]}},
               'notes': [{'id': 'g1', 'target': {'type': 'global'}, 'text': 'hi'},
                        {'id': 't1', 'target': {'type': 'table', 'table': 'secret'},
                         'text': 'bye'}]}
        path = self._p('c.json')
        Path(path).write_text(json.dumps(cfg))
        out = self._p('out.html')
        self._run('--config', path, '-o', out, '--exclude', 'secret')
        data = self._data(out)
        self.assertEqual([n['id'] for n in data['notes']], ['g1'])

    def test_no_filtering_flags_keeps_every_note(self):
        # sanity/no-op guard: without --only/--exclude, nothing is dropped
        path = self._p('c.json')
        Path(path).write_text(json.dumps(self._cfg()))
        out = self._p('out.html')
        self._run('--config', path, '-o', out)
        data = self._data(out)
        ids = {n['id'] for n in data['notes']}
        self.assertEqual(ids, {'g1', 't-secret', 't-users', 'r-orders-users'})


class TestNotesInferFkTargeting(_NoDBDriver):
    """Sol finding #3: notes resolution must run AFTER --infer-fk, so a note
    can target a relation that only exists because --infer-fk added it (a
    real DB/framework/config association never backed it)."""

    def _cfg(self):
        # activity_logs.user_id has no declared association anywhere — only
        # --infer-fk turns it into a real belongs_to edge to `users`
        return {'tables': {
                    'users': {'columns': [{'name': 'id', 'primary': True}]},
                    'activity_logs': {'columns': [{'name': 'id', 'primary': True},
                                                  {'name': 'user_id'}]}},
                'notes': [{'id': 'n1', 'target': {
                              'type': 'relation', 'source_table': 'activity_logs',
                              'target_table': 'users', 'foreign_key': 'user_id'},
                          'text': 'inferred-relation note'}]}

    def test_note_on_an_infer_fk_relation_resolves_with_infer_fk_on(self):
        path = self._p('c.json')
        Path(path).write_text(json.dumps(self._cfg()))
        out = self._p('out.html')
        self._run('--config', path, '-o', out, '--infer-fk')
        data = self._data(out)
        self.assertEqual(data['notes'][0]['source_table'], 'activity_logs')
        self.assertEqual(data['notes'][0]['target'], 'users')
        self.assertEqual(data['notes'][0]['type'], 'belongs_to')

    def test_same_note_errors_without_infer_fk(self):
        # sanity: without --infer-fk the relation genuinely doesn't exist, so
        # this must still be a "no relation" error, not silently pass
        path = self._p('c.json')
        Path(path).write_text(json.dumps(self._cfg()))
        out = self._p('out.html')
        with self.assertRaises(SystemExit) as cm:
            self._run('--config', path, '-o', out)
        msg = str(cm.exception)
        self.assertIn('n1', msg)
        self.assertIn('no relation', msg)


class TestNotesSerializeAndDemoParity(_NoDBDriver):
    def _schema(self):
        return {'tables': {'t': {'columns': [{'name': 'id', 'primary': True}]}}}

    def test_no_notes_key_in_config_omits_notes_from_data_json(self):
        # the demo-byte-equality guardrail (§10.1): a config with no `notes`
        # at all must produce EXACTLY today's DATA_JSON shape
        path = self._p('c.json')
        Path(path).write_text(json.dumps(self._schema()))
        out = self._p('out.html')
        self._run('--config', path, '-o', out)
        self.assertNotIn('notes', self._data(out))

    def test_empty_notes_list_also_omits_the_key(self):
        cfg = self._schema()
        cfg['notes'] = []
        path = self._p('c.json')
        Path(path).write_text(json.dumps(cfg))
        out = self._p('out.html')
        self._run('--config', path, '-o', out)
        self.assertNotIn('notes', self._data(out))

    def test_configured_notes_appear_in_data_json(self):
        cfg = self._schema()
        cfg['notes'] = [{'id': 'n1', 'target': {'type': 'global'}, 'text': 'hi'}]
        path = self._p('c.json')
        Path(path).write_text(json.dumps(cfg))
        out = self._p('out.html')
        self._run('--config', path, '-o', out)
        self.assertEqual(self._data(out)['notes'],
                         [{'id': 'n1', 'scope': 'global', 'text': 'hi'}])

    def test_embedded_script_close_tag_never_breaks_out_of_the_data_script_element(self):
        cfg = self._schema()
        cfg['notes'] = [{'id': 'n1', 'target': {'type': 'global'},
                         'text': 'boom </script><script>alert(1)</script>'}]
        path = self._p('c.json')
        Path(path).write_text(json.dumps(cfg))
        out = self._p('out.html')
        self._run('--config', path, '-o', out)
        html = Path(out).read_text()
        self.assertNotIn('</script><script>alert', html)
        self.assertIn('<\\/script>', html)  # same existing DATA_JSON `</` -> `<\/` guard
        # escaping is presentation-only: the JSON payload round-trips to the
        # original text once parsed back out
        self.assertEqual(self._data(out)['notes'][0]['text'],
                         'boom </script><script>alert(1)</script>')

    def test_html_special_characters_survive_into_data_json_unmodified(self):
        # config.py does no markdown/HTML sanitizing of note text — the
        # viewer's esc()/escMark() at RENDER time is the only place `<b>`/
        # `"onx` are neutralized (verified live by tests/test_e2e.py's
        # TestNotes against a real headless browser); the JSON payload must
        # carry the literal text unmodified except for the `</` guard above
        cfg = self._schema()
        payload_text = '<b>bold</b> "onx=alert(1)"'
        cfg['notes'] = [{'id': 'n1', 'target': {'type': 'global'}, 'text': payload_text}]
        path = self._p('c.json')
        Path(path).write_text(json.dumps(cfg))
        out = self._p('out.html')
        self._run('--config', path, '-o', out)
        self.assertEqual(self._data(out)['notes'][0]['text'], payload_text)


class TestNotesBackwardCompatRegression(_NoDBDriver):
    """§7 regression: framework-only / config-only pipelines with no `notes`
    at all behave exactly as before (no `notes` key, existing tables/
    associations unaffected)."""

    def test_framework_only_with_no_notes_config_key_is_unaffected(self):
        out = self._p('out.html')
        self._run('--models', str(FIXTURE_RAILS), '--no-config', '-o', out)
        data = self._data(out)
        self.assertNotIn('notes', data)
        self.assertIn('users', data['tables'])

    def test_excel_has_a_notes_sheet_when_notes_are_present(self):
        # backlog #4: notes now activate a Notes sheet in the workbook
        import zipfile
        cfg = {'tables': {'t': {'columns': [{'name': 'id', 'primary': True}]}},
               'notes': [{'id': 'n1', 'target': {'type': 'global'}, 'text': 'hi'}]}
        path = self._p('c.json')
        Path(path).write_text(json.dumps(cfg))
        out, xlsx = self._p('out.html'), self._p('defs.xlsx')
        self._run('--config', path, '-o', out, '--excel', xlsx)
        self.assertTrue(Path(xlsx).exists())
        with zipfile.ZipFile(xlsx) as z:
            wb = z.read('xl/workbook.xml').decode('utf-8')
            sheet_names = re.findall(r'<sheet name="([^"]+)"', wb)
            self.assertIn('Notes', sheet_names)
            notes_idx = sheet_names.index('Notes')
            sheet_xml = z.read(f'xl/worksheets/sheet{notes_idx + 1}.xml').decode('utf-8')
        self.assertIn('hi', sheet_xml)
        self.assertIn('global', sheet_xml)

    def test_excel_has_no_notes_sheet_when_there_are_no_notes(self):
        import zipfile
        cfg = {'tables': {'t': {'columns': [{'name': 'id', 'primary': True}]}}}
        path = self._p('c.json')
        Path(path).write_text(json.dumps(cfg))
        out, xlsx = self._p('out.html'), self._p('defs.xlsx')
        self._run('--config', path, '-o', out, '--excel', xlsx)
        with zipfile.ZipFile(xlsx) as z:
            names = z.namelist()
        self.assertFalse(any('notes' in n.lower() for n in names))


class TestNoteEditorAndConfigExportUIContract(unittest.TestCase):
    def setUp(self):
        table_rows = [('users', ''), ('posts', '')]
        col_rows = [
            ('users', 'id', 'int', 'int', 'NO', 'PRI', '', '', ''),
            ('posts', 'id', 'int', 'int', 'NO', 'PRI', '', '', ''),
            ('posts', 'user_id', 'int', 'int', 'NO', 'MUL', '', '', ''),
        ]
        fk_rows = [('posts', 'user_id', 'users')]
        self.tables = erd.mysql_ir(table_rows, col_rows, fk_rows, [])
        notes = [{'id': 'n1', 'target': {'type': 'table', 'table': 'users'}, 'text': 'Original note'}]

        tmp = tempfile.mkdtemp()
        self.out_path = Path(tmp) / 'out.html'
        args = type('Args', (), {'output': str(self.out_path), 'models': None, 'excel': None, 'max_rows': 15, 'only': None, 'exclude': None, 'infer_fk': False})()
        erd._finish(self.tables, args, 'note_edit_test', notes=notes)
        with open(self.out_path, 'r', encoding='utf-8') as f:
            self.html = f.read()

    def test_note_edit_modal_elements_exist(self):
        self.assertIn('id="note-edit-modal"', self.html)
        self.assertIn('id="note-edit-scope"', self.html)
        self.assertIn('id="note-edit-table"', self.html)
        self.assertIn('id="note-edit-title-val"', self.html)
        self.assertIn('id="note-edit-text-val"', self.html)
        self.assertIn('id="btn-note-save"', self.html)

    def test_note_action_buttons_and_config_export_exist(self):
        self.assertIn('data-note-edit', self.html)
        self.assertIn('data-note-delete', self.html)
        self.assertIn('btn-add-table-note', self.html)
        self.assertIn('btn-export-config-copy', self.html)
        self.assertIn('btn-export-config-download', self.html)
        self.assertIn('exportConfigJSON', self.html)
        self.assertIn('saveNoteFromModal', self.html)
        self.assertIn('deleteNote', self.html)
        self.assertIn('note-target-source-group', self.html)
        self.assertIn('note-target-rel-table-group', self.html)
        self.assertIn('note-target-fk-group', self.html)

    def test_config_export_roundtrip_validates_against_python_config_validator(self):
        import subprocess
        js_code = f"""
        const fs = require('fs');
        const html = fs.readFileSync({json.dumps(str(self.out_path))}, 'utf-8');
        const scriptMatch = html.match(/<script>([\\s\\S]*?)<\\/script>/);
        const scriptContent = scriptMatch[1];
        const dummyElem = {{ options: [], style: {{}}, addEventListener: () => {{}}, removeEventListener: () => {{}}, querySelectorAll: () => [], classList: {{ add: () => {{}}, remove: () => {{}}, toggle: () => {{}} }}, setAttribute: () => {{}}, getAttribute: () => null, insertBefore: () => {{}}, appendChild: () => ({{}}), getBoundingClientRect: () => ({{ width: 1000, height: 800, left: 0, top: 0, right: 1000, bottom: 800 }}) }};
        global.window = {{ addEventListener: () => {{}}, removeEventListener: () => {{}} }};
        global.location = {{ href: '', search: '', hash: '' }};
        global.localStorage = {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }};
        global.requestAnimationFrame = cb => cb();
        global.document = {{
          title: 'Test Title',
          body: dummyElem,
          getElementById: () => dummyElem,
          querySelectorAll: () => [],
          addEventListener: () => {{}},
          createElement: () => dummyElem,
          createElementNS: () => dummyElem
        }};
        const vm = require('vm');
        const context = vm.createContext({{ document: global.document, window: global.window, location: global.location, localStorage: global.localStorage, requestAnimationFrame: global.requestAnimationFrame, console }});
        vm.runInContext(scriptContent, context);
        console.log(vm.runInContext('exportConfigJSON()', context));
        """
        proc = subprocess.run(['node', '-e', js_code], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Node.js execution failed: {proc.stderr}")

        exported_json = proc.stdout.strip()
        cfg = json.loads(exported_json)

        # 1. Validate against Python's strict config note syntax validator (_check_config_notes)
        erd._check_config_notes(cfg['notes'], 'exported_config.json')

        # 2. Resolve notes against merged IR to verify 100% roundtrip validity
        resolved = erd.resolve_and_validate_notes(cfg['notes'], self.tables, 'exported_config.json')
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]['scope'], 'table')
        self.assertEqual(resolved[0]['table'], 'users')

    def test_config_export_preserves_explicit_null_foreign_key_and_through(self):
        import subprocess
        # Generate with valid relation note so _finish resolves it cleanly
        notes_rel = [
            {
                'id': 'rel_null_fk',
                'target': {
                    'type': 'relation',
                    'source_table': 'posts',
                    'target_table': 'users',
                    'foreign_key': 'user_id'
                },
                'text': 'Explicit null foreign_key and through test'
            }
        ]
        tmp = tempfile.mkdtemp()
        out_path = Path(tmp) / 'out.html'
        args = type('Args', (), {'output': str(out_path), 'models': None, 'excel': None, 'max_rows': 15, 'only': None, 'exclude': None, 'infer_fk': False})()
        erd._finish(self.tables, args, 'null_test', notes=notes_rel)

        # In Node context, set NOTES[0].foreign_key = null and NOTES[0].through = null to simulate null attributes in resolved NOTES
        js_code = f"""
        const fs = require('fs');
        const html = fs.readFileSync({json.dumps(str(out_path))}, 'utf-8');
        const scriptMatch = html.match(/<script>([\\s\\S]*?)<\\/script>/);
        const scriptContent = scriptMatch[1];
        const dummyElem = {{ options: [], style: {{}}, addEventListener: () => {{}}, removeEventListener: () => {{}}, querySelectorAll: () => [], classList: {{ add: () => {{}}, remove: () => {{}}, toggle: () => {{}} }}, setAttribute: () => {{}}, getAttribute: () => null, insertBefore: () => {{}}, appendChild: () => ({{}}), getBoundingClientRect: () => ({{ width: 1000, height: 800, left: 0, top: 0, right: 1000, bottom: 800 }}) }};
        global.window = {{ addEventListener: () => {{}}, removeEventListener: () => {{}} }};
        global.location = {{ href: '', search: '', hash: '' }};
        global.localStorage = {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }};
        global.requestAnimationFrame = cb => cb();
        global.document = {{
          title: 'Test Title',
          body: dummyElem,
          getElementById: () => dummyElem,
          querySelectorAll: () => [],
          addEventListener: () => {{}},
          createElement: () => dummyElem,
          createElementNS: () => dummyElem
        }};
        const vm = require('vm');
        const context = vm.createContext({{ document: global.document, window: global.window, location: global.location, localStorage: global.localStorage, requestAnimationFrame: global.requestAnimationFrame, console }});
        vm.runInContext(scriptContent, context);
        vm.runInContext('NOTES[0].foreign_key = null; NOTES[0].through = null;', context);
        console.log(vm.runInContext('exportConfigJSON()', context));
        """
        proc = subprocess.run(['node', '-e', js_code], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Node.js execution failed: {proc.stderr}")

        exported_json = proc.stdout.strip()
        cfg = json.loads(exported_json)
        note_target = cfg['notes'][0]['target']
        self.assertIn('foreign_key', note_target)
        self.assertIsNone(note_target['foreign_key'])
        self.assertIn('through', note_target)
        self.assertIsNone(note_target['through'])

    def test_save_note_from_modal_validates_table_existence(self):
        import subprocess, json
        js_code = f"""
        const fs = require('fs');
        const html = fs.readFileSync({json.dumps(str(self.out_path))}, 'utf-8');
        const scriptMatch = html.match(/<script>([\\s\\S]*?)<\\/script>/);
        const scriptContent = scriptMatch[1];
        const elemMap = {{}};
        const createFormInput = (val = '') => ({{ value: val, style: {{}}, addEventListener: () => {{}}, removeEventListener: () => {{}}, classList: {{ add: () => {{}}, remove: () => {{}}, toggle: () => {{}} }} }});
        const modalElem = {{
          addEventListener: () => {{}},
          classList: {{
            hidden: false,
            add: function(cls) {{ if (cls === 'hidden') this.hidden = true; }},
            remove: function(cls) {{ if (cls === 'hidden') this.hidden = false; }},
            contains: function(cls) {{ return cls === 'hidden' ? this.hidden : false; }}
          }}
        }};
        
        elemMap['note-edit-modal'] = modalElem;
        elemMap['note-edit-id'] = createFormInput('new_note');
        elemMap['note-edit-scope'] = createFormInput('table');
        elemMap['note-edit-table'] = createFormInput('');
        elemMap['note-edit-source-table'] = createFormInput('');
        elemMap['note-edit-target-table'] = createFormInput('');
        elemMap['note-edit-foreign-key'] = createFormInput('');
        elemMap['note-edit-title-val'] = createFormInput('Title');
        elemMap['note-edit-text-val'] = createFormInput('Text content');
        
        const toasts = [];
        global.showToast = msg => toasts.push(msg);
        global.window = {{ addEventListener: () => {{}}, removeEventListener: () => {{}} }};
        global.location = {{ href: '', search: '', hash: '' }};
        global.localStorage = {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }};
        global.requestAnimationFrame = cb => cb();
        global.clearTimeout = () => {{}};
        global.setTimeout = () => {{}};
        const dummyElem = {{ options: [], style: {{}}, addEventListener: () => {{}}, removeEventListener: () => {{}}, querySelectorAll: () => [], classList: {{ add: () => {{}}, remove: () => {{}}, toggle: () => {{}} }}, setAttribute: () => {{}}, getAttribute: () => null, insertBefore: () => {{}}, appendChild: () => ({{}}), getBoundingClientRect: () => ({{ width: 1000, height: 800, left: 0, top: 0, right: 1000, bottom: 800 }}) }};
        global.document = {{
          title: 'Test Title',
          body: dummyElem,
          getElementById: id => elemMap[id] || dummyElem,
          querySelectorAll: () => [],
          addEventListener: () => {{}},
          createElement: () => dummyElem,
          createElementNS: () => dummyElem
        }};
        const vm = require('vm');
        const context = vm.createContext({{ document: global.document, window: global.window, location: global.location, localStorage: global.localStorage, requestAnimationFrame: global.requestAnimationFrame, clearTimeout: global.clearTimeout, setTimeout: global.setTimeout, console, showToast: global.showToast, toasts, elemMap }});
        vm.runInContext(scriptContent, context);
        vm.runInContext('showToast = msg => toasts.push(msg);', context);
        
        const results = [];
        
        function testCase(caseName, scope, tbl, srcTbl, tgtTbl) {{
          context.toasts.length = 0;
          context.elemMap['note-edit-modal'].classList.hidden = false;
          context.elemMap['note-edit-scope'].value = scope;
          context.elemMap['note-edit-table'].value = tbl;
          context.elemMap['note-edit-source-table'].value = srcTbl;
          context.elemMap['note-edit-target-table'].value = tgtTbl;
          
          const initialLen = vm.runInContext('NOTES.length', context);
          vm.runInContext('saveNoteFromModal()', context);
          const finalLen = vm.runInContext('NOTES.length', context);
          const isModalHidden = context.elemMap['note-edit-modal'].classList.contains('hidden');
          const lastToast = context.toasts[context.toasts.length - 1] || '';
          
          results.push({{ caseName, initialLen, finalLen, isModalHidden, lastToast }});
        }}
        
        // 1. Table scope: empty table name
        testCase('table_empty', 'table', '', '', '');
        // 2. Table scope: unknown table
        testCase('table_unknown', 'table', 'non_existent_table', '', '');
        // 3. Table scope: prototype property name ("constructor")
        testCase('table_proto', 'table', 'constructor', '', '');
        
        // 4. Relation scope: empty source table
        testCase('rel_empty_src', 'relation', '', '', 'users');
        // 5. Relation scope: unknown source table
        testCase('rel_unknown_src', 'relation', '', 'invalid_src', 'users');
        // 6. Relation scope: empty target table
        testCase('rel_empty_tgt', 'relation', '', 'users', '');
        // 7. Relation scope: unknown target table
        testCase('rel_unknown_tgt', 'relation', '', 'users', 'invalid_tgt');

        // 8. Valid table note (should succeed & close modal)
        testCase('table_valid', 'table', 'users', '', '');

        console.log(JSON.stringify(results));
        """
        proc = subprocess.run(['node', '-e', js_code], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Node.js execution failed: {proc.stderr}")
        res_list = json.loads(proc.stdout.strip())
        res = { r['caseName']: r for r in res_list }

        # Case 1: Empty table name
        self.assertEqual(res['table_empty']['initialLen'], res['table_empty']['finalLen'])
        self.assertFalse(res['table_empty']['isModalHidden'])
        self.assertEqual(res['table_empty']['lastToast'], 'Table name is required for Table Note')

        # Case 2: Unknown table
        self.assertEqual(res['table_unknown']['initialLen'], res['table_unknown']['finalLen'])
        self.assertFalse(res['table_unknown']['isModalHidden'])
        self.assertEqual(res['table_unknown']['lastToast'], 'Unknown table: "non_existent_table"')

        # Case 3: Prototype property name ("constructor")
        self.assertEqual(res['table_proto']['initialLen'], res['table_proto']['finalLen'])
        self.assertFalse(res['table_proto']['isModalHidden'])
        self.assertEqual(res['table_proto']['lastToast'], 'Unknown table: "constructor"')

        # Case 4: Relation empty source table
        self.assertEqual(res['rel_empty_src']['initialLen'], res['rel_empty_src']['finalLen'])
        self.assertFalse(res['rel_empty_src']['isModalHidden'])
        self.assertEqual(res['rel_empty_src']['lastToast'], 'Source table is required for Relation Note')

        # Case 5: Relation unknown source table
        self.assertEqual(res['rel_unknown_src']['initialLen'], res['rel_unknown_src']['finalLen'])
        self.assertFalse(res['rel_unknown_src']['isModalHidden'])
        self.assertEqual(res['rel_unknown_src']['lastToast'], 'Unknown source table: "invalid_src"')

        # Case 6: Relation empty target table
        self.assertEqual(res['rel_empty_tgt']['initialLen'], res['rel_empty_tgt']['finalLen'])
        self.assertFalse(res['rel_empty_tgt']['isModalHidden'])
        self.assertEqual(res['rel_empty_tgt']['lastToast'], 'Target table is required for Relation Note')

        # Case 7: Relation unknown target table
        self.assertEqual(res['rel_unknown_tgt']['initialLen'], res['rel_unknown_tgt']['finalLen'])
        self.assertFalse(res['rel_unknown_tgt']['isModalHidden'])
        self.assertEqual(res['rel_unknown_tgt']['lastToast'], 'Unknown target table: "invalid_tgt"')

        # Case 8: Valid table note (succeeds)
        self.assertEqual(res['table_valid']['finalLen'], res['table_valid']['initialLen'] + 1)
        self.assertTrue(res['table_valid']['isModalHidden'])
        self.assertEqual(res['table_valid']['lastToast'], 'Saved note ✓')

    def test_auto_tidy_toggle_records_undo_snapshot(self):
        import subprocess, json
        js_code = f"""
        const fs = require('fs');
        const html = fs.readFileSync({json.dumps(str(self.out_path))}, 'utf-8');
        const scriptMatch = html.match(/<script>([\\s\\S]*?)<\\/script>/);
        const scriptContent = scriptMatch[1];
        const dummyElem = {{ options: [], style: {{}}, addEventListener: () => {{}}, removeEventListener: () => {{}}, querySelectorAll: () => [], classList: {{ add: () => {{}}, remove: () => {{}}, toggle: () => {{}} }}, setAttribute: () => {{}}, getAttribute: () => null, insertBefore: () => {{}}, appendChild: () => ({{}}), getBoundingClientRect: () => ({{ width: 1000, height: 800, left: 0, top: 0, right: 1000, bottom: 800 }}), getBBox: () => ({{ width: 100, height: 20, x: 0, y: 0 }}) }};
        global.window = {{ addEventListener: () => {{}}, removeEventListener: () => {{}} }};
        global.location = {{ href: '', search: '', hash: '' }};
        global.localStorage = {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }};
        global.requestAnimationFrame = cb => cb();
        global.clearTimeout = () => {{}};
        global.setTimeout = () => {{}};
        global.document = {{
          title: 'Test Title',
          body: dummyElem,
          getElementById: () => dummyElem,
          querySelectorAll: () => [],
          addEventListener: () => {{}},
          createElement: () => dummyElem,
          createElementNS: () => dummyElem
        }};
        const vm = require('vm');
        const context = vm.createContext({{ document: global.document, window: global.window, location: global.location, localStorage: global.localStorage, requestAnimationFrame: global.requestAnimationFrame, clearTimeout: global.clearTimeout, setTimeout: global.setTimeout, console }});
        vm.runInContext(scriptContent, context);
        
        const initialAutoLayout = vm.runInContext('autoLayout', context);
        const initialUndoLen = vm.runInContext('undoStack.length', context);
        
        vm.runInContext('pushUndoSnapshot(); autoLayout = true;', context);
        const onAutoLayout = vm.runInContext('autoLayout', context);
        const onUndoLen = vm.runInContext('undoStack.length', context);

        vm.runInContext('doUndo();', context);
        const restoredAutoLayout = vm.runInContext('autoLayout', context);
        
        console.log(JSON.stringify({{ initialAutoLayout, initialUndoLen, onAutoLayout, onUndoLen, restoredAutoLayout }}));
        """
        proc = subprocess.run(['node', '-e', js_code], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Node.js execution failed: {proc.stderr}")
        res = json.loads(proc.stdout.strip())
        self.assertFalse(res['initialAutoLayout'])
        self.assertTrue(res['onAutoLayout'])
        self.assertEqual(res['onUndoLen'], res['initialUndoLen'] + 1)
        self.assertFalse(res['restoredAutoLayout'])


if __name__ == '__main__':
    unittest.main()
