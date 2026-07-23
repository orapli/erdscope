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
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # chdir-back registered last so it runs first (addCleanup is LIFO) —
        # Windows can't rmtree a directory that's still the process cwd.
        self.addCleanup(os.chdir, self._orig_cwd)
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

        # the same merge, computed directly via the public pipeline blocks, then
        # run through the same serialize boundary main uses (provenance/sources
        # -> legacy flags) so it matches the viewer JSON `new` reads back.
        old = erd.serialize_for_viewer(erd.merge_ir([
            erd.make_provider_result('db', 'mysql',
                                     erd.mysql_ir(table_rows, col_rows, fk_rows, [])),
            erd.framework_provider(FIXTURE_RAILS),
        ]))

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

    def test_viewer_json_has_no_provenance_or_sources(self):
        # Step 10 (§9.3): the internal provenance/sources model must NOT leak
        # into the viewer JSON — it carries exactly today's legacy flags.
        self._run('--models', str(FIXTURE_RAILS))
        data = self._data()['tables']
        for t in data.values():
            for a in t['associations']:
                self.assertNotIn('provenance', a)
                self.assertNotIn('sources', a)

    def test_db_only_viewer_keeps_legacy_db_fk_flag(self):
        # a surviving DB FK serializes back to the legacy db_fk boolean
        self._run()  # DB only, no --models
        posts = self._data()['tables']['posts']
        user_fk = next(a for a in posts['associations'] if a['name'] == 'user')
        self.assertTrue(user_fk.get('db_fk'))
        self.assertNotIn('provenance', user_fk)


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


class TestDBAdapterRegistry(unittest.TestCase):
    """The DBAdapter base + scheme registry: register/resolve, db_provider
    dispatch through the registry, and the ABC contract."""
    def setUp(self):
        # snapshot the registry so a test's registration can't leak
        self._snapshot = dict(erd.DB_ADAPTERS)
        self.addCleanup(lambda: (erd.DB_ADAPTERS.clear(),
                                 erd.DB_ADAPTERS.update(self._snapshot)))

    def test_builtins_registered_with_aliases(self):
        self.assertIs(erd.db_adapter_for('mysql'), erd.MySQLAdapter)
        self.assertIs(erd.db_adapter_for('postgres'), erd.PostgresAdapter)
        self.assertIs(erd.db_adapter_for('postgresql'), erd.PostgresAdapter)
        self.assertIs(erd.db_adapter_for('MySQL'), erd.MySQLAdapter)  # case-insensitive
        self.assertIsNone(erd.db_adapter_for('mongodb'))

    def test_base_is_abstract(self):
        with self.assertRaises(TypeError):
            erd.DBAdapter()

    def test_register_and_dispatch(self):
        @erd.register_adapter
        class DemoAdapter(erd.DBAdapter):
            schemes = ('demo',)
            name = 'demo'
            label = 'Demo'
            def fetch(self, url):
                return erd.mysql_ir([('t', '')],
                                    [('t', 'id', 'integer', 'integer', 'NO', 'PRI', '', '', '')],
                                    [], [])
        self.assertIs(erd.db_adapter_for('demo'), DemoAdapter)
        result = erd.db_provider('demo://host/db')
        self.assertEqual(result['source'], {'kind': 'db', 'provider': 'demo',
                                            'location': 'demo://host/db'})
        self.assertIn('t', result['tables'])

    def test_db_provider_unknown_scheme_errors(self):
        with self.assertRaises(SystemExit) as cm:
            erd.db_provider('mongodb://h/d')
        self.assertIn('mongodb', str(cm.exception))

    def test_registration_rejects_invalid_scheme(self):
        with self.assertRaises(ValueError):
            @erd.register_adapter
            class BadSchemeAdapter(erd.DBAdapter):
                schemes = ('Not A Scheme',)
                name = 'bad'
                def fetch(self, url): return {}

    def test_dispatch_rejects_invalid_tables_ir(self):
        @erd.register_adapter
        class BrokenAdapter(erd.DBAdapter):
            schemes = ('broken',)
            name = 'broken'
            def fetch(self, url): return {'users': {'columns': {}}}
        with self.assertRaises(SystemExit) as cm:
            erd.db_provider('broken://host/db')
        self.assertIn('invalid output from database adapter', str(cm.exception))

    def test_dispatch_requires_complete_db_table_shape(self):
        @erd.register_adapter
        class SparseDBAdapter(erd.DBAdapter):
            schemes = ('sparsedb',)
            name = 'sparsedb'
            def fetch(self, url): return {'users': {'associations': []}}
        with self.assertRaises(SystemExit) as cm:
            erd.db_provider('sparsedb://host/db')
        self.assertIn('missing DB fields', str(cm.exception))

    def test_fetch_value_error_is_not_misreported_as_invalid_output(self):
        @erd.register_adapter
        class FailingAdapter(erd.DBAdapter):
            schemes = ('failing',)
            name = 'failing'
            def fetch(self, url): raise ValueError('bad connection option')
        with self.assertRaisesRegex(ValueError, 'bad connection option'):
            erd.db_provider('failing://host/db')


class TestFrameworkOverlayRegistry(unittest.TestCase):
    """The FrameworkOverlay base + registry: register/detect, dispatch through
    framework_provider, priority ordering, and the ABC contract."""
    def setUp(self):
        self._snapshot = list(erd.FRAMEWORK_OVERLAYS)
        self.addCleanup(lambda: erd.FRAMEWORK_OVERLAYS.__setitem__(
            slice(None), self._snapshot))

    def test_builtins_detect(self):
        self.assertEqual(erd.detect_code_source(FIXTURE_RAILS), 'rails')
        self.assertEqual(erd.detect_code_source(FIXTURE_PRISMA), 'prisma')
        self.assertEqual(erd.detect_code_source(FIXTURE_DJANGO), 'django')
        # the whole tests/ tree is not itself a Rails/Django/Prisma/Laravel root
        # (those checks are shallow, root-relative marker files) but IS correctly
        # detected as sqlalchemy: unlike the other four, its detect() is a
        # recursive *.py scan (backlog F1), and tests/fixture_contract/sqlalchemy/
        # models.py is genuine, detectable declarative-model code nested in it.
        self.assertEqual(erd.detect_code_source(Path(__file__).resolve().parent), 'sqlalchemy')

    def test_base_is_abstract(self):
        with self.assertRaises(TypeError):
            erd.FrameworkOverlay()

    def test_register_detect_and_build(self):
        @erd.register_overlay
        class MarkerOverlay(erd.FrameworkOverlay):
            name = 'marker'
            priority = 0  # runs before the built-ins
            expects = 'a directory containing .marker'
            def detect(self, root):
                return root.is_dir() and (root / '.marker').exists()
            def build(self, root, table_map):
                return erd.make_provider_result('framework', 'marker',
                    {'gizmos': {'associations': []}}, location=str(root))
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / '.marker').write_text('')
            self.assertEqual(erd.detect_code_source(Path(d)), 'marker')
            result = erd.framework_provider(Path(d))
            self.assertEqual(result['source']['provider'], 'marker')
            self.assertIn('gizmos', result['tables'])

    def test_undetected_errors(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(SystemExit) as cm:
                erd.framework_provider(Path(d))
            self.assertIn('could not detect', str(cm.exception))

    def test_registration_rejects_incomplete_metadata(self):
        with self.assertRaises(ValueError):
            @erd.register_overlay
            class MissingName(erd.FrameworkOverlay):
                expects = 'anything'
                def detect(self, root): return False
                def build(self, root, table_map): return None

    def test_dispatch_rejects_invalid_provider_result(self):
        @erd.register_overlay
        class BrokenOverlay(erd.FrameworkOverlay):
            name = 'broken'
            priority = -1
            expects = 'a directory containing .broken'
            def detect(self, root): return True
            def build(self, root, table_map):
                return {'tables': {}}
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(SystemExit) as cm:
                erd.framework_provider(Path(d))
            self.assertIn('invalid output from framework overlay', str(cm.exception))

    def test_sparse_association_only_result_is_valid(self):
        result = erd.make_provider_result(
            'framework', 'sparse',
            {'users': {'associations': [
                {'type': 'has_many', 'name': 'posts', 'target': 'posts'}]}})
        self.assertIs(erd.validate_provider_result(
            result, expected_kind='framework', expected_provider='sparse'), result)

    def test_build_value_error_is_not_misreported_as_invalid_output(self):
        @erd.register_overlay
        class FailingOverlay(erd.FrameworkOverlay):
            name = 'failing_overlay'
            priority = -1
            expects = 'anything'
            def detect(self, root): return True
            def build(self, root, table_map): raise ValueError('bad model syntax')
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ValueError, 'bad model syntax'):
                erd.framework_provider(Path(d))

    def test_later_overlay_registration_replaces_same_name(self):
        @erd.register_overlay
        class FirstOverlay(erd.FrameworkOverlay):
            name = 'duplicate_test'
            expects = 'anything'
            def detect(self, root): return False
            def build(self, root, table_map): return None
        @erd.register_overlay
        class SecondOverlay(erd.FrameworkOverlay):
            name = 'duplicate_test'
            expects = 'anything else'
            def detect(self, root): return False
            def build(self, root, table_map): return None
        matches = [c for c in erd.FRAMEWORK_OVERLAYS if c.name == 'duplicate_test']
        self.assertEqual(matches, [SecondOverlay])


class TestProviderBoundaryValidation(unittest.TestCase):
    def _tables(self):
        return {'users': {
            'primary_key': 'id',
            'columns': [{'name': 'id', 'type': 'integer',
                         'nullable': False, 'primary': True}],
            'indexes': [{'name': 'PRIMARY', 'columns': ['id'], 'unique': True}],
            'associations': []}}

    def test_complete_db_shape_is_valid(self):
        tables = self._tables()
        self.assertIs(erd.validate_tables_ir(tables, require_complete=True), tables)

    def test_optional_field_types_are_checked(self):
        mutations = [
            lambda t: t['users'].__setitem__('comment', 1),
            lambda t: t['users']['columns'][0].__setitem__('nullable', 'no'),
            lambda t: t['users']['columns'][0].__setitem__('type', 42),
            lambda t: t['users']['indexes'][0].__setitem__('unique', 1),
            lambda t: t['users']['indexes'][0].__setitem__('columns', []),
            lambda t: t['users'].__setitem__('associations', [{
                'type': 'belongs_to', 'name': 'account', 'target': 'accounts',
                'foreign_key': ['account_id']}]),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                tables = self._tables()
                mutate(tables)
                with self.assertRaises(ValueError):
                    erd.validate_tables_ir(tables, require_complete=True)

    def test_provider_result_rejects_wrong_location_and_extra_key(self):
        result = erd.make_provider_result('framework', 'x', {})
        result['source']['location'] = 123
        with self.assertRaises(ValueError):
            erd.validate_provider_result(result)
        result = erd.make_provider_result('framework', 'x', {})
        result['extra'] = True
        with self.assertRaises(ValueError):
            erd.validate_provider_result(result)


class TestAdapterPlugins(_NoDBDriver):
    """--adapter / config `adapters`: a plugin file that registers a custom
    DBAdapter makes its URL scheme usable end-to-end, and the built-in
    parse_mysql/parse_postgres spies confirm no built-in DB path is touched."""
    def setUp(self):
        super().setUp()
        self._snapshot = dict(erd.DB_ADAPTERS)
        self._overlays = list(erd.FRAMEWORK_OVERLAYS)
        self.addCleanup(lambda: (erd.DB_ADAPTERS.clear(),
                                 erd.DB_ADAPTERS.update(self._snapshot)))
        self.addCleanup(lambda: erd.FRAMEWORK_OVERLAYS.__setitem__(
            slice(None), self._overlays))

    _PLUGIN = (
        "from erd import DBAdapter, register_adapter, mysql_ir\n"
        "@register_adapter\n"
        "class FooAdapter(DBAdapter):\n"
        "    schemes = ('foo',)\n"
        "    name = 'foo'\n"
        "    label = 'Foo'\n"
        "    def fetch(self, url):\n"
        "        return mysql_ir([('widgets', '')],\n"
        "            [('widgets', 'id', 'integer', 'integer', 'NO', 'PRI', '', '', '')], [], [])\n"
    )

    def test_cli_adapter_registers_scheme_and_runs(self):
        plug, out = self._p('foo_adapter.py'), self._p('out.html')
        Path(plug).write_text(self._PLUGIN)
        self._run('foo://host/db', '--adapter', plug, '--no-config', '-o', out)
        self.assertEqual(self.calls, [])  # no built-in DB path touched
        data = self._data(out)['tables']
        self.assertIn('widgets', data)
        self.assertIn('<title>db — ERD</title>', Path(out).read_text())

    def test_config_adapters_registers_scheme(self):
        plug, cfg, out = self._p('foo_adapter.py'), self._p('c.json'), self._p('out.html')
        Path(plug).write_text(self._PLUGIN)
        Path(cfg).write_text(json.dumps({'adapters': plug, 'database': 'db',
                                         'engine': 'mysql'}))
        # config supplies the adapter; the CLI URL uses its scheme
        self._run('foo://host/db', '--config', cfg, '-o', out)
        self.assertEqual(self.calls, [])
        self.assertIn('widgets', self._data(out)['tables'])

    def test_missing_plugin_errors(self):
        with self.assertRaises(SystemExit) as cm:
            self._run('foo://host/db', '--adapter', self._p('nope.py'), '--no-config',
                      '-o', self._p('out.html'))
        self.assertIn('nope.py', str(cm.exception))

    def test_plugin_can_register_framework_overlay(self):
        # the same plugin mechanism registers a custom FrameworkOverlay, driven
        # off a --models path with no DB at all
        plug, out = self._p('gadget_overlay.py'), self._p('out.html')
        proj = Path(self._p('gadget_project'))
        proj.mkdir()
        (proj / '.gadget').write_text('')
        Path(plug).write_text(
            "from erd import FrameworkOverlay, register_overlay, make_provider_result\n"
            "@register_overlay\n"
            "class GadgetOverlay(FrameworkOverlay):\n"
            "    name = 'gadget'\n"
            "    priority = 0\n"
            "    expects = 'a directory containing .gadget'\n"
            "    def detect(self, root):\n"
            "        return root.is_dir() and (root / '.gadget').exists()\n"
            "    def build(self, root, table_map):\n"
            "        return make_provider_result('framework', 'gadget',\n"
            "            {'gadgets': {'columns': [{'name': 'id', 'type': 'integer',\n"
            "             'nullable': False, 'primary': True}], 'associations': [],\n"
            "             'primary_key': 'id'}}, location=str(root))\n")
        self._run('--adapter', plug, '--models', str(proj), '--no-config', '-o', out)
        self.assertEqual(self.calls, [])
        self.assertIn('gadgets', self._data(out)['tables'])


class TestInputSpecNormalization(unittest.TestCase):
    """D4: normalize_input_specs — the deterministic order code-source layers
    are built in (config `sources` first, in declared order, then legacy
    --models/config `models` entries), independent of any dispatch."""

    def test_config_sources_then_legacy_models_order(self):
        specs = erd.normalize_input_specs(
            ['a/models', 'b/models'],
            [{'id': 'primary', 'type': 'rails.models', 'path': str(FIXTURE_RAILS)}])
        self.assertEqual([s['id'] for s in specs], ['primary', 'models[0]', 'models[1]'])
        self.assertEqual(specs[0]['type'], 'rails.models')
        self.assertIsNone(specs[1]['type'])
        self.assertIsNone(specs[2]['type'])
        self.assertTrue(specs[1]['path'].is_absolute())  # resolved, like today's --models

    def test_no_sources_no_models_is_empty(self):
        self.assertEqual(erd.normalize_input_specs([], []), [])

    def test_known_source_type_names_includes_dynamic_models_types(self):
        names = erd.known_source_type_names()
        self.assertIn('rails.models', names)
        self.assertIn('prisma.models', names)
        self.assertIn('django.models', names)
        self.assertEqual(names, sorted(names))  # sorted, per D4


class TestSourceDispatch(_NoDBDriver):
    """D4 run_input_specs: typed sources skip detection and call the named
    overlay directly; untyped (legacy) sources keep today's auto-detection,
    now with ambiguity reporting when more than one framework matches."""

    def test_typed_models_source_skips_detection(self):
        # a directory with BOTH Rails-parseable models and a schema.prisma:
        # auto-detection would consider prisma too, but a typed rails.models
        # source dispatches straight to RailsOverlay — Prisma's Post model
        # (which detection WOULD have offered) never gets parsed.
        proj = Path(self._p('mixed_rails'))
        (proj / 'app' / 'models').mkdir(parents=True)
        (proj / 'app' / 'models' / 'widget.rb').write_text(
            'class Widget < ApplicationRecord\n  belongs_to :gadget\nend\n')
        (proj / 'schema.prisma').write_text(
            'model Post {\n  id Int @id\n  title String\n}\n')
        cfg = self._p('c.json')
        Path(cfg).write_text(json.dumps({'sources': [
            {'id': 'x', 'type': 'rails.models', 'path': str(proj)}]}))
        out = self._p('out.html')
        self._run('--config', cfg, '-o', out)
        data = self._data(out)['tables']
        self.assertIn('widgets', data)
        self.assertNotIn('Post', data)

    def test_typed_source_finding_nothing_is_an_error(self):
        # a path/type mismatch (a Prisma project declared as rails.models)
        # parses nothing — that must be a hard error naming the source id and
        # the layout the type expected, not a silently empty "success"
        cfg = self._p('c.json')
        Path(cfg).write_text(json.dumps({'sources': [
            {'id': 'x', 'type': 'rails.models', 'path': str(FIXTURE_PRISMA)}]}))
        with self.assertRaises(SystemExit) as cm:
            self._run('--config', cfg, '-o', self._p('out.html'))
        msg = str(cm.exception)
        self.assertIn("source 'x'", msg)
        self.assertIn('found nothing to parse', msg)
        self.assertIn('app/models', msg)       # the expected layout, quoted
        self.assertIn('allow_empty', msg)      # and the explicit opt-out

    def test_typed_source_allow_empty_accepts_an_empty_result(self):
        cfg = self._p('c.json')
        Path(cfg).write_text(json.dumps({'sources': [
            {'id': 'x', 'type': 'rails.models', 'path': str(FIXTURE_PRISMA),
             'allow_empty': True}]}))
        out = self._p('out.html')
        self._run('--config', cfg, '-o', out)
        self.assertEqual(self._data(out)['tables'], {})

    def test_empty_schema_rb_typed_source_is_an_error(self):
        empty = self._p('schema.rb')
        Path(empty).write_text('# no create_table here\n')
        cfg = self._p('c.json')
        Path(cfg).write_text(json.dumps({'sources': [
            {'id': 's', 'type': 'rails.schema', 'path': empty}]}))
        with self.assertRaises(SystemExit) as cm:
            self._run('--config', cfg, '-o', self._p('out.html'))
        msg = str(cm.exception)
        self.assertIn("source 's'", msg)
        self.assertIn('create_table', msg)

    def test_rails_project_propagates_allow_empty_to_its_halves(self):
        # rails.project with a schema.rb but an app/models dir holding no
        # models: the expanded rails.models half is empty. Without allow_empty
        # that fails naming the half's id; with it, the run succeeds on the
        # schema half alone.
        proj = Path(self._p('railsapp'))
        (proj / 'db').mkdir(parents=True)
        (proj / 'app' / 'models').mkdir(parents=True)
        (proj / 'db' / 'schema.rb').write_text(
            'ActiveRecord::Schema.define(version: 1) do\n'
            '  create_table "users" do |t|\n    t.string "name"\n  end\n'
            'end\n')
        cfg = self._p('c.json')
        Path(cfg).write_text(json.dumps({'sources': [
            {'id': 'p', 'type': 'rails.project', 'path': str(proj)}]}))
        with self.assertRaises(SystemExit) as cm:
            self._run('--config', cfg, '-o', self._p('out.html'))
        self.assertIn("source 'p:models'", str(cm.exception))
        Path(cfg).write_text(json.dumps({'sources': [
            {'id': 'p', 'type': 'rails.project', 'path': str(proj),
             'allow_empty': True}]}))
        out = self._p('out.html')
        self._run('--config', cfg, '-o', out)
        self.assertIn('users', self._data(out)['tables'])

    def test_typed_source_type_overrides_would_be_detection(self):
        # a directory with BOTH a schema.prisma and Rails-parseable models:
        # declaring type: prisma.models must retrieve Prisma's columns even
        # though Rails would win auto-detection (lower priority number).
        proj = Path(self._p('mixed'))
        (proj / 'app' / 'models').mkdir(parents=True)
        (proj / 'app' / 'models' / 'widget.rb').write_text(
            'class Widget < ApplicationRecord\nend\n')
        (proj / 'schema.prisma').write_text(
            'model Post {\n  id Int @id\n  title String\n}\n')
        cfg = self._p('c.json')
        Path(cfg).write_text(json.dumps({'sources': [
            {'id': 'x', 'type': 'prisma.models', 'path': str(proj)}]}))
        out = self._p('out.html')
        self._run('--config', cfg, '-o', out)
        data = self._data(out)['tables']
        self.assertIn('Post', data)
        self.assertNotIn('widgets', data)

    def test_unknown_source_type_lists_known_types(self):
        cfg = self._p('c.json')
        Path(cfg).write_text(json.dumps({'sources': [
            {'id': 'x', 'type': 'nope', 'path': str(FIXTURE_RAILS)}]}))
        with self.assertRaises(SystemExit) as cm:
            self._run('--config', cfg, '-o', self._p('out.html'))
        msg = str(cm.exception)
        self.assertIn("unknown type 'nope'", msg)
        self.assertIn('rails.models', msg)

    def test_typed_source_path_must_exist(self):
        cfg = self._p('c.json')
        Path(cfg).write_text(json.dumps({'sources': [
            {'id': 'x', 'type': 'rails.models', 'path': self._p('nope')}]}))
        with self.assertRaises(SystemExit) as cm:
            self._run('--config', cfg, '-o', self._p('out.html'))
        self.assertIn('does not exist', str(cm.exception))

    def test_sources_and_models_both_apply(self):
        cfg = self._p('c.json')
        Path(cfg).write_text(json.dumps({'sources': [
            {'id': 'x', 'type': 'prisma.models', 'path': str(FIXTURE_PRISMA)}]}))
        out = self._p('out.html')
        self._run('--config', cfg, '--models', str(FIXTURE_RAILS), '-o', out)
        data = self._data(out)['tables']
        self.assertIn('Post', data)      # from config sources
        self.assertIn('webhooks', data)  # from --models

    def test_multiple_framework_matches_reports_ambiguity_but_keeps_winner(self):
        # a directory containing both an app/models dir (Rails, priority 1)
        # and a schema.prisma (Prisma, priority 3) matches two overlays; the
        # winner is unchanged (today's framework_overlay_for pick) but a note
        # is printed naming the runner-up(s).
        proj = Path(self._p('ambiguous'))
        (proj / 'app' / 'models').mkdir(parents=True)
        (proj / 'app' / 'models' / 'widget.rb').write_text(
            'class Widget < ApplicationRecord\nend\n')
        (proj / 'schema.prisma').write_text('model Post {\n  id Int @id\n}\n')
        out = self._p('out.html')
        err = io.StringIO()
        with redirect_stderr(err):
            self._run('--models', str(proj), '--no-config', '-o', out)
        self.assertIn('matched multiple frameworks', err.getvalue())
        self.assertIn('rails', err.getvalue())
        self.assertIn('sources[].type', err.getvalue())
        data = self._data(out)['tables']
        self.assertIn('widgets', data)   # rails won (today's winner, unchanged)
        self.assertNotIn('Post', data)

    def test_single_framework_match_is_silent(self):
        out = self._p('out.html')
        err = io.StringIO()
        with redirect_stderr(err):
            self._run('--models', str(FIXTURE_RAILS), '--no-config', '-o', out)
        self.assertNotIn('matched multiple frameworks', err.getvalue())


if __name__ == '__main__':
    unittest.main()
