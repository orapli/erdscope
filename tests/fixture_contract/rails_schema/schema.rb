# Contract fixture: physical half of the same users/profiles/posts/tags
# domain (the m2m is a plain join table here — schema.rb has no habtm).
ActiveRecord::Schema[7.1].define(version: 2026_01_01_000000) do
  create_table "users", force: :cascade do |t|
    t.string "name"
  end

  create_table "profiles", force: :cascade do |t|
    t.bigint "user_id", null: false
    t.index ["user_id"], name: "index_profiles_on_user_id", unique: true
  end

  create_table "posts", force: :cascade do |t|
    t.string "title"
    t.bigint "user_id", null: false
    t.bigint "parent_id"
  end

  create_table "tags", force: :cascade do |t|
    t.string "label"
  end

  create_table "posts_tags", id: false, force: :cascade do |t|
    t.bigint "post_id", null: false
    t.bigint "tag_id", null: false
  end

  add_foreign_key "profiles", "users"
  add_foreign_key "posts", "users"
  add_foreign_key "posts", "posts", column: "parent_id"
  add_foreign_key "posts_tags", "posts"
  add_foreign_key "posts_tags", "tags"
end
