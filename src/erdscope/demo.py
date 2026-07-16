# ---------------------------------------------------------------------------
# `erdscope demo` — try the tool with zero setup, no database of your own.
#
# `pyproject.toml` ships this project as a single module (`py-modules =
# ["erd"]`), so examples/demo_shop.db is NOT part of the wheel — a `pip
# install`ed user has no file to point `erdscope sqlite:///...` at. Embedding
# the demo schema DDL here (it ends up inlined in erd.py by
# tools/build_single_file.py) sidesteps that entirely: `erdscope demo` builds
# a throwaway copy of the same sample database in a temp directory and runs
# it through the normal sqlite adapter pipeline, so it works identically
# whether you grabbed erd.py or did `pip install erdscope`.
#
# DEMO_SCHEMA_SQL is the same e-commerce schema as examples/build_demo_db.py
# (which now imports it from here — see that file) and, in spirit, the hosted
# demo (docs/gen_demo.py); kept byte-identical to the committed
# examples/demo_shop.db's schema so the two never drift apart.
# ---------------------------------------------------------------------------
DEMO_SCHEMA_SQL = """
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


def build_demo_db(path):
    """Create a fresh SQLite database at `path` from DEMO_SCHEMA_SQL (any
    existing file there is replaced). Shared by `erdscope demo` (built into a
    throwaway temp directory on every run) and examples/build_demo_db.py
    (regenerating the committed examples/demo_shop.db). Returns `path`."""
    import sqlite3
    path = Path(path)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(DEMO_SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()
    return path


def run_demo(args):
    """Handle `erdscope demo`: the CLI's `database` positional was the literal
    string "demo" (see the sentinel check in main()). Build a throwaway copy
    of the sample e-commerce database in a temp directory, point the normal
    sqlite adapter pipeline at it (as a sqlite:///<absolute path> URL, so
    every other flag — --only/--excel/--max-rows/... — still applies
    unmodified), then open the result in a browser.

    Config auto-discovery is force-disabled: a stray .erdscope.json in the
    cwd should not silently change what the demo looks like. An explicit
    --config is not an error either — it's ignored with a warning, since the
    whole point of `demo` is that it "just works" with no setup."""
    import tempfile
    import webbrowser

    if getattr(args, 'config', None):
        print('Warning: --config is ignored by `erdscope demo`', file=sys.stderr)
        args.config = None
    args.no_config = True  # also skips .erdscope.* auto-discovery (load_config)

    if not hasattr(args, 'output'):  # -o not given on the CLI — SUPPRESS default
        args.output = 'erd_demo.html'  # don't clobber a plain `erd.html` from a real run

    with tempfile.TemporaryDirectory() as tmp:
        db_path = build_demo_db(Path(tmp) / 'demo_shop.db')
        # 4-slash sqlite:// form: 3 literal slashes + the absolute path's own
        # leading slash (sqlite_path_from_url strips exactly one of them).
        args.database = 'sqlite:///' + str(db_path.resolve())
        _run_pipeline(args)  # builds, filters, and writes args.output — all
                             # while the temp db file above still exists

    if not getattr(args, 'no_open', False):
        try:
            webbrowser.open(Path(args.output).resolve().as_uri())
        except Exception:
            pass  # best-effort convenience only — never fail the run over it
