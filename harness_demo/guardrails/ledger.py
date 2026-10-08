"""Idempotency and rollback for the one mutating tool.

Agents RETRY: a timeout, a dropped stream, a re-planned step, and the
same refund gets issued twice. The customer is made whole twice and
someone finds out at month-end close.

Two mechanisms, both deliberately boring:

1. IDEMPOTENCY. The key is a hash of the semantic identity of the
   action -- (order_id, reason, amount_cents) -- not a random UUID the
   agent could regenerate. It is the PRIMARY KEY of refund_intents, so
   the guarantee is a uniqueness constraint enforced by the database,
   not application logic a race can slip between.

2. TWO-PHASE COMMIT with a compensating action. PENDING is written
   before any effect; COMMITTED after. A crash between the two leaves a
   PENDING row -- visible, reconcilable, and safe to replay because of
   mechanism 1. Failure mid-flight triggers rollback(), which reverses
   the order status and marks the intent ROLLED_BACK.
"""
import hashlib
import sqlite3
import time
from decimal import Decimal

from harness_demo.db import SupportDb


def idempotency_key(order_id: int, reason: str, amount: str) -> str:
    """Semantic identity of the action. Same refund -> same key, always,
    across retries, sessions and processes."""
    cents = int((Decimal(str(amount)) * 100).to_integral_value())
    raw = f"refund:v1:{order_id}:{reason.lower().strip()}:{cents}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class RefundLedger:
    def __init__(self, db: SupportDb):
        self.db = db

    def _conn(self) -> sqlite3.Connection:
        c = self.db.connect_rw()
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def begin(self, key: str, order_id: int, amount: str) -> tuple[str, dict]:
        """Reserve the intent. Returns ("NEW", row) or ("DUPLICATE", prior)."""
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO refund_intents "
                    "(key, order_id, amount_usd, state, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (key, order_id, str(amount), "PENDING", time.time()))
                conn.execute("COMMIT")
                return "NEW", {"key": key, "state": "PENDING"}
            except sqlite3.IntegrityError:
                # The uniqueness constraint IS the idempotency guarantee.
                conn.execute("ROLLBACK")
                return "DUPLICATE", self.get(key)
        finally:
            conn.close()

    def commit(self, key: str) -> dict:
        conn = self._conn()
        try:
            conn.execute(
                "UPDATE refund_intents SET state='COMMITTED', settled_at=? "
                "WHERE key=? AND state='PENDING'", (time.time(), key))
        finally:
            conn.close()
        return self.get(key)

    def rollback(self, key: str, error: str) -> dict:
        """Compensating action: reverse the effect, mark the intent."""
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT order_id FROM refund_intents WHERE key=?",
                (key,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE orders SET status='delivered' WHERE order_id=?",
                    (row[0],))
            conn.execute(
                "UPDATE refund_intents SET state='ROLLED_BACK', error=?, "
                "settled_at=? WHERE key=?", (error[:500], time.time(), key))
            conn.execute("COMMIT")
        finally:
            conn.close()
        return self.get(key)

    def apply_refund(self, order_id: int) -> None:
        """The actual effect. A separate method so a fault can be injected
        between begin() and commit() to demonstrate rollback."""
        conn = self._conn()
        try:
            conn.execute("UPDATE orders SET status='refunded' WHERE order_id=?",
                         (order_id,))
        finally:
            conn.close()

    def get(self, key: str) -> dict:
        return self._one("SELECT * FROM refund_intents WHERE key=?", (key,))

    def all_intents(self) -> list[dict]:
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM refund_intents ORDER BY created_at")
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            conn.close()

    def _one(self, sql: str, params: tuple) -> dict:
        conn = self._conn()
        try:
            cur = conn.execute(sql, params)
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
            return dict(zip(cols, row)) if row else {}
        finally:
            conn.close()
