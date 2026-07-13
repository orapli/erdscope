#!/usr/bin/env python3
"""Build the distributable single-file ``erd.py`` from the split development
source under ``src/erdscope/``.

The development source keeps the ~3,600-line embedded viewer (HTML/CSS/JS) in
its own file, ``src/erdscope/viewer.html``, out of the Python in
``src/erdscope/core.py`` — which carries a one-line sentinel where the template
belongs. This tool inlines the viewer back into the sentinel to produce the
self-contained, zero-dependency ``erd.py`` that ships on PyPI and that users can
grab and run directly.

Usage:
    python3 tools/build_single_file.py           # (re)generate erd.py
    python3 tools/build_single_file.py --check    # verify erd.py is up to date (CI)

The build is a pure textual inline, so the generated ``erd.py`` is byte-for-byte
determined by ``core.py`` + ``viewer.html``; ``--check`` fails if the committed
``erd.py`` has drifted from the source.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / 'src' / 'erdscope'
TARGET = ROOT / 'erd.py'

# The sentinel line in core.py. It is itself valid Python (a placeholder string
# assignment), so core.py stays importable; the build swaps it for the real
# template. The marker must never occur in the viewer content.
SENTINEL = 'HTML_TEMPLATE = r"""__ERDSCOPE_VIEWER_TEMPLATE__"""'


def build():
    core = (SRC / 'core.py').read_text(encoding='utf-8')
    viewer = (SRC / 'viewer.html').read_text(encoding='utf-8')
    if core.count(SENTINEL) != 1:
        sys.exit(f'build error: expected exactly one sentinel in core.py, '
                 f'found {core.count(SENTINEL)}')
    if '"""' in viewer:
        sys.exit('build error: viewer.html contains a triple double-quote, '
                 'which would break the r""" ... """ inlining')
    return core.replace(SENTINEL, 'HTML_TEMPLATE = r"""' + viewer + '"""', 1)


def main():
    out = build()
    if '--check' in sys.argv:
        if not TARGET.exists():
            sys.exit('erd.py does not exist — run: python3 tools/build_single_file.py')
        if TARGET.read_text(encoding='utf-8') != out:
            sys.exit('erd.py is out of date with src/erdscope/ — run: '
                     'python3 tools/build_single_file.py')
        print('erd.py is up to date with src/erdscope/')
        return
    TARGET.write_text(out, encoding='utf-8')
    print(f'Wrote {TARGET} ({len(out)} bytes)')


if __name__ == '__main__':
    main()
