class Post < ApplicationRecord
  belongs_to :user
  belongs_to :author, class_name: 'User', foreign_key: 'user_id'
  has_many :comments
  has_and_belongs_to_many :tags
end
