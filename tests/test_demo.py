"""Tests for `erdscope demo` (see src/erdscope/demo.py).

`erdscope demo` exists because pyproject.toml ships this project as a single
module (`py-modules = ["erd"]`), so examples/demo_shop.db never reaches a
`pip install`ed user — there's no file for the README quickstart's
`erdscope sqlite:///examples/demo_shop.db` to point at. `demo` builds a
throwaway copy of the same sample database in a temp directory instead, and
otherwise behaves like a normal sqlite:// run (--only/--excel/etc. all still
apply).

Most of this drives the CLI as a subprocess (like
TestMainConfigIntegration.test_cli_requires_database_url in test_erd.py) since
the interesting behavior IS the argparse sentinel branch in main(). Run from
the repository root:
    python3 -m unittest tests.test_demo -v
"""
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location('erd', ROOT / 'erd.py')
erd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(erd)


def _run(*extra_args):
    return subprocess.run(
        [sys.executable, str(ROOT / 'erd.py'), 'demo', *extra_args],
        capture_output=True, text=True)


class TestDemoCommand(unittest.TestCase):
    def test_generates_html_with_demo_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out.html'
            res = _run('--no-open', '-o', str(out))
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertTrue(out.exists())
            html = out.read_text(encoding='utf-8')
            data = json.loads(re.search(r'^const DATA = (.+);$', html,
                                        re.MULTILINE).group(1))
            table_names = set(data['tables'])
            self.assertIn('users', table_names)
            self.assertIn('orders', table_names)
            self.assertIn('order_items', table_names)

    def test_default_output_is_erd_demo_html_not_erd_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = subprocess.run(
                [sys.executable, str(ROOT / 'erd.py'), 'demo', '--no-open'],
                capture_output=True, text=True, cwd=tmp)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertTrue((Path(tmp) / 'erd_demo.html').exists())
            self.assertFalse((Path(tmp) / 'erd.html').exists())

    def test_only_filter_applies(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out.html'
            res = _run('--only', 'order*', '--no-open', '-o', str(out))
            self.assertEqual(res.returncode, 0, res.stderr)
            html = out.read_text(encoding='utf-8')
            data = json.loads(re.search(r'^const DATA = (.+);$', html,
                                        re.MULTILINE).group(1))
            self.assertEqual(set(data['tables']), {'orders', 'order_items', 'order_coupons'})

    def test_excel_flag_applies(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out.html'
            xlsx = Path(tmp) / 'out.xlsx'
            res = _run('--excel', str(xlsx), '--no-open', '-o', str(out))
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertTrue(out.exists())
            self.assertTrue(xlsx.exists())

    def test_explicit_config_flag_warns_and_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / 'custom.json'
            cfg.write_text('{"max_rows": 3}', encoding='utf-8')
            out = Path(tmp) / 'out.html'
            res = _run('--config', str(cfg), '--no-open', '-o', str(out))
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn('ignored', res.stderr)
            self.assertIn('demo', res.stderr)
            # the config's max_rows: 3 must NOT have taken effect
            html = out.read_text(encoding='utf-8')
            self.assertIn('let maxRows = 15;', html)

    def test_no_open_flag_accepted_on_a_normal_run(self):
        # --no-open must be a no-op (not an error) outside `demo` too.
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / 'not-a-url'
            res = subprocess.run(
                [sys.executable, str(ROOT / 'erd.py'), str(fixture), '--no-open'],
                capture_output=True, text=True)
            self.assertNotEqual(res.returncode, 0)
            # fails for the expected reason (bad URL scheme), not an
            # unrecognized-argument error about --no-open
            self.assertIn('unrecognized database URL scheme', res.stderr)


class TestBuildDemoDb(unittest.TestCase):
    """Unit tests for build_demo_db() / DEMO_SCHEMA_SQL directly, no subprocess."""

    def test_creates_expected_tables_and_row_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / 'demo.db'
            returned = erd.build_demo_db(db_path)
            self.assertEqual(returned, db_path)
            self.assertTrue(db_path.exists())

            import sqlite3
            conn = sqlite3.connect(db_path)
            try:
                tables = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
            finally:
                conn.close()
            # AUTOINCREMENT adds its own sqlite_sequence bookkeeping table, so
            # this checks the expected tables are present rather than an exact
            # set match (== 13 tables + sqlite_sequence)
            expected = {'users', 'addresses', 'products', 'categories',
                       'product_categories', 'orders', 'order_items', 'payments',
                       'shipments', 'reviews', 'coupons', 'order_coupons',
                       'activity_logs'}
            self.assertTrue(expected <= tables, tables)
            self.assertEqual(len(expected), 13)

    def test_has_foreign_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / 'demo.db'
            erd.build_demo_db(db_path)
            import sqlite3
            conn = sqlite3.connect(db_path)
            try:
                fks = conn.execute('PRAGMA foreign_key_list(orders)').fetchall()
                shipment_fks = conn.execute('PRAGMA foreign_key_list(shipments)').fetchall()
            finally:
                conn.close()
            self.assertTrue(fks)  # orders.user_id / address_id reference other tables
            referenced = {row[2] for row in fks}  # table column of PRAGMA foreign_key_list
            self.assertIn('users', referenced)
            self.assertIn('addresses', referenced)
            self.assertTrue(shipment_fks)  # shipments.order_id -> orders (the 1:1 FK)

    def test_rebuilding_replaces_existing_file(self):
        # build_demo_db must not error, and must not append, if the path
        # already has a (possibly different-shaped) sqlite file at it.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / 'demo.db'
            erd.build_demo_db(db_path)
            first_size = db_path.stat().st_size
            erd.build_demo_db(db_path)
            self.assertEqual(db_path.stat().st_size, first_size)


if __name__ == '__main__':
    unittest.main()
