from langfuse_lab.fixtures import load_fixtures


def test_counts_and_referential_integrity():
    fx = load_fixtures()
    assert len(fx.customers) == 10
    assert len(fx.orders) == 32
    assert all(o["customer_id"] in fx.customers for o in fx.orders.values())


def test_pinned_orders_used_by_existing_cases():
    fx = load_fixtures()
    assert fx.orders["8129"]["status"] == "charged_twice"
    assert fx.orders["9912"]["status"] == "in_transit"


def test_order_9912_eta_matches_case_reference():
    fx = load_fixtures()
    assert fx.orders["9912"]["eta_days"] == 1
