#!/usr/bin/env python3
"""Regenerate the manual's screenshots (docs/img/*.png) by driving the live
demo (docs/index.html) with Playwright.

    pip install playwright && playwright install chromium
    python3 docs/gen_shots.py

Any Playwright-equipped interpreter works — this script has no hardcoded
path, it just needs `playwright` importable by `sys.executable`. (Sandbox
hint, safe to ignore elsewhere: a working venv lives at
~/.venvs/erdscope-dev/bin/python)

Each shot drives the real UI (clicks, typing, keyboard) rather than poking
internal JS state, so it looks like what a user actually sees. Viewport and
device-scale are fixed for reproducible crops.
"""
import importlib.util
import tempfile
from pathlib import Path
from types import SimpleNamespace
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DEMO_URL = (ROOT / 'docs' / 'index.html').as_uri()
IMG_DIR = ROOT / 'docs' / 'img'

VIEWPORT = {'width': 1200, 'height': 800}
SCALE = 2


def new_page(browser, url=DEMO_URL):
    """`url` defaults to the live demo; a couple of shots (groups.png's
    sibling ideas, edges.png) need a throwaway variant of it instead — see
    _build_edges_demo_html — so this takes the URL as a parameter rather
    than hardcoding DEMO_URL."""
    ctx = browser.new_context(viewport=VIEWPORT, device_scale_factor=SCALE, color_scheme='light')
    page = ctx.new_page()
    page.goto(url)
    page.wait_for_selector('.er-node')
    page.locator('#legend-toggle').click()
    page.wait_for_timeout(250)
    return ctx, page


def _boxes(page, target):
    """All bounding boxes for a target: every match if it's a selector
    string (e.g. '.er-node' for the whole visible node set), or the single
    element if it's already a Locator."""
    if isinstance(target, str):
        elements = page.locator(target).all()
    else:
        elements = [target]
    boxes = [b for b in (el.bounding_box() for el in elements) if b is not None]
    if not boxes:
        raise RuntimeError(f'no visible elements for {target!r}')
    return boxes


def clip_of(page, targets, pad=16, y_min=None, y_max=None):
    """y_min/y_max clamp the padded union so a crop edge lands exactly on a
    chrome boundary (topbar, toolbar) instead of slicing through it."""
    boxes = [b for t in targets for b in _boxes(page, t)]
    x0 = max(0, min(b['x'] for b in boxes) - pad)
    y0 = max(0, min(b['y'] for b in boxes) - pad)
    x1 = min(VIEWPORT['width'], max(b['x'] + b['width'] for b in boxes) + pad)
    y1 = min(VIEWPORT['height'], max(b['y'] + b['height'] for b in boxes) + pad)
    if y_min is not None:
        y0 = max(y0, y_min)
    if y_max is not None:
        y1 = min(y1, y_max)
    return {'x': x0, 'y': y0, 'width': x1 - x0, 'height': y1 - y0}


def shot_focus(browser):
    """Focus mode on a hub table, depth/direction changed from the defaults
    so both are visibly non-default in the focus bar."""
    ctx, page = new_page(browser)
    page.locator('.er-node[data-name="orders"]').dblclick()
    page.wait_for_timeout(300)
    page.locator('button.dep-btn[data-d="2"]').click()
    page.locator('button.dir-btn[data-dir="out"]').click()
    page.locator('#btn-fit').click()
    page.wait_for_timeout(400)
    bar = page.locator('#focus-bar').bounding_box()
    clip = clip_of(page, ['#focus-bar', '.er-node'], y_min=bar['y'] - 2)
    page.screenshot(path=str(IMG_DIR / 'focus.png'), clip=clip)
    ctx.close()


def shot_highlight(browser):
    """Toolbar Highlight search: matches marked, non-matches dimmed, nothing
    filtered out of the diagram."""
    ctx, page = new_page(browser)
    page.locator('#word-search').click()
    page.locator('#word-search').type('user')
    page.wait_for_timeout(300)
    clip = clip_of(page, ['#diagram-toolbar', '.er-node'])
    page.screenshot(path=str(IMG_DIR / 'highlight.png'), clip=clip)
    ctx.close()


def shot_multiselect(browser):
    """Three tables multi-selected (shift-click) so both the align and the
    distribute button groups are enabled in the right pane."""
    ctx, page = new_page(browser)
    tables = ['payments', 'shipments', 'order_coupons']
    page.locator(f'.er-node[data-name="{tables[0]}"]').click()
    for name in tables[1:]:
        page.locator(f'.er-node[data-name="{name}"]').click(modifiers=['Shift'])
    page.wait_for_timeout(300)
    toolbar = page.locator('#diagram-toolbar').bounding_box()
    clip = clip_of(page, [f'.er-node[data-name="{n}"]' for n in tables]
                    + ['.detail-name', '.msel-btns', '.msel-list'],
                   y_max=toolbar['y'] - 2)
    page.screenshot(path=str(IMG_DIR / 'multiselect.png'), clip=clip)
    ctx.close()


def shot_export(browser):
    """Export panel open: image options plus the per-format copy/download
    rows."""
    ctx, page = new_page(browser)
    page.locator('#btn-export-toggle').click()
    page.wait_for_timeout(200)
    clip = clip_of(page, ['#btn-export-toggle', '#export-menu'])
    page.screenshot(path=str(IMG_DIR / 'export.png'), clip=clip)
    ctx.close()


def shot_hiding(browser):
    """Two-level hiding: 'products' banned via the list's 🚫 button, showing
    the red banned-count banner and the struck-through row together."""
    ctx, page = new_page(browser)
    row = page.locator('.table-item').filter(has_text='products').first
    row.hover()
    row.locator('.hide-btn').click(force=True)
    page.wait_for_timeout(250)
    clip = clip_of(page, ['#left-controls', '#search-box', '#hidden-bar', '.table-item'])
    page.screenshot(path=str(IMG_DIR / 'hiding.png'), clip=clip)
    ctx.close()


def shot_notes(browser):
    """Design notes (notes Phase 1): a table note ('Order state machine')
    and a relation note ('Customer retention', nested under the :user
    association it's attached to) both visible at once by selecting
    'orders', the one demo table that carries both note kinds."""
    ctx, page = new_page(browser)
    page.locator('.er-node[data-name="orders"]').click()
    page.wait_for_timeout(300)
    targets = ['.detail-name',
               page.locator('.sec-title').filter(has_text='Associations'),
               '.assoc-list',
               page.locator('.sec-title').filter(has_text='Notes'),
               '.note-list']
    clip = clip_of(page, targets, pad=14)
    page.screenshot(path=str(IMG_DIR / 'notes.png'), clip=clip)
    ctx.close()


def shot_groups(browser):
    """Group frames (groups Phase 1): the demo's one configured group
    ('Catalog', wrapping products/product_categories/categories — see
    docs/gen_demo.py's GROUPS) is shown by default (showGroups defaults to
    true), so unlike edges.png this needs no throwaway schema — just a
    tight crop around the frame and its draggable title chip."""
    ctx, page = new_page(browser)
    tables = ['products', 'product_categories', 'categories']
    targets = [f'.er-node[data-name="{n}"]' for n in tables] + ['.grp-chip']
    clip = clip_of(page, targets)
    page.screenshot(path=str(IMG_DIR / 'groups.png'), clip=clip)
    ctx.close()


def _load_demo_constants():
    """Reuse docs/gen_demo.py's T/C/FK/IX information_schema-style row
    constants (and its `c`/`pk` row-builder helpers) for edges.png's
    throwaway schema, WITHOUT importing/executing gen_demo.py itself — its
    top-level code writes docs/index.html and docs/screenshot.png as a side
    effect the moment it's imported, which must not happen from this script.
    Only the prefix of the file up to where it starts feeding those
    constants into the real demo build is compiled and exec'd, so this is
    read-only as far as gen_demo.py/index.html are concerned."""
    gen_demo_path = ROOT / 'docs' / 'gen_demo.py'
    src = gen_demo_path.read_text()
    marker = 'tables = erd.mysql_ir(T, C, FK, IX)'
    prefix, sep, _ = src.partition(marker)
    if not sep:
        raise RuntimeError('docs/gen_demo.py no longer has the expected '
                           f'split point ({marker!r}) — update the marker')
    ns = {'__name__': '_gen_demo_constants', '__file__': str(gen_demo_path)}
    exec(compile(prefix, str(gen_demo_path), 'exec'), ns)  # defines erd, T, C, FK, IX, c, pk, ...
    return ns


def _build_edges_demo_html(out_path):
    """Build a throwaway variant of the demo schema, written to `out_path`,
    that exercises all three association-provenance badges (DB FK / manual /
    inferred) on one table — the real demo (docs/index.html) only ever shows
    DB FK + inferred, since it declares no config `relations:`. Never touches
    docs/gen_demo.py or docs/index.html.

    Adds two extra nullable *_id columns to 'payments' on top of its real
    order_id (an actual FK -> DB FK badge): 'shipment_id', declared via a
    config `relations:` entry -> forced to manual provenance by merge_ir
    (§9.1); and 'coupon_id', left alone for --infer-fk to pick up from the
    column name -> inferred provenance. Mirrors the real CLI pipeline's own
    layering (db layer, then a relations_to_config_layer merged on top —
    see erd.py's main()) rather than poking the IR directly.

    No `schema_fk` badge here — that provenance only comes from parsing a
    real Rails db/schema.rb file (rails.schema), which has no equivalent in
    an information_schema-style row set; not covered by any shot."""
    ns = _load_demo_constants()
    erd = ns['erd']
    c = ns['c']
    extra_columns = [
        c('payments', 'coupon_id', 'bigint', 'bigint'),
        c('payments', 'shipment_id', 'bigint', 'bigint'),
    ]
    tables = erd.mysql_ir(ns['T'], ns['C'] + extra_columns, ns['FK'], ns['IX'])
    db_layer = erd.make_provider_result('db', 'mysql', tables)
    relations = [{'table': 'payments', 'column': 'shipment_id',
                 'references': 'shipments', 'name': 'shipment'}]
    base = erd.merge_ir([db_layer])
    tables = erd.merge_ir([db_layer, erd.relations_to_config_layer(relations, base)])
    args = SimpleNamespace(output=str(out_path), models=None, excel=None, max_rows=15,
                           only=None, exclude=None, infer_fk=True)
    erd._finish(tables, args, 'demo_shop_edges')


def shot_edges(browser):
    """Association-provenance badges in the right-pane Associations list:
    DB FK (blue), manual (purple), inferred (yellow) — see
    _build_edges_demo_html for why a throwaway schema is needed to get all
    three onto one table."""
    with tempfile.TemporaryDirectory() as td:
        html_path = Path(td) / 'edges_demo.html'
        _build_edges_demo_html(html_path)
        ctx, page = new_page(browser, url=html_path.resolve().as_uri())
        page.locator('.er-node[data-name="payments"]').click()
        page.wait_for_timeout(300)
        targets = ['.detail-name',
                   page.locator('.sec-title').filter(has_text='Associations'),
                   '.assoc-list']
        clip = clip_of(page, targets, pad=14)
        page.screenshot(path=str(IMG_DIR / 'edges.png'), clip=clip)
        ctx.close()


def shot_colmodes(browser):
    """Column display modes: the global All/PK/FK/Name segmented control
    (set to PK/FK here) plus a per-node ▤ override cycled to 'Name' on
    'categories' alone, so it collapses to a bare header while its siblings
    stay in the global PK/FK view. Clips the whole diagram + toolbar (like
    focus.png/highlight.png) since the point is a diagram-wide setting with
    one visible exception, not a single element."""
    ctx, page = new_page(browser)
    page.locator('#colmode-group button[data-cm="1"]').click()
    page.wait_for_timeout(200)
    # the per-table ▤ toggle shares its 'n-mode' CSS class with the ⊖/⊕
    # header icons, so it's picked out by its own glyph rather than by
    # position among them.
    page.locator('.er-node[data-name="categories"] text.n-mode', has_text='▤').click()
    page.wait_for_timeout(2600)  # let the "categories: Name" toast (2.4s) clear before the shot
    clip = clip_of(page, ['#diagram-toolbar', '.er-node'])
    page.screenshot(path=str(IMG_DIR / 'colmodes.png'), clip=clip)
    ctx.close()


def shot_logical_names(browser):
    """Physical/logical name toggle: namemode set to 'Logical', so every
    node header shows its table comment instead of its physical name —
    except 'shipments', which has no comment in the demo schema and so
    falls back to its physical name (the documented fallback behavior)."""
    ctx, page = new_page(browser)
    page.locator('#namemode-group button[data-nm="2"]').click()
    page.wait_for_timeout(300)
    clip = clip_of(page, ['#diagram-toolbar', '.er-node'])
    page.screenshot(path=str(IMG_DIR / 'logical-names.png'), clip=clip)
    ctx.close()


def shot_views(browser):
    """Named views: after saving the current view under a name (view-save
    opens a native prompt(), auto-accepted here via page.on('dialog')), the
    Views selector shows it applied, alongside delete/share."""
    ctx, page = new_page(browser)
    page.on('dialog', lambda d: d.accept('Order fulfillment'))
    page.locator('#view-save').click()
    page.wait_for_timeout(200)
    # Whole topbar strip, not just the four buttons: the saved view's name in
    # the selector reads better next to the diagram title and table count.
    clip = clip_of(page, ['#topbar'], pad=0)
    clip['x'], clip['width'] = 0, VIEWPORT['width']
    page.screenshot(path=str(IMG_DIR / 'views.png'), clip=clip)
    ctx.close()


def shot_darkmode(browser):
    """Dark mode, toggled via the viewer's own 🌙 button (not the OS/
    Playwright color-scheme, which new_page() fixes to 'light' — this is
    the in-app override). 'orders' is selected so the right pane's badges
    and note accent colors are exercised in dark mode too, not just the
    diagram."""
    ctx, page = new_page(browser)
    page.locator('#btn-dark').click()
    page.wait_for_timeout(300)
    page.locator('.er-node[data-name="orders"]').click()
    page.wait_for_timeout(300)
    clip = clip_of(page, ['#topbar', '#diagram-toolbar', '.er-node'])
    page.screenshot(path=str(IMG_DIR / 'darkmode.png'), clip=clip)
    ctx.close()


SHOTS = [shot_focus, shot_highlight, shot_multiselect, shot_export, shot_hiding,
         shot_notes, shot_groups, shot_edges, shot_colmodes, shot_logical_names,
         shot_views, shot_darkmode]


def main():
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for shot in SHOTS:
            shot(browser)
            print(f'wrote {shot.__name__}')
        browser.close()


if __name__ == '__main__':
    main()
