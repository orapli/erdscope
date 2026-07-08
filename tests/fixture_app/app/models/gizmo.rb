class Gizmo < ApplicationRecord
  include SomeGemProvidedConcern # lives in a gem, not this app — unresolvable statically
end
