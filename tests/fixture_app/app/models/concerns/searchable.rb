module Searchable
  extend ActiveSupport::Concern
  included do
    has_many :bogus_from_concern  # concerns dir is excluded; must not appear
  end
end
