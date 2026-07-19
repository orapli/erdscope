from sqlalchemy import Column, ForeignKey, Integer, String, Table
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
    name = Column(String(100))
    posts = relationship('Post')  # inverse side, no own FK -> enrichment has_many


class Profile(Base):
    __tablename__ = 'profiles'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), unique=True)


class Post(Base):
    __tablename__ = 'posts'
    id = Column(Integer, primary_key=True)
    title = Column(String(200))
    # ForeignKey AND relationship() on the SAME pair — must merge into one
    # edge, not two (Fable's dedup requirement)
    user_id = Column(Integer, ForeignKey('users.id'))
    user = relationship('User')
    parent_id = Column(Integer, ForeignKey('posts.id'), nullable=True)
    parent = relationship('Post', remote_side=[id])
    children = relationship('Post')
    tags = relationship('Tag', secondary=post_tags)


class Tag(Base):
    __tablename__ = 'tags'
    id = Column(Integer, primary_key=True)
    label = Column(String(50))
