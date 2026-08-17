"""Tier 1 evals: deterministic, offline, every commit.

No model calls. These test the HARNESS, and the harness is ordinary
software -- which is the point. The expensive non-deterministic tests
live in test_trajectory.py and run on a schedule, not on every push.

Run:  pytest evals/test_invariants.py -v
"""
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import gates
import refund_calc
from refund_ledger import RefundLedger, idempotency_key


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db = str(tmp_path / "test.db")
    monkeypatch.setattr(gates, "DB_PATH", db)
    monkeypatch.setattr("refund_ledger.DB_PATH", db)
    monkeypatch.setattr(gates.RefundLedger, "__init__",
                        lambda self, p=db: setattr(self, "db_path", db))
    gates.seed_demo_db()
    yield db


# Derived, never hardcoded: order 4711 is $149.00 + $9.99 shipping,
# gold tier, damaged. The day count comes from the seed, which sets
# delivery dates relative to today -- a literal here would drift out of
# the return window and fail for the wrong reason a month from now.
DAYS_4711 = gates.DELIVERED_DAYS_AGO[4711]
CORRECT_TOTAL = float(refund_calc.compute_refund(
    149.00, 9.99, DAYS_4711, "damaged", "gold")["total"])

REFUND_4711 = {"order_id": 4711, "reason": "damaged",
               "amount_usd": CORRECT_TOTAL,
               "days_since_delivery": DAYS_4711}


# ---------------------------------------------------- read-only gate
@pytest.mark.parametrize("query", [
    "UPDATE orders SET status='refunded' WHERE order_id=4711",
    "DELETE FROM orders",
    "DROP TABLE orders",
    "  update orders set status='x'",          # whitespace + case
])
def test_mutations_are_blocked(query):
    assert gates.run_sql({"query": query})["blocked"] is True


def test_stacked_statement_blocked_by_second_layer():
    # Passes the regex, dies on the read-only single-statement connection.
    r = gates.run_sql({"query": "SELECT 1; UPDATE orders SET status='x'"})
    assert r["blocked"] is True


def test_select_is_allowed():
    r = gates.run_sql({"query": "SELECT * FROM orders WHERE order_id=4711"})
    assert r["blocked"] is False and r["row_count"] == 1


# ------------------------------------------------ deterministic money
def test_calculator_is_deterministic():
    runs = {refund_calc.compute_refund(149.00, 9.99, 5, "damaged", "gold")["total"]
            for _ in range(100)}
    assert len(runs) == 1


def test_gold_tier_waives_restocking_fee():
    gold = refund_calc.compute_refund(100, 0, 5, "changed_mind", "gold")
    std = refund_calc.compute_refund(100, 0, 5, "changed_mind", "standard")
    assert Decimal(gold["total"]) > Decimal(std["total"])


def test_outside_return_window_is_ineligible():
    assert refund_calc.compute_refund(100, 0, 31, "damaged", "gold")["eligible"] is False


def test_agent_hallucinated_amount_is_refused():
    """The single most important gate: the model reports a number, we
    recompute it independently, and disagreement stops the money."""
    bad = dict(REFUND_4711, amount_usd=999.00)
    r = gates.issue_refund(bad)
    assert r["blocked"] is True and "mismatch" in r["reason"].lower()
    assert RefundLedger().all_intents() == []      # nothing reserved


def test_understated_day_count_is_refused():
    """Order 4712 was delivered well outside the return window. An agent
    that reports a small day count would otherwise be refunded on a
    total that reconciles perfectly -- the arithmetic was never the lie."""
    # The amount an in-window 4712 WOULD earn, so only the days are wrong.
    plausible = float(refund_calc.compute_refund(
        89.50, 5.99, 5, "damaged", "standard")["total"])
    r = gates.issue_refund({"order_id": 4712, "reason": "damaged",
                            "amount_usd": plausible,
                            "days_since_delivery": 5})
    assert r["blocked"] is True
    assert "day-count mismatch" in r["reason"].lower()
    assert RefundLedger().all_intents() == []      # nothing reserved


def test_day_count_is_derived_from_the_order_not_the_agent():
    """Eligibility is decided by OUR reading of orders.delivered_on."""
    order = gates._load_order(4712)
    actual = refund_calc.days_since_delivery(order["delivered_on"])
    assert actual == gates.DELIVERED_DAYS_AGO[4712] > refund_calc.RETURN_WINDOW_DAYS

    # Honest day count on a stale order: refused on policy, not on the
    # mismatch -- a different gate, and it must still stop the money.
    r = gates.issue_refund({"order_id": 4712, "reason": "damaged",
                            "amount_usd": 0.00, "days_since_delivery": actual})
    assert r["blocked"] is True
    assert r["policy"]["reason_code"] == "OUTSIDE_RETURN_WINDOW"


def test_day_count_tolerates_clock_skew():
    """One day of slack absorbs midnight and timezones; it does not
    excuse a guess."""
    ok = dict(REFUND_4711, days_since_delivery=DAYS_4711 + gates.DAY_TOLERANCE)
    assert gates.issue_refund(ok)["blocked"] is False

    off = dict(REFUND_4711,
               days_since_delivery=DAYS_4711 + gates.DAY_TOLERANCE + 1)
    assert gates.issue_refund(off)["blocked"] is True


# ------------------------------------------------------- idempotency
def test_key_is_semantic_not_random():
    a = idempotency_key(4711, "damaged", "172.99")
    b = idempotency_key(4711, "DAMAGED ", "172.99")   # same action
    c = idempotency_key(4711, "damaged", "172.98")    # different amount
    assert a == b and a != c


def test_retry_storm_issues_exactly_one_refund():
    first = gates.issue_refund(REFUND_4711)
    assert first["duplicate"] is False and first["state"] == "COMMITTED"
    for _ in range(5):
        again = gates.issue_refund(REFUND_4711)
        assert again["duplicate"] is True
        assert again["idempotency_key"] == first["idempotency_key"]
    assert len(RefundLedger().all_intents()) == 1


# ---------------------------------------------------------- rollback
def test_failure_mid_flight_is_reversed(monkeypatch):
    monkeypatch.setattr(gates, "INJECT_COMMIT_FAULT", True)
    r = gates.issue_refund(REFUND_4711)
    assert r["blocked"] is True and r["rolled_back"] is True

    intents = RefundLedger().all_intents()
    assert len(intents) == 1 and intents[0]["state"] == "ROLLED_BACK"

    order = gates._load_order(4711)
    assert order["status"] == "delivered"   # effect reversed, not left dirty
