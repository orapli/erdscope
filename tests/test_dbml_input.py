"""--- DBML INPUT (backlog P4) — typed source `dbml`.

Unit tests against dbml_provider() directly (hand-written .dbml text via a
temp file), covering the grammar subset documented in dbml.py's INPUT module
docstring: Table/column/indexes parsing, all four Ref symbols, Enum/Project
(silently consumed)/TableGroup/standalone-Note (warn+skip), composite-PK via
indexes{}, inline vs block Ref forms, default-literal handling, and the
"never silently drop" warning philosophy. Plus CLI-level wiring through a
config `sources[].type: dbml` entry, driven through main() the same way
tests/test_provider_contract.py's contract tests do (that file separately
covers the shared 1:N/1:1/M:N/self-ref domain contract).

Run from the repository root:
    python3 -m unittest tests.test_dbml_input -v
"""
import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location('erd', ROOT / 'erd.py')
erd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(erd)


def _parse(text):
    """Write `text` to a temp .dbml file and run it through dbml_provider,
    returning (tables, warnings) — warnings captured via the ProviderResult,
    never via stderr (dbml_provider itself never prints; only sources.py's
    run_input_specs does that, for the real CLI path)."""
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / 'schema.dbml'
    path.write_text(text)
    result = erd.dbml_provider(path, given='schema.dbml')
    tmp.cleanup()
    return result['tables'], result['warnings']


class TestBasicTableColumns(unittest.TestCase):
    def test_simple_table_and_types(self):
        tables, warnings = _parse('''
            Table users {
              id integer [pk]
              email varchar(255) [not null]
              bio text
            }
        ''')
        self.assertEqual(warnings, [])
        self.assertIn('users', tables)
        cols = {c['name']: c for c in tables['users']['columns']}
        self.assertEqual(cols['id']['sql_type'], 'integer')
        self.assertEqual(cols['id']['type'], 'integer')
        self.assertFalse(cols['id']['nullable'])  # pk forces not-null
        self.assertEqual(cols['email']['sql_type'], 'varchar(255)')
        self.assertEqual(cols['email']['type'], 'string')
        self.assertFalse(cols['email']['nullable'])
        self.assertTrue(cols['bio']['nullable'])  # no `not null` -> nullable
        self.assertEqual(tables['users']['primary_key'], 'id')

    def test_unknown_type_falls_back_to_verbatim_token(self):
        tables, _ = _parse('Table t {\n  status order_status\n}\n')
        col = tables['t']['columns'][0]
        self.assertEqual(col['type'], 'order_status')
        self.assertEqual(col['sql_type'], 'order_status')

    def test_quoted_table_and_column_names(self):
        tables, warnings = _parse('Table "my table" {\n  "weird name" integer\n}\n')
        self.assertEqual(warnings, [])
        self.assertIn('my table', tables)
        self.assertEqual(tables['my table']['columns'][0]['name'], 'weird name')

    def test_schema_qualified_table_name_collapses_to_last_segment(self):
        tables, _ = _parse('Table public.users {\n  id integer [pk]\n}\n')
        self.assertIn('users', tables)
        self.assertNotIn('public.users', tables)

    def test_table_alias_and_settings_consumed(self):
        tables, warnings = _parse(
            'Table users as u [headercolor: #3498DB] {\n  id integer [pk]\n}\n')
        self.assertEqual(warnings, [])
        self.assertIn('users', tables)

    def test_increment_maps_to_extra_auto_increment(self):
        tables, _ = _parse('Table t {\n  id integer [pk, increment]\n}\n')
        self.assertEqual(tables['t']['columns'][0]['extra'], 'auto_increment')

    def test_note_attr_becomes_column_comment(self):
        tables, _ = _parse("Table t {\n  a integer [note: 'a comment']\n}\n")
        self.assertEqual(tables['t']['columns'][0]['comment'], 'a comment')

    def test_unique_attr_becomes_synthetic_single_column_index(self):
        tables, _ = _parse('Table t {\n  a integer [unique]\n}\n')
        self.assertEqual(tables['t']['indexes'], [{'columns': ['a'], 'unique': True}])

    def test_unique_attr_not_duplicated_against_explicit_indexes_block(self):
        tables, _ = _parse(
            "Table t {\n  a integer [unique]\n  indexes {\n"
            "    (a) [unique, name: 'ix_a']\n  }\n}\n")
        self.assertEqual(len(tables['t']['indexes']), 1)
        self.assertEqual(tables['t']['indexes'][0]['name'], 'ix_a')

    def test_pseudo_column_style_unknown_line_warns_and_is_skipped(self):
        tables, warnings = _parse('Table t {\n  a integer\n  ???not a column\n}\n')
        self.assertEqual(len(tables['t']['columns']), 1)
        self.assertTrue(any('unknown table statement' in w for w in warnings))


class TestDefaults(unittest.TestCase):
    def test_string_default_unescaped(self):
        tables, _ = _parse("Table t {\n  s string [default: 'a\\'b']\n}\n")
        self.assertEqual(tables['t']['columns'][0]['default'], "a'b")

    def test_numeric_default_bare(self):
        tables, _ = _parse('Table t {\n  n integer [default: 42]\n}\n')
        self.assertEqual(tables['t']['columns'][0]['default'], '42')

    def test_backtick_expression_default_kept_verbatim(self):
        tables, _ = _parse('Table t {\n  ts timestamp [default: `now()`]\n}\n')
        self.assertEqual(tables['t']['columns'][0]['default'], 'now()')

    def test_null_default_omits_attribute(self):
        tables, _ = _parse('Table t {\n  a integer [default: null]\n}\n')
        self.assertNotIn('default', tables['t']['columns'][0])

    def test_unparseable_default_warns_and_is_skipped(self):
        tables, warnings = _parse('Table t {\n  a integer [default: some_call()]\n}\n')
        self.assertNotIn('default', tables['t']['columns'][0])
        self.assertTrue(any('unparseable default' in w for w in warnings))


class TestCompositePK(unittest.TestCase):
    def test_composite_pk_via_indexes_block(self):
        tables, warnings = _parse(
            'Table t {\n  a integer\n  b integer\n'
            '  indexes {\n    (a, b) [pk]\n  }\n}\n')
        self.assertEqual(warnings, [])
        self.assertEqual(tables['t']['primary_key'], ['a', 'b'])
        # composite PK is NOT also a regular index entry
        self.assertEqual(tables['t']['indexes'], [])
        cols = {c['name']: c for c in tables['t']['columns']}
        self.assertFalse(cols['a']['nullable'])
        self.assertFalse(cols['b']['nullable'])

    def test_single_column_pk_stays_a_string(self):
        tables, _ = _parse('Table t {\n  id integer [pk]\n}\n')
        self.assertEqual(tables['t']['primary_key'], 'id')

    def test_named_unique_index_alongside_composite_pk(self):
        tables, _ = _parse(
            "Table t {\n  a integer\n  b integer\n  c integer\n"
            "  indexes {\n    (a, b) [pk]\n    (c) [unique, name: 'ix_c']\n  }\n}\n")
        self.assertEqual(tables['t']['primary_key'], ['a', 'b'])
        self.assertEqual(tables['t']['indexes'],
                         [{'columns': ['c'], 'unique': True, 'name': 'ix_c'}])


class TestTableNote(unittest.TestCase):
    def test_single_line_note_is_table_comment(self):
        tables, _ = _parse("Table t {\n  a integer\n  Note: 'hello'\n}\n")
        self.assertEqual(tables['t']['comment'], 'hello')

    def test_multiline_triple_quoted_note(self):
        tables, _ = _parse(
            "Table t {\n  a integer\n  Note: '''line1\nline2'''\n}\n")
        self.assertEqual(tables['t']['comment'], 'line1\nline2')

    def test_single_line_triple_quoted_note(self):
        tables, _ = _parse("Table t {\n  a integer\n  Note: '''short'''\n}\n")
        self.assertEqual(tables['t']['comment'], 'short')


class TestRefSymbols(unittest.TestCase):
    def _schema(self, ref_line):
        return f'''
            Table a {{
              id integer [pk]
              b_id integer
            }}
            Table b {{
              id integer [pk]
            }}
            {ref_line}
        '''

    def test_many_to_one_gt_is_belongs_to_on_left(self):
        tables, warnings = _parse(self._schema('Ref: a.b_id > b.id'))
        self.assertEqual(warnings, [])
        assoc = tables['a']['associations'][0]
        self.assertEqual(assoc['type'], 'belongs_to')
        self.assertEqual(assoc['target'], 'b')
        self.assertEqual(assoc['foreign_key'], 'b_id')
        self.assertTrue(assoc['schema_fk'])
        self.assertEqual(tables['b']['associations'], [])

    def test_one_to_many_lt_is_belongs_to_on_right_mirror(self):
        tables, warnings = _parse(self._schema('Ref: b.id < a.b_id'))
        self.assertEqual(warnings, [])
        assoc = tables['a']['associations'][0]
        self.assertEqual(assoc['type'], 'belongs_to')
        self.assertEqual(assoc['target'], 'b')
        self.assertEqual(assoc['foreign_key'], 'b_id')

    def test_one_to_one_dash_is_has_one_on_left(self):
        tables, warnings = _parse(self._schema('Ref: a.b_id - b.id'))
        self.assertEqual(warnings, [])
        assoc = tables['a']['associations'][0]
        self.assertEqual(assoc['type'], 'has_one')
        self.assertEqual(assoc['target'], 'b')
        self.assertEqual(assoc['foreign_key'], 'b_id')

    def test_many_to_many_is_habtm_on_left(self):
        tables, warnings = _parse(self._schema('Ref: a.b_id <> b.id'))
        self.assertEqual(warnings, [])
        assoc = tables['a']['associations'][0]
        self.assertEqual(assoc['type'], 'has_and_belongs_to_many')
        self.assertEqual(assoc['target'], 'b')

    def test_named_ref_parses_the_same(self):
        tables, warnings = _parse(self._schema('Ref fk_a_b: a.b_id > b.id'))
        self.assertEqual(warnings, [])
        self.assertEqual(tables['a']['associations'][0]['type'], 'belongs_to')

    def test_association_name_strips_trailing_id(self):
        tables, _ = _parse(self._schema('Ref: a.b_id > b.id'))
        self.assertEqual(tables['a']['associations'][0]['name'], 'b')

    def test_inline_column_ref(self):
        tables, warnings = _parse('''
            Table a {
              id integer [pk]
              b_id integer [ref: > b.id]
            }
            Table b {
              id integer [pk]
            }
        ''')
        self.assertEqual(warnings, [])
        assoc = tables['a']['associations'][0]
        self.assertEqual(assoc['type'], 'belongs_to')
        self.assertEqual(assoc['target'], 'b')
        self.assertEqual(assoc['foreign_key'], 'b_id')

    def test_ref_block_form_multiple_refs(self):
        tables, warnings = _parse('''
            Table a {
              id integer [pk]
              b_id integer
              c_id integer
            }
            Table b {
              id integer [pk]
            }
            Table c {
              id integer [pk]
            }
            Ref {
              a.b_id > b.id
              a.c_id > c.id
            }
        ''')
        self.assertEqual(warnings, [])
        targets = {a['target'] for a in tables['a']['associations']}
        self.assertEqual(targets, {'b', 'c'})

    def test_composite_ref_is_unsupported_and_warns(self):
        tables, warnings = _parse('''
            Table a {
              x integer
              y integer
            }
            Table b {
              p integer
              q integer
            }
            Ref: a.(x, y) > b.(p, q)
        ''')
        self.assertEqual(tables['a']['associations'], [])
        self.assertTrue(any('composite Ref' in w for w in warnings))

    def test_ref_to_unknown_table_warns(self):
        tables, warnings = _parse('''
            Table a {
              id integer [pk]
              b_id integer
            }
            Ref: a.b_id > ghost.id
        ''')
        self.assertEqual(tables['a']['associations'], [])
        self.assertTrue(any("unknown table 'ghost'" in w for w in warnings))

    def test_ref_to_unknown_column_warns(self):
        tables, warnings = _parse('''
            Table a {
              id integer [pk]
              b_id integer
            }
            Table b {
              id integer [pk]
            }
            Ref: a.nope > b.id
        ''')
        self.assertEqual(tables['a']['associations'], [])
        self.assertTrue(any('does not exist' in w for w in warnings))


class TestOutOfScopeConstructs(unittest.TestCase):
    def test_enum_is_silently_consumed(self):
        tables, warnings = _parse('''
            Enum order_status {
              active
              cancelled
            }
            Table t {
              status order_status
            }
        ''')
        self.assertEqual(warnings, [])
        self.assertEqual(len(tables), 1)

    def test_project_is_silently_consumed(self):
        tables, warnings = _parse('''
            Project myapp {
              database_type: 'PostgreSQL'
              Note: 'hello'
            }
            Table t {
              id integer [pk]
            }
        ''')
        self.assertEqual(warnings, [])
        self.assertEqual(len(tables), 1)

    def test_tablegroup_warns_and_is_skipped(self):
        tables, warnings = _parse('''
            Table t {
              id integer [pk]
            }
            TableGroup g1 {
              t
            }
        ''')
        self.assertEqual(len(tables), 1)
        self.assertTrue(any("TableGroup 'g1'" in w and 'not' in w for w in warnings))

    def test_standalone_note_block_warns_and_is_skipped(self):
        tables, warnings = _parse('''
            Table t {
              id integer [pk]
            }
            Note project_notes {
              'some free text'
            }
        ''')
        self.assertEqual(len(tables), 1)
        self.assertTrue(any("Note block 'project_notes'" in w for w in warnings))

    def test_unknown_top_level_block_warns_and_is_skipped_wholesale(self):
        tables, warnings = _parse('''
            TablePartial base_cols {
              id integer [pk]
            }
            Table t {
              id integer [pk]
            }
        ''')
        self.assertEqual(len(tables), 1)
        self.assertTrue(any('unknown top-level construct' in w for w in warnings))

    def test_unknown_bare_statement_warns(self):
        tables, warnings = _parse(
            'this is not valid dbml at all\nTable t {\n  id integer [pk]\n}\n')
        self.assertEqual(len(tables), 1)
        self.assertTrue(any('unknown statement' in w for w in warnings))


class TestRobustness(unittest.TestCase):
    def test_unterminated_table_is_discarded_with_warning(self):
        tables, warnings = _parse('Table t {\n  id integer [pk]\n')
        self.assertEqual(tables, {})
        self.assertTrue(any('unterminated Table' in w for w in warnings))

    def test_duplicate_table_keeps_first_and_warns(self):
        tables, warnings = _parse('''
            Table t {
              a integer
            }
            Table t {
              b integer
            }
        ''')
        self.assertEqual([c['name'] for c in tables['t']['columns']], ['a'])
        self.assertTrue(any('duplicate Table' in w for w in warnings))

    def test_line_comment_stripped(self):
        tables, warnings = _parse('Table t {\n  a integer // trailing comment\n}\n')
        self.assertEqual(warnings, [])
        self.assertEqual(tables['t']['columns'][0]['name'], 'a')

    def test_empty_file_produces_no_tables_no_warnings(self):
        tables, warnings = _parse('// just a comment\n')
        self.assertEqual(tables, {})
        self.assertEqual(warnings, [])

    def test_warning_message_includes_file_and_line(self):
        _, warnings = _parse('bogus line\nTable t {\n  id integer [pk]\n}\n')
        self.assertTrue(warnings[0].startswith('schema.dbml:1:'))

    def test_single_line_table_block_is_unsupported_and_warns(self):
        # Documented scope limit (see dbml.py's INPUT module docstring): one
        # statement per physical line, same assumption rails_schema.py makes
        # for schema.rb — a Table that opens AND closes on the same line
        # isn't recognized as a Table header at all, and warns like any
        # other unrecognized statement rather than silently losing data.
        tables, warnings = _parse('Table t { id integer [pk] }\n')
        self.assertEqual(tables, {})
        self.assertTrue(any('unknown statement' in w for w in warnings))


class TestSourceShape(unittest.TestCase):
    def test_provider_result_kind_and_provider(self):
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / 'schema.dbml'
        path.write_text('Table t {\n  id integer [pk]\n}\n')
        result = erd.dbml_provider(path, given='schema.dbml')
        tmp.cleanup()
        self.assertEqual(result['source']['kind'], 'schema')
        self.assertEqual(result['source']['provider'], 'dbml')
        self.assertEqual(result['source']['location'], 'schema.dbml')


# ---------------------------------------------------------------------------
# CLI wiring — a `dbml` typed source declared in config `sources[]`, driven
# through main() (no DB needed: dbml is a fully standalone source, like
# rails.schema).
# ---------------------------------------------------------------------------
class TestCLIWiring(unittest.TestCase):
    def test_dbml_source_generates_html_standalone(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            dbml_path = tmp / 'schema.dbml'
            dbml_path.write_text('''
                Table users {
                  id integer [pk]
                  email varchar(255)
                }
                Table posts {
                  id integer [pk]
                  user_id integer
                }
                Ref: posts.user_id > users.id
            ''')
            cfg_path = tmp / 'c.json'
            cfg_path.write_text(
                '{"sources": [{"id": "s", "type": "dbml", "path": "%s"}]}'
                % str(dbml_path).replace('\\', '\\\\'))
            out = tmp / 'out.html'
            import sys
            argv = sys.argv
            try:
                sys.argv = ['erd.py', '--config', str(cfg_path), '-o', str(out)]
                with redirect_stderr(io.StringIO()):
                    erd.main()
            finally:
                sys.argv = argv
            html = out.read_text()
            self.assertIn('"users"', html)
            self.assertIn('"posts"', html)

    def test_unknown_type_name_is_a_clean_error(self):
        specs = erd.normalize_input_specs(
            [], [{'id': 's', 'type': 'dbml.wrong', 'path': '/nonexistent'}])
        with self.assertRaises(SystemExit) as cm, redirect_stderr(io.StringIO()):
            erd.run_input_specs(specs, {})
        self.assertIn("unknown type 'dbml.wrong'", str(cm.exception))

    def test_dbml_is_a_known_type_name(self):
        self.assertIn('dbml', erd.known_source_type_names())

    def test_directory_path_for_dbml_is_a_clean_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            specs = erd.normalize_input_specs(
                [], [{'id': 's', 'type': 'dbml', 'path': tmp}])
            with self.assertRaises(SystemExit) as cm, redirect_stderr(io.StringIO()):
                erd.run_input_specs(specs, {})
            self.assertIn('dbml expects a .dbml file', str(cm.exception))


if __name__ == '__main__':
    unittest.main()
