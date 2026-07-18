#!/usr/bin/env python3
"""Regenerate the live demo (docs/index.html).

The demo uses a small e-commerce schema expressed as information_schema-style
rows, so it exercises exactly the same code path as a live MySQL connection.

    python3 docs/gen_demo.py
"""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('erd', ROOT / 'erd.py')
erd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(erd)

T = [  # TABLE_NAME, TABLE_COMMENT
    ('users', 'Customer accounts'),
    ('addresses', 'Shipping / billing addresses'),
    ('products', 'Sellable products'),
    ('categories', 'Product category tree'),
    ('product_categories', 'Join table: products <-> categories'),
    ('orders', 'Customer orders'),
    ('order_items', 'Order line items'),
    ('payments', 'Payment attempts per order'),
    ('shipments', ''),
    ('reviews', 'Product reviews written by customers'),
    ('coupons', 'Discount coupons'),
    ('order_coupons', 'Join table: orders <-> coupons'),
    ('activity_logs', 'Append-only audit trail (no FK constraints on purpose)'),
]

def c(t, name, dtype, ctype, null='YES', key='', default='', extra='', comment=''):
    return (t, name, dtype, ctype, null, key, default, extra, comment)

def pk(t):
    return c(t, 'id', 'bigint', 'bigint', 'NO', 'PRI', '', 'auto_increment')

C = [
    pk('users'),
    c('users', 'email', 'varchar', 'varchar(255)', 'NO', 'UNI', comment='Login e-mail, unique'),
    c('users', 'name', 'varchar', 'varchar(100)', 'NO'),
    c('users', 'status', 'tinyint', 'tinyint', 'NO', '', '1', '', '1: active, 2: suspended'),
    c('users', 'created_at', 'datetime', 'datetime', 'NO'),
    pk('addresses'),
    c('addresses', 'user_id', 'bigint', 'bigint', 'NO', 'MUL'),
    c('addresses', 'kind', 'varchar', 'varchar(20)', 'NO', '', 'shipping', '', 'shipping | billing'),
    c('addresses', 'line1', 'varchar', 'varchar(200)', 'NO'),
    c('addresses', 'city', 'varchar', 'varchar(100)', 'NO'),
    c('addresses', 'country', 'char', 'char(2)', 'NO', '', 'JP', '', 'ISO 3166-1 alpha-2'),
    pk('products'),
    c('products', 'sku', 'varchar', 'varchar(40)', 'NO', 'UNI', comment='Stock keeping unit'),
    c('products', 'title', 'varchar', 'varchar(200)', 'NO'),
    c('products', 'price_cents', 'integer', 'int', 'NO', '', '0', '', 'Price in the smallest currency unit'),
    c('products', 'stock', 'integer', 'int', 'NO', '', '0'),
    c('products', 'discontinued', 'tinyint', 'tinyint(1)', 'NO', '', '0'),
    pk('categories'),
    c('categories', 'parent_id', 'bigint', 'bigint', comment='Self-reference: parent category'),
    c('categories', 'name', 'varchar', 'varchar(100)', 'NO'),
    c('product_categories', 'product_id', 'bigint', 'bigint', 'NO', 'PRI'),
    c('product_categories', 'category_id', 'bigint', 'bigint', 'NO', 'PRI'),
    pk('orders'),
    c('orders', 'user_id', 'bigint', 'bigint', 'NO', 'MUL'),
    c('orders', 'address_id', 'bigint', 'bigint', 'NO', comment='Ship-to address'),
    c('orders', 'state', 'varchar', 'varchar(20)', 'NO', '', 'cart', '', 'cart | placed | paid | shipped'),
    c('orders', 'total_cents', 'integer', 'int', 'NO', '', '0'),
    c('orders', 'placed_at', 'datetime', 'datetime'),
    pk('order_items'),
    c('order_items', 'order_id', 'bigint', 'bigint', 'NO', 'MUL'),
    c('order_items', 'product_id', 'bigint', 'bigint', 'NO', 'MUL'),
    c('order_items', 'quantity', 'integer', 'int', 'NO', '', '1'),
    c('order_items', 'unit_price_cents', 'integer', 'int', 'NO', comment='Price snapshot at purchase time'),
    pk('payments'),
    c('payments', 'order_id', 'bigint', 'bigint', 'NO', 'MUL'),
    c('payments', 'provider', 'varchar', 'varchar(30)', 'NO', '', '', '', 'stripe | paypal | ...'),
    c('payments', 'amount_cents', 'integer', 'int', 'NO'),
    c('payments', 'captured_at', 'datetime', 'datetime'),
    pk('shipments'),
    c('shipments', 'order_id', 'bigint', 'bigint', 'NO', 'UNI', comment='One shipment per order'),
    c('shipments', 'carrier', 'varchar', 'varchar(30)', 'NO'),
    c('shipments', 'tracking_no', 'varchar', 'varchar(60)'),
    c('shipments', 'shipped_at', 'datetime', 'datetime'),
    pk('reviews'),
    c('reviews', 'product_id', 'bigint', 'bigint', 'NO', 'MUL'),
    c('reviews', 'user_id', 'bigint', 'bigint', 'NO', 'MUL'),
    c('reviews', 'rating', 'tinyint', 'tinyint', 'NO', '', '', '', '1-5 stars'),
    c('reviews', 'body', 'text', 'text'),
    pk('coupons'),
    c('coupons', 'code', 'varchar', 'varchar(30)', 'NO', 'UNI'),
    c('coupons', 'discount_cents', 'integer', 'int', 'NO'),
    c('coupons', 'expires_at', 'datetime', 'datetime'),
    c('order_coupons', 'order_id', 'bigint', 'bigint', 'NO', 'PRI'),
    c('order_coupons', 'coupon_id', 'bigint', 'bigint', 'NO', 'PRI'),
    pk('activity_logs'),
    c('activity_logs', 'user_id', 'bigint', 'bigint', comment='No FK constraint — edge is inferred from the name'),
    c('activity_logs', 'order_id', 'bigint', 'bigint'),
    c('activity_logs', 'action', 'varchar', 'varchar(50)', 'NO'),
    c('activity_logs', 'created_at', 'datetime', 'datetime', 'NO'),
]

FK = [
    ('addresses', 'user_id', 'users'),
    ('categories', 'parent_id', 'categories'),
    ('product_categories', 'product_id', 'products'),
    ('product_categories', 'category_id', 'categories'),
    ('orders', 'user_id', 'users'),
    ('orders', 'address_id', 'addresses'),
    ('order_items', 'order_id', 'orders'),
    ('order_items', 'product_id', 'products'),
    ('payments', 'order_id', 'orders'),
    ('shipments', 'order_id', 'orders'),
    ('reviews', 'product_id', 'products'),
    ('reviews', 'user_id', 'users'),
    ('order_coupons', 'order_id', 'orders'),
    ('order_coupons', 'coupon_id', 'coupons'),
]

IX = [
    ('users', 'PRIMARY', 0, 1, 'id'),
    ('users', 'uk_users_email', 0, 1, 'email'),
    ('addresses', 'idx_addresses_user_kind', 1, 1, 'user_id'),
    ('addresses', 'idx_addresses_user_kind', 1, 2, 'kind'),
    ('products', 'uk_products_sku', 0, 1, 'sku'),
    ('orders', 'idx_orders_user_state', 1, 1, 'user_id'),
    ('orders', 'idx_orders_user_state', 1, 2, 'state'),
    ('order_items', 'idx_order_items_order', 1, 1, 'order_id'),
    ('reviews', 'uk_reviews_product_user', 0, 1, 'product_id'),
    ('reviews', 'uk_reviews_product_user', 0, 2, 'user_id'),
    ('activity_logs', 'idx_logs_created', 1, 1, 'created_at'),
    # both columns are marked UNI above — a real DB always backs that with a
    # matching unique index, which is also what tells the tool "this FK is
    # 1:1" (shipments.order_id) rather than the default many:1
    ('shipments', 'uk_shipments_order_id', 0, 1, 'order_id'),
    ('coupons', 'uk_coupons_code', 0, 1, 'code'),
]

# Design notes (notes Phase 1) — a small, illustrative set so the live demo
# actually shows the feature: a global note in the legend, table notes in the
# right pane, and a relation note under an association. Kept short and generic
# (the ADR link points at example.com on purpose).
NOTES = [
    {'id': 'design-intro',
     'target': {'type': 'global'},
     'text': 'Design notes travel with the schema: attach decisions, operational '
             'rules, and ADR links to tables and relationships, then read them back '
             'right here in the diagram.'},
    {'id': 'activity-retention',
     'target': {'type': 'table', 'table': 'activity_logs'},
     'title': 'Retention & audit',
     'text': 'Append-only audit trail — rows are never updated or deleted, and it '
             'deliberately carries no FK constraints so a write never blocks on a '
             'referential check. Retained 7 years for compliance.',
     'links': [{'label': 'ADR-014: audit logging', 'url': 'https://example.com/adr/014'}]},
    {'id': 'order-lifecycle',
     'target': {'type': 'table', 'table': 'orders'},
     'title': 'Order state machine',
     'text': 'state drives fulfillment: cart -> placed -> paid -> shipped. An order '
             'is created in "cart" and is never hard-deleted, so history stays intact.'},
    {'id': 'customer-anonymization',
     'target': {'type': 'relation', 'source_table': 'orders',
                'target_table': 'users', 'foreign_key': 'user_id'},
     'title': 'Customer retention',
     'text': 'When a customer deletes their account we anonymize the user row, but '
             'their orders are kept for accounting and warranty history.'},
]

# Visual groups (groups Phase 1) — one illustrative group so the live demo
# shows the feature: the order-fulfillment tables boxed together. Every table
# here belongs to exactly ONE group (Phase 1 forbids overlapping membership).
# Phase 1 has no layout affinity yet (DESIGN_ROADMAP §P2 follow-up), so a
# group frame just wraps its members wherever the layout already placed them —
# a group whose members are scattered would balloon into a frame that also
# encloses unrelated tables. The catalog trio (products + its category join +
# categories) lands as a tight vertical cluster under the default layout, so it
# frames cleanly. Revisit once layout affinity keeps members together.
GROUPS = [
    {'id': 'catalog', 'title': 'Catalog',
     'tables': ['products', 'product_categories', 'categories']},
]

tables = erd.mysql_ir(T, C, FK, IX)
args = SimpleNamespace(output=str(ROOT / 'docs' / 'index.html'),
                       models=None, excel=None, max_rows=15,
                       only=None, exclude=None,
                       infer_fk=True)  # showcase the feature: activity_logs' *_id
                                       # columns are deliberately FK-less in the schema
# notes Phase 1 (Sol finding #3): _finish now resolves/validates notes itself,
# AFTER infer_fk — pass the RAW NOTES list (not pre-resolved) so this exercises
# the same post-infer resolution path the real CLI pipeline uses. groups
# Phase 1 mirrors that exactly with the RAW GROUPS list.
erd._finish(tables, args, 'demo_shop', notes=NOTES, notes_label='demo',
            groups=GROUPS, groups_label='demo')
print('demo written to docs/index.html', file=sys.stderr)

# README's screenshot used to be a hand-exported SVG that this script never
# touched, so it silently drifted out of sync with the actual UI every time
# the demo was regenerated. Regenerate docs/screenshot.png here too, right
# from the file this script just wrote, so the two can never diverge again.
# Soft dependency on Playwright (same pattern as tests/test_e2e.py) — skip
# with a note rather than fail if it's not installed.
try:
    from playwright.sync_api import sync_playwright
    import base64
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': 1400, 'height': 1000})
        page.goto((ROOT / 'docs' / 'index.html').resolve().as_uri())
        page.wait_for_function('typeof nodePos.users !== "undefined"')
        page.wait_for_timeout(300)
        # rasterizePNGBlob()'s own logic, but with an explicit white
        # background fill instead of transparency — a README screenshot
        # should look right regardless of the viewer's page background,
        # unlike a user-triggered export meant to be composed elsewhere
        data_url = page.evaluate('''async () => {
            const built = buildExportSvg();
            const MAX_DIM = 8000;
            const scale = Math.max(0.1, Math.min(2, MAX_DIM/built.vw, MAX_DIM/built.vh));
            const W = Math.ceil(built.vw*scale), H = Math.ceil(built.vh*scale);
            built.svg.setAttribute('width', W); built.svg.setAttribute('height', H);
            const svgStr = new XMLSerializer().serializeToString(built.svg);
            const blob = new Blob([svgStr], {type:'image/svg+xml;charset=utf-8'});
            const url = URL.createObjectURL(blob);
            return await new Promise(resolve => {
                const img = new Image();
                img.onload = () => {
                    const canvas = document.createElement('canvas');
                    canvas.width = W; canvas.height = H;
                    const ctx = canvas.getContext('2d');
                    ctx.fillStyle = '#ffffff';
                    ctx.fillRect(0, 0, W, H);
                    ctx.drawImage(img, 0, 0, W, H);
                    URL.revokeObjectURL(url);
                    resolve(canvas.toDataURL('image/png'));
                };
                img.src = url;
            });
        }''')
        browser.close()
    _, encoded = data_url.split(',', 1)
    (ROOT / 'docs' / 'screenshot.png').write_bytes(base64.b64decode(encoded))
    print('demo written to docs/screenshot.png', file=sys.stderr)
except ImportError:
    print('playwright not installed — skipping docs/screenshot.png '
          '(pip install playwright && playwright install chromium)', file=sys.stderr)
except Exception as e:
    # Browser present but unlaunchable (e.g. sandbox / OS-permission error in a
    # restricted CI): skip the screenshot instead of crashing the whole demo
    # regen — index.html was already written above. Sol's re-review flagged the
    # full-suite byte-identical test failing here only because a launch error
    # escaped instead of being skipped like a missing Playwright would be.
    print(f'skipping docs/screenshot.png — browser unavailable '
          f'({type(e).__name__}: {e})', file=sys.stderr)
