class User < ApplicationRecord
  has_many :posts, dependent: :destroy
  has_many :comments
  has_many :commented_posts, through: :comments, class_name: 'Post'
  has_one :profile
end
