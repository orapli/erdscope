"""Unit tests for erd.py

Run from the repository root:
    python3 -m unittest discover -s tests -v

The database is the required source, so most tests exercise the pure
IR-building functions (mysql_ir) and the code-overlay parsers directly.
tests/fixture_app (Rails models), tests/fixture_prisma and
tests/fixture_django cover the --models overlay parsers.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / 'fixture_app'

spec = importlib.util.spec_from_file_location('erd', ROOT / 'erd.py')
erd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(erd)


# information_schema fixture rows shared across tests
TABLE_ROWS = [
    ('users', 'User accounts'),
    ('posts', ''),
    ('comments', ''),
    ('tags', ''),
    ('posts_tags', ''),
    ('likes', ''),
    ('old_items', ''),
    ('people', ''),
    ('audit_logs', ''),
]
def _col(t, name, dtype, ctype, null='YES', key='', default='', extra='', comment=''):
    return (t, name, dtype, ctype, null, key, default, extra, comment)
COL_ROWS = [
    _col('users', 'id', 'bigint', 'bigint', 'NO', 'PRI', '', 'auto_increment'),
    _col('users', 'email', 'varchar', 'varchar(255)', 'NO', 'UNI',
         comment='Login email address'),
    _col('users', 'bio', 'text', 'text'),
    _col('posts', 'id', 'bigint', 'bigint', 'NO', 'PRI'),
    _col('posts', 'user_id', 'bigint', 'bigint', 'NO', 'MUL'),
    _col('posts', 'title', 'varchar', 'varchar(200)', 'NO'),
    _col('comments', 'id', 'bigint', 'bigint', 'NO', 'PRI'),
    _col('comments', 'post_id', 'bigint', 'bigint', 'NO'),
    _col('comments', 'user_id', 'bigint', 'bigint', 'NO'),
    _col('tags', 'id', 'bigint', 'bigint', 'NO', 'PRI'),
    _col('tags', 'label', 'varchar', 'varchar(50)', 'NO'),
    _col('posts_tags', 'post_id', 'bigint', 'bigint', 'NO'),
    _col('posts_tags', 'tag_id', 'bigint', 'bigint', 'NO'),
    _col('likes', 'id', 'bigint', 'bigint', 'NO', 'PRI'),
    _col('likes', 'post_id', 'bigint', 'bigint', 'NO'),
    _col('likes', 'user_id', 'bigint', 'bigint', 'NO'),
    _col('likes', 'unknown_thing_id', 'bigint', 'bigint'),
    _col('old_items', 'legacy_id', 'bigint', 'bigint', 'NO', 'PRI'),
    _col('people', 'id', 'bigint', 'bigint', 'NO', 'PRI'),
    _col('audit_logs', 'id', 'bigint', 'bigint', 'NO', 'PRI'),
    _col('audit_logs', 'user_id', 'bigint', 'bigint'),
    # view column — no entry in TABLE_ROWS, must be skipped
    _col('v_recent', 'id', 'bigint', 'bigint', 'NO'),
]
FK_ROWS = [
    ('posts', 'user_id', 'users'),
    ('posts_tags', 'post_id', 'posts'),
    ('posts_tags', 'tag_id', 'tags'),
]
INDEX_ROWS = [
    ('users', 'PRIMARY', 0, 1, 'id'),
    ('users', 'index_users_on_email', 0, 1, 'email'),
    ('posts', 'index_posts_on_user_id_and_title', 1, 1, 'user_id'),
    ('posts', 'index_posts_on_user_id_and_title', 1, 2, 'title'),
]

def db_tables():
    return erd.mysql_ir(TABLE_ROWS, COL_ROWS, FK_ROWS, INDEX_ROWS)


class TestInflector(unittest.TestCase):
    def test_pluralize(self):
        cases = {'user': 'users', 'category': 'categories', 'status': 'statuses',
                 'box': 'boxes', 'branch': 'branches', 'leaf': 'leaves',
                 'person': 'people', 'child': 'children'}
        for word, expected in cases.items():
            self.assertEqual(erd.pluralize(word), expected, word)

    def test_to_snake(self):
        self.assertEqual(erd.to_snake('UserStat'), 'user_stat')
        self.assertEqual(erd.to_snake('APIKey'), 'api_key')

    def test_class_to_table(self):
        self.assertEqual(erd.class_to_table('User'), 'users')
        self.assertEqual(erd.class_to_table('Person'), 'people')
        self.assertEqual(erd.class_to_table('Admin::AuditLog'), 'audit_logs')


class TestParseMysqlUrl(unittest.TestCase):
    def test_missing_database_name_exits(self):
        with self.assertRaises(SystemExit) as cm:
            erd.parse_mysql('mysql://readonly@127.0.0.1:3306/')
        self.assertIn('database name', str(cm.exception))

    def test_non_word_database_name_exits(self):
        with self.assertRaises(SystemExit):
            erd.parse_mysql('mysql://readonly@127.0.0.1:3306/db;drop table x')

    def test_valid_url_queries_information_schema_for_db(self):
        seen = []
        orig = erd.mysql_query_rows
        erd.mysql_query_rows = lambda url, sql: (seen.append(sql) or [])
        try:
            erd.parse_mysql('mysql://readonly@127.0.0.1:3306/myapp_production')
        finally:
            erd.mysql_query_rows = orig
        self.assertEqual(len(seen), 4)  # tables, columns, FKs, indexes
        self.assertTrue(all("TABLE_SCHEMA='myapp_production'" in q for q in seen))

    def _parse_with_stubs(self, url, isatty, getpass_return='should-not-be-called'):
        """Run parse_mysql with mysql_query_rows/isatty/getpass stubbed out.
        Stub restoration is registered via addCleanup; MYSQL_PWD is left
        exactly as parse_mysql leaves it so tests can assert on it — callers
        own their own MYSQL_PWD setup/teardown."""
        orig_query = erd.mysql_query_rows
        orig_isatty = sys.stdin.isatty
        orig_getpass = erd.getpass.getpass
        self.addCleanup(lambda: setattr(erd, 'mysql_query_rows', orig_query))
        self.addCleanup(lambda: setattr(sys.stdin, 'isatty', orig_isatty))
        self.addCleanup(lambda: setattr(erd.getpass, 'getpass', orig_getpass))
        erd.mysql_query_rows = lambda url, sql: []
        sys.stdin.isatty = lambda: isatty

        def fake_getpass(prompt=''):
            self._getpass_prompt = prompt
            return getpass_return
        erd.getpass.getpass = fake_getpass
        erd.parse_mysql(url)

    def test_password_prompt_skipped_when_mysql_pwd_already_set(self):
        os.environ['MYSQL_PWD'] = 'preset'
        self.addCleanup(os.environ.pop, 'MYSQL_PWD', None)
        self._parse_with_stubs('mysql://readonly@127.0.0.1:3306/db', isatty=True)
        # getpass would have recorded a prompt if called; confirm it wasn't
        self.assertFalse(hasattr(self, '_getpass_prompt'))
        self.assertEqual(os.environ['MYSQL_PWD'], 'preset')  # left untouched

    def test_password_prompt_skipped_when_not_interactive(self):
        os.environ.pop('MYSQL_PWD', None)
        self._parse_with_stubs('mysql://readonly@127.0.0.1:3306/db', isatty=False)
        self.assertFalse(hasattr(self, '_getpass_prompt'))
        self.assertNotIn('MYSQL_PWD', os.environ)

    def test_password_prompted_when_interactive_and_unset(self):
        os.environ.pop('MYSQL_PWD', None)
        self.addCleanup(os.environ.pop, 'MYSQL_PWD', None)
        self._parse_with_stubs('mysql://readonly@127.0.0.1:3306/db',
                                isatty=True, getpass_return='hunter2')
        self.assertIn('readonly@127.0.0.1', self._getpass_prompt)
        self.assertEqual(os.environ.get('MYSQL_PWD'), 'hunter2')

    def test_password_prompt_skipped_when_url_has_explicit_empty_password(self):
        # mysql://user:@host/db — an explicit empty password should be
        # respected as "no password", not treated as "unset, please prompt"
        os.environ.pop('MYSQL_PWD', None)
        self._parse_with_stubs('mysql://readonly:@127.0.0.1:3306/db', isatty=True)
        self.assertFalse(hasattr(self, '_getpass_prompt'))


class TestMySQLIr(unittest.TestCase):
    def setUp(self):
        self.tables = db_tables()

    def test_views_are_skipped(self):
        self.assertNotIn('v_recent', self.tables)
        self.assertEqual(len(self.tables), len(TABLE_ROWS))

    def test_table_comment(self):
        self.assertEqual(self.tables['users']['comment'], 'User accounts')
        self.assertNotIn('comment', self.tables['posts'])

    def test_types_nullable_pk(self):
        cols = {c['name']: c for c in self.tables['users']['columns']}
        self.assertEqual(cols['email']['type'], 'string')
        self.assertEqual(cols['email']['sql_type'], 'varchar(255)')
        self.assertFalse(cols['email']['nullable'])
        self.assertTrue(cols['bio']['nullable'])
        self.assertTrue(cols['id']['primary'])
        self.assertEqual(cols['id']['extra'], 'auto_increment')
        self.assertEqual(self.tables['users']['primary_key'], 'id')
        self.assertEqual(self.tables['old_items']['primary_key'], 'legacy_id')

    def test_column_comment(self):
        cols = {c['name']: c for c in self.tables['users']['columns']}
        self.assertEqual(cols['email']['comment'], 'Login email address')
        self.assertNotIn('comment', cols['bio'])

    def test_db_fk_association(self):
        a = self.tables['posts']['associations'][0]
        self.assertEqual((a['type'], a['name'], a['target'], a['foreign_key']),
                         ('belongs_to', 'user', 'users', 'user_id'))
        self.assertTrue(a['db_fk'])

    def test_indexes(self):
        idx = {i['name']: i for i in self.tables['users']['indexes']}
        self.assertIn('PRIMARY', idx)
        self.assertTrue(idx['index_users_on_email']['unique'])
        multi = self.tables['posts']['indexes'][0]
        self.assertEqual(multi['columns'], ['user_id', 'title'])  # SEQ order
        self.assertFalse(multi['unique'])

    def test_db_fk_under_unique_index_is_1to1(self):
        # a FK column alone under a UNIQUE index can't repeat — real 1:1,
        # not the default many:1 a bare FK column implies
        table_rows = [('accounts', ''), ('profiles', '')]
        col_rows = [
            _col('accounts', 'id', 'bigint', 'bigint', 'NO', 'PRI'),
            _col('profiles', 'id', 'bigint', 'bigint', 'NO', 'PRI'),
            _col('profiles', 'account_id', 'bigint', 'bigint', 'NO', 'UNI'),
        ]
        fk_rows = [('profiles', 'account_id', 'accounts')]
        index_rows = [('profiles', 'uk_profiles_account_id', 0, 1, 'account_id')]
        tables = erd.mysql_ir(table_rows, col_rows, fk_rows, index_rows)
        a = tables['profiles']['associations'][0]
        self.assertEqual(a['type'], 'has_one')
        self.assertEqual(a['foreign_key'], 'account_id')


class TestOverlayAndInference(unittest.TestCase):
    def setUp(self):
        self.tables = db_tables()

    def test_rails_overlay_merges_associations(self):
        kind = erd.merge_code_semantics(self.tables, FIXTURE)
        self.assertEqual(kind, 'rails')
        names = {a['name'] for a in self.tables['users']['associations']}
        self.assertIn('posts', names)
        self.assertIn('commented_posts', names)  # has_many :through

    def test_belongs_to_backfills_conventional_foreign_key(self):
        # comment.rb: `belongs_to :post` / `belongs_to :user` — neither gives
        # an explicit foreign_key: option, so it must default to Rails
        # convention (<name>_id) rather than being left unset
        erd.merge_code_semantics(self.tables, FIXTURE)
        assocs = {a['name']: a for a in self.tables['comments']['associations']}
        self.assertEqual(assocs['post']['foreign_key'], 'post_id')
        self.assertEqual(assocs['user']['foreign_key'], 'user_id')
        # an explicit foreign_key: option is still honored, not overridden
        post_assocs = {a['name']: a for a in self.tables['posts']['associations']}
        self.assertEqual(post_assocs['author']['foreign_key'], 'user_id')

    def test_dedupe_after_overlay(self):
        erd.merge_code_semantics(self.tables, FIXTURE)
        removed = erd.dedupe_db_fk(self.tables)
        # posts.user_id DB FK is covered by the explicit belongs_to
        self.assertGreaterEqual(removed, 1)
        self.assertFalse(any(a.get('db_fk')
                             for a in self.tables['posts']['associations']))
        # posts_tags FKs stay: no model declares that pair
        self.assertTrue(any(a.get('db_fk')
                            for a in self.tables['posts_tags']['associations']))

    def test_model_without_db_table_flagged(self):
        erd.merge_code_semantics(self.tables, FIXTURE)
        self.assertIn('webhooks', self.tables)
        self.assertTrue(self.tables['webhooks']['schema_missing'])

    def test_dedupe_upgrades_lone_belongs_to_when_db_says_1to1(self):
        # simulates an incomplete Rails declaration: `belongs_to :account`
        # with no matching `has_one` on the other side, so the code alone
        # never asserts cardinality — but the DB has a unique index on the
        # FK column, so dedupe should promote the belongs_to to has_one
        # rather than just deleting the DB FK and losing that signal
        table_rows = [('accounts', ''), ('profiles', '')]
        col_rows = [
            _col('accounts', 'id', 'bigint', 'bigint', 'NO', 'PRI'),
            _col('profiles', 'id', 'bigint', 'bigint', 'NO', 'PRI'),
            _col('profiles', 'account_id', 'bigint', 'bigint', 'NO', 'UNI'),
        ]
        fk_rows = [('profiles', 'account_id', 'accounts')]
        index_rows = [('profiles', 'uk_profiles_account_id', 0, 1, 'account_id')]
        tables = erd.mysql_ir(table_rows, col_rows, fk_rows, index_rows)
        self.assertEqual(tables['profiles']['associations'][0]['type'], 'has_one')

        # an incomplete Rails-style overlay: only the belongs_to side declared
        tables['profiles']['associations'].append(
            {'type': 'belongs_to', 'name': 'account', 'target': 'accounts',
             'foreign_key': 'account_id'})

        removed = erd.dedupe_db_fk(tables)
        self.assertEqual(removed, 1)
        assocs = tables['profiles']['associations']
        self.assertEqual(len(assocs), 1, 'the DB FK should be dropped, not duplicated')
        self.assertEqual(assocs[0]['type'], 'has_one')
        self.assertNotIn('db_fk', assocs[0])  # still the explicit (code-declared) entry

    def test_dedupe_leaves_explicit_cardinality_alone(self):
        # when the code *does* declare cardinality (has_many here), dedupe
        # must not second-guess it even if the DB FK resolved to has_one
        table_rows = [('accounts', ''), ('profiles', '')]
        col_rows = [
            _col('accounts', 'id', 'bigint', 'bigint', 'NO', 'PRI'),
            _col('profiles', 'id', 'bigint', 'bigint', 'NO', 'PRI'),
            _col('profiles', 'account_id', 'bigint', 'bigint', 'NO', 'UNI'),
        ]
        fk_rows = [('profiles', 'account_id', 'accounts')]
        index_rows = [('profiles', 'uk_profiles_account_id', 0, 1, 'account_id')]
        tables = erd.mysql_ir(table_rows, col_rows, fk_rows, index_rows)
        tables['profiles']['associations'].append(
            {'type': 'belongs_to', 'name': 'account', 'target': 'accounts',
             'foreign_key': 'account_id'})
        tables['accounts']['associations'].append(
            {'type': 'has_many', 'name': 'profiles', 'target': 'profiles'})

        erd.dedupe_db_fk(tables)
        types = {a['type'] for a in tables['profiles']['associations']}
        self.assertEqual(types, {'belongs_to'})  # left as-is, not upgraded

    def test_inference_on_constraintless_fk(self):
        added = erd.infer_fk_associations(self.tables)
        targets = {a['target'] for a in self.tables['likes']['associations']}
        self.assertEqual(targets, {'posts', 'users'})
        self.assertTrue(all(a['inferred']
                            for a in self.tables['likes']['associations']))
        self.assertGreaterEqual(added, 2)

    def test_inference_skips_covered_pairs(self):
        erd.infer_fk_associations(self.tables)
        # posts.user_id has a DB FK — no duplicate inferred edge
        assocs = [a for a in self.tables['posts']['associations']
                  if a['target'] == 'users']
        self.assertEqual(len(assocs), 1)

    def test_inference_falls_back_to_singular_table_name(self):
        # Rails-style pluralized tables are tried first, but schemas that
        # don't pluralize (common outside Rails, e.g. Prisma defaults) should
        # still resolve — 'author_id' -> 'author' when 'authors' doesn't exist
        table_rows = [('posts', ''), ('author', '')]  # singular, deliberately
        col_rows = [
            _col('posts', 'id', 'bigint', 'bigint', 'NO', 'PRI'),
            _col('posts', 'author_id', 'bigint', 'bigint'),
            _col('author', 'id', 'bigint', 'bigint', 'NO', 'PRI'),
        ]
        tables = erd.mysql_ir(table_rows, col_rows, [], [])
        added = erd.infer_fk_associations(tables)
        self.assertEqual(added, 1)
        a = tables['posts']['associations'][0]
        self.assertEqual((a['target'], a['inferred']), ('author', True))

    def test_inference_detects_1to1_via_unique_index(self):
        table_rows = [('users', ''), ('profiles', '')]
        col_rows = [
            _col('users', 'id', 'bigint', 'bigint', 'NO', 'PRI'),
            _col('profiles', 'id', 'bigint', 'bigint', 'NO', 'PRI'),
            _col('profiles', 'user_id', 'bigint', 'bigint', 'NO', 'UNI'),
        ]
        index_rows = [('profiles', 'uk_profiles_user_id', 0, 1, 'user_id')]
        tables = erd.mysql_ir(table_rows, col_rows, [], index_rows)
        erd.infer_fk_associations(tables)
        a = tables['profiles']['associations'][0]
        self.assertEqual(a['type'], 'has_one')


class TestFkColumns(unittest.TestCase):
    """fk_columns (computed in _finish(), the single source of truth the FK
    badge and PK/FK column view read) must only ever contain columns backed
    by a real association — never a bare *_id name match. --infer-fk is the
    only thing that can widen it, and only for names that also resolve to a
    real table (comments.post_id/user_id do; likes.unknown_thing_id never
    can, since no `unknown_things` table exists)."""
    def _args(self, **kw):
        base = dict(output='', models=None, excel=None, max_rows=15,
                    only=None, exclude=None, infer_fk=False)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_default_excludes_unbacked_id_columns(self):
        tables = db_tables()
        with tempfile.TemporaryDirectory() as tmp:
            erd._finish(tables, self._args(output=str(Path(tmp) / 'out.html')), 'testdb')
        self.assertNotIn('post_id', tables['comments']['fk_columns'])
        self.assertNotIn('user_id', tables['comments']['fk_columns'])
        # posts.user_id has a real DB FK constraint — always included
        self.assertIn('user_id', tables['posts']['fk_columns'])

    def test_infer_fk_widens_it_to_matching_tables_only(self):
        tables = db_tables()
        with tempfile.TemporaryDirectory() as tmp:
            erd._finish(tables, self._args(output=str(Path(tmp) / 'out.html'), infer_fk=True),
                        'testdb')
        self.assertIn('post_id', tables['comments']['fk_columns'])
        self.assertIn('user_id', tables['comments']['fk_columns'])
        # no `unknown_things` table exists — never inferred, flag or not
        self.assertNotIn('unknown_thing_id', tables['likes']['fk_columns'])


class TestParsePrisma(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables = erd.parse_prisma(
            Path(__file__).resolve().parent / 'fixture_prisma' / 'schema.prisma')

    def test_models_found_with_map(self):
        self.assertEqual(set(self.tables), {'users', 'Profile', 'Post', 'Tag'})

    def test_relations(self):
        assocs = {a['name']: a for a in self.tables['Post']['associations']}
        self.assertEqual(assocs['author']['type'], 'belongs_to')
        self.assertEqual(assocs['author']['foreign_key'], 'authorId')
        self.assertEqual(assocs['tags']['type'], 'has_and_belongs_to_many')

    def test_at_unique_fk_field_is_1to1_not_manyto1(self):
        # Profile.userId is `Int @unique` — each value can appear at most
        # once, so it's a real 1:1, not the default many:1 a bare FK implies
        assocs = {a['name']: a for a in self.tables['Profile']['associations']}
        self.assertEqual(assocs['user']['type'], 'has_one')
        self.assertEqual(assocs['user']['foreign_key'], 'userId')
        # Post.authorId has no @unique — stays the default many:1
        post_assocs = {a['name']: a for a in self.tables['Post']['associations']}
        self.assertEqual(post_assocs['author']['type'], 'belongs_to')

    def test_prisma_as_overlay(self):
        tables = {'users': {'columns': [], 'associations': [],
                            'indexes': [], 'primary_key': 'id'}}
        kind = erd.merge_code_semantics(
            tables, Path(__file__).resolve().parent / 'fixture_prisma')
        self.assertEqual(kind, 'prisma')
        self.assertTrue(any(a['name'] == 'posts'
                            for a in tables['users']['associations']))
        self.assertTrue(tables['Post']['schema_missing'])


class TestParseDjango(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables = erd.parse_django(
            Path(__file__).resolve().parent / 'fixture_django')

    def test_tables_found(self):
        self.assertEqual(set(self.tables),
                         {'blog_author', 'blog_entries', 'blog_posttag',
                          'blog_tag', 'shop_product'})

    def test_relations(self):
        assocs = {a['name']: a for a in self.tables['blog_entries']['associations']}
        self.assertEqual(assocs['author']['target'], 'blog_author')
        self.assertEqual(assocs['parent']['target'], 'blog_entries')  # self
        self.assertEqual(assocs['tags']['through'], 'blog_posttag')

    def test_detect_code_source(self):
        base = Path(__file__).resolve().parent
        self.assertEqual(erd.detect_code_source(base / 'fixture_django'), 'django')
        self.assertEqual(erd.detect_code_source(base / 'fixture_prisma'), 'prisma')
        self.assertEqual(erd.detect_code_source(FIXTURE), 'rails')


class TestGeneration(unittest.TestCase):
    def _args(self, **kw):
        base = dict(output='', models=None, excel=None, max_rows=15,
                    only=None, exclude=None, infer_fk=False)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_html_generation(self):
        tables = db_tables()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out.html'
            erd._finish(tables, self._args(output=str(out)), 'testdb')
            html = out.read_text(encoding='utf-8')
        for ph in ('__DATA_JSON__', '__MAX_ROWS__', '__TITLE__'):
            self.assertNotIn(ph, html)
        self.assertIn('<title>testdb — ERD</title>', html)
        m = re.search(r'^const DATA = (.+);$', html, re.MULTILINE)
        data = json.loads(m.group(1))
        self.assertIn('users', data['tables'])
        self.assertEqual(data['tables']['users']['comment'], 'User accounts')
        self.assertTrue(data['tables']['users']['indexes'])

    def test_only_exclude(self):
        tables = db_tables()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out.html'
            erd._finish(tables, self._args(output=str(out), only=['post*,tags'],
                                           exclude=['posts_tags']), 'testdb')
            html = out.read_text(encoding='utf-8')
        data = json.loads(re.search(r'^const DATA = (.+);$', html,
                                    re.MULTILINE).group(1))
        self.assertEqual(set(data['tables']), {'posts', 'tags'})

    def test_cli_requires_database_url(self):
        res = subprocess.run([sys.executable, str(ROOT / 'erd.py'),
                              str(FIXTURE)], capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn('database URL is required', res.stderr)


class TestExcel(unittest.TestCase):
    def test_workbook_structure(self):
        tables = db_tables()
        erd.merge_code_semantics(tables, FIXTURE)
        erd.dedupe_db_fk(tables)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'defs.xlsx'
            erd.write_excel(tables, out, 'testdb')
            with zipfile.ZipFile(out) as z:
                names = z.namelist()
                self.assertIn('xl/workbook.xml', names)
                sheets = [n for n in names if n.startswith('xl/worksheets/')]
                # overview + one per table
                self.assertEqual(len(sheets), 1 + len(tables))
                wb = z.read('xl/workbook.xml').decode()
                self.assertIn('name="Tables"', wb)
                self.assertIn('name="users"', wb)
                users_sheet = None
                for i in range(len(sheets)):
                    xml = z.read(f'xl/worksheets/sheet{i+1}.xml').decode()
                    if '>Login email address<' in xml:
                        users_sheet = xml
                self.assertIsNotNone(users_sheet, 'column comment in a sheet')
                self.assertIn('varchar(255)', users_sheet)
                self.assertIn('index_users_on_email', users_sheet)
                self.assertIn('>UNIQUE<', users_sheet)
                overview = z.read('xl/worksheets/sheet1.xml').decode()
                self.assertIn('User accounts', overview)
                self.assertIn('<hyperlink', overview)

    def test_sheet_name_sanitizing(self):
        used = set()
        self.assertEqual(erd._sheet_name('a/b:c*d', used), 'a_b_c_d')
        long = erd._sheet_name('x' * 40, used)
        self.assertLessEqual(len(long), 31)
        dup = erd._sheet_name('a/b:c*d', used)
        self.assertNotEqual(dup, 'a_b_c_d')

    def test_openpyxl_roundtrip_if_available(self):
        try:
            import openpyxl
        except ImportError:
            self.skipTest('openpyxl not installed')
        tables = db_tables()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'defs.xlsx'
            erd.write_excel(tables, out, 'testdb')
            wb = openpyxl.load_workbook(out)
            self.assertEqual(wb.sheetnames[0], 'Tables')
            self.assertIn('users', wb.sheetnames)


if __name__ == '__main__':
    unittest.main()
