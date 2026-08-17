# agentcore-harness-demo

A production harness around a non-deterministic agent, on **Amazon
Bedrock AgentCore**. Domain: e-commerce returns and refunds support.

**The ticket:** *"Order #4711 arrived damaged, I want a refund."*
Resolving it correctly requires reading customer data, computing money
under policy, and executing an irreversible write — three different
risk tiers in one request.

## The trust boundary

Five capabilities, deliberately split across two sides:

| Capability | Runs on | Why |
|---|---|---|
| `code_interpreter` | AWS microVM | Sandboxed compute; AWS handles isolation |
| managed memory | AWS, actor-scoped | Per-customer history, isolated by `actorId` |
| `run_sql` (L1 read) | **Our process** | Gated: read-only contract + read-only connection |
| `issue_refund` (L3 write) | **Our process** | Gated: recompute + idempotency + 2-phase commit |

Anything that can move money or leak data is an `inline_function`,
because an inline function pauses the harness loop and hands control
back to code we own. The gate is not in the model's environment, so it
cannot be prompt-injected or argued with.

## Four gates before money moves

1. **Schema** — Pydantic contract on the tool boundary.
2. **Independent recompute** — we re-run the policy engine ourselves and
   refuse on any disagreement with the agent's figure. *The model is
   what transcribed the number, so the number is not trusted.* That
   includes `days_since_delivery`, which is derived from
   `orders.delivered_on` rather than believed: it is the input that
   decides eligibility, so trusting it would recompute the arithmetic
   but not the decision.
3. **Idempotency** — key is a hash of the semantic action
   `(order_id, reason, amount)`, and it's the `PRIMARY KEY` of
   `refund_intents`. The guarantee is a database uniqueness
   constraint, not application logic a race can slip between.
4. **Two-phase commit** — `PENDING` before the effect, `COMMITTED`
   after; a failure mid-flight triggers a compensating rollback.

The policy calculator (`refund_calc.py`) is **shipped, not
model-generated** — a sandbox doesn't make arithmetic deterministic if
the model writes the arithmetic. It's seeded onto the microVM with
`InvokeAgentRuntimeCommand`, and the same module is imported by the
gate for verification. Money is `Decimal` end to end.

## Files

| File | Slide | What it is |
|---|---|---|
| `gates.py` | 6, 8 | Both tool contracts; the four refund gates |
| `refund_calc.py` | 7 | Deterministic policy engine (shipped + verifying) |
| `refund_ledger.py` | 8 | Idempotency keys, two-phase commit, rollback |
| `budget.py` | — | Cost/iteration caps, bounded aborts |
| `harness_client.py` | 5 | invoke → stream → gate → continue; episode records |
| `demo_run.py` | 10 | The six recorded beats |
| `evals/test_invariants.py` | 9 | 16 offline tests, no model calls |
| `evals/test_trajectory.py` | 9 | Live N-run trajectory + memory isolation |

## Run it

```bash
python -m pip install boto3 pydantic pytest
bash setup.sh                             # AWS setup (read the comments)
python gates.py                           # seed the database
python -m pytest evals/test_invariants.py -v   # 16 tests, offline, ~0.1s
export HARNESS_ARN=...                    # setup.sh prints this line
python demo_run.py                        # the recorded scenario (reseeds itself)
python -m pytest evals/test_trajectory.py -v -s  # 10 live runs + isolation
```

Use **one** interpreter for all of it, and invoke the tests as
`python -m pytest`. A bare `pytest` resolves independently of `python`,
so a machine with two environments will happily run the demo on the one
with `boto3` and the tests on the one without.

## Two-tier eval strategy

Tier 1 (`test_invariants.py`) is deterministic and offline — it runs on
every commit because the harness is ordinary software. Tier 2
(`test_trajectory.py`) makes live model calls and asserts on the
*distribution* across N runs; it runs on a schedule, not per-push.
Memory isolation is the exception: it's a security property, so one
failure fails the suite with no pass-rate tolerance.

## What `demo_run.py` shows live:

1. **Read-only gate** — `UPDATE`, `DELETE`, `DROP` and a stacked
   `SELECT 1; UPDATE ...` all blocked, driven directly against the same
   `run_sql` the agent's tool use hits
2. **The agent works the ticket** — the code interpreter runs *our*
   shipped calculator → **$172.03**, and `issue_refund` commits after
   the independent recompute agrees
3. **Amount gate** — `$999.00` and a 96-cent slip both refused, driven
   directly against the same `issue_refund`; the correct figure comes
   back `duplicate=True`, collapsing onto the refund the agent just made
4. **Retry storm** — the same semantic action replayed 3×, ledger still
   shows one row *(because the key is the action, not the call site)*
5. **New session, same customer** — memory answers with **zero tool
   calls**
6. **New customer** — asked the same kind of question, the agent has
   **no record of them at all**; nothing carries across the `actorId`
   boundary *(note: memory is isolated; row-level SQL access is not —
   see `code_readme.md`)*

### Beats 1 and 3 are driven by us, not by the agent

Those blocked mutations and wrong amounts are direct calls into
`run_sql` and `issue_refund`. The agent attempts neither: given the
schema up front it goes straight to `SELECT` and routes the write
through `issue_refund`, and when its figure matches the policy engine
there is nothing to refuse.

Driving the gates ourselves is the more honest demonstration. The
guarantees are a Pydantic contract, a `mode=ro` connection and an
independent recompute, so they hold whether or not the model misbehaves
on camera — waiting for it to trip them would be theatre, and would
prove strictly less. Note the stacked statement fails with a *different*
error than the rest: it satisfies the `SELECT` regex and dies on the
read-only connection, which is the second layer doing its job.

Every one of those paths is also proved offline, with no model in the
loop: `test_mutations_are_blocked`,
`test_stacked_statement_blocked_by_second_layer`,
`test_agent_hallucinated_amount_is_refused`, and
`test_understated_day_count_is_refused`.
