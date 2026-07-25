"""Tests for --no-html CLI flag (backlog #0.8.x)."""
import tempfile
import unittest
from pathlib import Path
import importlib.util

ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location('erd', ROOT / 'erd.py')
erd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(erd)


class TestNoHTML(unittest.TestCase):
    def test_no_html_skips_html_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_out = Path(tmp) / 'out.json'
            html_out = Path(tmp) / 'erd.html'
            args = ['demo', '--no-html', '--emit-json', str(json_out)]
            # Run CLI via erd.main with mock args
            p = erd.parse_args if hasattr(erd, 'parse_args') else None
            import subprocess
            import sys
            r = subprocess.run([sys.executable, str(ROOT / 'erd.py')] + args,
                               cwd=tmp, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f'CLI failed: {r.stderr}')
            self.assertTrue(json_out.exists(), 'emit-json file was not generated')
            self.assertFalse(html_out.exists(), 'erd.html was generated despite --no-html')

    def test_no_html_conflicts_with_output_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            import subprocess
            import sys
            r = subprocess.run([sys.executable, str(ROOT / 'erd.py'), 'demo', '--no-html', '-o', 'custom.html', '--emit-json', 'out.json'],
                               cwd=tmp, capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn('--no-html cannot be combined with -o/--output', r.stderr)

    def test_no_html_requires_at_least_one_other_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            import subprocess
            import sys
            r = subprocess.run([sys.executable, str(ROOT / 'erd.py'), 'demo', '--no-html'],
                               cwd=tmp, capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn('--no-html specified but no other output format was requested', r.stderr)


if __name__ == '__main__':
    unittest.main()
