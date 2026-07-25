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
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / 'fixture_sqlalchemy'
FIXTURE_20 = Path(__file__).resolve().parent / 'fixture_sqlalchemy_20'
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
        # file:line — a bare number after the path, colon-separated. Split on
        # the LAST ':<digits>:' rather than the first ':': a Windows path's
        # drive letter ('D:\...') is itself a colon that a naive
        # split(':', 1) would mistake for the file/line separator.
        m = re.match(r'^(.*):(\d+):', self.warnings[0])
        self.assertIsNotNone(m, self.warnings[0])
        self.assertEqual(m.group(1), str(FIXTURE / 'models.py'))
        self.assertTrue(m.group(2).isdigit())

    def test_detect_code_source(self):
        self.assertEqual(erd.detect_code_source(FIXTURE), 'sqlalchemy')


class TestSQLAlchemy20AnnotationIRSnapshot(unittest.TestCase):
    """Full parse_sqlalchemy() output for tests/fixture_sqlalchemy_20 — the
    2.0 annotation-first style, where `Mapped[...]` is the only place the
    column type and the relationship target appear because the
    mapped_column()/relationship() call beside it carries neither.

    Covers: annotation-derived types (int/str/Decimal/datetime), an explicit
    type argument still winning over the annotation (User.name), nullability
    from Optional[...] and from PEP 604 `str | None`, an explicit nullable=
    beating the annotation (User.nickname), an unrecognised annotation name
    passing through lowercased (User.role), a collection annotation left
    untyped rather than typed as its element (User.scores), relationship
    targets resolved from `Mapped[List["Post"]]` / `Mapped["Tag"]` /
    `Mapped[Optional["Profile"]]` with no call argument at all, cardinality
    read off the annotation (list -> has_many, scalar -> has_one,
    WriteOnlyMapped -> has_many), and the FK-column/relationship() dedup
    still collapsing Post.author_id + Post.author into one edge."""

    @classmethod
    def setUpClass(cls):
        cls.tables, cls.warnings = erd.parse_sqlalchemy(FIXTURE_20)

    def test_no_warnings(self):
        self.assertEqual(self.warnings, [])

    def test_full_ir(self):
        expected = {
            'users': {
                'columns': [
                    {'name': 'id', 'type': 'integer', 'nullable': False, 'primary': True},
                    {'name': 'name', 'type': 'string', 'nullable': False, 'primary': False},
                    {'name': 'email', 'type': 'string', 'nullable': True, 'primary': False},
                    {'name': 'bio', 'type': 'string', 'nullable': True, 'primary': False},
                    {'name': 'nickname', 'type': 'string', 'nullable': False, 'primary': False},
                    {'name': 'balance', 'type': 'decimal', 'nullable': False, 'primary': False},
                    {'name': 'created_at', 'type': 'datetime', 'nullable': False, 'primary': False},
                    {'name': 'role', 'type': 'role', 'nullable': False, 'primary': False},
                    {'name': 'scores', 'type': '', 'nullable': False, 'primary': False},
                ],
                'associations': [
                    {'type': 'has_many', 'name': 'posts', 'target': 'posts'},
                    {'type': 'has_one', 'name': 'profile', 'target': 'profiles'},
                    {'type': 'has_many', 'name': 'audits', 'target': 'audits'},
                ],
                'primary_key': 'id',
            },
            'posts': {
                'columns': [
                    {'name': 'id', 'type': 'integer', 'nullable': False, 'primary': True},
                    {'name': 'author_id', 'type': 'integer', 'nullable': False, 'primary': False},
                ],
                'associations': [
                    {'type': 'belongs_to', 'name': 'author', 'target': 'users',
                     'foreign_key': 'author_id'},
                    {'type': 'has_one', 'name': 'primary_tag', 'target': 'tags'},
                ],
                'primary_key': 'id',
            },
            'profiles': {
                'columns': [
                    {'name': 'id', 'type': 'integer', 'nullable': False, 'primary': True},
                    {'name': 'user_id', 'type': 'integer', 'nullable': False, 'primary': False},
                ],
                'associations': [
                    {'type': 'has_one', 'name': 'user', 'target': 'users',
                     'foreign_key': 'user_id'},
                ],
                'primary_key': 'id',
            },
            'tags': {
                'columns': [
                    {'name': 'id', 'type': 'integer', 'nullable': False, 'primary': True},
                    {'name': 'label', 'type': 'string', 'nullable': False, 'primary': False},
                ],
                'associations': [],
                'primary_key': 'id',
            },
            'audits': {
                'columns': [
                    {'name': 'id', 'type': 'integer', 'nullable': False, 'primary': True},
                    {'name': 'action', 'type': 'string', 'nullable': False, 'primary': False},
                ],
                'associations': [],
                'primary_key': 'id',
            },
        }
        self.assertEqual(self.tables, expected)

    def test_annotation_only_relationship_is_not_dropped(self):
        # the regression this fixture exists for: `posts: Mapped[list["Post"]]
        # = relationship()` has no target argument, so the association used to
        # vanish entirely rather than merely lose its type
        self.assertIn('posts', [a['name'] for a in self.tables['users']['associations']])

    def test_fk_column_and_annotated_relationship_stay_one_edge(self):
        edges = [a for a in self.tables['posts']['associations'] if a['target'] == 'users']
        self.assertEqual(len(edges), 1, edges)


class TestSQLAlchemyMappedAnnotationUnwrap(unittest.TestCase):
    """_unwrap_mapped_annotation() shapes, incl. the ones that must NOT
    resolve (a real union, an unrecognised generic, a bare annotation)."""

    def _unwrap(self, source):
        import ast
        stmt = ast.parse(source).body[0]
        return erd._unwrap_mapped_annotation(stmt.annotation)

    def test_plain_scalar(self):
        self.assertEqual(self._unwrap('x: Mapped[int] = c()'), ('int', False, False))

    def test_optional_scalar(self):
        self.assertEqual(self._unwrap('x: Mapped[Optional[str]] = c()'), ('str', False, True))

    def test_pep604_optional(self):
        self.assertEqual(self._unwrap('x: Mapped[str | None] = c()'), ('str', False, True))
        self.assertEqual(self._unwrap('x: Mapped[None | str] = c()'), ('str', False, True))

    def test_forward_reference_collection(self):
        self.assertEqual(self._unwrap('x: Mapped[list["Item"]] = c()'), ('Item', True, False))
        self.assertEqual(self._unwrap('x: Mapped[List[Item]] = c()'), ('Item', True, False))

    def test_write_only_and_dynamic_are_collections(self):
        self.assertEqual(self._unwrap('x: WriteOnlyMapped["Item"] = c()'), ('Item', True, False))
        self.assertEqual(self._unwrap('x: DynamicMapped["Item"] = c()'), ('Item', True, False))

    def test_qualified_wrapper(self):
        self.assertEqual(self._unwrap('x: orm.Mapped[int] = c()'), ('int', False, False))

    def test_optional_collection(self):
        self.assertEqual(self._unwrap('x: Mapped[Optional[list["Item"]]] = c()'),
                         ('Item', True, True))

    def test_non_mapped_annotation_is_ignored(self):
        self.assertEqual(self._unwrap('x: int = c()'), (None, False, False))
        self.assertEqual(self._unwrap('x: Mapped = c()'), (None, False, False))

    def test_genuine_union_has_no_single_type(self):
        self.assertEqual(self._unwrap('x: Mapped[int | str] = c()'), (None, False, False))

    def test_unrecognised_generic_has_no_single_type(self):
        self.assertEqual(self._unwrap('x: Mapped[dict[str, Any]] = c()'), (None, False, False))


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


class TestDeclarativeBaseSubclassIsABase(unittest.TestCase):
    """`class Base(DeclarativeBase): pass` (the standard 2.0-style app-defined
    base) must be treated as a declarative base, not as a model — no phantom
    to_snake('Base') table, no missing-__tablename__ warning for it — while
    its subclasses still resolve as models (Sol release-review P1)."""

    SOURCE = (
        'from sqlalchemy.orm import DeclarativeBase, mapped_column\n'
        'from sqlalchemy import Integer, String\n'
        'class Base(DeclarativeBase):\n'
        '    pass\n'
        'class User(Base):\n'
        '    __tablename__ = "users"\n'
        '    id = mapped_column(Integer, primary_key=True)\n'
        '    email = mapped_column(String(255))\n'
    )

    def _parse(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'models.py').write_text(text)
            return erd.parse_sqlalchemy(Path(tmp))

    def test_no_phantom_base_table(self):
        tables, warnings = self._parse(self.SOURCE)
        self.assertIn('users', tables)
        self.assertNotIn('base', tables)
        self.assertEqual([w for w in warnings if "'Base'" in w or '"Base"' in w], [])

    def test_chained_base_subclass_is_also_a_base(self):
        chained = self.SOURCE.replace(
            'class User(Base):',
            'class ProjectBase(Base):\n    pass\nclass User(ProjectBase):')
        tables, warnings = self._parse(chained)
        self.assertIn('users', tables)
        self.assertNotIn('base', tables)
        self.assertNotIn('project_base', tables)


if __name__ == '__main__':
    unittest.main()
