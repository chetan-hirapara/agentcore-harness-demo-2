"""Tier 1 evals: deterministic, offline, every commit.

No model calls. These test the HARNESS, and the harness is ordinary
software -- which is the point. The expensive non-deterministic tests
live in test_trajectory.py and run on a schedule, not on every push.

Run:  pytest evals/test_invariants.py -v
"""
import base64
import json
import os
import sys
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
# evals/ too, so the tier-2 module can be imported for the one contract
# the two tiers share -- see test_zero_facts_is_a_measurement.
sys.path.insert(0, HERE)

import pytest
import gates
import harness_client
import refund_calc
from refund_ledger import RefundLedger, idempotency_key


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db = str(tmp_path / "test.db")
    monkeypatch.setattr(gates, "DB_PATH", db)
    monkeypatch.setattr("refund_ledger.DB_PATH", db)
    monkeypatch.setattr(gates.RefundLedger, "__init__",
                        lambda self, p=db: setattr(self, "db_path", db))
    gates.seed_demo_db()
    yield db


# Derived, never hardcoded: order 4711 is $149.00 + $9.99 shipping,
# gold tier, damaged. The day count comes from the seed, which sets
# delivery dates relative to today -- a literal here would drift out of
# the return window and fail for the wrong reason a month from now.
DAYS_4711 = gates.DELIVERED_DAYS_AGO[4711]
CORRECT_TOTAL = float(refund_calc.compute_refund(
    149.00, 9.99, DAYS_4711, "damaged", "gold")["total"])

REFUND_4711 = {"order_id": 4711, "reason": "damaged",
               "amount_usd": CORRECT_TOTAL,
               "days_since_delivery": DAYS_4711}


# ---------------------------------------------------- read-only gate
@pytest.mark.parametrize("query", [
    "UPDATE orders SET status='refunded' WHERE order_id=4711",
    "DELETE FROM orders",
    "DROP TABLE orders",
    "  update orders set status='x'",          # whitespace + case
])
def test_mutations_are_blocked(query):
    assert gates.run_sql({"query": query})["blocked"] is True


def test_stacked_statement_blocked_by_second_layer():
    # Passes the regex, dies on the read-only single-statement connection.
    r = gates.run_sql({"query": "SELECT 1; UPDATE orders SET status='x'"})
    assert r["blocked"] is True


def test_select_is_allowed():
    r = gates.run_sql({"query": "SELECT * FROM orders WHERE order_id=4711"})
    assert r["blocked"] is False and r["row_count"] == 1


# ------------------------------------------------ deterministic money
def test_calculator_is_deterministic():
    runs = {refund_calc.compute_refund(149.00, 9.99, 5, "damaged", "gold")["total"]
            for _ in range(100)}
    assert len(runs) == 1


def test_gold_tier_waives_restocking_fee():
    gold = refund_calc.compute_refund(100, 0, 5, "changed_mind", "gold")
    std = refund_calc.compute_refund(100, 0, 5, "changed_mind", "standard")
    assert Decimal(gold["total"]) > Decimal(std["total"])


def test_outside_return_window_is_ineligible():
    assert refund_calc.compute_refund(100, 0, 31, "damaged", "gold")["eligible"] is False


def test_agent_hallucinated_amount_is_refused():
    """The single most important gate: the model reports a number, we
    recompute it independently, and disagreement stops the money."""
    bad = dict(REFUND_4711, amount_usd=999.00)
    r = gates.issue_refund(bad)
    assert r["blocked"] is True and "mismatch" in r["reason"].lower()
    assert RefundLedger().all_intents() == []      # nothing reserved
    # Both figures quantized to cents. This message goes on screen in the
    # demo, and "$999.0" next to "$172.03" reads as a defect in the gate.
    assert "$999.00" in r["reason"] and f"${CORRECT_TOTAL:.2f}" in r["reason"]


def test_refusal_message_quantizes_a_one_decimal_figure():
    """The regression the display quantizer exists for.

    999.00 in JSON reaches the gate as Decimal("999.0") -- trailing zeros
    are not preserved -- so f-stringing the raw Decimal prints "$999.0".
    A figure with a single decimal digit is the case that catches a
    quantizer removed or applied to only one of the two amounts.
    """
    r = gates.issue_refund(dict(REFUND_4711, amount_usd=999.50))
    assert r["blocked"] is True
    assert "$999.50" in r["reason"], r["reason"]
    assert "$999.5," not in r["reason"] and "$999.5 " not in r["reason"]
    # The policy figure is quantized by the same call, not by luck.
    assert f"${CORRECT_TOTAL:.2f}" in r["reason"]


def test_understated_day_count_is_refused():
    """Order 4712 was delivered well outside the return window. An agent
    that reports a small day count would otherwise be refunded on a
    total that reconciles perfectly -- the arithmetic was never the lie."""
    # The amount an in-window 4712 WOULD earn, so only the days are wrong.
    plausible = float(refund_calc.compute_refund(
        89.50, 5.99, 5, "damaged", "standard")["total"])
    r = gates.issue_refund({"order_id": 4712, "reason": "damaged",
                            "amount_usd": plausible,
                            "days_since_delivery": 5})
    assert r["blocked"] is True
    assert "day-count mismatch" in r["reason"].lower()
    assert RefundLedger().all_intents() == []      # nothing reserved


def test_day_count_is_derived_from_the_order_not_the_agent():
    """Eligibility is decided by OUR reading of orders.delivered_on."""
    order = gates._load_order(4712)
    actual = refund_calc.days_since_delivery(order["delivered_on"])
    assert actual == gates.DELIVERED_DAYS_AGO[4712] > refund_calc.RETURN_WINDOW_DAYS

    # Honest day count on a stale order: refused on policy, not on the
    # mismatch -- a different gate, and it must still stop the money.
    r = gates.issue_refund({"order_id": 4712, "reason": "damaged",
                            "amount_usd": 0.00, "days_since_delivery": actual})
    assert r["blocked"] is True
    assert r["policy"]["reason_code"] == "OUTSIDE_RETURN_WINDOW"


def test_day_count_tolerates_clock_skew():
    """One day of slack absorbs midnight and timezones; it does not
    excuse a guess."""
    ok = dict(REFUND_4711, days_since_delivery=DAYS_4711 + gates.DAY_TOLERANCE)
    assert gates.issue_refund(ok)["blocked"] is False

    off = dict(REFUND_4711,
               days_since_delivery=DAYS_4711 + gates.DAY_TOLERANCE + 1)
    assert gates.issue_refund(off)["blocked"] is True


# ------------------------------------------------------- idempotency
def test_key_is_semantic_not_random():
    a = idempotency_key(4711, "damaged", "172.99")
    b = idempotency_key(4711, "DAMAGED ", "172.99")   # same action
    c = idempotency_key(4711, "damaged", "172.98")    # different amount
    assert a == b and a != c


def test_retry_storm_issues_exactly_one_refund():
    first = gates.issue_refund(REFUND_4711)
    assert first["duplicate"] is False and first["state"] == "COMMITTED"
    for _ in range(5):
        again = gates.issue_refund(REFUND_4711)
        assert again["duplicate"] is True
        assert again["idempotency_key"] == first["idempotency_key"]
    assert len(RefundLedger().all_intents()) == 1


# ---------------------------------------------------------- rollback
def test_failure_mid_flight_is_reversed(monkeypatch):
    monkeypatch.setattr(gates, "INJECT_COMMIT_FAULT", True)
    r = gates.issue_refund(REFUND_4711)
    assert r["blocked"] is True and r["rolled_back"] is True

    intents = RefundLedger().all_intents()
    assert len(intents) == 1 and intents[0]["state"] == "ROLLED_BACK"

    order = gates._load_order(4711)
    assert order["status"] == "delivered"   # effect reversed, not left dirty


# =====================================================================
# harness client -- the memory measurement, offline
#
# The isolation claim in test_trajectory.py is a COUNT read off the
# episode (ep.memory_facts). That count used to be computed in
# demo_run.py, where no eval could see it, and the eval reported
# "unavailable" instead. It lives in run_episode now, and it belongs in
# tier 1 because every way of breaking it fails SILENTLY IN THE
# PASSING DIRECTION: a count that never arrives, or a namespace typo,
# both look like "0 facts retrieved" -- which is exactly what the
# isolation test wants to see. A security gate that passes for the wrong
# reason is worse than one that fails.
#
# No model and no network: a scripted event stream stands in for the
# harness, so invoke -> gate -> continue runs as ordinary software.
# =====================================================================
MEMORY_ARN = ("arn:aws:bedrock-agentcore:us-east-1:111122223333:"
              "memory/demo-mem-abc123")
HARNESS_ARN = ("arn:aws:bedrock-agentcore:us-east-1:111122223333:"
               "runtime/hz-test")
ACTOR = "customer:CUST-100:0101-0000"


class _FakeControl:
    """bedrock-agentcore-control: resolves the store behind the harness."""

    def __init__(self):
        self.get_harness_calls = 0

    def get_harness(self, harnessId):          # noqa: N803 - boto3 casing
        self.get_harness_calls += 1
        return {"harness": {"memory": {
            "agentCoreMemoryConfiguration": {"arn": MEMORY_ARN}}}}

    def get_memory(self, memoryId):            # noqa: N803 - boto3 casing
        return {"memory": {"strategies": [
            {"strategyId": "summary-1", "type": "SUMMARIZATION"},
            {"strategyId": "semantic-1", "type": "SEMANTIC"}]}}


class _FakeAgentCore:
    """bedrock-agentcore data plane: scripted streams, recorded calls."""

    def __init__(self, streams, facts=0, memory_error=None):
        self._streams = list(streams)
        self.facts = facts                     # what the store will return
        self.memory_error = memory_error
        self.log = []                          # every call, in order
        self.commands = []
        self.memory_queries = []

    def invoke_harness(self, **kw):
        self.log.append("invoke")
        return {"stream": self._streams.pop(0)}

    def list_memory_records(self, **kw):
        self.log.append("memory")
        self.memory_queries.append(kw)
        if self.memory_error:
            raise self.memory_error
        return {"memoryRecordSummaries":
                [{"memoryRecordId": f"rec-{i}"} for i in range(self.facts)]}


def _text(s, idx=0):
    return {"contentBlockDelta": {"contentBlockIndex": idx,
                                  "delta": {"text": s}}}


def _tool_chunk(s, idx):
    return {"contentBlockDelta": {"contentBlockIndex": idx,
                                  "delta": {"toolUse": {"input": s}}}}


def _tool_start(name, idx, tool_use_id):
    return {"contentBlockStart": {"contentBlockIndex": idx, "start": {
        "toolUse": {"name": name, "toolUseId": tool_use_id}}}}


_STOP = {"messageStop": {"stopReason": "end_turn"}}
_USAGE = {"metadata": {"usage": {"inputTokens": 900, "outputTokens": 120}}}


def _script():
    """One episode: a server-side tool, a gated SELECT, a final answer.

    Two details are deliberate. The gated tool's input arrives in TWO
    deltas, because streamed toolUse is chunked and per-block
    accumulation is what stops two parallel calls concatenating into one
    unparseable blob. And the last stream carries TWO messages, which is
    the case the streamed-line flush exists for.
    """
    return [
        [_text("Looking up order 4711."),        # no trailing newline
         _tool_start("shell", 1, "tu-shell"),    # harness side: observed
         _tool_start("run_sql", 2, "tu-sql"),    # our side: gated
         _tool_chunk('{"query": "SELECT status FROM ', 2),
         _tool_chunk('orders WHERE order_id=4711"}', 2),
         _STOP, _USAGE],
        [_text("Order 4711 is delivered."), _STOP,
         _text("Anything else?"), _STOP, _USAGE],
    ]


@pytest.fixture
def harness(monkeypatch):
    """run_episode with the network removed and nothing else changed."""
    fake = _FakeAgentCore(_script())
    fake.control = _FakeControl()
    monkeypatch.setattr(harness_client, "HARNESS_ARN", HARNESS_ARN)
    # Resolved once per PROCESS, so a stale id would leak across tests.
    monkeypatch.setattr(harness_client, "_MEMORY_ID", None)

    def fake_shell(session_id, script):
        fake.log.append("command")
        fake.commands.append(script)

    monkeypatch.setattr(harness_client, "_shell_exec", fake_shell)
    monkeypatch.setattr(
        harness_client.boto3, "client",
        lambda service, **kw: (fake.control if service.endswith("control")
                               else fake))
    monkeypatch.chdir(ROOT)          # seed_calculator reads refund_calc.py
    return fake


def _run(verbose=False):
    return harness_client.run_episode("Order #4711 arrived damaged.",
                                      actor_id=ACTOR, verbose=verbose)


def test_episode_has_no_fact_count_until_one_is_measured():
    """None and 0 are different claims and must not be conflated.

    None means nobody measured; 0 means the store was asked and returned
    nothing, which IS the isolation result. Defaulting to 0 would let an
    unmeasured episode assert isolation it never checked.
    """
    assert harness_client.Episode("t", ACTOR).memory_facts is None


def test_run_episode_records_the_retrieved_fact_count(harness):
    harness.facts = 3
    ep = _run()
    assert ep.memory_facts == 3
    # Measured AFTER the loop, not before: the episode's own events are
    # part of what the store has by then.
    assert harness.log[-1] == "memory", harness.log


def test_zero_facts_is_a_measurement_not_a_missing_one(harness):
    """The exact contract tier 2 reads. This is the assertion that would
    have caught 'Retrieved-fact count unavailable' before a live sweep
    spent twenty invocations discovering it."""
    from test_trajectory import _memory_fact_count

    ep = _run()                                  # harness.facts == 0
    assert ep.memory_facts == 0
    assert _memory_fact_count(ep) == 0           # not None, and not falsy-skipped


def test_unqueryable_memory_store_does_not_discard_the_episode(harness):
    """A store that cannot be read is a MISSING MEASUREMENT, not a failed
    episode -- raising here would throw away a completed trajectory over
    a telemetry read."""
    harness.memory_error = RuntimeError("AccessDeniedException")
    ep = _run()
    assert ep.memory_facts is None
    assert "delivered" in ep.text
    assert ep.calls == ["shell", "run_sql"]      # trajectory intact


def test_fact_count_is_namespaced_by_actor(harness):
    """The actorId boundary IS this string.

    A typo in it returns 0 records for everybody, and 0 is what the leak
    test is looking for -- so the isolation eval would pass while
    measuring nothing at all. Pin it here, where it costs nothing.
    """
    harness_client.count_memory_facts("customer:CUST-200:0101-0000")
    q = harness.memory_queries[-1]
    assert q["namespace"] == (
        "/strategies/semantic-1/actors/customer:CUST-200:0101-0000/")
    assert q["memoryId"] == "demo-mem-abc123"    # id, not the full ARN


def test_memory_store_is_resolved_once(harness):
    """Three counts, one control-plane lookup. wait_for_memory polls this
    every 10s for up to 3 minutes; re-resolving each time is 18 needless
    get_harness calls per wait."""
    for _ in range(3):
        harness_client.count_memory_facts(ACTOR)
    assert harness.control.get_harness_calls == 1


def test_episode_record_carries_the_fact_count(harness, tmp_path):
    """The saved record is the audit artefact. Isolation evidence that
    lives only in stdout is not evidence anyone can re-examine."""
    harness.facts = 2
    ep = _run()
    with open(ep.save(str(tmp_path))) as f:
        saved = json.load(f)
    assert saved["memory_facts"] == 2
    assert saved["actor_id"] == ACTOR


def test_the_policy_engine_is_shipped_before_the_model_reasons(harness):
    """Our calculator lands on the microVM first, without the model in
    the loop. A sandbox does not make arithmetic deterministic if the
    model writes the arithmetic."""
    _run()
    assert harness.log[0] == "command"           # before any invoke
    body = harness.commands[0].split("<<'B64'\n")[1].split("\nB64")[0]
    assert "def compute_refund" in base64.b64decode(body).decode()


def test_streamed_messages_do_not_run_together(harness, capsys):
    """Two turns must not render as one line.

    Unfixed, this prints "Order 4711 is delivered.Anything else?" -- which
    reads on camera as a rendering bug rather than as two messages.
    """
    _run(verbose=True)
    out = capsys.readouterr().out
    assert "Order 4711 is delivered.\nAnything else?" in out
    assert "delivered.Anything" not in out


def test_the_gate_still_fires_inside_the_loop(harness):
    """The offline harness must not quietly bypass the gate: run_sql is
    executed by OUR process, and the trace records both sides."""
    ep = _run()
    sql = ep.gated_calls[-1]
    assert sql["tool"] == "run_sql" and sql["side"] == "client"
    assert sql["result"]["blocked"] is False and sql["result"]["row_count"] == 1
    assert [t["side"] for t in ep.trace] == ["harness", "client"]
