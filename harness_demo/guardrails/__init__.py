"""Guardrails: the code that stands between the model and anything risky.

Every gated tool is declared to the harness as an `inline_function`, so
the harness PAUSES and hands the call to our process. The gate therefore
is not in the model's environment and cannot be prompt-injected.

  sql_gate      L1 read-only    contract + read-only + one-customer scope
  refund_gate   L3 money write  recompute + idempotency + two-phase commit
  ledger        the idempotency key and the rollback
  budget        cost and tool-call caps for the episode
  toolbox       binds the gates to one database and one customer session
"""
