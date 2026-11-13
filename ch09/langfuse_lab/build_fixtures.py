"""Deterministically generate customers.json and orders.json. Run: $PY -m langfuse_lab.build_fixtures"""
import json
import random
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
NAMES = ["Asha", "Ben", "Chen", "Dana", "Eli", "Farah", "Gus", "Hana", "Ivan", "Jo"]
STATUSES = ["delivered", "in_transit", "charged_twice", "delayed", "refunded"]
CARRIERS = ["UPS", "FedEx", "DHL"]


def build(seed: int = 7):
    rng = random.Random(seed)
    customers = [
        {
            "customer_id": f"cust_{i:02d}",
            "name": name,
            "email": f"{name.lower()}@example.com",
            "plan": rng.choice(["basic", "pro"]),
            "subscription_id": f"sub_{i:02d}",
        }
        for i, name in enumerate(NAMES)
    ]

    def order(order_id, customer_id, status):
        return {
            "order_id": order_id,
            "customer_id": customer_id,
            "amount": round(rng.uniform(15, 400), 2),
            "status": status,
            "carrier": rng.choice(CARRIERS),
            "eta_days": 0 if status in ("delivered", "refunded", "charged_twice") else rng.randint(1, 6),
        }

    orders = [order(str(8100 + i * 3), customers[i % 10]["customer_id"], STATUSES[i % 5]) for i in range(30)]
    # Pinned ids referenced by the existing ch09 cases.
    orders.append(order("8129", "cust_00", "charged_twice"))
    orders.append(order("9912", "cust_01", "in_transit"))
    orders[-1]["eta_days"] = 1  # matches the "should arrive tomorrow" reference answer in cases.json
    return customers, orders


if __name__ == "__main__":
    customers, orders = build()
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "customers.json").write_text(json.dumps(customers, indent=1), encoding="utf-8")
    (DATA_DIR / "orders.json").write_text(json.dumps(orders, indent=1), encoding="utf-8")
    print(f"wrote {len(customers)} customers, {len(orders)} orders")
