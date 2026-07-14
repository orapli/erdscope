"""Unit tests for the pure layered-merge core — REFACTOR_PLAN.md §15 Step 6a.

merge_ir(layers) and reconcile_db_fks(tables) are built and tested here in
isolation with SYNTHETIC ProviderResult layers (no real parsers needed). They
are NOT yet wired into main/_finish (Step 6b), so nothing in the pipeline
changes — this file is the payload that proves §7 (field rules), §8
(association identity / reconcile), and §9 (provenance) before the rewire.

Run from the repository root:
    python3 -m unittest tests.test_merge_ir -v
"""
import contextlib
import copy
import importlib.util
import io
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('erd', ROOT / 'erd.py')
erd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(erd)


def db_layer(tables):
    return erd.make_provider_result('db', 'mysql', tables)

def fw_layer(tables, provider='rails'):
    return erd.make_provider_result('framework', provider, tables)

def config_layer(tables):
    return erd.make_provider_result('config', 'config', tables)


def col(name, **attrs):
    return {'name': name, **attrs}


# ---------------------------------------------------------------------------
# §16.3 — columns / physical vs logical authority / determinism / purity
# ---------------------------------------------------------------------------
class TestColumnMerge(unittest.TestCase):
    def test_rails_columnless_fragment_does_not_erase_db_columns(self):
        db = {'posts': {
            'columns': [col('id', type='bigint', primary=True),
                        col('user_id', type='bigint', sql_type='bigint')],
            'indexes': [{'name': 'PRIMARY', 'columns': ['id'], 'unique': True}],
            'primary_key': 'id',
            'associations': []}}
        rails = {'posts': {  # NO columns key — associations only
            'associations': [{'type': 'belongs_to', 'name': 'author',
                              'target': 'users', 'foreign_key': 'user_id'}]}}
        merged = erd.merge_ir([db_layer(db), fw_layer(rails)])
        self.assertEqual([c['name'] for c in merged['posts']['columns']], ['id', 'user_id'])
        self.assertEqual(merged['posts']['indexes'],
                         [{'name': 'PRIMARY', 'columns': ['id'], 'unique': True}])
        self.assertEqual(merged['posts']['primary_key'], 'id')
        self.assertNotIn('schema_missing', merged['posts'])
        self.assertEqual(merged['posts']['associations'][0]['name'], 'author')

    def test_framework_columns_used_when_no_db_layer(self):
        prisma = {'users': {
            'columns': [col('id', type='integer', primary=True),
                        col('email', type='string', nullable=False)],
            'primary_key': 'id', 'associations': []}}
        merged = erd.merge_ir([fw_layer(prisma, 'prisma')])
        self.assertEqual([c['name'] for c in merged['users']['columns']], ['id', 'email'])
        self.assertNotIn('schema_missing', merged['users'])  # has columns -> present

    def test_db_wins_physical_attributes_over_framework(self):
        # Django emits author_id bigint; DB has it as int — DB (physical) wins
        db = {'posts': {'columns': [col('author_id', type='integer', sql_type='int',
                                         nullable=False)], 'associations': []}}
        django = {'posts': {'columns': [col('author_id', type='bigint', nullable=True)],
                            'associations': []}}
        merged = erd.merge_ir([db_layer(db), fw_layer(django, 'django')])
        c = merged['posts']['columns'][0]
        self.assertEqual(c['type'], 'integer')     # DB physical
        self.assertEqual(c['sql_type'], 'int')     # DB-only attr retained
        self.assertFalse(c['nullable'])            # DB physical

    def test_config_wins_physical_over_db(self):
        db = {'t': {'columns': [col('x', type='integer')], 'associations': []}}
        cfg = {'t': {'columns': [col('x', type='uuid')], 'associations': []}}
        merged = erd.merge_ir([db_layer(db), config_layer(cfg)])
        self.assertEqual(merged['t']['columns'][0]['type'], 'uuid')

    def test_column_comment_is_logical_config_over_framework_over_db(self):
        db = {'t': {'columns': [col('x', type='int', comment='db note')], 'associations': []}}
        fw = {'t': {'columns': [col('x', comment='logical name')], 'associations': []}}
        cfg = {'t': {'columns': [col('x', comment='config note')], 'associations': []}}
        # framework beats db
        m1 = erd.merge_ir([db_layer(db), fw_layer(fw)])
        self.assertEqual(m1['t']['columns'][0]['comment'], 'logical name')
        self.assertEqual(m1['t']['columns'][0]['type'], 'int')  # physical still db
        # config beats both
        m2 = erd.merge_ir([db_layer(db), fw_layer(fw), config_layer(cfg)])
        self.assertEqual(m2['t']['columns'][0]['comment'], 'config note')

    def test_new_columns_appended_in_first_seen_order(self):
        db = {'t': {'columns': [col('a'), col('b')], 'associations': []}}
        cfg = {'t': {'columns': [col('b'), col('c'), col('a'), col('d')], 'associations': []}}
        merged = erd.merge_ir([db_layer(db), config_layer(cfg)])
        # a,b first-seen from db; c,d appended in config order
        self.assertEqual([c['name'] for c in merged['t']['columns']], ['a', 'b', 'c', 'd'])

    def test_present_only_absent_attr_does_not_override(self):
        # framework provides only nullable; db's type must survive
        db = {'t': {'columns': [col('x', type='integer', nullable=False)], 'associations': []}}
        fw = {'t': {'columns': [col('x', nullable=True)], 'associations': []}}
        merged = erd.merge_ir([db_layer(db), fw_layer(fw)])
        self.assertEqual(merged['t']['columns'][0]['type'], 'integer')
        # nullable: framework is lower physical authority than db, so db wins
        self.assertFalse(merged['t']['columns'][0]['nullable'])

    def test_multi_framework_later_wins_on_logical_tie(self):
        db = {'t': {'columns': [col('x', comment='db')], 'associations': []}}
        fw1 = {'t': {'columns': [col('x', comment='fw1')], 'associations': []}}
        fw2 = {'t': {'columns': [col('x', comment='fw2')], 'associations': []}}
        merged = erd.merge_ir([db_layer(db), fw_layer(fw1), fw_layer(fw2)])
        self.assertEqual(merged['t']['columns'][0]['comment'], 'fw2')  # later framework

    def test_deterministic_same_input_same_output(self):
        db = {'t': {'columns': [col('a', type='int', comment='c'), col('b')],
                    'primary_key': 'a', 'associations': []}}
        fw = {'t': {'columns': [col('b', comment='x')],
                    'associations': [{'type': 'has_many', 'name': 'us', 'target': 'u'}]}}
        out1 = erd.merge_ir([db_layer(db), fw_layer(fw)])
        out2 = erd.merge_ir([db_layer(db), fw_layer(fw)])
        self.assertEqual(out1, out2)
        # and key order within a column dict is stable
        self.assertEqual(list(out1['t']['columns'][0]), list(out2['t']['columns'][0]))

    def test_inputs_not_mutated(self):
        db = {'t': {'columns': [col('x', type='integer')],
                    'associations': [{'type': 'belongs_to', 'name': 'y', 'target': 'y',
                                      'foreign_key': 'y_id', 'db_fk': True}]}}
        cfg = {'t': {'columns': [col('x', type='uuid')], 'associations': []}}
        db_before, cfg_before = copy.deepcopy(db), copy.deepcopy(cfg)
        erd.merge_ir([db_layer(db), config_layer(cfg)])
        self.assertEqual(db, db_before)
        self.assertEqual(cfg, cfg_before)


# ---------------------------------------------------------------------------
# §16.3 — tables union, primary_key, indexes, comment, schema_missing
# ---------------------------------------------------------------------------
class TestTableLevelMerge(unittest.TestCase):
    def test_tables_are_the_union_first_seen_order(self):
        db = {'a': {'columns': [], 'associations': []},
              'b': {'columns': [], 'associations': []}}
        fw = {'b': {'associations': []}, 'c': {'associations': []}}
        merged = erd.merge_ir([db_layer(db), fw_layer(fw)])
        self.assertEqual(list(merged), ['a', 'b', 'c'])

    def test_primary_key_physical_authority_and_normalization(self):
        # db has no pk; config declares one -> config wins, and the named
        # column's primary flag is corrected to True
        db = {'t': {'columns': [col('id', type='bigint', primary=False)],
                    'primary_key': None, 'associations': []}}
        cfg = {'t': {'primary_key': 'id', 'associations': []}}
        merged = erd.merge_ir([db_layer(db), config_layer(cfg)])
        self.assertEqual(merged['t']['primary_key'], 'id')
        self.assertTrue(merged['t']['columns'][0]['primary'])

    def test_composite_pk_columns_keep_primary_flags(self):
        # DB stores primary_key as the FIRST pri column only, but flags both;
        # normalization must not flip the second one off
        db = {'order_items': {
            'columns': [col('order_id', type='bigint', primary=True),
                        col('item_id', type='bigint', primary=True)],
            'primary_key': 'order_id', 'associations': []}}
        merged = erd.merge_ir([db_layer(db)])
        prim = {c['name']: c['primary'] for c in merged['order_items']['columns']}
        self.assertEqual(prim, {'order_id': True, 'item_id': True})

    def test_primary_key_list_composite_from_config(self):
        db = {'order_items': {'columns': [col('order_id'), col('item_id')],
                              'associations': []}}
        cfg = {'order_items': {'primary_key': ['order_id', 'item_id'], 'associations': []}}
        merged = erd.merge_ir([db_layer(db), config_layer(cfg)])
        self.assertEqual(merged['order_items']['primary_key'], ['order_id', 'item_id'])
        self.assertTrue(all(c['primary'] for c in merged['order_items']['columns']))

    def test_primary_key_naming_missing_column_warns_not_fatal(self):
        db = {'t': {'columns': [col('id')], 'primary_key': 'ghost', 'associations': []}}
        # should not raise; ghost simply isn't a column
        merged = erd.merge_ir([db_layer(db)])
        self.assertEqual(merged['t']['primary_key'], 'ghost')

    def test_index_config_replaces_same_named_index_whole(self):
        db = {'t': {'columns': [], 'indexes': [
            {'name': 'idx_x', 'columns': ['x'], 'unique': False}], 'associations': []}}
        cfg = {'t': {'indexes': [
            {'name': 'idx_x', 'columns': ['x'], 'unique': True}], 'associations': []}}
        merged = erd.merge_ir([db_layer(db), config_layer(cfg)])
        self.assertEqual(merged['t']['indexes'],
                         [{'name': 'idx_x', 'columns': ['x'], 'unique': True}])  # config won whole

    def test_index_union_across_keys(self):
        db = {'t': {'columns': [], 'indexes': [
            {'name': 'a', 'columns': ['x'], 'unique': False}], 'associations': []}}
        cfg = {'t': {'indexes': [
            {'name': 'b', 'columns': ['y'], 'unique': True}], 'associations': []}}
        merged = erd.merge_ir([db_layer(db), config_layer(cfg)])
        self.assertEqual([i['name'] for i in merged['t']['indexes']], ['a', 'b'])

    def test_table_comment_config_over_framework_over_db(self):
        db = {'t': {'columns': [col('x')], 'comment': 'db', 'associations': []}}
        fw = {'t': {'comment': 'fw', 'associations': []}}
        cfg = {'t': {'comment': 'cfg', 'associations': []}}
        self.assertEqual(erd.merge_ir([db_layer(db), fw_layer(fw)])['t']['comment'], 'fw')
        self.assertEqual(erd.merge_ir([db_layer(db), fw_layer(fw), config_layer(cfg)])['t']['comment'], 'cfg')

    def test_comment_explicit_empty_deletes(self):
        db = {'t': {'columns': [col('x')], 'comment': 'db', 'associations': []}}
        cfg = {'t': {'comment': '', 'associations': []}}  # explicit "" = delete
        merged = erd.merge_ir([db_layer(db), config_layer(cfg)])
        self.assertNotIn('comment', merged['t'])

    def test_schema_missing_derived_zero_columns(self):
        # Rails-only (no columns) -> schema_missing; Prisma-only (columns) -> not
        rails = {'ghost': {'associations': [
            {'type': 'belongs_to', 'name': 'u', 'target': 'users', 'foreign_key': 'u_id'}]}}
        prisma = {'real': {'columns': [col('id', primary=True)], 'associations': []}}
        merged = erd.merge_ir([fw_layer(rails), fw_layer(prisma, 'prisma')])
        self.assertTrue(merged['ghost']['schema_missing'])
        self.assertNotIn('schema_missing', merged['real'])


# ---------------------------------------------------------------------------
# §16.4 — associations (identity, cardinality, provenance, fk_columns)
# ---------------------------------------------------------------------------
class TestAssociationMerge(unittest.TestCase):
    def test_created_by_and_updated_by_not_merged(self):
        # two owner_fk to the same target but different FK columns -> distinct
        db = {'posts': {'columns': [col('created_by_id'), col('updated_by_id')],
                        'associations': [
            {'type': 'belongs_to', 'name': 'c', 'target': 'users',
             'foreign_key': 'created_by_id', 'db_fk': True},
            {'type': 'belongs_to', 'name': 'u', 'target': 'users',
             'foreign_key': 'updated_by_id', 'db_fk': True}]}}
        merged = erd.merge_ir([db_layer(db)])
        fks = sorted(a['foreign_key'] for a in merged['posts']['associations'])
        self.assertEqual(fks, ['created_by_id', 'updated_by_id'])

    def test_db_fk_dropped_when_code_renames_the_column(self):
        # db FK (machine name 'user') + code belongs_to (declared 'author'),
        # same column but different name -> distinct identities (name is in the
        # owner_fk identity now), so they DON'T merge in Phase A; Phase B
        # reconcile then drops the covered db_fk, leaving one declared 'author'.
        db = {'posts': {'columns': [col('user_id')], 'associations': [
            {'type': 'belongs_to', 'name': 'user', 'target': 'users',
             'foreign_key': 'user_id', 'db_fk': True}]}}
        rails = {'posts': {'associations': [
            {'type': 'belongs_to', 'name': 'author', 'target': 'users',
             'foreign_key': 'user_id'}]}}
        merged = erd.merge_ir([db_layer(db), fw_layer(rails)])
        self.assertEqual(len(merged['posts']['associations']), 1)
        a = merged['posts']['associations'][0]
        self.assertEqual(a['name'], 'author')          # the surviving code edge
        self.assertNotIn('db_fk', a)                   # db_fk dropped by reconcile

    def test_db_fk_merges_with_conventional_same_name_code(self):
        # db FK 'user' (from user_id) + a conventional belongs_to :user, same
        # name -> same identity -> merge in Phase A -> representative declared
        db = {'posts': {'columns': [col('user_id')], 'associations': [
            {'type': 'belongs_to', 'name': 'user', 'target': 'users',
             'foreign_key': 'user_id', 'db_fk': True}]}}
        rails = {'posts': {'associations': [
            {'type': 'belongs_to', 'name': 'user', 'target': 'users',
             'foreign_key': 'user_id'}]}}
        merged = erd.merge_ir([db_layer(db), fw_layer(rails)])
        self.assertEqual(len(merged['posts']['associations']), 1)
        self.assertNotIn('db_fk', merged['posts']['associations'][0])

    def test_two_code_names_same_column_both_kept(self):
        # the Rails alias pattern: belongs_to :user AND belongs_to :author,
        # both on user_id -> users. name is in the owner_fk identity, so these
        # stay two distinct associations (neither is a db_fk, so Phase B leaves
        # both). This is the regression the name-in-identity fix restores.
        db = {'users': {'columns': [col('id', primary=True)], 'associations': []},
              'posts': {'columns': [col('user_id')], 'associations': []}}
        rails = {'posts': {'associations': [
            {'type': 'belongs_to', 'name': 'user', 'target': 'users', 'foreign_key': 'user_id'},
            {'type': 'belongs_to', 'name': 'author', 'target': 'users', 'foreign_key': 'user_id'}]}}
        merged = erd.merge_ir([db_layer(db), fw_layer(rails)])
        self.assertEqual(sorted(a['name'] for a in merged['posts']['associations']),
                         ['author', 'user'])

    def test_has_one_1to1_preserved_when_merged_with_belongs_to(self):
        # DB unique-index FK resolved to has_one; code declares belongs_to.
        # Same identity -> merged, and the 1:1 (has_one) must survive.
        db = {'profiles': {'columns': [col('user_id')], 'associations': [
            {'type': 'has_one', 'name': 'user', 'target': 'users',
             'foreign_key': 'user_id', 'db_fk': True}]}}
        rails = {'profiles': {'associations': [
            {'type': 'belongs_to', 'name': 'user', 'target': 'users',
             'foreign_key': 'user_id'}]}}
        merged = erd.merge_ir([db_layer(db), fw_layer(rails)])
        self.assertEqual(len(merged['profiles']['associations']), 1)
        self.assertEqual(merged['profiles']['associations'][0]['type'], 'has_one')

    def test_belongs_to_upgraded_via_reconcile_across_tables(self):
        # profiles.belongs_to:account (db_fk has_one, own identity) + a covering
        # inverse on accounts. Here the covering explicit is on the OTHER table
        # (no fk) so identities don't merge in Phase A -> Phase B reconcile runs,
        # drops the db_fk and upgrades the lone belongs_to to has_one.
        db = {'accounts': {'columns': [col('id', primary=True)], 'associations': []},
              'profiles': {'columns': [col('account_id')], 'associations': [
                  {'type': 'has_one', 'name': 'account', 'target': 'accounts',
                   'foreign_key': 'account_id', 'db_fk': True}]}}
        code = {'profiles': {'associations': [
            {'type': 'belongs_to', 'name': 'account', 'target': 'accounts',
             'foreign_key': 'account_id'}]}}
        merged = erd.merge_ir([db_layer(db), fw_layer(code)])
        # both associations share identity (owner_fk same col) so they MERGE in
        # Phase A and has_one is preserved directly
        assoc = merged['profiles']['associations']
        self.assertEqual(len(assoc), 1)
        self.assertEqual(assoc[0]['type'], 'has_one')

    def test_has_many_and_belongs_to_and_inverse_all_kept(self):
        db = {'users': {'columns': [col('id', primary=True)], 'associations': []},
              'posts': {'columns': [col('user_id')], 'associations': [
                  {'type': 'belongs_to', 'name': 'user', 'target': 'users',
                   'foreign_key': 'user_id', 'db_fk': True}]}}
        code = {'users': {'associations': [
                    {'type': 'has_many', 'name': 'posts', 'target': 'posts'},
                    {'type': 'has_one', 'name': 'profile', 'target': 'profiles'}]},
                'posts': {'associations': [
                    {'type': 'belongs_to', 'name': 'author', 'target': 'users',
                     'foreign_key': 'user_id'}]}}
        merged = erd.merge_ir([db_layer(db), fw_layer(code)])
        users_assoc = {a['name']: a['type'] for a in merged['users']['associations']}
        self.assertEqual(users_assoc, {'posts': 'has_many', 'profile': 'has_one'})
        # posts: db_fk 'user' and code 'author' merge (same col) -> one declared
        self.assertEqual(len(merged['posts']['associations']), 1)
        self.assertEqual(merged['posts']['associations'][0]['name'], 'author')

    def test_through_and_polymorphic_and_self_reference(self):
        db = {'categories': {'columns': [col('id', primary=True), col('parent_id')],
                             'associations': [
            {'type': 'belongs_to', 'name': 'parent', 'target': 'categories',
             'foreign_key': 'parent_id', 'db_fk': True}]}}   # self-reference
        code = {'users': {'associations': [
                    {'type': 'has_many', 'name': 'commented_posts', 'target': 'posts',
                     'through': 'comments'}]},
                'comments': {'associations': [
                    {'type': 'belongs_to', 'name': 'subject', 'target': 'subjects',
                     'foreign_key': 'subject_id', 'polymorphic': True}]}}
        merged = erd.merge_ir([db_layer(db), fw_layer(code)])
        self.assertEqual(merged['categories']['associations'][0]['target'], 'categories')
        through = merged['users']['associations'][0]
        self.assertEqual(through['through'], 'comments')
        poly = merged['comments']['associations'][0]
        self.assertTrue(poly['polymorphic'])

    def test_config_association_overrides_framework(self):
        # config assoc with the SAME identity (target+fk+name) as a framework
        # one: config wins provenance (manual) and cardinality per §8.6/§8.4.
        # A shared name is what makes it an override (vs. a separate edge) now
        # that name is part of the owner_fk identity.
        fw = {'orders': {'columns': [col('created_by_id')], 'associations': [
            {'type': 'belongs_to', 'name': 'creator', 'target': 'users',
             'foreign_key': 'created_by_id'}]}}
        cfg = {'orders': {'associations': [
            {'type': 'has_one', 'name': 'creator', 'target': 'users',
             'foreign_key': 'created_by_id', 'manual': True}]}}
        merged = erd.merge_ir([fw_layer(fw), config_layer(cfg)])
        assoc = merged['orders']['associations']
        self.assertEqual(len(assoc), 1)
        self.assertEqual(assoc[0]['name'], 'creator')
        self.assertEqual(assoc[0]['type'], 'has_one')       # config cardinality wins
        self.assertEqual(assoc[0]['provenance'], 'manual')  # manual provenance wins
        # sources union: both the framework and config layers contributed
        self.assertEqual(assoc[0]['sources'],
                         [{'kind': 'config', 'provider': 'config'},
                          {'kind': 'framework', 'provider': 'rails'}])

    def test_config_association_with_different_name_is_a_separate_edge(self):
        # a config association that RENAMES (different name, same column) is a
        # distinct identity now -> it's an ADD, not an override; both are kept.
        # (Renaming/overriding across names is done via drop+add in Step 7.)
        fw = {'orders': {'columns': [col('created_by_id')], 'associations': [
            {'type': 'belongs_to', 'name': 'creator', 'target': 'users',
             'foreign_key': 'created_by_id'}]}}
        cfg = {'orders': {'associations': [
            {'type': 'belongs_to', 'name': 'placed_by', 'target': 'users',
             'foreign_key': 'created_by_id', 'manual': True}]}}
        merged = erd.merge_ir([fw_layer(fw), config_layer(cfg)])
        self.assertEqual(sorted(a['name'] for a in merged['orders']['associations']),
                         ['creator', 'placed_by'])

    def test_config_kind_forces_manual_even_without_flag(self):
        # a config-layer association carrying no explicit flag is still manual
        # by definition (§9.1)
        cfg = {'t': {'columns': [col('x_id')], 'associations': [
            {'type': 'belongs_to', 'name': 'x', 'target': 'xs', 'foreign_key': 'x_id'}]}}
        merged = erd.merge_ir([config_layer(cfg)])
        a = merged['t']['associations'][0]
        self.assertEqual(a['provenance'], 'manual')
        self.assertEqual(a['sources'], [{'kind': 'config', 'provider': 'config'}])
        # the merged IR carries NO legacy booleans
        self.assertNotIn('manual', a)

    def test_provenance_representative_precedence(self):
        # manual + db_fk on the same identity -> manual wins; sources unions both
        db = {'t': {'columns': [col('x_id')], 'associations': [
            {'type': 'belongs_to', 'name': 'x', 'target': 'xs',
             'foreign_key': 'x_id', 'db_fk': True}]}}
        cfg = {'t': {'associations': [
            {'type': 'belongs_to', 'name': 'x', 'target': 'xs',
             'foreign_key': 'x_id', 'manual': True}]}}
        merged = erd.merge_ir([db_layer(db), config_layer(cfg)])
        a = merged['t']['associations'][0]
        self.assertEqual(a['provenance'], 'manual')
        self.assertEqual(a['sources'], [{'kind': 'config', 'provider': 'config'},
                                        {'kind': 'db', 'provider': 'mysql'}])
        self.assertNotIn('manual', a)
        self.assertNotIn('db_fk', a)

    def test_fk_columns_recomputed_from_final_associations(self):
        db = {'posts': {'columns': [col('user_id'), col('editor_id')], 'associations': [
            {'type': 'belongs_to', 'name': 'user', 'target': 'users',
             'foreign_key': 'user_id', 'db_fk': True}]}}
        code = {'posts': {'associations': [
            {'type': 'belongs_to', 'name': 'editor', 'target': 'users',
             'foreign_key': 'editor_id'}]}}
        merged = erd.merge_ir([db_layer(db), fw_layer(code)])
        self.assertEqual(merged['posts']['fk_columns'], ['editor_id', 'user_id'])

    def test_fk_columns_ignores_input_value(self):
        # a bogus input fk_columns must be discarded and recomputed
        db = {'t': {'columns': [col('x_id')], 'fk_columns': ['bogus'], 'associations': [
            {'type': 'belongs_to', 'name': 'x', 'target': 'xs',
             'foreign_key': 'x_id', 'db_fk': True}]},
              'xs': {'columns': [col('id', primary=True)], 'associations': []}}
        merged = erd.merge_ir([db_layer(db)])
        self.assertEqual(merged['t']['fk_columns'], ['x_id'])


# ---------------------------------------------------------------------------
# reconcile_db_fks (Phase B) — expected output pinned directly on four shapes
# (same-column coverage, has_one upgrade, column-aware two-FK, self-reference).
# ---------------------------------------------------------------------------
class TestReconcileDbFks(unittest.TestCase):
    def test_a_db_fk_covered_by_same_column_belongs_to_is_dropped(self):
        shape = {'posts': {'associations': [
                    {'type': 'belongs_to', 'name': 'user', 'target': 'users',
                     'foreign_key': 'user_id', 'db_fk': True},
                    {'type': 'belongs_to', 'name': 'author', 'target': 'users',
                     'foreign_key': 'user_id'}]},
                 'users': {'associations': []}}
        removed = erd.reconcile_db_fks(shape)
        self.assertEqual(removed, 1)
        self.assertEqual(shape['posts']['associations'], [
            {'type': 'belongs_to', 'name': 'author', 'target': 'users',
             'foreign_key': 'user_id'}])

    def test_b_has_one_db_fk_upgrades_lone_covering_belongs_to(self):
        shape = {'profiles': {'associations': [
                    {'type': 'has_one', 'name': 'account', 'target': 'accounts',
                     'foreign_key': 'account_id', 'db_fk': True},
                    {'type': 'belongs_to', 'name': 'account', 'target': 'accounts',
                     'foreign_key': 'account_id'}]},
                 'accounts': {'associations': []}}
        removed = erd.reconcile_db_fks(shape)
        self.assertEqual(removed, 1)
        # the surviving belongs_to is promoted to has_one in place (the DB's
        # 1:1 signal is preserved rather than silently discarded)
        self.assertEqual(shape['profiles']['associations'], [
            {'type': 'has_one', 'name': 'account', 'target': 'accounts',
             'foreign_key': 'account_id'}])

    def test_c_column_aware_only_the_covered_fk_is_dropped(self):
        shape = {'posts': {'associations': [
                    {'type': 'belongs_to', 'name': 'a', 'target': 'users',
                     'foreign_key': 'author_id', 'db_fk': True},
                    {'type': 'belongs_to', 'name': 'e', 'target': 'users',
                     'foreign_key': 'editor_id', 'db_fk': True},
                    {'type': 'belongs_to', 'name': 'author', 'target': 'users',
                     'foreign_key': 'author_id'}]},
                 'users': {'associations': []}}
        removed = erd.reconcile_db_fks(shape)
        self.assertEqual(removed, 1)  # only the author_id DB FK
        self.assertEqual(shape['posts']['associations'], [
            {'type': 'belongs_to', 'name': 'e', 'target': 'users',
             'foreign_key': 'editor_id', 'db_fk': True},   # editor_id DB FK survives
            {'type': 'belongs_to', 'name': 'author', 'target': 'users',
             'foreign_key': 'author_id'}])

    def test_d_self_reference(self):
        shape = {'categories': {'associations': [
                    {'type': 'belongs_to', 'name': 'parent', 'target': 'categories',
                     'foreign_key': 'parent_id', 'db_fk': True},
                    {'type': 'belongs_to', 'name': 'parent', 'target': 'categories',
                     'foreign_key': 'parent_id'}]}}
        removed = erd.reconcile_db_fks(shape)
        self.assertEqual(removed, 1)
        self.assertEqual(shape['categories']['associations'], [
            {'type': 'belongs_to', 'name': 'parent', 'target': 'categories',
             'foreign_key': 'parent_id'}])


# ---------------------------------------------------------------------------
# §8.6 (P0-3) — config override content-diff warning
# ---------------------------------------------------------------------------
class TestConfigOverrideWarning(unittest.TestCase):
    def _merge_capturing_stderr(self, layers):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            merged = erd.merge_ir(layers)
        return merged, buf.getvalue()

    def test_warns_when_config_overrides_differing_cardinality(self):
        # framework belongs_to vs config has_one on the same identity (target+fk)
        fw = {'t': {'columns': [col('u_id')], 'associations': [
            {'type': 'belongs_to', 'name': 'u', 'target': 'us', 'foreign_key': 'u_id'}]}}
        cfg = {'t': {'associations': [
            {'type': 'has_one', 'name': 'u', 'target': 'us', 'foreign_key': 'u_id'}]}}
        _, err = self._merge_capturing_stderr([fw_layer(fw), config_layer(cfg)])
        self.assertIn('overrides a differing', err)
        self.assertIn('framework', err)

    def test_no_warning_and_no_override_when_name_differs(self):
        # a config association with a DIFFERENT name on the same column is a
        # separate identity (not an override), so nothing is overridden and no
        # warning fires — both associations are kept.
        fw = {'t': {'columns': [col('u_id')], 'associations': [
            {'type': 'belongs_to', 'name': 'user', 'target': 'us', 'foreign_key': 'u_id'}]}}
        cfg = {'t': {'associations': [
            {'type': 'belongs_to', 'name': 'owner', 'target': 'us', 'foreign_key': 'u_id'}]}}
        merged, err = self._merge_capturing_stderr([fw_layer(fw), config_layer(cfg)])
        self.assertNotIn('overrides a differing', err)
        self.assertEqual(sorted(a['name'] for a in merged['t']['associations']),
                         ['owner', 'user'])

    def test_no_warning_when_content_identical(self):
        fw = {'t': {'columns': [col('u_id')], 'associations': [
            {'type': 'belongs_to', 'name': 'u', 'target': 'us', 'foreign_key': 'u_id'}]}}
        cfg = {'t': {'associations': [
            {'type': 'belongs_to', 'name': 'u', 'target': 'us', 'foreign_key': 'u_id'}]}}
        _, err = self._merge_capturing_stderr([fw_layer(fw), config_layer(cfg)])
        self.assertNotIn('overrides a differing', err)

    def test_framework_vs_framework_override_warns_and_later_wins(self):
        # P1-a (§10): two --models. A later framework layer overriding an
        # earlier one's same-identity association with a DIFFERING cardinality
        # warns, and the later layer's value wins.
        fw1 = {'t': {'columns': [col('u_id')], 'associations': [
            {'type': 'belongs_to', 'name': 'u', 'target': 'us', 'foreign_key': 'u_id'}]}}
        fw2 = {'t': {'associations': [
            {'type': 'has_one', 'name': 'u', 'target': 'us', 'foreign_key': 'u_id'}]}}
        merged, err = self._merge_capturing_stderr(
            [fw_layer(fw1, 'rails'), fw_layer(fw2, 'prisma')])
        self.assertIn('overrides a differing earlier framework', err)
        a = next(a for a in merged['t']['associations'] if a['name'] == 'u')
        self.assertEqual(a['type'], 'has_one')  # later framework wins

    def test_framework_vs_framework_no_warning_when_identical(self):
        fw1 = {'t': {'associations': [
            {'type': 'belongs_to', 'name': 'u', 'target': 'us', 'foreign_key': 'u_id'}]}}
        fw2 = {'t': {'associations': [
            {'type': 'belongs_to', 'name': 'u', 'target': 'us', 'foreign_key': 'u_id'}]}}
        _, err = self._merge_capturing_stderr(
            [fw_layer(fw1, 'rails'), fw_layer(fw2, 'prisma')])
        self.assertNotIn('framework association', err)


class TestSameRankFieldConflictWarning(unittest.TestCase):
    """§7/§10: two SAME-priority layers (e.g. two --models frameworks) disagreeing
    on a field's value is a silent last-wins — warn, don't hide it. A lower-rank
    layer losing to a higher one is the authority ladder and must NOT warn."""
    def _merge_capturing_stderr(self, layers):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            merged = erd.merge_ir(layers)
        return merged, buf.getvalue()

    def test_two_frameworks_conflicting_column_type_warns_and_later_wins(self):
        prisma = {'t': {'columns': [col('x', type='integer')], 'associations': []}}
        django = {'t': {'columns': [col('x', type='uuid')], 'associations': []}}
        merged, err = self._merge_capturing_stderr(
            [fw_layer(prisma, 'prisma'), fw_layer(django, 'django')])
        self.assertIn('t.x.type', err)
        self.assertIn('conflicting values', err)
        self.assertIn('uuid', err)  # the winner (later layer)
        x = next(c for c in merged['t']['columns'] if c['name'] == 'x')
        self.assertEqual(x['type'], 'uuid')

    def test_db_vs_framework_different_rank_does_not_warn(self):
        # DB and framework have DIFFERENT ranks, so DB's physical type winning
        # is the ladder working — no warning, even though the values differ.
        db = {'t': {'columns': [col('x', type='integer')], 'associations': []}}
        fw = {'t': {'columns': [col('x', type='uuid')], 'associations': []}}
        merged, err = self._merge_capturing_stderr([db_layer(db), fw_layer(fw)])
        self.assertNotIn('conflicting values', err)
        x = next(c for c in merged['t']['columns'] if c['name'] == 'x')
        self.assertEqual(x['type'], 'integer')  # DB wins physical

    def test_two_frameworks_same_value_no_warning(self):
        prisma = {'t': {'columns': [col('x', type='integer')], 'associations': []}}
        django = {'t': {'columns': [col('x', type='integer')], 'associations': []}}
        _, err = self._merge_capturing_stderr(
            [fw_layer(prisma, 'prisma'), fw_layer(django, 'django')])
        self.assertNotIn('conflicting values', err)

    def test_two_frameworks_conflicting_primary_key_warns(self):
        fw1 = {'t': {'columns': [col('a')], 'associations': [], 'primary_key': 'a'}}
        fw2 = {'t': {'columns': [col('a')], 'associations': [], 'primary_key': 'b'}}
        _, err = self._merge_capturing_stderr(
            [fw_layer(fw1, 'prisma'), fw_layer(fw2, 'django')])
        self.assertIn('t.primary_key', err)
        self.assertIn('conflicting values', err)


class TestPrimaryKeyAuthoritative(unittest.TestCase):
    """P1-c: a config layer's primary_key is authoritative/COMPLETE — it resets
    other columns' `primary` flags. A DB/framework PK only ever sets True (safe
    for composite PKs the DB reports partially)."""

    def test_config_null_pk_clears_stale_db_primary(self):
        db = {'t': {'columns': [col('id', type='bigint', primary=True),
                                col('name', type='string')],
                    'primary_key': 'id', 'associations': []}}
        cfg = {'t': {'primary_key': None}}
        merged = erd.merge_ir([db_layer(db), config_layer(cfg)])
        self.assertIsNone(merged['t']['primary_key'])
        self.assertFalse(any(c['primary'] for c in merged['t']['columns']))

    def test_config_narrower_pk_over_db_composite_resets_the_rest(self):
        # DB composite PK (a, b) flags both a and b primary; config narrows it
        # to just [a] -> only a stays primary, b is reset to False.
        db = {'t': {'columns': [col('a', type='bigint', primary=True),
                                col('b', type='bigint', primary=True),
                                col('c', type='string')],
                    'primary_key': 'a', 'associations': []}}
        cfg = {'t': {'primary_key': ['a']}}
        merged = erd.merge_ir([db_layer(db), config_layer(cfg)])
        prim = {c['name']: c['primary'] for c in merged['t']['columns']}
        self.assertEqual(prim, {'a': True, 'b': False, 'c': False})

    def test_plain_db_composite_pk_keeps_all_members_primary(self):
        # no config: the existing composite-PK-safe behavior is preserved
        # (DB stores only the first PK col in primary_key but flags all members)
        db = {'t': {'columns': [col('a', type='bigint', primary=True),
                                col('b', type='bigint', primary=True)],
                    'primary_key': 'a', 'associations': []}}
        merged = erd.merge_ir([db_layer(db)])
        self.assertTrue(all(c['primary'] for c in merged['t']['columns']))


class TestConfigIndexUniqueNormalization(unittest.TestCase):
    """P1-b: a config index may omit `unique` (Step-3 accepts it as optional);
    merge_ir's derive step normalizes it to False so every downstream
    ix['unique'] read is safe."""

    def test_config_index_without_unique_defaults_to_false(self):
        cfg = {'t': {'columns': [col('id', type='bigint', primary=True)],
                     'primary_key': 'id',
                     'indexes': [{'name': 'idx_t_id', 'columns': ['id']}],  # no `unique`
                     'associations': []}}
        merged = erd.merge_ir([config_layer(cfg)])
        self.assertEqual(merged['t']['indexes'][0]['unique'], False)

    def test_write_excel_survives_config_index_without_unique(self):
        import tempfile, zipfile
        cfg = {'t': {'columns': [col('id', type='bigint', primary=True)],
                     'primary_key': 'id',
                     'indexes': [{'name': 'idx_t_id', 'columns': ['id']}],
                     'associations': []}}
        merged = erd.merge_ir([config_layer(cfg)])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'defs.xlsx'
            erd.write_excel(merged, out, 'testdb')  # must not raise KeyError
            with zipfile.ZipFile(out) as z:
                blob = ''.join(z.read(n).decode() for n in z.namelist()
                               if n.startswith('xl/worksheets/'))
        self.assertIn('idx_t_id', blob)


class TestProvenanceAndSources(unittest.TestCase):
    """Step 10 (§9.1): the merged IR carries structured `provenance` + `sources`
    on associations instead of the legacy db_fk/manual/inferred booleans."""

    def test_db_fk_plus_rails_declared_is_declared_with_both_sources(self):
        # a DB FK ALSO declared in Rails on the same identity: provenance is
        # 'declared' (declared beats db_fk), sources unions {db,mysql} + {fw,rails}
        db = {'posts': {'columns': [col('user_id')], 'associations': [
            {'type': 'belongs_to', 'name': 'user', 'target': 'users',
             'foreign_key': 'user_id', 'db_fk': True}]},
              'users': {'columns': [col('id', primary=True)], 'associations': []}}
        rails = {'posts': {'associations': [
            {'type': 'belongs_to', 'name': 'user', 'target': 'users',
             'foreign_key': 'user_id'}]}}
        merged = erd.merge_ir([db_layer(db), fw_layer(rails, 'rails')])
        a = next(x for x in merged['posts']['associations'] if x['name'] == 'user')
        self.assertEqual(a['provenance'], 'declared')
        self.assertEqual(a['sources'], [{'kind': 'db', 'provider': 'mysql'},
                                        {'kind': 'framework', 'provider': 'rails'}])
        for legacy in ('db_fk', 'manual', 'inferred'):
            self.assertNotIn(legacy, a)

    def test_lone_db_fk_keeps_db_fk_provenance_and_source(self):
        db = {'posts': {'columns': [col('user_id')], 'associations': [
            {'type': 'belongs_to', 'name': 'user', 'target': 'users',
             'foreign_key': 'user_id', 'db_fk': True}]},
              'users': {'columns': [col('id', primary=True)], 'associations': []}}
        a = erd.merge_ir([db_layer(db)])['posts']['associations'][0]
        self.assertEqual(a['provenance'], 'db_fk')
        self.assertEqual(a['sources'], [{'kind': 'db', 'provider': 'mysql'}])

    def test_manual_config_association_provenance(self):
        cfg = {'t': {'columns': [col('x_id')], 'associations': [
            {'type': 'belongs_to', 'name': 'x', 'target': 'xs', 'foreign_key': 'x_id'}]}}
        a = erd.merge_ir([config_layer(cfg)])['t']['associations'][0]
        self.assertEqual(a['provenance'], 'manual')
        self.assertEqual(a['sources'], [{'kind': 'config', 'provider': 'config'}])

    def test_serialize_converts_each_provenance_to_its_legacy_flag(self):
        # a table whose four associations carry the four provenances -> the
        # serialized (viewer) shape carries the matching legacy flags and NO
        # provenance/sources keys.
        tables = {'t': {'associations': [
            {'type': 'belongs_to', 'name': 'a', 'target': 'x', 'foreign_key': 'a_id',
             'provenance': 'db_fk', 'sources': [{'kind': 'db', 'provider': 'mysql'}]},
            {'type': 'belongs_to', 'name': 'b', 'target': 'x', 'foreign_key': 'b_id',
             'provenance': 'manual', 'sources': [{'kind': 'config', 'provider': 'config'}]},
            {'type': 'belongs_to', 'name': 'c', 'target': 'x', 'foreign_key': 'c_id',
             'provenance': 'inferred', 'sources': []},
            {'type': 'has_many', 'name': 'd', 'target': 'x',
             'provenance': 'declared', 'sources': [{'kind': 'framework', 'provider': 'rails'}]}]}}
        out = erd.serialize_for_viewer(tables)['t']['associations']
        self.assertEqual(out[0].get('db_fk'), True)
        self.assertEqual(out[1].get('manual'), True)
        self.assertEqual(out[2].get('inferred'), True)
        self.assertNotIn('db_fk', out[3])  # declared -> no badge
        self.assertNotIn('manual', out[3])
        for a in out:  # internal keys never leak into the viewer JSON
            self.assertNotIn('provenance', a)
            self.assertNotIn('sources', a)

    def test_serialize_passes_legacy_shape_through_unchanged(self):
        # demo shape: parser output already carries legacy flags and NO
        # provenance -> serialize is a byte-preserving pass-through (deep-copied)
        tables = {'t': {'associations': [
            {'type': 'belongs_to', 'name': 'u', 'target': 'users',
             'foreign_key': 'u_id', 'db_fk': True},
            {'type': 'has_many', 'name': 'posts', 'target': 'posts'}]}}  # bare -> declared
        out = erd.serialize_for_viewer(tables)
        self.assertEqual(out, tables)
        self.assertIsNot(out['t']['associations'][0], tables['t']['associations'][0])

    def test_serialize_is_pure(self):
        tables = {'t': {'associations': [
            {'type': 'belongs_to', 'name': 'x', 'target': 'xs', 'foreign_key': 'x_id',
             'provenance': 'manual', 'sources': [{'kind': 'config', 'provider': 'config'}]}]}}
        erd.serialize_for_viewer(tables)
        self.assertEqual(tables['t']['associations'][0]['provenance'], 'manual')  # input intact


# ---------------------------------------------------------------------------
# §6.2 — config operation markers: drop / *_mode: replace (Step 7a)
# ---------------------------------------------------------------------------
class TestConfigDropAndReplace(unittest.TestCase):
    def _base(self):
        # a db + framework base for the config ops to act on
        db = {'users': {'columns': [col('id', type='bigint', primary=True),
                                    col('email', type='string'),
                                    col('legacy', type='string')],
                        'indexes': [{'name': 'idx_email', 'columns': ['email'], 'unique': True},
                                    {'name': 'idx_legacy', 'columns': ['legacy'], 'unique': False}],
                        'primary_key': 'id',
                        'associations': [
                            {'type': 'belongs_to', 'name': 'company', 'target': 'companies',
                             'foreign_key': 'company_id', 'db_fk': True}]},
              'companies': {'columns': [col('id', primary=True)], 'associations': []},
              'temp_scratch': {'columns': [col('id', primary=True)], 'associations': []}}
        return db

    # ── table drop ──
    def test_table_drop_removes_table(self):
        db = self._base()
        cfg = {'temp_scratch': {'drop': True}}
        merged = erd.merge_ir([db_layer(db), config_layer(cfg)])
        self.assertNotIn('temp_scratch', merged)
        self.assertIn('users', merged)

    def test_table_drop_of_absent_table_is_noop(self):
        cfg = {'ghost': {'drop': True}}
        merged = erd.merge_ir([db_layer(self._base()), config_layer(cfg)])
        self.assertNotIn('ghost', merged)  # simply absent; no error (7b adds it)

    # ── column drop ──
    def test_column_drop_removes_column(self):
        cfg = {'users': {'columns': [{'name': 'legacy', 'drop': True}]}}
        merged = erd.merge_ir([db_layer(self._base()), config_layer(cfg)])
        names = [c['name'] for c in merged['users']['columns']]
        self.assertEqual(names, ['id', 'email'])  # legacy gone, order preserved

    def test_column_drop_of_absent_column_is_noop(self):
        cfg = {'users': {'columns': [{'name': 'nope', 'drop': True}]}}
        merged = erd.merge_ir([db_layer(self._base()), config_layer(cfg)])
        self.assertEqual([c['name'] for c in merged['users']['columns']],
                         ['id', 'email', 'legacy'])

    def test_op_markers_absent_from_output(self):
        cfg = {'users': {'columns': [{'name': 'legacy', 'drop': True},
                                     {'name': 'email', 'comment': 'the email'}]}}
        merged = erd.merge_ir([db_layer(self._base()), config_layer(cfg)])
        for c in merged['users']['columns']:
            self.assertNotIn('drop', c)
        # and the override still applied
        email = next(c for c in merged['users']['columns'] if c['name'] == 'email')
        self.assertEqual(email['comment'], 'the email')

    # ── index drop ──
    def test_index_drop_removes_index(self):
        cfg = {'users': {'indexes': [{'name': 'idx_legacy', 'drop': True}]}}
        merged = erd.merge_ir([db_layer(self._base()), config_layer(cfg)])
        self.assertEqual([i['name'] for i in merged['users']['indexes']], ['idx_email'])

    # ── association drop ──
    def test_assoc_drop_owner_fk_by_column_no_name(self):
        # drop every owner_fk on company_id -> companies, without naming it
        cfg = {'users': {'associations': [
            {'target': 'companies', 'foreign_key': 'company_id', 'drop': True}]}}
        merged = erd.merge_ir([db_layer(self._base()), config_layer(cfg)])
        self.assertEqual(merged['users']['associations'], [])

    def test_assoc_drop_owner_fk_by_column_and_name(self):
        # two aliases on the same column; drop only the one named 'author'
        db = {'users': {'columns': [col('id', primary=True)], 'associations': []},
              'posts': {'columns': [col('user_id')], 'associations': [
                  {'type': 'belongs_to', 'name': 'user', 'target': 'users', 'foreign_key': 'user_id'},
                  {'type': 'belongs_to', 'name': 'author', 'target': 'users', 'foreign_key': 'user_id'}]}}
        cfg = {'posts': {'associations': [
            {'target': 'users', 'foreign_key': 'user_id', 'name': 'author', 'drop': True}]}}
        merged = erd.merge_ir([db_layer(db), config_layer(cfg)])
        self.assertEqual([a['name'] for a in merged['posts']['associations']], ['user'])

    def test_assoc_drop_collection_by_type_target_name(self):
        db = {'users': {'columns': [col('id', primary=True)], 'associations': [
                  {'type': 'has_many', 'name': 'posts', 'target': 'posts'},
                  {'type': 'has_many', 'name': 'edited_posts', 'target': 'posts'}]}}
        cfg = {'users': {'associations': [
            {'type': 'has_many', 'name': 'edited_posts', 'target': 'posts', 'drop': True}]}}
        merged = erd.merge_ir([db_layer(db), config_layer(cfg)])
        self.assertEqual([a['name'] for a in merged['users']['associations']], ['posts'])

    def test_assoc_drop_of_absent_is_noop(self):
        cfg = {'users': {'associations': [
            {'target': 'nowhere', 'foreign_key': 'x_id', 'drop': True}]}}
        merged = erd.merge_ir([db_layer(self._base()), config_layer(cfg)])
        self.assertEqual(len(merged['users']['associations']), 1)  # company edge intact

    def test_assoc_drop_then_add_different_target(self):
        # §6.8 pattern: drop the wrong owner_fk edge, add the correct one on the
        # same column to a different target
        db = {'orders': {'columns': [col('created_by_id')], 'associations': [
                  {'type': 'belongs_to', 'name': 'creator', 'target': 'users',
                   'foreign_key': 'created_by_id', 'db_fk': True}]},
              'users': {'columns': [col('id', primary=True)], 'associations': []},
              'staff': {'columns': [col('id', primary=True)], 'associations': []}}
        cfg = {'orders': {'associations': [
            {'target': 'users', 'foreign_key': 'created_by_id', 'drop': True},
            {'type': 'belongs_to', 'name': 'creator', 'target': 'staff',
             'foreign_key': 'created_by_id'}]}}
        merged = erd.merge_ir([db_layer(db), config_layer(cfg)])
        assoc = merged['orders']['associations']
        self.assertEqual(len(assoc), 1)
        self.assertEqual(assoc[0]['target'], 'staff')
        self.assertEqual(assoc[0]['provenance'], 'manual')

    # ── *_mode: replace ──
    def test_columns_mode_replace_discards_lower_layers(self):
        cfg = {'users': {'columns_mode': 'replace',
                         'columns': [col('id', type='bigint', primary=True),
                                     col('handle', type='string')]}}
        merged = erd.merge_ir([db_layer(self._base()), config_layer(cfg)])
        self.assertEqual([c['name'] for c in merged['users']['columns']], ['id', 'handle'])

    def test_columns_mode_replace_empty_list_clears_columns(self):
        # columns: [] + replace -> all columns removed (§6.3); schema_missing derives
        cfg = {'companies': {'columns_mode': 'replace', 'columns': []}}
        merged = erd.merge_ir([db_layer(self._base()), config_layer(cfg)])
        self.assertEqual(merged['companies']['columns'], [])
        self.assertTrue(merged['companies']['schema_missing'])

    def test_indexes_mode_replace(self):
        cfg = {'users': {'indexes_mode': 'replace',
                         'indexes': [{'name': 'idx_new', 'columns': ['id'], 'unique': True}]}}
        merged = erd.merge_ir([db_layer(self._base()), config_layer(cfg)])
        self.assertEqual([i['name'] for i in merged['users']['indexes']], ['idx_new'])

    def test_associations_mode_replace(self):
        cfg = {'users': {'associations_mode': 'replace', 'associations': [
            {'type': 'has_many', 'name': 'invoices', 'target': 'invoices'}]}}
        merged = erd.merge_ir([db_layer(self._base()), config_layer(cfg)])
        self.assertEqual([a['name'] for a in merged['users']['associations']], ['invoices'])

    def test_replace_takes_precedence_over_drop(self):
        # a drop under replace is a no-op: nothing lower is left to drop, and the
        # config's own list is what survives
        cfg = {'users': {'columns_mode': 'replace',
                         'columns': [col('id', primary=True),
                                     {'name': 'legacy', 'drop': True}]}}
        merged = erd.merge_ir([db_layer(self._base()), config_layer(cfg)])
        self.assertEqual([c['name'] for c in merged['users']['columns']], ['id'])

    # ── determinism & purity ──
    def test_ops_deterministic_and_inputs_unmutated(self):
        db = self._base()
        cfg = {'temp_scratch': {'drop': True},
               'users': {'columns': [{'name': 'legacy', 'drop': True}],
                         'indexes': [{'name': 'idx_legacy', 'drop': True}],
                         'associations': [{'target': 'companies', 'foreign_key': 'company_id',
                                           'drop': True}]}}
        db_before, cfg_before = copy.deepcopy(db), copy.deepcopy(cfg)
        out1 = erd.merge_ir([db_layer(db), config_layer(cfg)])
        out2 = erd.merge_ir([db_layer(db), config_layer(cfg)])
        self.assertEqual(out1, out2)
        self.assertEqual(db, db_before)   # inputs not mutated
        self.assertEqual(cfg, cfg_before)


if __name__ == '__main__':
    unittest.main()
