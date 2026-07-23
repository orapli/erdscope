# Provider showcase — schema digest

Review fixture: SQLite, config, and SQLAlchemy model inputs describe the same small publishing domain. Generated outputs are committed and drift-checked.

## Tables (5)

### post_tags
- post_id: integer, pk, fk→posts
- tag_id: integer, pk, fk→tags
Rel: belongs_to posts as post fk=post_id, belongs_to tags as tag fk=tag_id

### posts
_Retention: Posts are retained after user anonymization so links remain stable._
- id: integer, pk
- user_id: integer, fk→users
- parent_id: integer, fk→posts
- title: string
- body: text
Rel: belongs_to posts as parent fk=parent_id, belongs_to users as user fk=user_id, has_and_belongs_to_many tags through post_tags

### profiles
- id: integer, pk
- user_id: integer, fk→users
- bio: text
Rel: has_one users as user fk=user_id

### tags
- id: integer, pk
- label: string
Rel: has_and_belongs_to_many posts through post_tags

### users
- id: integer, pk
- email: string
- name: string
Rel: has_many posts, has_one profiles as profile
