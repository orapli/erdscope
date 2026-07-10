#!/usr/bin/env python3
"""Regenerate the manual's screenshots (docs/img/*.png) by driving the live
demo (docs/index.html) with Playwright.

    pip install playwright && playwright install chromium
    python3 docs/gen_shots.py

Any Playwright-equipped interpreter works — this script has no hardcoded
path, it just needs `playwright` importable by `sys.executable`. (Sandbox
hint, safe to ignore elsewhere: a working venv lives at
/tmp/claude-1000/-Users-tashiro-work-er/0b173e9c-655a-4a70-ae30-57b6a10ac401/scratchpad/pwvenv/bin/python)

Each shot drives the real UI (clicks, typing, keyboard) rather than poking
internal JS state, so it looks like what a user actually sees. Viewport and
device-scale are fixed for reproducible crops.
"""
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DEMO_URL = (ROOT / 'docs' / 'index.html').as_uri()
IMG_DIR = ROOT / 'docs' / 'img'

VIEWPORT = {'width': 1200, 'height': 800}
SCALE = 2


def new_page(browser):
    ctx = browser.new_context(viewport=VIEWPORT, device_scale_factor=SCALE, color_scheme='light')
    page = ctx.new_page()
    page.goto(DEMO_URL)
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


SHOTS = [shot_focus, shot_highlight, shot_multiselect, shot_export, shot_hiding]


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
