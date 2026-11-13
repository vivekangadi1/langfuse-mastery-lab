"""Load the lab's JSON fixtures once."""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"


@dataclass(frozen=True)
class Fixtures:
    customers: dict
    orders: dict
    kb: list
    cases: list
    sessions: list


def _read(name: str, default=None):
    path = DATA_DIR / name
    if not path.exists() and default is not None:
        return default
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def load_fixtures() -> Fixtures:
    return Fixtures(
        customers={c["customer_id"]: c for c in _read("customers.json")},
        orders={o["order_id"]: o for o in _read("orders.json")},
        kb=_read("kb.json", default=[]),
        cases=_read("cases.json", default=[]),
        sessions=_read("sessions.json", default=[]),
    )
