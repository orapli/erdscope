"""Committed multi-provider review showcase drift gate."""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GENERATOR = ROOT / 'examples' / 'showcase' / 'generate.py'


class TestReviewShowcase(unittest.TestCase):
    def test_inputs_and_outputs_are_current(self):
        result = subprocess.run(
            [sys.executable, str(GENERATOR), '--check'],
            cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(
            result.returncode, 0,
            'review showcase is stale; run '
            '`python3 examples/showcase/generate.py`\n'
            + result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
