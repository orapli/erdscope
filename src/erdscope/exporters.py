def _xml(s):
    return (str(s).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))

def _sheet_xml(rows, widths=None, links=None):
    """rows: list of rows; each cell is a value or (value, style_idx).
    links: [(cell_ref, target_sheet)] internal hyperlinks."""
    cols = ''
    if widths:
        cols = '<cols>' + ''.join(
            f'<col min="{i+1}" max="{i+1}" width="{w}" customWidth="1"/>'
            for i, w in enumerate(widths)) + '</cols>'
    body = []
    for r, row in enumerate(rows, 1):
        cells = []
        for c, cell in enumerate(row):
            val, style = cell if isinstance(cell, tuple) else (cell, 0)
            if val is None or val == '':
                # an empty value with a style (border/zebra-fill role) still
                # needs to exist as a cell, or the styling — the whole point
                # of a 5-role stylesheet — has visible holes wherever a
                # column happens to be blank (very common: Default/Extra/
                # Comment are empty far more often than not)
                if style:
                    ref = f'{_col_letter(c)}{r}'
                    cells.append(f'<c r="{ref}" s="{style}"/>')
                continue
            ref = f'{_col_letter(c)}{r}'
            s = f' s="{style}"' if style else ''
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                cells.append(f'<c r="{ref}"{s}><v>{val}</v></c>')
            else:
                cells.append(f'<c r="{ref}"{s} t="inlineStr"><is><t xml:space="preserve">{_xml(val)}</t></is></c>')
        body.append(f'<row r="{r}">' + ''.join(cells) + '</row>')
    hl = ''
    if links:
        hl = '<hyperlinks>' + ''.join(
            f'<hyperlink ref="{ref}" location="{_xml(loc)}!A1" display="{_xml(disp)}"/>'
            for ref, loc, disp in links) + '</hyperlinks>'
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            + cols + '<sheetData>' + ''.join(body) + '</sheetData>' + hl + '</worksheet>')

def _col_letter(idx):
    s = ''
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s

def _sheet_name(name, used):
    clean = re.sub(r"[\[\]:*?/\\']", '_', name)[:31] or 'sheet'
    base, n = clean, 2
    while clean.lower() in used:
        suffix = f'~{n}'
        clean = base[:31 - len(suffix)] + suffix
        n += 1
    used.add(clean.lower())
    return clean


# Style role indices used throughout write_excel's row-building — kept as
# named constants (rather than the old bare HDR=1) because there are now
# six distinct looks instead of two, and "3" or "4" alone reads as noise
# at every call site. Role 0 ("default") is never assigned explicitly;
# it's simply what a cell looks like with no style index at all.
S_TITLE, S_HEADER, S_DATA, S_DATA_ALT, S_SECTION = 1, 2, 3, 4, 5
_ROLES = (S_TITLE, S_HEADER, S_DATA, S_DATA_ALT, S_SECTION)

def _default_role_styles():
    """role (1-5) -> (font, fill, border) XML fragment dict for the
    built-in look. Colors mirror the HTML UI's own slate palette
    (#1e293b header/title, #cbd5e1 borders, #f8fafc/#f1f5f9 light fills)
    for a family resemblance between the diagram and the workbook."""
    plain_font = '<font><sz val="11"/><name val="Calibri"/></font>'
    none_fill = '<fill><patternFill patternType="none"/></fill>'
    no_border = '<border><left/><right/><top/><bottom/><diagonal/></border>'
    thin = '<color rgb="FFCBD5E1"/>'
    thin_border = (f'<border><left style="thin">{thin}</left><right style="thin">{thin}</right>'
                    f'<top style="thin">{thin}</top><bottom style="thin">{thin}</bottom><diagonal/></border>')
    header_fill = ('<fill><patternFill patternType="solid"><fgColor rgb="FF1E293B"/>'
                    '<bgColor indexed="64"/></patternFill></fill>')
    alt_fill = ('<fill><patternFill patternType="solid"><fgColor rgb="FFF8FAFC"/>'
                '<bgColor indexed="64"/></patternFill></fill>')
    section_fill = ('<fill><patternFill patternType="solid"><fgColor rgb="FFF1F5F9"/>'
                     '<bgColor indexed="64"/></patternFill></fill>')
    return {
        S_TITLE:    ('<font><b/><sz val="14"/><color rgb="FF1E293B"/><name val="Calibri"/></font>',
                      none_fill, no_border),
        S_HEADER:   ('<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>',
                      header_fill, thin_border),
        S_DATA:     (plain_font, none_fill, thin_border),
        S_DATA_ALT: (plain_font, alt_fill, thin_border),
        S_SECTION:  ('<font><b/><sz val="11"/><color rgb="FF1E293B"/><name val="Calibri"/></font>',
                      section_fill, no_border),
    }

def _strip_ns_tags(elem):
    """In-place: rewrite every tag in the subtree from '{uri}local' to
    'local'. ElementTree's built-in namespace handling is otherwise all
    prefix-preserving noise once serialized back out — this makes the
    extracted fragments splice cleanly into our own hand-written XML."""
    for e in elem.iter():
        e.tag = e.tag.rsplit('}', 1)[-1]
    return elem

def _extract_template_role_styles(template_path):
    """role (1-5) -> (font, fill, border) XML fragments, extracted from a
    user-supplied .xlsx template. Contract: the template's FIRST
    worksheet, column A, rows 1-5, hold cells styled as
    title/header/data/data-alt/section respectively (column B is free for
    a human-readable label, so the template self-documents when opened in
    Excel). A role whose contract cell is missing falls back to the
    built-in style for that one role, with a warning on stderr — only a
    template that can't be read as a .xlsx at all is a hard error, since
    the user asked for it explicitly and a silent fallback would hide
    their mistake."""
    import zipfile
    import xml.etree.ElementTree as ET
    try:
        zf = zipfile.ZipFile(template_path)
    except (FileNotFoundError, zipfile.BadZipFile, IsADirectoryError, PermissionError) as e:
        sys.exit(f'Error: --excel-template {template_path!s} is not a readable .xlsx file ({e})')
    try:
        wb_root = _strip_ns_tags(ET.fromstring(zf.read('xl/workbook.xml')))
        rels_root = _strip_ns_tags(ET.fromstring(zf.read('xl/_rels/workbook.xml.rels')))
    except KeyError as e:
        sys.exit(f'Error: --excel-template {template_path!s} is missing {e} — not a valid .xlsx')
    first_sheet = wb_root.find('./sheets/sheet')
    if first_sheet is None:
        sys.exit(f'Error: --excel-template {template_path!s} has no worksheets')
    rid = next(v for k, v in first_sheet.attrib.items() if k.rsplit('}', 1)[-1] == 'id')
    rel = next((r for r in rels_root.findall('Relationship') if r.get('Id') == rid), None)
    if rel is None:
        sys.exit(f'Error: --excel-template {template_path!s} has a broken worksheet relationship')
    # a rels Target is either package-absolute ("/xl/worksheets/sheet1.xml",
    # openpyxl's own convention) or relative to xl/ ("worksheets/sheet1.xml",
    # the more common convention, and what this file's own writer emits) —
    # both are valid per the OOXML spec, so accept either
    target = rel.get('Target')
    sheet_path = target.lstrip('/') if target.startswith('/') else 'xl/' + target

    sheet_root = _strip_ns_tags(ET.fromstring(zf.read(sheet_path)))
    styles_root = _strip_ns_tags(ET.fromstring(zf.read('xl/styles.xml')))
    tpl_cellxfs = styles_root.findall('./cellXfs/xf')
    tpl_fonts = styles_root.findall('./fonts/font')
    tpl_fills = styles_root.findall('./fills/fill')
    tpl_borders = styles_root.findall('./borders/border')

    def _at(elems, idx):  # bounds-checked list lookup — a hand-edited or
        return elems[idx] if 0 <= idx < len(elems) else None  # corrupted template can reference an id that doesn't exist

    defaults = _default_role_styles()
    out = {}
    for row_n, role in enumerate(_ROLES, 1):
        cell = sheet_root.find(f'.//c[@r="A{row_n}"]')
        xf_idx = int(cell.get('s', '0')) if cell is not None else None
        if xf_idx is None or xf_idx >= len(tpl_cellxfs):
            if cell is None:
                print(f'Warning: --excel-template has no cell A{row_n} — role {role} '
                      'falls back to the built-in style', file=sys.stderr)
            elif xf_idx is not None:
                print(f'Warning: --excel-template cell A{row_n} references a style that '
                      f"doesn't exist in the template — role {role} falls back to the "
                      'built-in style', file=sys.stderr)
            out[role] = defaults[role]
            continue
        xf = tpl_cellxfs[xf_idx]
        font = _at(tpl_fonts, int(xf.get('fontId', 0)))
        fill = _at(tpl_fills, int(xf.get('fillId', 0)))
        border = _at(tpl_borders, int(xf.get('borderId', 0)))
        d_font, d_fill, d_border = defaults[role]
        out[role] = (
            ET.tostring(font, encoding='unicode') if font is not None else d_font,
            ET.tostring(fill, encoding='unicode') if fill is not None else d_fill,
            ET.tostring(border, encoding='unicode') if border is not None else d_border,
        )
    theme = zf.read('xl/theme/theme1.xml').decode('utf-8') if 'xl/theme/theme1.xml' in zf.namelist() else None
    return out, theme

def _build_stylesheet_parts(role_styles):
    """role (1-5) -> (font,fill,border) dict -> (fonts,fills,borders,
    cellxfs) XML fragment lists, in the fixed layout every generated
    workbook uses: fills[0]/[1] reserved as none/gray125 (Excel treats
    their absence as a corrupt file, regardless of whether any cell
    references them), font/fill/border index 0 is the plain unstyled
    default, then one fresh slot per role in _ROLES order — so role N's
    cellxfs entry is always at index N, matching the S_* constants.
    Returned as lists (not joined strings) so the caller can both count
    and concatenate them for the count="N" attributes OOXML requires."""
    fonts = ['<font><sz val="11"/><name val="Calibri"/></font>']
    fills = ['<fill><patternFill patternType="none"/></fill>',
             '<fill><patternFill patternType="gray125"/></fill>']
    borders = ['<border><left/><right/><top/><bottom/><diagonal/></border>']
    cellxfs = ['<xf xfId="0"/>']
    for role in _ROLES:
        font, fill, border = role_styles[role]
        fonts.append(font); fills.append(fill); borders.append(border)
        fi, fli, bi = len(fonts)-1, len(fills)-1, len(borders)-1
        cellxfs.append(f'<xf xfId="0" fontId="{fi}" fillId="{fli}" borderId="{bi}" '
                        'applyFont="1" applyFill="1" applyBorder="1"/>')
    return fonts, fills, borders, cellxfs

def write_excel(tables, path, title, template_path=None, notes=None, groups=None):
    """`notes`/`groups` (backlog #4, activating the Phase 1 wiring): a Notes
    sheet and a Groups sheet are appended when either is non-empty, and the
    overview sheet gains a trailing Group column when `groups` is non-empty.
    Both additions are fully omitted (not just left empty) when there's
    nothing to show, so a run with no notes/groups still produces
    byte-identical output to before this feature existed."""
    import zipfile
    used = set()
    sheets = []  # (sheet_name, xml)
    role_styles, theme_xml = (
        _extract_template_role_styles(template_path) if template_path
        else (_default_role_styles(), None))
    fonts, fills, borders, cellxfs = _build_stylesheet_parts(role_styles)

    def alt(i):  # zebra stripe: 0-indexed data row -> data/data-alt role
        return S_DATA_ALT if i % 2 == 1 else S_DATA

    # ── overview sheet ──
    used.add('tables')
    names = sorted(tables)
    sheet_of = {n: _sheet_name(n, used) for n in names}
    # table -> its group's display label (title if set, else the group id).
    # Phase 1 groups have non-overlapping membership (resolve_and_validate_
    # groups), so a table maps to at most one label here.
    group_of = {}
    for g in (groups or []):
        label = g.get('title') or g['id']
        for tn in g.get('tables', []):
            group_of[tn] = label
    header = [('#', S_HEADER), ('Table', S_HEADER), ('Comment', S_HEADER),
              ('Columns', S_HEADER), ('Indexes', S_HEADER), ('Missing schema', S_HEADER)]
    widths = [5, 32, 50, 10, 10, 14]
    if groups:  # column omitted entirely (not left blank) when there are no groups
        header.append(('Group', S_HEADER))
        widths.append(20)
    rows = [[(f'{title} — table definitions', S_TITLE)], [], header]
    links = []
    for i, n in enumerate(names, 1):
        t = tables[n]
        r = len(rows) + 1
        s = alt(i - 1)
        row = [(i, s), (n, s), (t.get('comment', ''), s), (len(t['columns']), s),
               (len(t.get('indexes', [])), s),
               ('yes' if t.get('schema_missing') else '', s)]
        if groups:
            row.append((group_of.get(n, ''), s))
        rows.append(row)
        links.append((f'B{r}', f"'{sheet_of[n]}'", n))
    overview = _sheet_xml(rows, widths=widths, links=links)

    # ── per-table sheets ──
    for n in names:
        t = tables[n]
        rows = [[('Table', S_HEADER), n],
                [('Comment', S_HEADER), t.get('comment', '')],
                [],
                [('#', S_HEADER), ('Column', S_HEADER), ('Type', S_HEADER), ('Nullable', S_HEADER),
                 ('Default', S_HEADER), ('Key', S_HEADER), ('Extra', S_HEADER), ('Comment', S_HEADER)]]
        fk_cols = set(t.get('fk_columns') or
                      {a.get('foreign_key') for a in t['associations'] if a.get('foreign_key')})
        for i, c in enumerate(t['columns'], 1):
            key = 'PK' if c.get('primary') else ('FK' if c['name'] in fk_cols else '')
            s = alt(i - 1)
            rows.append([(i, s), (c['name'], s), (c.get('sql_type', c['type']), s),
                         ('YES' if c['nullable'] else 'NO', s),
                         (c.get('default', ''), s), (key, s), (c.get('extra', ''), s),
                         (c.get('comment', ''), s)])
        if t.get('indexes'):
            rows += [[], [('Indexes', S_SECTION)],
                     [('Name', S_HEADER), ('Columns', S_HEADER), ('Unique', S_HEADER)]]
            for i, ix in enumerate(t['indexes']):
                s = alt(i)
                rows.append([(ix['name'], s), (', '.join(ix['columns']), s),
                             ('UNIQUE' if ix['unique'] else '', s)])
        if t['associations']:
            rows += [[], [('Associations', S_SECTION)],
                     [('Type', S_HEADER), ('Name', S_HEADER), ('Target', S_HEADER), ('Via', S_HEADER)]]
            for i, a in enumerate(t['associations']):
                via = ('DB FK' if a.get('db_fk') else
                       'schema FK' if a.get('schema_fk') else
                       'inferred' if a.get('inferred') else
                       'manual' if a.get('manual') else 'code')
                s = alt(i)
                rows.append([(a['type'], s), (a['name'], s), (a['target'], s), (via, s)])
        sheets.append((sheet_of[n],
                       _sheet_xml(rows, widths=[12, 28, 24, 10, 18, 6, 16, 50])))

    # ── notes sheet (backlog #4) — omitted entirely when there are no notes ──
    if notes:
        rows = [[(f'{title} — notes', S_TITLE)], [],
                [('#', S_HEADER), ('ID', S_HEADER), ('Scope', S_HEADER), ('Target', S_HEADER),
                 ('Title', S_HEADER), ('Text', S_HEADER), ('Links', S_HEADER)]]
        for i, n in enumerate(sorted(notes, key=lambda n: n['id']), 1):
            if n['scope'] == 'global':
                target = ''
            elif n['scope'] == 'table':
                target = n['table']
            else:  # relation
                target = f"{n['source_table']} → {n['target']}"
            link_text = '; '.join((f"{l['label']} " if l.get('label') else '') + l['url']
                                  for l in n.get('links') or [])
            s = alt(i - 1)
            rows.append([(i, s), (n['id'], s), (n['scope'], s), (target, s),
                        (n.get('title', ''), s), (n['text'], s), (link_text, s)])
        sheets.append(('Notes', _sheet_xml(rows, widths=[5, 12, 12, 30, 20, 60, 40])))

    # ── groups sheet (backlog #4) — omitted entirely when there are no groups ──
    if groups:
        rows = [[(f'{title} — groups', S_TITLE)], [],
                [('#', S_HEADER), ('Group', S_HEADER), ('Title', S_HEADER),
                 ('Color', S_HEADER), ('Tables', S_HEADER)]]
        for i, g in enumerate(sorted(groups, key=lambda g: g['id']), 1):
            s = alt(i - 1)
            rows.append([(i, s), (g['id'], s), (g.get('title', ''), s), (g.get('color', ''), s),
                        (', '.join(sorted(g.get('tables', []))), s)])
        sheets.append(('Groups', _sheet_xml(rows, widths=[5, 16, 24, 12, 60])))

    sheets.insert(0, ('Tables', overview))

    # ── workbook plumbing ──
    sheet_entries = ''.join(
        f'<sheet name="{_xml(nm)}" sheetId="{i+1}" r:id="rId{i+1}"/>'
        for i, (nm, _) in enumerate(sheets))
    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets>{sheet_entries}</sheets></workbook>')
    styles_rid = f'rId{len(sheets)+1}'
    theme_rid = f'rId{len(sheets)+2}'
    wb_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + ''.join(f'<Relationship Id="rId{i+1}" '
                  'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                  f'Target="worksheets/sheet{i+1}.xml"/>' for i in range(len(sheets)))
        + f'<Relationship Id="{styles_rid}" '
          'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
          'Target="styles.xml"/>'
        + (f'<Relationship Id="{theme_rid}" '
           'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" '
           'Target="theme/theme1.xml"/>' if theme_xml else '')
        + '</Relationships>')
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<fonts count="{len(fonts)}">{"".join(fonts)}</fonts>'
        f'<fills count="{len(fills)}">{"".join(fills)}</fills>'
        f'<borders count="{len(borders)}">{"".join(borders)}</borders>'
        '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
        f'<cellXfs count="{len(cellxfs)}">{"".join(cellxfs)}</cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '</styleSheet>')
    content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        + ''.join(f'<Override PartName="/xl/worksheets/sheet{i+1}.xml" '
                  'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                  for i in range(len(sheets)))
        + ('<Override PartName="/xl/theme/theme1.xml" '
           'ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>' if theme_xml else '')
        + '</Types>')
    root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>')

    # Explicit ZipInfo metadata makes generated workbooks byte-deterministic.
    # Besides reproducible builds, this lets committed example workbooks use a
    # strict drift check instead of an unzip-and-normalize comparison.
    def put(z, name, data):
        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.create_system = 3
        info.external_attr = 0o600 << 16
        z.writestr(info, data)

    with zipfile.ZipFile(path, 'w') as z:
        put(z, '[Content_Types].xml', content_types)
        put(z, '_rels/.rels', root_rels)
        put(z, 'xl/workbook.xml', workbook)
        if theme_xml:
            put(z, 'xl/theme/theme1.xml', theme_xml)
        put(z, 'xl/_rels/workbook.xml.rels', wb_rels)
        put(z, 'xl/styles.xml', styles)
        for i, (_, xml) in enumerate(sheets):
            put(z, f'xl/worksheets/sheet{i+1}.xml', xml)

# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------
HTML_TEMPLATE = r"""__ERDSCOPE_VIEWER_TEMPLATE__"""

# ---------------------------------------------------------------------------
# Config file
# ---------------------------------------------------------------------------
# CLI flags mirrored by config keys of the same name -> (default value if
# neither config nor CLI supplies one). `relations` is config-only (no CLI
# equivalent — a list of individual FK declarations doesn't fit a flag).
