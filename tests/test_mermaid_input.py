"""--- MERMAID INPUT (backlog P5) — typed source `mermaid.er`.

Unit tests against mermaid_er_provider() directly (hand-written .mmd text
via a temp file), covering the grammar subset documented in mermaid.py's
INPUT module docstring: entity blocks (PK/UK/FK, comments), all relationship
cardinality combinations, label handling, stub-table creation for
relationship-only entities, and the "never silently drop" warning
philosophy. Plus a round-trip test (this erdscope's own --emit-mermaid
output, re-imported, must recover the same tables/columns/associations —
the "supported subset + general core" fixed point DESIGN_ROADMAP.md's P5
task breakdown calls for) and CLI-level wiring through a config
`sources[].type: mermaid.er` entry.

Run from the repository root:
    python3 -m unittest tests.test_mermaid_input -v
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
    """Write `text` to a temp .mmd file and run it through
    mermaid_er_provider, returning (tables, warnings)."""
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / 'schema.mmd'
    path.write_text(text)
    result = erd.mermaid_er_provider(path, given='schema.mmd')
    tmp.cleanup()
    return result['tables'], result['warnings']


class TestEntityBlocks(unittest.TestCase):
    def test_simple_entity_and_types_kept_verbatim(self):
        tables, warnings = _parse('''
            erDiagram
            users {
                integer id PK
                string email
            }
        ''')
        self.assertEqual(warnings, [])
        cols = {c['name']: c for c in tables['users']['columns']}
        self.assertEqual(cols['id']['type'], 'integer')
        self.assertEqual(cols['id']['sql_type'], 'integer')
        self.assertFalse(cols['id']['nullable'])  # pk forces not-null
        self.assertTrue(cols['email']['nullable'])
        self.assertEqual(tables['users']['primary_key'], 'id')

    def test_erDiagram_header_optional(self):
        tables_with, _ = _parse('erDiagram\nusers {\n  integer id PK\n}\n')
        tables_without, _ = _parse('users {\n  integer id PK\n}\n')
        self.assertEqual(tables_with.keys(), tables_without.keys())

    def test_uk_becomes_synthetic_unique_index(self):
        tables, _ = _parse('users {\n  string email UK\n}\n')
        self.assertEqual(tables['users']['indexes'],
                         [{'columns': ['email'], 'unique': True}])

    def test_fk_marker_is_inert(self):
        tables, warnings = _parse('posts {\n  integer user_id FK\n}\n')
        self.assertEqual(warnings, [])
        self.assertNotIn('indexes', {k: v for k, v in tables['posts'].items() if v})
        col = tables['posts']['columns'][0]
        self.assertNotIn('primary', col)

    def test_quoted_comment_becomes_column_comment(self):
        tables, _ = _parse('t {\n  string a "a comment"\n}\n')
        self.assertEqual(tables['t']['columns'][0]['comment'], 'a comment')

    def test_multiple_keys_comma_separated(self):
        tables, warnings = _parse('t {\n  integer a PK, FK\n}\n')
        self.assertEqual(warnings, [])
        self.assertEqual(tables['t']['primary_key'], 'a')

    def test_unknown_key_token_warns_but_keeps_column(self):
        tables, warnings = _parse('t {\n  integer a ZZ\n}\n')
        self.assertEqual(len(tables['t']['columns']), 1)
        self.assertTrue(any("unknown column key 'ZZ'" in w for w in warnings))

    def test_unknown_entity_line_warns_and_is_skipped(self):
        tables, warnings = _parse('t {\n  integer a\n  ???not a column\n}\n')
        self.assertEqual(len(tables['t']['columns']), 1)
        self.assertTrue(any('unknown entity statement' in w for w in warnings))

    def test_composite_pk_from_two_pk_columns(self):
        tables, _ = _parse('t {\n  integer a PK\n  integer b PK\n}\n')
        self.assertEqual(tables['t']['primary_key'], ['a', 'b'])
        cols = {c['name']: c for c in tables['t']['columns']}
        self.assertFalse(cols['a']['nullable'])
        self.assertFalse(cols['b']['nullable'])

    def test_duplicate_entity_keeps_first_and_warns(self):
        tables, warnings = _parse('''
            t {
              integer a
            }
            t {
              integer b
            }
        ''')
        self.assertEqual([c['name'] for c in tables['t']['columns']], ['a'])
        self.assertTrue(any('duplicate entity' in w for w in warnings))

    def test_unterminated_entity_is_discarded_with_warning(self):
        tables, warnings = _parse('t {\n  integer a\n')
        self.assertEqual(tables, {})
        self.assertTrue(any('unterminated entity' in w for w in warnings))


class TestRelationshipCardinality(unittest.TestCase):
    def test_one_to_many_belongs_to_on_many_side(self):
        tables, warnings = _parse('a ||--o{ b : "rel"\n')
        self.assertEqual(warnings, [])
        assoc = tables['b']['associations'][0]
        self.assertEqual(assoc['type'], 'belongs_to')
        self.assertEqual(assoc['target'], 'a')
        self.assertEqual(assoc['name'], 'rel')
        self.assertNotIn('foreign_key', assoc)
        self.assertEqual(tables['a']['associations'], [])

    def test_one_to_many_mirrored_direction(self):
        # many-side token on the LEFT this time
        tables, warnings = _parse('a }o--|| b : "rel"\n')
        self.assertEqual(warnings, [])
        assoc = tables['a']['associations'][0]
        self.assertEqual(assoc['type'], 'belongs_to')
        self.assertEqual(assoc['target'], 'b')

    def test_one_to_one_has_one_on_left(self):
        tables, warnings = _parse('a ||--|| b : "rel"\n')
        self.assertEqual(warnings, [])
        assoc = tables['a']['associations'][0]
        self.assertEqual(assoc['type'], 'has_one')
        self.assertEqual(assoc['target'], 'b')
        self.assertEqual(tables['b']['associations'], [])

    def test_many_to_many_habtm_on_left(self):
        tables, warnings = _parse('a }o--o{ b : "rel"\n')
        self.assertEqual(warnings, [])
        assoc = tables['a']['associations'][0]
        self.assertEqual(assoc['type'], 'has_and_belongs_to_many')
        self.assertEqual(assoc['target'], 'b')

    def test_one_or_more_and_zero_or_one_tokens(self):
        # left `|o` = zero-or-one ("one"-ish); right `|{` = one-or-more
        # ("many") -> b is the many side, so b belongs_to a
        tables, warnings = _parse('a |o--|{ b : "rel"\n')
        self.assertEqual(warnings, [])
        assoc = tables['b']['associations'][0]
        self.assertEqual(assoc['type'], 'belongs_to')
        self.assertEqual(assoc['target'], 'a')
        self.assertEqual(tables['a']['associations'], [])

    def test_dotted_line_same_as_solid(self):
        tables, warnings = _parse('a ||..o{ b : "rel"\n')
        self.assertEqual(warnings, [])
        self.assertEqual(tables['b']['associations'][0]['type'], 'belongs_to')

    def test_no_label_omits_name(self):
        tables, warnings = _parse('a ||--|| b : ""\n')
        self.assertEqual(warnings, [])
        self.assertEqual(tables['a']['associations'][0]['name'], '')

    def test_no_colon_segment_at_all(self):
        tables, warnings = _parse('a ||--|| b\n')
        self.assertEqual(warnings, [])
        self.assertEqual(tables['a']['associations'][0]['name'], '')

    def test_bare_unquoted_label_accepted_leniently(self):
        tables, warnings = _parse('a ||--o{ b : has\n')
        self.assertEqual(warnings, [])
        self.assertEqual(tables['b']['associations'][0]['name'], 'has')

    def test_self_reference(self):
        tables, warnings = _parse('a ||--o{ a : "parent"\n')
        self.assertEqual(warnings, [])
        assoc = tables['a']['associations'][0]
        self.assertEqual(assoc['type'], 'belongs_to')
        self.assertEqual(assoc['target'], 'a')

    def test_relationship_only_entities_get_columnless_stub_tables(self):
        tables, warnings = _parse('a ||--o{ b : "rel"\n')
        self.assertEqual(warnings, [])
        self.assertEqual(tables['a']['columns'], [])
        self.assertEqual(tables['b']['columns'], [])

    def test_entity_declared_after_relationship_line_keeps_its_columns(self):
        # the block comes AFTER the relationship line mentioning it — must
        # not be treated as a duplicate of an auto-created stub
        tables, warnings = _parse('''
            a ||--o{ b : "rel"
            b {
              integer id PK
              string name
            }
        ''')
        self.assertEqual(warnings, [])
        self.assertEqual([c['name'] for c in tables['b']['columns']], ['id', 'name'])


class TestUnknownConstructs(unittest.TestCase):
    def test_unknown_top_level_statement_warns(self):
        tables, warnings = _parse('this is not valid mermaid\na ||--|| b\n')
        self.assertIn('a', tables)
        self.assertTrue(any('unknown statement' in w for w in warnings))

    def test_empty_file_produces_no_tables_no_warnings(self):
        tables, warnings = _parse('erDiagram\n')
        self.assertEqual(tables, {})
        self.assertEqual(warnings, [])

    def test_percent_comment_stripped(self):
        tables, warnings = _parse('a ||--o{ b : "rel" %% trailing comment\n')
        self.assertEqual(warnings, [])
        self.assertEqual(tables['b']['associations'][0]['name'], 'rel')

    def test_warning_message_includes_file_and_line(self):
        _, warnings = _parse('bogus line\na ||--|| b\n')
        self.assertTrue(warnings[0].startswith('schema.mmd:1:'))

    def test_single_line_entity_block_is_unsupported_and_warns(self):
        tables, warnings = _parse('t { integer id PK }\n')
        self.assertEqual(tables, {})
        self.assertTrue(any('unknown statement' in w for w in warnings))


class TestSourceShape(unittest.TestCase):
    def test_provider_result_kind_and_provider(self):
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / 'schema.mmd'
        path.write_text('t {\n  integer id PK\n}\n')
        result = erd.mermaid_er_provider(path, given='schema.mmd')
        tmp.cleanup()
        self.assertEqual(result['source']['kind'], 'sketch')
        self.assertEqual(result['source']['provider'], 'mermaid.er')
        self.assertEqual(result['source']['location'], 'schema.mmd')


# ---------------------------------------------------------------------------
# Round trip: erdscope's OWN --emit-mermaid output, re-imported, must recover
# the same tables/columns/associations. This is the "supported subset +
# general core" fixed point DESIGN_ROADMAP.md's P5 breakdown asks for.
# ---------------------------------------------------------------------------
class TestRoundTrip(unittest.TestCase):
    def _schema(self):
        return {'tables': {
            'users': {'columns': [
                {'name': 'id', 'type': 'integer', 'nullable': False, 'primary': True},
                {'name': 'email', 'type': 'string', 'nullable': False},
            ], 'indexes': [], 'associations': []},
            'posts': {'columns': [
                {'name': 'id', 'type': 'integer', 'nullable': False, 'primary': True},
                {'name': 'user_id', 'type': 'integer', 'nullable': False},
            ], 'indexes': [], 'associations': [
                {'type': 'belongs_to', 'target': 'users', 'foreign_key': 'user_id',
                 'name': 'user'},
            ]},
            'profiles': {'columns': [
                {'name': 'id', 'type': 'integer', 'nullable': False, 'primary': True},
                {'name': 'user_id', 'type': 'integer', 'nullable': False},
            ], 'indexes': [], 'associations': [
                {'type': 'has_one', 'target': 'users', 'foreign_key': 'user_id', 'name': 'user'},
            ]},
        }}

    def test_tables_and_associations_survive_a_round_trip(self):
        mmd_text = erd.render_mermaid(self._schema())
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / 'schema.mmd'
        path.write_text(mmd_text)
        result = erd.mermaid_er_provider(path, given='schema.mmd')
        tmp.cleanup()
        self.assertEqual(result['warnings'], [])
        tables = result['tables']
        self.assertEqual(set(tables), {'users', 'posts', 'profiles'})
        self.assertEqual({c['name'] for c in tables['users']['columns']}, {'id', 'email'})
        self.assertTrue(any(a['type'] == 'belongs_to' and a['target'] == 'users'
                            for a in tables['posts']['associations']))
        self.assertTrue(any(a['type'] == 'has_one' and a['target'] == 'users'
                            for a in tables['profiles']['associations']))


# ---------------------------------------------------------------------------
# CLI wiring — a `mermaid.er` typed source declared in config `sources[]`,
# driven through main() (standalone, no DB needed).
# ---------------------------------------------------------------------------
class TestCLIWiring(unittest.TestCase):
    def test_mermaid_er_source_generates_html_standalone(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            mmd_path = tmp / 'schema.mmd'
            mmd_path.write_text('''
                users {
                  integer id PK
                  string email
                }
                posts {
                  integer id PK
                  integer user_id
                }
                users ||--o{ posts : "user"
            ''')
            cfg_path = tmp / 'c.json'
            cfg_path.write_text(
                '{"sources": [{"id": "s", "type": "mermaid.er", "path": "%s"}]}'
                % str(mmd_path).replace('\\', '\\\\'))
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

    def test_mermaid_er_is_a_known_type_name(self):
        self.assertIn('mermaid.er', erd.known_source_type_names())

    def test_directory_path_for_mermaid_er_is_a_clean_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            specs = erd.normalize_input_specs(
                [], [{'id': 's', 'type': 'mermaid.er', 'path': tmp}])
            with self.assertRaises(SystemExit) as cm, redirect_stderr(io.StringIO()):
                erd.run_input_specs(specs, {})
            self.assertIn('mermaid.er expects a .mmd/.mermaid file', str(cm.exception))


if __name__ == '__main__':
    unittest.main()
