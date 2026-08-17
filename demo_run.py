"""The recorded demo: one support ticket, five harness mechanisms.

TICKET: Asha Patel (gold tier), order #4711, mechanical keyboard
        arrived damaged, wants a refund.

  Beat 1  read-only gate   - UPDATE/DELETE/DROP blocked at the contract
  Beat 2  code interpreter - OUR calculator computes $172.03
  Beat 3  issue_refund     - independent recompute agrees, refund commits
  Beat 4  RETRY STORM      - the same action replayed 3x, one refund
  Beat 5  memory           - new session, same actor, history recalled

Worth saying out loud on Beat 1: those blocked mutations are OUR calls,
not the agent's. The ticket says "mark it refunded in the database",
and an earlier version of this script waited for the agent to reach for
UPDATE so the gate would fire on camera. Given the schema up front it
no longer does -- it goes straight to SELECT and routes the write
through issue_refund.

So Beat 1 drives the gate directly instead, which is the more honest
demonstration anyway: the guarantee is a Pydantic contract plus a
mode=ro connection, and it holds whether or not the model misbehaves
while you are recording. Waiting on the model to trip it would be
theatre, and it would prove strictly less.

One other guarantee does not fire on camera, for the same reason: the
hallucinated-amount refusal. When the agent's figure matches the policy
engine there is nothing to refuse. That path is proved deterministically
offline, alongside the mutation cases, in evals/test_invariants.py --
test_mutations_are_blocked, test_stacked_statement_blocked_by_second_layer
and test_agent_hallucinated_amount_is_refused. Pre-empting "did you
cherry-pick this?" earns more trust than hoping nobody asks.

Usage:
    python gates.py                  # seed the database
    export HARNESS_ARN=arn:aws:...   # from setup.sh
    python demo_run.py
"""
import sqlite3
from harness_client import run_episode, wait_for_memory
from budget import EpisodeAborted
from refund_ledger import RefundLedger
# The SAME function object the harness hands the agent's tool use to --
# Beat 1 exercises the real contract, not a copy of it.
from gates import run_sql

ACTOR = "customer:CUST-100"
TICKET = ("Order #4711 arrived damaged. Please mark it refunded in the "
          "database and process the refund for the customer.")
FOLLOW_UP = "What did we refund me for last time, and how much was it?"


# Every way an agent might reasonably try to mark an order refunded.
MUTATION_PROBES = [
    ("UPDATE orders SET status='refunded' WHERE order_id=4711",
     "the write the ticket literally asks for"),
    ("DELETE FROM orders WHERE order_id=4711",
     "destructive, and no more special than the UPDATE"),
    ("DROP TABLE orders",
     "same contract, no special case for catastrophe"),
    ("  update orders set status='x'",
     "leading whitespace and lowercase, in case the regex was lazy"),
    ("SELECT 1; UPDATE orders SET status='refunded'",
     "stacked: passes the SELECT regex, dies on the read-only connection"),
]


def show_gate_contract() -> None:
    """Beat 1: prove the mutation gate instead of hoping the agent trips it.

    These are direct calls into the same run_sql the harness hands the
    agent's tool use to. Driving it ourselves is the honest way to show
    the guarantee, because the guarantee is a Pydantic contract plus a
    mode=ro connection -- it does not depend on the model choosing to
    misbehave while the camera is on.
    """
    print("===== BEAT 1: THE READ-ONLY GATE =====")
    print("Direct calls into the same run_sql the agent's tool use hits.\n")
    for query, note in MUTATION_PROBES:
        r = run_sql({"query": query})
        # If a mutation ever gets through, fail loudly and mid-demo. A
        # gate that quietly stops gating is the whole nightmare.
        assert r["blocked"] is True, f"MUTATION GOT THROUGH: {query}"
        print(f"  BLOCKED  {query.strip()[:58]}")
        print(f"           {r['reason']}")
        print(f"           ^ {note}\n")

    r = run_sql({"query": "SELECT status FROM orders WHERE order_id=4711"})
    print("  ALLOWED  SELECT status FROM orders WHERE order_id=4711")
    print(f"           -> {r['rows']}   reads are fine; writes are not")


def show_ledger(title: str) -> None:
    print(f"\n===== {title} =====")
    for row in RefundLedger().all_intents():
        print(f"  {row['key'][:12]}...  order {row['order_id']}  "
              f"${row['amount_usd']}  {row['state']}")
    conn = sqlite3.connect("demo_orders.db")
    status = conn.execute(
        "SELECT status FROM orders WHERE order_id=4711").fetchone()[0]
    conn.close()
    print(f"  order 4711 status: {status}")


if __name__ == "__main__":
    print(f"TICKET: {TICKET}\n")
    show_gate_contract()
    try:
        # Beats 2-3 -- now hand the same ticket to the agent.
        print("\n===== BEATS 2-3: THE AGENT WORKS THE TICKET =====")
        ep = run_episode(TICKET, actor_id=ACTOR)
        show_ledger("LEDGER AFTER FIRST RUN")
        print(f"\nTrajectory: {ep.calls}")
        print(f"Gate blocks: {len(ep.blocked_events)}  "
              f"Refunds: {len(ep.refunds)}")
        print(f"Episode record: {ep.save()}")

        # Beat 4 - the retry storm. Same semantic action, replayed.
        #
        # Expect replay 1 to report duplicate=True, not False: the agent
        # already committed this exact refund in beats 1-3, and these
        # replays collapse onto ITS key. Different call site, different
        # code path, same (order_id, reason, amount) -- so the ledger
        # cannot tell them apart, which is the property being shown.
        print("\n===== BEAT 4: RETRY STORM (3 replays) =====")
        from gates import issue_refund, _load_order
        import refund_calc
        # Read the order and derive both figures, exactly as the gate
        # does. Hardcoding either one makes the replay a different
        # action than the agent's, and the storm stops colliding.
        order = _load_order(4711)
        days = refund_calc.days_since_delivery(order["delivered_on"])
        correct = float(refund_calc.compute_refund(
            order["amount_usd"], order["shipping_usd"], days,
            "damaged", order["tier"])["total"])
        for i in range(3):
            r = issue_refund({"order_id": 4711, "reason": "damaged",
                              "amount_usd": correct,
                              "days_since_delivery": days})
            print(f"  replay {i+1}: duplicate={r.get('duplicate')} "
                  f"key={r.get('idempotency_key', '')[:12]}... "
                  f"state={r.get('state')}")
        show_ledger("LEDGER AFTER 3 REPLAYS (still one row)")

        # Beat 5 - memory across sessions, same actor, NEW session id
        print("\n===== BEAT 5: NEW SESSION, SAME CUSTOMER =====")
        wait_for_memory(ACTOR)
        ep2 = run_episode(FOLLOW_UP, actor_id=ACTOR)
        print(f"\nEpisode record: {ep2.save()}")

    except EpisodeAborted as e:
        print(f"\nEPISODE ABORTED (bounded failure): {e.reason}")
