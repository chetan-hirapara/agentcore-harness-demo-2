"""The gated tools, bound to one database and one customer session.

An episode owns exactly one GatedToolbox. The customer id comes from the
calling application (who authenticated the user), NEVER from the model,
so no prompt can widen what the gates let through.
"""
from collections.abc import Callable

from harness_demo.db import SupportDb
from harness_demo.guardrails.refund_gate import issue_refund
from harness_demo.guardrails.sql_gate import run_sql


class GatedToolbox:
    def __init__(self, db: SupportDb, customer_id: str, *,
                 inject_commit_fault: bool = False):
        self.db = db
        self.customer_id = customer_id
        self.inject_commit_fault = inject_commit_fault
        self._handlers: dict[str, Callable[[dict], dict]] = {
            "run_sql": self.run_sql,
            "issue_refund": self.issue_refund,
        }

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._handlers)

    def dispatch(self, name: str, payload: dict) -> dict:
        return self._handlers[name](payload)

    def run_sql(self, payload: dict) -> dict:
        return run_sql(self.db, self.customer_id, payload)

    def issue_refund(self, payload: dict) -> dict:
        return issue_refund(self.db, self.customer_id, payload,
                            inject_commit_fault=self.inject_commit_fault)
