"""--emit-dbml (backlog #5) — minimal DBML export of the schema.

Covers the new src/erdscope/dbml.py surface (render_dbml, emit_dbml_document)
as direct unit tests against hand-built canonical schemas (mirrors
tests/test_emit_digest.py's style — no need to route through
merge_ir/canonical_schema for tests that are purely about rendering), plus
CLI-level wiring tests (--emit-dbml alongside --excel/--emit-json/
--emit-config/--emit-digest/HTML, `-` for stdout, output-collision guard,
--only/--exclude reflected, --diff rejecting the combination) driven through
main() the same way tests/test_emit_digest.py's _EmitDigestDriver does.

Run from the repository root:
    python3 -m unittest tests.test_emit_dbml -v
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
# simple table: columns, single-column pk, not null, defaults, increment,
# unique
# ---------------------------------------------------------------------------
class TestSimpleTable(unittest.TestCase):
    def test_golden_output(self):
        schema = {'tables': {
            'users': _table([
                _col('id', primary=True, extra='auto_increment'),
                _col('email', type_='string', sql_type='varchar(255)', nullable=True),
                _col('age', type_='integer', default='0'),
                _col('bio', type_='string', default='n/a'),
            ], indexes=[{'columns': ['email'], 'unique': True}]),
        }}
        out = erd.render_dbml(schema)
        self.assertEqual(out, (
            'Table users {\n'
            '  id integer [pk, increment, not null]\n'
            '  email varchar(255) [unique]\n'
            "  age integer [not null, default: 0]\n"
            "  bio string [not null, default: 'n/a']\n"
            '\n'
            '  indexes {\n'
            '    (email) [unique]\n'
            '  }\n'
            '}\n'
        ))

    def test_type_falls_back_when_no_sql_type(self):
        schema = {'tables': {'t': _table([_col('id', type_='integer', primary=True)])}}
        out = erd.render_dbml(schema)
        self.assertIn('  id integer [pk, not null]', out)

    def test_sql_type_preferred_over_type_shorthand(self):
        schema = {'tables': {'t': _table([
            _col('id', type_='integer', sql_type='bigint unsigned', primary=True)])}}
        out = erd.render_dbml(schema)
        self.assertIn('  id bigint unsigned [pk, not null]', out)

    def test_numeric_default_is_bare(self):
        schema = {'tables': {'t': _table([_col('n', default='42')])}}
        out = erd.render_dbml(schema)
        self.assertIn('default: 42', out)
        self.assertNotIn("default: '42'", out)

    def test_negative_and_float_numeric_defaults_are_bare(self):
        schema = {'tables': {'t': _table([_col('a', default='-3'), _col('b', default='1.5')])}}
        out = erd.render_dbml(schema)
        self.assertIn('default: -3', out)
        self.assertIn('default: 1.5', out)

    def test_string_default_is_quoted_and_escaped(self):
        schema = {'tables': {'t': _table([_col('s', default="a'b")])}}
        out = erd.render_dbml(schema)
        self.assertIn("default: 'a\\'b'", out)

    def test_no_default_omits_attr(self):
        schema = {'tables': {'t': _table([_col('s')])}}
        out = erd.render_dbml(schema)
        self.assertNotIn('default', out)

    def test_nullable_column_has_no_not_null_attr(self):
        schema = {'tables': {'t': _table([_col('s', nullable=True)])}}
        out = erd.render_dbml(schema)
        line = [l for l in out.splitlines() if l.strip().startswith('s ')][0]
        self.assertNotIn('not null', line)

    def test_column_with_no_attrs_has_bare_line(self):
        schema = {'tables': {'t': _table([_col('s', nullable=True)])}}
        out = erd.render_dbml(schema)
        self.assertIn('  s integer\n', out)


# ---------------------------------------------------------------------------
# composite PK -> indexes block, no per-column pk
# ---------------------------------------------------------------------------
class TestCompositePK(unittest.TestCase):
    def test_composite_pk_in_indexes_block_not_inline(self):
        schema = {'tables': {'t': _table([
            _col('a', primary=True), _col('b', primary=True)])}}
        out = erd.render_dbml(schema)
        self.assertNotIn('[pk]', out.split('indexes')[0])
        self.assertIn('    (a, b) [pk]', out)

    def test_single_column_pk_stays_inline_never_in_indexes_block(self):
        schema = {'tables': {'t': _table([_col('a', primary=True)])}}
        out = erd.render_dbml(schema)
        self.assertIn('  a integer [pk, not null]', out)
        self.assertNotIn('indexes', out)


# ---------------------------------------------------------------------------
# indexes block: named/unnamed/unique, single-column unique dup inline+block
# ---------------------------------------------------------------------------
class TestIndexesBlock(unittest.TestCase):
    def test_named_unique_composite_index(self):
        schema = {'tables': {'t': _table(
            [_col('a'), _col('b')],
            indexes=[{'columns': ['a', 'b'], 'unique': True, 'name': 'ix_ab'}])}}
        out = erd.render_dbml(schema)
        self.assertIn("    (a, b) [unique, name: 'ix_ab']", out)

    def test_unnamed_non_unique_index(self):
        schema = {'tables': {'t': _table(
            [_col('a'), _col('b')],
            indexes=[{'columns': ['a', 'b'], 'unique': False}])}}
        out = erd.render_dbml(schema)
        self.assertIn('    (a, b)\n', out)

    def test_single_column_unique_index_appears_inline_and_in_block(self):
        schema = {'tables': {'t': _table(
            [_col('a')],
            indexes=[{'columns': ['a'], 'unique': True, 'name': 'ix_a'}])}}
        out = erd.render_dbml(schema)
        self.assertIn('  a integer [not null, unique]', out)
        self.assertIn("    (a) [unique, name: 'ix_a']", out)

    def test_composite_pk_line_comes_before_other_indexes(self):
        schema = {'tables': {'t': _table(
            [_col('a', primary=True), _col('b', primary=True), _col('c')],
            indexes=[{'columns': ['c'], 'unique': False}])}}
        out = erd.render_dbml(schema)
        idx_block = out.split('indexes {')[1]
        self.assertLess(idx_block.index('(a, b) [pk]'), idx_block.index('(c)'))

    def test_no_pk_no_indexes_omits_block_entirely(self):
        schema = {'tables': {'t': _table([_col('a')])}}
        out = erd.render_dbml(schema)
        self.assertNotIn('indexes', out)

    def test_index_name_escaped(self):
        schema = {'tables': {'t': _table(
            [_col('a')], indexes=[{'columns': ['a'], 'unique': False, 'name': "o'clock"}])}}
        out = erd.render_dbml(schema)
        self.assertIn("name: 'o\\'clock'", out)


# ---------------------------------------------------------------------------
# table Note (comment)
# ---------------------------------------------------------------------------
class TestTableNote(unittest.TestCase):
    def test_comment_rendered_as_last_line(self):
        schema = {'tables': {'t': _table([_col('a')], comment='hello')}}
        out = erd.render_dbml(schema)
        block = out.split('Table t {')[1]
        self.assertTrue(block.rstrip('\n').rstrip('}').rstrip().endswith("Note: 'hello'"))

    def test_no_comment_omits_note_line(self):
        schema = {'tables': {'t': _table([_col('a')])}}
        out = erd.render_dbml(schema)
        self.assertNotIn('Note:', out)

    def test_comment_with_quote_escaped(self):
        schema = {'tables': {'t': _table([_col('a')], comment="it's here")}}
        out = erd.render_dbml(schema)
        self.assertIn("Note: 'it\\'s here'", out)

    def test_multiline_comment_uses_triple_quote_unescaped(self):
        schema = {'tables': {'t': _table([_col('a')], comment="line1\nline2 with 'quote'")}}
        out = erd.render_dbml(schema)
        self.assertIn("Note: '''line1\nline2 with 'quote'''", out)


# ---------------------------------------------------------------------------
# Ref generation — the lossy contract
# ---------------------------------------------------------------------------
class TestRefGeneration(unittest.TestCase):
    def test_belongs_to_with_foreign_key_produces_ref(self):
        schema = {'tables': {
            'users': _table([_col('id', primary=True)]),
            'posts': _table([_col('id', primary=True), _col('user_id')],
                            associations=[{'type': 'belongs_to', 'target': 'users',
                                          'foreign_key': 'user_id'}]),
        }}
        out = erd.render_dbml(schema)
        self.assertIn('Ref: posts.user_id > users.id', out)

    def test_has_one_never_exported_as_ref(self):
        schema = {'tables': {
            'users': _table([_col('id', primary=True)]),
            'profiles': _table([_col('id', primary=True), _col('user_id')],
                               associations=[{'type': 'has_one', 'target': 'users',
                                             'foreign_key': 'user_id'}]),
        }}
        out = erd.render_dbml(schema)
        self.assertNotIn('Ref:', out)

    def test_has_many_and_habtm_never_exported_as_ref(self):
        schema = {'tables': {
            'users': _table([_col('id', primary=True)],
                            associations=[
                                {'type': 'has_many', 'target': 'posts'},
                                {'type': 'has_and_belongs_to_many', 'target': 'tags',
                                 'foreign_key': 'tag_id'},
                            ]),
            'posts': _table([_col('id', primary=True)]),
            'tags': _table([_col('id', primary=True)]),
        }}
        out = erd.render_dbml(schema)
        self.assertNotIn('Ref:', out)

    def test_polymorphic_belongs_to_skipped_silently(self):
        schema = {'tables': {
            'comments': _table([_col('id', primary=True), _col('commentable_id')],
                               associations=[{'type': 'belongs_to', 'target': 'commentable',
                                             'foreign_key': 'commentable_id',
                                             'polymorphic': True}]),
        }}
        err = io.StringIO()
        with redirect_stderr(err):
            out = erd.render_dbml(schema)  # must not raise
        self.assertNotIn('Ref:', out)
        self.assertEqual(err.getvalue(), '')

    def test_target_with_no_pk_skipped_with_warning(self):
        schema = {'tables': {
            'users': _table([_col('name')]),  # no primary key at all
            'posts': _table([_col('id', primary=True), _col('user_id')],
                            associations=[{'type': 'belongs_to', 'target': 'users',
                                          'foreign_key': 'user_id'}]),
        }}
        err = io.StringIO()
        with redirect_stderr(err):
            out = erd.render_dbml(schema)
        self.assertNotIn('Ref:', out)
        self.assertIn('Warning: --emit-dbml: skipping Ref posts.user_id -> users '
                      '(target has no single-column primary key)', err.getvalue())

    def test_target_with_composite_pk_skipped_with_warning(self):
        schema = {'tables': {
            'users': _table([_col('a', primary=True), _col('b', primary=True)]),
            'posts': _table([_col('id', primary=True), _col('user_id')],
                            associations=[{'type': 'belongs_to', 'target': 'users',
                                          'foreign_key': 'user_id'}]),
        }}
        err = io.StringIO()
        with redirect_stderr(err):
            out = erd.render_dbml(schema)
        self.assertNotIn('Ref:', out)
        self.assertIn('Warning: --emit-dbml: skipping Ref posts.user_id -> users '
                      '(target has no single-column primary key)', err.getvalue())

    def test_ref_direction_always_greater_than(self):
        schema = {'tables': {
            'users': _table([_col('id', primary=True)]),
            'posts': _table([_col('id', primary=True), _col('user_id')],
                            associations=[{'type': 'belongs_to', 'target': 'users',
                                          'foreign_key': 'user_id'}]),
        }}
        out = erd.render_dbml(schema)
        self.assertIn(' > ', out)
        self.assertNotIn(' - ', out.split('Ref:')[1] if 'Ref:' in out else '')

    def test_refs_sorted_deterministically(self):
        schema = {'tables': {
            'users': _table([_col('id', primary=True)]),
            'a_posts': _table([_col('id', primary=True), _col('user_id')],
                              associations=[{'type': 'belongs_to', 'target': 'users',
                                            'foreign_key': 'user_id'}]),
            'z_posts': _table([_col('id', primary=True), _col('user_id')],
                              associations=[{'type': 'belongs_to', 'target': 'users',
                                            'foreign_key': 'user_id'}]),
        }}
        out = erd.render_dbml(schema)
        refs_section = out[out.index('Ref:'):]
        lines = refs_section.strip().splitlines()
        self.assertEqual(lines, sorted(lines))
        self.assertTrue(lines[0].startswith('Ref: a_posts'))


# ---------------------------------------------------------------------------
# identifier quoting
# ---------------------------------------------------------------------------
class TestIdentifierQuoting(unittest.TestCase):
    def test_table_name_with_space_quoted(self):
        schema = {'tables': {'my table': _table([_col('a')])}}
        out = erd.render_dbml(schema)
        self.assertIn('Table "my table" {', out)

    def test_column_name_starting_with_digit_quoted(self):
        schema = {'tables': {'t': _table([_col('2fa')])}}
        out = erd.render_dbml(schema)
        self.assertIn('  "2fa" integer', out)

    def test_simple_identifier_stays_bare(self):
        schema = {'tables': {'users': _table([_col('id')])}}
        out = erd.render_dbml(schema)
        self.assertIn('Table users {', out)
        self.assertNotIn('"users"', out)

    def test_double_quote_in_name_escaped(self):
        schema = {'tables': {'t': _table([_col('weird"name')])}}
        out = erd.render_dbml(schema)
        self.assertIn('"weird\\"name"', out)


# ---------------------------------------------------------------------------
# determinism / purity
# ---------------------------------------------------------------------------
class TestDeterminism(unittest.TestCase):
    def _schema(self):
        return {'tables': {
            'users': _table([_col('id', primary=True), _col('email', type_='string')],
                            indexes=[{'columns': ['email'], 'unique': True}],
                            comment='App users'),
            'posts': _table([_col('id', primary=True), _col('user_id')],
                            associations=[{'type': 'belongs_to', 'target': 'users',
                                          'foreign_key': 'user_id'}]),
        }}

    def test_same_schema_same_output(self):
        schema = self._schema()
        self.assertEqual(erd.render_dbml(schema), erd.render_dbml(copy.deepcopy(schema)))

    def test_table_dict_reorder_does_not_change_output(self):
        schema = self._schema()
        reversed_schema = {**schema,
                           'tables': {k: schema['tables'][k]
                                     for k in reversed(list(schema['tables']))}}
        out1 = erd.render_dbml(schema)
        out2 = erd.render_dbml(reversed_schema)
        self.assertEqual(out1, out2)

    def test_ends_with_single_trailing_newline(self):
        out = erd.render_dbml(self._schema())
        self.assertTrue(out.endswith('\n'))
        self.assertFalse(out.endswith('\n\n'))

    def test_emit_dbml_document_matches_render_dbml_of_canonical_schema(self):
        tables = {
            'users': {'columns': [_col('id', primary=True)], 'indexes': [], 'associations': [],
                      'fk_columns': []},
        }
        expected = erd.render_dbml(erd.canonical_schema(tables, None, None))
        actual = erd.emit_dbml_document(tables, None, None)
        self.assertEqual(expected, actual)

    def test_notes_and_groups_ignored(self):
        tables = {
            'users': {'columns': [_col('id', primary=True)], 'indexes': [], 'associations': [],
                      'fk_columns': []},
        }
        notes = [{'id': 'n1', 'scope': 'global', 'text': 'hello'}]
        groups = [{'id': 'g1', 'tables': ['users'], 'title': 'Core'}]
        with_notes = erd.emit_dbml_document(tables, notes, groups)
        without_notes = erd.emit_dbml_document(tables, None, None)
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
        erd.render_dbml(schema)
        self.assertEqual(schema, before)

    def test_tables_unchanged_after_emit_dbml_document(self):
        tables = {
            'users': {'columns': [_col('id', primary=True)], 'indexes': [], 'associations': [],
                      'fk_columns': []},
        }
        before = copy.deepcopy(tables)
        erd.emit_dbml_document(tables, None, None)
        self.assertEqual(tables, before)


# ---------------------------------------------------------------------------
# CLI wiring — driven through main(), mirroring test_emit_digest.py's
# _EmitDigestDriver technique (stubbed parse_mysql, a temp cwd, sys.argv
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


class _EmitDbmlDriver(unittest.TestCase):
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


class TestCLIFileOutput(_EmitDbmlDriver):
    def test_emit_dbml_writes_alongside_html(self):
        self._run('--emit-dbml', 'schema.dbml')
        self.assertTrue((Path(self.tmp.name) / 'erd.html').exists())
        text = (Path(self.tmp.name) / 'schema.dbml').read_text()
        self.assertIn('Table posts {', text)
        self.assertIn('Table users {', text)
        self.assertIn('Ref: posts.user_id > users.id', text)

    def test_emit_dbml_reports_generated_path_on_stderr(self):
        _, err = self._run('--emit-dbml', 'schema.dbml')
        self.assertIn('Generated: schema.dbml', err)

    def test_emit_dbml_coexists_with_excel_emit_json_emit_config_emit_digest(self):
        self._run('--emit-dbml', 'schema.dbml', '--excel', 'defs.xlsx',
                  '--emit-json', 'snap.json', '--emit-config', 'cfg.json',
                  '--emit-digest', 'digest.md')
        self.assertTrue((Path(self.tmp.name) / 'erd.html').exists())
        self.assertTrue((Path(self.tmp.name) / 'defs.xlsx').exists())
        self.assertTrue((Path(self.tmp.name) / 'snap.json').exists())
        self.assertTrue((Path(self.tmp.name) / 'cfg.json').exists())
        self.assertTrue((Path(self.tmp.name) / 'digest.md').exists())
        self.assertTrue((Path(self.tmp.name) / 'schema.dbml').exists())


class TestCLIOutputCollision(_EmitDbmlDriver):
    def test_emit_dbml_colliding_with_html_output_errors(self):
        with self.assertRaises(SystemExit):
            self._run('-o', 'same.dbml', '--emit-dbml', 'same.dbml')

    def test_emit_dbml_colliding_with_emit_json_errors(self):
        with self.assertRaises(SystemExit):
            self._run('--emit-dbml', 'same.out', '--emit-json', 'same.out')

    def test_distinct_paths_and_stdout_do_not_collide(self):
        out, _ = self._run('-o', 'erd.html', '--emit-dbml', '-')
        self.assertIn('Table users', out)


class TestCLIStdout(_EmitDbmlDriver):
    def test_emit_dbml_dash_writes_to_stdout_and_still_generates_html(self):
        out, _ = self._run('--emit-dbml', '-')
        self.assertIn('Table users', out)
        self.assertTrue((Path(self.tmp.name) / 'erd.html').exists())


class TestCLIFiltering(_EmitDbmlDriver):
    def test_only_reflected_including_dangling_association_pruning(self):
        self._run('--emit-dbml', 'schema.dbml', '--only', 'posts')
        text = (Path(self.tmp.name) / 'schema.dbml').read_text()
        self.assertIn('Table posts {', text)
        self.assertNotIn('Table users', text)
        # 'users' filtered out -> posts' belongs_to :user must be pruned as
        # dangling (canonical_schema), so no Ref: line at all.
        self.assertNotIn('Ref:', text)

    def test_exclude_reflected(self):
        self._run('--emit-dbml', 'schema.dbml', '--exclude', 'posts')
        text = (Path(self.tmp.name) / 'schema.dbml').read_text()
        self.assertIn('Table users', text)
        self.assertNotIn('Table posts', text)


class TestCLIDiffRejectsCombination(_EmitDbmlDriver):
    def test_diff_with_emit_dbml_exits_2(self):
        snap_path = Path(self.tmp.name) / 'snap.json'
        self._run('--emit-json', str(snap_path))
        with self.assertRaises(SystemExit) as cm:
            self._run('--diff', str(snap_path), '--emit-dbml', 'schema.dbml')
        self.assertEqual(cm.exception.code, 2)


class TestByteEquality(_EmitDbmlDriver):
    def test_html_byte_identical_with_and_without_emit_dbml(self):
        self._run('-o', 'a.html')
        self._run('-o', 'b.html', '--emit-dbml', 'schema.dbml')
        self.assertEqual((Path(self.tmp.name) / 'a.html').read_bytes(),
                         (Path(self.tmp.name) / 'b.html').read_bytes())


if __name__ == '__main__':
    unittest.main()
