CREATE DATABASE IF NOT EXISTS inventory_db;
USE inventory_db;

CREATE TABLE IF NOT EXISTS products (
    product_id INT AUTO_INCREMENT PRIMARY KEY,
    sku VARCHAR(40) NOT NULL UNIQUE,
    product_name VARCHAR(120) NOT NULL,
    category VARCHAR(80) NOT NULL,
    unit VARCHAR(20) NOT NULL DEFAULT 'unit',
    reorder_level DECIMAL(12, 2) NOT NULL DEFAULT 0,
    reorder_quantity DECIMAL(12, 2) NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS inventory_batches (
    batch_id INT AUTO_INCREMENT PRIMARY KEY,
    product_id INT NOT NULL,
    quantity DECIMAL(12, 2) NOT NULL,
    received_date DATE NOT NULL,
    expiry_date DATE NOT NULL,
    FOREIGN KEY (product_id) REFERENCES products(product_id),
    INDEX idx_batch_expiry (expiry_date)
);

CREATE TABLE IF NOT EXISTS inventory_transactions (
    transaction_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    product_id INT NOT NULL,
    batch_id INT NULL,
    transaction_type ENUM('purchase', 'consumption', 'waste', 'adjustment') NOT NULL,
    quantity DECIMAL(12, 2) NOT NULL,
    transaction_date DATE NOT NULL,
    note VARCHAR(255),
    FOREIGN KEY (product_id) REFERENCES products(product_id),
    FOREIGN KEY (batch_id) REFERENCES inventory_batches(batch_id),
    INDEX idx_transaction_product_date (product_id, transaction_date)
);

CREATE OR REPLACE VIEW inventory_position AS
SELECT
    p.product_id,
    p.sku,
    p.product_name,
    p.category,
    p.reorder_level,
    p.reorder_quantity,
    COALESCE(SUM(b.quantity), 0) AS on_hand_quantity,
    MIN(CASE WHEN b.quantity > 0 THEN b.expiry_date END) AS next_expiry_date
FROM products p
LEFT JOIN inventory_batches b ON b.product_id = p.product_id
GROUP BY p.product_id, p.sku, p.product_name, p.category,
         p.reorder_level, p.reorder_quantity;

-- Usage rate (units per day) over the last 30 days.
SELECT p.sku, p.product_name,
       ROUND(COALESCE(SUM(t.quantity), 0) / 30, 2) AS daily_usage_rate
FROM products p
LEFT JOIN inventory_transactions t
  ON t.product_id = p.product_id
 AND t.transaction_type = 'consumption'
 AND t.transaction_date >= CURRENT_DATE - INTERVAL 30 DAY
GROUP BY p.product_id, p.sku, p.product_name;

-- Products that need restocking based on current inventory and recent usage.
WITH usage AS (
    SELECT product_id, COALESCE(SUM(quantity), 0) / 30 AS daily_usage_rate
    FROM inventory_transactions
    WHERE transaction_type = 'consumption'
      AND transaction_date >= CURRENT_DATE - INTERVAL 30 DAY
    GROUP BY product_id
)
SELECT ip.sku, ip.product_name, ip.on_hand_quantity,
       ROUND(COALESCE(u.daily_usage_rate, 0), 2) AS daily_usage_rate,
       ip.reorder_level,
       ip.reorder_quantity
FROM inventory_position ip
LEFT JOIN usage u ON u.product_id = ip.product_id
WHERE ip.on_hand_quantity <= ip.reorder_level
   OR (COALESCE(u.daily_usage_rate, 0) > 0
       AND ip.on_hand_quantity / u.daily_usage_rate <= 7)
ORDER BY ip.on_hand_quantity / NULLIF(u.daily_usage_rate, 0);

-- Batch shelf-life risk: expired or expiring within seven days.
SELECT p.sku, p.product_name, b.batch_id, b.quantity, b.expiry_date,
       DATEDIFF(b.expiry_date, CURRENT_DATE) AS days_to_expiry
FROM inventory_batches b
JOIN products p ON p.product_id = b.product_id
WHERE b.quantity > 0
  AND b.expiry_date <= CURRENT_DATE + INTERVAL 7 DAY
ORDER BY b.expiry_date;
