-- PostgreSQL dialect of DEMO_SCHEMA_SQL + verification extras
CREATE TABLE users (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email      VARCHAR(255) NOT NULL UNIQUE,
    name       VARCHAR(100) NOT NULL,
    status     INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL
);
COMMENT ON TABLE users IS 'ユーザー';
COMMENT ON COLUMN users.name IS '氏名';
COMMENT ON COLUMN users.status IS E'1: active\n2: suspended';

CREATE TABLE addresses (
    id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    kind    VARCHAR(20) NOT NULL DEFAULT 'shipping',
    line1   VARCHAR(200) NOT NULL,
    city    VARCHAR(100) NOT NULL,
    country CHAR(2) NOT NULL DEFAULT 'JP'
);
COMMENT ON TABLE addresses IS E'住所\tタブ入りコメント';
CREATE INDEX idx_addresses_user_kind ON addresses (user_id, kind);

CREATE TABLE products (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sku          VARCHAR(40) NOT NULL UNIQUE,
    title        VARCHAR(200) NOT NULL,
    price_cents  INTEGER NOT NULL DEFAULT 0,
    stock        INTEGER NOT NULL DEFAULT 0,
    discontinued BOOLEAN NOT NULL DEFAULT FALSE,
    attrs        JSONB
);
-- expression index (past verification point)
CREATE INDEX idx_products_title_lower ON products (lower(title));

CREATE TABLE categories (
    id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    parent_id BIGINT REFERENCES categories(id),
    name      VARCHAR(100) NOT NULL
);

CREATE TABLE product_categories (
    product_id  BIGINT NOT NULL REFERENCES products(id),
    category_id BIGINT NOT NULL REFERENCES categories(id),
    PRIMARY KEY (product_id, category_id)
);

CREATE TABLE orders (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id),
    address_id  BIGINT NOT NULL REFERENCES addresses(id),
    state       VARCHAR(20) NOT NULL DEFAULT 'cart',
    total_cents INTEGER NOT NULL DEFAULT 0,
    placed_at   TIMESTAMP
);
COMMENT ON TABLE orders IS '注文';
CREATE INDEX idx_orders_user_state ON orders (user_id, state);

CREATE TABLE order_items (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id         BIGINT NOT NULL REFERENCES orders(id),
    product_id       BIGINT NOT NULL REFERENCES products(id),
    quantity         INTEGER NOT NULL DEFAULT 1,
    unit_price_cents INTEGER NOT NULL
);
CREATE INDEX idx_order_items_order ON order_items (order_id);

CREATE TABLE payments (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id     BIGINT NOT NULL REFERENCES orders(id),
    provider     VARCHAR(30) NOT NULL,
    amount_cents INTEGER NOT NULL,
    captured_at  TIMESTAMP
);

CREATE TABLE shipments (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id    BIGINT NOT NULL UNIQUE REFERENCES orders(id),
    carrier     VARCHAR(30) NOT NULL,
    tracking_no VARCHAR(60),
    shipped_at  TIMESTAMP
);

CREATE TABLE reviews (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id BIGINT NOT NULL REFERENCES products(id),
    user_id    BIGINT NOT NULL REFERENCES users(id),
    rating     INTEGER NOT NULL,
    body       TEXT,
    UNIQUE (product_id, user_id)
);

CREATE TABLE coupons (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code           VARCHAR(30) NOT NULL UNIQUE,
    discount_cents INTEGER NOT NULL,
    expires_at     TIMESTAMP
);

CREATE TABLE order_coupons (
    order_id  BIGINT NOT NULL REFERENCES orders(id),
    coupon_id BIGINT NOT NULL REFERENCES coupons(id),
    PRIMARY KEY (order_id, coupon_id)
);

CREATE TABLE activity_logs (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id    BIGINT,
    order_id   BIGINT,
    action     VARCHAR(50) NOT NULL,
    created_at TIMESTAMP NOT NULL
);
CREATE INDEX idx_logs_created ON activity_logs (created_at);

-- view: must be excluded
CREATE VIEW active_users AS SELECT id, email FROM users WHERE status = 1;

-- second schema: verifies the ?schema=name URL parameter picks up a
-- non-public schema instead of (or in addition to) the default
CREATE SCHEMA app2;
CREATE TABLE app2.widgets (
    id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name VARCHAR(100) NOT NULL
);
