"""--emit-mermaid — Mermaid erDiagram export of the schema.

Covers the new src/erdscope/mermaid.py surface (render_mermaid,
emit_mermaid_document) as direct unit tests against hand-built canonical
schemas (mirrors tests/test_emit_dbml.py's style — no need to route through
merge_ir/canonical_schema for tests that are purely about rendering), plus
CLI-level wiring tests (--emit-mermaid alongside --excel/--emit-json/
--emit-config/--emit-digest/--emit-dbml/HTML, `-` for stdout, output-
collision guard, --only/--exclude reflected, --diff rejecting the
combination) driven through main() the same way tests/test_emit_dbml.py's
_EmitDbmlDriver does.

Run from the repository root:
    python3 -m unittest tests.test_emit_mermaid -v
"""
import contextlib
import copy
import importlib.util
import io
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


def _table(columns, indexes=None, associations=None, comment=None):
    t = {'columns': columns, 'indexes': indexes or [], 'associations': associations or []}
    if comment:
        t['comment'] = comment
    return t


# ---------------------------------------------------------------------------
# simple table: columns, PK/FK markers, pseudo-column skip
# ---------------------------------------------------------------------------
class TestSimpleTable(unittest.TestCase):
    def test_golden_output(self):
        schema = {'tables': {
            'users': _table([_col('id', primary=True), _col('email', type_='string')]),
            'posts': _table([_col('id', primary=True), _col('user_id'), _col('title', type_='string')],
                            associations=[{'type': 'belongs_to', 'target': 'users',
                                          'foreign_key': 'user_id', 'name': 'user'}]),
        }}
        out = erd.render_mermaid(schema)
        self.assertEqual(out, (
            'erDiagram\n'
            '    users ||--o{ posts : "user"\n'
            '    posts {\n'
            '        integer id PK\n'
            '        integer user_id FK\n'
            '        string title\n'
            '    }\n'
            '    users {\n'
            '        integer id PK\n'
            '        string email\n'
            '    }\n'
        ))

    def test_type_falls_back_to_string_when_empty(self):
        schema = {'tables': {'t': _table([_col('a', type_='')])}}
        out = erd.render_mermaid(schema)
        self.assertIn('        string a\n', out)

    def test_pk_marker_takes_precedence(self):
        schema = {'tables': {'t': _table([_col('id', primary=True)])}}
        out = erd.render_mermaid(schema)
        self.assertIn('        integer id PK\n', out)

    def test_fk_marker_from_local_foreign_key_recomputation(self):
        schema = {'tables': {
            'a': _table([_col('id', primary=True)]),
            'b': _table([_col('id', primary=True), _col('a_id')],
                       associations=[{'type': 'belongs_to', 'target': 'a', 'foreign_key': 'a_id'}]),
        }}
        out = erd.render_mermaid(schema)
        self.assertIn('        integer a_id FK\n', out)

    def test_no_pk_no_fk_bare_line(self):
        schema = {'tables': {'t': _table([_col('name', type_='string')])}}
        out = erd.render_mermaid(schema)
        self.assertIn('        string name\n', out)

    def test_pseudo_column_skipped(self):
        schema = {'tables': {'t': _table([_col('id', primary=True), _col('lower(email)')])}}
        out = erd.render_mermaid(schema)
        self.assertNotIn('lower(email)', out)

    def test_tables_rendered_in_name_order(self):
        schema = {'tables': {
            'zebra': _table([_col('id', primary=True)]),
            'apple': _table([_col('id', primary=True)]),
        }}
        out = erd.render_mermaid(schema)
        self.assertLess(out.index('apple {'), out.index('zebra {'))


# ---------------------------------------------------------------------------
# cardinality — the 4 kinds
# ---------------------------------------------------------------------------
class TestCardinality(unittest.TestCase):
    def test_through_only_is_many_to_many(self):
        schema = {'tables': {
            'a': _table([_col('id', primary=True)],
                       associations=[{'type': 'has_many', 'target': 'b', 'through': 'join_x'}]),
            'b': _table([_col('id', primary=True)]),
        }}
        out = erd.render_mermaid(schema)
        self.assertIn('    a }o--o{ b : ""\n', out)

    def test_habtm_only_is_many_to_many(self):
        schema = {'tables': {
            'a': _table([_col('id', primary=True)],
                       associations=[{'type': 'has_and_belongs_to_many', 'target': 'b',
                                     'foreign_key': 'b_id'}]),
            'b': _table([_col('id', primary=True)]),
        }}
        out = erd.render_mermaid(schema)
        self.assertIn('}o--o{', out)

    def test_has_one_is_one_to_one(self):
        schema = {'tables': {
            'a': _table([_col('id', primary=True)],
                       associations=[{'type': 'has_one', 'target': 'b'}]),
            'b': _table([_col('id', primary=True)]),
        }}
        out = erd.render_mermaid(schema)
        self.assertIn('    a ||--|| b : ""\n', out)

    def test_has_many_declaring_side_is_one_target_is_many(self):
        schema = {'tables': {
            'owner': _table([_col('id', primary=True)],
                            associations=[{'type': 'has_many', 'target': 'child'}]),
            'child': _table([_col('id', primary=True)]),
        }}
        out = erd.render_mermaid(schema)
        self.assertIn('    owner ||--o{ child : ""\n', out)

    def test_belongs_to_declaring_side_is_many(self):
        schema = {'tables': {
            'users': _table([_col('id', primary=True)]),
            'posts': _table([_col('id', primary=True), _col('user_id')],
                            associations=[{'type': 'belongs_to', 'target': 'users',
                                          'foreign_key': 'user_id'}]),
        }}
        out = erd.render_mermaid(schema)
        self.assertIn('    users ||--o{ posts : ""\n', out)

    def test_has_many_and_reverse_belongs_to_agree_on_direction(self):
        # 'posts' (alphabetically first) declares belongs_to -> its association
        # creates the edge entry first (edge.source='posts'); 'users' has_many
        # is appended second, so hm.from != edge.source — exercises the
        # "else: many = edge.source" branch and must still resolve posts=many.
        schema = {'tables': {
            'users': _table([_col('id', primary=True)],
                            associations=[{'type': 'has_many', 'target': 'posts'}]),
            'posts': _table([_col('id', primary=True), _col('user_id')],
                            associations=[{'type': 'belongs_to', 'target': 'users',
                                          'foreign_key': 'user_id'}]),
        }}
        out = erd.render_mermaid(schema)
        self.assertIn('    users ||--o{ posts : ""\n', out)

    def test_join_table_present_suppresses_direct_habtm_edge(self):
        schema = {'tables': {
            'a': _table([_col('id', primary=True)],
                       associations=[{'type': 'has_and_belongs_to_many', 'target': 'b',
                                     'foreign_key': 'b_id'}]),
            'b': _table([_col('id', primary=True)]),
            'a_b': _table([_col('id', primary=True)]),
        }}
        out = erd.render_mermaid(schema)
        self.assertNotIn('a }o--o{ b', out)
        self.assertNotIn('a ||--', out)

    def test_through_join_table_present_suppresses_direct_edge(self):
        schema = {'tables': {
            'a': _table([_col('id', primary=True)],
                       associations=[{'type': 'has_many', 'target': 'b', 'through': 'j'}]),
            'b': _table([_col('id', primary=True)]),
            'j': _table([_col('id', primary=True)]),
        }}
        out = erd.render_mermaid(schema)
        self.assertNotIn('a }o--o{ b', out)

    def test_polymorphic_association_skipped(self):
        schema = {'tables': {
            'comments': _table([_col('id', primary=True), _col('commentable_id')],
                               associations=[{'type': 'belongs_to', 'target': 'commentable',
                                             'foreign_key': 'commentable_id', 'polymorphic': True}]),
        }}
        out = erd.render_mermaid(schema)
        self.assertNotIn('--', out.split('erDiagram')[1].split('comments {')[0])

    def test_dangling_target_skipped_defensively(self):
        schema = {'tables': {
            'a': _table([_col('id', primary=True)],
                       associations=[{'type': 'belongs_to', 'target': 'ghost', 'foreign_key': 'g_id'}]),
        }}
        out = erd.render_mermaid(schema)
        self.assertNotIn('ghost', out)


# ---------------------------------------------------------------------------
# label quoting/escaping
# ---------------------------------------------------------------------------
class TestLabelEscaping(unittest.TestCase):
    def test_label_quote_replaced_with_single_quote(self):
        schema = {'tables': {
            'a': _table([_col('id', primary=True)],
                       associations=[{'type': 'has_one', 'target': 'b', 'name': 'the "thing"'}]),
            'b': _table([_col('id', primary=True)]),
        }}
        out = erd.render_mermaid(schema)
        self.assertIn(': "the \'thing\'"\n', out)
        self.assertNotIn('the "thing"', out)

    def test_no_name_renders_empty_label(self):
        schema = {'tables': {
            'a': _table([_col('id', primary=True)],
                       associations=[{'type': 'has_one', 'target': 'b'}]),
            'b': _table([_col('id', primary=True)]),
        }}
        out = erd.render_mermaid(schema)
        self.assertIn(' : ""\n', out)


# ---------------------------------------------------------------------------
# determinism / purity
# ---------------------------------------------------------------------------
class TestDeterminism(unittest.TestCase):
    def _schema(self):
        return {'tables': {
            'users': _table([_col('id', primary=True), _col('email', type_='string')]),
            'posts': _table([_col('id', primary=True), _col('user_id')],
                            associations=[{'type': 'belongs_to', 'target': 'users',
                                          'foreign_key': 'user_id', 'name': 'user'}]),
        }}

    def test_same_schema_same_output(self):
        schema = self._schema()
        self.assertEqual(erd.render_mermaid(schema), erd.render_mermaid(copy.deepcopy(schema)))

    def test_table_dict_reorder_does_not_change_output(self):
        schema = self._schema()
        reversed_schema = {**schema,
                           'tables': {k: schema['tables'][k]
                                     for k in reversed(list(schema['tables']))}}
        out1 = erd.render_mermaid(schema)
        out2 = erd.render_mermaid(reversed_schema)
        self.assertEqual(out1, out2)

    def test_association_list_reorder_does_not_change_output(self):
        schema = {'tables': {
            'a': _table([_col('id', primary=True)],
                       associations=[
                           {'type': 'has_many', 'target': 'b'},
                           {'type': 'has_many', 'target': 'c'},
                       ]),
            'b': _table([_col('id', primary=True)]),
            'c': _table([_col('id', primary=True)]),
        }}
        reordered = copy.deepcopy(schema)
        reordered['tables']['a']['associations'].reverse()
        self.assertEqual(erd.render_mermaid(schema), erd.render_mermaid(reordered))

    def test_ends_with_single_trailing_newline(self):
        out = erd.render_mermaid(self._schema())
        self.assertTrue(out.endswith('\n'))
        self.assertFalse(out.endswith('\n\n'))

    def test_emit_mermaid_document_matches_render_mermaid_of_canonical_schema(self):
        tables = {
            'users': {'columns': [_col('id', primary=True)], 'indexes': [], 'associations': [],
                      'fk_columns': []},
        }
        expected = erd.render_mermaid(erd.canonical_schema(tables, None, None))
        actual = erd.emit_mermaid_document(tables, None, None)
        self.assertEqual(expected, actual)

    def test_notes_and_groups_ignored(self):
        tables = {
            'users': {'columns': [_col('id', primary=True)], 'indexes': [], 'associations': [],
                      'fk_columns': []},
        }
        notes = [{'id': 'n1', 'scope': 'global', 'text': 'hello'}]
        groups = [{'id': 'g1', 'tables': ['users'], 'title': 'Core'}]
        with_notes = erd.emit_mermaid_document(tables, notes, groups)
        without_notes = erd.emit_mermaid_document(tables, None, None)
        self.assertEqual(with_notes, without_notes)


class TestPurity(unittest.TestCase):
    def test_schema_unchanged_after_render(self):
        schema = {'tables': {
            'users': _table([_col('id', primary=True)]),
            'posts': _table([_col('id', primary=True), _col('user_id')],
                            associations=[{'type': 'belongs_to', 'target': 'users',
                                          'foreign_key': 'user_id'}]),
        }}
        before = copy.deepcopy(schema)
        erd.render_mermaid(schema)
        self.assertEqual(schema, before)

    def test_tables_unchanged_after_emit_mermaid_document(self):
        tables = {
            'users': {'columns': [_col('id', primary=True)], 'indexes': [], 'associations': [],
                      'fk_columns': []},
        }
        before = copy.deepcopy(tables)
        erd.emit_mermaid_document(tables, None, None)
        self.assertEqual(tables, before)


# ---------------------------------------------------------------------------
# CLI wiring — driven through main(), mirroring test_emit_dbml.py's
# _EmitDbmlDriver technique (stubbed parse_mysql, a temp cwd, sys.argv
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


class _EmitMermaidDriver(unittest.TestCase):
    def setUp(self):
        self._orig_parse = erd.parse_mysql
        self._orig_argv = sys.argv
        self._orig_cwd = os.getcwd()
        self.addCleanup(lambda: setattr(erd, 'parse_mysql', self._orig_parse))
        self.addCleanup(lambda: setattr(sys, 'argv', self._orig_argv))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # chdir-back registered last so it runs first (addCleanup is LIFO) —
        # Windows can't rmtree a directory that's still the process cwd.
        self.addCleanup(os.chdir, self._orig_cwd)
        os.chdir(self.tmp.name)
        erd.parse_mysql = lambda url: erd.mysql_ir(TABLE_ROWS, COL_ROWS, FK_ROWS, INDEX_ROWS)

    def _run(self, *cli_args, capture_stdout=False, capture_stderr=False):
        sys.argv = ['erd.py', 'mysql://x@localhost/testdb', *cli_args]
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            erd.main()
        return out_buf.getvalue(), err_buf.getvalue()


class TestCLIFileOutput(_EmitMermaidDriver):
    def test_emit_mermaid_writes_alongside_html(self):
        self._run('--emit-mermaid', 'schema.mmd')
        self.assertTrue((Path(self.tmp.name) / 'erd.html').exists())
        text = (Path(self.tmp.name) / 'schema.mmd').read_text()
        self.assertIn('erDiagram', text)
        self.assertIn('posts {', text)
        self.assertIn('users {', text)
        self.assertIn('users ||--o{ posts', text)

    def test_emit_mermaid_reports_generated_path_on_stderr(self):
        _, err = self._run('--emit-mermaid', 'schema.mmd')
        self.assertIn('Generated: schema.mmd', err)

    def test_emit_mermaid_coexists_with_other_emit_flags(self):
        self._run('--emit-mermaid', 'schema.mmd', '--excel', 'defs.xlsx',
                  '--emit-json', 'snap.json', '--emit-config', 'cfg.json',
                  '--emit-digest', 'digest.md', '--emit-dbml', 'schema.dbml')
        for name in ('erd.html', 'defs.xlsx', 'snap.json', 'cfg.json',
                    'digest.md', 'schema.dbml', 'schema.mmd'):
            self.assertTrue((Path(self.tmp.name) / name).exists(), name)


class TestCLIOutputCollision(_EmitMermaidDriver):
    def test_emit_mermaid_colliding_with_html_output_errors(self):
        with self.assertRaises(SystemExit):
            self._run('-o', 'same.mmd', '--emit-mermaid', 'same.mmd')

    def test_emit_mermaid_colliding_with_emit_json_errors(self):
        with self.assertRaises(SystemExit):
            self._run('--emit-mermaid', 'same.out', '--emit-json', 'same.out')

    def test_distinct_paths_and_stdout_do_not_collide(self):
        out, _ = self._run('-o', 'erd.html', '--emit-mermaid', '-')
        self.assertIn('erDiagram', out)


class TestCLIStdout(_EmitMermaidDriver):
    def test_emit_mermaid_dash_writes_to_stdout_and_still_generates_html(self):
        out, _ = self._run('--emit-mermaid', '-')
        self.assertIn('erDiagram', out)
        self.assertTrue((Path(self.tmp.name) / 'erd.html').exists())


class TestCLIFiltering(_EmitMermaidDriver):
    def test_only_reflected_including_dangling_association_pruning(self):
        self._run('--emit-mermaid', 'schema.mmd', '--only', 'posts')
        text = (Path(self.tmp.name) / 'schema.mmd').read_text()
        self.assertIn('posts {', text)
        self.assertNotIn('users {', text)
        self.assertNotIn('--', text.split('erDiagram')[1].split('posts {')[0])

    def test_exclude_reflected(self):
        self._run('--emit-mermaid', 'schema.mmd', '--exclude', 'posts')
        text = (Path(self.tmp.name) / 'schema.mmd').read_text()
        self.assertIn('users {', text)
        self.assertNotIn('posts {', text)


class TestCLIDiffRejectsCombination(_EmitMermaidDriver):
    def test_diff_with_emit_mermaid_exits_2(self):
        snap_path = Path(self.tmp.name) / 'snap.json'
        self._run('--emit-json', str(snap_path))
        with self.assertRaises(SystemExit) as cm:
            self._run('--diff', str(snap_path), '--emit-mermaid', 'schema.mmd')
        self.assertEqual(cm.exception.code, 2)


class TestByteEquality(_EmitMermaidDriver):
    def test_html_byte_identical_with_and_without_emit_mermaid(self):
        self._run('-o', 'a.html')
        self._run('-o', 'b.html', '--emit-mermaid', 'schema.mmd')
        self.assertEqual((Path(self.tmp.name) / 'a.html').read_bytes(),
                         (Path(self.tmp.name) / 'b.html').read_bytes())


if __name__ == '__main__':
    unittest.main()
