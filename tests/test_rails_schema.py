"""Tests for the rails.schema provider (D7/D8): the static db/schema.rb
parser, its wiring into merge_ir's provenance/reconciliation (D1/D2), and an
end-to-end run through the CLI via a typed config `sources` entry.

Run from the repository root:
    python3 -m unittest tests.test_rails_schema -v
"""
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_RAILS = Path(__file__).resolve().parent / 'fixture_app'
FIXTURE_SCHEMA = FIXTURE_RAILS / 'db' / 'schema.rb'

spec = importlib.util.spec_from_file_location('erd', ROOT / 'erd.py')
erd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(erd)


# ---------------------------------------------------------------------------
# Parser units (D8.1)
# ---------------------------------------------------------------------------
class _ParserTestCase(unittest.TestCase):
    def _parse(self, text):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'schema.rb'
            p.write_text(text)
            return erd.rails_schema_provider(p)

    def _warnings_text(self, result):
        return '\n'.join(result['warnings'])


class TestColumnTypes(_ParserTestCase):
    def test_every_supported_type_maps_through_sql_types(self):
        result = self._parse('''
          create_table "widgets", force: :cascade do |t|
            t.string "a"
            t.text "b"
            t.integer "c"
            t.bigint "d"
            t.float "e"
            t.decimal "f"
            t.numeric "g"
            t.datetime "h"
            t.timestamp "i"
            t.time "j"
            t.date "k"
            t.boolean "l"
            t.binary "m"
            t.blob "n"
            t.json "o"
            t.jsonb "p"
            t.uuid "q"
            t.inet "r"
          end
        ''')
        self.assertEqual(result['warnings'], [])
        cols = {c['name']: c for c in result['tables']['widgets']['columns']}
        expect_type = {
            'a': 'string', 'b': 'text', 'c': 'integer', 'd': 'bigint', 'e': 'float',
            'f': 'decimal', 'g': 'decimal', 'h': 'datetime', 'i': 'datetime',
            'j': 'time', 'k': 'date', 'l': 'boolean', 'm': 'binary', 'n': 'binary',
            'o': 'json', 'p': 'jsonb', 'q': 'uuid', 'r': 'inet',
        }
        expect_sql_type = dict(a='string', b='text', c='integer', d='bigint', e='float',
                               f='decimal', g='numeric', h='datetime', i='timestamp',
                               j='time', k='date', l='boolean', m='binary', n='blob',
                               o='json', p='jsonb', q='uuid', r='inet')
        for name, t in expect_type.items():
            self.assertEqual(cols[name]['type'], t, name)
            self.assertEqual(cols[name]['sql_type'], expect_sql_type[name], name)

    def test_null_false_and_default_and_comment(self):
        result = self._parse('''
          create_table "widgets", force: :cascade do |t|
            t.string "name", null: false, default: "widget", comment: "the name"
            t.integer "qty", default: 0
            t.boolean "active", default: true
            t.boolean "inactive", default: false
          end
        ''')
        self.assertEqual(result['warnings'], [])
        cols = {c['name']: c for c in result['tables']['widgets']['columns']}
        self.assertFalse(cols['name']['nullable'])
        self.assertEqual(cols['name']['default'], 'widget')
        self.assertEqual(cols['name']['comment'], 'the name')
        self.assertEqual(cols['qty']['default'], '0')
        self.assertEqual(cols['active']['default'], 'true')
        self.assertEqual(cols['inactive']['default'], 'false')

    def test_nullable_defaults_true_without_null_opt(self):
        result = self._parse('''
          create_table "widgets", force: :cascade do |t|
            t.string "name"
          end
        ''')
        self.assertTrue(result['tables']['widgets']['columns'][-1]['nullable'])

    def test_limit_modifier_on_sql_type(self):
        result = self._parse('''
          create_table "widgets", force: :cascade do |t|
            t.string "name", limit: 50
          end
        ''')
        col = next(c for c in result['tables']['widgets']['columns'] if c['name'] == 'name')
        self.assertEqual(col['sql_type'], 'string(50)')

    def test_precision_and_scale_modifiers(self):
        result = self._parse('''
          create_table "widgets", force: :cascade do |t|
            t.decimal "price", precision: 10, scale: 2
            t.decimal "weight", precision: 10
          end
        ''')
        cols = {c['name']: c for c in result['tables']['widgets']['columns']}
        self.assertEqual(cols['price']['sql_type'], 'decimal(10,2)')
        self.assertEqual(cols['weight']['sql_type'], 'decimal(10)')

    def test_unknown_column_type_warns_and_is_skipped(self):
        result = self._parse('''
          create_table "widgets", force: :cascade do |t|
            t.string "name"
            t.geometry "location"
          end
        ''')
        self.assertEqual([c['name'] for c in result['tables']['widgets']['columns']],
                         ['id', 'name'])
        self.assertTrue(any("unsupported column type 't.geometry'" in w
                            for w in result['warnings']))

    def test_unknown_column_option_warns_once_per_option_name(self):
        result = self._parse('''
          create_table "widgets", force: :cascade do |t|
            t.string "a", array: true
            t.string "b", array: true
            t.string "c", unsigned: true
          end
        ''')
        array_warnings = [w for w in result['warnings'] if "'array'" in w]
        unsigned_warnings = [w for w in result['warnings'] if "'unsigned'" in w]
        self.assertEqual(len(array_warnings), 1)
        self.assertEqual(len(unsigned_warnings), 1)
        # parsing continues — every column still lands, just without the option
        self.assertEqual([c['name'] for c in result['tables']['widgets']['columns']],
                         ['id', 'a', 'b', 'c'])

    def test_dynamic_default_warns_and_skips_attribute_only(self):
        result = self._parse('''
          create_table "widgets", force: :cascade do |t|
            t.string "token", default: -> { SecureRandom.uuid }
          end
        ''')
        col = next(c for c in result['tables']['widgets']['columns'] if c['name'] == 'token')
        self.assertNotIn('default', col)
        self.assertTrue(any('dynamic default' in w for w in result['warnings']))
        # warning carries a "<path>:<line>:" prefix (D7's file:line format)
        self.assertTrue(any(re.search(r':\d+: dynamic default', w) for w in result['warnings']))


class TestPrimaryKeys(_ParserTestCase):
    def test_default_synthesized_bigint_id(self):
        result = self._parse('create_table "widgets", force: :cascade do |t|\nend\n')
        cols = result['tables']['widgets']['columns']
        self.assertEqual(cols, [{'name': 'id', 'type': 'bigint', 'sql_type': 'bigint',
                                 'nullable': False, 'primary': True}])
        self.assertEqual(result['tables']['widgets']['primary_key'], 'id')

    def test_id_false_synthesizes_nothing(self):
        result = self._parse('''
          create_table "joins", id: false, force: :cascade do |t|
            t.integer "a_id"
            t.integer "b_id"
          end
        ''')
        names = [c['name'] for c in result['tables']['joins']['columns']]
        self.assertEqual(names, ['a_id', 'b_id'])
        self.assertNotIn('primary', result['tables']['joins']['columns'][0])

    def test_custom_primary_key_name(self):
        result = self._parse(
            'create_table "widgets", primary_key: "uid", force: :cascade do |t|\nend\n')
        cols = result['tables']['widgets']['columns']
        self.assertEqual(cols[0]['name'], 'uid')
        self.assertEqual(result['tables']['widgets']['primary_key'], 'uid')

    def test_uuid_id_type(self):
        result = self._parse(
            'create_table "widgets", id: :uuid, force: :cascade do |t|\nend\n')
        cols = result['tables']['widgets']['columns']
        self.assertEqual(cols[0]['type'], 'uuid')
        self.assertEqual(cols[0]['sql_type'], 'uuid')

    def test_composite_primary_key_synthesizes_nothing(self):
        result = self._parse('''
          create_table "grants", primary_key: ["tenant_id", "item_id"], force: :cascade do |t|
            t.integer "tenant_id"
            t.integer "item_id"
          end
        ''')
        t = result['tables']['grants']
        self.assertEqual(t['primary_key'], ['tenant_id', 'item_id'])
        self.assertEqual([c['name'] for c in t['columns']], ['tenant_id', 'item_id'])


class TestReferences(_ParserTestCase):
    def test_plain_reference_creates_id_column_and_default_index(self):
        result = self._parse('''
          create_table "users", force: :cascade do |t|
          end
          create_table "posts", force: :cascade do |t|
            t.references "user", null: false
          end
        ''')
        posts = result['tables']['posts']
        self.assertIn('user_id', [c['name'] for c in posts['columns']])
        self.assertEqual(posts['indexes'],
                         [{'name': 'index_posts_on_user_id', 'columns': ['user_id'],
                          'unique': False}])
        self.assertNotIn('associations', posts)  # no foreign_key: -> no FK

    def test_reference_index_false_suppresses_index(self):
        result = self._parse('''
          create_table "users", force: :cascade do |t|
          end
          create_table "posts", force: :cascade do |t|
            t.references "user", index: false
          end
        ''')
        self.assertEqual(result['tables']['posts']['indexes'], [])

    def test_reference_index_unique_hash(self):
        result = self._parse('''
          create_table "users", force: :cascade do |t|
          end
          create_table "posts", force: :cascade do |t|
            t.references "user", index: { unique: true }
          end
        ''')
        self.assertTrue(result['tables']['posts']['indexes'][0]['unique'])

    def test_foreign_key_true_defaults_to_pluralized_target(self):
        result = self._parse('''
          create_table "users", force: :cascade do |t|
          end
          create_table "posts", force: :cascade do |t|
            t.references "user", null: false, foreign_key: true
          end
        ''')
        assoc = result['tables']['posts']['associations'][0]
        self.assertEqual(assoc['target'], 'users')
        self.assertEqual(assoc['foreign_key'], 'user_id')
        self.assertEqual(assoc['type'], 'belongs_to')
        self.assertTrue(assoc['schema_fk'])
        self.assertNotIn('db_fk', assoc)

    def test_foreign_key_to_table_overrides_target(self):
        result = self._parse('''
          create_table "users", force: :cascade do |t|
          end
          create_table "posts", force: :cascade do |t|
            t.references "author", null: false, foreign_key: { to_table: "users" }
          end
        ''')
        assoc = result['tables']['posts']['associations'][0]
        self.assertEqual(assoc['target'], 'users')
        self.assertEqual(assoc['name'], 'author')
        self.assertEqual(assoc['foreign_key'], 'author_id')

    def test_polymorphic_reference_adds_type_column_no_fk_composite_index(self):
        result = self._parse('''
          create_table "comments", force: :cascade do |t|
            t.references "commentable", polymorphic: true, null: false
          end
        ''')
        t = result['tables']['comments']
        names = [c['name'] for c in t['columns']]
        self.assertIn('commentable_id', names)
        self.assertIn('commentable_type', names)
        self.assertNotIn('associations', t)
        self.assertEqual(t['indexes'][0]['columns'], ['commentable_type', 'commentable_id'])

    def test_polymorphic_with_foreign_key_true_warns_contradiction_and_skips_fk(self):
        result = self._parse('''
          create_table "comments", force: :cascade do |t|
            t.references "commentable", polymorphic: true, foreign_key: true
          end
        ''')
        self.assertNotIn('associations', result['tables']['comments'])
        self.assertTrue(any('polymorphic' in w and 'foreign_key' in w
                            for w in result['warnings']))

    def test_belongs_to_is_an_alias_for_references(self):
        result = self._parse('''
          create_table "users", force: :cascade do |t|
          end
          create_table "posts", force: :cascade do |t|
            t.belongs_to "user", null: false, foreign_key: true
          end
        ''')
        self.assertIn('user_id', [c['name'] for c in result['tables']['posts']['columns']])
        self.assertEqual(result['tables']['posts']['associations'][0]['target'], 'users')


class TestTimestamps(_ParserTestCase):
    def test_default_null_false(self):
        result = self._parse('create_table "widgets", force: :cascade do |t|\n'
                             '  t.timestamps\nend\n')
        cols = {c['name']: c for c in result['tables']['widgets']['columns']}
        self.assertEqual(cols['created_at']['type'], 'datetime')
        self.assertFalse(cols['created_at']['nullable'])
        self.assertFalse(cols['updated_at']['nullable'])

    def test_null_true_override(self):
        result = self._parse('create_table "widgets", force: :cascade do |t|\n'
                             '  t.timestamps null: true\nend\n')
        cols = {c['name']: c for c in result['tables']['widgets']['columns']}
        self.assertTrue(cols['created_at']['nullable'])
        self.assertTrue(cols['updated_at']['nullable'])


class TestIndexes(_ParserTestCase):
    def test_t_index_array_default_name(self):
        result = self._parse('''
          create_table "widgets", force: :cascade do |t|
            t.string "a"
            t.string "b"
            t.index ["a", "b"]
          end
        ''')
        ix = result['tables']['widgets']['indexes'][0]
        self.assertEqual(ix['name'], 'index_widgets_on_a_b')
        self.assertEqual(ix['columns'], ['a', 'b'])
        self.assertFalse(ix['unique'])

    def test_t_index_single_string_and_explicit_name_unique(self):
        result = self._parse('''
          create_table "widgets", force: :cascade do |t|
            t.string "a"
            t.index "a", name: "my_index", unique: true
          end
        ''')
        ix = result['tables']['widgets']['indexes'][0]
        self.assertEqual(ix['name'], 'my_index')
        self.assertEqual(ix['columns'], ['a'])
        self.assertTrue(ix['unique'])

    def test_add_index_on_known_table(self):
        result = self._parse('''
          create_table "widgets", force: :cascade do |t|
            t.string "a"
          end
          add_index "widgets", ["a"], name: "idx_a"
        ''')
        self.assertEqual(result['tables']['widgets']['indexes'],
                         [{'name': 'idx_a', 'columns': ['a'], 'unique': False}])

    def test_add_index_on_unknown_table_warns(self):
        result = self._parse('add_index "nope", ["a"]\n')
        self.assertTrue(any('unknown table' in w for w in result['warnings']))


class TestAddForeignKey(_ParserTestCase):
    def test_default_column_name_singularizes_target(self):
        result = self._parse('''
          create_table "users", force: :cascade do |t|
          end
          create_table "posts", force: :cascade do |t|
            t.bigint "user_id"
          end
          add_foreign_key "posts", "users"
        ''')
        assoc = result['tables']['posts']['associations'][0]
        self.assertEqual(assoc['foreign_key'], 'user_id')
        self.assertEqual(assoc['target'], 'users')
        self.assertTrue(assoc['schema_fk'])

    def test_explicit_column_option(self):
        result = self._parse('''
          create_table "users", force: :cascade do |t|
          end
          create_table "posts", force: :cascade do |t|
            t.bigint "editor_id"
          end
          add_foreign_key "posts", "users", column: "editor_id"
        ''')
        assoc = result['tables']['posts']['associations'][0]
        self.assertEqual(assoc['foreign_key'], 'editor_id')
        self.assertEqual(assoc['name'], 'editor')

    def test_unknown_table_warns_and_skips(self):
        result = self._parse('add_foreign_key "nope", "users"\n')
        self.assertTrue(any('unknown table' in w for w in result['warnings']))
        self.assertEqual(result['tables'], {})

    def test_missing_column_warns_and_skips(self):
        result = self._parse('''
          create_table "users", force: :cascade do |t|
          end
          create_table "posts", force: :cascade do |t|
          end
          add_foreign_key "posts", "users", column: "author_id"
        ''')
        self.assertTrue(any('does not exist' in w for w in result['warnings']))
        self.assertNotIn('associations', result['tables']['posts'])

    def test_dedup_reference_fk_and_redundant_add_foreign_key(self):
        result = self._parse('''
          create_table "users", force: :cascade do |t|
          end
          create_table "posts", force: :cascade do |t|
            t.references "user", null: false, foreign_key: true
          end
          add_foreign_key "posts", "users"
        ''')
        self.assertEqual(len(result['tables']['posts']['associations']), 1)


class TestHasOneViaUniqueFk(_ParserTestCase):
    def test_unique_single_column_index_makes_has_one(self):
        result = self._parse('''
          create_table "users", force: :cascade do |t|
          end
          create_table "profiles", force: :cascade do |t|
            t.bigint "user_id"
            t.index ["user_id"], unique: true
          end
          add_foreign_key "profiles", "users"
        ''')
        assoc = result['tables']['profiles']['associations'][0]
        self.assertEqual(assoc['type'], 'has_one')

    def test_non_unique_fk_column_is_belongs_to(self):
        result = self._parse('''
          create_table "users", force: :cascade do |t|
          end
          create_table "posts", force: :cascade do |t|
            t.bigint "user_id"
          end
          add_foreign_key "posts", "users"
        ''')
        self.assertEqual(result['tables']['posts']['associations'][0]['type'], 'belongs_to')


class TestTableLevel(_ParserTestCase):
    def test_table_comment(self):
        result = self._parse(
            'create_table "widgets", comment: "the widgets", force: :cascade do |t|\nend\n')
        self.assertEqual(result['tables']['widgets']['comment'], 'the widgets')

    def test_unknown_create_table_option_warns(self):
        result = self._parse(
            'create_table "widgets", bogus: true, force: :cascade do |t|\nend\n')
        self.assertTrue(any("unknown option 'bogus'" in w for w in result['warnings']))

    def test_ignored_create_table_options_are_silent(self):
        result = self._parse(
            'create_table "widgets", force: :cascade, charset: "utf8mb4", '
            'collation: "utf8mb4_bin", options: "ENGINE=InnoDB", if_not_exists: true '
            'do |t|\nend\n')
        self.assertEqual(result['warnings'], [])

    def test_header_and_terminal_end_are_ignored(self):
        result = self._parse('''
          ActiveRecord::Schema[7.1].define(version: 2024_01_01_000000) do
            create_table "widgets", force: :cascade do |t|
            end
          end
        ''')
        self.assertEqual(result['warnings'], [])
        self.assertIn('widgets', result['tables'])

    def test_enable_extension_and_create_schema_are_ignored(self):
        result = self._parse('''
          enable_extension "pgcrypto"
          create_schema "audit"
          create_table "widgets", force: :cascade do |t|
          end
        ''')
        self.assertEqual(result['warnings'], [])

    def test_unknown_top_level_statement_warns(self):
        result = self._parse('execute "SELECT 1"\n')
        self.assertTrue(any("unknown statement" in w and 'execute' in w
                            for w in result['warnings']))

    def test_unsupported_t_methods_warn_and_skip(self):
        result = self._parse('''
          create_table "widgets", force: :cascade do |t|
            t.check_constraint "a > 0", name: "check_a"
            t.virtual "computed", type: :integer, as: "1"
          end
        ''')
        self.assertTrue(any("t.check_constraint" in w for w in result['warnings']))
        self.assertTrue(any("t.virtual" in w for w in result['warnings']))

    def test_comment_stripping_preserves_hash_inside_string(self):
        result = self._parse('''
          create_table "widgets", force: :cascade do |t| # trailing comment
            t.string "tag", comment: "a #hashtag value" # another comment
          end
        ''')
        self.assertEqual(result['warnings'], [])
        col = next(c for c in result['tables']['widgets']['columns'] if c['name'] == 'tag')
        self.assertEqual(col['comment'], 'a #hashtag value')

    def test_source_and_location(self):
        result = self._parse('create_table "widgets", force: :cascade do |t|\nend\n')
        self.assertEqual(result['source']['kind'], 'schema')
        self.assertEqual(result['source']['provider'], 'rails.schema')


# ---------------------------------------------------------------------------
# Merge / reconcile with a schema-kind layer (D8.3) — synthetic layers, same
# style as tests/test_merge_ir.py.
# ---------------------------------------------------------------------------
def db_layer(tables):
    return erd.make_provider_result('db', 'mysql', tables)

def fw_layer(tables, provider='rails'):
    return erd.make_provider_result('framework', provider, tables)

def schema_layer(tables):
    return erd.make_provider_result('schema', 'rails.schema', tables)

def config_layer(tables):
    return erd.make_provider_result('config', 'config', tables)

def col(name, **attrs):
    return {'name': name, **attrs}


class TestMergeWithSchemaLayer(unittest.TestCase):
    def test_schema_only_run_yields_columns_and_schema_fk_association(self):
        schema = {
            'users': {'columns': [col('id', type='bigint', primary=True)],
                      'indexes': [], 'primary_key': 'id'},
            'posts': {'columns': [col('id', type='bigint', primary=True),
                                  col('user_id', type='bigint')],
                     'indexes': [], 'primary_key': 'id',
                     'associations': [{'type': 'belongs_to', 'name': 'user',
                                       'target': 'users', 'foreign_key': 'user_id',
                                       'schema_fk': True}]}}
        merged = erd.merge_ir([schema_layer(schema)])
        self.assertNotIn('schema_missing', merged['posts'])
        self.assertEqual(len(merged['posts']['columns']), 2)
        assoc = merged['posts']['associations'][0]
        self.assertEqual(assoc['provenance'], 'schema_fk')

    def test_model_belongs_to_same_identity_merges_to_declared_with_union_sources(self):
        schema = {'posts': {'columns': [col('id', primary=True), col('user_id')],
                            'indexes': [], 'primary_key': 'id',
                            'associations': [{'type': 'belongs_to', 'name': 'user',
                                              'target': 'users', 'foreign_key': 'user_id',
                                              'schema_fk': True}]}}
        fw = {'posts': {'associations': [{'type': 'belongs_to', 'name': 'user',
                                          'target': 'users', 'foreign_key': 'user_id'}]}}
        merged = erd.merge_ir([schema_layer(schema), fw_layer(fw)])
        assocs = merged['posts']['associations']
        self.assertEqual(len(assocs), 1)
        self.assertEqual(assocs[0]['provenance'], 'declared')
        kinds = {s['kind'] for s in assocs[0]['sources']}
        self.assertEqual(kinds, {'schema', 'framework'})

    def test_renamed_model_assoc_drops_covered_schema_fk(self):
        # schema_fk on user_id (machine-derived name "user"), but the model
        # declares belongs_to :author, foreign_key: "user_id" — same column,
        # different name -> Phase B drops the now-covered schema_fk (§8.5,
        # extended to schema_fk per D2/reconcile_db_fks).
        schema = {'posts': {'columns': [col('id', primary=True), col('user_id')],
                            'indexes': [], 'primary_key': 'id',
                            'associations': [{'type': 'belongs_to', 'name': 'user',
                                              'target': 'users', 'foreign_key': 'user_id',
                                              'schema_fk': True}]}}
        fw = {'posts': {'associations': [{'type': 'belongs_to', 'name': 'author',
                                          'target': 'users', 'foreign_key': 'user_id'}]}}
        merged = erd.merge_ir([schema_layer(schema), fw_layer(fw)])
        assocs = merged['posts']['associations']
        self.assertEqual(len(assocs), 1)
        self.assertEqual(assocs[0]['name'], 'author')
        self.assertEqual(assocs[0]['provenance'], 'declared')

    def test_live_db_and_schema_same_fk_yields_db_fk_provenance(self):
        db = {'posts': {'columns': [col('id', type='bigint', primary=True),
                                    col('user_id', type='bigint')],
                        'indexes': [], 'primary_key': 'id',
                        'associations': [{'type': 'belongs_to', 'name': 'user',
                                          'target': 'users', 'foreign_key': 'user_id',
                                          'db_fk': True}]}}
        schema = {'posts': {'columns': [col('id', type='bigint', primary=True),
                                        col('user_id', type='bigint')],
                            'indexes': [], 'primary_key': 'id',
                            'associations': [{'type': 'belongs_to', 'name': 'user',
                                              'target': 'users', 'foreign_key': 'user_id',
                                              'schema_fk': True}]}}
        merged = erd.merge_ir([db_layer(db), schema_layer(schema)])
        assocs = merged['posts']['associations']
        self.assertEqual(len(assocs), 1)
        self.assertEqual(assocs[0]['provenance'], 'db_fk')

    def test_column_attrs_prefer_db_over_schema_over_framework(self):
        db = {'widgets': {'columns': [col('id', type='bigint', primary=True),
                                      col('price', type='decimal', sql_type='decimal(8,2)')],
                          'indexes': [], 'primary_key': 'id'}}
        schema = {'widgets': {'columns': [col('price', type='decimal', sql_type='decimal(10,2)')],
                              'indexes': [], 'primary_key': 'id'}}
        merged = erd.merge_ir([db_layer(db), schema_layer(schema)])
        self.assertEqual(merged['widgets']['columns'][1]['sql_type'], 'decimal(8,2)')

    def test_config_tables_still_top_authority_over_schema(self):
        schema = {'widgets': {'columns': [col('id', primary=True),
                                          col('note', type='text')],
                              'indexes': [], 'primary_key': 'id'}}
        cfg = {'widgets': {'columns': [{'name': 'note', 'type': 'string'}]}}
        merged = erd.merge_ir([schema_layer(schema), config_layer(cfg)])
        note = next(c for c in merged['widgets']['columns'] if c['name'] == 'note')
        self.assertEqual(note['type'], 'string')

    def test_serialize_for_viewer_emits_schema_fk_legacy_flag(self):
        schema = {'posts': {'columns': [col('id', primary=True), col('user_id')],
                            'indexes': [], 'primary_key': 'id',
                            'associations': [{'type': 'belongs_to', 'name': 'user',
                                              'target': 'users', 'foreign_key': 'user_id',
                                              'schema_fk': True}]}}
        merged = erd.merge_ir([schema_layer(schema)])
        out = erd.serialize_for_viewer(merged)
        assoc = out['posts']['associations'][0]
        self.assertEqual(assoc.get('schema_fk'), True)
        self.assertNotIn('provenance', assoc)
        self.assertNotIn('sources', assoc)


# ---------------------------------------------------------------------------
# End-to-end via the CLI (D8.4) — a typed config `sources` entry pointing at
# the checked-in fixture, and an untyped --models auto-detect of a schema.rb
# file directly.
# ---------------------------------------------------------------------------
class _NoDBDriver(unittest.TestCase):
    def setUp(self):
        self._orig = (erd.parse_mysql, erd.parse_postgres)
        erd.parse_mysql = lambda url: (_ for _ in ()).throw(AssertionError('DB contacted'))
        erd.parse_postgres = lambda url: (_ for _ in ()).throw(AssertionError('DB contacted'))
        self._argv = sys.argv
        self._cwd = os.getcwd()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: setattr(sys, 'argv', self._argv))
        self.addCleanup(os.chdir, self._cwd)
        self.addCleanup(lambda: (setattr(erd, 'parse_mysql', self._orig[0]),
                                 setattr(erd, 'parse_postgres', self._orig[1])))
        os.chdir(self.tmp.name)

    def _p(self, name):
        return str(Path(self.tmp.name) / name)

    def _run(self, *argv):
        sys.argv = ['erd.py', *argv]
        erd.main()

    def _data(self, path):
        return json.loads(re.search(r'const DATA = (\{.*?\});\s*\n',
                                    Path(path).read_text()).group(1))


class TestRailsSchemaEndToEnd(_NoDBDriver):
    def test_config_sources_rails_schema_produces_columns_and_badge(self):
        cfg = self._p('c.json')
        Path(cfg).write_text(json.dumps({'sources': [
            {'id': 'schema', 'type': 'rails.schema', 'path': str(FIXTURE_SCHEMA)}]}))
        out = self._p('out.html')
        self._run('--config', cfg, '-o', out)
        data = self._data(out)['tables']
        self.assertIn('users', data)
        self.assertIn('posts', data)
        col_names = {c['name'] for c in data['posts']['columns']}
        self.assertIn('user_id', col_names)
        assoc = next(a for a in data['posts']['associations'] if a['target'] == 'users')
        self.assertTrue(assoc.get('schema_fk'))
        html = Path(out).read_text()
        self.assertIn('badge-schemafk', html)

    def test_rails_schema_path_must_be_a_file(self):
        cfg = self._p('c.json')
        Path(cfg).write_text(json.dumps({'sources': [
            {'id': 'schema', 'type': 'rails.schema', 'path': str(FIXTURE_RAILS)}]}))
        with self.assertRaises(SystemExit) as cm:
            self._run('--config', cfg, '-o', self._p('out.html'))
        msg = str(cm.exception)
        self.assertIn('rails.schema expects', msg)
        self.assertIn('schema.rb', msg)

    def test_untyped_models_path_pointing_at_schema_rb_auto_detects(self):
        out = self._p('out.html')
        err = io.StringIO()
        with redirect_stderr(err):
            self._run('--models', str(FIXTURE_SCHEMA), '--no-config', '-o', out)
        self.assertIn('auto-detected as rails.schema', err.getvalue())
        data = self._data(out)['tables']
        self.assertIn('users', data)

    def test_excel_via_column_contains_schema_fk(self):
        cfg = self._p('c.json')
        Path(cfg).write_text(json.dumps({'sources': [
            {'id': 'schema', 'type': 'rails.schema', 'path': str(FIXTURE_SCHEMA)}]}))
        out, xlsx = self._p('out.html'), self._p('defs.xlsx')
        self._run('--config', cfg, '-o', out, '--excel', xlsx)
        with zipfile.ZipFile(xlsx) as z:
            sheets = ''.join(z.read(n).decode() for n in z.namelist()
                             if n.startswith('xl/worksheets/'))
        self.assertIn('schema FK', sheets)

    def test_fixture_app_models_still_works_unaffected_by_db_schema_dir(self):
        out = self._p('out.html')
        self._run('--models', str(FIXTURE_RAILS), '--no-config', '-o', out)
        data = self._data(out)['tables']
        self.assertIn('users', data)  # RailsOverlay still finds app/models, ignores db/


# ---------------------------------------------------------------------------
# rails.project macro (D3/D4/D8.2) — normalize_input_specs expands it away
# before dispatch ever sees it; unit-test the expansion directly, then prove
# an expanded project runs schema+models together through the real CLI.
# ---------------------------------------------------------------------------
class TestRailsProjectMacro(unittest.TestCase):
    def test_both_present_expands_to_schema_then_models_no_note(self):
        err = io.StringIO()
        with redirect_stderr(err):
            specs = erd.normalize_input_specs(
                [], [{'id': 'app', 'type': 'rails.project', 'path': str(FIXTURE_RAILS)}])
        self.assertEqual([(s['id'], s['type']) for s in specs],
                         [('app:schema', 'rails.schema'), ('app:models', 'rails.models')])
        self.assertEqual(specs[0]['path'], FIXTURE_RAILS.resolve() / 'db' / 'schema.rb')
        self.assertEqual(specs[1]['path'], FIXTURE_RAILS.resolve() / 'app' / 'models')
        self.assertEqual(err.getvalue(), '')  # both present -> silent, no skip note

    def test_schema_only_present_notes_the_skipped_models_half(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'db').mkdir()
            (root / 'db' / 'schema.rb').write_text(
                'create_table "widgets", force: :cascade do |t|\nend\n')
            err = io.StringIO()
            with redirect_stderr(err):
                specs = erd.normalize_input_specs(
                    [], [{'id': 'app', 'type': 'rails.project', 'path': str(root)}])
        self.assertEqual([s['type'] for s in specs], ['rails.schema'])
        self.assertIn('rails.project', err.getvalue())
        self.assertIn('rails.models half', err.getvalue())

    def test_models_only_present_notes_the_skipped_schema_half(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'app' / 'models').mkdir(parents=True)
            (root / 'app' / 'models' / 'widget.rb').write_text(
                'class Widget < ApplicationRecord\nend\n')
            err = io.StringIO()
            with redirect_stderr(err):
                specs = erd.normalize_input_specs(
                    [], [{'id': 'app', 'type': 'rails.project', 'path': str(root)}])
        self.assertEqual([s['type'] for s in specs], ['rails.models'])
        self.assertIn('rails.schema half', err.getvalue())

    def test_neither_present_is_a_hard_error_naming_both_paths(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with self.assertRaises(SystemExit) as cm:
                erd.normalize_input_specs(
                    [], [{'id': 'app', 'type': 'rails.project', 'path': str(root)}])
        msg = str(cm.exception)
        self.assertIn('db', msg)
        self.assertIn('schema.rb', msg)
        self.assertIn('app', msg)
        self.assertIn('models', msg)

    def test_known_source_type_names_includes_the_macro(self):
        self.assertIn('rails.project', erd.known_source_type_names())


class TestRailsProjectMacroEndToEnd(_NoDBDriver):
    def test_project_macro_merges_schema_and_model_layers(self):
        cfg = self._p('c.json')
        Path(cfg).write_text(json.dumps({'sources': [
            {'id': 'app', 'type': 'rails.project', 'path': str(FIXTURE_RAILS)}]}))
        out = self._p('out.html')
        self._run('--config', cfg, '-o', out)
        data = self._data(out)['tables']
        # rails.schema half: real columns, no schema_missing
        self.assertFalse(data['users'].get('schema_missing'))
        self.assertIn('email', {c['name'] for c in data['users']['columns']})
        # rails.models half: fixture_app's Rails models declare associations
        # that aren't in the (deliberately small) fixture schema.rb — e.g.
        # webhooks only exists via app/models, proving that half ran too
        self.assertIn('webhooks', data)


if __name__ == '__main__':
    unittest.main()
