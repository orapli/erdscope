import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('erd', ROOT / 'erd.py')
erd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(erd)


from types import SimpleNamespace
import tempfile

class TestSchemaGridContract(unittest.TestCase):
    def setUp(self):
        table_rows = [('users', ''), ('posts', '')]
        col_rows = [
            ('users', 'id', 'int', 'int', 'NO', 'PRI', '', '', ''),
            ('users', 'email', 'varchar', 'varchar(255)', 'YES', '', '', '', ''),
            ('posts', 'id', 'int', 'int', 'NO', 'PRI', '', '', ''),
            ('posts', 'user_id', 'int', 'int', 'NO', 'MUL', '', '', ''),
        ]
        fk_rows = [('posts', 'user_id', 'users')]
        tables = erd.mysql_ir(table_rows, col_rows, fk_rows, [])
        groups = [{'id': 'content', 'title': 'Content', 'tables': ['posts']}]
        notes = [{'id': 'n1', 'target': {'type': 'table', 'table': 'users'}, 'text': 'User account table'},
                 {'id': 'n2', 'target': {'type': 'relation', 'source_table': 'posts', 'target_table': 'users', 'foreign_key': 'user_id'}, 'text': 'FK note'}]

        tmp = tempfile.mkdtemp()
        out = pathlib.Path(tmp) / 'out.html'
        args = SimpleNamespace(output=str(out), models=None, excel=None, max_rows=15,
                                only=None, exclude=None, infer_fk=False)
        erd._finish(tables, args, 'grid_test', groups=groups, notes=notes, groups_label='test')
        with open(out, 'r', encoding='utf-8') as f:
            self.html = f.read()

    def test_schema_grid_elements_and_buttons_exist(self):
        self.assertIn('id="schema-grid-modal"', self.html)
        self.assertIn('id="btn-grid-modal"', self.html)
        self.assertIn('id="btn-grid-copy"', self.html)
        self.assertIn('id="btn-grid-csv"', self.html)
        self.assertIn('id="btn-grid-mode-compact"', self.html)
        self.assertIn('id="btn-grid-mode-detailed"', self.html)
        self.assertIn('id="grid-search-scope"', self.html)
        self.assertIn('id="grid-search-exact"', self.html)

    def test_schema_grid_css_outside_print_block(self):
        # Ensure @media print is closed before /* ── Schema Grid Modal & Table Styling ── */
        print_idx = self.html.find('@media print')
        grid_css_idx = self.html.find('/* ── Schema Grid Modal & Table Styling ── */')
        self.assertGreater(grid_css_idx, print_idx)
        print_block = self.html[print_idx:grid_css_idx]
        self.assertIn('}', print_block)

    def test_schema_grid_data_contracts(self):
        # Must refer to g.title and g.id for group names
        self.assertIn('g.title', self.html)
        self.assertIn('g.id', self.html)
        # Must use notesForColumn for column-level relation note scoping
        self.assertIn('function notesForColumn', self.html)
        self.assertIn('notesForTable', self.html)
        self.assertIn('isFkCol', self.html)
        self.assertIn('grid-note-text', self.html)
        self.assertIn('focusGridTable', self.html)
        self.assertIn('window.focusGridTable = focusGridTable;', self.html)
        self.assertIn('matchFields', self.html)
        self.assertIn('filterScope', self.html)
        self.assertIn('exactMatch', self.html)

    def test_no_undefined_escape_html(self):
        # Must use esc(...) helper instead of non-existent escapeHtml(...)
        self.assertNotIn('escapeHtml(', self.html)

    def test_schema_grid_e2_batch_note_editing_contracts(self):
        import subprocess, json
        self.assertIn('saveGridNote', self.html)
        self.assertIn('bindNoteEvents', self.html)
        self.assertIn('grid-note-cell', self.html)
        self.assertIn('grid-inline-editor', self.html)

        tmp = tempfile.NamedTemporaryFile(suffix='.html', delete=False)
        tmp.write(self.html.encode('utf-8'))
        tmp.close()

        js_code = f"""
        const fs = require('fs');
        const html = fs.readFileSync({json.dumps(tmp.name)}, 'utf-8');
        const scriptMatch = html.match(/<script>([\\s\\S]*?)<\\/script>/);
        const scriptContent = scriptMatch[1];
        const dummyElem = {{ options: [], style: {{}}, addEventListener: () => {{}}, removeEventListener: () => {{}}, querySelectorAll: () => [], classList: {{ add: () => {{}}, remove: () => {{}}, toggle: () => {{}} }}, setAttribute: () => {{}}, getAttribute: () => null, insertBefore: () => {{}}, appendChild: () => ({{}}), getBoundingClientRect: () => ({{ width: 1000, height: 800, left: 0, top: 0, right: 1000, bottom: 800 }}), getBBox: () => ({{ width: 100, height: 20, x: 0, y: 0 }}) }};
        global.window = {{ addEventListener: () => {{}}, removeEventListener: () => {{}} }};
        global.location = {{ href: '', search: '', hash: '' }};
        global.localStorage = {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }};
        global.requestAnimationFrame = cb => cb();
        global.clearTimeout = () => {{}};
        global.setTimeout = () => {{}};
        global.document = {{
          title: 'Test Title',
          body: dummyElem,
          getElementById: () => dummyElem,
          querySelectorAll: () => [],
          addEventListener: () => {{}},
          createElement: () => dummyElem,
          createElementNS: () => dummyElem
        }};
        const vm = require('vm');
        const context = vm.createContext({{ document: global.document, window: global.window, location: global.location, localStorage: global.localStorage, requestAnimationFrame: global.requestAnimationFrame, clearTimeout: global.clearTimeout, setTimeout: global.setTimeout, console }});
        vm.runInContext(scriptContent, context);
        vm.runInContext("saveGridNote('table', 'posts', null, 'New Posts Note');", context);
        const notes = vm.runInContext('NOTES', context);
        console.log(JSON.stringify(notes));
        """
        proc = subprocess.run(['node', '-e', js_code], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Node.js execution failed: {proc.stderr}")
        notes = json.loads(proc.stdout.strip())
        posts_note = next((n for n in notes if n.get('table') == 'posts'), None)
        self.assertIsNotNone(posts_note)
        self.assertEqual(posts_note['text'], 'New Posts Note')


if __name__ == '__main__':
    unittest.main()
