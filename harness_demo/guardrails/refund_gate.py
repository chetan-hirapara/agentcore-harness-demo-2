"""Gate for issue_refund (risk tier L3: irreversible external write).

The only tool that moves money. Before it does, the request passes:

  SCOPE CHECK   the order must belong to THIS session's customer
  GATE 1        schema: a Pydantic contract on the tool boundary
  GATE 2        independent recompute of the day count AND the amount
  GATE 3        idempotency: a semantic key enforced by a PRIMARY KEY
  GATE 4        two-phase commit with a compensating rollback

The model proposes; this code decides.
"""
import logging
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from harness_demo.db import SupportDb
from harness_demo.guardrails.ledger import RefundLedger, idempotency_key
from harness_demo.guardrails.results import blocked_from_validation
from harness_demo.policy import refund_calc

log = logging.getLogger(__name__)

CENT = Decimal("0.01")

# Slack on the derived day count, for clock skew and timezone edges. One
# day, not two: the tolerance exists to absorb midnight, not to excuse a
# guess.
DAY_TOLERANCE = 1


class RefundToolInput(BaseModel):
    order_id: int = Field(ge=1)
    reason: Literal["damaged", "defective", "wrong_item", "changed_mind"]
    amount_usd: Decimal = Field(ge=0, le=10_000)   # the agent's figure
    days_since_delivery: int = Field(ge=0, le=3650)


def issue_refund(db: SupportDb, customer_id: str, payload: dict, *,
                 inject_commit_fault: bool = False) -> dict:
    try:
        req = RefundToolInput(**payload)                       # GATE 1
    except ValidationError as e:
        return blocked_from_validation("Gate: invalid refund request.", e)

    order = db.load_order(req.order_id)
    # Same message for "missing" and "someone else's": do not let the
    # model probe which order ids exist.
    if not order or order["customer_id"] != customer_id:
        log.warning("refund refused: order %s not visible to %s",
                    req.order_id, customer_id)
        return {"blocked": True, "reason": f"Order {req.order_id} not found.",
                "fix": []}

    # GATE 2 - INDEPENDENT RECOMPUTE. We never trust a number the model
    # reports back, even one the sandbox produced: the model is what
    # transcribed it. That includes the DAY COUNT, because it decides
    # eligibility -- recomputing the total from a reported day count
    # checks the arithmetic but not the decision.
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
        # JSON 999.0 reaches us as Decimal("999.0"); quantize both figures
        # so the refusal never prints "$999.0" next to "$172.03".
        return {"blocked": True,
                "reason": ("Gate: amount mismatch. Agent reported "
                           f"${refund_calc.money(req.amount_usd)}, policy "
                           f"computes ${refund_calc.money(expected_total)}. "
                           "Refusing to move money."),
                "policy": expected}

    # GATE 3 - IDEMPOTENCY.
    key = idempotency_key(req.order_id, req.reason, str(expected_total))
    ledger = RefundLedger(db)
    state, prior = ledger.begin(key, req.order_id, str(expected_total))
    if state == "DUPLICATE":
        log.info("refund collapsed onto existing key %s", key[:12])
        return {"blocked": False, "duplicate": True, "idempotency_key": key,
                "amount_usd": str(expected_total), "state": prior.get("state"),
                "note": "Refund already issued for this exact action. "
                        "Returning the prior result; no second refund."}

    # GATE 4 - TWO-PHASE COMMIT with a compensating action.
    try:
        ledger.apply_refund(req.order_id)
        if inject_commit_fault:
            raise RuntimeError("payment processor timeout")
        ledger.commit(key)
    except Exception as e:                      # noqa: BLE001 - must reverse on ANY failure
        log.error("refund %s failed, rolling back: %s", key[:12], e)
        rolled = ledger.rollback(key, str(e))
        return {"blocked": True, "rolled_back": True, "idempotency_key": key,
                "reason": f"Refund failed and was reversed: {e}",
                "state": rolled.get("state")}

    log.info("refund committed: order %s key %s", req.order_id, key[:12])
    return {"blocked": False, "duplicate": False, "idempotency_key": key,
            "amount_usd": str(expected_total), "state": "COMMITTED",
            "policy_version": expected["policy_version"],
            "breakdown": expected["breakdown"]}
