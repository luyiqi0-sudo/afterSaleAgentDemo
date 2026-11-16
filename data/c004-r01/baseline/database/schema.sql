-- C-002-v1: candidate-authored synthetic data; only the original three tables.
PRAGMA foreign_keys = ON;
CREATE TABLE orders (
    order_id TEXT PRIMARY KEY NOT NULL,
    user_id TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK(typeof(amount) = 'integer' AND amount BETWEEN 0 AND 1000000),
    status INTEGER CHECK(status IS NULL OR (typeof(status) = 'integer' AND status BETWEEN 0 AND 6)),
    create_time TEXT
);
CREATE INDEX orders_user_time ON orders(user_id, create_time);
CREATE TABLE logistics (
    logistics_id TEXT PRIMARY KEY NOT NULL,
    order_id TEXT NOT NULL UNIQUE REFERENCES orders(order_id),
    status INTEGER NOT NULL CHECK(typeof(status) = 'integer' AND status BETWEEN 0 AND 4),
    update_time TEXT NOT NULL
);
CREATE TABLE products (
    sku_id TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL CHECK(length(name) > 0),
    stock INTEGER NOT NULL CHECK(typeof(stock) = 'integer' AND stock BETWEEN 0 AND 10000),
    price INTEGER NOT NULL CHECK(typeof(price) = 'integer' AND price BETWEEN 0 AND 1000000)
);
