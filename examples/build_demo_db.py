#!/usr/bin/env python3
"""(Re)build the sample SQLite database used in examples/README.md.

Creates examples/demo_shop.db from scratch: the same small e-commerce schema as
the hosted demo (docs/gen_demo.py), but as a REAL SQLite database with actual
foreign keys, unique constraints, and indexes — so `python3 erd.py
sqlite:///examples/demo_shop.db` reads a live schema and reproduces a diagram
just like the online demo. Committed to the repo so you can try it without
building anything; run this script to regenerate it.

    python3 examples/build_demo_db.py
"""
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent / 'demo_shop.db'

SCHEMA = """
CREATE TABLE users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email      VARCHAR(255) NOT NULL UNIQUE,
    name       VARCHAR(100) NOT NULL,
    status     INTEGER NOT NULL DEFAULT 1,   -- 1: active, 2: suspended
    created_at DATETIME NOT NULL
);

CREATE TABLE addresses (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    kind    VARCHAR(20) NOT NULL DEFAULT 'shipping',  -- shipping | billing
    line1   VARCHAR(200) NOT NULL,
    city    VARCHAR(100) NOT NULL,
    country CHAR(2) NOT NULL DEFAULT 'JP'
);
CREATE INDEX idx_addresses_user_kind ON addresses (user_id, kind);

CREATE TABLE products (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sku          VARCHAR(40) NOT NULL UNIQUE,
    title        VARCHAR(200) NOT NULL,
    price_cents  INTEGER NOT NULL DEFAULT 0,
    stock        INTEGER NOT NULL DEFAULT 0,
    discontinued INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE categories (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER REFERENCES categories(id),  -- self-reference
    name      VARCHAR(100) NOT NULL
);

CREATE TABLE product_categories (
    product_id  INTEGER NOT NULL REFERENCES products(id),
    category_id INTEGER NOT NULL REFERENCES categories(id),
    PRIMARY KEY (product_id, category_id)
);

CREATE TABLE orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    address_id  INTEGER NOT NULL REFERENCES addresses(id),
    state       VARCHAR(20) NOT NULL DEFAULT 'cart',  -- cart | placed | paid | shipped
    total_cents INTEGER NOT NULL DEFAULT 0,
    placed_at   DATETIME
);
CREATE INDEX idx_orders_user_state ON orders (user_id, state);

CREATE TABLE order_items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id         INTEGER NOT NULL REFERENCES orders(id),
    product_id       INTEGER NOT NULL REFERENCES products(id),
    quantity         INTEGER NOT NULL DEFAULT 1,
    unit_price_cents INTEGER NOT NULL
);
CREATE INDEX idx_order_items_order ON order_items (order_id);

CREATE TABLE payments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     INTEGER NOT NULL REFERENCES orders(id),
    provider     VARCHAR(30) NOT NULL,   -- stripe | paypal | ...
    amount_cents INTEGER NOT NULL,
    captured_at  DATETIME
);

CREATE TABLE shipments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL UNIQUE REFERENCES orders(id),  -- 1:1 with orders
    carrier     VARCHAR(30) NOT NULL,
    tracking_no VARCHAR(60),
    shipped_at  DATETIME
);

CREATE TABLE reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    user_id    INTEGER NOT NULL REFERENCES users(id),
    rating     INTEGER NOT NULL,   -- 1-5 stars
    body       TEXT,
    UNIQUE (product_id, user_id)   -- one review per product per user
);

CREATE TABLE coupons (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    code           VARCHAR(30) NOT NULL UNIQUE,
    discount_cents INTEGER NOT NULL,
    expires_at     DATETIME
);

CREATE TABLE order_coupons (
    order_id  INTEGER NOT NULL REFERENCES orders(id),
    coupon_id INTEGER NOT NULL REFERENCES coupons(id),
    PRIMARY KEY (order_id, coupon_id)
);

-- append-only audit trail: *_id columns on purpose have NO foreign keys, so
-- `--infer-fk` can demonstrate guessing edges from column names alone
CREATE TABLE activity_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    order_id   INTEGER,
    action     VARCHAR(50) NOT NULL,
    created_at DATETIME NOT NULL
);
CREATE INDEX idx_logs_created ON activity_logs (created_at);
"""


def build():
    if DB.exists():
        DB.unlink()
    conn = sqlite3.connect(DB)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
    print(f'Wrote {DB}')


if __name__ == '__main__':
    build()
