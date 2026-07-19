"""SQLAlchemy overlay (backlog F1) semantics — unit tests, in the same format
as test_characterization.py's Django section: a pinned full-IR snapshot over
a purpose-built fixture (tests/fixture_sqlalchemy/), plus focused tests for
detection, the provider wrapper, and the FK/relationship() dedup rule.

The shared cross-provider contract (1:N/1:1/M:N/self-reference over the
users/posts/profiles/tags domain, including the ForeignKey+relationship()
dedup case) lives in test_provider_contract.py's TestSQLAlchemyContract —
this file is for the parser's own, more nuanced semantics: abstract-base and
plain-mixin column inheritance, a physical column name override, the
mapped_column() 2.0 style, an unresolvable relationship()/ForeignKey target,
a missing __tablename__ fallback, and unknown-type passthrough.

Run from the repository root:
    python3 -m unittest tests.test_sqlalchemy_provider -v
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / 'fixture_sqlalchemy'
FIXTURE_CONTRACT = Path(__file__).resolve().parent / 'fixture_contract' / 'sqlalchemy'

spec = importlib.util.spec_from_file_location('erd', ROOT / 'erd.py')
erd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(erd)


class TestSQLAlchemyIRSnapshot(unittest.TestCase):
    """Full, exact parse_sqlalchemy() output for tests/fixture_sqlalchemy —
    covers plain-mixin field inheritance (TimestampMixin -> Team/User),
    __abstract__ base field inheritance (AuditedBase -> User), a physical
    column name override (Team.name -> 'team_name'), the mapped_column()
    2.0 style (User.id/email), a unique FK promoted to has_one (Account ->
    User), many-to-many via secondary=<Table variable> (Team<->User), a
    missing __tablename__ falling back to to_snake(classname) with a
    warning (Account), an unresolvable relationship() target skipped
    silently (Order.reviewer), an unresolvable ForeignKey() target keeping
    its column but skipping the edge (Order.approver_id), an untyped FK
    column defaulting to bigint (Widget.team_id), and an unmapped type name
    passing through lowercased (Widget.state -> 'moneytype')."""

    @classmethod
    def setUpClass(cls):
        cls.tables, cls.warnings = erd.parse_sqlalchemy(FIXTURE)

    def test_full_ir(self):
        expected = {
            'teams': {
                'columns': [
                    {'name': 'created_at', 'type': 'string', 'nullable': True, 'primary': False},
                    {'name': 'updated_at', 'type': 'string', 'nullable': True, 'primary': False},
                    {'name': 'id', 'type': 'integer', 'nullable': False, 'primary': True},
                    {'name': 'team_name', 'type': 'string', 'nullable': True, 'primary': False},
                ],
                'associations': [
                    {'type': 'has_and_belongs_to_many', 'name': 'members',
                     'target': 'users', 'through': 'team_members'},
                ],
                'primary_key': 'id',
            },
            'users': {
                'columns': [
                    {'name': 'note', 'type': 'string', 'nullable': True, 'primary': False},
                    {'name': 'created_at', 'type': 'string', 'nullable': True, 'primary': False},
                    {'name': 'updated_at', 'type': 'string', 'nullable': True, 'primary': False},
                    {'name': 'id', 'type': 'integer', 'nullable': False, 'primary': True},
                    {'name': 'email', 'type': 'string', 'nullable': True, 'primary': False},
                    {'name': 'balance', 'type': 'decimal', 'nullable': True, 'primary': False},
                ],
                'associations': [
                    {'type': 'has_and_belongs_to_many', 'name': 'teams',
                     'target': 'teams', 'through': 'team_members'},
                ],
                'primary_key': 'id',
            },
            'account': {
                'columns': [
                    {'name': 'id', 'type': 'integer', 'nullable': False, 'primary': True},
                    {'name': 'owner_id', 'type': 'integer', 'nullable': True, 'primary': False},
                ],
                'associations': [
                    {'type': 'has_one', 'name': 'owner', 'target': 'users', 'foreign_key': 'owner_id'},
                ],
                'primary_key': 'id',
            },
            'orders': {
                'columns': [
                    {'name': 'id', 'type': 'integer', 'nullable': False, 'primary': True},
                    {'name': 'account_id', 'type': 'integer', 'nullable': True, 'primary': False},
                    {'name': 'approver_id', 'type': 'integer', 'nullable': True, 'primary': False},
                ],
                'associations': [
                    {'type': 'belongs_to', 'name': 'account', 'target': 'account',
                     'foreign_key': 'account_id'},
                ],
                'primary_key': 'id',
            },
            'widgets': {
                'columns': [
                    {'name': 'id', 'type': 'integer', 'nullable': False, 'primary': True},
                    {'name': 'state', 'type': 'moneytype', 'nullable': True, 'primary': False},
                    {'name': 'team_id', 'type': 'bigint', 'nullable': True, 'primary': False},
                ],
                'associations': [
                    {'type': 'belongs_to', 'name': 'team', 'target': 'teams', 'foreign_key': 'team_id'},
                ],
                'primary_key': 'id',
            },
        }
        self.assertEqual(self.tables, expected)

    def test_missing_tablename_warns_with_file_and_line(self):
        self.assertEqual(len(self.warnings), 1)
        self.assertIn(str(FIXTURE / 'models.py'), self.warnings[0])
        self.assertIn("'Account'", self.warnings[0])
        self.assertIn('account', self.warnings[0])
        # file:line — a bare number after the path, colon-separated
        path_part, rest = self.warnings[0].split(':', 1)
        self.assertEqual(path_part, str(FIXTURE / 'models.py'))
        self.assertTrue(rest.split(':', 1)[0].isdigit())

    def test_detect_code_source(self):
        self.assertEqual(erd.detect_code_source(FIXTURE), 'sqlalchemy')


class TestSQLAlchemyDetection(unittest.TestCase):
    """The three independent detect() signals (declarative_base() call,
    DeclarativeBase subclass, __tablename__ + Column()/mapped_column()
    combination), a single-file root, and the negative case."""

    def _detect(self, text, name='models.py'):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / name
            f.write_text(text)
            return erd.SQLAlchemyOverlay().detect(f), erd.SQLAlchemyOverlay().detect(Path(tmp))

    def test_declarative_base_call_alone_is_enough(self):
        by_file, by_dir = self._detect(
            'from sqlalchemy.orm import declarative_base\nBase = declarative_base()\n')
        self.assertTrue(by_file)
        self.assertTrue(by_dir)

    def test_declarative_base_subclass_alone_is_enough(self):
        by_file, by_dir = self._detect(
            'from sqlalchemy.orm import DeclarativeBase\nclass Base(DeclarativeBase):\n    pass\n')
        self.assertTrue(by_file)
        self.assertTrue(by_dir)

    def test_tablename_alone_is_not_enough(self):
        by_file, _ = self._detect('class Foo:\n    __tablename__ = "foos"\n')
        self.assertFalse(by_file)

    def test_column_call_alone_is_not_enough(self):
        by_file, _ = self._detect('from sqlalchemy import Column, Integer\nx = Column(Integer)\n')
        self.assertFalse(by_file)

    def test_tablename_and_column_combination_is_enough(self):
        by_file, _ = self._detect(
            'from sqlalchemy import Column, Integer\n'
            'class Foo:\n'
            '    __tablename__ = "foos"\n'
            '    id = Column(Integer, primary_key=True)\n')
        self.assertTrue(by_file)

    def test_non_python_file_is_never_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / 'schema.rb'
            f.write_text('declarative_base()')
            self.assertFalse(erd.SQLAlchemyOverlay().detect(f))

    def test_priority_runs_after_rails_django_prisma(self):
        self.assertEqual(erd.SQLAlchemyOverlay.priority, 4)
        priorities = {cls.name: cls.priority for cls in erd.FRAMEWORK_OVERLAYS}
        for other in ('rails', 'django', 'prisma'):
            self.assertLess(priorities[other], priorities['sqlalchemy'])


class TestSQLAlchemyProvider(unittest.TestCase):
    """sqlalchemy_provider wraps parse_sqlalchemy into a ProviderResult,
    same shape as django_provider/prisma_provider (kind='framework',
    columns retained, pure — no mutation of the parser's own output)."""

    def test_provider_shape(self):
        pr = erd.sqlalchemy_provider(FIXTURE)
        self.assertEqual(pr['source'],
                         {'kind': 'framework', 'provider': 'sqlalchemy', 'location': str(FIXTURE)})
        tables, warnings = erd.parse_sqlalchemy(FIXTURE)
        self.assertEqual(pr['tables'], tables)
        self.assertEqual(pr['warnings'], warnings)

    def test_provider_does_not_mutate_parser_output(self):
        tables_a, _ = erd.parse_sqlalchemy(FIXTURE)
        erd.sqlalchemy_provider(FIXTURE)
        tables_b, _ = erd.parse_sqlalchemy(FIXTURE)
        self.assertEqual(tables_a, tables_b)


class TestSQLAlchemyForeignKeyRelationshipDedup(unittest.TestCase):
    """Fable's dedup requirement: a ForeignKey column and a relationship()
    declared on the SAME class for the SAME target must merge into exactly
    one association, not two — exercised over the contract fixture's
    Post.user_id / Post.user pair (tests/fixture_contract/sqlalchemy/)."""

    @classmethod
    def setUpClass(cls):
        cls.tables, cls.warnings = erd.parse_sqlalchemy(FIXTURE_CONTRACT)

    def test_no_warnings(self):
        self.assertEqual(self.warnings, [])

    def test_exactly_one_posts_to_users_edge(self):
        to_users = [a for a in self.tables['posts']['associations'] if a['target'] == 'users']
        self.assertEqual(len(to_users), 1)
        self.assertEqual(to_users[0]['type'], 'belongs_to')
        self.assertEqual(to_users[0]['foreign_key'], 'user_id')

    def test_self_reference_keeps_both_the_fk_side_and_the_inverse_collection(self):
        # parent_id's belongs_to (the FK side) and children's has_many (the
        # inverse collection) are two DIFFERENT, both-legitimate edges on the
        # same table pair — only the parent/user_id-style same-direction
        # duplicate is deduped, never a genuine FK-vs-inverse pair
        to_posts = [a for a in self.tables['posts']['associations'] if a['target'] == 'posts']
        types = {a['type'] for a in to_posts}
        self.assertEqual(types, {'belongs_to', 'has_many'})
        self.assertEqual(len(to_posts), 2)

    def test_remote_side_relationship_produces_no_extra_edge(self):
        # 'parent' appears exactly once: the FK-derived belongs_to (its name
        # is the parent_id column's stem). The `parent = relationship('Post',
        # remote_side=[id])` declaration aliases that same edge and must not
        # add a second one.
        parents = [a for a in self.tables['posts']['associations'] if a['name'] == 'parent']
        self.assertEqual(len(parents), 1)
        self.assertEqual(parents[0]['type'], 'belongs_to')
        self.assertEqual(parents[0].get('foreign_key'), 'parent_id')


if __name__ == '__main__':
    unittest.main()
