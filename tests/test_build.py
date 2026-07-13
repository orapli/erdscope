"""The committed ``erd.py`` is a build artifact of the split source under
``src/erdscope/``: concern-named Python fragments (concatenated in order) plus
``viewer.html`` (the ~3,600-line embedded viewer, out of the Python).
``tools/build_single_file.py`` concatenates the fragments and inlines the viewer
into a one-line sentinel to produce ``erd.py``.

This mirrors, as a unittest, the CI check that ``erd.py`` has not drifted from
its source — so editing ``erd.py`` directly (instead of the source) or forgetting
to rebuild is caught locally too.
"""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / 'tools' / 'build_single_file.py'
SRC = ROOT / 'src' / 'erdscope'


@unittest.skipUnless(BUILD.exists() and SRC.exists(),
                     'split source / build tool not present in this checkout')
class TestSingleFileBuild(unittest.TestCase):
    def test_erd_py_matches_the_build(self):
        # `--check` exits non-zero (with a helpful message) if the committed
        # erd.py differs from what core.py + viewer.html build to.
        r = subprocess.run([sys.executable, str(BUILD), '--check'],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0,
                         f'erd.py is out of date with src/erdscope/ — run '
                         f'`python3 tools/build_single_file.py`.\n{r.stdout}{r.stderr}')

    def test_build_is_byte_deterministic(self):
        # Building twice yields identical bytes (pure textual inline).
        import importlib.util
        spec = importlib.util.spec_from_file_location('_bsf', BUILD)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.build(), mod.build())
        # and the build equals the committed artifact
        self.assertEqual(mod.build(), (ROOT / 'erd.py').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
