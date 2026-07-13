# ---------------------------------------------------------------------------
# Prisma schema parser (schema.prisma)
# ---------------------------------------------------------------------------
PRISMA_TYPES = {
    'Int': 'integer', 'BigInt': 'bigint', 'String': 'string',
    'Boolean': 'boolean', 'DateTime': 'datetime', 'Json': 'jsonb',
    'Float': 'float', 'Decimal': 'decimal', 'Bytes': 'binary',
}

def parse_prisma(schema_path):
    text = schema_path.read_text(encoding='utf-8', errors='replace')
    text = re.sub(r'//[^\n]*', '', text)  # strip comments

    blocks = {m.group(1): m.group(2)
              for m in re.finditer(r'model\s+(\w+)\s*\{([^}]*)\}', text)}
    enums = set(re.findall(r'enum\s+(\w+)\s*\{', text))

    def table_of(model):
        mm = re.search(r'@@map\("([^"]+)"\)', blocks[model])
        return mm.group(1) if mm else model

    def is_list_of(model, other):
        # does `model` declare an `xxx Other[]` field? (implicit m2m check)
        return re.search(r'\w+\s+%s\[\]' % re.escape(other), blocks[model]) is not None

    tables = {}
    for model, block in blocks.items():
        cols, assocs, pk = [], [], None
        unique_cols = set()  # scalar fields with @unique — used below to
                              # tell a 1:1 FK-holding side from a plain belongs_to
        lines = [l.strip() for l in block.splitlines() if l.strip() and not l.strip().startswith('@@')]

        # pass 1: scalar/enum columns only — relation fields need unique_cols
        # fully populated first (a relation's `fields: [...]` FK column can be
        # declared on any line in the block, not necessarily before it)
        for line in lines:
            fm = re.match(r'(\w+)\s+(\w+)(\[\])?(\?)?\s*(.*)', line)
            if not fm:
                continue
            fname, ftype, is_list, optional, rest = fm.groups()
            if ftype in blocks:
                continue  # relation field, handled in pass 2
            col = fname
            cm = re.search(r'@map\("([^"]+)"\)', rest)
            if cm:
                col = cm.group(1)
            primary = '@id' in rest
            if primary:
                pk = col
            if '@unique' in rest:
                unique_cols.add(col)
            cols.append({
                'name': col,
                'type': PRISMA_TYPES.get(ftype, ftype if ftype in enums else ftype.lower()),
                'nullable': bool(optional) and not primary,
                'primary': primary,
            })

        # pass 2: relation fields
        for line in lines:
            fm = re.match(r'(\w+)\s+(\w+)(\[\])?(\?)?\s*(.*)', line)
            if not fm:
                continue
            fname, ftype, is_list, optional, rest = fm.groups()
            if ftype not in blocks:
                continue
            target = table_of(ftype)
            fields = re.search(r'fields:\s*\[\s*(\w+)', rest)
            if is_list:
                # the other side lists this model too -> implicit many-to-many
                if is_list_of(ftype, model):
                    assocs.append({'type': 'has_and_belongs_to_many',
                                   'name': fname, 'target': target})
                else:
                    assocs.append({'type': 'has_many', 'name': fname, 'target': target})
            elif fields:  # the side holding the FK
                fk_col = fields.group(1)
                # @unique on the scalar FK field means each value can only
                # appear once — a real 1:1, not the default many:1 a bare FK
                # column implies. Same has_one convention parse_django uses.
                assoc_type = 'has_one' if fk_col in unique_cols else 'belongs_to'
                assocs.append({'type': assoc_type, 'name': fname,
                               'target': target, 'foreign_key': fk_col})
            else:  # 1:1 parent side without the FK
                assocs.append({'type': 'has_one', 'name': fname, 'target': target})

        tables[table_of(model)] = {'columns': cols, 'associations': assocs,
                                   'primary_key': pk}
    return tables

def prisma_provider(schema_path):
    """ProviderResult for a resolved Prisma schema file. Retains columns
    (with Prisma types, including enum field types as the enum name) so a
    Prisma-only run (Step 8) or the Step-6 merge can use them instead of
    discarding them the way the current association-only overlay does."""
    tables = parse_prisma(schema_path)
    return make_provider_result('framework', 'prisma', tables,
                                location=str(schema_path))

@register_overlay
class PrismaOverlay(FrameworkOverlay):
    """A Prisma schema: a schema.prisma file directly, or a project containing
    prisma/schema.prisma (or schema.prisma at the root). Retains columns."""
    name = 'prisma'
    priority = 3

    def _schema(self, root):
        if root.is_file():
            return root
        return next(c for c in (root / 'prisma' / 'schema.prisma', root / 'schema.prisma')
                    if c.exists())

    def detect(self, root):
        if root.is_file():
            return root.suffix == '.prisma'
        return any(c.exists() for c in
                   (root / 'prisma' / 'schema.prisma', root / 'schema.prisma'))

    def build(self, root, table_map):
        return prisma_provider(self._schema(root))
