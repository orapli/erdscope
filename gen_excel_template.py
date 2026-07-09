#!/usr/bin/env python3
"""Regenerate excel-template.xlsx — the default --excel-template example,
bundled in the repo so users can copy it and edit its five contract cells
(in Excel/LibreOffice) to restyle --excel output without touching Python.

See write_excel()'s --excel-template contract in erd.py: the template's
first worksheet, column A, rows 1-5, must be styled as
title/header/data/data-alt/section respectively. This script builds
exactly that sheet, pre-styled with erdscope's own built-in look, so the
bundled file is a working (not just documented) example — open it, tweak
one of the five A1:A5 cells' fill/font/border in Excel, save, and pass it
back as --excel-template. Column B carries a label per row so the sheet
explains itself when opened; rows below the contract hold free-text
instructions and are never read by erdscope.

    python3 gen_excel_template.py
"""
import importlib.util
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('erd', ROOT / 'erd.py')
erd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(erd)

# role, cell text, label (column B) — rows 1-5, matching write_excel's
# --excel-template contract exactly (do not insert/reorder rows above
# row 5, or the contract cells land on the wrong row)
CONTRACT = [
    (erd.S_TITLE, 'Title', 'Style of the workbook title row'),
    (erd.S_HEADER, 'Header', 'Style of column header rows'),
    (erd.S_DATA, 'Data', 'Style of a plain data row'),
    (erd.S_DATA_ALT, 'Data (alternate row)',
     'Style of alternating/zebra-striped data rows — match "Data" for no striping'),
    (erd.S_SECTION, 'Section', 'Style of section labels (e.g. "Indexes", "Associations")'),
]

rows = [[(name, role), label] for role, name, label in CONTRACT]
rows += [[],
    [('How this template works', erd.S_SECTION)],
    ['Rows 1-5 above are read by erdscope: each row’s style (font / fill / '
     'border — not its text) becomes the matching role in every workbook '
     '--excel generates. Edit a cell’s style in Excel or LibreOffice, save '
     'this file (or a copy), then pass it with --excel-template path/to/file.xlsx.'],
    ['Only cells A1:A5 are read. Everything else on this sheet, and every '
     'other sheet, is ignored — free to use for your own notes.'],
]

fonts, fills, borders, cellxfs = erd._build_stylesheet_parts(erd._default_role_styles())
sheet_xml = erd._sheet_xml(rows, widths=[28, 70])

content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    '</Types>')
root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="xl/workbook.xml"/></Relationships>')
workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    '<sheets><sheet name="Styles" sheetId="1" r:id="rId1"/></sheets></workbook>')
wb_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
    'Target="worksheets/sheet1.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
    'Target="styles.xml"/></Relationships>')
styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    f'<fonts count="{len(fonts)}">{"".join(fonts)}</fonts>'
    f'<fills count="{len(fills)}">{"".join(fills)}</fills>'
    f'<borders count="{len(borders)}">{"".join(borders)}</borders>'
    '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
    f'<cellXfs count="{len(cellxfs)}">{"".join(cellxfs)}</cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    '</styleSheet>')

out = ROOT / 'excel-template.xlsx'
with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
    z.writestr('[Content_Types].xml', content_types)
    z.writestr('_rels/.rels', root_rels)
    z.writestr('xl/workbook.xml', workbook)
    z.writestr('xl/_rels/workbook.xml.rels', wb_rels)
    z.writestr('xl/styles.xml', styles)
    z.writestr('xl/worksheets/sheet1.xml', sheet_xml)
print(f'Generated: {out}', file=sys.stderr)
