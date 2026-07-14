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
