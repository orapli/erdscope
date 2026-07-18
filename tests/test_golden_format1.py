"""format-1 golden contract test.

--emit-json's canonical projection + content fingerprint (src/erdscope/
emit.py: canonical_schema / snapshot_fingerprint / emit_json_document) is a
STABLE CONTRACT other tools (diffing, digesting, DBML generation, whatever a
user builds against `sha256:...`) rely on staying put across erdscope
releases. tests/test_emit_json.py and tests/test_emit_config.py already unit-
test each individual rule (allowlist, falsy-omission, sort order, provenance
normalization, ...) in isolation, but nothing previously asserted the FULL,
assembled output byte-for-byte — a change that accidentally reorders two
rules, or drops a falsy-omission exception, could still pass every existing
unit test while silently changing every --emit-json/--emit-config user gets.

This file is that end-to-end guard: a hand-built, DB-free, fully
deterministic final-IR fixture (build_fixture_tables/_notes/_groups below)
covering every format-1 element in one shot, run through the real
emit_json_document(), and compared BYTE-FOR-BYTE against a committed golden
snapshot (tests/fixtures/format1_golden.json). Any unintended change to
canonical ordering, falsy-omission rules, or provenance normalization changes
the golden's `fingerprint` too (fingerprint is computed FROM canonical_schema,
same source of truth), so this one test catches every axis at once.

Fixture coverage (per the format-1 spec this locks in):
  - multiple tables (6: users, posts, comments, categories, organizations,
    memberships), some carrying a table `comment`, some not
  - columns exercising every optional key: sql_type, nullable (True and
    False), primary (single- and composite-PK), default, extra, comment —
    including one column (posts.legacy_flag) that carries ALL of sql_type/
    default/extra/comment at once
  - a composite primary key (memberships: org_id + user_id)
  - both a NAMED index (users.idx_users_email, unique) and an UNNAMED index
    (users on `status`; posts on `user_id`)
  - associations spanning all 5 provenance values (declared/manual/db_fk/
    schema_fk/inferred), with `sources` shapes ranging from a single {kind,
    provider} pair, to a multi-source union (db+framework agreeing), to an
    empty list (inferred — a post-merge heuristic, no contributing layer)
  - a polymorphic belongs_to with a synthetic, tableless target
    (comments.commentable -> "commentables")
  - a `through` association (users.comments -> posts)
  - a self-referential association (categories.parent -> categories)
  - notes across all THREE scopes (global / table / relation), including one
    with `links`, one without, one with `through` set, one polymorphic, and
    one with no `foreign_key` (a collection-role relation note)
  - groups, one with title+color, one with neither

HOW TO REGENERATE THE GOLDEN (only when format-1 is INTENTIONALLY changed —
never to make a failing test pass without understanding why it failed):
    python3 tests/test_golden_format1.py --write-golden
then inspect the diff of tests/fixtures/format1_golden.json in `git diff`
before committing it — that diff IS the format-1 changelog entry.

Run from the repository root:
    python3 -m unittest tests.test_golden_format1 -v
"""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN_PATH = ROOT / 'tests' / 'fixtures' / 'format1_golden.json'

spec = importlib.util.spec_from_file_location('erd', ROOT / 'erd.py')
erd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(erd)


def _col(name, type_, nullable, **extra):
    c = {'name': name, 'type': type_, 'nullable': nullable}
    c.update(extra)
    return c


def build_fixture_tables():
    """A hand-built final-merged-IR `tables` dict (associations carry
    'provenance'/'sources', like merge_ir's real output) — see the module
    docstring for exactly what it covers and why. Deterministic: no DB, no
    randomness, no wall-clock — every field is a literal."""
    return {
        'users': {
            'comment': 'Application users',
            'columns': [
                _col('id', 'bigint', False, primary=True,
                     sql_type='bigint unsigned', extra='auto_increment'),
                _col('email', 'string', False,
                     sql_type='varchar(255)', comment='login email'),
                _col('status', 'string', False,
                     sql_type='varchar(20)', default='active'),
                _col('bio', 'text', True, sql_type='text'),
            ],
            'indexes': [
                {'name': 'idx_users_email', 'columns': ['email'], 'unique': True},
                {'columns': ['status'], 'unique': False},  # unnamed
            ],
            'associations': [
                {'type': 'has_many', 'name': 'posts', 'target': 'posts',
                 'provenance': 'declared',
                 'sources': [{'kind': 'framework', 'provider': 'rails'}]},
                {'type': 'has_many', 'name': 'comments', 'target': 'comments',
                 'through': 'posts', 'provenance': 'declared',
                 'sources': [{'kind': 'framework', 'provider': 'rails'}]},
            ],
        },
        'posts': {
            # no table comment — falsy-omission coverage
            'columns': [
                _col('id', 'bigint', False, primary=True,
                     sql_type='bigint unsigned', extra='auto_increment'),
                _col('user_id', 'bigint', False, sql_type='bigint unsigned'),
                _col('category_id', 'bigint', True, sql_type='bigint unsigned'),
                _col('editor_id', 'bigint', True, sql_type='bigint unsigned'),
                _col('created_by_id', 'bigint', True, sql_type='bigint unsigned'),
                _col('title', 'string', False,
                     sql_type='varchar(255)', comment='post title'),
                _col('body', 'text', True),
                # exercises sql_type + default + extra + comment together
                _col('legacy_flag', 'integer', False,
                     sql_type='tinyint(1)', default='0', extra='unsigned',
                     comment='legacy migration flag'),
            ],
            'indexes': [
                {'columns': ['user_id'], 'unique': False},  # unnamed
            ],
            'associations': [
                {'type': 'belongs_to', 'name': 'user', 'target': 'users',
                 'foreign_key': 'user_id', 'provenance': 'db_fk',
                 'sources': [{'kind': 'db', 'provider': 'mysql'},
                            {'kind': 'framework', 'provider': 'rails'}]},
                {'type': 'belongs_to', 'name': 'category', 'target': 'categories',
                 'foreign_key': 'category_id', 'provenance': 'schema_fk',
                 'sources': [{'kind': 'schema', 'provider': 'rails.schema'}]},
                {'type': 'belongs_to', 'name': 'editor', 'target': 'users',
                 'foreign_key': 'editor_id', 'provenance': 'manual',
                 'sources': [{'kind': 'config', 'provider': 'config'}]},
                {'type': 'belongs_to', 'name': 'creator', 'target': 'users',
                 'foreign_key': 'created_by_id', 'provenance': 'inferred',
                 'sources': []},
                {'type': 'has_many', 'name': 'comments', 'target': 'comments',
                 'provenance': 'declared',
                 'sources': [{'kind': 'framework', 'provider': 'rails'}]},
            ],
        },
        'comments': {
            'comment': 'Comments on posts, or (polymorphically) other things',
            'columns': [
                _col('id', 'bigint', False, primary=True,
                     sql_type='bigint unsigned', extra='auto_increment'),
                _col('post_id', 'bigint', True, sql_type='bigint unsigned'),
                _col('commentable_id', 'bigint', False, sql_type='bigint unsigned'),
                _col('commentable_type', 'string', False, sql_type='varchar(60)'),
                _col('body', 'text', False),
            ],
            'indexes': [],
            'associations': [
                {'type': 'belongs_to', 'name': 'post', 'target': 'posts',
                 'foreign_key': 'post_id', 'provenance': 'db_fk',
                 'sources': [{'kind': 'db', 'provider': 'mysql'}]},
                # polymorphic: synthetic, tableless target — kept, not pruned
                {'type': 'belongs_to', 'name': 'commentable', 'target': 'commentables',
                 'foreign_key': 'commentable_id', 'polymorphic': True,
                 'provenance': 'declared',
                 'sources': [{'kind': 'framework', 'provider': 'rails'}]},
            ],
        },
        'categories': {
            'columns': [
                _col('id', 'bigint', False, primary=True,
                     sql_type='bigint unsigned', extra='auto_increment'),
                _col('parent_id', 'bigint', True, sql_type='bigint unsigned'),
                _col('name', 'string', False, sql_type='varchar(100)'),
            ],
            'indexes': [],
            'associations': [
                # self-referential
                {'type': 'belongs_to', 'name': 'parent', 'target': 'categories',
                 'foreign_key': 'parent_id', 'provenance': 'db_fk',
                 'sources': [{'kind': 'db', 'provider': 'mysql'}]},
                {'type': 'has_many', 'name': 'posts', 'target': 'posts',
                 'provenance': 'declared',
                 'sources': [{'kind': 'framework', 'provider': 'rails'}]},
            ],
        },
        'organizations': {
            'columns': [
                _col('id', 'bigint', False, primary=True,
                     sql_type='bigint unsigned', extra='auto_increment'),
                _col('name', 'string', False, sql_type='varchar(100)'),
            ],
            'indexes': [],
            'associations': [
                {'type': 'has_many', 'name': 'memberships', 'target': 'memberships',
                 'provenance': 'declared',
                 'sources': [{'kind': 'framework', 'provider': 'rails'}]},
            ],
        },
        'memberships': {
            'columns': [
                # composite primary key: org_id + user_id
                _col('org_id', 'bigint', False, primary=True, sql_type='bigint unsigned'),
                _col('user_id', 'bigint', False, primary=True, sql_type='bigint unsigned'),
                _col('role', 'string', True, sql_type='varchar(30)', default='member'),
            ],
            'indexes': [
                {'name': 'idx_memberships_role', 'columns': ['role'], 'unique': False},
            ],
            'associations': [
                {'type': 'belongs_to', 'name': 'org', 'target': 'organizations',
                 'foreign_key': 'org_id', 'provenance': 'db_fk',
                 'sources': [{'kind': 'db', 'provider': 'mysql'},
                            {'kind': 'framework', 'provider': 'rails'}]},
                {'type': 'belongs_to', 'name': 'member', 'target': 'users',
                 'foreign_key': 'user_id', 'provenance': 'db_fk',
                 'sources': [{'kind': 'db', 'provider': 'mysql'},
                            {'kind': 'framework', 'provider': 'rails'}]},
            ],
        },
    }


def build_fixture_notes():
    """Viewer-ready (already resolved, as resolve_and_validate_notes would
    return — this fixture skips that resolver and builds its OUTPUT shape
    directly, mirroring tests/test_emit_json.py's TestDeterminism style) notes
    covering all three scopes."""
    return [
        {'id': 'note-global', 'scope': 'global', 'title': 'Overview',
         'text': 'This schema models a small blog with org membership.',
         'links': [{'label': 'Design doc', 'url': 'https://example.com/design'}]},
        {'id': 'note-table-users', 'scope': 'table', 'table': 'users',
         'text': 'Users table stores auth + profile fields.'},
        {'id': 'note-rel-post-user', 'scope': 'relation',
         'source_table': 'posts', 'target': 'users', 'type': 'belongs_to',
         'name': 'user', 'foreign_key': 'user_id', 'through': None,
         'polymorphic': False, 'title': 'Author relation',
         'text': 'Every post has exactly one author.',
         'links': [{'url': 'https://example.com/authors'}]},
        {'id': 'note-rel-commentable', 'scope': 'relation',
         'source_table': 'comments', 'target': 'commentables',
         'type': 'belongs_to', 'name': 'commentable',
         'foreign_key': 'commentable_id', 'through': None,
         'polymorphic': True,
         'text': 'Polymorphic: may point at posts or other commentable types.'},
        {'id': 'note-rel-users-comments', 'scope': 'relation',
         'source_table': 'users', 'target': 'comments', 'type': 'has_many',
         'name': 'comments', 'foreign_key': None, 'through': 'posts',
         'polymorphic': False,
         'text': 'Indirect: a user\'s comments, reached via their posts.'},
    ]


def build_fixture_groups():
    """Viewer-ready groups — one with title+color, one with neither."""
    return [
        {'id': 'g-core', 'title': 'Core commerce', 'tables': ['posts', 'users'],
         'color': '#0d9488'},
        {'id': 'g-content', 'tables': ['categories', 'comments']},
    ]


def _write_golden():
    document = erd.emit_json_document(build_fixture_tables(), build_fixture_notes(),
                                      build_fixture_groups())
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_PATH.write_text(document, encoding='utf-8')
    print(f'Wrote {GOLDEN_PATH}')


class TestFormat1Golden(unittest.TestCase):
    """The contract: emit_json_document(fixture) must match the committed
    golden BYTE-FOR-BYTE. A failure here means format-1's canonical
    projection, ordering, falsy-omission rules, or fingerprint computation
    changed — see the module docstring for the intentional-update procedure
    before assuming this is simply a test to fix."""

    def test_matches_committed_golden_snapshot(self):
        self.assertTrue(GOLDEN_PATH.exists(),
                        f'{GOLDEN_PATH} is missing — run `python3 '
                        f'tests/test_golden_format1.py --write-golden` to generate it')
        actual = erd.emit_json_document(build_fixture_tables(), build_fixture_notes(),
                                        build_fixture_groups())
        expected = GOLDEN_PATH.read_text(encoding='utf-8')
        self.assertEqual(
            actual, expected,
            'format-1 output no longer matches the committed golden snapshot '
            f'({GOLDEN_PATH}). If this change to canonical_schema/'
            'snapshot_fingerprint/emit_json_document was INTENTIONAL, '
            'regenerate the golden with `python3 tests/test_golden_format1.py '
            '--write-golden` and review the resulting `git diff` before '
            'committing it — that diff is effectively the format-1 changelog.')

    def test_fingerprint_is_present_and_well_formed(self):
        # belt-and-suspenders: the fingerprint (which is what actually
        # detects a canonical-ordering/provenance-normalization change) is
        # baked into the golden file itself, but assert its shape directly
        # too so a golden that was hand-edited into something invalid is
        # still caught here.
        import json
        doc = json.loads(GOLDEN_PATH.read_text(encoding='utf-8'))
        fp = doc['fingerprint']
        self.assertTrue(fp.startswith('sha256:'))
        int(fp[len('sha256:'):], 16)  # raises ValueError if not valid hex


if __name__ == '__main__':
    if '--write-golden' in sys.argv:
        _write_golden()
    else:
        unittest.main()
