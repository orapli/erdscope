class Admin::AuditLog < ApplicationRecord
  belongs_to :user
end
