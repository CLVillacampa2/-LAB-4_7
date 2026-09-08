from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable

import pandas as pd


@dataclass(frozen=True)
class RestockAlert:
    sku: str
    product_name: str
    on_hand: float
    daily_usage_rate: float
    days_remaining: float | None
    reorder_quantity: float
    reason: str


class InventoryService:
    """Inventory logging and trend calculations for a food or retail pantry."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS products (
                product_id INTEGER PRIMARY KEY AUTOINCREMENT,
                sku TEXT NOT NULL UNIQUE,
                product_name TEXT NOT NULL,
                category TEXT NOT NULL,
                unit TEXT NOT NULL DEFAULT 'unit',
                reorder_level REAL NOT NULL DEFAULT 0,
                reorder_quantity REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS inventory_batches (
                batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL REFERENCES products(product_id),
                quantity REAL NOT NULL,
                received_date TEXT NOT NULL,
                expiry_date TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS inventory_transactions (
                transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL REFERENCES products(product_id),
                batch_id INTEGER REFERENCES inventory_batches(batch_id),
                transaction_type TEXT NOT NULL CHECK(transaction_type IN ('purchase', 'consumption', 'waste', 'adjustment')),
                quantity REAL NOT NULL,
                transaction_date TEXT NOT NULL,
                note TEXT
            );
            """
        )
        self.connection.commit()

    def add_product(self, sku: str, product_name: str, category: str,
                    reorder_level: float, reorder_quantity: float,
                    unit: str = "unit") -> int:
        cursor = self.connection.execute(
            """INSERT INTO products
               (sku, product_name, category, unit, reorder_level, reorder_quantity)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (sku, product_name, category, unit, reorder_level, reorder_quantity),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def receive_batch(self, product_id: int, quantity: float,
                      received_date: date, expiry_date: date) -> int:
        cursor = self.connection.execute(
            """INSERT INTO inventory_batches
               (product_id, quantity, received_date, expiry_date)
               VALUES (?, ?, ?, ?)""",
            (product_id, quantity, received_date.isoformat(), expiry_date.isoformat()),
        )
        self.connection.execute(
            """INSERT INTO inventory_transactions
               (product_id, batch_id, transaction_type, quantity, transaction_date, note)
               VALUES (?, ?, 'purchase', ?, ?, 'Batch received')""",
            (product_id, cursor.lastrowid, quantity, received_date.isoformat()),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def log_consumption(self, product_id: int, quantity: float,
                        transaction_date: date, note: str = "") -> None:
        remaining = quantity
        batches = self.connection.execute(
            """SELECT batch_id, quantity FROM inventory_batches
               WHERE product_id = ? AND quantity > 0
               ORDER BY expiry_date, batch_id""",
            (product_id,),
        ).fetchall()
        if sum(float(batch["quantity"]) for batch in batches) < quantity:
            raise ValueError("consumption exceeds available inventory")
        for batch in batches:
            used = min(remaining, float(batch["quantity"]))
            self.connection.execute(
                "UPDATE inventory_batches SET quantity = quantity - ? WHERE batch_id = ?",
                (used, batch["batch_id"]),
            )
            remaining -= used
            if remaining <= 0:
                break
        self.connection.execute(
            """INSERT INTO inventory_transactions
               (product_id, transaction_type, quantity, transaction_date, note)
               VALUES (?, 'consumption', ?, ?, ?)""",
            (product_id, quantity, transaction_date.isoformat(), note),
        )
        self.connection.commit()

    def inventory_snapshot(self, as_of: date | None = None) -> pd.DataFrame:
        as_of = as_of or date.today()
        return pd.read_sql_query(
            """
            SELECT p.sku, p.product_name, p.category, p.reorder_level,
                   p.reorder_quantity, COALESCE(SUM(b.quantity), 0) AS on_hand,
                   MIN(CASE WHEN b.quantity > 0 THEN b.expiry_date END) AS next_expiry
            FROM products p
            LEFT JOIN inventory_batches b ON b.product_id = p.product_id
            GROUP BY p.product_id, p.sku, p.product_name, p.category,
                     p.reorder_level, p.reorder_quantity
            ORDER BY p.product_name
            """,
            self.connection,
        ).assign(next_expiry=lambda frame: pd.to_datetime(frame["next_expiry"]))

    def usage_rates(self, as_of: date | None = None, window_days: int = 30) -> pd.DataFrame:
        as_of = as_of or date.today()
        start = as_of - timedelta(days=window_days - 1)
        frame = pd.read_sql_query(
            """
            SELECT p.sku, p.product_name, p.category,
                   COALESCE(SUM(CASE WHEN t.transaction_type = 'consumption'
                                     THEN t.quantity ELSE 0 END), 0) AS consumed_quantity
            FROM products p
            LEFT JOIN inventory_transactions t
              ON t.product_id = p.product_id
             AND t.transaction_date BETWEEN ? AND ?
            GROUP BY p.product_id, p.sku, p.product_name, p.category
            ORDER BY consumed_quantity DESC
            """,
            self.connection,
            params=(start.isoformat(), as_of.isoformat()),
        )
        frame["daily_usage_rate"] = (frame["consumed_quantity"] / window_days).round(2)
        frame["weekly_usage_rate"] = (frame["daily_usage_rate"] * 7).round(2)
        return frame

    def restock_alerts(self, as_of: date | None = None, window_days: int = 30,
                       coverage_days: int = 7) -> list[RestockAlert]:
        snapshot = self.inventory_snapshot(as_of).set_index("sku")
        rates = self.usage_rates(as_of, window_days).set_index("sku")
        alerts: list[RestockAlert] = []
        for sku, product in snapshot.iterrows():
            daily_rate = float(rates.loc[sku, "daily_usage_rate"])
            on_hand = float(product["on_hand"])
            days_remaining = on_hand / daily_rate if daily_rate else None
            reasons: list[str] = []
            if on_hand <= float(product["reorder_level"]):
                reasons.append("at or below reorder level")
            if days_remaining is not None and days_remaining <= coverage_days:
                reasons.append(f"only {days_remaining:.1f} days of stock")
            if reasons:
                alerts.append(RestockAlert(
                    sku=sku,
                    product_name=str(product["product_name"]),
                    on_hand=on_hand,
                    daily_usage_rate=daily_rate,
                    days_remaining=days_remaining,
                    reorder_quantity=float(product["reorder_quantity"]),
                    reason="; ".join(reasons),
                ))
        return alerts

    def expiring_batches(self, as_of: date | None = None, within_days: int = 7) -> pd.DataFrame:
        as_of = as_of or date.today()
        end = as_of + timedelta(days=within_days)
        return pd.read_sql_query(
            """
            SELECT p.sku, p.product_name, b.batch_id, b.quantity, b.expiry_date,
                   CAST(julianday(b.expiry_date) - julianday(?) AS INTEGER) AS days_to_expiry
            FROM inventory_batches b
            JOIN products p ON p.product_id = b.product_id
            WHERE b.quantity > 0 AND b.expiry_date <= ?
            ORDER BY b.expiry_date
            """,
            self.connection,
            params=(as_of.isoformat(), end.isoformat()),
        )


def build_demo_service() -> InventoryService:
    service = InventoryService(sqlite3.connect(":memory:"))
    today = date(2026, 9, 8)
    rice = service.add_product("RICE-001", "Brown Rice", "Grains", 10, 30, "kg")
    milk = service.add_product("MILK-001", "Long-life Milk", "Dairy", 12, 24, "carton")
    service.receive_batch(rice, 40, today - timedelta(days=8), today + timedelta(days=120))
    service.receive_batch(milk, 26, today - timedelta(days=20), today + timedelta(days=4))
    service.log_consumption(rice, 21, today - timedelta(days=14), "Meal preparation")
    service.log_consumption(milk, 18, today - timedelta(days=14), "Breakfast service")
    return service


if __name__ == "__main__":
    demo = build_demo_service()
    print("Usage rates")
    print(demo.usage_rates(as_of=date(2026, 9, 8)).to_string(index=False))
    print("\nRestock alerts")
    for alert in demo.restock_alerts(as_of=date(2026, 9, 8)):
        print(alert)
    print("\nExpiring batches")
    print(demo.expiring_batches(as_of=date(2026, 9, 8)).to_string(index=False))
