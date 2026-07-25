"""SQLAlchemy 2.0 annotation-first style: the type and the relationship
target live in `Mapped[...]`, and the mapped_column()/relationship() call
beside it often carries neither. Kept separate from fixture_sqlalchemy/
(1.x-flavoured Column() usage) so each snapshot stays readable."""
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import DeclarativeBase, Mapped, WriteOnlyMapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Role:  # a custom/enum-ish type: unknown name passes through lowercased
    pass


class User(Base):
    __tablename__ = 'users'
    # annotation-only: the type comes from Mapped[int], not from a call arg
    id: Mapped[int] = mapped_column(primary_key=True)
    # an explicit type argument still wins over the annotation
    name: Mapped[str] = mapped_column(String(50))
    # Optional[...] and PEP 604 `| None` both mean nullable
    email: Mapped[Optional[str]] = mapped_column(unique=True)
    bio: Mapped[str | None] = mapped_column()
    # an explicit nullable= keyword beats the annotation
    nickname: Mapped[Optional[str]] = mapped_column(nullable=False)
    balance: Mapped[Decimal] = mapped_column()
    created_at: Mapped[datetime] = mapped_column()
    role: Mapped[Role] = mapped_column()
    # a collection annotation is not a scalar type — left untyped rather than
    # reported as the element type
    scores: Mapped[list[int]] = mapped_column()
    # target class only in the annotation, no argument to relationship()
    posts: Mapped[List['Post']] = relationship(back_populates='author')
    profile: Mapped[Optional['Profile']] = relationship(back_populates='user')
    audits: WriteOnlyMapped['Audit'] = relationship()


class Post(Base):
    __tablename__ = 'posts'
    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    # same edge as author_id above — must not become a second association
    author: Mapped['User'] = relationship(back_populates='posts')
    # scalar relationship with no FK column of its own on this class
    primary_tag: Mapped['Tag'] = relationship()


class Profile(Base):
    __tablename__ = 'profiles'
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), unique=True)
    user: Mapped['User'] = relationship(back_populates='profile')


class Tag(Base):
    __tablename__ = 'tags'
    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column()


class Audit(Base):
    __tablename__ = 'audits'
    id: Mapped[int] = mapped_column(primary_key=True)
    action: Mapped[str] = mapped_column()
