"""A production-style harness around a non-deterministic support agent.

Read the package in this order:

  policy/      the refund rules (deterministic, shipped to the sandbox)
  guardrails/  the gates that sit between the model and anything risky
  agent/       the harness loop: invoke -> stream -> gate -> continue
  demo/        the six-beat live scenario that exercises all of it

Vocabulary used throughout:

  harness  the managed AgentCore loop that calls the model and its tools
  episode  one ticket worked end to end, recorded as an audit artefact
  actor    the memory-isolation boundary (one customer = one actorId)
  gate     code WE own that can refuse a tool call the model asked for
"""
