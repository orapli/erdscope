#!/usr/bin/env python3
"""Manual performance benchmark for erdscope against large synthetic schemas.

NOT part of `python3 -m unittest discover -s tests` — this is a slow,
manual, human-run script. Typical use:

    python3 benchmarks/run_bench.py --tables 100,300,1000

For each table count it:
  (a) generates a synthetic SQLite schema via gen_schema.py (~2 FK edges/table)
  (b) times `python3 erd.py sqlite:///... -o out.html` — median of 3 runs
      (the "official" number) — plus a single-shot breakdown of the
      pipeline's adapter-fetch / merge / HTML-export phases (see
      measure_reference_phases below) as a "reference" value showing where
      CLI time goes
  (c) times first paint and a reset-button re-layout in headless Chromium
      via Playwright (median of 3 runs each)
  (d) records generated HTML file size, and (low-cost, best-effort) Python
      child RSS and browser JS heap size

Prints a JSON blob (everything) followed by a formatted summary table to
stdout. All generated .db/.html files live in a temp directory that is
removed afterwards unless --keep is passed — nothing here is meant to be
committed to the repo.

Requires the project's dev venv for the browser measurements
(~/.venvs/erdscope-dev by default; override with --venv-python:
`pip install playwright && playwright install chromium` inside it). If that
interpreter or Playwright/Chromium isn't available, browser metrics are
recorded as an error/N/A and the rest of the benchmark still runs.
"""
import argparse
import importlib.util
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import textwrap
import time
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VENV_PYTHON = Path.home() / '.venvs' / 'erdscope-dev' / 'bin' / 'python'

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_schema  # noqa: E402  (benchmarks/gen_schema.py, same directory)

# "Large schema" thresholds used to flag rows in the summary table (§4 of
# the design: first paint > 3s OR re-layout > 1s => recommend --only/--exclude).
PAINT_THRESHOLD_MS = 3000
RELAYOUT_THRESHOLD_MS = 1000


def sqlite_url(db_path):
    """sqlite:// URL for an absolute path. db/sqlite.py's
    sqlite_path_from_url strips exactly one leading slash from the URL
    path, so an absolute path (which already starts with '/') needs a
    4-slash prefix: sqlite:/// + /abs/path -> sqlite:////abs/path, which
    strips back down to /abs/path."""
    return 'sqlite:///' + str(Path(db_path).resolve())


# ---------------------------------------------------------------------------
# (a) CLI generation time (subprocess, median of 3) + reference phase timings
#     (in-process, single shot) + memory
# ---------------------------------------------------------------------------

def median_cli_time(erd_py, db_path, out_html, runs=3):
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        subprocess.run(
            [sys.executable, str(erd_py), sqlite_url(db_path), '-o', str(out_html)],
            cwd=str(REPO_ROOT), check=True, capture_output=True, text=True)
        times.append(time.perf_counter() - t0)
    return statistics.median(times), times


def measure_child_rss_kb(erd_py, db_path, out_html):
    """One extra isolated CLI run, spawned from a throwaway wrapper process
    so RUSAGE_CHILDREN starts at zero. A plain
    resource.getrusage(RUSAGE_CHILDREN) taken directly in run_bench.py would
    be a monotonic high-water mark across every subprocess this script has
    ever spawned (all 3 CLI runs, all table sizes so far) — not the child
    RSS for this particular N. ru_maxrss is kilobytes on Linux."""
    wrapper = (
        "import subprocess, resource, sys\n"
        "subprocess.run(sys.argv[1:], check=True, capture_output=True)\n"
        "print(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, '-c', wrapper, sys.executable, str(erd_py),
             sqlite_url(db_path), '-o', str(out_html)],
            cwd=str(REPO_ROOT), check=True, capture_output=True, text=True, timeout=600)
        return int(proc.stdout.strip())
    except Exception as e:
        print(f'  [memory] measurement failed: {e}', file=sys.stderr)
        return None


def import_erd_module():
    """Import the built erd.py single file as a fresh module object (not
    erd.py's own __main__ guard — importing just defines functions/classes,
    no CLI side effects) so its internal pipeline functions are callable
    directly for phase-level timing."""
    spec = importlib.util.spec_from_file_location('erd_bench', REPO_ROOT / 'erd.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def measure_reference_phases(erd, db_path, out_html):
    """Single-shot (not medianed — these are reference/diagnostic numbers,
    not the headline metric) timings for the three phases inside the
    generate pipeline (src/erdscope/cli.py: _run_pipeline / _finish):

      - fetch:       erd.db_provider(url)   — dispatches to the SQLite
                      adapter's fetch() (db/sqlite.py: parse_sqlite, via
                      PRAGMA table_info/foreign_key_list/index_list) and
                      normalizes into the IR via mysql_ir()
      - merge:        erd.merge_ir([db_result]) — identity merge + DB-FK
                      reconciliation (merge.py); only one layer here since
                      this benchmark has no --models/config overlay
      - html_export:  erd._finish(...) — fk_columns derivation (cheap),
                      serialize_for_viewer(), json.dumps of the IR, the
                      three placeholder substitutions into HTML_TEMPLATE,
                      and the file write. _finish is called with a
                      SimpleNamespace standing in for argparse's Namespace
                      (infer_fk/only/exclude off, matching a plain
                      `erd.py sqlite:///...` invocation) so this is exactly
                      the code path the CLI runs, minus argument parsing.
    """
    url = sqlite_url(db_path)

    t0 = time.perf_counter()
    db_result = erd.db_provider(url)
    t1 = time.perf_counter()

    tables = erd.merge_ir([db_result])
    t2 = time.perf_counter()

    args_ns = types.SimpleNamespace(
        infer_fk=False, only=None, exclude=None, max_rows=15,
        output=str(out_html), excel=None, excel_template=None)
    erd._finish(tables, args_ns, 'bench')
    t3 = time.perf_counter()

    return {'fetch_s': t1 - t0, 'merge_s': t2 - t1, 'html_export_s': t3 - t2}


# ---------------------------------------------------------------------------
# (b)/(c) Browser measurements via Playwright, run in the dev venv
# ---------------------------------------------------------------------------

# Definition per the design: page.goto() then a double-rAF round trip. Since
# viewer.html's init (renderTableList(); renderDiagram(); ...) runs as a
# synchronous top-level <script> block (see the end of viewer.html), goto()
# only resolves once that has already executed; the double rAF after it
# captures the two paints scheduled after (parse+DOM build+layout are
# already done by then, so this is "time until first pixels are on screen").
_RAF2 = ("() => new Promise(r => requestAnimationFrame("
         "() => requestAnimationFrame(() => r(performance.now()))))")

# Re-layout: click #btn-reset (the "Re-layout now" toolbar button — see
# viewer.html's btn-reset handler: clears nodePos, re-runs gridLayout(),
# then refreshView()) and time from just before the click to the settling
# double rAF, all inside one page.evaluate so the click's synchronous
# handler (gridLayout + renderDiagram) is included in the measured window
# rather than having already finished before Python regains control.
_RESET_AND_RAF2 = (
    "() => new Promise(r => {"
    "const t0 = performance.now();"
    "document.getElementById('btn-reset').click();"
    "requestAnimationFrame(() => requestAnimationFrame(() => r(performance.now() - t0)));"
    "})"
)

_PLAYWRIGHT_SCRIPT = textwrap.dedent(f'''
    import json, statistics, sys
    from playwright.sync_api import sync_playwright

    html_path = sys.argv[1]
    file_url = 'file://' + html_path
    RAF2 = {_RAF2!r}
    RESET_AND_RAF2 = {_RESET_AND_RAF2!r}
    NAV_TIMEOUT_MS = 180000

    paint_samples = []
    relayout_samples = []
    heap_used = None
    error = None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for i in range(3):
                page = browser.new_page()
                page.goto(file_url, timeout=NAV_TIMEOUT_MS)
                t = page.evaluate(RAF2)
                paint_samples.append(t)
                if i == 0:
                    cdp = page.context.new_cdp_session(page)
                    cdp.send('Performance.enable')
                    metrics = cdp.send('Performance.getMetrics')
                    heap_used = next((m['value'] for m in metrics['metrics']
                                       if m['name'] == 'JSHeapUsedSize'), None)
                page.close()
            for i in range(3):
                page = browser.new_page()
                page.goto(file_url, timeout=NAV_TIMEOUT_MS)
                page.evaluate(RAF2)  # let the initial paint settle before resetting
                t = page.evaluate(RESET_AND_RAF2)
                relayout_samples.append(t)
                page.close()
            browser.close()
    except Exception as e:
        error = str(e)

    print(json.dumps({{
        'paint_ms_samples': paint_samples,
        'paint_ms_median': statistics.median(paint_samples) if paint_samples else None,
        'relayout_ms_samples': relayout_samples,
        'relayout_ms_median': statistics.median(relayout_samples) if relayout_samples else None,
        'js_heap_used_bytes': heap_used,
        'error': error,
    }}))
''')


def measure_browser(venv_python, html_path, timeout=900):
    if not Path(venv_python).exists():
        return {'error': f'venv python not found: {venv_python}'}
    fd_path = None
    try:
        with tempfile.NamedTemporaryFile('w', suffix='.py', delete=False) as f:
            f.write(_PLAYWRIGHT_SCRIPT)
            fd_path = f.name
        proc = subprocess.run(
            [str(venv_python), fd_path, str(Path(html_path).resolve())],
            capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            return {'error': f'playwright script failed (rc={proc.returncode}): '
                              f'{proc.stderr[-2000:]}'}
        line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ''
        result = json.loads(line)
        if result.get('error'):
            return {'error': result['error']}
        return result
    except Exception as e:
        return {'error': str(e)}
    finally:
        if fd_path:
            Path(fd_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_one(erd_py, venv_python, n_tables, work_dir, keep):
    print(f'== {n_tables} tables ==', file=sys.stderr)
    db_path = work_dir / f'bench_{n_tables}.db'
    gstats = gen_schema.generate(n_tables, db_path)
    out_html = work_dir / f'bench_{n_tables}.html'

    print('  [1/4] CLI generation time (median of 3)...', file=sys.stderr)
    cli_median, cli_samples = median_cli_time(erd_py, db_path, out_html, runs=3)
    html_size_bytes = out_html.stat().st_size

    print('  [2/4] reference phase breakdown...', file=sys.stderr)
    erd = import_erd_module()
    ref_out_html = work_dir / f'bench_{n_tables}_ref.html'
    ref_phases = measure_reference_phases(erd, db_path, ref_out_html)

    print('  [3/4] python child memory (RUSAGE_CHILDREN)...', file=sys.stderr)
    rss_kb = measure_child_rss_kb(erd_py, db_path, out_html)

    print('  [4/4] browser paint/re-layout (Playwright)...', file=sys.stderr)
    browser = measure_browser(venv_python, out_html)

    result = {
        'tables': n_tables,
        'edges': gstats['edges'],
        'cli_gen_median_s': cli_median,
        'cli_gen_samples_s': cli_samples,
        'html_size_bytes': html_size_bytes,
        'reference_phases_s': ref_phases,
        'python_child_max_rss_kb': rss_kb,
        'browser': browser,
    }

    if not keep:
        for p in (db_path, out_html, ref_out_html):
            p.unlink(missing_ok=True)

    return result


def fmt_s(x, digits=2):
    return 'N/A' if x is None else f'{x:.{digits}f}'


def fmt_ms(x, digits=0):
    return 'N/A' if x is None else f'{x:.{digits}f}'


def fmt_mb(kb_or_bytes, from_bytes=False):
    if kb_or_bytes is None:
        return 'N/A'
    mb = (kb_or_bytes / 1024 / 1024) if from_bytes else (kb_or_bytes / 1024)
    return f'{mb:.1f}'


def print_summary_table(results):
    headers = ['Tables', 'Edges', 'CLI gen (s, median)', 'HTML size (KB)',
               'Init paint (ms, median)', 'Re-layout (ms, median)',
               'Py RSS (MB)', 'JS heap (MB)', 'Regime']
    rows = []
    for r in results:
        browser = r['browser']
        paint = browser.get('paint_ms_median') if not browser.get('error') else None
        relayout = browser.get('relayout_ms_median') if not browser.get('error') else None
        regime = 'N/A'
        if paint is not None and relayout is not None:
            large = paint > PAINT_THRESHOLD_MS or relayout > RELAYOUT_THRESHOLD_MS
            regime = 'LARGE (use --only)' if large else 'ok'
        rows.append([
            str(r['tables']),
            str(r['edges']),
            fmt_s(r['cli_gen_median_s']),
            f"{r['html_size_bytes'] // 1024}",
            fmt_ms(paint),
            fmt_ms(relayout),
            fmt_mb(r['python_child_max_rss_kb']),
            fmt_mb(browser.get('js_heap_used_bytes'), from_bytes=True)
                if not browser.get('error') else 'N/A',
            regime,
        ])
    widths = [max(len(h), *(len(row[i]) for row in rows)) for i, h in enumerate(headers)]
    def fmt_row(cells):
        return '  '.join(c.ljust(w) for c, w in zip(cells, widths))
    print(fmt_row(headers))
    print(fmt_row(['-' * w for w in widths]))
    for row in rows:
        print(fmt_row(row))

    print()
    print('Reference phase breakdown (single-shot, in-process; not the median-of-3 '
          'CLI number above):')
    headers2 = ['Tables', 'fetch (s)', 'merge (s)', 'html_export (s)']
    rows2 = [[str(r['tables']),
              fmt_s(r['reference_phases_s']['fetch_s'], 3),
              fmt_s(r['reference_phases_s']['merge_s'], 3),
              fmt_s(r['reference_phases_s']['html_export_s'], 3)] for r in results]
    widths2 = [max(len(h), *(len(row[i]) for row in rows2)) for i, h in enumerate(headers2)]
    def fmt_row2(cells):
        return '  '.join(c.ljust(w) for c, w in zip(cells, widths2))
    print(fmt_row2(headers2))
    print(fmt_row2(['-' * w for w in widths2]))
    for row in rows2:
        print(fmt_row2(row))

    for r in results:
        if r['browser'].get('error'):
            print(f"\nNote: browser measurement for {r['tables']} tables failed: "
                  f"{r['browser']['error']}", file=sys.stderr)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('--tables', default='100,300,1000',
                    help='Comma-separated table counts to benchmark (default: 100,300,1000)')
    p.add_argument('--erd-py', default=str(REPO_ROOT / 'erd.py'),
                    help='Path to erd.py (default: repo root erd.py)')
    p.add_argument('--venv-python', default=str(DEFAULT_VENV_PYTHON),
                    help='Python interpreter with playwright installed, for browser '
                         'measurements (default: ~/.venvs/erdscope-dev/bin/python)')
    p.add_argument('--work-dir', default=None,
                    help='Directory for generated .db/.html files (default: a fresh '
                         'temp directory, removed afterwards unless --keep)')
    p.add_argument('--keep', action='store_true',
                    help='Keep generated .db/.html files instead of deleting them')
    p.add_argument('--json-out', default=None,
                    help='Also write the full JSON results to this path')
    args = p.parse_args()

    sizes = [int(x) for x in args.tables.split(',') if x.strip()]
    erd_py = Path(args.erd_py).resolve()
    venv_python = Path(args.venv_python).expanduser()

    own_work_dir = args.work_dir is None
    work_dir = Path(args.work_dir).resolve() if args.work_dir else \
        Path(tempfile.mkdtemp(prefix='erdscope-bench-'))
    work_dir.mkdir(parents=True, exist_ok=True)
    print(f'Work dir: {work_dir}', file=sys.stderr)

    results = []
    try:
        for n in sizes:
            results.append(run_one(erd_py, venv_python, n, work_dir, args.keep))
    finally:
        if own_work_dir and not args.keep:
            shutil.rmtree(work_dir, ignore_errors=True)

    print(json.dumps(results, indent=2))
    print()
    print_summary_table(results)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, indent=2), encoding='utf-8')
        print(f'\nWrote {args.json_out}', file=sys.stderr)


if __name__ == '__main__':
    main()
