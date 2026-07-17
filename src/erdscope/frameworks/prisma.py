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

    def relation_name(rest):
        # @relation("Name", ...) — the disambiguator Prisma requires when two
        # relations link the same pair of models (and for self-relations)
        m = re.search(r'@relation\(\s*"([^"]+)"', rest)
        return m.group(1) if m else None

    # every list field in every model: model -> [(field, target model, relation
    # name)]. Implicit m2m pairing below matches on (target, relation name) —
    # NOT merely "the other model declares some list of us", which misread a
    # self-relation's own back-reference (`replies Post[]` next to `parent
    # Post?`) and any mixed named relations as many-to-many.
    list_fields = {}
    for model, block in blocks.items():
        entries = []
        for line in block.splitlines():
            lm = re.match(r'\s*(\w+)\s+(\w+)\[\]\s*(.*)', line)
            if lm and lm.group(2) in blocks:
                entries.append((lm.group(1), lm.group(2), relation_name(lm.group(3))))
        list_fields[model] = entries

    def paired_list_field(model, fname, other, rel):
        # does `other` declare a list field back at `model` in the SAME
        # relation (matching @relation name, both possibly unnamed)? The field
        # itself is excluded so a self-relation's one list field never pairs
        # with itself.
        for f, t, r in list_fields.get(other, ()):
            if t == model and r == rel and not (other == model and f == fname):
                return True
        return False

    tables = {}
    for model, block in blocks.items():
        cols, assocs, pk = [], [], None
        unique_cols = set()  # scalar fields with @unique — used below to
                              # tell a 1:1 FK-holding side from a plain belongs_to
        lines = [l.strip() for l in block.splitlines() if l.strip() and not l.strip().startswith('@@')]

        # pass 1: scalar/enum columns only — relation fields need unique_cols
        # fully populated first (a relation's `fields: [...]` FK column can be
        # declared on any line in the block, not necessarily before it)
        field_col = {}  # field name -> column name (differs under @map)
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
            field_col[fname] = col
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

        # block-level attributes (the @@ lines pass 1 skips), read from the
        # raw block: @@id([a, b]) is a composite PK (primary_key becomes the
        # IR's list form, same as the DB adapters emit); a single-field
        # @@unique([x]) is the same 1:1 signal as an inline @unique, while a
        # multi-field one is a composite constraint and deliberately not one
        mid = re.search(r'@@id\([^)]*?\[([^\]]+)\]', block)
        if mid:
            pk_cols = [field_col.get(f.strip(), f.strip())
                       for f in mid.group(1).split(',') if f.strip()]
            for c in cols:
                if c['name'] in pk_cols:
                    c['primary'] = True
                    c['nullable'] = False
            if pk_cols:
                pk = pk_cols if len(pk_cols) > 1 else pk_cols[0]
        for mu in re.finditer(r'@@unique\([^)]*?\[([^\]]+)\]', block):
            ufields = [f.strip() for f in mu.group(1).split(',') if f.strip()]
            if len(ufields) == 1:
                unique_cols.add(field_col.get(ufields[0], ufields[0]))

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
                # the SAME relation has a list field on the other side too ->
                # implicit many-to-many (see paired_list_field for why the
                # pairing must match @relation names, not just field types)
                if paired_list_field(model, fname, ftype, relation_name(rest)):
                    assocs.append({'type': 'has_and_belongs_to_many',
                                   'name': fname, 'target': target})
                else:
                    assocs.append({'type': 'has_many', 'name': fname, 'target': target})
            elif fields:  # the side holding the FK
                # `fields:` names the scalar FIELD; the column differs when
                # that field carries @map — resolve so foreign_key (and the
                # unique_cols lookup below) name the real column
                fk_col = field_col.get(fields.group(1), fields.group(1))
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
    expects = ('a schema.prisma file (or a project containing '
               'prisma/schema.prisma) declaring at least one model')

    def _schema(self, root):
        if root.is_file():
            return root
        found = next((c for c in (root / 'prisma' / 'schema.prisma', root / 'schema.prisma')
                      if c.exists()), None)
        if found is None:
            # reachable via a typed prisma.models source, which skips detect()
            # — a raw StopIteration traceback is not an error message
            sys.exit(f'Error: no prisma/schema.prisma or schema.prisma found under {root}')
        return found

    def detect(self, root):
        if root.is_file():
            return root.suffix == '.prisma'
        return any(c.exists() for c in
                   (root / 'prisma' / 'schema.prisma', root / 'schema.prisma'))

    def build(self, root, table_map):
        return prisma_provider(self._schema(root))
