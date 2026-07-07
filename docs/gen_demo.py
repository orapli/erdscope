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
]

tables = erd.mysql_ir(T, C, FK, IX)
args = SimpleNamespace(output=str(ROOT / 'docs' / 'index.html'),
                       models=None, excel=None, max_rows=15,
                       only=None, exclude=None, no_infer_fk=False)
erd._finish(tables, args, 'demo_shop')
print('demo written to docs/index.html', file=sys.stderr)
