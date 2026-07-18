"""--emit-plantuml — PlantUML entity-relationship export of the schema.

Covers the new src/erdscope/plantuml.py surface (render_plantuml,
emit_plantuml_document) as direct unit tests against hand-built canonical
schemas (mirrors tests/test_emit_dbml.py's style — no need to route through
merge_ir/canonical_schema for tests that are purely about rendering), plus
CLI-level wiring tests (--emit-plantuml alongside --excel/--emit-json/
--emit-config/--emit-digest/--emit-dbml/--emit-mermaid/HTML, `-` for
stdout, output-collision guard, --only/--exclude reflected, --diff
rejecting the combination) driven through main() the same way
tests/test_emit_dbml.py's _EmitDbmlDriver does.

Run from the repository root:
    python3 -m unittest tests.test_emit_plantuml -v
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
        out = erd.render_plantuml(schema)
        self.assertEqual(out, (
            '@startuml\n'
            'hide circle\n'
            'skinparam linetype ortho\n'
            '\n'
            'entity posts {\n'
            '  * id : integer <<PK>>\n'
            '  --\n'
            '  * user_id : integer <<FK>>\n'
            '  * title : string\n'
            '}\n'
            '\n'
            'entity users {\n'
            '  * id : integer <<PK>>\n'
            '  --\n'
            '  * email : string\n'
            '}\n'
            '\n'
            'users ||--o{ posts : user\n'
            '@enduml\n'
        ))

    def test_type_falls_back_to_string_when_empty(self):
        schema = {'tables': {'t': _table([_col('a', type_='')])}}
        out = erd.render_plantuml(schema)
        self.assertIn('* a : string', out)

    def test_pk_before_separator_before_rest(self):
        schema = {'tables': {'t': _table([_col('id', primary=True), _col('name', type_='string')])}}
        out = erd.render_plantuml(schema)
        block = out.split('entity t {')[1].split('}')[0]
        self.assertLess(block.index('<<PK>>'), block.index('--'))
        self.assertLess(block.index('--'), block.index('name'))

    def test_no_pk_or_no_rest_omits_separator(self):
        schema = {'tables': {'t': _table([_col('id', primary=True)])}}
        out = erd.render_plantuml(schema)
        block = out.split('entity t {')[1].split('}')[0]
        self.assertNotIn('--', block)

    def test_nullable_column_has_no_star_mark(self):
        schema = {'tables': {'t': _table([_col('a'), _col('n', nullable=True)])}}
        out = erd.render_plantuml(schema)
        self.assertIn('  * a : integer\n', out)
        self.assertIn('  n : integer\n', out)

    def test_fk_marker_from_local_foreign_key_recomputation(self):
        schema = {'tables': {
            'a': _table([_col('id', primary=True)]),
            'b': _table([_col('id', primary=True), _col('a_id')],
                       associations=[{'type': 'belongs_to', 'target': 'a', 'foreign_key': 'a_id'}]),
        }}
        out = erd.render_plantuml(schema)
        self.assertIn('a_id : integer <<FK>>', out)

    def test_pseudo_column_skipped(self):
        schema = {'tables': {'t': _table([_col('id', primary=True), _col('lower(email)')])}}
        out = erd.render_plantuml(schema)
        self.assertNotIn('lower(email)', out)

    def test_entities_rendered_in_name_order(self):
        schema = {'tables': {
            'zebra': _table([_col('id', primary=True)]),
            'apple': _table([_col('id', primary=True)]),
        }}
        out = erd.render_plantuml(schema)
        self.assertLess(out.index('entity apple'), out.index('entity zebra'))


# ---------------------------------------------------------------------------
# entity alias sanitization + comment suffix
# ---------------------------------------------------------------------------
class TestAliasAndComment(unittest.TestCase):
    def test_simple_identifier_needs_no_alias(self):
        schema = {'tables': {'users': _table([_col('id', primary=True)])}}
        out = erd.render_plantuml(schema)
        self.assertIn('entity users {', out)
        self.assertNotIn(' as users', out)

    def test_non_identifier_table_name_gets_sanitized_alias(self):
        schema = {'tables': {'my table': _table([_col('id', primary=True)])}}
        out = erd.render_plantuml(schema)
        self.assertIn('entity "my table" as my_table {', out)

    def test_alias_used_in_relationship_line_not_raw_name(self):
        schema = {'tables': {
            'my table': _table([_col('id', primary=True)],
                               associations=[{'type': 'has_one', 'target': 'other'}]),
            'other': _table([_col('id', primary=True)]),
        }}
        out = erd.render_plantuml(schema)
        rel_line = out.splitlines()[-2]
        self.assertIn('my_table', rel_line)
        self.assertNotIn('my table', rel_line)

    def test_comment_forces_aliased_declaration_even_for_simple_name(self):
        schema = {'tables': {'users': _table([_col('id', primary=True)], comment='App users')}}
        out = erd.render_plantuml(schema)
        self.assertIn('entity "users（App users）" as users {', out)

    def test_comment_uses_fullwidth_parens(self):
        schema = {'tables': {'t': _table([_col('id', primary=True)], comment='hello')}}
        out = erd.render_plantuml(schema)
        self.assertIn('（hello）', out)
        self.assertNotIn('(hello)', out)

    def test_comment_quote_escaped(self):
        schema = {'tables': {'t': _table([_col('id', primary=True)], comment='say "hi"')}}
        out = erd.render_plantuml(schema)
        self.assertIn("say 'hi'", out)
        self.assertNotIn('say "hi"', out)

    def test_no_comment_and_simple_name_bare_entity(self):
        schema = {'tables': {'t': _table([_col('id', primary=True)])}}
        out = erd.render_plantuml(schema)
        self.assertIn('entity t {', out)


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
        out = erd.render_plantuml(schema)
        self.assertIn('a }o--o{ b\n', out)

    def test_habtm_only_is_many_to_many(self):
        schema = {'tables': {
            'a': _table([_col('id', primary=True)],
                       associations=[{'type': 'has_and_belongs_to_many', 'target': 'b',
                                     'foreign_key': 'b_id'}]),
            'b': _table([_col('id', primary=True)]),
        }}
        out = erd.render_plantuml(schema)
        self.assertIn('}o--o{', out)

    def test_has_one_is_one_to_one(self):
        schema = {'tables': {
            'a': _table([_col('id', primary=True)],
                       associations=[{'type': 'has_one', 'target': 'b'}]),
            'b': _table([_col('id', primary=True)]),
        }}
        out = erd.render_plantuml(schema)
        self.assertIn('a ||--|| b\n', out)

    def test_has_many_declaring_side_is_one_target_is_many(self):
        schema = {'tables': {
            'owner': _table([_col('id', primary=True)],
                            associations=[{'type': 'has_many', 'target': 'child'}]),
            'child': _table([_col('id', primary=True)]),
        }}
        out = erd.render_plantuml(schema)
        self.assertIn('owner ||--o{ child\n', out)

    def test_belongs_to_declaring_side_is_many(self):
        schema = {'tables': {
            'users': _table([_col('id', primary=True)]),
            'posts': _table([_col('id', primary=True), _col('user_id')],
                            associations=[{'type': 'belongs_to', 'target': 'users',
                                          'foreign_key': 'user_id'}]),
        }}
        out = erd.render_plantuml(schema)
        self.assertIn('users ||--o{ posts\n', out)

    def test_has_many_and_reverse_belongs_to_agree_on_direction(self):
        schema = {'tables': {
            'users': _table([_col('id', primary=True)],
                            associations=[{'type': 'has_many', 'target': 'posts'}]),
            'posts': _table([_col('id', primary=True), _col('user_id')],
                            associations=[{'type': 'belongs_to', 'target': 'users',
                                          'foreign_key': 'user_id'}]),
        }}
        out = erd.render_plantuml(schema)
        self.assertIn('users ||--o{ posts\n', out)

    def test_polymorphic_association_skipped(self):
        schema = {'tables': {
            'comments': _table([_col('id', primary=True), _col('commentable_id')],
                               associations=[{'type': 'belongs_to', 'target': 'commentable',
                                             'foreign_key': 'commentable_id', 'polymorphic': True}]),
        }}
        out = erd.render_plantuml(schema)
        self.assertNotIn('commentable', out.split('@enduml')[0].split('}\n\n')[-1])

    def test_dangling_target_skipped_defensively(self):
        schema = {'tables': {
            'a': _table([_col('id', primary=True)],
                       associations=[{'type': 'belongs_to', 'target': 'ghost', 'foreign_key': 'g_id'}]),
        }}
        out = erd.render_plantuml(schema)
        self.assertNotIn('ghost', out)


# ---------------------------------------------------------------------------
# label quoting/escaping (PlantUML relation labels are unquoted)
# ---------------------------------------------------------------------------
class TestLabelEscaping(unittest.TestCase):
    def test_label_quote_replaced_with_single_quote(self):
        schema = {'tables': {
            'a': _table([_col('id', primary=True)],
                       associations=[{'type': 'has_one', 'target': 'b', 'name': 'the "thing"'}]),
            'b': _table([_col('id', primary=True)]),
        }}
        out = erd.render_plantuml(schema)
        self.assertIn("a ||--|| b : the 'thing'\n", out)

    def test_no_name_omits_colon_segment(self):
        schema = {'tables': {
            'a': _table([_col('id', primary=True)],
                       associations=[{'type': 'has_one', 'target': 'b'}]),
            'b': _table([_col('id', primary=True)]),
        }}
        out = erd.render_plantuml(schema)
        self.assertIn('a ||--|| b\n', out)
        self.assertNotIn('a ||--|| b :', out)


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
        self.assertEqual(erd.render_plantuml(schema), erd.render_plantuml(copy.deepcopy(schema)))

    def test_table_dict_reorder_does_not_change_output(self):
        schema = self._schema()
        reversed_schema = {**schema,
                           'tables': {k: schema['tables'][k]
                                     for k in reversed(list(schema['tables']))}}
        out1 = erd.render_plantuml(schema)
        out2 = erd.render_plantuml(reversed_schema)
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
        self.assertEqual(erd.render_plantuml(schema), erd.render_plantuml(reordered))

    def test_ends_with_single_trailing_newline(self):
        out = erd.render_plantuml(self._schema())
        self.assertTrue(out.endswith('\n'))
        self.assertFalse(out.endswith('\n\n'))

    def test_emit_plantuml_document_matches_render_plantuml_of_canonical_schema(self):
        tables = {
            'users': {'columns': [_col('id', primary=True)], 'indexes': [], 'associations': [],
                      'fk_columns': []},
        }
        expected = erd.render_plantuml(erd.canonical_schema(tables, None, None))
        actual = erd.emit_plantuml_document(tables, None, None)
        self.assertEqual(expected, actual)

    def test_notes_and_groups_ignored(self):
        tables = {
            'users': {'columns': [_col('id', primary=True)], 'indexes': [], 'associations': [],
                      'fk_columns': []},
        }
        notes = [{'id': 'n1', 'scope': 'global', 'text': 'hello'}]
        groups = [{'id': 'g1', 'tables': ['users'], 'title': 'Core'}]
        with_notes = erd.emit_plantuml_document(tables, notes, groups)
        without_notes = erd.emit_plantuml_document(tables, None, None)
        self.assertEqual(with_notes, without_notes)


class TestPurity(unittest.TestCase):
    def test_schema_unchanged_after_render(self):
        schema = {'tables': {
            'users': _table([_col('id', primary=True)], comment='c'),
            'posts': _table([_col('id', primary=True), _col('user_id')],
                            associations=[{'type': 'belongs_to', 'target': 'users',
                                          'foreign_key': 'user_id'}]),
        }}
        before = copy.deepcopy(schema)
        erd.render_plantuml(schema)
        self.assertEqual(schema, before)

    def test_tables_unchanged_after_emit_plantuml_document(self):
        tables = {
            'users': {'columns': [_col('id', primary=True)], 'indexes': [], 'associations': [],
                      'fk_columns': []},
        }
        before = copy.deepcopy(tables)
        erd.emit_plantuml_document(tables, None, None)
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


class _EmitPlantUMLDriver(unittest.TestCase):
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


class TestCLIFileOutput(_EmitPlantUMLDriver):
    def test_emit_plantuml_writes_alongside_html(self):
        self._run('--emit-plantuml', 'schema.puml')
        self.assertTrue((Path(self.tmp.name) / 'erd.html').exists())
        text = (Path(self.tmp.name) / 'schema.puml').read_text()
        self.assertIn('@startuml', text)
        self.assertIn('entity posts', text)
        self.assertIn('entity users', text)
        self.assertIn('users ||--o{ posts', text)

    def test_emit_plantuml_reports_generated_path_on_stderr(self):
        _, err = self._run('--emit-plantuml', 'schema.puml')
        self.assertIn('Generated: schema.puml', err)

    def test_emit_plantuml_coexists_with_other_emit_flags(self):
        self._run('--emit-plantuml', 'schema.puml', '--excel', 'defs.xlsx',
                  '--emit-json', 'snap.json', '--emit-config', 'cfg.json',
                  '--emit-digest', 'digest.md', '--emit-dbml', 'schema.dbml',
                  '--emit-mermaid', 'schema.mmd')
        for name in ('erd.html', 'defs.xlsx', 'snap.json', 'cfg.json', 'digest.md',
                    'schema.dbml', 'schema.mmd', 'schema.puml'):
            self.assertTrue((Path(self.tmp.name) / name).exists(), name)


class TestCLIOutputCollision(_EmitPlantUMLDriver):
    def test_emit_plantuml_colliding_with_html_output_errors(self):
        with self.assertRaises(SystemExit):
            self._run('-o', 'same.puml', '--emit-plantuml', 'same.puml')

    def test_emit_plantuml_colliding_with_emit_json_errors(self):
        with self.assertRaises(SystemExit):
            self._run('--emit-plantuml', 'same.out', '--emit-json', 'same.out')

    def test_distinct_paths_and_stdout_do_not_collide(self):
        out, _ = self._run('-o', 'erd.html', '--emit-plantuml', '-')
        self.assertIn('@startuml', out)


class TestCLIStdout(_EmitPlantUMLDriver):
    def test_emit_plantuml_dash_writes_to_stdout_and_still_generates_html(self):
        out, _ = self._run('--emit-plantuml', '-')
        self.assertIn('@startuml', out)
        self.assertTrue((Path(self.tmp.name) / 'erd.html').exists())


class TestCLIFiltering(_EmitPlantUMLDriver):
    def test_only_reflected_including_dangling_association_pruning(self):
        self._run('--emit-plantuml', 'schema.puml', '--only', 'posts')
        text = (Path(self.tmp.name) / 'schema.puml').read_text()
        self.assertIn('entity posts', text)
        self.assertNotIn('entity users', text)
        self.assertNotIn('--o{', text)

    def test_exclude_reflected(self):
        self._run('--emit-plantuml', 'schema.puml', '--exclude', 'posts')
        text = (Path(self.tmp.name) / 'schema.puml').read_text()
        self.assertIn('entity users', text)
        self.assertNotIn('entity posts', text)


class TestCLIDiffRejectsCombination(_EmitPlantUMLDriver):
    def test_diff_with_emit_plantuml_exits_2(self):
        snap_path = Path(self.tmp.name) / 'snap.json'
        self._run('--emit-json', str(snap_path))
        with self.assertRaises(SystemExit) as cm:
            self._run('--diff', str(snap_path), '--emit-plantuml', 'schema.puml')
        self.assertEqual(cm.exception.code, 2)


class TestByteEquality(_EmitPlantUMLDriver):
    def test_html_byte_identical_with_and_without_emit_plantuml(self):
        self._run('-o', 'a.html')
        self._run('-o', 'b.html', '--emit-plantuml', 'schema.puml')
        self.assertEqual((Path(self.tmp.name) / 'a.html').read_bytes(),
                         (Path(self.tmp.name) / 'b.html').read_bytes())


if __name__ == '__main__':
    unittest.main()
