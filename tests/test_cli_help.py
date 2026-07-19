"""Guard against the CLI help text drifting behind the registered framework
overlays (the exact drift an external reviewer flagged for 0.9.0: SQLAlchemy
and Laravel were supported but invisible in --help / package metadata).
"""
import importlib.util
import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location('erd', ROOT / 'erd.py')
erd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(erd)


class TestCliHelpListsEveryFramework(unittest.TestCase):
    def test_every_registered_overlay_name_appears_in_help(self):
        import sys
        buf = io.StringIO()
        old_argv = sys.argv
        sys.argv = ['erdscope', '--help']
        try:
            with redirect_stdout(buf):
                erd.main()
        except SystemExit:
            pass
        finally:
            sys.argv = old_argv
        help_text = buf.getvalue().lower()
        names = sorted(cls.name for cls in erd.FRAMEWORK_OVERLAYS)
        self.assertTrue(names, 'no framework overlays registered — test would be vacuous')
        for name in names:
            self.assertIn(name, help_text,
                          f'framework overlay {name!r} is registered but missing from --help output')


if __name__ == '__main__':
    unittest.main()
