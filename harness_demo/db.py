"""The support database and the ONLY ways code is allowed to open it.

Three kinds of connection, from most to least trusted:

  connect_rw()       our own code (ledger, seeding). Never the model's.
  connect_ro()       our own read-only lookups (the gate loading an order).
  connect_scoped()   what the MODEL's SQL runs on. Read-only AND limited to
                     one customer's rows.

THE SCOPING TRICK (this is what closes the cross-customer read):
SQLite has no row-level security, so we build it from two features.

  1. Per-connection TEMP VIEWs named `customers`, `orders` and
     `refund_intents`. The temp schema is searched first, so the model's
     `SELECT * FROM orders` silently hits the filtered view.
  2. An authorizer that denies any direct read of the real `main.*`
     tables. Only reads made *through* a view are allowed, so
     `SELECT * FROM main.orders` is refused.

The customer id comes from the calling application's session, never from
the model.
"""
import logging
import re
import sqlite3
from datetime import date, timedelta

from harness_demo.policy import refund_calc

log = logging.getLogger(__name__)

# Delivery dates are seeded RELATIVE TO TODAY, never hardcoded. A fixed
# date is a time bomb against a 30-day return window: the demo works the
# week it is written and starts answering OUTSIDE_RETURN_WINDOW a month
# later, for a reason nobody in the room will guess. 4712 is deliberately
# stale -- an ineligible order to query.
DELIVERED_DAYS_AGO = {4711: 2, 4712: 227, 4713: 5, 4801: 3, 4802: 2}

_CUSTOMER_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SQLITE_RECURSIVE = getattr(sqlite3, "SQLITE_RECURSIVE", 33)

_SCOPED_VIEWS = {
    "customers": "SELECT * FROM main.customers WHERE customer_id = '{cid}'",
    "orders": "SELECT * FROM main.orders WHERE customer_id = '{cid}'",
    "refund_intents": (
        "SELECT ri.* FROM main.refund_intents ri JOIN main.orders o "
        "ON o.order_id = ri.order_id WHERE o.customer_id = '{cid}'"),
}

_SCHEMA = """
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
"""

_ORDERS = (
    (4711, "CUST-100", "Mechanical keyboard", 149.00, 9.99),
    (4712, "CUST-200", "USB-C dock", 89.50, 5.99),
    (4713, "CUST-100", "Monitor arm", 59.99, 0.00),
    (4801, "CUST-200", "Laptop stand", 39.99, 4.99),
    (4802, "CUST-300", "Wireless mouse", 29.99, 3.99),
)


def delivered_on(order_id: int, today: date | None = None) -> str:
    return ((today or date.today())
            - timedelta(days=DELIVERED_DAYS_AGO[order_id])).isoformat()


def _scope_authorizer(action, arg1, arg2, dbname, source):
    if action == sqlite3.SQLITE_READ:
        if dbname == "main" and source is None:
            return sqlite3.SQLITE_DENY       # direct read of a real table
        if str(arg1).startswith("sqlite_"):
            return sqlite3.SQLITE_DENY       # no peeking at view definitions
        return sqlite3.SQLITE_OK
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION,
                  _SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


class SupportDb:
    def __init__(self, path: str):
        self.path = path

    def connect_rw(self, timeout: float = 10) -> sqlite3.Connection:
        return sqlite3.connect(self.path, isolation_level=None, timeout=timeout)

    def connect_ro(self, timeout: float = 10) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True,
                               timeout=timeout)

    def connect_scoped(self, customer_id: str,
                       timeout: float = 10) -> sqlite3.Connection:
        # DDL cannot take bound parameters, so the id is validated, not escaped.
        if not _CUSTOMER_ID.match(customer_id or ""):
            raise ValueError(f"invalid customer_id: {customer_id!r}")
        conn = self.connect_ro(timeout)
        for name, body in _SCOPED_VIEWS.items():
            conn.execute(f"CREATE TEMP VIEW {name} AS "
                         + body.format(cid=customer_id))
        conn.set_authorizer(_scope_authorizer)
        return conn

    def load_order(self, order_id: int) -> dict | None:
        """One order joined to its customer's tier. UNSCOPED: callers must
        check ownership themselves (the refund gate does)."""
        conn = self.connect_ro()
        try:
            cur = conn.execute(
                "SELECT o.order_id, o.customer_id, o.amount_usd, "
                "o.shipping_usd, o.delivered_on, o.status, c.tier "
                "FROM orders o JOIN customers c "
                "ON c.customer_id = o.customer_id WHERE o.order_id = ?",
                (order_id,))
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
            return dict(zip(cols, row)) if row else None
        finally:
            conn.close()

    def order_status(self, order_id: int) -> str:
        conn = self.connect_ro()
        try:
            return conn.execute("SELECT status FROM orders WHERE order_id=?",
                                (order_id,)).fetchone()[0]
        finally:
            conn.close()

    def schema_card(self) -> str:
        """Real column names, read from the database, for the system prompt.

        Hand-copied DDL drifts from the table; derived DDL cannot. Returns
        "" if the database is not seeded yet.
        """
        try:
            conn = self.connect_ro()
        except sqlite3.Error:
            return ""
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "ORDER BY name")]
            return "\n".join(
                "  {}({})".format(t, ", ".join(
                    f"{c[1]} {c[2]}"
                    for c in conn.execute(f"PRAGMA table_info({t})")))
                for t in tables)
        except sqlite3.Error:
            return ""
        finally:
            conn.close()

    def seed(self) -> None:
        """Recreate the support-desk schema with demo data."""
        conn = sqlite3.connect(self.path)
        try:
            conn.executescript(_SCHEMA)
            conn.executemany(
                "INSERT INTO orders VALUES (?,?,?,?,?,?,?)",
                [(oid, cust, item, price, ship, delivered_on(oid), "delivered")
                 for oid, cust, item, price, ship in _ORDERS])
            conn.commit()
        finally:
            conn.close()
        log.info("seeded %s: 3 customers, %d orders, empty refund ledger",
                 self.path, len(_ORDERS))


if __name__ == "__main__":
    from harness_demo.config import Settings

    SupportDb(Settings().db_path).seed()
    print(f"Seeded {Settings().db_path}: order 4711 delivered "
          f"{delivered_on(4711)} ({DELIVERED_DAYS_AGO[4711]}d ago, inside the "
          f"{refund_calc.RETURN_WINDOW_DAYS}d window).")
