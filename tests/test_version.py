"""``__version__`` (defined in src/erdscope/header.py, mirrored into erd.py by
the build) and pyproject.toml's `version` are two hand-maintained copies of
the same value — see the comment above `__version__` for why they can't be
unified at runtime. This test is the drift guard.
"""
import importlib.util
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location('erd', ROOT / 'erd.py')
erd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(erd)


class TestVersion(unittest.TestCase):
    def test_dunder_version_matches_pyproject(self):
        pyproject = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
        m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', pyproject)
        self.assertIsNotNone(m, 'could not find version = "..." in pyproject.toml')
        self.assertEqual(erd.__version__, m.group(1))

    def test_version_flag_prints_version(self):
        import subprocess
        import sys
        r = subprocess.run([sys.executable, str(ROOT / 'erd.py'), '--version'],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn(erd.__version__, r.stdout)


if __name__ == '__main__':
    unittest.main()
