module HasCrmTable
  extend ActiveSupport::Concern
  included do
    self.table_name = 'crm_widgets'
  end
end
