"""Net worth = manual accounts (assets − debts) + live brokerage value."""

from __future__ import annotations

from . import portfolio
from .store import Store


def compute(store: Store) -> dict:
    accounts = store.list_accounts()
    assets = 0.0
    debts = 0.0
    for a in accounts:
        if a["type"] == "debt":
            debts += a["balance"]
        else:
            assets += a["balance"]

    brokerage = portfolio.valuation(store)["total_value"]
    total = round(assets + brokerage - debts, 2)
    return {
        "total": total,
        "assets": round(assets, 2),
        "debts": round(debts, 2),
        "brokerage": brokerage,
        "accounts": accounts,
        "series": store.snapshots(),
    }


def snapshot(store: Store) -> float:
    total = compute(store)["total"]
    store.add_snapshot(total)
    return total
