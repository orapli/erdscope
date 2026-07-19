from sqlalchemy import Column, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class TimestampMixin:
    """A plain (non-Base) mixin — not itself a declarative model, but its
    columns must still be inherited by any subclass that combines it with
    Base (multiple inheritance is the common way SQLAlchemy code shares
    columns across otherwise-unrelated tables)."""
    created_at = Column(String(30))
    updated_at = Column(String(30))


class AuditedBase(Base):
    """An abstract declarative base (__abstract__ = True): a genuine
    subclass of Base, contributes columns to its own subclasses, but never
    gets a table of its own."""
    __abstract__ = True
    note = Column(String(200))
