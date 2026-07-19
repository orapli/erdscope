"""--emit-digest (backlog #3) — token-efficient Markdown digest of the
schema, with design notes, for LLM/agent consumption.

Covers the new src/erdscope/digest.py surface (render_digest,
emit_digest_document) as direct unit tests against hand-built canonical
schemas (mirrors tests/test_emit_json.py's style — no need to route through
merge_ir/canonical_schema for tests that are purely about rendering), plus
CLI-level wiring tests (--emit-digest alongside --excel/--emit-json/HTML, `-`
for stdout, output-collision guard, --only/--exclude reflected,
--digest-verbose, --diff rejecting the combination) driven through main() the
same way tests/test_emit_json.py's _EmitJsonDriver does.

Run from the repository root:
    python3 -m unittest tests.test_emit_digest -v
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


def _schema():
    """A hand-built canonical schema (already the emit.py output shape) —
    users (1) <- posts (N) belongs_to on user_id, plus one note of each
    scope. Mirrors test_emit_json.py's _merged_tables fixture but already
    post-canonical_schema, since render_digest consumes that shape directly."""
    return {
        'tables': {
            'users': {
                'comment': 'App users',
                'columns': [
                    _col('id', primary=True),
                    _col('email', type_='string', comment='Unique login'),
                ],
                'indexes': [],
                'associations': [
                    {'type': 'has_many', 'name': 'posts', 'target': 'posts',
                     'provenance': 'declared'},
                ],
            },
            'posts': {
                'columns': [
                    _col('id', primary=True),
                    _col('user_id', nullable=True, default='0', sql_type='bigint unsigned'),
                ],
                'indexes': [],
                'associations': [
                    {'type': 'belongs_to', 'name': 'user', 'target': 'users',
                     'foreign_key': 'user_id', 'provenance': 'db_fk'},
                ],
            },
        },
        'notes': [
            {'id': 'n1', 'scope': 'global', 'text': 'Simple blog schema.'},
            {'id': 'n2', 'scope': 'table', 'table': 'users', 'title': 'PII',
             'text': 'email is personal data'},
            {'id': 'n3', 'scope': 'relation', 'source_table': 'posts', 'target': 'users',
             'type': 'belongs_to', 'name': 'user', 'foreign_key': 'user_id',
             'through': None, 'polymorphic': False, 'text': 'cascade on delete'},
        ],
        'groups': [{'id': 'g1', 'tables': ['users', 'posts'], 'title': 'Core'}],
    }


# ---------------------------------------------------------------------------
# structure / headings
# ---------------------------------------------------------------------------
class TestStructure(unittest.TestCase):
    def test_title_in_heading(self):
        out = erd.render_digest(_schema(), title='blogdb')
        self.assertEqual(out.splitlines()[0], '# blogdb — schema digest')

    def test_no_title_falls_back_to_generic_heading(self):
        out = erd.render_digest(_schema())
        self.assertEqual(out.splitlines()[0], '# Schema digest')

    def test_table_count_in_heading(self):
        out = erd.render_digest(_schema())
        self.assertIn('## Tables (2)', out)

    def test_tables_sorted_alphabetically_regardless_of_dict_order(self):
        schema = _schema()
        reversed_schema = {**schema,
                           'tables': {k: schema['tables'][k]
                                     for k in reversed(list(schema['tables']))}}
        out1 = erd.render_digest(schema)
        out2 = erd.render_digest(reversed_schema)
        self.assertEqual(out1, out2)
        self.assertLess(out1.index('### posts'), out1.index('### users'))

    def test_table_comment_on_heading_line(self):
        out = erd.render_digest(_schema())
        self.assertIn('### users  — App users', out)

    def test_table_without_comment_has_bare_heading(self):
        out = erd.render_digest(_schema())
        self.assertIn('### posts\n', out)

    def test_ends_with_single_trailing_newline(self):
        out = erd.render_digest(_schema())
        self.assertTrue(out.endswith('\n'))
        self.assertFalse(out.endswith('\n\n'))


# ---------------------------------------------------------------------------
# columns
# ---------------------------------------------------------------------------
class TestColumns(unittest.TestCase):
    def test_pk_marker(self):
        out = erd.render_digest(_schema())
        self.assertIn('- id: integer, pk', out)

    def test_fk_arrow_to_target(self):
        out = erd.render_digest(_schema())
        self.assertIn('- user_id: integer, fk→users', out)

    def test_comment_quoted(self):
        out = erd.render_digest(_schema())
        self.assertIn('- email: string, "Unique login"', out)

    def test_nullable_default_sql_type_omitted_by_default(self):
        out = erd.render_digest(_schema())
        line = [l for l in out.splitlines() if l.startswith('- user_id:')][0]
        self.assertNotIn('null', line)
        self.assertNotIn('default=', line)
        self.assertNotIn('bigint unsigned', line)

    def test_verbose_adds_nullable_default_sql_type(self):
        out = erd.render_digest(_schema(), verbose=True)
        line = [l for l in out.splitlines() if l.startswith('- user_id:')][0]
        self.assertIn('null', line)
        self.assertIn('default=0', line)
        self.assertIn('bigint unsigned', line)

    def test_verbose_does_not_affect_non_nullable_no_default_column(self):
        out_plain = erd.render_digest(_schema())
        out_verbose = erd.render_digest(_schema(), verbose=True)
        id_line_plain = [l for l in out_plain.splitlines() if l.startswith('- id:')][0]
        id_line_verbose = [l for l in out_verbose.splitlines() if l.startswith('- id:')][0]
        self.assertEqual(id_line_plain, id_line_verbose)


# ---------------------------------------------------------------------------
# associations (Rel: line)
# ---------------------------------------------------------------------------
class TestAssociations(unittest.TestCase):
    def test_belongs_to_token_with_as_and_fk(self):
        out = erd.render_digest(_schema())
        self.assertIn('Rel: belongs_to users as user fk=user_id', out)

    def test_has_many_token_without_as_when_name_matches_target(self):
        out = erd.render_digest(_schema())
        self.assertIn('Rel: has_many posts', out)

    def test_through_and_polymorphic_markers(self):
        schema = _schema()
        schema['tables']['posts']['associations'] = [
            {'type': 'has_many', 'name': 'tags', 'target': 'tags', 'through': 'taggings'},
            {'type': 'belongs_to', 'name': 'commentable', 'target': 'commentable',
             'polymorphic': True},
        ]
        out = erd.render_digest(schema)
        self.assertIn('Rel: has_many tags through taggings, '
                      'belongs_to commentable (poly)', out)

    def test_no_associations_omits_rel_line(self):
        schema = _schema()
        schema['tables']['users']['associations'] = []
        out = erd.render_digest(schema)
        users_block = out.split('### users')[1].split('### ')[0]
        self.assertNotIn('Rel:', users_block)


# ---------------------------------------------------------------------------
# notes
# ---------------------------------------------------------------------------
class TestNotes(unittest.TestCase):
    def test_global_note_appears_before_tables_section(self):
        out = erd.render_digest(_schema())
        self.assertLess(out.index('Simple blog schema.'), out.index('## Tables'))

    def test_table_note_rendered_with_title(self):
        out = erd.render_digest(_schema())
        self.assertIn('_PII: email is personal data_', out)

    def test_table_note_appears_under_its_table(self):
        out = erd.render_digest(_schema())
        users_block = out.split('### users')[1].split('### ')[0]
        self.assertIn('PII: email is personal data', users_block)

    def test_relation_note_attached_to_matching_rel_token(self):
        out = erd.render_digest(_schema())
        self.assertIn('Rel: belongs_to users as user fk=user_id — "cascade on delete"', out)

    def test_multiple_relation_notes_joined(self):
        schema = _schema()
        schema['notes'].append(
            {'id': 'n4', 'scope': 'relation', 'source_table': 'posts', 'target': 'users',
             'type': 'belongs_to', 'name': 'user', 'foreign_key': 'user_id',
             'through': None, 'polymorphic': False, 'text': 'second note'})
        out = erd.render_digest(schema)
        self.assertIn('"cascade on delete"; "second note"', out)

    def test_no_notes_key_renders_without_error(self):
        schema = _schema()
        del schema['notes']
        out = erd.render_digest(schema)  # must not raise
        self.assertIn('## Tables (2)', out)

    def test_links_never_rendered(self):
        schema = _schema()
        schema['notes'][0]['links'] = [{'url': 'https://example.com', 'label': 'ADR'}]
        out = erd.render_digest(schema)
        self.assertNotIn('example.com', out)


# ---------------------------------------------------------------------------
# groups — intentionally dropped (viewer-cosmetic, no schema meaning)
# ---------------------------------------------------------------------------
class TestGroupsDropped(unittest.TestCase):
    def test_group_title_never_appears(self):
        out = erd.render_digest(_schema())
        self.assertNotIn('Core', out)


# ---------------------------------------------------------------------------
# determinism / purity
# ---------------------------------------------------------------------------
class TestDeterminism(unittest.TestCase):
    def test_same_schema_same_output(self):
        schema = _schema()
        self.assertEqual(erd.render_digest(schema), erd.render_digest(copy.deepcopy(schema)))

    def test_emit_digest_document_matches_render_digest_of_canonical_schema(self):
        tables = {
            'users': {'columns': [_col('id', primary=True)], 'indexes': [], 'associations': [],
                      'fk_columns': []},
        }
        expected = erd.render_digest(erd.canonical_schema(tables, None, None), title='t')
        actual = erd.emit_digest_document(tables, None, None, title='t')
        self.assertEqual(expected, actual)


class TestPurity(unittest.TestCase):
    def test_schema_unchanged_after_render(self):
        schema = _schema()
        before = copy.deepcopy(schema)
        erd.render_digest(schema)
        self.assertEqual(schema, before)


# ---------------------------------------------------------------------------
# CLI wiring — driven through main(), mirroring test_emit_json.py's
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


class _EmitDigestDriver(unittest.TestCase):
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


class TestCLIFileOutput(_EmitDigestDriver):
    def test_emit_digest_writes_alongside_html(self):
        self._run('--emit-digest', 'digest.md')
        self.assertTrue((Path(self.tmp.name) / 'erd.html').exists())
        text = (Path(self.tmp.name) / 'digest.md').read_text()
        self.assertTrue(text.startswith('# testdb — schema digest'))
        self.assertIn('### posts', text)
        self.assertIn('### users', text)

    def test_emit_digest_reports_generated_path_on_stderr(self):
        _, err = self._run('--emit-digest', 'digest.md')
        self.assertIn('Generated: digest.md', err)

    def test_emit_digest_coexists_with_excel_and_emit_json(self):
        self._run('--emit-digest', 'digest.md', '--excel', 'defs.xlsx', '--emit-json', 'snap.json')
        self.assertTrue((Path(self.tmp.name) / 'erd.html').exists())
        self.assertTrue((Path(self.tmp.name) / 'defs.xlsx').exists())
        self.assertTrue((Path(self.tmp.name) / 'snap.json').exists())
        self.assertTrue((Path(self.tmp.name) / 'digest.md').exists())


class TestCLIOutputCollision(_EmitDigestDriver):
    def test_emit_digest_colliding_with_html_output_errors(self):
        with self.assertRaises(SystemExit):
            self._run('-o', 'same.md', '--emit-digest', 'same.md')

    def test_emit_digest_colliding_with_emit_json_errors(self):
        with self.assertRaises(SystemExit):
            self._run('--emit-digest', 'same.out', '--emit-json', 'same.out')

    def test_distinct_paths_and_stdout_do_not_collide(self):
        out, _ = self._run('-o', 'erd.html', '--emit-digest', '-')
        self.assertTrue(out.startswith('# testdb'))


class TestCLIStdout(_EmitDigestDriver):
    def test_emit_digest_dash_writes_to_stdout_and_still_generates_html(self):
        out, _ = self._run('--emit-digest', '-')
        self.assertIn('### users', out)
        self.assertTrue((Path(self.tmp.name) / 'erd.html').exists())


class TestCLIFiltering(_EmitDigestDriver):
    def test_only_reflected_including_dangling_association_pruning(self):
        self._run('--emit-digest', 'digest.md', '--only', 'posts')
        text = (Path(self.tmp.name) / 'digest.md').read_text()
        self.assertIn('### posts', text)
        self.assertNotIn('### users', text)
        # 'users' filtered out -> posts' belongs_to :user must be pruned as
        # dangling, so no Rel: line (and no dangling fk→users column note).
        self.assertNotIn('Rel:', text)

    def test_exclude_reflected(self):
        self._run('--emit-digest', 'digest.md', '--exclude', 'posts')
        text = (Path(self.tmp.name) / 'digest.md').read_text()
        self.assertIn('### users', text)
        self.assertNotIn('### posts', text)


class TestCLIVerbose(_EmitDigestDriver):
    def test_digest_verbose_has_no_effect_without_emit_digest(self):
        # accepted but inert with no --emit-digest (mirrors --excel-template's
        # own "has no effect" pattern) -- just must not error.
        self._run('--digest-verbose')

    def test_digest_verbose_changes_output(self):
        plain, _ = self._run('--emit-digest', '-')
        verbose, _ = self._run('--emit-digest', '-', '--digest-verbose')
        self.assertNotEqual(plain, verbose)


class TestCLIDiffRejectsCombination(_EmitDigestDriver):
    def test_diff_with_emit_digest_exits_2(self):
        snap_path = Path(self.tmp.name) / 'snap.json'
        self._run('--emit-json', str(snap_path))
        with self.assertRaises(SystemExit) as cm:
            self._run('--diff', str(snap_path), '--emit-digest', 'digest.md')
        self.assertEqual(cm.exception.code, 2)


class TestByteEquality(_EmitDigestDriver):
    def test_html_byte_identical_with_and_without_emit_digest(self):
        self._run('-o', 'a.html')
        self._run('-o', 'b.html', '--emit-digest', 'digest.md')
        self.assertEqual((Path(self.tmp.name) / 'a.html').read_bytes(),
                         (Path(self.tmp.name) / 'b.html').read_bytes())


if __name__ == '__main__':
    unittest.main()
