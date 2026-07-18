"""Config `tables:`/`title` syntactic validation — REFACTOR_PLAN.md §15 Step 3.

These cover ONLY the syntactic (IR-free) validation added to load_config /
_check_config_types: shape, required fields, types, *_mode values,
DropOperation identity, single-column-FK enforcement, and Config-internal
duplicates. The critical boundary test (TestSyntacticVsSemanticBoundary)
locks in that this layer does NOT do existence/referential checks — a config
that drops or references a not-yet-known table/column must load cleanly; those
checks belong to Step 7 (semantic validation, at apply time).

Run from the repository root:
    python3 -m unittest tests.test_config_validation -v
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('erd', ROOT / 'erd.py')
erd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(erd)


def _load(obj):
    """Round-trip `obj` through a temp JSON config file and load_config —
    the same mechanism the existing TestConfigTypeValidation uses, so these
    exercise the real entry point (auto-discovery disabled via an explicit
    --config path)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'my.json'
        path.write_text(json.dumps(obj))
        return erd.load_config(SimpleNamespace(config=str(path), no_config=False))


class TestTopLevelSchemaKeys(unittest.TestCase):
    def test_title_and_tables_are_accepted(self):
        cfg = _load({'title': 'billing', 'tables': {'users': {}}})
        self.assertEqual(cfg['title'], 'billing')
        self.assertEqual(cfg['tables'], {'users': {}})

    def test_title_must_be_a_string(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'title': 123})
        self.assertIn('title', str(cm.exception))

    def test_name_at_top_level_is_rejected(self):
        # `name` is deliberately not a top-level key (§6.2) — it's caught by
        # the existing unknown-key guard
        with self.assertRaises(SystemExit) as cm:
            _load({'name': 'billing'})
        self.assertIn('name', str(cm.exception))

    def test_tables_must_be_a_map(self):
        for bad in ([{'name': 'users'}], 'users', 42):
            with self.assertRaises(SystemExit) as cm:
                _load({'tables': bad})
            self.assertIn('tables', str(cm.exception))

    def test_a_table_definition_must_be_an_object(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'tables': {'users': 'not-an-object'}})
        self.assertIn('users', str(cm.exception))

    def test_empty_tables_map_is_syntactically_fine(self):
        # emptiness / "at least one table" is a §6.5 concern for Step 8/9's
        # validity check, not a syntactic error here
        self.assertEqual(_load({'tables': {}})['tables'], {})


class TestTableLevelFragment(unittest.TestCase):
    def test_valid_full_table_passes(self):
        cfg = _load({'tables': {'customers': {
            'comment': 'customer accounts',
            'primary_key': 'id',
            'columns': [
                {'name': 'id', 'type': 'bigint', 'primary': True},
                {'name': 'email', 'type': 'varchar', 'nullable': False, 'comment': 'login'},
            ],
            'indexes': [{'name': 'idx_customers_email', 'columns': ['email'], 'unique': True}],
            'associations': [{'type': 'has_many', 'name': 'invoices', 'target': 'invoices'}],
        }}})
        self.assertIn('customers', cfg['tables'])

    def test_composite_primary_key_list_allowed(self):
        cfg = _load({'tables': {'order_items': {'primary_key': ['order_id', 'item_id']}}})
        self.assertEqual(cfg['tables']['order_items']['primary_key'], ['order_id', 'item_id'])

    def test_primary_key_null_allowed(self):
        self.assertIsNone(_load({'tables': {'t': {'primary_key': None}}})['tables']['t']['primary_key'])

    def test_primary_key_wrong_type_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'tables': {'t': {'primary_key': 5}}})
        self.assertIn('primary_key', str(cm.exception))

    def test_primary_key_list_with_non_string_rejected(self):
        with self.assertRaises(SystemExit):
            _load({'tables': {'t': {'primary_key': ['id', 7]}}})

    def test_comment_null_allowed(self):
        # null/"" comment is an explicit "delete the comment" op (§6.3)
        self.assertIsNone(_load({'tables': {'t': {'comment': None}}})['tables']['t']['comment'])

    def test_comment_wrong_type_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'tables': {'t': {'comment': 42}}})
        self.assertIn('comment', str(cm.exception))

    def test_mode_replace_and_merge_allowed(self):
        for mode in ('merge', 'replace'):
            cfg = _load({'tables': {'t': {'columns_mode': mode, 'columns': []}}})
            self.assertEqual(cfg['tables']['t']['columns_mode'], mode)

    def test_bad_mode_value_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'tables': {'t': {'associations_mode': 'overwrite'}}})
        self.assertIn('associations_mode', str(cm.exception))

    def test_all_three_mode_keys_checked(self):
        for mk in ('columns_mode', 'indexes_mode', 'associations_mode'):
            with self.assertRaises(SystemExit) as cm:
                _load({'tables': {'t': {mk: 'nope'}}})
            self.assertIn(mk, str(cm.exception))

    def test_table_drop_needs_only_the_key(self):
        # TableDrop identity is the map key, so { drop: true } alone is valid
        self.assertTrue(_load({'tables': {'temp': {'drop': True}}})['tables']['temp']['drop'])

    def test_drop_must_be_bool(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'tables': {'t': {'drop': 'yes'}}})
        self.assertIn('drop', str(cm.exception))


class TestColumnFragmentAndDrop(unittest.TestCase):
    def test_columns_must_be_a_list(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'tables': {'t': {'columns': {'name': 'id'}}}})
        self.assertIn('columns', str(cm.exception))

    def test_column_requires_name(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'tables': {'t': {'columns': [{'type': 'bigint'}]}}})
        self.assertIn('name', str(cm.exception))

    def test_column_drop_needs_only_name(self):
        cfg = _load({'tables': {'t': {'columns': [{'name': 'legacy', 'drop': True}]}}})
        self.assertTrue(cfg['tables']['t']['columns'][0]['drop'])

    def test_column_drop_still_requires_name(self):
        with self.assertRaises(SystemExit):
            _load({'tables': {'t': {'columns': [{'drop': True}]}}})

    def test_column_attr_type_checks(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'tables': {'t': {'columns': [{'name': 'id', 'nullable': 'yes'}]}}})
        self.assertIn('nullable', str(cm.exception))

    def test_column_type_field_must_be_string(self):
        with self.assertRaises(SystemExit):
            _load({'tables': {'t': {'columns': [{'name': 'id', 'type': 5}]}}})

    def test_duplicate_column_name_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'tables': {'t': {'columns': [{'name': 'id'}, {'name': 'id'}]}}})
        self.assertIn('duplicate', str(cm.exception).lower())


class TestIndexFragmentAndDrop(unittest.TestCase):
    def test_index_requires_name(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'tables': {'t': {'indexes': [{'columns': ['email']}]}}})
        self.assertIn('name', str(cm.exception))

    def test_index_requires_columns(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'tables': {'t': {'indexes': [{'name': 'idx'}]}}})
        self.assertIn('columns', str(cm.exception))

    def test_index_columns_must_be_list_of_strings(self):
        with self.assertRaises(SystemExit):
            _load({'tables': {'t': {'indexes': [{'name': 'idx', 'columns': ['a', 3]}]}}})

    def test_index_drop_needs_only_name(self):
        cfg = _load({'tables': {'t': {'indexes': [{'name': 'idx_old', 'drop': True}]}}})
        self.assertTrue(cfg['tables']['t']['indexes'][0]['drop'])

    def test_index_unique_must_be_bool(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'tables': {'t': {'indexes': [
                {'name': 'idx', 'columns': ['a'], 'unique': 'true'}]}}})
        self.assertIn('unique', str(cm.exception))

    def test_duplicate_index_name_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'tables': {'t': {'indexes': [
                {'name': 'idx', 'columns': ['a']},
                {'name': 'idx', 'columns': ['b']}]}}})
        self.assertIn('duplicate', str(cm.exception).lower())


class TestAssociationFragmentAndDrop(unittest.TestCase):
    def test_fragment_requires_type_name_target(self):
        for bad in ({'name': 'x', 'target': 'y'},
                    {'type': 'has_many', 'target': 'y'},
                    {'type': 'has_many', 'name': 'x'}):
            with self.assertRaises(SystemExit):
                _load({'tables': {'t': {'associations': [bad]}}})

    def test_fragment_type_must_be_valid(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'tables': {'t': {'associations': [
                {'type': 'has_lots', 'name': 'x', 'target': 'y'}]}}})
        self.assertIn('type', str(cm.exception))

    def test_valid_fragment_passes(self):
        cfg = _load({'tables': {'t': {'associations': [
            {'type': 'belongs_to', 'name': 'customer', 'target': 'customers',
             'foreign_key': 'customer_id'}]}}})
        self.assertEqual(cfg['tables']['t']['associations'][0]['name'], 'customer')

    def test_owner_fk_drop_needs_target_and_foreign_key(self):
        # valid owner_fk drop
        cfg = _load({'tables': {'t': {'associations': [
            {'target': 'users', 'foreign_key': 'author_id', 'drop': True}]}}})
        self.assertTrue(cfg['tables']['t']['associations'][0]['drop'])
        # missing target -> cannot identify
        with self.assertRaises(SystemExit) as cm:
            _load({'tables': {'t': {'associations': [
                {'foreign_key': 'author_id', 'drop': True}]}}})
        self.assertIn('target', str(cm.exception))

    def test_collection_drop_needs_type_target_name(self):
        # valid collection/inverse drop
        cfg = _load({'tables': {'t': {'associations': [
            {'type': 'has_many', 'name': 'posts', 'target': 'posts', 'drop': True}]}}})
        self.assertTrue(cfg['tables']['t']['associations'][0]['drop'])
        # a drop with neither an FK nor a full type+target+name can't be identified
        with self.assertRaises(SystemExit) as cm:
            _load({'tables': {'t': {'associations': [
                {'type': 'has_many', 'target': 'posts', 'drop': True}]}}})
        self.assertIn('identify', str(cm.exception).lower())

    def test_drop_does_not_require_name_when_owner_fk(self):
        # the Fragment-vs-Drop split: `name` is required for a fragment but an
        # owner_fk drop is identified by target+foreign_key, so no `name` needed
        cfg = _load({'tables': {'t': {'associations': [
            {'target': 'users', 'foreign_key': 'created_by_id', 'drop': True}]}}})
        self.assertNotIn('name', cfg['tables']['t']['associations'][0])

    def test_composite_foreign_key_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'tables': {'t': {'associations': [
                {'type': 'belongs_to', 'name': 'x', 'target': 'y',
                 'foreign_key': ['a_id', 'b_id']}]}}})
        msg = str(cm.exception).lower()
        self.assertIn('composite', msg)

    def test_foreign_key_non_string_rejected(self):
        with self.assertRaises(SystemExit):
            _load({'tables': {'t': {'associations': [
                {'type': 'belongs_to', 'name': 'x', 'target': 'y', 'foreign_key': 5}]}}})

    def test_duplicate_owner_fk_identity_rejected(self):
        # an EXACT duplicate (same name + fk + target) is still an error
        with self.assertRaises(SystemExit) as cm:
            _load({'tables': {'t': {'associations': [
                {'type': 'belongs_to', 'name': 'a', 'target': 'users', 'foreign_key': 'uid'},
                {'type': 'belongs_to', 'name': 'a', 'target': 'users', 'foreign_key': 'uid'}]}}})
        self.assertIn('duplicate', str(cm.exception).lower())

    def test_owner_fk_aliases_on_same_column_allowed(self):
        # P1-e: the Rails alias pattern — two differently-NAMED owner_fk
        # associations on the same fk column/target (user AND author, both on
        # user_id -> users) is two distinct associations (aligned with the
        # runtime association_key, 6b), NOT a duplicate. Must load cleanly.
        cfg = _load({'tables': {'t': {'associations': [
            {'type': 'belongs_to', 'name': 'user', 'target': 'users', 'foreign_key': 'user_id'},
            {'type': 'belongs_to', 'name': 'author', 'target': 'users', 'foreign_key': 'user_id'}]}}})
        self.assertEqual(len(cfg['tables']['t']['associations']), 2)

    def test_duplicate_collection_identity_rejected(self):
        with self.assertRaises(SystemExit):
            _load({'tables': {'t': {'associations': [
                {'type': 'has_many', 'name': 'posts', 'target': 'posts'},
                {'type': 'has_many', 'name': 'posts', 'target': 'posts'}]}}})

    def test_two_fks_to_same_target_are_not_duplicates(self):
        # created_by_id / updated_by_id to the same target differ by FK column,
        # so their identities differ — must NOT be flagged as duplicates
        cfg = _load({'tables': {'posts': {'associations': [
            {'type': 'belongs_to', 'name': 'creator', 'target': 'users', 'foreign_key': 'created_by_id'},
            {'type': 'belongs_to', 'name': 'updater', 'target': 'users', 'foreign_key': 'updated_by_id'}]}}})
        self.assertEqual(len(cfg['tables']['posts']['associations']), 2)

    def test_through_variants_are_not_duplicates(self):
        # P2: same type/name/target but a different `through` are distinct edges
        # at runtime (association_key includes through), so the syntactic dup
        # check must accept them too rather than falsely reject the second.
        cfg = _load({'tables': {'users': {'associations': [
            {'type': 'has_many', 'name': 'products', 'target': 'products', 'through': 'orders'},
            {'type': 'has_many', 'name': 'products', 'target': 'products',
             'through': 'archived_orders'}]}}})
        self.assertEqual(len(cfg['tables']['users']['associations']), 2)

    def test_polymorphic_flag_distinguishes_identity(self):
        # P2: polymorphic is part of the runtime identity, so a polymorphic and
        # a non-polymorphic association that otherwise match are not duplicates.
        cfg = _load({'tables': {'comments': {'associations': [
            {'type': 'belongs_to', 'name': 'subject', 'target': 'posts', 'foreign_key': 'subject_id'},
            {'type': 'belongs_to', 'name': 'subject', 'target': 'posts',
             'foreign_key': 'subject_id', 'polymorphic': True}]}}})
        self.assertEqual(len(cfg['tables']['comments']['associations']), 2)

    def test_exact_duplicate_with_same_through_still_rejected(self):
        # the fix must not weaken the real check: identical through and all —
        # still a duplicate.
        with self.assertRaises(SystemExit) as cm:
            _load({'tables': {'users': {'associations': [
                {'type': 'has_many', 'name': 'products', 'target': 'products', 'through': 'orders'},
                {'type': 'has_many', 'name': 'products', 'target': 'products', 'through': 'orders'}]}}})
        self.assertIn('duplicate', str(cm.exception).lower())


class TestUnknownNestedKeys(unittest.TestCase):
    """Typo protection (§6.4 "typo を黙って無視しない"): a misspelled nested
    key inside a well-defined structure must be a hard error, not silently
    ignored. Allow-lists are fixed per structure (not derived from the Step-2
    contract types)."""

    def test_unknown_table_key_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'tables': {'orders': {'primary_ky': 'id'}}})
        msg = str(cm.exception)
        self.assertIn('primary_ky', msg)
        self.assertIn('tables.orders', msg)

    def test_unknown_column_key_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'tables': {'orders': {'columns': [{'name': 'id', 'nulable': True}]}}})
        msg = str(cm.exception)
        self.assertIn('nulable', msg)
        self.assertIn('columns[0]', msg)

    def test_unknown_index_key_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'tables': {'t': {'indexes': [
                {'name': 'idx', 'columns': ['a'], 'uniqe': True}]}}})
        self.assertIn('uniqe', str(cm.exception))

    def test_unknown_association_key_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'tables': {'t': {'associations': [
                {'type': 'belongs_to', 'name': 'x', 'target': 'y', 'foriegn_key': 'x_id'}]}}})
        self.assertIn('foriegn_key', str(cm.exception))

    def test_all_documented_keys_pass(self):
        # every allowed key on every structure, at once, must load cleanly
        cfg = _load({'title': 'ok', 'tables': {'t': {
            'comment': 'c', 'primary_key': 'id', 'drop': False,
            'columns_mode': 'merge', 'indexes_mode': 'replace', 'associations_mode': 'merge',
            'columns': [{'name': 'id', 'type': 'bigint', 'sql_type': 'bigint',
                         'nullable': False, 'primary': True, 'default': '0',
                         'extra': 'auto_increment', 'comment': 'pk'}],
            'indexes': [{'name': 'idx', 'columns': ['id'], 'unique': True}],
            'associations': [{'type': 'belongs_to', 'name': 'owner', 'target': 'users',
                              'foreign_key': 'owner_id', 'through': None,
                              'polymorphic': False}],
        }}})
        self.assertIn('t', cfg['tables'])


class TestSyntacticVsSemanticBoundary(unittest.TestCase):
    """The P0-1 crux: load_config does SYNTACTIC validation only. A config that
    drops or references tables/columns/targets nothing yet knows about must
    load without error — existence is checked later, at apply time (Step 7),
    once every provider's IR is collected. If any of these start failing at
    load time, the syntactic/semantic boundary has been violated."""

    def test_drop_of_unknown_table_loads(self):
        cfg = _load({'tables': {'not_in_any_db_yet': {'drop': True}}})
        self.assertIn('not_in_any_db_yet', cfg['tables'])

    def test_drop_of_unknown_column_loads(self):
        cfg = _load({'tables': {'users': {'columns': [
            {'name': 'never_heard_of_this', 'drop': True}]}}})
        self.assertTrue(cfg['tables']['users']['columns'][0]['drop'])

    def test_drop_of_unknown_index_loads(self):
        cfg = _load({'tables': {'users': {'indexes': [
            {'name': 'idx_nonexistent', 'drop': True}]}}})
        self.assertIn('users', cfg['tables'])

    def test_association_to_unknown_target_loads(self):
        # target 'planets' need not exist yet — resolving it is semantic (Step 7)
        cfg = _load({'tables': {'users': {'associations': [
            {'type': 'belongs_to', 'name': 'planet', 'target': 'planets',
             'foreign_key': 'planet_id'}]}}})
        self.assertEqual(cfg['tables']['users']['associations'][0]['target'], 'planets')

    def test_primary_key_naming_unknown_column_loads(self):
        # whether 'id' actually exists as a column is a semantic (§7.3) check
        cfg = _load({'tables': {'ghost': {'primary_key': 'id'}}})
        self.assertEqual(cfg['tables']['ghost']['primary_key'], 'id')

    def test_self_referential_and_cross_table_relations_load(self):
        # self-reference (source == target) and A->B->A cross-references are
        # explicitly legal (§6.4) — only broken refs are errors, and that's
        # a semantic check
        cfg = _load({'tables': {
            'a': {'associations': [
                {'type': 'belongs_to', 'name': 'parent', 'target': 'a', 'foreign_key': 'parent_id'},
                {'type': 'belongs_to', 'name': 'b', 'target': 'b', 'foreign_key': 'b_id'}]},
            'b': {'associations': [
                {'type': 'belongs_to', 'name': 'a', 'target': 'a', 'foreign_key': 'a_id'}]},
        }})
        self.assertIn('a', cfg['tables'])
        self.assertIn('b', cfg['tables'])


class TestLegacyRelationsUnchanged(unittest.TestCase):
    """The legacy `relations` key keeps its existing syntactic check and does
    NOT gain any load-time existence check (those stay in
    relations_to_config_layer at apply time)."""

    def test_relations_referencing_unknown_tables_loads(self):
        cfg = _load({'relations': [
            {'table': 'orders', 'column': 'buyer_id', 'references': 'users'}]})
        self.assertEqual(len(cfg['relations']), 1)

    def test_relations_still_rejects_non_object_entries(self):
        with self.assertRaises(SystemExit):
            _load({'relations': ['not-an-object']})


class TestConfigModelsKey(unittest.TestCase):
    """P1-a: config `models` accepts a single path (str) or a list of paths."""

    def test_models_string_accepted(self):
        self.assertEqual(_load({'models': '/some/app'})['models'], '/some/app')

    def test_models_list_accepted(self):
        self.assertEqual(_load({'models': ['/a', '/b']})['models'], ['/a', '/b'])

    def test_models_list_with_non_string_element_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'models': ['/a', 5]})
        self.assertIn('models', str(cm.exception))

    def test_models_wrong_type_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'models': 5})
        self.assertIn('models', str(cm.exception))


class TestConfigVersionKey(unittest.TestCase):
    """`version:` is a purely documented marker (no runtime behavior) — the
    only accepted value is the literal int 1."""

    def test_version_1_accepted(self):
        self.assertEqual(_load({'version': 1})['version'], 1)

    def test_version_2_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'version': 2})
        msg = str(cm.exception)
        self.assertIn('version', msg)
        self.assertIn('must be 1', msg)

    def test_version_as_string_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'version': '1'})
        self.assertIn('version', str(cm.exception))

    def test_version_as_bool_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'version': True})
        self.assertIn('version', str(cm.exception))

    def test_absent_version_is_fine(self):
        self.assertNotIn('version', _load({'title': 'x'}))


class TestConfigSourcesKey(unittest.TestCase):
    """D5: config `sources` — a typed code-source list. Purely syntactic here
    (shape/required-keys/duplicate-id); whether `type` names a REGISTERED
    source type is a dispatch-time concern (sources.py), not load time."""

    def test_valid_sources_list_accepted(self):
        cfg = _load({'sources': [
            {'id': 'app', 'type': 'rails.models', 'path': '/proj/app/models'},
            {'id': 'schema', 'type': 'rails.schema', 'path': '/proj/db/schema.rb'}]})
        self.assertEqual(len(cfg['sources']), 2)

    def test_unregistered_type_still_loads_clean(self):
        # a plugin type an --adapter file registers later; unknown at load
        # time is fine (only dispatch checks registration)
        cfg = _load({'sources': [{'id': 'x', 'type': 'totally.unknown', 'path': '/a'}]})
        self.assertEqual(cfg['sources'][0]['type'], 'totally.unknown')

    def test_sources_must_be_a_list(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'sources': {'id': 'x'}})
        self.assertIn('sources', str(cm.exception))

    def test_source_entry_must_be_an_object(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'sources': ['not-an-object']})
        self.assertIn('sources[0]', str(cm.exception))

    def test_source_missing_id_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'sources': [{'type': 'rails.models', 'path': '/a'}]})
        self.assertIn('id', str(cm.exception))

    def test_source_missing_type_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'sources': [{'id': 'x', 'path': '/a'}]})
        self.assertIn('type', str(cm.exception))

    def test_source_missing_path_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'sources': [{'id': 'x', 'type': 'rails.models'}]})
        self.assertIn('path', str(cm.exception))

    def test_source_empty_id_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'sources': [{'id': '', 'type': 'rails.models', 'path': '/a'}]})
        self.assertIn('id', str(cm.exception))

    def test_source_unknown_key_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'sources': [{'id': 'x', 'type': 'rails.models', 'path': '/a',
                                'bogus': 1}]})
        self.assertIn('bogus', str(cm.exception))

    def test_source_allow_empty_accepted(self):
        cfg = _load({'sources': [{'id': 'x', 'type': 'rails.models', 'path': '/a',
                                  'allow_empty': True}]})
        self.assertIs(cfg['sources'][0]['allow_empty'], True)

    def test_source_allow_empty_must_be_bool(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'sources': [{'id': 'x', 'type': 'rails.models', 'path': '/a',
                                'allow_empty': 'yes'}]})
        self.assertIn('allow_empty', str(cm.exception))

    def test_duplicate_source_id_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'sources': [{'id': 'x', 'type': 'rails.models', 'path': '/a'},
                               {'id': 'x', 'type': 'rails.schema', 'path': '/b'}]})
        self.assertIn('duplicate', str(cm.exception))
        self.assertIn('x', str(cm.exception))

    def test_empty_sources_list_is_syntactically_fine(self):
        self.assertEqual(_load({'sources': []})['sources'], [])


class TestConfigNotesValidation(unittest.TestCase):
    """`notes:` syntactic validation (config.py `_check_config_notes`) — notes
    Phase 1. Purely syntactic: target existence (table/relation identity
    actually resolving in the final IR) is semantic validation, covered in
    tests/test_notes.py against providers.resolve_and_validate_notes, NOT
    here (mirrors the tables §6.4①/② split already tested above)."""

    def _note(self, **overrides):
        n = {'id': 'n1', 'target': {'type': 'global'}, 'text': 'hello'}
        n.update(overrides)
        return n

    def test_minimal_global_note_accepted(self):
        cfg = _load({'notes': [self._note()]})
        self.assertEqual(cfg['notes'][0]['id'], 'n1')

    def test_notes_must_be_a_list(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'notes': {'id': 'n1'}})
        self.assertIn('notes', str(cm.exception))

    def test_note_must_be_an_object(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'notes': ['nope']})
        self.assertIn('notes[0]', str(cm.exception))

    def test_unknown_top_level_key_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'notes': [self._note(bogus=1)]})
        self.assertIn('bogus', str(cm.exception))

    def test_missing_id_rejected(self):
        n = self._note()
        del n['id']
        with self.assertRaises(SystemExit) as cm:
            _load({'notes': [n]})
        self.assertIn('id', str(cm.exception))

    def test_empty_id_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'notes': [self._note(id='')]})
        self.assertIn('id', str(cm.exception))

    def test_non_string_id_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'notes': [self._note(id=123)]})
        self.assertIn('id', str(cm.exception))

    def test_duplicate_id_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'notes': [self._note(id='dup'), self._note(id='dup')]})
        self.assertIn('duplicate', str(cm.exception))
        self.assertIn('dup', str(cm.exception))

    def test_missing_text_rejected(self):
        n = self._note()
        del n['text']
        with self.assertRaises(SystemExit) as cm:
            _load({'notes': [n]})
        self.assertIn('n1', str(cm.exception))
        self.assertIn('text', str(cm.exception))

    def test_empty_text_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'notes': [self._note(text='')]})
        self.assertIn('text', str(cm.exception))

    def test_non_string_text_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'notes': [self._note(text=123)]})
        self.assertIn('text', str(cm.exception))

    def test_title_optional(self):
        cfg = _load({'notes': [self._note(title='Heading')]})
        self.assertEqual(cfg['notes'][0]['title'], 'Heading')

    def test_non_string_title_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'notes': [self._note(title=123)]})
        self.assertIn('title', str(cm.exception))

    def test_links_must_be_a_list(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'notes': [self._note(links={'url': 'https://x'})]})
        self.assertIn('links', str(cm.exception))

    def test_link_must_be_an_object(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'notes': [self._note(links=['https://x'])]})
        self.assertIn('n1', str(cm.exception))

    def test_link_unknown_key_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'notes': [self._note(links=[{'url': 'https://x', 'bogus': 1}])]})
        self.assertIn('bogus', str(cm.exception))

    def test_link_missing_url_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'notes': [self._note(links=[{'label': 'ADR'}])]})
        self.assertIn('url', str(cm.exception))

    def test_link_url_must_be_http_or_https(self):
        for bad in ('javascript:alert(1)', 'data:text/html,x', 'file:///etc/passwd', 'ftp://x'):
            with self.assertRaises(SystemExit) as cm:
                _load({'notes': [self._note(links=[{'url': bad}])]})
            self.assertIn('http', str(cm.exception))

    def test_link_url_empty_string_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'notes': [self._note(links=[{'url': ''}])]})
        self.assertIn('url', str(cm.exception))

    def test_link_url_accepts_http_and_https(self):
        cfg = _load({'notes': [self._note(
            links=[{'label': 'a', 'url': 'http://example.com'},
                   {'label': 'b', 'url': 'https://example.com'}])]})
        self.assertEqual(len(cfg['notes'][0]['links']), 2)

    def test_link_url_scheme_check_is_case_insensitive(self):
        cfg = _load({'notes': [self._note(links=[{'url': 'HTTPS://example.com'}])]})
        self.assertEqual(cfg['notes'][0]['links'][0]['url'], 'HTTPS://example.com')

    def test_link_label_must_be_a_string(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'notes': [self._note(links=[{'label': 123, 'url': 'https://x'}])]})
        self.assertIn('label', str(cm.exception))

    def test_target_must_be_an_object(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'notes': [self._note(target='table')]})
        self.assertIn('n1', str(cm.exception))

    def test_target_type_must_be_known(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'notes': [self._note(target={'type': 'bogus'})]})
        self.assertIn('n1', str(cm.exception))

    def test_global_target_rejects_extra_keys(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'notes': [self._note(target={'type': 'global', 'table': 'x'})]})
        self.assertIn('table', str(cm.exception))

    def test_table_target_requires_table_name(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'notes': [self._note(target={'type': 'table'})]})
        self.assertIn('table', str(cm.exception))

    def test_table_target_accepted(self):
        cfg = _load({'notes': [self._note(target={'type': 'table', 'table': 'invoices'})]})
        self.assertEqual(cfg['notes'][0]['target']['table'], 'invoices')

    def test_relation_target_requires_source_and_target_table(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'notes': [self._note(target={'type': 'relation', 'source_table': 'orders'})]})
        self.assertIn('target_table', str(cm.exception))

    def test_relation_target_minimal_accepted(self):
        cfg = _load({'notes': [self._note(target={
            'type': 'relation', 'source_table': 'orders', 'target_table': 'users'})]})
        self.assertEqual(cfg['notes'][0]['target']['target_table'], 'users')

    def test_relation_target_full_narrowing_accepted(self):
        cfg = _load({'notes': [self._note(target={
            'type': 'relation', 'source_table': 'orders', 'target_table': 'users',
            'foreign_key': 'user_id', 'name': 'user', 'assoc_type': 'belongs_to',
            'through': None, 'polymorphic': False})]})
        self.assertEqual(cfg['notes'][0]['target']['foreign_key'], 'user_id')
        self.assertEqual(cfg['notes'][0]['target']['assoc_type'], 'belongs_to')

    def test_relation_target_assoc_type_accepted(self):
        for at in ('has_many', 'belongs_to', 'has_one', 'has_and_belongs_to_many'):
            cfg = _load({'notes': [self._note(target={
                'type': 'relation', 'source_table': 'orders', 'target_table': 'users',
                'assoc_type': at})]})
            self.assertEqual(cfg['notes'][0]['target']['assoc_type'], at)

    def test_relation_target_assoc_type_invalid_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'notes': [self._note(target={
                'type': 'relation', 'source_table': 'orders', 'target_table': 'users',
                'assoc_type': 'nonsense'})]})
        self.assertIn('assoc_type', str(cm.exception))

    def test_relation_target_foreign_key_composite_list_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'notes': [self._note(target={
                'type': 'relation', 'source_table': 'orders', 'target_table': 'users',
                'foreign_key': ['a', 'b']})]})
        self.assertIn('foreign_key', str(cm.exception))

    def test_relation_target_polymorphic_must_be_bool(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'notes': [self._note(target={
                'type': 'relation', 'source_table': 'orders', 'target_table': 'users',
                'polymorphic': 'yes'})]})
        self.assertIn('polymorphic', str(cm.exception))

    def test_relation_target_rejects_unknown_key(self):
        with self.assertRaises(SystemExit) as cm:
            _load({'notes': [self._note(target={
                'type': 'relation', 'source_table': 'orders', 'target_table': 'users',
                'bogus': 1})]})
        self.assertIn('bogus', str(cm.exception))

    def test_empty_notes_list_is_syntactically_fine(self):
        self.assertEqual(_load({'notes': []})['notes'], [])


class TestConfigOwnerFkAliasMerge(unittest.TestCase):
    def test_owner_fk_aliases_survive_into_merged_ir(self):
        # P1-e: two aliased owner_fk associations on the same column both reach
        # the merged IR (not collapsed / rejected).
        db = erd.make_provider_result('db', 'mysql', {
            'posts': {'columns': [{'name': 'user_id'}], 'indexes': [], 'associations': []},
            'users': {'columns': [{'name': 'id'}], 'indexes': [], 'associations': []}})
        cfg = erd.make_provider_result('config', 'config', {'posts': {'associations': [
            {'type': 'belongs_to', 'name': 'user', 'target': 'users', 'foreign_key': 'user_id'},
            {'type': 'belongs_to', 'name': 'author', 'target': 'users', 'foreign_key': 'user_id'}]}})
        merged = erd.merge_ir([db, cfg])
        names = sorted(a['name'] for a in merged['posts']['associations'])
        self.assertEqual(names, ['author', 'user'])


if __name__ == '__main__':
    unittest.main()
