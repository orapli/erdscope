#!/usr/bin/env python3
"""Build the distributable single-file ``erd.py`` from the split development
source under ``src/erdscope/``.

erd.py ships on PyPI and is meant to be grabbed and run as one self-contained,
zero-dependency file. For development it is split up:

  * the Python is organised into concern-named fragments (``MODULES`` below),
    which are concatenated **in order** — they are an amalgamation of one flat
    module (SQLite-style), not an importable package, so no cross-module imports
    are needed and the result is byte-for-byte the assembled Python;
  * the ~3,600-line embedded viewer (HTML/CSS/JS) lives in ``viewer.html`` and is
    inlined into a one-line sentinel that sits in ``exporters.py``.

Because both steps are pure textual assembly, the generated ``erd.py`` is fully
determined by the source; ``--check`` fails if the committed ``erd.py`` has
drifted (someone edited it directly, or forgot to rebuild).

Usage:
    python3 tools/build_single_file.py           # (re)generate erd.py
    python3 tools/build_single_file.py --check    # verify erd.py is up to date (CI)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / 'src' / 'erdscope'
TARGET = ROOT / 'erd.py'

# Concatenation order of the Python fragments. This is the source of truth for
# assembly order; the files are contiguous slices of one flat module. An entry
# ending in '/' is a *folder* of fragments — every ``*.py`` inside is included
# automatically (see expand_module), so a pluggable area like ``db/`` grows by
# just dropping a new file in, with no edit here.
MODULES = [
    'header.py',      # shebang, module docstring, imports
    'ir.py',          # IR/provider/provenance contract + SQL type shorthand
    'db/',            # DBAdapter base + registry, and the built-in DB adapters
    'merge.py',       # merge_ir + reconcile_db_fks + association identity
    'frameworks/',    # FrameworkOverlay base + registry, Rails/Prisma/Django, FK inference
    'providers.py',   # DB provider seam, config layer + validation
    'exporters.py',   # Excel writer + the HTML_TEMPLATE sentinel (viewer inlined below)
    'config.py',      # config-file loading/validation, URL assembly, title
    'cli.py',         # argparse main, serialize_for_viewer, _finish
]


def expand_module(entry):
    """Resolve a MODULES entry to the ordered list of fragment paths it emits.

    A plain filename is itself. A folder entry (trailing '/') expands to every
    ``*.py`` inside it, with ``base.py`` emitted FIRST (it defines the abstract
    base and registry that the sibling implementation files subclass at import
    time) and the rest in sorted order — a deterministic, "base before its
    subclasses" ordering so the amalgamation stays load-safe and byte-stable."""
    if not entry.endswith('/'):
        return [SRC / entry]
    folder = SRC / entry.rstrip('/')
    files = sorted(folder.glob('*.py'), key=lambda p: (p.name != 'base.py', p.name))
    if not files:
        sys.exit(f'build error: folder module {entry!r} contains no *.py files')
    return files

# The sentinel line in exporters.py. It is valid Python (a placeholder string
# assignment), so the fragment stays parseable; the build swaps it for the real
# viewer. The marker must never occur in the viewer content.
SENTINEL = 'HTML_TEMPLATE = r"""__ERDSCOPE_VIEWER_TEMPLATE__"""'


def build():
    fragments = [path for m in MODULES for path in expand_module(m)]
    python = ''.join(p.read_text(encoding='utf-8') for p in fragments)
    viewer = (SRC / 'viewer.html').read_text(encoding='utf-8')
    if python.count(SENTINEL) != 1:
        sys.exit(f'build error: expected exactly one viewer sentinel across '
                 f'{", ".join(MODULES)}, found {python.count(SENTINEL)}')
    if '"""' in viewer:
        sys.exit('build error: viewer.html contains a triple double-quote, '
                 'which would break the r""" ... """ inlining')
    return python.replace(SENTINEL, 'HTML_TEMPLATE = r"""' + viewer + '"""', 1)


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
