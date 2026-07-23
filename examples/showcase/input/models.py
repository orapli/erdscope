"""Static SQLAlchemy showcase input; parsing does not import SQLAlchemy."""
from sqlalchemy import Column, ForeignKey, Integer, String, Table, Text
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

post_tags = Table(
    'post_tags', Base.metadata,
    Column('post_id', Integer, ForeignKey('posts.id'), primary_key=True),
    Column('tag_id', Integer, ForeignKey('tags.id'), primary_key=True),
)


class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False)
    name = Column(String(100), nullable=False)
    posts = relationship('Post')
    profile = relationship('Profile', uselist=False)


class Profile(Base):
    __tablename__ = 'profiles'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), unique=True, nullable=False)
    bio = Column(Text)


class Post(Base):
    __tablename__ = 'posts'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    parent_id = Column(Integer, ForeignKey('posts.id'), nullable=True)
    title = Column(String(200), nullable=False)
    body = Column(Text)
    user = relationship('User')
    parent = relationship('Post', remote_side=[id])
    children = relationship('Post')
    tags = relationship('Tag', secondary=post_tags)


class Tag(Base):
    __tablename__ = 'tags'
    id = Column(Integer, primary_key=True)
    label = Column(String(50), unique=True, nullable=False)
