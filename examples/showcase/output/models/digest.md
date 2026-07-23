# Provider showcase — schema digest

Review fixture: SQLite, config, and SQLAlchemy model inputs describe the same small publishing domain. Generated outputs are committed and drift-checked.

## Tables (4)

### posts
_Retention: Posts are retained after user anonymization so links remain stable._
- id: integer, pk
- user_id: integer, fk→users
- parent_id: integer, fk→posts
- title: string
- body: text
Rel: belongs_to posts as parent fk=parent_id, belongs_to users as user fk=user_id, has_and_belongs_to_many tags through post_tags, has_many posts as children

### profiles
- id: integer, pk
- user_id: integer, fk→users
- bio: text
Rel: has_one users as user fk=user_id

### tags
- id: integer, pk
- label: string

### users
- id: integer, pk
- email: string
- name: string
Rel: has_many posts, has_one profiles as profile
