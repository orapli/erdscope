"""End-to-end tests for the rewired main() pipeline — REFACTOR_PLAN.md Step 6b.

main() now collects ProviderResults (db_provider / framework_provider /
relations_to_config_layer) and runs them through merge_ir + reconcile_db_fks
(inside merge_ir). These drive main() with a
stubbed parse_mysql (same technique as test_erd.TestMainConfigIntegration) and
assert the new flow's behavior, including the two plan-sanctioned changes:
Prisma/Django framework columns are now retained, and config `relations`
override (not skip) a conflicting framework/db association.

Run from the repository root:
    python3 -m unittest tests.test_pipeline -v
"""
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_RAILS = Path(__file__).resolve().parent / 'fixture_app'
FIXTURE_PRISMA = Path(__file__).resolve().parent / 'fixture_prisma'
FIXTURE_DJANGO = Path(__file__).resolve().parent / 'fixture_django'

spec = importlib.util.spec_from_file_location('erd', ROOT / 'erd.py')
erd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(erd)


def _col(t, name, dtype, ctype, null='YES', key='', default='', extra='', comment=''):
    return (t, name, dtype, ctype, null, key, default, extra, comment)


# A small DB fixture: users + posts (posts.user_id FK), plus a table Prisma
# knows nothing about, so overlays are visible.
TABLE_ROWS = [('users', ''), ('posts', '')]
COL_ROWS = [
    _col('users', 'id', 'bigint', 'bigint', 'NO', 'PRI', '', 'auto_increment'),
    _col('users', 'email', 'varchar', 'varchar(255)', 'NO', 'UNI'),
    _col('posts', 'id', 'bigint', 'bigint', 'NO', 'PRI', '', 'auto_increment'),
    _col('posts', 'user_id', 'bigint', 'bigint', 'NO', 'MUL'),
    _col('posts', 'created_by_id', 'bigint', 'bigint'),
]
FK_ROWS = [('posts', 'user_id', 'users')]
INDEX_ROWS = [('users', 'PRIMARY', 0, 1, 'id')]


class _MainDriver(unittest.TestCase):
    """Drive main() with a stubbed parse_mysql and read back the DATA payload."""
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

    def _run(self, *cli_args, capture_stderr=False):
        sys.argv = ['erd.py', 'mysql://x@localhost/testdb', *cli_args]
        if capture_stderr:
            buf = io.StringIO()
            with redirect_stderr(buf):
                erd.main()
            return buf.getvalue()
        erd.main()

    def _data(self, filename='erd.html'):
        html = (Path(self.tmp.name) / filename).read_text()
        return json.loads(re.search(r'const DATA = (\{.*?\});\s*\n', html).group(1))


class TestPipelineDBOnly(_MainDriver):
    def test_db_only_matches_direct_mysql_ir(self):
        # no overlays: the pipeline output equals the plain DB IR run through
        # merge_ir (identity merge is a no-op here besides fk_columns/schema_missing)
        self._run()
        data = self._data()
        self.assertEqual(set(data['tables']), {'users', 'posts'})
        posts = data['tables']['posts']
        self.assertEqual([c['name'] for c in posts['columns']], ['id', 'user_id', 'created_by_id'])
        # the DB FK is the sole association, still flagged db_fk (nothing covers it)
        self.assertEqual(len(posts['associations']), 1)
        self.assertTrue(posts['associations'][0]['db_fk'])
        self.assertEqual(posts['fk_columns'], ['user_id'])


class TestPipelineDBPlusRails(_MainDriver):
    def test_db_plus_rails_matches_direct_merge_ir(self):
        # Drive DB+Rails through main(), and independently compute the same merge
        # directly with the public building blocks main uses (db provider +
        # framework_provider folded by merge_ir). main must wire them so the
        # serialized IR equals the direct merge for the ENTIRE fixture — including
        # posts, where the Rails alias pattern (:user and :author, both on
        # user_id) keeps both associations now that `name` is part of the
        # owner_fk identity (§8.1).
        table_rows = [('users', ''), ('posts', ''), ('comments', ''), ('tags', ''),
                      ('posts_tags', ''), ('likes', ''), ('old_items', ''),
                      ('people', ''), ('audit_logs', '')]
        col_rows = [
            _col('users', 'id', 'bigint', 'bigint', 'NO', 'PRI'),
            _col('posts', 'id', 'bigint', 'bigint', 'NO', 'PRI'),
            _col('posts', 'user_id', 'bigint', 'bigint', 'NO', 'MUL'),
            _col('comments', 'id', 'bigint', 'bigint', 'NO', 'PRI'),
            _col('comments', 'post_id', 'bigint', 'bigint'),
            _col('comments', 'user_id', 'bigint', 'bigint'),
            _col('tags', 'id', 'bigint', 'bigint', 'NO', 'PRI'),
            _col('posts_tags', 'post_id', 'bigint', 'bigint'),
            _col('posts_tags', 'tag_id', 'bigint', 'bigint'),
            _col('likes', 'id', 'bigint', 'bigint', 'NO', 'PRI'),
            _col('old_items', 'legacy_id', 'bigint', 'bigint', 'NO', 'PRI'),
            _col('people', 'id', 'bigint', 'bigint', 'NO', 'PRI'),
            _col('audit_logs', 'id', 'bigint', 'bigint', 'NO', 'PRI'),
        ]
        fk_rows = [('posts', 'user_id', 'users')]
        erd.parse_mysql = lambda url: erd.mysql_ir(table_rows, col_rows, fk_rows, [])

        self._run('--models', str(FIXTURE_RAILS))
        new = self._data()['tables']

        # the same merge, computed directly via the public pipeline blocks
        old = erd.merge_ir([
            erd.make_provider_result('db', 'mysql',
                                     erd.mysql_ir(table_rows, col_rows, fk_rows, [])),
            erd.framework_provider(FIXTURE_RAILS),
        ])

        self.assertEqual(list(new), list(old))  # same table set + order
        for name in old:
            self.assertEqual(new[name]['associations'], old[name]['associations'], name)
            self.assertEqual(new[name].get('fk_columns'), old[name].get('fk_columns'), name)
        # posts specifically keeps BOTH :user and :author (the alias pattern)
        post_names = [(a['type'], a['name']) for a in new['posts']['associations']]
        self.assertIn(('belongs_to', 'user'), post_names)
        self.assertIn(('belongs_to', 'author'), post_names)

    def test_rails_only_table_is_schema_missing(self):
        self._run('--models', str(FIXTURE_RAILS))
        data = self._data()
        # crm_widgets is a Rails model with no DB table -> derived schema_missing
        self.assertIn('crm_widgets', data['tables'])
        self.assertTrue(data['tables']['crm_widgets']['schema_missing'])
        self.assertEqual(data['tables']['crm_widgets']['columns'], [])


class TestPipelineDBPlusPrisma(_MainDriver):
    def test_prisma_framework_columns_now_retained(self):
        # INTENDED CHANGE (§7.8): the old association-only overlay discarded
        # Prisma columns; the pipeline now keeps them. A Prisma-only model gains real
        # columns (and is NOT schema_missing), where the old flow would have
        # emitted an empty, schema_missing table.
        self._run('--models', str(FIXTURE_PRISMA))
        data = self._data()
        # 'Post' has no DB table here, but Prisma supplies its columns
        self.assertIn('Post', data['tables'])
        self.assertNotIn('schema_missing', data['tables']['Post'])
        self.assertEqual([c['name'] for c in data['tables']['Post']['columns']],
                         ['id', 'title', 'authorId'])
        # and the DB tables keep their DB columns (physical DB authority)
        self.assertEqual([c['name'] for c in data['tables']['posts']['columns']],
                         ['id', 'user_id', 'created_by_id'])


class TestPipelineRelationsOverride(_MainDriver):
    def test_relation_overrides_conflicting_framework_association_with_warning(self):
        # A framework declares posts.user_id -> users as belongs_to :owner.
        # A config relation with the SAME name marks it one_to_one (has_one).
        # Same name+column -> same identity -> config OVERRIDES (P0-3): the
        # merged edge is has_one, provenance manual, and a §8.6 warning fires
        # for the differing cardinality. The DB FK on user_id (name 'user') is
        # a separate identity and is dropped by Phase B reconcile.
        Path('.erdscope.json').write_text(json.dumps({
            'relations': [{'table': 'posts', 'column': 'user_id',
                           'references': 'users', 'one_to_one': True, 'name': 'owner'}],
        }))
        models = Path(self.tmp.name) / 'app' / 'models'
        models.mkdir(parents=True)
        (models / 'user.rb').write_text('class User < ApplicationRecord\nend\n')
        (models / 'post.rb').write_text(
            "class Post < ApplicationRecord\n  belongs_to :owner, class_name: 'User', "
            "foreign_key: 'user_id'\nend\n")
        err = self._run('--models', str(self.tmp.name), capture_stderr=True)
        data = self._data()
        posts = data['tables']['posts']
        owner = [a for a in posts['associations'] if a['foreign_key'] == 'user_id']
        self.assertEqual(len(owner), 1)
        self.assertEqual(owner[0]['type'], 'has_one')   # config one_to_one won
        self.assertEqual(owner[0]['name'], 'owner')      # override (shared name)
        self.assertTrue(owner[0]['manual'])              # config provenance
        self.assertIn('overrides a differing', err)      # §8.6 warning fired

    def test_relation_on_plain_db_fk_overrides_it(self):
        # relations override a bare DB FK too: user_id already has a DB FK;
        # the relation renames it and marks the provenance manual.
        Path('.erdscope.json').write_text(json.dumps({
            'relations': [{'table': 'posts', 'column': 'user_id',
                           'references': 'users', 'name': 'author'}],
        }))
        self._run()
        posts = self._data()['tables']['posts']
        owner = [a for a in posts['associations'] if a['foreign_key'] == 'user_id']
        self.assertEqual(len(owner), 1)
        self.assertEqual(owner[0]['name'], 'author')
        self.assertTrue(owner[0]['manual'])
        self.assertNotIn('db_fk', owner[0])  # manual provenance wins over db_fk

    def test_relation_unknown_column_exits_cleanly(self):
        # relations_to_config_layer validates against the merged base
        Path('.erdscope.json').write_text(json.dumps({
            'relations': [{'table': 'posts', 'column': 'nope_id', 'references': 'users'}],
        }))
        with self.assertRaises(SystemExit) as cm:
            self._run()
        self.assertIn('nope_id', str(cm.exception))


class TestPipelineConfigTables(_MainDriver):
    """DB + config.tables, wired in Step 7b: add/override/drop/replace via the
    config layer, plus §6.4② semantic validation (drops vs the merged base;
    references vs the final IR)."""

    def _write(self, cfg):
        Path('.erdscope.json').write_text(json.dumps(cfg))

    # ── add ──
    def test_config_adds_table_column_index_association(self):
        self._write({'tables': {
            'audit_logs': {  # brand-new table, not in the DB
                'primary_key': 'id',
                'columns': [{'name': 'id', 'type': 'bigint', 'primary': True},
                            {'name': 'user_id', 'type': 'bigint'},
                            {'name': 'action', 'type': 'string'}],
                'indexes': [{'name': 'idx_audit_action', 'columns': ['action']}],
                'associations': [{'type': 'belongs_to', 'name': 'user',
                                  'target': 'users', 'foreign_key': 'user_id'}],
            },
            'users': {'columns': [{'name': 'nickname', 'type': 'string'}]},  # add a column
        }})
        self._run()
        data = self._data()['tables']
        self.assertIn('audit_logs', data)
        self.assertEqual([c['name'] for c in data['audit_logs']['columns']],
                         ['id', 'user_id', 'action'])
        self.assertNotIn('schema_missing', data['audit_logs'])  # has columns
        self.assertEqual([i['name'] for i in data['audit_logs']['indexes']], ['idx_audit_action'])
        a = data['audit_logs']['associations'][0]
        self.assertEqual((a['target'], a['name']), ('users', 'user'))
        self.assertTrue(a['manual'])  # config-kind -> manual provenance
        self.assertIn('nickname', [c['name'] for c in data['users']['columns']])

    # ── override ──
    def test_config_overrides_db_physical_attr_and_comment(self):
        self._write({'tables': {'users': {
            'comment': 'application users',
            'columns': [{'name': 'email', 'type': 'text', 'comment': 'the login email'}],
        }}})
        self._run()
        users = self._data()['tables']['users']
        self.assertEqual(users['comment'], 'application users')
        email = next(c for c in users['columns'] if c['name'] == 'email')
        self.assertEqual(email['type'], 'text')          # Config > DB physical (was 'string')
        self.assertEqual(email['comment'], 'the login email')  # Config comment
        # untouched DB column keeps its DB type
        self.assertEqual(next(c for c in users['columns'] if c['name'] == 'id')['type'], 'bigint')

    # ── drop / replace ──
    def test_config_drops_db_column(self):
        self._write({'tables': {'posts': {'columns': [{'name': 'created_by_id', 'drop': True}]}}})
        self._run()
        self.assertEqual([c['name'] for c in self._data()['tables']['posts']['columns']],
                         ['id', 'user_id'])

    def test_config_drops_db_table(self):
        self._write({'tables': {'posts': {'drop': True}}})
        self._run()
        data = self._data()['tables']
        self.assertNotIn('posts', data)
        self.assertIn('users', data)

    def test_config_columns_mode_replace(self):
        self._write({'tables': {'users': {
            'columns_mode': 'replace',
            'columns': [{'name': 'id', 'type': 'bigint', 'primary': True},
                        {'name': 'handle', 'type': 'string'}],
        }}})
        self._run()
        self.assertEqual([c['name'] for c in self._data()['tables']['users']['columns']],
                         ['id', 'handle'])

    # ── references resolve config-added items ──
    def test_relation_references_config_added_table(self):
        # relations validate against base2 (db + config.tables), so a relation
        # may point at a table config.tables just added
        self._write({
            'tables': {'audit_logs': {
                'primary_key': 'id',
                'columns': [{'name': 'id', 'type': 'bigint', 'primary': True},
                            {'name': 'user_id', 'type': 'bigint'}]}},
            'relations': [{'table': 'audit_logs', 'column': 'user_id', 'references': 'users'}],
        })
        self._run()
        audit = self._data()['tables']['audit_logs']
        a = [x for x in audit['associations'] if x['foreign_key'] == 'user_id']
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0]['target'], 'users')
        self.assertTrue(a[0]['manual'])

    def test_config_assoc_references_config_added_table(self):
        # a config that both ADDS a table and references it from another table
        self._write({'tables': {
            'invoices': {'primary_key': 'id',
                         'columns': [{'name': 'id', 'type': 'bigint', 'primary': True}]},
            'users': {'associations': [{'type': 'has_many', 'name': 'invoices',
                                        'target': 'invoices'}]},
        }})
        self._run()
        data = self._data()['tables']
        self.assertIn('invoices', data)
        self.assertIn('invoices', [a['name'] for a in data['users']['associations']])

    # ── §6.4② semantic errors ──
    def test_drop_nonexistent_table_errors(self):
        self._write({'tables': {'ghost': {'drop': True}}})
        with self.assertRaises(SystemExit) as cm:
            self._run()
        self.assertIn('ghost', str(cm.exception))

    def test_drop_nonexistent_column_errors(self):
        self._write({'tables': {'users': {'columns': [{'name': 'nope', 'drop': True}]}}})
        with self.assertRaises(SystemExit) as cm:
            self._run()
        self.assertIn('nope', str(cm.exception))

    def test_drop_nonexistent_index_errors(self):
        self._write({'tables': {'users': {'indexes': [{'name': 'idx_ghost', 'drop': True}]}}})
        with self.assertRaises(SystemExit) as cm:
            self._run()
        self.assertIn('idx_ghost', str(cm.exception))

    def test_config_assoc_target_nonexistent_errors(self):
        self._write({'tables': {'users': {'associations': [
            {'type': 'belongs_to', 'name': 'planet', 'target': 'planets',
             'foreign_key': 'planet_id'}]}}})
        with self.assertRaises(SystemExit) as cm:
            self._run()
        self.assertIn('planets', str(cm.exception))

    def test_primary_key_nonexistent_column_errors(self):
        self._write({'tables': {'widgets': {
            'primary_key': 'nope_id',
            'columns': [{'name': 'id', 'type': 'bigint', 'primary': True}]}}})
        with self.assertRaises(SystemExit) as cm:
            self._run()
        self.assertIn('nope_id', str(cm.exception))

    def test_relation_to_truly_nonexistent_table_errors(self):
        self._write({'relations': [{'table': 'users', 'column': 'email', 'references': 'planets'}]})
        with self.assertRaises(SystemExit) as cm:
            self._run()
        self.assertIn('planets', str(cm.exception))

    def test_config_assoc_foreign_key_nonexistent_column_errors(self):
        # P1-d: the association target exists (users), but the declared
        # foreign_key names a column the source table (posts) doesn't have.
        self._write({'tables': {'posts': {'associations': [
            {'type': 'belongs_to', 'name': 'author', 'target': 'users',
             'foreign_key': 'author_id'}]}}})  # posts has no author_id
        with self.assertRaises(SystemExit) as cm:
            self._run()
        self.assertIn('author_id', str(cm.exception))

    # ── syntactic/semantic boundary, end-to-end ──
    def test_syntactically_valid_but_semantically_bad_errors_at_runtime(self):
        # a drop of an absent column: Step-3 load validation accepts it
        # (syntax only); the pipeline now rejects it (§6.4② semantic).
        self._write({'tables': {'users': {'columns': [{'name': 'absent', 'drop': True}]}}})
        loaded = erd.load_config(SimpleNamespace(config=None, no_config=False))
        self.assertIn('users', loaded['tables'])  # loads clean (syntactic pass)
        with self.assertRaises(SystemExit) as cm:  # but errors when applied
            self._run()
        self.assertIn('absent', str(cm.exception))


class _NoDBDriver(unittest.TestCase):
    """Drive main() with NO database url, spying on the DB parse entry points to
    prove they're never called (no connection, no password prompt) — Steps 8/9.
    Everything is written under a temp dir so nothing lands in the repo."""
    def setUp(self):
        self._orig = (erd.parse_mysql, erd.parse_postgres)
        self.calls = []
        erd.parse_mysql = lambda url: self.calls.append(('mysql', url))
        erd.parse_postgres = lambda url: self.calls.append(('postgres', url))
        self._argv = sys.argv
        self._cwd = os.getcwd()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: setattr(sys, 'argv', self._argv))
        self.addCleanup(os.chdir, self._cwd)
        self.addCleanup(lambda: (setattr(erd, 'parse_mysql', self._orig[0]),
                                 setattr(erd, 'parse_postgres', self._orig[1])))
        os.chdir(self.tmp.name)  # empty cwd -> no .erdscope auto-discovery

    def _p(self, name):
        return str(Path(self.tmp.name) / name)

    def _run(self, *argv):
        sys.argv = ['erd.py', *argv]
        erd.main()

    def _data(self, path):
        return json.loads(re.search(r'const DATA = (\{.*?\});\s*\n',
                                    Path(path).read_text()).group(1))


class TestPipelineFrameworkOnly(_NoDBDriver):
    def test_rails_only_no_db_contact_and_schema_missing(self):
        out = self._p('out.html')
        self._run('--models', str(FIXTURE_RAILS), '--no-config', '-o', out)
        self.assertEqual(self.calls, [])  # DB never contacted
        data = self._data(out)['tables']
        self.assertIn('users', data)
        # Rails carries no columns -> derived schema_missing
        self.assertTrue(data['users']['schema_missing'])
        self.assertEqual(data['users']['columns'], [])
        # title falls back to the framework project name (§10)
        self.assertIn('<title>fixture_app — ERD</title>', Path(out).read_text())

    def test_prisma_only_keeps_columns(self):
        out = self._p('out.html')
        self._run('--models', str(FIXTURE_PRISMA), '--no-config', '-o', out)
        self.assertEqual(self.calls, [])
        data = self._data(out)['tables']
        self.assertIn('Post', data)
        self.assertNotIn('schema_missing', data['Post'])  # has columns
        self.assertEqual([c['name'] for c in data['Post']['columns']],
                         ['id', 'title', 'authorId'])
        self.assertIn('<title>fixture_prisma — ERD</title>', Path(out).read_text())

    def test_django_only_keeps_columns(self):
        out = self._p('out.html')
        self._run('--models', str(FIXTURE_DJANGO), '--no-config', '-o', out)
        self.assertEqual(self.calls, [])
        data = self._data(out)['tables']
        self.assertIn('blog_entries', data)
        self.assertNotIn('schema_missing', data['blog_entries'])
        self.assertIn('<title>fixture_django — ERD</title>', Path(out).read_text())

    def test_framework_only_excel(self):
        import zipfile
        out, xlsx = self._p('out.html'), self._p('defs.xlsx')
        self._run('--models', str(FIXTURE_PRISMA), '--no-config', '-o', out, '--excel', xlsx)
        self.assertEqual(self.calls, [])
        self.assertTrue(Path(xlsx).exists())
        with zipfile.ZipFile(xlsx) as z:
            self.assertIn('xl/workbook.xml', z.namelist())
            wb = z.read('xl/workbook.xml').decode()
            self.assertIn('name="Post"', wb)  # a Prisma model became a sheet

    def test_config_title_wins_over_framework_name(self):
        cfg = self._p('c.json')
        Path(cfg).write_text(json.dumps({'title': 'custom', 'models': str(FIXTURE_PRISMA)}))
        out = self._p('out.html')
        self._run('--config', cfg, '-o', out)
        self.assertEqual(self.calls, [])
        self.assertIn('<title>custom — ERD</title>', Path(out).read_text())

    def test_multiple_models_merge_both_frameworks(self):
        # P1-a (§10): two --models flags merge BOTH frameworks, rather than a
        # second --models silently winning/dropping the first.
        out = self._p('out.html')
        self._run('--models', str(FIXTURE_RAILS), '--models', str(FIXTURE_PRISMA),
                  '--no-config', '-o', out)
        self.assertEqual(self.calls, [])
        data = self._data(out)['tables']
        self.assertIn('webhooks', data)  # a Rails-only table
        self.assertIn('Post', data)      # a Prisma-only table

    def test_config_models_list_merges_both_frameworks(self):
        # P1-a: config `models` accepts a list of framework paths.
        cfg = self._p('c.json')
        Path(cfg).write_text(json.dumps(
            {'models': [str(FIXTURE_RAILS), str(FIXTURE_PRISMA)]}))
        out = self._p('out.html')
        self._run('--config', cfg, '-o', out)
        self.assertEqual(self.calls, [])
        data = self._data(out)['tables']
        self.assertIn('webhooks', data)
        self.assertIn('Post', data)

    def test_config_models_single_string_still_works(self):
        # P1-a: a single string stays valid (back-compat).
        cfg = self._p('c.json')
        Path(cfg).write_text(json.dumps({'models': str(FIXTURE_PRISMA)}))
        out = self._p('out.html')
        self._run('--config', cfg, '-o', out)
        self.assertEqual(self.calls, [])
        self.assertIn('Post', self._data(out)['tables'])


class TestPipelineConfigOnly(_NoDBDriver):
    def _schema_config(self):
        return {'tables': {
            'customers': {'primary_key': 'id',
                          'columns': [{'name': 'id', 'type': 'bigint', 'primary': True},
                                      {'name': 'email', 'type': 'string'}],
                          'associations': [{'type': 'has_many', 'name': 'invoices',
                                            'target': 'invoices'}]},
            'invoices': {'primary_key': 'id',
                         'columns': [{'name': 'id', 'type': 'bigint', 'primary': True},
                                     {'name': 'customer_id', 'type': 'bigint'}],
                         'associations': [{'type': 'belongs_to', 'name': 'customer',
                                           'target': 'customers', 'foreign_key': 'customer_id'}]}}}

    def test_config_only_html_with_title(self):
        cfg = self._p('schema.json')
        Path(cfg).write_text(json.dumps({'title': 'billing', **self._schema_config()}))
        out = self._p('out.html')
        self._run('--config', cfg, '-o', out)
        self.assertEqual(self.calls, [])  # no DB
        data = self._data(out)['tables']
        self.assertEqual(set(data), {'customers', 'invoices'})
        self.assertNotIn('schema_missing', data['customers'])
        self.assertEqual(data['invoices']['fk_columns'], ['customer_id'])
        self.assertIn('<title>billing — ERD</title>', Path(out).read_text())

    def test_config_only_excel(self):
        import zipfile
        cfg = self._p('schema.json')
        Path(cfg).write_text(json.dumps(self._schema_config()))
        out, xlsx = self._p('out.html'), self._p('defs.xlsx')
        self._run('--config', cfg, '-o', out, '--excel', xlsx)
        self.assertTrue(Path(xlsx).exists())
        with zipfile.ZipFile(xlsx) as z:
            wb = z.read('xl/workbook.xml').decode()
            self.assertIn('name="customers"', wb)
            self.assertIn('name="invoices"', wb)

    def test_config_only_no_title_falls_back_to_output_stem(self):
        cfg = self._p('schema.json')
        Path(cfg).write_text(json.dumps(
            {'tables': {'t': {'columns': [{'name': 'id', 'type': 'bigint', 'primary': True}]}}}))
        out = self._p('mybilling.html')
        self._run('--config', cfg, '-o', out)
        self.assertIn('<title>mybilling — ERD</title>', Path(out).read_text())


class TestPipelineNoSchemaSourceErrors(_NoDBDriver):
    def test_no_source_at_all_errors(self):
        with self.assertRaises(SystemExit) as cm:
            self._run('--no-config')
        self.assertIn('no schema input', str(cm.exception))
        self.assertEqual(self.calls, [])  # never tried to connect

    def test_output_settings_only_config_errors(self):
        cfg = self._p('c.json')
        Path(cfg).write_text(json.dumps({'output': 'x.html', 'max_rows': 20}))
        with self.assertRaises(SystemExit) as cm:
            self._run('--config', cfg)
        self.assertIn('no schema input', str(cm.exception))
        self.assertEqual(self.calls, [])

    def test_relations_only_config_no_base_errors(self):
        cfg = self._p('c.json')
        Path(cfg).write_text(json.dumps(
            {'relations': [{'table': 'a', 'column': 'b_id', 'references': 'b'}]}))
        with self.assertRaises(SystemExit) as cm:
            self._run('--config', cfg)
        self.assertIn('no schema input', str(cm.exception))
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
