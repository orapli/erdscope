class Task < ApplicationRecord
  belongs_to :project
  belongs_to :owner, class_name: 'Project'
end
