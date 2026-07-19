from sqlalchemy import Column, ForeignKey, Integer, Numeric, String, Table
from sqlalchemy.orm import mapped_column, relationship

from .base import AuditedBase, Base, TimestampMixin

team_members = Table(
    'team_members', Base.metadata,
    Column('team_id', Integer, ForeignKey('teams.id'), primary_key=True),
    Column('user_id', Integer, ForeignKey('users.id'), primary_key=True),
)


class Team(Base, TimestampMixin):
    __tablename__ = 'teams'
    id = Column(Integer, primary_key=True)
    name = Column('team_name', String(80))  # physical column name override
    members = relationship('User', secondary=team_members)


class User(AuditedBase, TimestampMixin):
    __tablename__ = 'users'
    # SQLAlchemy 2.0 mapped_column() style, mixed with classic Column() below
    id = mapped_column(Integer, primary_key=True)
    email = mapped_column(String(255), unique=True)
    balance = Column(Numeric(10, 2))
    teams = relationship('Team', secondary=team_members)


class Account(Base):
    # no __tablename__ at all -> file:line warning + to_snake fallback ('account')
    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey('users.id'), unique=True)


class Order(Base):
    __tablename__ = 'orders'
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey('account.id'))
    # unresolvable relationship target (no such class parsed) -> skipped silently
    reviewer = relationship('Ghost')
    # unresolvable ForeignKey target (not a string literal) -> column kept, edge skipped
    approver_id = Column(Integer, ForeignKey(User.id))


class Widget(Base):
    __tablename__ = 'widgets'
    id = Column(Integer, primary_key=True)
    # a type name absent from SQLALCHEMY_TYPES passes through lowercased
    status = Column('state', MoneyType)
    # no explicit type at all -> defaults to bigint (inferred from the FK)
    team_id = Column(ForeignKey('teams.id'))


class MoneyType:
    pass
