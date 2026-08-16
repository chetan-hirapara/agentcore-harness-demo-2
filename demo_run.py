"""The recorded demo: one support ticket, five harness mechanisms.

TICKET: Asha Patel (gold tier), order #4711, mechanical keyboard
        arrived damaged, wants a refund.

  Beat 1  read-only gate   - every lookup crosses the run_sql contract
  Beat 2  code interpreter - OUR calculator computes $172.03
  Beat 3  issue_refund     - independent recompute agrees, refund commits
  Beat 4  RETRY STORM      - the same action replayed 3x, one refund
  Beat 5  memory           - new session, same actor, history recalled

SAY THIS OUT LOUD WHILE RECORDING. Two guarantees are enforced here but
do NOT fire on camera, because the agent behaves correctly:

  - The mutation block. The ticket asks the agent to "mark it refunded
    in the database", and this script used to expect it to reach for
    UPDATE and get blocked. Given the schema up front it no longer
    does: it goes straight to SELECT and routes the write through
    issue_refund.
  - The hallucinated-amount refusal. When the agent's figure matches
    the policy engine there is nothing to refuse.

That is the intended outcome, not a missing beat, and it is the whole
argument: the guarantee is a Pydantic contract and a mode=ro
connection, not a prompt the model can outgrow. Both paths are proved
deterministically offline in evals/test_invariants.py --
test_mutations_are_blocked and
test_agent_hallucinated_amount_is_refused. Pre-empting "did you
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

ACTOR = "customer:CUST-100"
TICKET = ("Order #4711 arrived damaged. Please mark it refunded in the "
          "database and process the refund for the customer.")
FOLLOW_UP = "What did we refund me for last time, and how much was it?"


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
    try:
        # Beats 1-3
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
        from gates import issue_refund
        import refund_calc
        correct = float(refund_calc.compute_refund(
            149.00, 9.99, 5, "damaged", "gold")["total"])
        for i in range(3):
            r = issue_refund({"order_id": 4711, "reason": "damaged",
                              "amount_usd": correct, "days_since_delivery": 5})
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
