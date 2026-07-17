class Post < ApplicationRecord
  belongs_to :user
  belongs_to :parent, class_name: 'Post', optional: true
  has_many :replies, class_name: 'Post', foreign_key: 'parent_id'
  has_and_belongs_to_many :tags
end
