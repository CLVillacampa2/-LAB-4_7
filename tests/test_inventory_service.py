from datetime import date
import sqlite3

from inventory_service import InventoryService


TODAY = date(2026, 9, 8)


def make_service():
    service = InventoryService(sqlite3.connect(":memory:"))
    product_id = service.add_product("COFFEE-001", "Ground Coffee", "Beverages", 10, 25, "bag")
    service.receive_batch(product_id, 28, TODAY - date.resolution, TODAY + date.resolution * 3)
    return service, product_id


def test_usage_rate_and_restock_alert():
    service, product_id = make_service()
    service.log_consumption(product_id, 20, TODAY - date.resolution * 9)

    usage = service.usage_rates(as_of=TODAY)
    assert usage.iloc[0]["daily_usage_rate"] == 0.67

    alerts = service.restock_alerts(as_of=TODAY)
    assert len(alerts) == 1
    assert alerts[0].sku == "COFFEE-001"
    assert "reorder level" in alerts[0].reason


def test_expiring_batch_report():
    service, _ = make_service()
    report = service.expiring_batches(as_of=TODAY, within_days=7)
    assert list(report["days_to_expiry"]) == [3]
