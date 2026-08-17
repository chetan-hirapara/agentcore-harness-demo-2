"""Layer 2 - Guardrails. The tool boundary contracts.

Both gated tools are declared to the AgentCore harness as
`inline_function`s: the harness pauses with stopReason "tool_use" and
hands the call to THIS process. The gate is therefore not in the
model's environment and cannot be reasoned around, prompt-injected, or
argued with.

Two tools, two risk tiers:

  run_sql       L1 read-only      -> regex contract + read-only connection
  issue_refund  L3 external write -> schema + INDEPENDENT RECOMPUTE +
                                     idempotency key + two-phase commit
"""
import sqlite3
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, Field, ValidationError

import refund_calc
from refund_ledger import RefundLedger, idempotency_key

DB_PATH = "demo_orders.db"
CENT = Decimal("0.01")

# Slack on the derived day count, for clock skew and timezone edges
# between whoever computed the date and this process. One day, not two:
# the tolerance exists to absorb midnight, not to excuse a guess.
DAY_TOLERANCE = 1

# Set True to demonstrate rollback: the effect is applied, then the
# commit fails, and the compensating action reverses it.
INJECT_COMMIT_FAULT = False


# ---------------------------------------------------------------- L1
class SqlToolInput(BaseModel):            # tool boundary contract
    # read-only: enforced, not requested
    query: str = Field(pattern=r"(?i)^\s*SELECT\b")
    max_rows: int = Field(default=100, le=1_000)
    timeout_s: int = Field(default=10, le=30)


def run_sql(payload: dict) -> dict:
    try:
        req = SqlToolInput(**payload)     # hard gate, not advice
    except ValidationError as e:
        return _blocked("Gate: only read-only SELECT queries are permitted.", e)
    return execute_readonly(req.query, req.max_rows, req.timeout_s)


def execute_readonly(query: str, max_rows: int, timeout_s: int) -> dict:
    # mode=ro is the second layer. A mutation that somehow satisfied the
    # regex (stacked statements, CTEs, comments) dies here instead.
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=timeout_s)
    try:
        cur = conn.execute(query)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(max_rows)
        return {"blocked": False, "columns": cols,
                "rows": [list(r) for r in rows], "row_count": len(rows)}
    except sqlite3.Error as e:
        return {"blocked": True, "reason": f"Database error: {e}", "fix": []}
    finally:
        conn.close()


# ---------------------------------------------------------------- L3
class RefundToolInput(BaseModel):
    order_id: int = Field(ge=1)
    reason: Literal["damaged", "defective", "wrong_item", "changed_mind"]
    amount_usd: Decimal = Field(ge=0, le=10_000)   # the agent's figure
    days_since_delivery: int = Field(ge=0, le=3650)


def issue_refund(payload: dict) -> dict:
    """The only tool that moves money. Four gates before it does."""
    # GATE 1 - schema
    try:
        req = RefundToolInput(**payload)
    except ValidationError as e:
        return _blocked("Gate: invalid refund request.", e)

    order = _load_order(req.order_id)
    if not order:
        return {"blocked": True, "reason": f"Order {req.order_id} not found.",
                "fix": []}

    # GATE 2 - INDEPENDENT RECOMPUTE. We never trust a number the model
    # reports back to us, even one the sandbox produced: the model is
    # what transcribed it.
    #
    # That includes the DAY COUNT, and this is the input that matters
    # most, because days_since_delivery is what decides eligibility.
    # Recomputing the total from a reported day count checks the
    # arithmetic but not the decision: an agent claiming "5 days" for a
    # year-old order would get a refund the policy owes nobody, and
    # every figure in it would reconcile. So derive it from
    # orders.delivered_on, and refuse on disagreement exactly as we do
    # for the amount.
    actual_days = refund_calc.days_since_delivery(order["delivered_on"])
    if abs(req.days_since_delivery - actual_days) > DAY_TOLERANCE:
        return {"blocked": True,
                "reason": ("Gate: day-count mismatch. Agent reported "
                           f"{req.days_since_delivery} days since delivery; "
                           f"orders.delivered_on ({order['delivered_on']}) "
                           f"gives {actual_days}. Refusing to move money."),
                "fix": [{"field": ["days_since_delivery"],
                         "problem": f"must be {actual_days} for this order"}]}

    expected = refund_calc.compute_refund(
        item_price=order["amount_usd"],
        shipping_paid=order["shipping_usd"],
        days_since_delivery=actual_days,      # derived, not reported
        reason=req.reason,
        tier=order["tier"],
    )
    if not expected["eligible"]:
        return {"blocked": True,
                "reason": f"Policy: {expected['reason_code']}.",
                "policy": expected}

    expected_total = Decimal(expected["total"])
    if abs(req.amount_usd - expected_total) > CENT:
        return {"blocked": True,
                "reason": ("Gate: amount mismatch. Agent reported "
                           f"${req.amount_usd}, policy computes "
                           f"${expected_total}. Refusing to move money."),
                "policy": expected}

    # GATE 3 - IDEMPOTENCY. Semantic key, enforced by a PRIMARY KEY.
    key = idempotency_key(req.order_id, req.reason, str(expected_total))
    ledger = RefundLedger(DB_PATH)
    state, prior = ledger.begin(key, req.order_id, str(expected_total))
    if state == "DUPLICATE":
        return {"blocked": False, "duplicate": True, "idempotency_key": key,
                "amount_usd": str(expected_total), "state": prior.get("state"),
                "note": "Refund already issued for this exact action. "
                        "Returning the prior result; no second refund."}

    # GATE 4 - TWO-PHASE COMMIT with a compensating action.
    try:
        ledger.apply_refund(req.order_id)
        if INJECT_COMMIT_FAULT:
            raise RuntimeError("payment processor timeout")
        ledger.commit(key)
    except Exception as e:                      # noqa: BLE001 - demo fault
        rolled = ledger.rollback(key, str(e))
        return {"blocked": True, "rolled_back": True, "idempotency_key": key,
                "reason": f"Refund failed and was reversed: {e}",
                "state": rolled.get("state")}

    return {"blocked": False, "duplicate": False, "idempotency_key": key,
            "amount_usd": str(expected_total), "state": "COMMITTED",
            "policy_version": expected["policy_version"],
            "breakdown": expected["breakdown"]}


# ------------------------------------------------------------ helpers
def _blocked(reason: str, e: ValidationError) -> dict:
    # Structured remediation -> the agent can self-correct.
    return {"blocked": True, "reason": reason,
            "fix": [{"field": list(err["loc"]), "problem": err["msg"]}
                    for err in e.errors()]}


def _load_order(order_id: int) -> dict | None:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        cur = conn.execute(
            "SELECT o.order_id, o.customer_id, o.amount_usd, o.shipping_usd, "
            "o.delivered_on, o.status, c.tier FROM orders o JOIN customers c "
            "ON c.customer_id = o.customer_id WHERE o.order_id = ?",
            (order_id,))
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
        return dict(zip(cols, row)) if row else None
    finally:
        conn.close()


# Delivery dates are seeded RELATIVE TO TODAY, never hardcoded. A fixed
# date is a time bomb against a 30-day return window: the demo works the
# week it is written and starts answering OUTSIDE_RETURN_WINDOW a month
# later, on camera, correctly, for a reason nobody in the room will
# guess. 4712 is deliberately stale -- an ineligible order to query.
DELIVERED_DAYS_AGO = {4711: 2, 4712: 227, 4713: 5, 4801: 3, 4802: 2}


def _delivered_on(order_id: int, today: date | None = None) -> str:
    d = (today or date.today()) - timedelta(days=DELIVERED_DAYS_AGO[order_id])
    return d.isoformat()


def seed_demo_db() -> None:
    """Create the support-desk schema. Run once before the demo."""
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        DROP TABLE IF EXISTS orders;
        DROP TABLE IF EXISTS customers;
        DROP TABLE IF EXISTS refund_intents;

        CREATE TABLE customers (
            customer_id TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            tier        TEXT NOT NULL
        );
        CREATE TABLE orders (
            order_id     INTEGER PRIMARY KEY,
            customer_id  TEXT NOT NULL REFERENCES customers(customer_id),
            item         TEXT NOT NULL,
            amount_usd   REAL NOT NULL,
            shipping_usd REAL NOT NULL,
            delivered_on TEXT NOT NULL,
            status       TEXT NOT NULL
        );
        -- The idempotency guarantee is this PRIMARY KEY.
        CREATE TABLE refund_intents (
            key        TEXT PRIMARY KEY,
            order_id   INTEGER NOT NULL,
            amount_usd TEXT NOT NULL,
            state      TEXT NOT NULL,
            created_at REAL NOT NULL,
            settled_at REAL,
            error      TEXT
        );

        INSERT INTO customers VALUES
            ('CUST-100', 'Asha Patel',  'gold'),
            ('CUST-200', 'Diego Ramos', 'standard'),
            ('CUST-300', 'Liu Wei',    'platinum');
    """)
    conn.executemany(
        "INSERT INTO orders VALUES (?,?,?,?,?,?,?)",
        [(oid, cust, item, price, ship, _delivered_on(oid), "delivered")
         for oid, cust, item, price, ship in (
             (4711, 'CUST-100', 'Mechanical keyboard', 149.00, 9.99),
             (4712, 'CUST-200', 'USB-C dock',           89.50, 5.99),
             (4713, 'CUST-100', 'Monitor arm',          59.99, 0.00),
             (4801, 'CUST-200', 'Laptop stand',         39.99, 4.99),
             (4802, 'CUST-300', 'Wireless mouse',       29.99, 3.99),
         )])
    conn.commit()
    conn.close()
    print(f"Seeded {DB_PATH}: 3 customers, 5 orders, empty refund ledger. "
          f"Order 4711 delivered {_delivered_on(4711)} "
          f"({DELIVERED_DAYS_AGO[4711]}d ago, inside the "
          f"{refund_calc.RETURN_WINDOW_DAYS}d window).")


if __name__ == "__main__":
    seed_demo_db()
