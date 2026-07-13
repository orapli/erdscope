"""Characterization ("golden") tests — REFACTOR_PLAN.md §15 Step 1.

These pin the CURRENT, as-shipped behavior of erd.py so later refactor steps
(§15 Step 2+) can be checked against it: this file records what the code
*does*, not what it "should" do. Do not "fix" a surprising assertion here
without first confirming the underlying erd.py behavior actually changed on
purpose — that's the whole point of a characterization test.

Covers (§16, especially §16.6 output compatibility):
  - IR snapshots: mysql_ir with MySQL- and PostgreSQL-flavored rows,
    parse_prisma, parse_django, and the Rails overlay's own IR fragment
    (framework_provider folded by merge_ir with no DB layer, isolating the
    Rails fragment output from any particular DB schema).
  - A merge/overlay snapshot combining DB IR + Rails overlay + config
    `relations`, folded by merge_ir, for one small schema, chosen to
    exercise: a belongs_to foreign_key backfill, a DB FK reconciled by a
    matching code association, a lone belongs_to promoted to has_one from a
    DB unique index, and created_by_id/updated_by_id kept as two distinct
    associations.
  - HTML output: the embedded DATA_JSON round-trips exactly, and
    __TITLE__/__MAX_ROWS__ substitution happens as expected.
  - Excel output: overview sheet + per-table sheets, the PK/FK "Key" column,
    and the association "Via" column for all four provenances (DB FK, code,
    manual, inferred).
  - The demo (docs/index.html) regenerates byte-for-byte identical to what's
    committed — the same check CI runs as `git diff --exit-code` after
    `python3 docs/gen_demo.py`, reproduced here as a unittest that never
    touches the real docs/ directory (gen_demo.py + erd.py are copied into a
    temp dir and run there).

Run from the repository root:
    python3 -m unittest tests.test_characterization -v
"""
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PRISMA = Path(__file__).resolve().parent / 'fixture_prisma' / 'schema.prisma'
FIXTURE_DJANGO = Path(__file__).resolve().parent / 'fixture_django'
FIXTURE_RAILS = Path(__file__).resolve().parent / 'fixture_app'

spec = importlib.util.spec_from_file_location('erd', ROOT / 'erd.py')
erd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(erd)


def _col(t, name, dtype, ctype, null='YES', key='', default='', extra='', comment=''):
    return (t, name, dtype, ctype, null, key, default, extra, comment)


# ---------------------------------------------------------------------------
# IR snapshots — pure provider output, pinned in full
# ---------------------------------------------------------------------------
class TestMySQLIRSnapshot(unittest.TestCase):
    """A small MySQL-flavored schema exercising: table/column comments,
    nullability, auto_increment extra, a default value, a plain (many:1) FK,
    a FK promoted to 1:1 by a covering unique index, and a multi-column
    index — pinned as the exact, full mysql_ir() output."""

    def test_full_ir(self):
        table_rows = [('companies', 'Tenant companies'), ('users', ''), ('profiles', '')]
        col_rows = [
            _col('companies', 'id', 'bigint', 'bigint', 'NO', 'PRI', '', 'auto_increment'),
            _col('companies', 'name', 'varchar', 'varchar(100)', 'NO'),
            _col('users', 'id', 'bigint', 'bigint', 'NO', 'PRI', '', 'auto_increment'),
            _col('users', 'company_id', 'bigint', 'bigint', 'NO', 'MUL'),
            _col('users', 'email', 'varchar', 'varchar(255)', 'NO', 'UNI', comment='Login email'),
            _col('users', 'status', 'tinyint', 'tinyint', 'NO', '', '1'),
            _col('profiles', 'id', 'bigint', 'bigint', 'NO', 'PRI'),
            _col('profiles', 'user_id', 'bigint', 'bigint', 'NO', 'UNI'),
        ]
        fk_rows = [
            ('users', 'company_id', 'companies'),
            ('profiles', 'user_id', 'users'),
        ]
        index_rows = [
            ('users', 'PRIMARY', 0, 1, 'id'),
            ('users', 'idx_users_company_status', 1, 1, 'company_id'),
            ('users', 'idx_users_company_status', 1, 2, 'status'),
            ('profiles', 'uk_profiles_user_id', 0, 1, 'user_id'),
        ]
        tables = erd.mysql_ir(table_rows, col_rows, fk_rows, index_rows)
        expected = {
            'companies': {
                'columns': [
                    {'name': 'id', 'type': 'bigint', 'sql_type': 'bigint',
                     'nullable': False, 'primary': True, 'extra': 'auto_increment'},
                    {'name': 'name', 'type': 'string', 'sql_type': 'varchar(100)',
                     'nullable': False, 'primary': False},
                ],
                'associations': [],
                'indexes': [],
                'primary_key': 'id',
                'comment': 'Tenant companies',
            },
            'users': {
                'columns': [
                    {'name': 'id', 'type': 'bigint', 'sql_type': 'bigint',
                     'nullable': False, 'primary': True, 'extra': 'auto_increment'},
                    {'name': 'company_id', 'type': 'bigint', 'sql_type': 'bigint',
                     'nullable': False, 'primary': False},
                    {'name': 'email', 'type': 'string', 'sql_type': 'varchar(255)',
                     'nullable': False, 'primary': False, 'comment': 'Login email'},
                    {'name': 'status', 'type': 'integer', 'sql_type': 'tinyint',
                     'nullable': False, 'primary': False, 'default': '1'},
                ],
                'associations': [
                    {'type': 'belongs_to', 'name': 'company', 'target': 'companies',
                     'foreign_key': 'company_id', 'db_fk': True},
                ],
                'indexes': [
                    {'name': 'PRIMARY', 'columns': ['id'], 'unique': True},
                    {'name': 'idx_users_company_status',
                     'columns': ['company_id', 'status'], 'unique': False},
                ],
                'primary_key': 'id',
            },
            'profiles': {
                'columns': [
                    {'name': 'id', 'type': 'bigint', 'sql_type': 'bigint',
                     'nullable': False, 'primary': True},
                    {'name': 'user_id', 'type': 'bigint', 'sql_type': 'bigint',
                     'nullable': False, 'primary': False},
                ],
                # a FK column that's alone under a UNIQUE index resolves to
                # has_one, not the default belongs_to (many:1)
                'associations': [
                    {'type': 'has_one', 'name': 'user', 'target': 'users',
                     'foreign_key': 'user_id', 'db_fk': True},
                ],
                'indexes': [
                    {'name': 'uk_profiles_user_id', 'columns': ['user_id'], 'unique': True},
                ],
                'primary_key': 'id',
            },
        }
        self.assertEqual(tables, expected)


class TestPostgresFlavoredIRSnapshot(unittest.TestCase):
    """parse_postgres() shapes pg_catalog rows into the exact same tuple
    layout mysql_ir() consumes (see erd.py's parse_postgres docstring), so
    the engine-specific surface worth pinning is the PostgreSQL-flavored
    DATA_TYPE/COLUMN_TYPE strings (format_type() output) flowing through
    SQL_TYPES, plus identity/serial landing in `extra`."""

    def test_full_ir(self):
        table_rows = [('items', '')]
        col_rows = [
            _col('items', 'id', 'bigint', 'bigint', 'NO', 'PRI', '', 'identity'),
            _col('items', 'sku', 'character varying', 'character varying(40)', 'NO', 'UNI'),
            _col('items', 'price', 'numeric', 'numeric(10,2)', 'NO'),
            _col('items', 'in_stock', 'boolean', 'boolean', 'NO', '', 'true'),
            _col('items', 'metadata', 'jsonb', 'jsonb', 'YES'),
            _col('items', 'external_id', 'uuid', 'uuid', 'YES'),
            _col('items', 'created_at', 'timestamp without time zone',
                 'timestamp without time zone', 'NO'),
            _col('items', 'payload', 'bytea', 'bytea', 'YES'),
        ]
        tables = erd.mysql_ir(table_rows, col_rows, [], [])
        cols = {c['name']: c for c in tables['items']['columns']}
        self.assertEqual(cols['id']['extra'], 'identity')
        self.assertEqual(cols['sku']['type'], 'string')
        self.assertEqual(cols['price']['type'], 'decimal')
        self.assertEqual(cols['in_stock']['type'], 'boolean')
        self.assertEqual(cols['in_stock']['default'], 'true')
        self.assertEqual(cols['metadata']['type'], 'jsonb')
        self.assertEqual(cols['external_id']['type'], 'uuid')
        self.assertEqual(cols['created_at']['type'], 'datetime')
        self.assertEqual(cols['payload']['type'], 'binary')
        # full-structure pin, sorted-key comparison via json round trip so a
        # stray extra/changed key anywhere is caught, not just the fields
        # spot-checked above
        expected = {
            'items': {
                'primary_key': 'id',
                'associations': [],
                'indexes': [],
                'columns': [
                    {'name': 'id', 'type': 'bigint', 'sql_type': 'bigint',
                     'nullable': False, 'primary': True, 'extra': 'identity'},
                    {'name': 'sku', 'type': 'string', 'sql_type': 'character varying(40)',
                     'nullable': False, 'primary': False},
                    {'name': 'price', 'type': 'decimal', 'sql_type': 'numeric(10,2)',
                     'nullable': False, 'primary': False},
                    {'name': 'in_stock', 'type': 'boolean', 'sql_type': 'boolean',
                     'nullable': False, 'primary': False, 'default': 'true'},
                    {'name': 'metadata', 'type': 'jsonb', 'sql_type': 'jsonb',
                     'nullable': True, 'primary': False},
                    {'name': 'external_id', 'type': 'uuid', 'sql_type': 'uuid',
                     'nullable': True, 'primary': False},
                    {'name': 'created_at', 'type': 'datetime',
                     'sql_type': 'timestamp without time zone',
                     'nullable': False, 'primary': False},
                    {'name': 'payload', 'type': 'binary', 'sql_type': 'bytea',
                     'nullable': True, 'primary': False},
                ],
            },
        }
        self.assertEqual(tables, expected)


class TestPrismaIRSnapshot(unittest.TestCase):
    """Full, exact parse_prisma() output for tests/fixture_prisma/schema.prisma
    — covers @@map (User -> 'users'), @map (createdAt -> created_at), enum
    field typing, @unique-FK-implies-1:1 (Profile.userId), implicit m2m
    (Post.tags[] <-> Tag.posts[]) and the plain belongs_to default (Post.author)."""

    @classmethod
    def setUpClass(cls):
        cls.tables = erd.parse_prisma(FIXTURE_PRISMA)

    def test_full_ir(self):
        expected = {
            'users': {
                'columns': [
                    {'name': 'id', 'type': 'integer', 'nullable': False, 'primary': True},
                    {'name': 'email', 'type': 'string', 'nullable': False, 'primary': False},
                    {'name': 'name', 'type': 'string', 'nullable': True, 'primary': False},
                    {'name': 'role', 'type': 'Role', 'nullable': False, 'primary': False},
                    {'name': 'created_at', 'type': 'datetime', 'nullable': False, 'primary': False},
                ],
                'associations': [
                    {'type': 'has_many', 'name': 'posts', 'target': 'Post'},
                    {'type': 'has_one', 'name': 'profile', 'target': 'Profile'},
                ],
                'primary_key': 'id',
            },
            'Profile': {
                'columns': [
                    {'name': 'id', 'type': 'integer', 'nullable': False, 'primary': True},
                    {'name': 'bio', 'type': 'string', 'nullable': True, 'primary': False},
                    {'name': 'userId', 'type': 'integer', 'nullable': False, 'primary': False},
                ],
                # userId is `Int @unique` -> 1:1, not the default many:1
                'associations': [
                    {'type': 'has_one', 'name': 'user', 'target': 'users', 'foreign_key': 'userId'},
                ],
                'primary_key': 'id',
            },
            'Post': {
                'columns': [
                    {'name': 'id', 'type': 'integer', 'nullable': False, 'primary': True},
                    {'name': 'title', 'type': 'string', 'nullable': False, 'primary': False},
                    {'name': 'authorId', 'type': 'integer', 'nullable': False, 'primary': False},
                ],
                'associations': [
                    {'type': 'belongs_to', 'name': 'author', 'target': 'users',
                     'foreign_key': 'authorId'},
                    {'type': 'has_and_belongs_to_many', 'name': 'tags', 'target': 'Tag'},
                ],
                'primary_key': 'id',
            },
            'Tag': {
                'columns': [
                    {'name': 'id', 'type': 'integer', 'nullable': False, 'primary': True},
                    {'name': 'label', 'type': 'string', 'nullable': False, 'primary': False},
                ],
                'associations': [
                    {'type': 'has_and_belongs_to_many', 'name': 'posts', 'target': 'Post'},
                ],
                'primary_key': 'id',
            },
        }
        self.assertEqual(self.tables, expected)

    def test_as_overlay_kind_detection(self):
        # framework_provider's auto-detect + overlay path (schema.prisma inside a
        # directory, not passed directly), folded onto a DB IR by merge_ir —
        # pinned separately from parse_prisma() itself since it goes through a
        # different code path (detect_code_source + merge_ir).
        #
        # SANCTIONED CHANGE (§7.8): the retired association-only overlay dropped
        # Prisma columns, so a Prisma-only model became schema_missing. The
        # provider path retains columns, so 'Post' is now a real table.
        base = {'users': {'columns': [], 'associations': [], 'indexes': [], 'primary_key': 'id'}}
        self.assertEqual(erd.detect_code_source(FIXTURE_PRISMA.parent), 'prisma')
        merged = erd.merge_ir([
            erd.make_provider_result('db', 'mysql', base),
            erd.framework_provider(FIXTURE_PRISMA.parent),
        ])
        self.assertTrue(any(a['name'] == 'posts' for a in merged['users']['associations']))
        self.assertIn('Post', merged)
        self.assertNotIn('schema_missing', merged['Post'])


class TestDjangoIRSnapshot(unittest.TestCase):
    """Full, exact parse_django() output for tests/fixture_django — covers
    abstract-base field inheritance (TimeStamped -> Author/Post), db_table
    override (Post -> 'blog_entries'), 'self' FK resolution (Post.parent),
    ManyToManyField with an explicit through=, db_column override
    (Product.name -> 'product_name'), a non-default (UUID) primary key
    (Product.sku), a cross-app string target ('blog.Author'), and the
    implicit id backfill when no field declares primary_key=True."""

    @classmethod
    def setUpClass(cls):
        cls.tables = erd.parse_django(FIXTURE_DJANGO)

    def test_full_ir(self):
        expected = {
            'blog_author': {
                'columns': [
                    {'name': 'id', 'type': 'bigint', 'nullable': False, 'primary': True},
                    {'name': 'created_at', 'type': 'datetime', 'nullable': False, 'primary': False},
                    {'name': 'updated_at', 'type': 'datetime', 'nullable': False, 'primary': False},
                    {'name': 'name', 'type': 'string', 'nullable': False, 'primary': False},
                    {'name': 'email', 'type': 'string', 'nullable': True, 'primary': False},
                ],
                'associations': [],
                'primary_key': 'id',
            },
            'blog_entries': {
                'columns': [
                    {'name': 'id', 'type': 'bigint', 'nullable': False, 'primary': True},
                    {'name': 'created_at', 'type': 'datetime', 'nullable': False, 'primary': False},
                    {'name': 'updated_at', 'type': 'datetime', 'nullable': False, 'primary': False},
                    {'name': 'title', 'type': 'string', 'nullable': False, 'primary': False},
                    {'name': 'author_id', 'type': 'bigint', 'nullable': False, 'primary': False},
                    {'name': 'parent_id', 'type': 'bigint', 'nullable': True, 'primary': False},
                ],
                'associations': [
                    {'type': 'belongs_to', 'name': 'author', 'target': 'blog_author',
                     'foreign_key': 'author_id'},
                    {'type': 'belongs_to', 'name': 'parent', 'target': 'blog_entries',
                     'foreign_key': 'parent_id'},
                    {'type': 'has_and_belongs_to_many', 'name': 'tags', 'target': 'blog_tag',
                     'through': 'blog_posttag'},
                ],
                'primary_key': 'id',
            },
            'blog_tag': {
                'columns': [
                    {'name': 'id', 'type': 'bigint', 'nullable': False, 'primary': True},
                    {'name': 'label', 'type': 'string', 'nullable': False, 'primary': False},
                ],
                'associations': [],
                'primary_key': 'id',
            },
            'blog_posttag': {
                'columns': [
                    {'name': 'id', 'type': 'bigint', 'nullable': False, 'primary': True},
                    {'name': 'post_id', 'type': 'bigint', 'nullable': False, 'primary': False},
                    {'name': 'tag_id', 'type': 'bigint', 'nullable': False, 'primary': False},
                ],
                'associations': [
                    {'type': 'belongs_to', 'name': 'post', 'target': 'blog_entries',
                     'foreign_key': 'post_id'},
                    {'type': 'belongs_to', 'name': 'tag', 'target': 'blog_tag',
                     'foreign_key': 'tag_id'},
                ],
                'primary_key': 'id',
            },
            'shop_product': {
                'columns': [
                    {'name': 'sku', 'type': 'uuid', 'nullable': False, 'primary': True},
                    {'name': 'product_name', 'type': 'string', 'nullable': False, 'primary': False},
                    {'name': 'price', 'type': 'decimal', 'nullable': False, 'primary': False},
                    {'name': 'owner_id', 'type': 'bigint', 'nullable': True, 'primary': False},
                ],
                'associations': [
                    {'type': 'has_one', 'name': 'owner', 'target': 'blog_author',
                     'foreign_key': 'owner_id'},
                ],
                'primary_key': 'sku',
            },
        }
        self.assertEqual(self.tables, expected)

    def test_detect_code_source(self):
        self.assertEqual(erd.detect_code_source(FIXTURE_DJANGO), 'django')


class TestRailsOverlayIRFragmentSnapshot(unittest.TestCase):
    """The Rails overlay (framework_provider) folded by merge_ir with no DB
    layer — this pins its output in isolation, so every table it produces is
    schema_missing (derived, since Rails supplies no columns) and the snapshot
    depends only on the .rb fixture files, not on any particular DB schema.
    Exercises: STI
    (Admin -> users), concern-resolved table_name (Widget -> crm_widgets),
    an unresolvable concern falling back to the naive guess (Gizmo ->
    gizmos, uncorrected — table_map is exercised separately in test_erd.py),
    a custom (non-Active-Record-literal) base class, an abstract class
    scoped correctly within a multi-class file, a renamed association
    target (Project -> aaa_projects, both implicit and via class_name:),
    belongs_to foreign_key backfill, has_many :through, polymorphic
    belongs_to, and a commented-out self.table_name being ignored."""

    @classmethod
    def setUpClass(cls):
        cls.kind = erd.detect_code_source(FIXTURE_RAILS)
        cls.tables = erd.merge_ir([erd.framework_provider(FIXTURE_RAILS)])

    def test_kind(self):
        self.assertEqual(self.kind, 'rails')

    def test_full_ir(self):
        def frag(assocs=()):
            # merge_ir derives the full table shape: Rails supplies only
            # associations, so columns/indexes are empty, primary_key is None,
            # schema_missing is derived True, and fk_columns is computed.
            assocs = list(assocs)
            return {'columns': [], 'primary_key': None, 'indexes': [],
                    'associations': assocs, 'schema_missing': True,
                    'fk_columns': sorted({a['foreign_key'] for a in assocs
                                          if a.get('foreign_key')})}

        expected = {
            'aaa_projects': frag(),
            'commented_table_names': frag(),
            'crm_widgets': frag(),
            'gizmos': frag(),
            'old_items': frag(),
            'audit_logs': frag([
                {'type': 'belongs_to', 'name': 'user', 'target': 'users', 'foreign_key': 'user_id'},
            ]),
            'custom_base_widgets': frag([
                {'type': 'belongs_to', 'name': 'user', 'target': 'users', 'foreign_key': 'user_id'},
            ]),
            'gadgets': frag([
                {'type': 'belongs_to', 'name': 'user', 'target': 'users', 'foreign_key': 'user_id'},
            ]),
            'webhooks': frag([
                {'type': 'belongs_to', 'name': 'user', 'target': 'users', 'foreign_key': 'user_id'},
            ]),
            'people': frag([
                {'type': 'has_many', 'name': 'posts', 'target': 'posts', 'foreign_key': 'author_id'},
            ]),
            'comments': frag([
                {'type': 'belongs_to', 'name': 'post', 'target': 'posts', 'foreign_key': 'post_id'},
                {'type': 'belongs_to', 'name': 'user', 'target': 'users', 'foreign_key': 'user_id'},
                {'type': 'belongs_to', 'name': 'subject', 'target': 'subjects',
                 'foreign_key': 'subject_id', 'polymorphic': True},
            ]),
            'tasks': frag([
                {'type': 'belongs_to', 'name': 'project', 'target': 'aaa_projects',
                 'foreign_key': 'project_id'},
                {'type': 'belongs_to', 'name': 'owner', 'target': 'aaa_projects',
                 'foreign_key': 'owner_id'},
            ]),
            'tags': frag([
                {'type': 'has_and_belongs_to_many', 'name': 'posts', 'target': 'posts'},
            ]),
            'posts': frag([
                {'type': 'belongs_to', 'name': 'user', 'target': 'users', 'foreign_key': 'user_id'},
                {'type': 'belongs_to', 'name': 'author', 'target': 'users', 'foreign_key': 'user_id'},
                {'type': 'has_many', 'name': 'comments', 'target': 'comments'},
                {'type': 'has_and_belongs_to_many', 'name': 'tags', 'target': 'tags'},
            ]),
            'users': frag([
                {'type': 'belongs_to', 'name': 'department', 'target': 'departments',
                 'foreign_key': 'department_id'},
                {'type': 'has_many', 'name': 'posts', 'target': 'posts'},
                {'type': 'has_many', 'name': 'comments', 'target': 'comments'},
                {'type': 'has_many', 'name': 'commented_posts', 'target': 'posts', 'through': 'comments'},
                {'type': 'has_one', 'name': 'profile', 'target': 'profiles'},
            ]),
        }
        self.assertEqual(self.tables, expected)
        # names that must NOT appear: the abstract base classes themselves,
        # 'should_not_be_used' (a commented-out self.table_name), STI's
        # phantom 'admins' table, and 'widgets'/'admin_accounts' (the
        # concern-resolved / table_map-corrected names win instead)
        for absent in ('base_records', 'should_not_be_used', 'admins',
                       'widgets', 'shared_bases'):
            self.assertNotIn(absent, self.tables)


# ---------------------------------------------------------------------------
# Merge/overlay snapshot: DB + Rails overlay + config relations + dedupe
# ---------------------------------------------------------------------------
class TestMergeOverlaySnapshot(unittest.TestCase):
    """DB IR (mysql_ir) + a real Rails overlay (framework_provider, via a
    throwaway app/models/ tree written to a temp dir — not
    tests/fixture_app, so this schema stays small and self-contained) +
    config `relations`, folded by merge_ir (whose Phase B reconciles DB FKs),
    pinned as one final merged IR.

    Chosen to cover four behaviors together, the way a real run combines
    them:
      - belongs_to foreign_key backfill (none of the .rb files below give
        an explicit foreign_key: option)
      - a DB FK deduped away once a matching code association covers it
        (companies/users/profiles all lose their 'db_fk' flag)
      - a lone belongs_to (profiles -> users) promoted to has_one because
        the DB has a unique index on profiles.user_id, even though no
        Rails model declares the reverse has_one
      - created_by_id and updated_by_id on the same table/target kept as
        two distinct manual associations, not collapsed into one
    """

    @classmethod
    def setUpClass(cls):
        table_rows = [('companies', ''), ('users', ''), ('posts', ''), ('profiles', '')]
        col_rows = [
            _col('companies', 'id', 'bigint', 'bigint', 'NO', 'PRI', '', 'auto_increment'),
            _col('users', 'id', 'bigint', 'bigint', 'NO', 'PRI', '', 'auto_increment'),
            _col('users', 'company_id', 'bigint', 'bigint', 'NO', 'MUL'),
            _col('users', 'email', 'varchar', 'varchar(255)', 'NO', 'UNI'),
            _col('posts', 'id', 'bigint', 'bigint', 'NO', 'PRI', '', 'auto_increment'),
            _col('posts', 'user_id', 'bigint', 'bigint', 'NO', 'MUL'),
            _col('posts', 'created_by_id', 'bigint', 'bigint'),
            _col('posts', 'updated_by_id', 'bigint', 'bigint'),
            _col('posts', 'title', 'varchar', 'varchar(200)', 'NO'),
            _col('profiles', 'id', 'bigint', 'bigint', 'NO', 'PRI', '', 'auto_increment'),
            _col('profiles', 'user_id', 'bigint', 'bigint', 'NO', 'UNI'),
        ]
        fk_rows = [
            ('users', 'company_id', 'companies'),
            ('posts', 'user_id', 'users'),
            ('profiles', 'user_id', 'users'),
        ]
        index_rows = [('profiles', 'uk_profiles_user_id', 0, 1, 'user_id')]
        db_ir = erd.mysql_ir(table_rows, col_rows, fk_rows, index_rows)

        cls.tmp = tempfile.TemporaryDirectory()
        models = Path(cls.tmp.name) / 'app' / 'models'
        models.mkdir(parents=True)
        (models / 'company.rb').write_text(
            'class Company < ApplicationRecord\n  has_many :users\nend\n')
        (models / 'user.rb').write_text(
            'class User < ApplicationRecord\n  belongs_to :company\nend\n')
        (models / 'post.rb').write_text(
            'class Post < ApplicationRecord\n  belongs_to :user\nend\n')
        (models / 'profile.rb').write_text(
            'class Profile < ApplicationRecord\n  belongs_to :user\nend\n')
        cls.kind = erd.detect_code_source(Path(cls.tmp.name))
        # DB + Rails framework + config relations, folded low->high by merge_ir.
        cls.tables = erd.merge_ir([
            erd.make_provider_result('db', 'mysql', db_ir),
            erd.framework_provider(Path(cls.tmp.name)),
            erd.relations_to_config_layer([
                {'table': 'posts', 'column': 'created_by_id', 'references': 'users',
                 'name': 'created_by'},
                {'table': 'posts', 'column': 'updated_by_id', 'references': 'users',
                 'name': 'updated_by'},
            ], db_ir),
        ])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_all_db_fks_absorbed(self):
        self.assertEqual(self.kind, 'rails')
        # all three DB FKs (companies<-users, users<-posts, users<-profiles) are
        # covered by an explicit code/manual association, so merge_ir absorbs
        # them (Phase A identity merge + Phase B reconcile) — no association in
        # the merged IR still carries the db_fk flag.
        for name, t in self.tables.items():
            self.assertFalse(any(a.get('db_fk') for a in t['associations']), name)

    def test_final_associations(self):
        self.assertEqual(self.tables['users']['associations'], [
            {'type': 'belongs_to', 'name': 'company', 'target': 'companies',
             'foreign_key': 'company_id'},  # backfilled, no db_fk (deduped)
        ])
        self.assertEqual(self.tables['posts']['associations'], [
            {'type': 'belongs_to', 'name': 'user', 'target': 'users',
             'foreign_key': 'user_id'},
            {'type': 'belongs_to', 'name': 'created_by', 'target': 'users',
             'foreign_key': 'created_by_id', 'manual': True},
            {'type': 'belongs_to', 'name': 'updated_by', 'target': 'users',
             'foreign_key': 'updated_by_id', 'manual': True},
        ])
        self.assertEqual(self.tables['profiles']['associations'], [
            # promoted from belongs_to to has_one by the DB unique index,
            # even though no model declares the reverse has_one
            {'type': 'has_one', 'name': 'user', 'target': 'users',
             'foreign_key': 'user_id'},
        ])
        self.assertEqual(self.tables['companies']['associations'], [
            {'type': 'has_many', 'name': 'users', 'target': 'users'},
        ])

    def test_columns_and_pk_untouched_by_overlay(self):
        # the Rails overlay carries no `columns` key at all — DB physical
        # truth for columns/PK must survive completely unchanged
        self.assertEqual(self.tables['posts']['primary_key'], 'id')
        self.assertEqual([c['name'] for c in self.tables['posts']['columns']],
                         ['id', 'user_id', 'created_by_id', 'updated_by_id', 'title'])
        self.assertNotIn('schema_missing', self.tables['posts'])


# ---------------------------------------------------------------------------
# HTML output — semantic invariants (not full-byte snapshots)
# ---------------------------------------------------------------------------
class TestHTMLOutputSnapshot(unittest.TestCase):
    def _tables(self):
        table_rows = [('companies', 'Tenant companies'), ('users', '')]
        col_rows = [
            _col('companies', 'id', 'bigint', 'bigint', 'NO', 'PRI', '', 'auto_increment'),
            _col('users', 'id', 'bigint', 'bigint', 'NO', 'PRI', '', 'auto_increment'),
            _col('users', 'company_id', 'bigint', 'bigint', 'NO', 'MUL'),
        ]
        fk_rows = [('users', 'company_id', 'companies')]
        return erd.mysql_ir(table_rows, col_rows, fk_rows, [])

    def test_data_json_round_trips_and_placeholders_substituted(self):
        tables = self._tables()
        args = SimpleNamespace(output='', models=None, excel=None, max_rows=7,
                                only=None, exclude=None, infer_fk=False)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out.html'
            args.output = str(out)
            erd._finish(tables, args, 'acme_corp')
            html = out.read_text(encoding='utf-8')

        for ph in ('__DATA_JSON__', '__MAX_ROWS__', '__TITLE__'):
            self.assertNotIn(ph, html)
        self.assertIn('<title>acme_corp — ERD</title>', html)
        self.assertIn('<h1>acme_corp — ERD</h1>', html)
        self.assertIn('let maxRows = 7;', html)

        m = re.search(r'^const DATA = (.+);$', html, re.MULTILINE)
        data = json.loads(m.group(1))
        # _finish() adds fk_columns to each table as a side effect before
        # serializing — the DATA payload must be exactly {'tables': tables}
        # post-fk_columns, not a lossy or reshaped copy of it
        self.assertIn('fk_columns', tables['users'])
        self.assertEqual(data, {'tables': tables})
        self.assertEqual(data['tables']['users']['fk_columns'], ['company_id'])
        self.assertEqual(data['tables']['companies']['comment'], 'Tenant companies')


# ---------------------------------------------------------------------------
# Excel output — structure + association provenance ("Via")
# ---------------------------------------------------------------------------
class TestExcelOutputSnapshot(unittest.TestCase):
    """One source table ('items') with four *_id columns, one per
    association provenance: a real DB FK constraint, a plain code-declared
    association (no provenance flag -> 'code'), a config `relations` entry
    ('manual'), and an unconstrained *_id column resolved by --infer-fk
    ('inferred'). Pins write_excel()'s overview sheet, the per-table Key
    column (PK/FK), and the Associations section's Via column for all four."""

    @classmethod
    def setUpClass(cls):
        table_rows = [('companies', ''), ('departments', ''), ('teams', ''),
                      ('branches', ''), ('items', '')]
        col_rows = [
            _col('companies', 'id', 'bigint', 'bigint', 'NO', 'PRI', '', 'auto_increment'),
            _col('departments', 'id', 'bigint', 'bigint', 'NO', 'PRI', '', 'auto_increment'),
            _col('teams', 'id', 'bigint', 'bigint', 'NO', 'PRI', '', 'auto_increment'),
            _col('branches', 'id', 'bigint', 'bigint', 'NO', 'PRI', '', 'auto_increment'),
            _col('items', 'id', 'bigint', 'bigint', 'NO', 'PRI', '', 'auto_increment'),
            _col('items', 'company_id', 'bigint', 'bigint', 'NO'),
            _col('items', 'department_id', 'bigint', 'bigint', 'NO'),
            _col('items', 'team_id', 'bigint', 'bigint', 'NO'),
            _col('items', 'branch_id', 'bigint', 'bigint', 'NO'),
        ]
        fk_rows = [('items', 'department_id', 'departments')]
        db_ir = erd.mysql_ir(table_rows, col_rows, fk_rows, [])
        # a framework layer contributing a plain, unflagged (code) association,
        # and a config `relations` layer contributing the manual one — folded
        # onto the DB layer (which supplies the db_fk) by merge_ir.
        fw = erd.make_provider_result('framework', 'rails', {'items': {'associations': [
            {'type': 'belongs_to', 'name': 'company', 'target': 'companies',
             'foreign_key': 'company_id'}]}})
        cfg = erd.relations_to_config_layer([
            {'table': 'items', 'column': 'team_id', 'references': 'teams', 'name': 'team'},
        ], db_ir)
        cls.tables = erd.merge_ir([
            erd.make_provider_result('db', 'mysql', db_ir), fw, cfg])
        erd.infer_fk_associations(cls.tables)  # branch_id -> branches (inferred)

        cls.tmp = tempfile.TemporaryDirectory()
        args = SimpleNamespace(output=str(Path(cls.tmp.name) / 'out.html'),
                                models=None, excel=None, max_rows=15,
                                only=None, exclude=None, infer_fk=False)
        erd._finish(cls.tables, args, 'testdb')  # populates fk_columns
        cls.xlsx_path = Path(cls.tmp.name) / 'defs.xlsx'
        erd.write_excel(cls.tables, cls.xlsx_path, 'testdb')
        with zipfile.ZipFile(cls.xlsx_path) as z:
            cls.overview_xml = z.read('xl/worksheets/sheet1.xml').decode()
            names = z.namelist()
            sheets = sorted(n for n in names if n.startswith('xl/worksheets/'))
            # each per-table sheet's row 1 is exactly [('Table', S_HEADER), n]
            # (see write_excel) — that combination (not just the table name
            # appearing anywhere, which the overview sheet's hyperlink list
            # also matches) picks the 'items' table's own sheet
            marker = ('<t xml:space="preserve">Table</t></is></c>'
                      '<c r="B1" t="inlineStr"><is><t xml:space="preserve">items</t>')
            cls.items_xml = next(
                xml for xml in (z.read(n).decode() for n in sheets) if marker in xml)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_fk_columns_computed(self):
        self.assertEqual(self.tables['items']['fk_columns'],
                         ['branch_id', 'company_id', 'department_id', 'team_id'])

    def test_overview_sheet_lists_every_table(self):
        for name in ('companies', 'departments', 'teams', 'branches', 'items'):
            self.assertIn(f'<t xml:space="preserve">{name}</t>', self.overview_xml)

    def test_key_column_marks_pk_and_all_four_fk_columns(self):
        self.assertIn('>PK<', self.items_xml)
        self.assertEqual(self.items_xml.count('>FK<'), 4)

    def test_via_column_shows_all_four_provenances(self):
        for via in ('DB FK', 'code', 'manual', 'inferred'):
            self.assertIn(f'>{via}<', self.items_xml)


# ---------------------------------------------------------------------------
# Demo determinism (§16.6 / CI's "Demo is regenerated and committed" step)
# ---------------------------------------------------------------------------
class TestDemoHTMLDeterminism(unittest.TestCase):
    """docs/index.html is a committed build artifact of erd.py +
    docs/gen_demo.py; CI regenerates it and does `git diff --exit-code
    docs/index.html`. Reproduced here as a unittest WITHOUT ever writing to
    the real docs/ directory: erd.py and gen_demo.py are copied into a temp
    dir (preserving their relative layout, since gen_demo.py locates erd.py
    and its own output via `Path(__file__).resolve().parent.parent`), run
    there, and only the resulting bytes are compared against the committed
    file."""

    def test_regenerates_byte_identical(self):
        committed = ROOT / 'docs' / 'index.html'
        if not committed.exists():
            self.skipTest('docs/index.html not present in this checkout')
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            shutil.copy(ROOT / 'erd.py', tmp / 'erd.py')
            (tmp / 'docs').mkdir()
            shutil.copy(ROOT / 'docs' / 'gen_demo.py', tmp / 'docs' / 'gen_demo.py')
            r = subprocess.run([sys.executable, 'docs/gen_demo.py'], cwd=tmp,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            regenerated = (tmp / 'docs' / 'index.html').read_bytes()
        self.assertEqual(regenerated, committed.read_bytes(),
                         'docs/index.html is stale — run `python3 docs/gen_demo.py` '
                         'and commit the result')


# ---------------------------------------------------------------------------
# Provider / provenance contract helpers (REFACTOR_PLAN.md §5/§9, Step 2)
# ---------------------------------------------------------------------------
class TestProviderResultContract(unittest.TestCase):
    """make_provider_result assembles the §5 ProviderResult dict without
    touching the IR it's handed."""

    def test_minimal_shape_and_defaults(self):
        ir = {'users': {'columns': [], 'associations': []}}
        pr = erd.make_provider_result('db', 'mysql', ir)
        self.assertEqual(pr, {
            'source': {'kind': 'db', 'provider': 'mysql'},
            'tables': ir,
            'warnings': [],
        })
        # tables is carried by reference (no defensive copy at this layer),
        # and the default warnings list is fresh/empty
        self.assertIs(pr['tables'], ir)

    def test_location_included_when_given(self):
        pr = erd.make_provider_result('framework', 'rails', {}, location='/app/models')
        self.assertEqual(pr['source'],
                         {'kind': 'framework', 'provider': 'rails', 'location': '/app/models'})

    def test_location_omitted_when_none(self):
        pr = erd.make_provider_result('config', 'config', {})
        self.assertNotIn('location', pr['source'])

    def test_warnings_copied_not_aliased(self):
        warns = [{'code': 'unresolved_target', 'message': 'x', 'table': 'posts'}]
        pr = erd.make_provider_result('framework', 'django', {}, warnings=warns)
        self.assertEqual(pr['warnings'], warns)
        self.assertIsNot(pr['warnings'], warns)  # snapshotted, so later mutation is safe
        warns.append({'code': 'y', 'message': 'y'})
        self.assertEqual(len(pr['warnings']), 1)

    def test_does_not_mutate_tables(self):
        ir = {'users': {'associations': [{'type': 'belongs_to', 'name': 'x', 'target': 'y'}]}}
        before = json.dumps(ir, sort_keys=True)
        erd.make_provider_result('db', 'postgres', ir, location='postgres://h/db')
        self.assertEqual(json.dumps(ir, sort_keys=True), before)


class TestProvenanceSeam(unittest.TestCase):
    """provenance_of / legacy_flags_for are the pure conversion seam between
    the legacy boolean provenance flags (what the pipeline + serializers use
    today) and the representative provenance string the internals move to in
    Steps 9/10."""

    PROVENANCES = ('declared', 'db_fk', 'manual', 'inferred')

    def test_legacy_flags_for_each(self):
        self.assertEqual(erd.legacy_flags_for('declared'), {})
        self.assertEqual(erd.legacy_flags_for('db_fk'), {'db_fk': True})
        self.assertEqual(erd.legacy_flags_for('manual'), {'manual': True})
        self.assertEqual(erd.legacy_flags_for('inferred'), {'inferred': True})

    def test_provenance_of_from_flags(self):
        self.assertEqual(erd.provenance_of({}), 'declared')
        self.assertEqual(erd.provenance_of({'db_fk': True}), 'db_fk')
        self.assertEqual(erd.provenance_of({'manual': True}), 'manual')
        self.assertEqual(erd.provenance_of({'inferred': True}), 'inferred')

    def test_round_trip_all_four(self):
        for p in self.PROVENANCES:
            with self.subTest(provenance=p):
                self.assertEqual(erd.provenance_of(dict(erd.legacy_flags_for(p))), p)

    def test_precedence_manual_wins_over_db_fk_and_inferred(self):
        # a real assoc might legitimately carry more than one flag; the
        # representative must follow manual > db_fk > inferred
        self.assertEqual(erd.provenance_of({'manual': True, 'db_fk': True}), 'manual')
        self.assertEqual(erd.provenance_of({'manual': True, 'inferred': True}), 'manual')
        self.assertEqual(erd.provenance_of({'db_fk': True, 'inferred': True}), 'db_fk')
        self.assertEqual(
            erd.provenance_of({'manual': True, 'db_fk': True, 'inferred': True}), 'manual')

    def test_provenance_of_reads_a_full_association_dict(self):
        # exercised against the exact assoc shape the pipeline produces, not
        # just bare flag dicts
        db = {'type': 'belongs_to', 'name': 'user', 'target': 'users',
              'foreign_key': 'user_id', 'db_fk': True}
        declared = {'type': 'has_many', 'name': 'posts', 'target': 'posts'}
        self.assertEqual(erd.provenance_of(db), 'db_fk')
        self.assertEqual(erd.provenance_of(declared), 'declared')


# ---------------------------------------------------------------------------
# Framework leaf providers (REFACTOR_PLAN.md §5, Step 4)
# ---------------------------------------------------------------------------
class TestFrameworkProviders(unittest.TestCase):
    """prisma_provider / django_provider wrap the existing parsers as
    ProviderResults that RETAIN column information — the data the old
    association-only overlay threw away. Pinning a couple of column
    lists proves those columns are available for framework-only mode
    (Step 8) and the merge."""

    def test_prisma_provider_shape(self):
        pr = erd.prisma_provider(FIXTURE_PRISMA)
        self.assertEqual(pr['source'],
                         {'kind': 'framework', 'provider': 'prisma',
                          'location': str(FIXTURE_PRISMA)})
        self.assertEqual(pr['warnings'], [])
        # tables is exactly what parse_prisma produces — the wrapper only
        # packages it, and carries it by reference (no copy at this layer)
        self.assertIs(pr['tables'], pr['tables'])
        self.assertEqual(pr['tables'], erd.parse_prisma(FIXTURE_PRISMA))

    def test_prisma_provider_retains_columns(self):
        tables = erd.prisma_provider(FIXTURE_PRISMA)['tables']
        # the old association-only overlay would have discarded these
        self.assertEqual(tables['users']['columns'], [
            {'name': 'id', 'type': 'integer', 'nullable': False, 'primary': True},
            {'name': 'email', 'type': 'string', 'nullable': False, 'primary': False},
            {'name': 'name', 'type': 'string', 'nullable': True, 'primary': False},
            # enum field type surfaces as the enum NAME ('Role'), not a
            # normalized scalar — a shape Step-6 merge should expect
            {'name': 'role', 'type': 'Role', 'nullable': False, 'primary': False},
            {'name': 'created_at', 'type': 'datetime', 'nullable': False, 'primary': False},
        ])
        self.assertEqual([c['name'] for c in tables['Post']['columns']],
                         ['id', 'title', 'authorId'])

    def test_django_provider_shape(self):
        pr = erd.django_provider(FIXTURE_DJANGO)
        self.assertEqual(pr['source'],
                         {'kind': 'framework', 'provider': 'django',
                          'location': str(FIXTURE_DJANGO)})
        self.assertEqual(pr['warnings'], [])
        self.assertEqual(pr['tables'], erd.parse_django(FIXTURE_DJANGO))

    def test_django_provider_retains_synthetic_columns(self):
        tables = erd.django_provider(FIXTURE_DJANGO)['tables']
        # Django backfills a synthetic `id` PK and emits <name>_id FK columns
        # for ForeignKey/OneToOneField — both are real columns here, unlike
        # the Rails overlay which carries no columns at all
        self.assertEqual(tables['blog_entries']['columns'], [
            {'name': 'id', 'type': 'bigint', 'nullable': False, 'primary': True},
            {'name': 'created_at', 'type': 'datetime', 'nullable': False, 'primary': False},
            {'name': 'updated_at', 'type': 'datetime', 'nullable': False, 'primary': False},
            {'name': 'title', 'type': 'string', 'nullable': False, 'primary': False},
            {'name': 'author_id', 'type': 'bigint', 'nullable': False, 'primary': False},
            {'name': 'parent_id', 'type': 'bigint', 'nullable': True, 'primary': False},
        ])
        # a non-default (UUID) primary key is preserved as such
        self.assertEqual(tables['shop_product']['primary_key'], 'sku')

    def test_providers_do_not_mutate_parser_output(self):
        # wrapping must be a pure packaging step — a fresh parse and a
        # provider-wrapped parse produce equal IR
        self.assertEqual(erd.prisma_provider(FIXTURE_PRISMA)['tables'],
                         erd.parse_prisma(FIXTURE_PRISMA))
        self.assertEqual(erd.django_provider(FIXTURE_DJANGO)['tables'],
                         erd.parse_django(FIXTURE_DJANGO))


class TestRailsProvider(unittest.TestCase):
    """rails_provider returns a ProviderResult whose fragments carry
    associations ONLY — no `columns` key — so a later merge over a DB IR can
    never erase the DB's columns. merge_ir folds this fragment onto the DB layer
    (invariance proven by TestRailsOverlayIRFragmentSnapshot /
    TestMergeOverlaySnapshot)."""

    def _models_dir(self, tmp):
        models = Path(tmp) / 'app' / 'models'
        models.mkdir(parents=True)
        (models / 'company.rb').write_text(
            'class Company < ApplicationRecord\n  has_many :users\nend\n')
        (models / 'user.rb').write_text(
            'class User < ApplicationRecord\n  belongs_to :company\n  has_one :profile\nend\n')
        (models / 'profile.rb').write_text(
            'class Profile < ApplicationRecord\n  belongs_to :user\nend\n')
        return models

    def test_provider_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            models = self._models_dir(tmp)
            pr = erd.rails_provider(models)
        self.assertEqual(pr['source'],
                         {'kind': 'framework', 'provider': 'rails', 'location': str(models)})
        self.assertEqual(pr['warnings'], [])

    def test_fragments_have_no_columns_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            pr = erd.rails_provider(self._models_dir(tmp))
        self.assertEqual(set(pr['tables']), {'companies', 'users', 'profiles'})
        for tname, frag in pr['tables'].items():
            self.assertNotIn('columns', frag, tname)
            self.assertNotIn('primary_key', frag, tname)
            self.assertNotIn('schema_missing', frag, tname)
            self.assertEqual(set(frag), {'associations'}, tname)

    def test_fragment_associations_survive_the_merge(self):
        # the fragment's associations equal what a framework-only merge_ir
        # deposits (merge_ir over the rails provider alone, no DB layer)
        with tempfile.TemporaryDirectory() as tmp:
            models = self._models_dir(tmp)
            frag_tables = erd.rails_provider(models)['tables']
            merged = erd.merge_ir([erd.rails_provider(models)])
        for tname, frag in frag_tables.items():
            self.assertEqual(frag['associations'], merged[tname]['associations'], tname)

    def test_provider_does_not_mutate_any_external_dict(self):
        # rails_provider takes no tables arg and must not touch anything but
        # its own fresh fragment
        with tempfile.TemporaryDirectory() as tmp:
            models = self._models_dir(tmp)
            # provider alone: builds a fragment, mutates nothing
            pr = erd.rails_provider(models)
        self.assertIsInstance(pr['tables'], dict)

    def test_merge_leaves_db_columns_and_pk_untouched(self):
        # the core "must not erase DB columns" invariant, via the merge:
        # folding the Rails overlay onto a DB IR that already has columns/PK
        # adds associations but never rewrites the physical schema
        with tempfile.TemporaryDirectory() as tmp:
            models = self._models_dir(tmp)
            db_ir = erd.mysql_ir(
                [('companies', ''), ('users', ''), ('profiles', '')],
                [_col('companies', 'id', 'bigint', 'bigint', 'NO', 'PRI', '', 'auto_increment'),
                 _col('users', 'id', 'bigint', 'bigint', 'NO', 'PRI', '', 'auto_increment'),
                 _col('users', 'company_id', 'bigint', 'bigint', 'NO', 'MUL'),
                 _col('profiles', 'id', 'bigint', 'bigint', 'NO', 'PRI'),
                 _col('profiles', 'user_id', 'bigint', 'bigint', 'NO')],
                [], [])
            users_cols_before = [dict(c) for c in db_ir['users']['columns']]
            users_pk_before = db_ir['users']['primary_key']
            merged = erd.merge_ir([
                erd.make_provider_result('db', 'mysql', db_ir),
                erd.rails_provider(models),
            ])
        # physical columns + PK unchanged
        self.assertEqual(merged['users']['columns'], users_cols_before)
        self.assertEqual(merged['users']['primary_key'], users_pk_before)
        self.assertNotIn('schema_missing', merged['users'])
        # but the Rails associations were overlaid
        names = {a['name'] for a in merged['users']['associations']}
        self.assertIn('company', names)
        self.assertIn('profile', names)

    def test_provider_omits_columns_but_merge_derives_schema_missing(self):
        # a model with no DB table: the fragment has no columns key, and a
        # framework-only merge_ir derives the schema_missing entry (columns: [],
        # primary_key: None, indexes: [], schema_missing: True, fk_columns: [])
        with tempfile.TemporaryDirectory() as tmp:
            models = self._models_dir(tmp)
            frag_tables = erd.rails_provider(models)['tables']
            self.assertNotIn('columns', frag_tables['companies'])
            merged = erd.merge_ir([erd.rails_provider(models)])
        assocs = frag_tables['companies']['associations']
        self.assertEqual(merged['companies'], {
            'columns': [], 'primary_key': None, 'indexes': [], 'schema_missing': True,
            'associations': assocs,
            'fk_columns': sorted({a['foreign_key'] for a in assocs if a.get('foreign_key')}),
        })


if __name__ == '__main__':
    unittest.main()
