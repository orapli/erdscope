"""groups Phase 1 — the config `groups:` visual-grouping sidecar.

Covers what tests/test_config_validation.py deliberately does NOT (purely
syntactic checks live there): semantic validation + viewer resolution
(providers.resolve_and_validate_groups) against a final merged IR — including
the Phase 1 no-overlap rule — the config->CLI wiring (two-stage validation
against config.tables add/drop, DATA_JSON serialization), the demo/back-compat
byte-shape guarantee (no `groups` key when there are no groups), and
--only/--exclude membership filtering (including whole-group elimination when
every member is filtered out). Mirrors tests/test_notes.py's structure and
conventions throughout — groups are the same kind of sidecar as notes.

Run from the repository root:
    python3 -m unittest tests.test_groups -v
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


def _tables(extra=None):
    """A minimal hand-built final-IR shape (users, posts, comments) —
    resolve_and_validate_groups only reads the table KEYS of `tables`, so
    there's no need to route this through merge_ir for tests that are purely
    about group resolution."""
    t = {
        'users': {'columns': [{'name': 'id'}], 'associations': []},
        'posts': {'columns': [{'name': 'id'}], 'associations': []},
        'comments': {'columns': [{'name': 'id'}], 'associations': []},
    }
    if extra:
        t.update(extra)
    return t


# ---------------------------------------------------------------------------
# resolve_and_validate_groups — direct unit tests (no CLI/pipeline involved)
# ---------------------------------------------------------------------------
class TestResolveGroups(unittest.TestCase):
    def test_basic_group_resolves(self):
        groups = [{'id': 'g1', 'tables': ['users', 'posts']}]
        out = erd.resolve_and_validate_groups(groups, _tables(), 'cfg')
        self.assertEqual(out, [{'id': 'g1', 'tables': ['users', 'posts']}])

    def test_title_and_color_pass_through_when_present(self):
        groups = [{'id': 'g1', 'tables': ['users'], 'title': 'People', 'color': '#0d9488'}]
        out = erd.resolve_and_validate_groups(groups, _tables(), 'cfg')
        self.assertEqual(out, [{'id': 'g1', 'tables': ['users'],
                               'title': 'People', 'color': '#0d9488'}])

    def test_title_and_color_absent_when_not_configured(self):
        groups = [{'id': 'g1', 'tables': ['users']}]
        out = erd.resolve_and_validate_groups(groups, _tables(), 'cfg')
        self.assertNotIn('title', out[0])
        self.assertNotIn('color', out[0])

    def test_multiple_disjoint_groups_all_resolve(self):
        groups = [{'id': 'g1', 'tables': ['users']},
                  {'id': 'g2', 'tables': ['posts', 'comments']}]
        out = erd.resolve_and_validate_groups(groups, _tables(), 'cfg')
        self.assertEqual([g['id'] for g in out], ['g1', 'g2'])
        self.assertEqual(out[1]['tables'], ['posts', 'comments'])

    def test_unknown_table_errors_and_includes_group_id(self):
        groups = [{'id': 'g1', 'tables': ['ghosts']}]
        with self.assertRaises(SystemExit) as cm:
            erd.resolve_and_validate_groups(groups, _tables(), 'cfg')
        msg = str(cm.exception)
        self.assertIn('g1', msg)
        self.assertIn('ghosts', msg)

    def test_duplicate_membership_across_groups_errors_with_both_ids_and_table(self):
        # Phase 1: a table may belong to only ONE group (D-3) — this is the
        # semantic (final-IR) half of that rule; the syntactic half (a
        # duplicate WITHIN one group's own `tables` list) is rejected earlier
        # at config load time (test_config_validation).
        groups = [{'id': 'g1', 'tables': ['users', 'posts']},
                  {'id': 'g2', 'tables': ['posts']}]
        with self.assertRaises(SystemExit) as cm:
            erd.resolve_and_validate_groups(groups, _tables(), 'cfg')
        msg = str(cm.exception)
        self.assertIn('g2', msg)
        self.assertIn('g1', msg)
        self.assertIn('posts', msg)

    def test_empty_groups_list_resolves_to_empty(self):
        self.assertEqual(erd.resolve_and_validate_groups([], _tables(), 'cfg'), [])


class _NoDBDriver(unittest.TestCase):
    """Drive main() with NO database url — same technique as
    test_notes._NoDBDriver — so groups-only / config+framework runs never try
    to open a real connection."""
    def setUp(self):
        def _no_db(url):
            raise AssertionError('DB should not be contacted in a groups-only/config test')
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


class TestGroupsTwoStageValidation(_NoDBDriver):
    """§6.4①/② mirrored for groups: syntax at load time
    (test_config_validation), semantic validation against the FINAL merged
    IR — after config.tables add/drop — at apply time."""

    def test_group_on_a_table_added_by_config_tables_resolves(self):
        # the group's member table doesn't exist anywhere except config.tables
        # itself — must still resolve, since validation runs on the FINAL IR
        cfg = {'tables': {'settings': {'columns': [{'name': 'id', 'primary': True}]}},
               'groups': [{'id': 'g1', 'tables': ['settings']}]}
        path = self._p('c.json')
        Path(path).write_text(json.dumps(cfg))
        out = self._p('out.html')
        self._run('--config', path, '-o', out)
        data = self._data(out)
        self.assertEqual(data['groups'], [{'id': 'g1', 'tables': ['settings']}])

    def test_group_targeting_a_table_dropped_by_config_errors_against_final_ir(self):
        # 'webhooks' comes from the Rails fixture (a real lower layer);
        # config.tables drops it, so the group's member no longer exists in
        # the FINAL merged IR even though it existed in a raw layer
        cfg = {'models': str(FIXTURE_RAILS),
               'tables': {'webhooks': {'drop': True}},
               'groups': [{'id': 'g1', 'tables': ['webhooks']}]}
        path = self._p('c.json')
        Path(path).write_text(json.dumps(cfg))
        out = self._p('out.html')
        with self.assertRaises(SystemExit) as cm:
            self._run('--config', path, '-o', out)
        msg = str(cm.exception)
        self.assertIn('g1', msg)
        self.assertIn('webhooks', msg)

    def test_group_on_a_table_config_leaves_untouched_still_resolves_alongside_a_drop(self):
        # regression guard: a drop elsewhere in the SAME config.tables run
        # must not affect a group on an unrelated, still-present table
        cfg = {'models': str(FIXTURE_RAILS),
               'tables': {'webhooks': {'drop': True}},
               'groups': [{'id': 'g1', 'tables': ['users']}]}
        path = self._p('c.json')
        Path(path).write_text(json.dumps(cfg))
        out = self._p('out.html')
        self._run('--config', path, '-o', out)
        data = self._data(out)
        self.assertNotIn('webhooks', data['tables'])
        self.assertEqual(data['groups'][0]['tables'], ['users'])


class TestGroupsOnlyExcludeFiltering(_NoDBDriver):
    """Groups mirror notes' Sol finding #1: --only/--exclude must not leak a
    filtered-out table's group membership into the HTML. A group's `tables`
    is narrowed to the surviving set, and a group left with ZERO surviving
    members is dropped entirely (a frame around nothing is a viewer bug, not
    a feature)."""

    def test_exclude_drops_the_excluded_tables_own_group_entirely(self):
        cfg = {'tables': {
                   'users': {'columns': [{'name': 'id', 'primary': True}]},
                   'secret': {'columns': [{'name': 'id', 'primary': True}]}},
               'groups': [{'id': 'people', 'tables': ['users']},
                         {'id': 'secret-domain', 'tables': ['secret']}]}
        path = self._p('c.json')
        Path(path).write_text(json.dumps(cfg))
        out = self._p('out.html')
        self._run('--config', path, '-o', out, '--exclude', 'secret')
        data = self._data(out)
        self.assertNotIn('secret', data['tables'])
        ids = {g['id'] for g in data['groups']}
        self.assertEqual(ids, {'people'})  # secret-domain lost its only member

    def test_only_narrows_a_multi_member_group_without_dropping_it(self):
        cfg = {'tables': {
                   'orders': {'columns': [{'name': 'id', 'primary': True}]},
                   'payments': {'columns': [{'name': 'id', 'primary': True}]},
                   'users': {'columns': [{'name': 'id', 'primary': True}]}},
               'groups': [{'id': 'commerce', 'tables': ['orders', 'payments']}]}
        path = self._p('c.json')
        Path(path).write_text(json.dumps(cfg))
        out = self._p('out.html')
        self._run('--config', path, '-o', out, '--only', 'orders', '--only', 'users')
        data = self._data(out)
        self.assertEqual(set(data['tables']), {'orders', 'users'})
        # 'payments' didn't survive --only, but 'orders' did — the group
        # narrows to just its surviving member instead of disappearing
        self.assertEqual(data['groups'], [{'id': 'commerce', 'tables': ['orders']}])

    def test_no_filtering_flags_keeps_every_group_intact(self):
        cfg = {'tables': {
                   'users': {'columns': [{'name': 'id', 'primary': True}]},
                   'orders': {'columns': [{'name': 'id', 'primary': True}]}},
               'groups': [{'id': 'g1', 'tables': ['users', 'orders']}]}
        path = self._p('c.json')
        Path(path).write_text(json.dumps(cfg))
        out = self._p('out.html')
        self._run('--config', path, '-o', out)
        data = self._data(out)
        self.assertEqual(data['groups'], [{'id': 'g1', 'tables': ['users', 'orders']}])


class TestGroupsSerializeAndDemoParity(_NoDBDriver):
    def _schema(self):
        return {'tables': {'t': {'columns': [{'name': 'id', 'primary': True}]}}}

    def test_no_groups_key_in_config_omits_groups_from_data_json(self):
        # the demo-byte-equality guardrail: a config with no `groups` at all
        # must produce EXACTLY today's (pre-groups-feature) DATA_JSON shape
        path = self._p('c.json')
        Path(path).write_text(json.dumps(self._schema()))
        out = self._p('out.html')
        self._run('--config', path, '-o', out)
        self.assertNotIn('groups', self._data(out))

    def test_empty_groups_list_also_omits_the_key(self):
        cfg = self._schema()
        cfg['groups'] = []
        path = self._p('c.json')
        Path(path).write_text(json.dumps(cfg))
        out = self._p('out.html')
        self._run('--config', path, '-o', out)
        self.assertNotIn('groups', self._data(out))

    def test_configured_groups_appear_in_data_json(self):
        cfg = self._schema()
        cfg['groups'] = [{'id': 'g1', 'tables': ['t'], 'title': 'Everything',
                          'color': '#0d9488'}]
        path = self._p('c.json')
        Path(path).write_text(json.dumps(cfg))
        out = self._p('out.html')
        self._run('--config', path, '-o', out)
        self.assertEqual(self._data(out)['groups'],
                         [{'id': 'g1', 'tables': ['t'], 'title': 'Everything',
                           'color': '#0d9488'}])

    def test_embedded_script_close_tag_never_breaks_out_of_the_data_script_element(self):
        cfg = self._schema()
        cfg['groups'] = [{'id': 'g1', 'tables': ['t'],
                          'title': 'boom </script><script>alert(1)</script>'}]
        path = self._p('c.json')
        Path(path).write_text(json.dumps(cfg))
        out = self._p('out.html')
        self._run('--config', path, '-o', out)
        html = Path(out).read_text()
        self.assertNotIn('</script><script>alert', html)
        self.assertIn('<\\/script>', html)  # same existing DATA_JSON `</` -> `<\/` guard
        self.assertEqual(self._data(out)['groups'][0]['title'],
                         'boom </script><script>alert(1)</script>')


class TestGroupsBackwardCompatRegression(_NoDBDriver):
    """Regression: framework-only / config-only pipelines with no `groups` at
    all behave exactly as before this feature (no `groups` key, existing
    tables/associations/notes unaffected)."""

    def test_framework_only_with_no_groups_config_key_is_unaffected(self):
        out = self._p('out.html')
        self._run('--models', str(FIXTURE_RAILS), '--no-config', '-o', out)
        data = self._data(out)
        self.assertNotIn('groups', data)
        self.assertIn('users', data['tables'])

    def test_notes_and_groups_coexist_independently(self):
        cfg = self._schema_with_notes_and_groups()
        path = self._p('c.json')
        Path(path).write_text(json.dumps(cfg))
        out = self._p('out.html')
        self._run('--config', path, '-o', out)
        data = self._data(out)
        self.assertEqual(data['notes'], [{'id': 'n1', 'scope': 'global', 'text': 'hi'}])
        self.assertEqual(data['groups'], [{'id': 'g1', 'tables': ['t']}])

    def _schema_with_notes_and_groups(self):
        return {'tables': {'t': {'columns': [{'name': 'id', 'primary': True}]}},
               'notes': [{'id': 'n1', 'target': {'type': 'global'}, 'text': 'hi'}],
               'groups': [{'id': 'g1', 'tables': ['t']}]}

    def test_excel_still_generates_without_a_groups_sheet(self):
        # Phase 1 write_excel(groups=...) is wiring-only — no Groups sheet yet
        import zipfile
        cfg = {'tables': {'t': {'columns': [{'name': 'id', 'primary': True}]}},
               'groups': [{'id': 'g1', 'tables': ['t']}]}
        path = self._p('c.json')
        Path(path).write_text(json.dumps(cfg))
        out, xlsx = self._p('out.html'), self._p('defs.xlsx')
        self._run('--config', path, '-o', out, '--excel', xlsx)
        self.assertTrue(Path(xlsx).exists())
        with zipfile.ZipFile(xlsx) as z:
            names = z.namelist()
        self.assertFalse(any('group' in n.lower() for n in names))


if __name__ == '__main__':
    unittest.main()
