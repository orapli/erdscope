"""--diff (backlog #2) — level1 schema diff / CI drift gate.

Covers the new src/erdscope/diff.py surface (schema_diff, empty_schema_diff,
diff_is_empty, render_text, render_json) as direct unit tests against
hand-built canonical-schema dicts (the same shape emit.py's canonical_schema
produces — no need to route through merge_ir; mirrors tests/test_emit_json.py's
style for its own canonical_schema tests), plus CLI-level wiring tests
(--diff exit codes, --diff-exit-zero, --diff-format json, the fingerprint
fast path, and the "diff generates no output" guarantee) driven through
main() the same way tests/test_emit_json.py and tests/test_emit_config.py do.

Run from the repository root:
    python3 -m unittest tests.test_diff -v
"""
import copy
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location('erd', ROOT / 'erd.py')
erd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(erd)


def _col(name, type_='integer', nullable=False, **extra):
    c = {'name': name, 'type': type_, 'nullable': nullable}
    c.update(extra)
    return c


def _table(columns=None, indexes=None, associations=None, comment=None):
    t = {'columns': columns or [], 'indexes': indexes or [], 'associations': associations or []}
    if comment:
        t['comment'] = comment
    return t


def _schema(tables=None, notes=None, groups=None):
    s = {'tables': tables or {}}
    if notes is not None:
        s['notes'] = notes
    if groups is not None:
        s['groups'] = groups
    return s


def _base_pair():
    """Two identical minimal schemas (users/posts, mirroring
    tests/test_emit_json.py's _merged_tables fixture but already in the
    canonical/post-projection shape schema_diff consumes) — tests mutate a
    deep copy of one side."""
    tables = {
        'users': _table(
            columns=[_col('id', primary=True), _col('email', type_='string')],
            indexes=[{'columns': ['email'], 'unique': True}],
            comment='App users',
        ),
        'posts': _table(
            columns=[_col('id', primary=True), _col('user_id')],
            associations=[{'type': 'belongs_to', 'target': 'users', 'name': 'user',
                           'foreign_key': 'user_id', 'provenance': 'db_fk',
                           'sources': [{'kind': 'db', 'provider': 'mysql'}]}],
        ),
    }
    left = _schema(copy.deepcopy(tables))
    right = _schema(copy.deepcopy(tables))
    return left, right


# ---------------------------------------------------------------------------
# tables — added / removed / changed, and direction
# ---------------------------------------------------------------------------
class TestTablesDiff(unittest.TestCase):
    def test_identical_schemas_are_empty(self):
        left, right = _base_pair()
        diff = erd.schema_diff(left, right)
        self.assertTrue(erd.diff_is_empty(diff))
        self.assertEqual(diff['tables'], {'added': [], 'removed': [], 'changed': {}})

    def test_table_only_in_left_is_added(self):
        left, right = _base_pair()
        left['tables']['comments'] = _table(columns=[_col('id', primary=True)])
        diff = erd.schema_diff(left, right)
        self.assertEqual(diff['tables']['added'], ['comments'])
        self.assertEqual(diff['tables']['removed'], [])

    def test_table_only_in_right_is_removed(self):
        left, right = _base_pair()
        right['tables']['archived'] = _table(columns=[_col('id', primary=True)])
        diff = erd.schema_diff(left, right)
        self.assertEqual(diff['tables']['removed'], ['archived'])
        self.assertEqual(diff['tables']['added'], [])

    def test_comment_change_reported_as_old_new(self):
        left, right = _base_pair()
        left['tables']['users']['comment'] = 'Renamed users'
        diff = erd.schema_diff(left, right)
        self.assertEqual(diff['tables']['changed']['users']['comment'],
                         {'old': 'App users', 'new': 'Renamed users'})

    def test_comment_added_from_empty(self):
        left, right = _base_pair()
        del right['tables']['posts']  # posts has no comment on either side by default
        right['tables']['posts'] = _table(
            columns=[_col('id', primary=True), _col('user_id')])
        left['tables']['posts']['comment'] = 'New comment'
        diff = erd.schema_diff(left, right)
        self.assertEqual(diff['tables']['changed']['posts']['comment'],
                         {'old': '', 'new': 'New comment'})

    def test_unchanged_common_table_absent_from_changed(self):
        left, right = _base_pair()
        left['tables']['comments'] = _table(columns=[_col('id', primary=True)])
        diff = erd.schema_diff(left, right)
        self.assertNotIn('users', diff['tables']['changed'])
        self.assertNotIn('posts', diff['tables']['changed'])


# ---------------------------------------------------------------------------
# columns — name-matched, added/removed/changed with field enumeration
# ---------------------------------------------------------------------------
class TestColumnsDiff(unittest.TestCase):
    def test_column_added(self):
        left, right = _base_pair()
        left['tables']['users']['columns'].append(_col('created_at', type_='datetime'))
        diff = erd.schema_diff(left, right)
        self.assertEqual(diff['tables']['changed']['users']['columns']['added'], ['created_at'])

    def test_column_removed(self):
        left, right = _base_pair()
        right['tables']['users']['columns'].append(_col('legacy_flag', type_='boolean'))
        diff = erd.schema_diff(left, right)
        self.assertEqual(diff['tables']['changed']['users']['columns']['removed'],
                         ['legacy_flag'])

    def test_column_type_change_enumerated(self):
        left, right = _base_pair()
        left['tables']['users']['columns'][1]['type'] = 'text'
        diff = erd.schema_diff(left, right)
        fields = diff['tables']['changed']['users']['columns']['changed']['email']['fields']
        self.assertEqual(fields, {'type': {'old': 'string', 'new': 'text'}})

    def test_multiple_field_changes_all_enumerated(self):
        left, right = _base_pair()
        col = left['tables']['users']['columns'][1]
        col['nullable'] = True
        col['default'] = 'unknown@example.com'
        col['comment'] = 'primary email'
        diff = erd.schema_diff(left, right)
        fields = diff['tables']['changed']['users']['columns']['changed']['email']['fields']
        self.assertEqual(set(fields), {'nullable', 'default', 'comment'})
        self.assertEqual(fields['nullable'], {'old': False, 'new': True})
        self.assertEqual(fields['default'], {'old': '', 'new': 'unknown@example.com'})

    def test_primary_flag_change(self):
        left, right = _base_pair()
        left['tables']['posts']['columns'][1]['primary'] = True  # user_id becomes composite PK
        diff = erd.schema_diff(left, right)
        fields = diff['tables']['changed']['posts']['columns']['changed']['user_id']['fields']
        self.assertEqual(fields, {'primary': {'old': False, 'new': True}})

    def test_sql_type_and_extra_change(self):
        left, right = _base_pair()
        left['tables']['posts']['columns'][0]['sql_type'] = 'bigint unsigned'
        left['tables']['posts']['columns'][0]['extra'] = 'auto_increment'
        diff = erd.schema_diff(left, right)
        fields = diff['tables']['changed']['posts']['columns']['changed']['id']['fields']
        self.assertEqual(set(fields), {'sql_type', 'extra'})

    def test_unchanged_columns_absent_from_changed(self):
        left, right = _base_pair()
        left['tables']['users']['columns'].append(_col('created_at'))
        diff = erd.schema_diff(left, right)
        changed = diff['tables']['changed']['users']['columns']['changed']
        self.assertEqual(changed, {})


# ---------------------------------------------------------------------------
# indexes — identity is (tuple(columns), unique); name is NOT identity
# ---------------------------------------------------------------------------
class TestIndexesDiff(unittest.TestCase):
    def test_index_rename_alone_is_not_a_difference(self):
        left, right = _base_pair()
        left['tables']['users']['indexes'] = [{'name': 'idx_a', 'columns': ['email'], 'unique': True}]
        right['tables']['users']['indexes'] = [{'name': 'idx_b', 'columns': ['email'], 'unique': True}]
        diff = erd.schema_diff(left, right)
        self.assertNotIn('users', diff['tables']['changed'])

    def test_index_added(self):
        left, right = _base_pair()
        left['tables']['posts']['indexes'].append({'columns': ['user_id'], 'unique': False})
        diff = erd.schema_diff(left, right)
        self.assertEqual(diff['tables']['changed']['posts']['indexes']['added'],
                         [{'columns': ['user_id'], 'unique': False}])
        self.assertEqual(diff['tables']['changed']['posts']['indexes']['removed'], [])

    def test_index_removed(self):
        left, right = _base_pair()
        right['tables']['posts']['indexes'].append({'columns': ['user_id'], 'unique': False})
        diff = erd.schema_diff(left, right)
        self.assertEqual(diff['tables']['changed']['posts']['indexes']['removed'],
                         [{'columns': ['user_id'], 'unique': False}])

    def test_uniqueness_change_is_added_plus_removed(self):
        left, right = _base_pair()
        left['tables']['users']['indexes'] = [{'columns': ['email'], 'unique': False}]
        diff = erd.schema_diff(left, right)
        ix = diff['tables']['changed']['users']['indexes']
        self.assertEqual(ix['added'], [{'columns': ['email'], 'unique': False}])
        self.assertEqual(ix['removed'], [{'columns': ['email'], 'unique': True}])

    def test_indexes_have_no_changed_bucket(self):
        left, right = _base_pair()
        left['tables']['posts']['indexes'].append({'columns': ['user_id'], 'unique': False})
        diff = erd.schema_diff(left, right)
        self.assertNotIn('changed', diff['tables']['changed']['posts']['indexes'])


# ---------------------------------------------------------------------------
# associations — identity set diff; provenance excluded unless requested
# ---------------------------------------------------------------------------
class TestAssociationsDiff(unittest.TestCase):
    def test_identical_association_is_no_difference(self):
        left, right = _base_pair()
        diff = erd.schema_diff(left, right)
        self.assertNotIn('posts', diff['tables']['changed'])

    def test_provenance_only_change_ignored_by_default(self):
        left, right = _base_pair()
        left['tables']['posts']['associations'][0]['provenance'] = 'manual'
        left['tables']['posts']['associations'][0]['sources'] = [
            {'kind': 'config', 'provider': 'config'}]
        diff = erd.schema_diff(left, right)
        self.assertNotIn('posts', diff['tables']['changed'])

    def test_provenance_only_change_detected_with_flag(self):
        left, right = _base_pair()
        left['tables']['posts']['associations'][0]['provenance'] = 'manual'
        left['tables']['posts']['associations'][0]['sources'] = [
            {'kind': 'config', 'provider': 'config'}]
        diff = erd.schema_diff(left, right, include_provenance=True)
        asc = diff['tables']['changed']['posts']['associations']
        self.assertEqual(len(asc['added']), 1)
        self.assertEqual(len(asc['removed']), 1)
        self.assertEqual(asc['added'][0]['provenance'], 'manual')
        self.assertEqual(asc['removed'][0]['provenance'], 'db_fk')

    def test_retargeted_foreign_key_is_removed_plus_added(self):
        left, right = _base_pair()
        left['tables']['posts']['associations'][0]['foreign_key'] = 'author_id'
        diff = erd.schema_diff(left, right)
        asc = diff['tables']['changed']['posts']['associations']
        self.assertEqual(asc['added'], [{'type': 'belongs_to', 'target': 'users',
                                         'name': 'user', 'foreign_key': 'author_id'}])
        self.assertEqual(asc['removed'], [{'type': 'belongs_to', 'target': 'users',
                                           'name': 'user', 'foreign_key': 'user_id'}])
        self.assertNotIn('changed', asc)

    def test_association_added(self):
        left, right = _base_pair()
        left['tables']['posts']['associations'].append(
            {'type': 'has_many', 'target': 'comments', 'name': 'comments'})
        diff = erd.schema_diff(left, right)
        added = diff['tables']['changed']['posts']['associations']['added']
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]['target'], 'comments')

    def test_polymorphic_flag_is_part_of_identity(self):
        left, right = _base_pair()
        left['tables']['posts']['associations'][0]['polymorphic'] = True
        diff = erd.schema_diff(left, right)
        asc = diff['tables']['changed']['posts']['associations']
        self.assertEqual(len(asc['added']), 1)
        self.assertTrue(asc['added'][0]['polymorphic'])
        self.assertEqual(len(asc['removed']), 1)


# ---------------------------------------------------------------------------
# notes — id-matched, added/removed/changed
# ---------------------------------------------------------------------------
class TestNotesDiff(unittest.TestCase):
    def _notes_pair(self):
        notes = [{'id': 'n1', 'scope': 'global', 'text': 'diagram-wide note'}]
        left, right = _base_pair()
        left['notes'] = copy.deepcopy(notes)
        right['notes'] = copy.deepcopy(notes)
        return left, right

    def test_identical_notes_no_difference(self):
        left, right = self._notes_pair()
        diff = erd.schema_diff(left, right)
        self.assertEqual(diff['notes'], {'added': [], 'removed': [], 'changed': {}})

    def test_note_added(self):
        left, right = self._notes_pair()
        left['notes'].append({'id': 'n2', 'scope': 'table', 'table': 'users', 'text': 'PII'})
        diff = erd.schema_diff(left, right)
        self.assertEqual(diff['notes']['added'], ['n2'])

    def test_note_removed(self):
        left, right = self._notes_pair()
        right['notes'].append({'id': 'n2', 'scope': 'table', 'table': 'users', 'text': 'PII'})
        diff = erd.schema_diff(left, right)
        self.assertEqual(diff['notes']['removed'], ['n2'])

    def test_note_text_change(self):
        left, right = self._notes_pair()
        left['notes'][0]['text'] = 'updated diagram-wide note'
        diff = erd.schema_diff(left, right)
        fields = diff['notes']['changed']['n1']['fields']
        self.assertEqual(fields, {'text': {'old': 'diagram-wide note',
                                           'new': 'updated diagram-wide note'}})

    def test_note_scope_change(self):
        left, right = self._notes_pair()
        left['notes'][0] = {'id': 'n1', 'scope': 'table', 'table': 'users',
                            'text': 'diagram-wide note'}
        diff = erd.schema_diff(left, right)
        fields = diff['notes']['changed']['n1']['fields']
        self.assertEqual(fields['scope'], {'old': 'global', 'new': 'table'})
        self.assertEqual(fields['table'], {'old': None, 'new': 'users'})

    def test_absent_notes_key_treated_as_empty(self):
        left, right = _base_pair()  # neither side has a 'notes' key at all
        diff = erd.schema_diff(left, right)
        self.assertEqual(diff['notes'], {'added': [], 'removed': [], 'changed': {}})


# ---------------------------------------------------------------------------
# groups — id-matched, tables-set/title/color diff
# ---------------------------------------------------------------------------
class TestGroupsDiff(unittest.TestCase):
    def _groups_pair(self):
        groups = [{'id': 'g1', 'tables': ['users', 'posts'], 'title': 'Core'}]
        left, right = _base_pair()
        left['groups'] = copy.deepcopy(groups)
        right['groups'] = copy.deepcopy(groups)
        return left, right

    def test_identical_groups_no_difference(self):
        left, right = self._groups_pair()
        diff = erd.schema_diff(left, right)
        self.assertEqual(diff['groups'], {'added': [], 'removed': [], 'changed': {}})

    def test_group_added(self):
        left, right = self._groups_pair()
        left['groups'].append({'id': 'g2', 'tables': ['posts']})
        diff = erd.schema_diff(left, right)
        self.assertEqual(diff['groups']['added'], ['g2'])

    def test_group_removed(self):
        left, right = self._groups_pair()
        right['groups'].append({'id': 'g2', 'tables': ['posts']})
        diff = erd.schema_diff(left, right)
        self.assertEqual(diff['groups']['removed'], ['g2'])

    def test_group_membership_set_diff(self):
        left, right = self._groups_pair()
        left['groups'][0]['tables'] = ['posts']  # dropped 'users'
        diff = erd.schema_diff(left, right)
        membership = diff['groups']['changed']['g1']['tables']
        self.assertEqual(membership, {'added': [], 'removed': ['users']})

    def test_group_membership_reorder_is_not_a_difference(self):
        left, right = self._groups_pair()
        left['groups'][0]['tables'] = ['posts', 'users']  # same set, different order
        diff = erd.schema_diff(left, right)
        self.assertNotIn('g1', diff['groups']['changed'])

    def test_group_title_and_color_change(self):
        left, right = self._groups_pair()
        left['groups'][0]['title'] = 'Renamed'
        left['groups'][0]['color'] = '#ff0000'
        diff = erd.schema_diff(left, right)
        changed = diff['groups']['changed']['g1']
        self.assertEqual(changed['title'], {'old': 'Core', 'new': 'Renamed'})
        self.assertEqual(changed['color'], {'old': '', 'new': '#ff0000'})


# ---------------------------------------------------------------------------
# fingerprint fast path (function level) + is_empty / render determinism
# ---------------------------------------------------------------------------
class TestFingerprintFastPath(unittest.TestCase):
    def test_same_schema_same_fingerprint(self):
        left, right = _base_pair()
        self.assertEqual(erd.snapshot_fingerprint(left), erd.snapshot_fingerprint(right))

    def test_empty_schema_diff_matches_full_compute_on_identical_schemas(self):
        left, right = _base_pair()
        self.assertEqual(erd.schema_diff(left, right), erd.empty_schema_diff())


class TestIsEmpty(unittest.TestCase):
    def test_empty_schema_diff_is_empty(self):
        self.assertTrue(erd.diff_is_empty(erd.empty_schema_diff()))

    def test_any_difference_is_not_empty(self):
        left, right = _base_pair()
        left['tables']['comments'] = _table(columns=[_col('id', primary=True)])
        diff = erd.schema_diff(left, right)
        self.assertFalse(erd.diff_is_empty(diff))


class TestRenderJson(unittest.TestCase):
    def test_deterministic_sorted_indented_trailing_newline(self):
        left, right = _base_pair()
        left['tables']['comments'] = _table(columns=[_col('id', primary=True)])
        diff = erd.schema_diff(left, right)
        out1 = erd.render_json(diff)
        out2 = erd.render_json(erd.schema_diff(left, right))
        self.assertEqual(out1, out2)
        self.assertTrue(out1.endswith('\n'))
        parsed = json.loads(out1)
        self.assertEqual(parsed, diff)

    def test_render_json_of_empty_diff(self):
        out = erd.render_json(erd.empty_schema_diff())
        parsed = json.loads(out)
        self.assertTrue(erd.diff_is_empty(parsed))


class TestRenderText(unittest.TestCase):
    def test_no_differences_message(self):
        self.assertEqual(erd.render_text(erd.empty_schema_diff()), 'No schema differences.\n')

    def test_summary_line_and_markers(self):
        left, right = _base_pair()
        left['tables']['comments'] = _table(columns=[_col('id', primary=True)])
        right['tables']['archived'] = _table(columns=[_col('id', primary=True)])
        left['tables']['users']['comment'] = 'Renamed users'
        diff = erd.schema_diff(left, right)
        text = erd.render_text(diff)
        self.assertIn('1 added, 1 removed, 1 changed', text)
        self.assertIn('+ comments', text)
        self.assertIn('- archived', text)
        self.assertIn('~ users', text)


class TestPurity(unittest.TestCase):
    def test_schema_diff_does_not_mutate_inputs(self):
        left, right = _base_pair()
        left['tables']['comments'] = _table(columns=[_col('id', primary=True)])
        left_before, right_before = copy.deepcopy(left), copy.deepcopy(right)
        erd.schema_diff(left, right, include_provenance=True)
        self.assertEqual(left, left_before)
        self.assertEqual(right, right_before)


# ---------------------------------------------------------------------------
# CLI wiring — driven through main(), mirroring tests/test_emit_json.py's
# _EmitJsonDriver technique (stubbed parse_mysql, a temp cwd, sys.argv
# juggling).
# ---------------------------------------------------------------------------
def _dbcol(t, name, dtype, ctype, null='YES', key='', default='', extra='', comment=''):
    return (t, name, dtype, ctype, null, key, default, extra, comment)


TABLE_ROWS = [('users', ''), ('posts', '')]
COL_ROWS = [
    _dbcol('users', 'id', 'bigint', 'bigint', 'NO', 'PRI', '', 'auto_increment'),
    _dbcol('users', 'email', 'varchar', 'varchar(255)', 'NO', 'UNI'),
    _dbcol('posts', 'id', 'bigint', 'bigint', 'NO', 'PRI', '', 'auto_increment'),
    _dbcol('posts', 'user_id', 'bigint', 'bigint', 'NO', 'MUL'),
    _dbcol('posts', 'title', 'varchar', 'varchar(255)', 'NO'),
]
FK_ROWS = [('posts', 'user_id', 'users')]
INDEX_ROWS = [('users', 'PRIMARY', 0, 1, 'id')]


class _DiffDriver(unittest.TestCase):
    def setUp(self):
        self._orig_parse = erd.parse_mysql
        self._orig_argv = sys.argv
        self._orig_cwd = os.getcwd()
        self.addCleanup(lambda: setattr(erd, 'parse_mysql', self._orig_parse))
        self.addCleanup(lambda: setattr(sys, 'argv', self._orig_argv))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # chdir-back must be the LAST cleanup registered (addCleanup runs
        # LIFO) so it runs BEFORE tmp.cleanup — otherwise the temp dir is
        # still the process cwd when its own removal is attempted, which
        # Windows refuses (WinError 32/5); POSIX allows it, which is why
        # this only shows up there.
        self.addCleanup(os.chdir, self._orig_cwd)
        os.chdir(self.tmp.name)
        erd.parse_mysql = lambda url: erd.mysql_ir(TABLE_ROWS, COL_ROWS, FK_ROWS, INDEX_ROWS)

    def _run(self, *cli_args):
        sys.argv = ['erd.py', 'mysql://x@localhost/testdb', *cli_args]
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            erd.main()
        return out_buf.getvalue(), err_buf.getvalue()

    def _snapshot(self, name='snap.json'):
        """Generate a real --emit-json snapshot of the fixture schema (via
        the actual CLI path, not hand-built) so CLI-level tests exercise a
        genuine emit-json document — same technique the round-trip tests in
        tests/test_emit_config.py use for their config fixtures."""
        self._run('-o', 'gen.html', '--emit-json', name)
        return Path(self.tmp.name) / name


class TestCLIExitCodes(_DiffDriver):
    def test_identical_snapshot_exits_zero(self):
        snap = self._snapshot()
        with self.assertRaises(SystemExit) as cm:
            self._run('-o', 'out.html', '--diff', str(snap))
        self.assertEqual(cm.exception.code, 0)

    def test_different_snapshot_exits_one(self):
        snap = self._snapshot()
        doc = json.loads(snap.read_text())
        doc['schema']['tables']['users']['comment'] = 'CHANGED'
        doc['fingerprint'] = 'sha256:' + '0' * 64  # force a fingerprint mismatch
        snap.write_text(json.dumps(doc))
        with self.assertRaises(SystemExit) as cm:
            self._run('-o', 'out.html', '--diff', str(snap))
        self.assertEqual(cm.exception.code, 1)

    def test_diff_exit_zero_overrides_difference_exit_code(self):
        snap = self._snapshot()
        doc = json.loads(snap.read_text())
        doc['schema']['tables']['users']['comment'] = 'CHANGED'
        doc['fingerprint'] = 'sha256:' + '0' * 64
        snap.write_text(json.dumps(doc))
        with self.assertRaises(SystemExit) as cm:
            self._run('-o', 'out.html', '--diff', str(snap), '--diff-exit-zero')
        self.assertEqual(cm.exception.code, 0)

    def test_missing_file_exits_two(self):
        with self.assertRaises(SystemExit) as cm:
            self._run('-o', 'out.html', '--diff', 'does-not-exist.json')
        self.assertEqual(cm.exception.code, 2)

    def test_invalid_json_exits_two(self):
        Path(self.tmp.name, 'bad.json').write_text('not json')
        with self.assertRaises(SystemExit) as cm:
            self._run('-o', 'out.html', '--diff', 'bad.json')
        self.assertEqual(cm.exception.code, 2)

    def test_non_emit_json_document_exits_two(self):
        # A config-authoring document (--emit-config's own shape) has no
        # top-level format/schema — must be rejected, not silently
        # misread as a canonical schema.
        Path(self.tmp.name, 'config.json').write_text(
            json.dumps({'version': 1, 'tables': {'users': {}}}))
        with self.assertRaises(SystemExit) as cm:
            self._run('-o', 'out.html', '--diff', 'config.json')
        self.assertEqual(cm.exception.code, 2)

    def test_wrong_format_number_exits_two(self):
        Path(self.tmp.name, 'v2.json').write_text(
            json.dumps({'format': 2, 'schema': {'tables': {}}}))
        with self.assertRaises(SystemExit) as cm:
            self._run('-o', 'out.html', '--diff', 'v2.json')
        self.assertEqual(cm.exception.code, 2)

    def test_malformed_snapshot_exits_two(self):
        # Shape-valid (format/schema/tables all present) but internally broken:
        # an association with no `type` KeyErrors deep in schema_diff. That
        # must exit 2 ("the comparison couldn't run"), never surface as a
        # traceback→exit 1, which a CI gate would misread as "drift found".
        snap = self._snapshot()
        doc = json.loads(snap.read_text())
        tname = next(iter(doc['schema']['tables']))
        doc['schema']['tables'][tname]['associations'] = [{}]  # no 'type'
        snap.write_text(json.dumps(doc))
        with self.assertRaises(SystemExit) as cm:
            self._run('-o', 'out.html', '--diff', str(snap))
        self.assertEqual(cm.exception.code, 2)

    def test_stale_fingerprint_does_not_mask_a_real_change(self):
        # The fast path recomputes the snapshot's fingerprint from its schema
        # rather than trusting the stored one — a schema edited WITHOUT
        # updating `fingerprint` must still be detected as different (else a
        # CI gate silently passes on a genuinely drifted snapshot).
        snap = self._snapshot()
        doc = json.loads(snap.read_text())
        stale_fp = doc['fingerprint']  # keep the ORIGINAL fp on purpose
        tname = next(iter(doc['schema']['tables']))
        doc['schema']['tables'][tname]['comment'] = 'HAND-EDITED'
        doc['fingerprint'] = stale_fp
        snap.write_text(json.dumps(doc))
        with self.assertRaises(SystemExit) as cm:
            self._run('-o', 'out.html', '--diff', str(snap))
        self.assertEqual(cm.exception.code, 1)

    def test_column_reorder_is_not_a_difference(self):
        # level1 matches columns by name, so the same columns in a different
        # order compare equal (documented limitation). The reorder changes the
        # snapshot's byte content so the fingerprint fast path is skipped, and
        # the order-agnostic deep compare then finds no difference: exit 0.
        snap = self._snapshot()
        doc = json.loads(snap.read_text())
        tname = next(t for t, td in doc['schema']['tables'].items()
                     if len(td.get('columns', [])) >= 2)
        doc['schema']['tables'][tname]['columns'].reverse()
        snap.write_text(json.dumps(doc))
        with self.assertRaises(SystemExit) as cm:
            self._run('-o', 'out.html', '--diff', str(snap))
        self.assertEqual(cm.exception.code, 0)


class TestCLIConflictingFlags(_DiffDriver):
    def test_diff_with_emit_json_exits_two(self):
        snap = self._snapshot()
        with self.assertRaises(SystemExit) as cm:
            self._run('-o', 'out.html', '--diff', str(snap), '--emit-json', 'x.json')
        self.assertEqual(cm.exception.code, 2)
        self.assertFalse((Path(self.tmp.name) / 'x.json').exists())

    def test_diff_with_emit_config_exits_two(self):
        snap = self._snapshot()
        with self.assertRaises(SystemExit) as cm:
            self._run('-o', 'out.html', '--diff', str(snap), '--emit-config', 'x.json')
        self.assertEqual(cm.exception.code, 2)

    def test_diff_with_excel_exits_two(self):
        snap = self._snapshot()
        with self.assertRaises(SystemExit) as cm:
            self._run('-o', 'out.html', '--diff', str(snap), '--excel', 'x.xlsx')
        self.assertEqual(cm.exception.code, 2)


class TestCLINoOutputGenerated(_DiffDriver):
    def test_diff_writes_no_html_excel_or_emit_files(self):
        snap = self._snapshot()
        before = set(os.listdir(self.tmp.name))
        with self.assertRaises(SystemExit):
            self._run('-o', 'should-not-exist.html', '--diff', str(snap))
        after = set(os.listdir(self.tmp.name))
        self.assertEqual(before, after)
        self.assertFalse((Path(self.tmp.name) / 'should-not-exist.html').exists())


class TestCLIFormatAndOutput(_DiffDriver):
    def test_diff_format_json_is_deterministic_and_parses(self):
        snap = self._snapshot()
        doc = json.loads(snap.read_text())
        doc['schema']['tables']['users']['comment'] = 'CHANGED'
        doc['fingerprint'] = 'sha256:' + '0' * 64
        snap.write_text(json.dumps(doc))

        out1 = self._capture_stdout('-o', 'a.html', '--diff', str(snap), '--diff-format', 'json')
        out2 = self._capture_stdout('-o', 'b.html', '--diff', str(snap), '--diff-format', 'json')
        self.assertEqual(out1, out2)
        parsed = json.loads(out1)
        self.assertIn('tables', parsed)

    def _capture_stdout(self, *cli_args):
        sys.argv = ['erd.py', 'mysql://x@localhost/testdb', *cli_args]
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            try:
                erd.main()
            except SystemExit:
                pass
        return out_buf.getvalue()

    def test_direction_added_is_current_only_removed_is_snapshot_only(self):
        snap = self._snapshot()
        doc = json.loads(snap.read_text())
        # snapshot ('right'/base) has an extra table the current run lacks,
        # and lacks a table the current run has — verifies BOTH directions
        # in one document.
        doc['schema']['tables']['archived'] = {'columns': [{'name': 'id', 'type': 'integer',
                                                            'nullable': False, 'primary': True}],
                                               'indexes': [], 'associations': []}
        del doc['schema']['tables']['posts']
        doc['fingerprint'] = 'sha256:' + '0' * 64
        snap.write_text(json.dumps(doc))
        out = self._capture_stdout('-o', 'a.html', '--diff', str(snap), '--diff-format', 'json')
        parsed = json.loads(out)
        self.assertEqual(parsed['tables']['added'], ['posts'])
        self.assertEqual(parsed['tables']['removed'], ['archived'])

    def test_fingerprint_fast_path_skips_deep_compare(self):
        snap = self._snapshot()
        orig = erd.schema_diff

        def _boom(*a, **k):
            raise AssertionError('schema_diff should not be called on a fingerprint match')

        erd.schema_diff = _boom
        self.addCleanup(lambda: setattr(erd, 'schema_diff', orig))
        with self.assertRaises(SystemExit) as cm:
            self._run('-o', 'a.html', '--diff', str(snap))
        self.assertEqual(cm.exception.code, 0)


if __name__ == '__main__':
    unittest.main()
