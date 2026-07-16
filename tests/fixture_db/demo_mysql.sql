-- MySQL dialect of DEMO_SCHEMA_SQL + verification extras
CREATE TABLE users (
    id         BIGINT AUTO_INCREMENT PRIMARY KEY,
    email      VARCHAR(255) NOT NULL UNIQUE,
    name       VARCHAR(100) NOT NULL COMMENT '氏名',
    status     INT NOT NULL DEFAULT 1 COMMENT '1: active, 2: suspended',
    created_at DATETIME NOT NULL
) COMMENT='ユーザー';

CREATE TABLE addresses (
    id      BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id BIGINT NOT NULL,
    kind    VARCHAR(20) NOT NULL DEFAULT 'shipping',
    line1   VARCHAR(200) NOT NULL,
    city    VARCHAR(100) NOT NULL,
    country CHAR(2) NOT NULL DEFAULT 'JP',
    FOREIGN KEY (user_id) REFERENCES users(id)
) COMMENT='住所	タブ入りコメント';
CREATE INDEX idx_addresses_user_kind ON addresses (user_id, kind);

CREATE TABLE products (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    sku          VARCHAR(40) NOT NULL UNIQUE,
    title        VARCHAR(200) NOT NULL,
    price_cents  INT NOT NULL DEFAULT 0,
    stock        INT NOT NULL DEFAULT 0,
    discontinued TINYINT NOT NULL DEFAULT 0,
    attrs        JSON
);

CREATE TABLE categories (
    id        BIGINT AUTO_INCREMENT PRIMARY KEY,
    parent_id BIGINT,
    name      VARCHAR(100) NOT NULL,
    FOREIGN KEY (parent_id) REFERENCES categories(id)
);

CREATE TABLE product_categories (
    product_id  BIGINT NOT NULL,
    category_id BIGINT NOT NULL,
    PRIMARY KEY (product_id, category_id),
    FOREIGN KEY (product_id) REFERENCES products(id),
    FOREIGN KEY (category_id) REFERENCES categories(id)
);

CREATE TABLE orders (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    address_id  BIGINT NOT NULL,
    state       VARCHAR(20) NOT NULL DEFAULT 'cart',
    total_cents INT NOT NULL DEFAULT 0,
    placed_at   DATETIME,
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (address_id) REFERENCES addresses(id)
) COMMENT='注文';
CREATE INDEX idx_orders_user_state ON orders (user_id, state);

CREATE TABLE order_items (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    order_id         BIGINT NOT NULL,
    product_id       BIGINT NOT NULL,
    quantity         INT NOT NULL DEFAULT 1,
    unit_price_cents INT NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(id),
    FOREIGN KEY (product_id) REFERENCES products(id)
);

CREATE TABLE payments (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    order_id     BIGINT NOT NULL,
    provider     VARCHAR(30) NOT NULL,
    amount_cents INT NOT NULL,
    captured_at  DATETIME,
    FOREIGN KEY (order_id) REFERENCES orders(id)
);

CREATE TABLE shipments (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    order_id    BIGINT NOT NULL UNIQUE,
    carrier     VARCHAR(30) NOT NULL,
    tracking_no VARCHAR(60),
    shipped_at  DATETIME,
    FOREIGN KEY (order_id) REFERENCES orders(id)
);

CREATE TABLE reviews (
    id         BIGINT AUTO_INCREMENT PRIMARY KEY,
    product_id BIGINT NOT NULL,
    user_id    BIGINT NOT NULL,
    rating     INT NOT NULL,
    body       TEXT,
    UNIQUE (product_id, user_id),
    FOREIGN KEY (product_id) REFERENCES products(id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE coupons (
    id             BIGINT AUTO_INCREMENT PRIMARY KEY,
    code           VARCHAR(30) NOT NULL UNIQUE,
    discount_cents INT NOT NULL,
    expires_at     DATETIME
);

CREATE TABLE order_coupons (
    order_id  BIGINT NOT NULL,
    coupon_id BIGINT NOT NULL,
    PRIMARY KEY (order_id, coupon_id),
    FOREIGN KEY (order_id) REFERENCES orders(id),
    FOREIGN KEY (coupon_id) REFERENCES coupons(id)
);

CREATE TABLE activity_logs (
    id         BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id    BIGINT,
    order_id   BIGINT,
    action     VARCHAR(50) NOT NULL,
    created_at DATETIME NOT NULL
);
CREATE INDEX idx_logs_created ON activity_logs (created_at);

-- view: must be excluded (BASE TABLE filter)
CREATE VIEW active_users AS SELECT id, email FROM users WHERE status = 1;
