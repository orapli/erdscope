"""Excel notes/groups sheets (backlog #4) — activating write_excel(notes=,
groups=), which were wired-but-unused since notes/groups Phase 1.

Covers the Notes sheet (scope/target/title/text/links rendering, id sort),
the Groups sheet (id/title/color/tables, id sort), and the overview sheet's
Group column — all against a hand-built config-only schema, driven through
main() the same way tests/test_notes.py's _NoDBDriver does. The two
byte-equality regression tests (no notes/groups -> no sheet at all) live in
tests/test_notes.py / tests/test_groups.py alongside the rest of each
feature's own suite; this file is about the CONTENT of the new sheets.

Run from the repository root:
    python3 -m unittest tests.test_excel_notes_groups -v
"""
import importlib.util
import json
import re
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location('erd', ROOT / 'erd.py')
erd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(erd)


class _NoDBDriver(unittest.TestCase):
    def setUp(self):
        def _no_db(url):
            raise AssertionError('DB should not be contacted in a config-only test')
        self._orig = (erd.parse_mysql, erd.parse_postgres)
        erd.parse_mysql = _no_db
        erd.parse_postgres = _no_db
        self._argv = sys.argv
        import os
        self._cwd = os.getcwd()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: setattr(sys, 'argv', self._argv))
        self.addCleanup(os.chdir, self._cwd)
        self.addCleanup(lambda: (setattr(erd, 'parse_mysql', self._orig[0]),
                                 setattr(erd, 'parse_postgres', self._orig[1])))
        os.chdir(self.tmp.name)

    def _p(self, name):
        return str(Path(self.tmp.name) / name)

    def _run(self, *argv):
        sys.argv = ['erd.py', *argv]
        erd.main()

    def _sheet(self, xlsx_path, sheet_name):
        with zipfile.ZipFile(xlsx_path) as z:
            wb = z.read('xl/workbook.xml').decode('utf-8')
            sheet_names = re.findall(r'<sheet name="([^"]+)"', wb)
            idx = sheet_names.index(sheet_name)
            return z.read(f'xl/worksheets/sheet{idx + 1}.xml').decode('utf-8')

    def _schema(self):
        return {
            'tables': {
                'users': {'columns': [{'name': 'id', 'primary': True}],
                          'associations': [{'type': 'has_many', 'name': 'posts',
                                           'target': 'posts'}]},
                'posts': {'columns': [{'name': 'id', 'primary': True},
                                      {'name': 'user_id'}],
                          'associations': [{'type': 'belongs_to', 'name': 'user',
                                           'target': 'users', 'foreign_key': 'user_id'}]},
            },
            'notes': [
                {'id': 'n2', 'target': {'type': 'global'}, 'text': 'Simple blog schema'},
                {'id': 'n1', 'target': {'type': 'table', 'table': 'users'}, 'title': 'PII',
                 'text': 'email is personal data',
                 'links': [{'url': 'https://example.com/adr-1', 'label': 'ADR-1'}]},
                {'id': 'n3', 'target': {'type': 'relation', 'source_table': 'posts',
                                       'target_table': 'users'},
                 'text': 'cascade on delete'},
            ],
            'groups': [
                {'id': 'g2', 'title': 'Core', 'color': '#3366ff', 'tables': ['posts', 'users']},
            ],
        }

    def _write(self, schema):
        path = self._p('c.json')
        Path(path).write_text(json.dumps(schema))
        out, xlsx = self._p('out.html'), self._p('defs.xlsx')
        self._run('--config', path, '-o', out, '--excel', xlsx)
        return xlsx


class TestNotesSheet(_NoDBDriver):
    def test_notes_sorted_by_id(self):
        # config lists n2, n1, n3 (in that order) — the sheet must come out
        # id-sorted: n1 (table note) < n2 (global) < n3 (relation)
        xlsx = self._write(self._schema())
        xml = self._sheet(xlsx, 'Notes')
        self.assertLess(xml.index('email is personal data'), xml.index('Simple blog schema'))
        self.assertLess(xml.index('Simple blog schema'), xml.index('cascade on delete'))
        ids = re.findall(r'<is><t[^>]*>(n\d)</t></is>', xml)
        self.assertEqual(ids, ['n1', 'n2', 'n3'])

    def test_global_note_has_empty_target(self):
        xlsx = self._write(self._schema())
        xml = self._sheet(xlsx, 'Notes')
        self.assertIn('Simple blog schema', xml)
        self.assertIn('>global<', xml)

    def test_table_note_target_and_title(self):
        xlsx = self._write(self._schema())
        xml = self._sheet(xlsx, 'Notes')
        self.assertIn('>users<', xml)
        self.assertIn('>PII<', xml)
        self.assertIn('email is personal data', xml)

    def test_relation_note_target_shows_source_and_target_table(self):
        xlsx = self._write(self._schema())
        xml = self._sheet(xlsx, 'Notes')
        self.assertIn('posts', xml)
        self.assertIn('cascade on delete', xml)

    def test_links_rendered_with_label(self):
        xlsx = self._write(self._schema())
        xml = self._sheet(xlsx, 'Notes')
        self.assertIn('ADR-1', xml)
        self.assertIn('https://example.com/adr-1', xml)


class TestGroupsSheet(_NoDBDriver):
    def test_group_row_has_title_color_and_sorted_tables(self):
        xlsx = self._write(self._schema())
        xml = self._sheet(xlsx, 'Groups')
        self.assertIn('>g2<', xml)
        self.assertIn('Core', xml)
        self.assertIn('#3366ff', xml)
        self.assertIn('posts, users', xml)

    def test_groups_sorted_by_id(self):
        schema = self._schema()
        schema['groups'] = [
            {'id': 'g2', 'tables': ['users']},
            {'id': 'g1', 'tables': ['posts']},
        ]
        xlsx = self._write(schema)
        xml = self._sheet(xlsx, 'Groups')
        self.assertLess(xml.index('>g1<'), xml.index('>g2<'))


class TestOverviewGroupColumn(_NoDBDriver):
    def test_group_column_shows_table_label(self):
        xlsx = self._write(self._schema())
        xml = self._sheet(xlsx, 'Tables')
        self.assertIn('Group', xml)   # header
        self.assertIn('Core', xml)    # both users/posts belong to 'Core'

    def test_table_with_no_group_has_blank_cell_not_missing_column(self):
        schema = self._schema()
        schema['groups'] = [{'id': 'g1', 'tables': ['posts']}]  # users has none
        xlsx = self._write(schema)
        xml = self._sheet(xlsx, 'Tables')
        self.assertIn('Group', xml)


class TestNotesAndGroupsCoexist(_NoDBDriver):
    def test_both_sheets_present_alongside_table_sheets(self):
        xlsx = self._write(self._schema())
        with zipfile.ZipFile(xlsx) as z:
            wb = z.read('xl/workbook.xml').decode('utf-8')
        sheet_names = re.findall(r'<sheet name="([^"]+)"', wb)
        self.assertEqual(sheet_names[0], 'Tables')
        self.assertIn('Notes', sheet_names)
        self.assertIn('Groups', sheet_names)
        self.assertIn('posts', sheet_names)
        self.assertIn('users', sheet_names)


if __name__ == '__main__':
    unittest.main()
