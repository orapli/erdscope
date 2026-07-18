"""--emit-json (backlog #0) — canonical JSON schema snapshot + content
fingerprint.

Covers the new src/erdscope/emit.py surface (canonical_schema,
snapshot_fingerprint, emit_json_document) as direct unit tests against
hand-built final-IR table dicts (mirrors tests/test_notes.py's and
tests/test_groups.py's style — no need to route through merge_ir for tests
that are purely about the canonical projection), plus CLI-level wiring tests
(--emit-json alongside --excel/HTML, `-` for stdout, --only/--exclude
reflected including dangling-association pruning) driven through main() the
same way tests/test_pipeline.py does.

The demo/existing-Excel byte-for-byte guarantee is exercised by the existing,
unmodified tests/test_characterization.py (its demo regeneration check and
Excel golden assertions) — --emit-json touches no code on the HTML/Excel
path, so that suite staying green is itself the byte-equality proof for the
demo; this file additionally proves it directly for an arbitrary DB-backed
run (TestByteEquality below).

Run from the repository root:
    python3 -m unittest tests.test_emit_json -v
"""
import copy
import hashlib
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


def _merged_tables():
    """A minimal merged-IR shape (associations carry 'provenance'/'sources',
    like merge_ir's output) — users (1) <- posts (N) belongs_to on user_id.
    Each table also carries an internal-only `fk_columns` key (as merge_ir
    always adds) that must NOT survive the canonical allowlist."""
    return {
        'users': {
            'comment': 'App users',
            'columns': [_col('id', primary=True), _col('email', type_='string')],
            'indexes': [{'name': 'idx_users_email', 'columns': ['email'], 'unique': True}],
            'associations': [
                {'type': 'has_many', 'name': 'posts', 'target': 'posts',
                 'provenance': 'declared',
                 'sources': [{'kind': 'framework', 'provider': 'rails'}]},
            ],
            'fk_columns': [],
        },
        'posts': {
            'columns': [_col('id', primary=True), _col('user_id')],
            'indexes': [],
            'associations': [
                {'type': 'belongs_to', 'name': 'user', 'target': 'users',
                 'foreign_key': 'user_id', 'provenance': 'db_fk',
                 'sources': [{'kind': 'db', 'provider': 'mysql'},
                            {'kind': 'framework', 'provider': 'rails'}]},
            ],
            'fk_columns': ['user_id'],
        },
    }


# ---------------------------------------------------------------------------
# canonical_schema — allowlist / omission rules
# ---------------------------------------------------------------------------
class TestAllowlistAndOmission(unittest.TestCase):
    def test_only_allowlisted_table_keys_survive(self):
        schema = erd.canonical_schema(_merged_tables(), None, None)
        for t in schema['tables'].values():
            self.assertLessEqual(set(t), {'comment', 'columns', 'indexes', 'associations'})
            self.assertIn('columns', t)
            self.assertIn('indexes', t)
            self.assertIn('associations', t)
            self.assertNotIn('fk_columns', t)
            self.assertNotIn('schema_missing', t)

    def test_comment_omitted_when_absent_or_empty(self):
        tables = _merged_tables()
        tables['posts']['comment'] = ''  # falsy -> omitted, not "comment": ""
        schema = erd.canonical_schema(tables, None, None)
        self.assertNotIn('comment', schema['tables']['posts'])
        self.assertEqual(schema['tables']['users']['comment'], 'App users')

    def test_comment_omitted_when_key_absent(self):
        tables = _merged_tables()
        del tables['users']['comment']
        schema = erd.canonical_schema(tables, None, None)
        self.assertNotIn('comment', schema['tables']['users'])

    def test_column_optionals_omitted_when_falsy(self):
        schema = erd.canonical_schema(_merged_tables(), None, None)
        id_col, email_col = schema['tables']['users']['columns']
        self.assertEqual(id_col['name'], 'id')
        self.assertIs(id_col['primary'], True)
        self.assertNotIn('primary', email_col)  # False -> omitted entirely
        user_id_col = schema['tables']['posts']['columns'][1]
        self.assertNotIn('sql_type', user_id_col)
        self.assertNotIn('default', user_id_col)
        self.assertNotIn('extra', user_id_col)
        self.assertNotIn('comment', user_id_col)

    def test_column_always_carries_name_type_nullable(self):
        schema = erd.canonical_schema(_merged_tables(), None, None)
        for c in schema['tables']['posts']['columns']:
            self.assertIn('name', c)
            self.assertIn('type', c)
            self.assertIn('nullable', c)
            self.assertIsInstance(c['nullable'], bool)

    def test_column_order_preserved(self):
        schema = erd.canonical_schema(_merged_tables(), None, None)
        self.assertEqual([c['name'] for c in schema['tables']['posts']['columns']],
                         ['id', 'user_id'])

    def test_index_unnamed_omits_name_key(self):
        tables = _merged_tables()
        tables['posts']['indexes'] = [{'columns': ['user_id'], 'unique': False}]
        schema = erd.canonical_schema(tables, None, None)
        self.assertNotIn('name', schema['tables']['posts']['indexes'][0])
        self.assertEqual(schema['tables']['posts']['indexes'][0]['columns'], ['user_id'])

    def test_association_output_keys_allowlisted(self):
        schema = erd.canonical_schema(_merged_tables(), None, None)
        a = schema['tables']['posts']['associations'][0]
        allowed = {'type', 'target', 'name', 'foreign_key', 'through', 'polymorphic',
                  'provenance', 'sources'}
        self.assertLessEqual(set(a), allowed)
        self.assertEqual(a['type'], 'belongs_to')
        self.assertEqual(a['target'], 'users')


class TestIndexSort(unittest.TestCase):
    def test_indexes_sorted_by_name_columns_unique(self):
        tables = _merged_tables()
        tables['users']['indexes'] = [
            {'name': 'idx_b', 'columns': ['b'], 'unique': False},
            {'columns': ['a'], 'unique': True},   # no name -> "" sorts first
            {'name': 'idx_a', 'columns': ['a'], 'unique': False},
        ]
        schema = erd.canonical_schema(tables, None, None)
        names = [ix.get('name') for ix in schema['tables']['users']['indexes']]
        self.assertEqual(names, [None, 'idx_a', 'idx_b'])


class TestAssociationsSortAndPrune(unittest.TestCase):
    def test_dangling_association_pruned(self):
        tables = _merged_tables()
        tables['posts']['associations'].append(
            {'type': 'belongs_to', 'name': 'ghost', 'target': 'missing_table',
             'provenance': 'declared', 'sources': []})
        schema = erd.canonical_schema(tables, None, None)
        targets = {a['target'] for a in schema['tables']['posts']['associations']}
        self.assertEqual(targets, {'users'})

    def test_all_dangling_yields_empty_list(self):
        tables = _merged_tables()
        tables['posts']['associations'] = [
            {'type': 'belongs_to', 'name': 'ghost', 'target': 'nope',
             'provenance': 'declared', 'sources': []}]
        schema = erd.canonical_schema(tables, None, None)
        self.assertEqual(schema['tables']['posts']['associations'], [])

    def test_associations_sorted_by_type_name_fk_target_through_poly_provenance(self):
        tables = _merged_tables()
        tables['posts']['associations'] = [
            {'type': 'belongs_to', 'name': 'editor', 'target': 'users',
             'foreign_key': 'editor_id', 'provenance': 'manual', 'sources': []},
            {'type': 'belongs_to', 'name': 'author', 'target': 'users',
             'foreign_key': 'user_id', 'provenance': 'db_fk',
             'sources': [{'kind': 'db', 'provider': 'mysql'}]},
        ]
        schema = erd.canonical_schema(tables, None, None)
        names = [a['name'] for a in schema['tables']['posts']['associations']]
        self.assertEqual(names, ['author', 'editor'])  # 'author' < 'editor'

    def test_polymorphic_true_and_absent_do_not_crash_sort(self):
        # regression guard: a naive `polymorphic or ''` sort key would try to
        # compare True against '' the moment two associations tie on every
        # earlier field, which raises TypeError in Python 3.
        tables = _merged_tables()
        tables['posts']['associations'] = [
            {'type': 'belongs_to', 'name': 'commentable', 'target': 'users',
             'provenance': 'declared', 'sources': [], 'polymorphic': True},
            {'type': 'belongs_to', 'name': 'commentable', 'target': 'users',
             'provenance': 'declared', 'sources': []},
        ]
        schema = erd.canonical_schema(tables, None, None)  # must not raise
        self.assertEqual(len(schema['tables']['posts']['associations']), 2)

    def test_polymorphic_with_tableless_target_is_kept(self):
        # A real polymorphic belongs_to (django GenericForeignKey / rails
        # polymorphic) carries a synthetic target that is NOT a real table —
        # here 'commentables', with no such table in the set. It must survive:
        # the HTML/Excel path keeps it (details-pane, no edge), so the
        # canonical snapshot must too, or every polymorphic relation silently
        # vanishes from --emit-json (and every downstream diff/digest/DBML).
        tables = _merged_tables()
        tables['posts']['associations'] = [
            {'type': 'belongs_to', 'name': 'commentable', 'target': 'commentables',
             'polymorphic': True, 'provenance': 'declared', 'sources': []},
            {'type': 'belongs_to', 'name': 'ghost', 'target': 'gone',
             'provenance': 'declared', 'sources': []},  # truly dangling -> pruned
        ]
        assocs = erd.canonical_schema(tables, None, None)['tables']['posts']['associations']
        self.assertEqual([a['name'] for a in assocs], ['commentable'])
        self.assertTrue(assocs[0]['polymorphic'])


# ---------------------------------------------------------------------------
# provenance / sources
# ---------------------------------------------------------------------------
class TestProvenance(unittest.TestCase):
    def test_merged_ir_preserves_provenance_and_dedupes_sorts_sources(self):
        tables = _merged_tables()
        tables['posts']['associations'][0]['sources'] = [
            {'kind': 'framework', 'provider': 'rails'},
            {'kind': 'db', 'provider': 'mysql'},
            {'kind': 'db', 'provider': 'mysql'},  # duplicate
        ]
        schema = erd.canonical_schema(tables, None, None)
        a = schema['tables']['posts']['associations'][0]
        self.assertEqual(a['provenance'], 'db_fk')
        self.assertEqual(a['sources'], [{'kind': 'db', 'provider': 'mysql'},
                                        {'kind': 'framework', 'provider': 'rails'}])

    def test_legacy_ir_derives_provenance_and_omits_sources(self):
        tables = {
            'users': {'columns': [_col('id', primary=True)], 'indexes': [], 'associations': []},
            'posts': {'columns': [_col('id', primary=True), _col('user_id')],
                      'indexes': [],
                      'associations': [
                          {'type': 'belongs_to', 'name': 'user', 'target': 'users',
                           'foreign_key': 'user_id', 'db_fk': True},
                      ]},
        }
        schema = erd.canonical_schema(tables, None, None)
        a = schema['tables']['posts']['associations'][0]
        self.assertEqual(a['provenance'], 'db_fk')
        self.assertNotIn('sources', a)

    def test_legacy_bare_association_is_declared(self):
        tables = {
            'users': {'columns': [_col('id', primary=True)], 'indexes': [], 'associations': []},
            'posts': {'columns': [_col('id', primary=True), _col('user_id')],
                      'indexes': [],
                      'associations': [
                          {'type': 'belongs_to', 'name': 'user', 'target': 'users',
                           'foreign_key': 'user_id'},
                      ]},
        }
        schema = erd.canonical_schema(tables, None, None)
        self.assertEqual(schema['tables']['posts']['associations'][0]['provenance'], 'declared')

    def test_legacy_boolean_flags_never_leak_into_output(self):
        tables = {
            'users': {'columns': [_col('id', primary=True)], 'indexes': [], 'associations': []},
            'posts': {'columns': [_col('id', primary=True), _col('user_id')],
                      'indexes': [],
                      'associations': [
                          {'type': 'belongs_to', 'name': 'user', 'target': 'users',
                           'foreign_key': 'user_id', 'manual': True, 'inferred': False},
                      ]},
        }
        schema = erd.canonical_schema(tables, None, None)
        a = schema['tables']['posts']['associations'][0]
        for flag in ('db_fk', 'inferred', 'manual', 'schema_fk'):
            self.assertNotIn(flag, a)
        self.assertEqual(a['provenance'], 'manual')

    def test_unknown_provenance_raises(self):
        tables = {
            'posts': {'columns': [_col('id', primary=True)], 'indexes': [],
                      'associations': [
                          {'type': 'belongs_to', 'name': 'x', 'target': 'posts',
                           'provenance': 'bogus', 'sources': []}]},
        }
        with self.assertRaises(ValueError):
            erd.canonical_schema(tables, None, None)


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------
class TestDeterminism(unittest.TestCase):
    def test_table_insertion_order_does_not_affect_output_or_fingerprint(self):
        t1 = _merged_tables()
        t2 = {k: t1[k] for k in reversed(list(t1))}
        s1 = erd.canonical_schema(t1, None, None)
        s2 = erd.canonical_schema(t2, None, None)
        self.assertEqual(s1, s2)
        self.assertEqual(erd.snapshot_fingerprint(s1), erd.snapshot_fingerprint(s2))

    def test_notes_reorder_does_not_affect_document(self):
        tables = _merged_tables()
        notes = [
            {'id': 'n2', 'scope': 'global', 'text': 'two'},
            {'id': 'n1', 'scope': 'table', 'table': 'users', 'text': 'one'},
        ]
        doc1 = erd.emit_json_document(tables, notes, None)
        doc2 = erd.emit_json_document(tables, list(reversed(notes)), None)
        self.assertEqual(doc1, doc2)

    def test_groups_reorder_and_table_order_does_not_affect_document(self):
        tables = _merged_tables()
        groups_a = [{'id': 'g2', 'tables': ['posts', 'users']}, {'id': 'g1', 'tables': ['users']}]
        groups_b = [{'id': 'g1', 'tables': ['users']}, {'id': 'g2', 'tables': ['users', 'posts']}]
        doc1 = erd.emit_json_document(tables, None, groups_a)
        doc2 = erd.emit_json_document(tables, None, groups_b)
        self.assertEqual(doc1, doc2)

    def test_sources_reorder_does_not_affect_output(self):
        baseline = erd.canonical_schema(_merged_tables(), None, None)
        tables = _merged_tables()
        tables['posts']['associations'][0]['sources'] = list(
            reversed(tables['posts']['associations'][0]['sources']))
        reordered = erd.canonical_schema(tables, None, None)
        self.assertEqual(baseline, reordered)


# ---------------------------------------------------------------------------
# fingerprint
# ---------------------------------------------------------------------------
class TestFingerprint(unittest.TestCase):
    def test_same_schema_same_fingerprint(self):
        schema = erd.canonical_schema(_merged_tables(), None, None)
        self.assertEqual(erd.snapshot_fingerprint(schema),
                         erd.snapshot_fingerprint(copy.deepcopy(schema)))

    def test_column_change_changes_fingerprint(self):
        t1 = _merged_tables()
        t2 = _merged_tables()
        t2['users']['columns'][1]['type'] = 'text'
        f1 = erd.snapshot_fingerprint(erd.canonical_schema(t1, None, None))
        f2 = erd.snapshot_fingerprint(erd.canonical_schema(t2, None, None))
        self.assertNotEqual(f1, f2)

    def test_fingerprint_has_sha256_prefix_and_valid_hex_digest(self):
        schema = erd.canonical_schema(_merged_tables(), None, None)
        fp = erd.snapshot_fingerprint(schema)
        self.assertTrue(fp.startswith('sha256:'))
        hexpart = fp[len('sha256:'):]
        self.assertEqual(len(hexpart), 64)
        int(hexpart, 16)  # raises ValueError if not hex

    def test_format_is_baked_into_the_hashed_payload(self):
        # snapshot_fingerprint hashes {"format": 1, "schema": schema}; format
        # isn't a parameter, so this checks the design property directly —
        # changing `format` in that payload (a future format bump) changes
        # the digest even for byte-identical schema content, so format 1 and
        # a hypothetical format 2 never collide.
        schema = erd.canonical_schema(_merged_tables(), None, None)
        payload1 = json.dumps({'format': 1, 'schema': schema}, sort_keys=True,
                              ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        payload2 = json.dumps({'format': 2, 'schema': schema}, sort_keys=True,
                              ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        fp1 = 'sha256:' + hashlib.sha256(payload1.encode('utf-8')).hexdigest()
        fp2 = 'sha256:' + hashlib.sha256(payload2.encode('utf-8')).hexdigest()
        self.assertEqual(fp1, erd.snapshot_fingerprint(schema))
        self.assertNotEqual(fp1, fp2)


class TestAllowNan(unittest.TestCase):
    def test_nan_in_schema_is_rejected(self):
        tables = _merged_tables()
        tables['posts']['columns'][1]['default'] = float('nan')
        with self.assertRaises(ValueError):
            erd.emit_json_document(tables, None, None)

    def test_infinity_in_schema_is_rejected(self):
        tables = _merged_tables()
        tables['posts']['columns'][1]['default'] = float('inf')
        with self.assertRaises(ValueError):
            erd.snapshot_fingerprint(erd.canonical_schema(tables, None, None))


# ---------------------------------------------------------------------------
# purity / non-destructiveness
# ---------------------------------------------------------------------------
class TestPurity(unittest.TestCase):
    def test_input_tables_unchanged_after_canonical_schema(self):
        tables = _merged_tables()
        before = copy.deepcopy(tables)
        erd.canonical_schema(tables, None, None)
        self.assertEqual(tables, before)

    def test_input_notes_and_groups_unchanged(self):
        tables = _merged_tables()
        notes = [{'id': 'n1', 'scope': 'global', 'text': 'hi'}]
        groups = [{'id': 'g1', 'tables': ['posts', 'users']}]
        before_notes, before_groups = copy.deepcopy(notes), copy.deepcopy(groups)
        erd.canonical_schema(tables, notes, groups)
        self.assertEqual(notes, before_notes)
        self.assertEqual(groups, before_groups)

    def test_input_tables_unchanged_after_emit_json_document(self):
        tables = _merged_tables()
        before = copy.deepcopy(tables)
        erd.emit_json_document(tables, None, None)
        self.assertEqual(tables, before)


# ---------------------------------------------------------------------------
# CLI wiring — driven through main(), mirroring tests/test_pipeline.py's
# _MainDriver technique (stubbed parse_mysql, a temp cwd, sys.argv juggling).
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


class _EmitJsonDriver(unittest.TestCase):
    def setUp(self):
        self._orig_parse = erd.parse_mysql
        self._orig_argv = sys.argv
        self._orig_cwd = os.getcwd()
        self.addCleanup(lambda: setattr(erd, 'parse_mysql', self._orig_parse))
        self.addCleanup(lambda: setattr(sys, 'argv', self._orig_argv))
        self.addCleanup(os.chdir, self._orig_cwd)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.chdir(self.tmp.name)
        erd.parse_mysql = lambda url: erd.mysql_ir(TABLE_ROWS, COL_ROWS, FK_ROWS, INDEX_ROWS)

    def _run(self, *cli_args, capture_stdout=False, capture_stderr=False):
        sys.argv = ['erd.py', 'mysql://x@localhost/testdb', *cli_args]
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            erd.main()
        return out_buf.getvalue(), err_buf.getvalue()


class TestCLIFileOutput(_EmitJsonDriver):
    def test_emit_json_writes_alongside_html(self):
        self._run('--emit-json', 'snap.json')
        self.assertTrue((Path(self.tmp.name) / 'erd.html').exists())
        snap = json.loads((Path(self.tmp.name) / 'snap.json').read_text())
        self.assertEqual(snap['format'], 1)
        self.assertTrue(snap['fingerprint'].startswith('sha256:'))
        self.assertEqual(set(snap['schema']['tables']), {'users', 'posts'})

    def test_emit_json_reports_generated_path_on_stderr(self):
        _, err = self._run('--emit-json', 'snap.json')
        self.assertIn('Generated: snap.json', err)

    def test_emit_json_coexists_with_excel(self):
        self._run('--emit-json', 'snap.json', '--excel', 'defs.xlsx')
        self.assertTrue((Path(self.tmp.name) / 'erd.html').exists())
        self.assertTrue((Path(self.tmp.name) / 'defs.xlsx').exists())
        self.assertTrue((Path(self.tmp.name) / 'snap.json').exists())


class TestCLIOutputCollision(_EmitJsonDriver):
    def test_emit_json_colliding_with_html_output_errors(self):
        # -o and --emit-json at the same path would have the JSON clobber the
        # HTML (HTML is written first); reject it up front instead.
        with self.assertRaises(SystemExit):
            self._run('-o', 'same.json', '--emit-json', 'same.json')

    def test_emit_json_colliding_with_excel_errors(self):
        # --excel is written after --emit-json, so a shared path would clobber
        # the JSON with the workbook.
        with self.assertRaises(SystemExit):
            self._run('--emit-json', 'same.xlsx', '--excel', 'same.xlsx')

    def test_distinct_paths_and_stdout_do_not_collide(self):
        # the guard keys on resolved paths, so '-' (stdout) is never a collision
        out, _ = self._run('-o', 'erd.html', '--emit-json', '-')
        self.assertEqual(json.loads(out)['format'], 1)


class TestCLIStdout(_EmitJsonDriver):
    def test_emit_json_dash_writes_to_stdout_and_still_generates_html(self):
        out, _ = self._run('--emit-json', '-')
        doc = json.loads(out)
        self.assertEqual(doc['format'], 1)
        self.assertTrue((Path(self.tmp.name) / 'erd.html').exists())


class TestCLIFiltering(_EmitJsonDriver):
    def test_only_reflected_including_dangling_association_pruning(self):
        self._run('--emit-json', 'snap.json', '--only', 'posts')
        snap = json.loads((Path(self.tmp.name) / 'snap.json').read_text())
        self.assertEqual(set(snap['schema']['tables']), {'posts'})
        # 'users' is filtered out -> posts' belongs_to :user (target 'users')
        # must be pruned as dangling, not just left referencing a ghost table
        self.assertEqual(snap['schema']['tables']['posts']['associations'], [])

    def test_exclude_reflected(self):
        self._run('--emit-json', 'snap.json', '--exclude', 'posts')
        snap = json.loads((Path(self.tmp.name) / 'snap.json').read_text())
        self.assertEqual(set(snap['schema']['tables']), {'users'})


class TestByteEquality(_EmitJsonDriver):
    def test_html_byte_identical_with_and_without_emit_json(self):
        self._run('-o', 'a.html')
        self._run('-o', 'b.html', '--emit-json', 'snap.json')
        self.assertEqual((Path(self.tmp.name) / 'a.html').read_bytes(),
                         (Path(self.tmp.name) / 'b.html').read_bytes())

    def test_excel_byte_identical_with_and_without_emit_json(self):
        self._run('-o', 'a.html', '--excel', 'a.xlsx')
        self._run('-o', 'b.html', '--excel', 'b.xlsx', '--emit-json', 'snap.json')
        self.assertEqual((Path(self.tmp.name) / 'a.xlsx').read_bytes(),
                         (Path(self.tmp.name) / 'b.xlsx').read_bytes())


if __name__ == '__main__':
    unittest.main()
