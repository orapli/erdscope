import importlib.util
import pathlib
import unittest
import tempfile
import json
import subprocess
import shutil
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('erd', ROOT / 'erd.py')
erd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(erd)


class TestProposedSchemaAndUnsavedConfig(unittest.TestCase):
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
        
        tmp = tempfile.mkdtemp()
        self.out_path = pathlib.Path(tmp) / 'out.html'
        args = SimpleNamespace(output=str(self.out_path), models=None, excel=None, max_rows=15,
                                only=None, exclude=None, infer_fk=False)
        erd._finish(tables, args, 'proposed_test')

    @unittest.skipUnless(shutil.which('node'), 'node not available')
    def test_proposed_table_and_column_export_config_json(self):
        js_code = f"""
        const fs = require('fs');
        const html = fs.readFileSync({json.dumps(str(self.out_path))}, 'utf-8');
        const scriptMatch = html.match(/<script>([\\s\\S]*?)<\\/script>/);
        const scriptContent = scriptMatch[1];
        const elemMap = {{}};
        const createFormInput = (val = '') => ({{ value: val, style: {{}}, addEventListener: () => {{}}, removeEventListener: () => {{}}, classList: {{ add: () => {{}}, remove: () => {{}}, toggle: () => {{}} }} }});
        
        const toasts = [];
        global.showToast = msg => toasts.push(msg);
        global.window = {{ addEventListener: () => {{}}, removeEventListener: () => {{}} }};
        global.location = {{ href: '', search: '', hash: '' }};
        global.localStorage = {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }};
        global.requestAnimationFrame = cb => cb();
        global.clearTimeout = () => {{}};
        global.setTimeout = () => {{}};
        const dummyElem = {{ options: [], style: {{}}, addEventListener: () => {{}}, removeEventListener: () => {{}}, querySelectorAll: () => [], classList: {{ add: () => {{}}, remove: () => {{}}, toggle: () => {{}} }}, setAttribute: () => {{}}, getAttribute: () => null, insertBefore: () => {{}}, appendChild: () => ({{}}), getBoundingClientRect: () => ({{ width: 1000, height: 800, left: 0, top: 0, right: 1000, bottom: 800 }}), getBBox: () => ({{ width: 100, height: 20, x: 0, y: 0 }}) }};
        global.document = {{
          title: 'Test Title',
          body: dummyElem,
          getElementById: id => elemMap[id] || dummyElem,
          querySelectorAll: () => [],
          addEventListener: () => {{}},
          createElement: () => dummyElem,
          createElementNS: () => dummyElem
        }};
        const vm = require('vm');
        const context = vm.createContext({{ document: global.document, window: global.window, location: global.location, localStorage: global.localStorage, requestAnimationFrame: global.requestAnimationFrame, clearTimeout: global.clearTimeout, setTimeout: global.setTimeout, console, showToast: global.showToast, toasts, elemMap }});
        vm.runInContext(scriptContent, context);
        vm.runInContext('showToast = msg => toasts.push(msg);', context);
        
        const initialDirty = vm.runInContext('isConfigDirty', context);
        
        // 1. Add proposed table
        vm.runInContext("addProposedTable('proposed_orders', '注文提案テーブル', 'ToBe table', 'Orders Domain');", context);
        const dirtyAfterTable = vm.runInContext('isConfigDirty', context);
        
        // 2. Add proposed column to existing table
        vm.runInContext("addProposedColumn('users', 'nickname', 'ニックネーム', 'varchar(255)', 'YES', 'Proposed user nickname');", context);
        
        // 3. Export config JSON
        const exportedJson = vm.runInContext('exportConfigJSON();', context);
        const dirtyAfterExport = vm.runInContext('isConfigDirty', context);
        
        // 4. Mark clean after download/copy
        vm.runInContext('markConfigClean();', context);
        const cleanAfterSave = vm.runInContext('isConfigDirty', context);
        
        console.log(JSON.stringify({{ initialDirty, dirtyAfterTable, dirtyAfterExport, cleanAfterSave, exportedJson: JSON.parse(exportedJson) }}));
        """
        proc = subprocess.run(['node', '-e', js_code], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Node.js execution failed: {proc.stderr}")
        res = json.loads(proc.stdout.strip())
        
        self.assertFalse(res['initialDirty'])
        self.assertTrue(res['dirtyAfterTable'])
        self.assertTrue(res['dirtyAfterExport'])
        self.assertFalse(res['cleanAfterSave'])
        
        cfg = res['exportedJson']
        self.assertIn('tables', cfg)
        tables = cfg['tables']
        self.assertIn('proposed_orders', tables)
        self.assertIn('users', tables)
        
        prop_order = tables['proposed_orders']
        self.assertEqual(prop_order['logical_name'], '注文提案テーブル')
        self.assertEqual(prop_order['comment'], 'ToBe table')
        self.assertEqual(prop_order['columns'][0]['name'], 'id')
        
        users_table = tables['users']
        nick_col = next((c for c in users_table['columns'] if c['name'] == 'nickname'), None)
        self.assertIsNotNone(nick_col)
        self.assertEqual(nick_col['logical_name'], 'ニックネーム')
        self.assertEqual(nick_col['type'], 'varchar(255)')

    @unittest.skipUnless(shutil.which('node'), 'node not available')
    def test_persisted_config_restores_on_reload(self):
        js_code = f"""
        const fs = require('fs');
        const html = fs.readFileSync({json.dumps(str(self.out_path))}, 'utf-8');
        const scriptMatch = html.match(/<script>([\\s\\S]*?)<\\/script>/);
        const scriptContent = scriptMatch[1];
        
        const storageMap = {{}};
        global.localStorage = {{
          getItem: k => storageMap[k] || null,
          setItem: (k, v) => storageMap[k] = String(v),
          removeItem: k => delete storageMap[k]
        }};
        global.window = {{ addEventListener: () => {{}}, removeEventListener: () => {{}} }};
        global.location = {{ href: '', search: '', hash: '' }};
        global.requestAnimationFrame = cb => cb();
        global.clearTimeout = () => {{}};
        global.setTimeout = () => {{}};
        const dummyElem = {{ options: [], style: {{}}, addEventListener: () => {{}}, removeEventListener: () => {{}}, querySelectorAll: () => [], classList: {{ add: () => {{}}, remove: () => {{}}, toggle: () => {{}} }}, setAttribute: () => {{}}, getAttribute: () => null, insertBefore: () => {{}}, appendChild: () => ({{}}), getBoundingClientRect: () => ({{ width: 1000, height: 800, left: 0, top: 0, right: 1000, bottom: 800 }}), getBBox: () => ({{ width: 100, height: 20, x: 0, y: 0 }}) }};
        let docTitle = 'proposed_test';
        global.document = {{
          get title() {{ return docTitle; }},
          set title(v) {{ docTitle = v; }},
          body: dummyElem,
          getElementById: () => dummyElem,
          querySelectorAll: () => [],
          addEventListener: () => {{}},
          createElement: () => dummyElem,
          createElementNS: () => dummyElem
        }};
        const vm = require('vm');
        
        const toasts = [];
        global.showToast = msg => toasts.push(msg);
        
        // Session 1: Add proposed table
        const ctx1 = vm.createContext({{ document: global.document, window: global.window, location: global.location, localStorage: global.localStorage, requestAnimationFrame: global.requestAnimationFrame, clearTimeout: global.clearTimeout, setTimeout: global.setTimeout, console, showToast: global.showToast, toasts }});
        vm.runInContext(scriptContent, ctx1);
        vm.runInContext("addProposedTable('persisted_table', '永続テーブル', '', '');", ctx1);
        
        console.warn('STORAGE MAP:', JSON.stringify(storageMap));
        
        // Session 2: Reload page (fresh VM context with shared localStorage)
        const ctx2 = vm.createContext({{ document: global.document, window: global.window, location: global.location, localStorage: global.localStorage, requestAnimationFrame: global.requestAnimationFrame, clearTimeout: global.clearTimeout, setTimeout: global.setTimeout, console, showToast: global.showToast, toasts }});
        vm.runInContext(scriptContent, ctx2);
        
        const restoredTable = vm.runInContext("DATA.tables['persisted_table']", ctx2);
        console.log(JSON.stringify(restoredTable || null));
        """
        proc = subprocess.run(['node', '-e', js_code], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Node.js execution failed: {proc.stderr}")
        self.assertTrue(bool(proc.stdout.strip()), f"Empty stdout. Stderr: {proc.stderr}")
        res = json.loads(proc.stdout.strip())
        self.assertIsNotNone(res, f"restoredTable was null. Stderr: {proc.stderr}")
        self.assertEqual(res['name'], 'persisted_table')
        self.assertEqual(res['logical_name'], '永続テーブル')

    @unittest.skipUnless(shutil.which('node'), 'node not available')
    def test_export_updated_html(self):
        js_code = f"""
        const fs = require('fs');
        const html = fs.readFileSync({json.dumps(str(self.out_path))}, 'utf-8');
        const scriptMatch = html.match(/<script>([\\s\\S]*?)<\\/script>/);
        const scriptContent = scriptMatch[1];
        
        let downloadedHtml = '';
        global.Blob = class {{ constructor(parts) {{ this.content = parts.join(''); }} }};
        global.URL = {{ createObjectURL: b => {{ downloadedHtml = b.content; return 'blob:mock'; }}, revokeObjectURL: () => {{}} }};
        global.window = {{ addEventListener: () => {{}}, removeEventListener: () => {{}} }};
        global.location = {{ href: '', search: '', hash: '' }};
        global.localStorage = {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }};
        global.requestAnimationFrame = cb => cb();
        global.clearTimeout = () => {{}};
        global.setTimeout = () => {{}};
        const dummyElem = {{ options: [], style: {{}}, addEventListener: () => {{}}, removeEventListener: () => {{}}, querySelectorAll: () => [], classList: {{ add: () => {{}}, remove: () => {{}}, toggle: () => {{}} }}, setAttribute: () => {{}}, getAttribute: () => null, insertBefore: () => {{}}, appendChild: () => ({{}}), removeChild: () => ({{}}), click: () => ({{}}), getBoundingClientRect: () => ({{ width: 1000, height: 800, left: 0, top: 0, right: 1000, bottom: 800 }}), getBBox: () => ({{ width: 100, height: 20, x: 0, y: 0 }}) }};
        let docTitle = 'proposed_test';
        global.document = {{
          get title() {{ return docTitle; }},
          set title(v) {{ docTitle = v; }},
          body: dummyElem,
          documentElement: {{ outerHTML: html }},
          getElementById: () => dummyElem,
          querySelectorAll: () => [],
          addEventListener: () => {{}},
          createElement: () => dummyElem,
          createElementNS: () => dummyElem
        }};
        const vm = require('vm');
        const toasts = [];
        global.showToast = msg => toasts.push(msg);
        
        const ctx = vm.createContext({{ document: global.document, window: global.window, location: global.location, localStorage: global.localStorage, requestAnimationFrame: global.requestAnimationFrame, clearTimeout: global.clearTimeout, setTimeout: global.setTimeout, Blob: global.Blob, URL: global.URL, console, showToast: global.showToast, toasts }});
        vm.runInContext(scriptContent, ctx);
        vm.runInContext("addProposedTable('xss_tbl', '</script><script>alert(1)</script>', '', 'Test Group');", ctx);
        vm.runInContext("exportUpdatedHTML();", ctx);
        
        // Load the exported HTML script content in a fresh VM context to verify execution & GROUPS reload
        const newScriptMatch = downloadedHtml.match(/<script>([\\s\\S]*?)<\\/script>/);
        const newScript = newScriptMatch ? newScriptMatch[1] : '';
        const newCtx = vm.createContext({{ document: global.document, window: global.window, location: global.location, localStorage: global.localStorage, requestAnimationFrame: global.requestAnimationFrame, clearTimeout: global.clearTimeout, setTimeout: global.setTimeout, console, showToast: global.showToast, toasts }});
        vm.runInContext(newScript, newCtx);
        
        const restoredGroups = vm.runInContext("GROUPS", newCtx);
        const restoredXssTable = vm.runInContext("DATA.tables['xss_tbl']", newCtx);
        const restoredDirtyState = vm.runInContext("isConfigDirty", newCtx);
        
        console.log(JSON.stringify({{
          hasXssTable: downloadedHtml.includes('xss_tbl'),
          safeScriptTag: !downloadedHtml.includes('</script><script>alert(1)</script>'),
          escapedScriptTag: downloadedHtml.includes('\\u003c\\u002fscript\\u003e'),
          restoredGroupTitle: (restoredGroups[0] || {{}}).title,
          restoredTableLogical: (restoredXssTable || {{}}).logical_name,
          restoredDirtyState
        }}));
        """
        proc = subprocess.run(['node', '-e', js_code], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Node.js execution failed: {proc.stderr}")
        res = json.loads(proc.stdout.strip())
        self.assertTrue(res['hasXssTable'])
        self.assertTrue(res['safeScriptTag'], "Exported HTML contains raw unescaped script tag (XSS vulnerability)")
        self.assertTrue(res['escapedScriptTag'], "Exported HTML should escape script tags using safeJsonForScript")
        self.assertEqual(res['restoredGroupTitle'], 'Test Group')
        self.assertEqual(res['restoredTableLogical'], '</script><script>alert(1)</script>')
        self.assertFalse(res['restoredDirtyState'], "Newly opened exported HTML should be in a clean state (isConfigDirty = false)")

    @unittest.skipUnless(shutil.which('node'), 'node not available')
    def test_export_updated_html_crlf_input(self):
        # Regression test for B6: on Windows, Path.write_text() used to translate
        # every '\n' in the generated HTML to '\r\n'. generateUpdatedHTMLSource()
        # in viewer.html matched DATA/NOTES/GROUPS with a bare `;\n`, which never
        # matches inside a CRLF run (every \n is preceded by \r) — so the export
        # silently kept the ORIGINAL DATA/NOTES/GROUPS instead of the edited ones.
        # This reproduces that CRLF path on any platform (no Windows required) by
        # converting the fixture HTML to CRLF before feeding it through the exact
        # same node/vm export logic as test_export_updated_html.
        lf_html = self.out_path.read_text(encoding='utf-8')
        crlf_html = lf_html.replace('\r\n', '\n').replace('\n', '\r\n')
        crlf_path = self.out_path.with_name('out_crlf.html')
        crlf_path.write_bytes(crlf_html.encode('utf-8'))

        js_code = f"""
        const fs = require('fs');
        const html = fs.readFileSync({json.dumps(str(crlf_path))}, 'utf-8');
        const scriptMatch = html.match(/<script>([\\s\\S]*?)<\\/script>/);
        const scriptContent = scriptMatch[1];

        let downloadedHtml = '';
        global.Blob = class {{ constructor(parts) {{ this.content = parts.join(''); }} }};
        global.URL = {{ createObjectURL: b => {{ downloadedHtml = b.content; return 'blob:mock'; }}, revokeObjectURL: () => {{}} }};
        global.window = {{ addEventListener: () => {{}}, removeEventListener: () => {{}} }};
        global.location = {{ href: '', search: '', hash: '' }};
        global.localStorage = {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }};
        global.requestAnimationFrame = cb => cb();
        global.clearTimeout = () => {{}};
        global.setTimeout = () => {{}};
        const dummyElem = {{ options: [], style: {{}}, addEventListener: () => {{}}, removeEventListener: () => {{}}, querySelectorAll: () => [], classList: {{ add: () => {{}}, remove: () => {{}}, toggle: () => {{}} }}, setAttribute: () => {{}}, getAttribute: () => null, insertBefore: () => {{}}, appendChild: () => ({{}}), removeChild: () => ({{}}), click: () => ({{}}), getBoundingClientRect: () => ({{ width: 1000, height: 800, left: 0, top: 0, right: 1000, bottom: 800 }}), getBBox: () => ({{ width: 100, height: 20, x: 0, y: 0 }}) }};
        let docTitle = 'proposed_test';
        global.document = {{
          get title() {{ return docTitle; }},
          set title(v) {{ docTitle = v; }},
          body: dummyElem,
          documentElement: {{ outerHTML: html }},
          getElementById: () => dummyElem,
          querySelectorAll: () => [],
          addEventListener: () => {{}},
          createElement: () => dummyElem,
          createElementNS: () => dummyElem
        }};
        const vm = require('vm');
        const toasts = [];
        global.showToast = msg => toasts.push(msg);

        const ctx = vm.createContext({{ document: global.document, window: global.window, location: global.location, localStorage: global.localStorage, requestAnimationFrame: global.requestAnimationFrame, clearTimeout: global.clearTimeout, setTimeout: global.setTimeout, Blob: global.Blob, URL: global.URL, console, showToast: global.showToast, toasts }});
        vm.runInContext(scriptContent, ctx);
        vm.runInContext("addProposedTable('xss_tbl', '</script><script>alert(1)</script>', '', 'Test Group');", ctx);
        vm.runInContext("exportUpdatedHTML();", ctx);

        // Load the exported HTML script content in a fresh VM context to verify execution & GROUPS reload
        const newScriptMatch = downloadedHtml.match(/<script>([\\s\\S]*?)<\\/script>/);
        const newScript = newScriptMatch ? newScriptMatch[1] : '';
        const newCtx = vm.createContext({{ document: global.document, window: global.window, location: global.location, localStorage: global.localStorage, requestAnimationFrame: global.requestAnimationFrame, clearTimeout: global.clearTimeout, setTimeout: global.setTimeout, console, showToast: global.showToast, toasts }});
        vm.runInContext(newScript, newCtx);

        const restoredGroups = vm.runInContext("GROUPS", newCtx);
        const restoredXssTable = vm.runInContext("DATA.tables['xss_tbl']", newCtx);

        console.log(JSON.stringify({{
          hasXssTable: downloadedHtml.includes('xss_tbl'),
          restoredGroupTitle: (restoredGroups[0] || {{}}).title,
          restoredTableLogical: (restoredXssTable || {{}}).logical_name,
        }}));
        """
        proc = subprocess.run(['node', '-e', js_code], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Node.js execution failed: {proc.stderr}")
        res = json.loads(proc.stdout.strip())
        self.assertTrue(res['hasXssTable'])
        self.assertEqual(res['restoredGroupTitle'], 'Test Group')
        self.assertEqual(res['restoredTableLogical'], '</script><script>alert(1)</script>')

    @unittest.skipUnless(shutil.which('node'), 'node not available')
    def test_startup_does_not_write_empty_persisted_config(self):
        js_code = f"""
        const fs = require('fs');
        const html = fs.readFileSync({json.dumps(str(self.out_path))}, 'utf-8');
        const scriptMatch = html.match(/<script>([\\s\\S]*?)<\\/script>/);
        const scriptContent = scriptMatch[1];
        
        const storageMap = {{}};
        global.localStorage = {{
          getItem: k => storageMap[k] || null,
          setItem: (k, v) => storageMap[k] = String(v),
          removeItem: k => delete storageMap[k]
        }};
        global.window = {{ addEventListener: () => {{}}, removeEventListener: () => {{}} }};
        global.location = {{ href: '', search: '', hash: '' }};
        global.requestAnimationFrame = cb => cb();
        global.clearTimeout = () => {{}};
        global.setTimeout = () => {{}};
        const dummyElem = {{ options: [], style: {{}}, addEventListener: () => {{}}, removeEventListener: () => {{}}, querySelectorAll: () => [], classList: {{ add: () => {{}}, remove: () => {{}}, toggle: () => {{}} }}, setAttribute: () => {{}}, getAttribute: () => null, insertBefore: () => {{}}, appendChild: () => ({{}}), removeChild: () => ({{}}), click: () => ({{}}), getBoundingClientRect: () => ({{ width: 1000, height: 800, left: 0, top: 0, right: 1000, bottom: 800 }}), getBBox: () => ({{ width: 100, height: 20, x: 0, y: 0 }}) }};
        let docTitle = 'proposed_test';
        global.document = {{
          get title() {{ return docTitle; }},
          set title(v) {{ docTitle = v; }},
          body: dummyElem,
          documentElement: {{ outerHTML: html }},
          getElementById: () => dummyElem,
          querySelectorAll: () => [],
          addEventListener: () => {{}},
          createElement: () => dummyElem,
          createElementNS: () => dummyElem
        }};
        const vm = require('vm');
        const toasts = [];
        global.showToast = msg => toasts.push(msg);
        
        // Startup test: Run script content without modifying anything
        const ctx = vm.createContext({{ document: global.document, window: global.window, location: global.location, localStorage: global.localStorage, requestAnimationFrame: global.requestAnimationFrame, clearTimeout: global.clearTimeout, setTimeout: global.setTimeout, console, showToast: global.showToast, toasts }});
        vm.runInContext(scriptContent, ctx);
        
        const hasPersistedConfig = 'erd:proposed_test:persisted_config' in storageMap;
        console.log(JSON.stringify({{ hasPersistedConfig, keys: Object.keys(storageMap) }}));
        """
        proc = subprocess.run(['node', '-e', js_code], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Node.js execution failed: {proc.stderr}")
        res = json.loads(proc.stdout.strip())
        self.assertFalse(res['hasPersistedConfig'], "Unmodified startup should not write empty persisted_config to LocalStorage")


if __name__ == '__main__':
    unittest.main()
