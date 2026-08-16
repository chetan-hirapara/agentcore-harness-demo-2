"""Deterministic refund policy engine.

THE POINT OF THIS FILE: a code sandbox does not make arithmetic
deterministic if the *model* writes the arithmetic. Model-generated
policy logic is just non-determinism with better syntax highlighting.

So we ship the calculator. The agent only supplies inputs and runs it
in the AgentCore code interpreter. The same module is then imported by
gates.py to INDEPENDENTLY RECOMPUTE the figure before any money moves —
if the agent's reported amount disagrees with ours by a cent, the
refund is blocked.

Money is Decimal end to end. Floats in a money path are a defect, not
a style preference: 0.1 + 0.2 != 0.3, and a refund is a legal record.
"""
import argparse
import json
from decimal import Decimal, ROUND_HALF_UP

POLICY_VERSION = "2026.02"

RETURN_WINDOW_DAYS = 30
RESTOCKING_FEE_PCT = {
    "damaged": Decimal("0.00"),
    "defective": Decimal("0.00"),
    "wrong_item": Decimal("0.00"),
    "changed_mind": Decimal("0.15"),
}
SHIPPING_REFUNDABLE = {"damaged", "defective", "wrong_item"}
FEE_WAIVED_TIERS = {"gold", "platinum"}
TAX_RATE = Decimal("0.0875")


def money(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_refund(item_price, shipping_paid, days_since_delivery,
                   reason: str, tier: str = "standard") -> dict:
    item_price = money(item_price)
    shipping_paid = money(shipping_paid)
    reason = reason.lower().strip()
    tier = tier.lower().strip()

    if reason not in RESTOCKING_FEE_PCT:
        raise ValueError(f"unknown reason: {reason}")

    if days_since_delivery > RETURN_WINDOW_DAYS:
        return {
            "eligible": False,
            "total": "0.00",
            "policy_version": POLICY_VERSION,
            "reason_code": "OUTSIDE_RETURN_WINDOW",
            "breakdown": [
                f"Delivered {days_since_delivery} days ago; "
                f"window is {RETURN_WINDOW_DAYS} days."
            ],
        }

    fee_pct = RESTOCKING_FEE_PCT[reason]
    if tier in FEE_WAIVED_TIERS:
        fee_pct = Decimal("0.00")

    restocking_fee = money(item_price * fee_pct)
    item_refund = money(item_price - restocking_fee)
    shipping_refund = shipping_paid if reason in SHIPPING_REFUNDABLE else money(0)
    tax_refund = money(item_refund * TAX_RATE)
    total = money(item_refund + shipping_refund + tax_refund)

    return {
        "eligible": True,
        "item_refund": str(item_refund),
        "restocking_fee": str(restocking_fee),
        "shipping_refund": str(shipping_refund),
        "tax_refund": str(tax_refund),
        "total": str(total),
        "policy_version": POLICY_VERSION,
        "reason_code": "APPROVED",
        "breakdown": [
            f"Item {item_price} less restocking {restocking_fee} "
            f"({fee_pct:.0%}{' waived: ' + tier if tier in FEE_WAIVED_TIERS else ''})",
            f"Shipping {shipping_refund} "
            f"({'refundable' if reason in SHIPPING_REFUNDABLE else 'not refundable'} for {reason})",
            f"Tax {tax_refund} at {TAX_RATE:.2%}",
        ],
    }


def main() -> None:
    """CLI so the AgentCore code interpreter can run this on the microVM."""
    p = argparse.ArgumentParser()
    p.add_argument("--item-price", required=True)
    p.add_argument("--shipping", required=True)
    p.add_argument("--days", type=int, required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--tier", default="standard")
    a = p.parse_args()
    print(json.dumps(compute_refund(
        a.item_price, a.shipping, a.days, a.reason, a.tier), indent=2))


if __name__ == "__main__":
    main()
