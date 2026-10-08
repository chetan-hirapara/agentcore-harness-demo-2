"""Gate for run_sql (risk tier L1: read-only).

Four layers, each catching what the one before it can miss:

  1. Pydantic contract     the query must start with SELECT
  2. read-only connection  mode=ro, so a stacked statement that slips past
                           the regex still cannot write
  3. authorizer            only SELECT/READ/FUNCTION are permitted, and
                           real tables cannot be read directly
  4. per-customer views    the model sees ONLY this customer's rows
"""
import logging
import sqlite3

from pydantic import BaseModel, Field, ValidationError

from harness_demo.db import SupportDb
from harness_demo.guardrails.results import blocked_from_validation

log = logging.getLogger(__name__)


class SqlToolInput(BaseModel):
    query: str = Field(pattern=r"(?i)^\s*SELECT\b")
    max_rows: int = Field(default=100, le=1_000)
    timeout_s: int = Field(default=10, le=30)


def run_sql(db: SupportDb, customer_id: str, payload: dict) -> dict:
    try:
        req = SqlToolInput(**payload)
    except ValidationError as e:
        log.info("run_sql blocked by contract: %s", payload.get("query"))
        return blocked_from_validation(
            "Gate: only read-only SELECT queries are permitted.", e)
    return _execute_scoped(db, customer_id, req)


def _execute_scoped(db: SupportDb, customer_id: str,
                    req: SqlToolInput) -> dict:
    conn = db.connect_scoped(customer_id, timeout=req.timeout_s)
    try:
        cur = conn.execute(req.query)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(req.max_rows)
        return {"blocked": False, "columns": cols,
                "rows": [list(r) for r in rows], "row_count": len(rows)}
    except sqlite3.Error as e:
        log.info("run_sql blocked by database: %s", e)
        return {"blocked": True, "reason": f"Database error: {e}", "fix": []}
    finally:
        conn.close()
