class SharedBase < ApplicationRecord
  self.abstract_class = true
end

class Gadget < SharedBase
  belongs_to :user
end
