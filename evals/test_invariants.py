"""Tier 1 evals: deterministic, offline, every commit.

No model calls. These test the HARNESS, and the harness is ordinary
software -- which is the point. The expensive non-deterministic tests
live in test_trajectory.py and run on a schedule, not on every push.

Run:  python -m pytest evals/test_invariants.py -v
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

from harness_demo.agent.client import HarnessClient
from harness_demo.agent.episode import Episode
from harness_demo.agent.memory import MemoryStore
from harness_demo.agent.observer import ConsoleObserver
from harness_demo.agent.sandbox import CalculatorSandbox
from harness_demo.config import ConfigError, Settings, resolve_harness_arn
from harness_demo.db import DELIVERED_DAYS_AGO, SupportDb
from harness_demo.guardrails import refund_gate
from harness_demo.guardrails.ledger import RefundLedger, idempotency_key
from harness_demo.guardrails.toolbox import GatedToolbox
from harness_demo.policy import refund_calc


@pytest.fixture
def db(tmp_path):
    d = SupportDb(str(tmp_path / "test.db"))
    d.seed()
    return d


@pytest.fixture
def asha(db):
    """Gated tools for CUST-100, who owns order 4711."""
    return GatedToolbox(db, "CUST-100")


@pytest.fixture
def diego(db):
    """Gated tools for CUST-200, who owns order 4712."""
    return GatedToolbox(db, "CUST-200")


# Derived, never hardcoded: order 4711 is $149.00 + $9.99 shipping,
# gold tier, damaged. The day count comes from the seed, which sets
# delivery dates relative to today -- a literal here would drift out of
# the return window and fail for the wrong reason a month from now.
DAYS_4711 = DELIVERED_DAYS_AGO[4711]
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
def test_mutations_are_blocked(asha, query):
    assert asha.run_sql({"query": query})["blocked"] is True


def test_stacked_statement_blocked_by_second_layer(asha):
    # Passes the regex, dies on the read-only single-statement connection.
    r = asha.run_sql({"query": "SELECT 1; UPDATE orders SET status='x'"})
    assert r["blocked"] is True


def test_select_is_allowed(asha):
    r = asha.run_sql({"query": "SELECT * FROM orders WHERE order_id=4711"})
    assert r["blocked"] is False and r["row_count"] == 1


# ------------------------------------------- one customer's rows only
def test_a_customer_sees_only_their_own_orders(asha, diego):
    mine = asha.run_sql({"query": "SELECT order_id FROM orders ORDER BY 1"})
    theirs = diego.run_sql({"query": "SELECT order_id FROM orders ORDER BY 1"})
    assert mine["rows"] == [[4711], [4713]]
    assert theirs["rows"] == [[4712], [4801]]


def test_another_customers_order_is_invisible_by_id(diego):
    r = diego.run_sql({"query": "SELECT * FROM orders WHERE order_id=4711"})
    assert r["blocked"] is False and r["row_count"] == 0


def test_refund_ledger_is_scoped_to_the_customer(asha, diego):
    asha.issue_refund(REFUND_4711)
    assert asha.run_sql(
        {"query": "SELECT * FROM refund_intents"})["row_count"] == 1
    # Gap #5: this used to return CUST-100's refund to CUST-200.
    assert diego.run_sql(
        {"query": "SELECT * FROM refund_intents"})["row_count"] == 0


def test_customers_table_is_scoped(asha):
    r = asha.run_sql({"query": "SELECT customer_id FROM customers"})
    assert r["rows"] == [["CUST-100"]]


def test_real_tables_cannot_be_read_around_the_views(asha):
    r = asha.run_sql({"query": "SELECT * FROM main.orders"})
    assert r["blocked"] is True


@pytest.mark.parametrize("query", [
    "SELECT name FROM sqlite_master",
    "SELECT sql FROM sqlite_temp_master",
    "SELECT name FROM temp.sqlite_master",
])
def test_schema_tables_are_not_readable(asha, query):
    assert asha.run_sql({"query": query})["blocked"] is True


def test_joins_across_scoped_views_still_work(asha):
    r = asha.run_sql({"query": "SELECT o.order_id, c.name FROM orders o "
                               "JOIN customers c USING(customer_id)"})
    assert r["rows"] == [[4711, "Asha Patel"], [4713, "Asha Patel"]]


@pytest.mark.parametrize("bad", ["", "CUST-1'; DROP TABLE orders; --",
                                 "a b", "x" * 65])
def test_a_malformed_customer_id_cannot_open_a_scoped_connection(db, bad):
    with pytest.raises(ValueError):
        db.connect_scoped(bad)


def test_refunding_someone_elses_order_is_refused(db, diego):
    """CUST-200's session asks for CUST-100's order. Same answer as a
    missing order, so ids cannot be probed."""
    r = diego.issue_refund(REFUND_4711)
    assert r["blocked"] is True and "not found" in r["reason"].lower()
    assert RefundLedger(db).all_intents() == []
    assert db.order_status(4711) == "delivered"


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


def test_agent_hallucinated_amount_is_refused(db, asha):
    """The single most important gate: the model reports a number, we
    recompute it independently, and disagreement stops the money."""
    bad = dict(REFUND_4711, amount_usd=999.00)
    r = asha.issue_refund(bad)
    assert r["blocked"] is True and "mismatch" in r["reason"].lower()
    assert RefundLedger(db).all_intents() == []      # nothing reserved
    # Both figures quantized to cents. This message goes on screen in the
    # demo, and "$999.0" next to "$172.03" reads as a defect in the gate.
    assert "$999.00" in r["reason"] and f"${CORRECT_TOTAL:.2f}" in r["reason"]


def test_refusal_message_quantizes_a_one_decimal_figure(asha):
    """999.00 in JSON reaches the gate as Decimal("999.0") -- trailing
    zeros are not preserved -- so f-stringing the raw Decimal prints
    "$999.0". A one-decimal figure catches a quantizer removed or applied
    to only one of the two amounts."""
    r = asha.issue_refund(dict(REFUND_4711, amount_usd=999.50))
    assert r["blocked"] is True
    assert "$999.50" in r["reason"], r["reason"]
    assert "$999.5," not in r["reason"] and "$999.5 " not in r["reason"]
    assert f"${CORRECT_TOTAL:.2f}" in r["reason"]


def test_understated_day_count_is_refused(db, diego):
    """Order 4712 was delivered well outside the return window. An agent
    that reports a small day count would otherwise be refunded on a
    total that reconciles perfectly -- the arithmetic was never the lie."""
    # The amount an in-window 4712 WOULD earn, so only the days are wrong.
    plausible = float(refund_calc.compute_refund(
        89.50, 5.99, 5, "damaged", "standard")["total"])
    r = diego.issue_refund({"order_id": 4712, "reason": "damaged",
                            "amount_usd": plausible,
                            "days_since_delivery": 5})
    assert r["blocked"] is True
    assert "day-count mismatch" in r["reason"].lower()
    assert RefundLedger(db).all_intents() == []      # nothing reserved


def test_day_count_is_derived_from_the_order_not_the_agent(db, diego):
    """Eligibility is decided by OUR reading of orders.delivered_on."""
    order = db.load_order(4712)
    actual = refund_calc.days_since_delivery(order["delivered_on"])
    assert actual == DELIVERED_DAYS_AGO[4712] > refund_calc.RETURN_WINDOW_DAYS

    # Honest day count on a stale order: refused on policy, not on the
    # mismatch -- a different gate, and it must still stop the money.
    r = diego.issue_refund({"order_id": 4712, "reason": "damaged",
                            "amount_usd": 0.00, "days_since_delivery": actual})
    assert r["blocked"] is True
    assert r["policy"]["reason_code"] == "OUTSIDE_RETURN_WINDOW"


def test_day_count_tolerates_clock_skew(asha):
    """One day of slack absorbs midnight and timezones; it does not
    excuse a guess."""
    tol = refund_gate.DAY_TOLERANCE
    ok = dict(REFUND_4711, days_since_delivery=DAYS_4711 + tol)
    assert asha.issue_refund(ok)["blocked"] is False

    off = dict(REFUND_4711, days_since_delivery=DAYS_4711 + tol + 1)
    assert asha.issue_refund(off)["blocked"] is True


# ------------------------------------------------------- idempotency
def test_key_is_semantic_not_random():
    a = idempotency_key(4711, "damaged", "172.99")
    b = idempotency_key(4711, "DAMAGED ", "172.99")   # same action
    c = idempotency_key(4711, "damaged", "172.98")    # different amount
    assert a == b and a != c


def test_retry_storm_issues_exactly_one_refund(db, asha):
    first = asha.issue_refund(REFUND_4711)
    assert first["duplicate"] is False and first["state"] == "COMMITTED"
    for _ in range(5):
        again = asha.issue_refund(REFUND_4711)
        assert again["duplicate"] is True
        assert again["idempotency_key"] == first["idempotency_key"]
    assert len(RefundLedger(db).all_intents()) == 1


# ---------------------------------------------------------- rollback
def test_failure_mid_flight_is_reversed(db):
    faulty = GatedToolbox(db, "CUST-100", inject_commit_fault=True)
    r = faulty.issue_refund(REFUND_4711)
    assert r["blocked"] is True and r["rolled_back"] is True

    intents = RefundLedger(db).all_intents()
    assert len(intents) == 1 and intents[0]["state"] == "ROLLED_BACK"

    assert db.load_order(4711)["status"] == "delivered"   # not left dirty


# ---------------------------------------------------------- config
def test_an_explicit_harness_arn_needs_no_network():
    arn = "arn:aws:bedrock-agentcore:us-east-1:111122223333:harness/h-1"
    assert resolve_harness_arn(Settings(harness_arn=arn), control=object()) == arn


def test_harness_arn_is_found_by_name_across_pages():
    class Control:
        pages = [{"harnesses": [{"harnessName": "other", "arn": "a"}],
                  "nextToken": "t"},
                 {"harnesses": [{"harnessName": "support_agent", "arn": "b"}]}]

        def list_harnesses(self, **kw):
            return self.pages.pop(0)

    s = Settings(harness_arn="", harness_name="support_agent")
    assert resolve_harness_arn(s, control=Control()) == "b"


def test_a_missing_harness_is_a_clear_error():
    class Control:
        def list_harnesses(self, **kw):
            return {"harnesses": []}

    with pytest.raises(ConfigError, match="support_agent"):
        resolve_harness_arn(Settings(harness_arn="", harness_name="support_agent"),
                            control=Control())


# =====================================================================
# harness client -- the memory measurement, offline
#
# The isolation claim in test_trajectory.py is a COUNT read off the
# episode (ep.memory_facts). Every way of breaking it fails SILENTLY IN
# THE PASSING DIRECTION: a count that never arrives, or a namespace typo,
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
        self.control = None
        self.client = None

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


class _RecordingSandbox(CalculatorSandbox):
    """The real seeding script, with the WebSocket swapped for a recorder."""

    def __init__(self, fake, settings):
        super().__init__(settings, lambda: HARNESS_ARN)
        self.fake = fake

    def _shell_exec(self, session_id, script):
        self.fake.log.append("command")
        self.fake.commands.append(script)


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
def harness(db, tmp_path):
    """HarnessClient with the network removed and nothing else changed."""
    fake = _FakeAgentCore(_script())
    fake.control = _FakeControl()
    settings = Settings(harness_arn=HARNESS_ARN, db_path=db.path,
                        episodes_dir=str(tmp_path / "episodes"))
    memory = MemoryStore(settings, lambda: HARNESS_ARN,
                         control=fake.control, data=fake)
    fake.client = HarnessClient(
        settings, db=db, data_client=fake, memory=memory,
        sandbox=_RecordingSandbox(fake, settings))
    return fake


def _run(harness, verbose=False, customer_id="CUST-100"):
    return harness.client.run_episode(
        "Order #4711 arrived damaged.", actor_id=ACTOR,
        customer_id=customer_id,
        observer=ConsoleObserver() if verbose else None)


def test_episode_has_no_fact_count_until_one_is_measured():
    """None and 0 are different claims and must not be conflated.

    None means nobody measured; 0 means the store was asked and returned
    nothing, which IS the isolation result. Defaulting to 0 would let an
    unmeasured episode assert isolation it never checked.
    """
    assert Episode("t", ACTOR, "CUST-100").memory_facts is None


def test_run_episode_records_the_retrieved_fact_count(harness):
    harness.facts = 3
    ep = _run(harness)
    assert ep.memory_facts == 3
    # Measured AFTER the loop, not before: the episode's own events are
    # part of what the store has by then.
    assert harness.log[-1] == "memory", harness.log


def test_zero_facts_is_a_measurement_not_a_missing_one(harness):
    """The exact contract tier 2 reads. This is the assertion that would
    have caught 'Retrieved-fact count unavailable' before a live sweep
    spent twenty invocations discovering it."""
    from test_trajectory import _memory_fact_count

    ep = _run(harness)                           # harness.facts == 0
    assert ep.memory_facts == 0
    assert _memory_fact_count(ep) == 0           # not None, and not falsy-skipped


def test_unqueryable_memory_store_does_not_discard_the_episode(harness):
    """A store that cannot be read is a MISSING MEASUREMENT, not a failed
    episode -- raising here would throw away a completed trajectory over
    a telemetry read."""
    harness.memory_error = RuntimeError("AccessDeniedException")
    ep = _run(harness)
    assert ep.memory_facts is None
    assert "delivered" in ep.text
    assert ep.calls == ["shell", "run_sql"]      # trajectory intact


def test_fact_count_is_namespaced_by_actor(harness):
    """The actorId boundary IS this string.

    A typo in it returns 0 records for everybody, and 0 is what the leak
    test is looking for -- so the isolation eval would pass while
    measuring nothing at all. Pin it here, where it costs nothing.
    """
    harness.client.memory.count_facts("customer:CUST-200:0101-0000")
    q = harness.memory_queries[-1]
    assert q["namespace"] == (
        "/strategies/semantic-1/actors/customer:CUST-200:0101-0000/")
    assert q["memoryId"] == "demo-mem-abc123"    # id, not the full ARN


def test_memory_store_is_resolved_once(harness):
    """Three counts, one control-plane lookup. wait_for_facts polls this
    every 10s; re-resolving each time is needless get_harness traffic."""
    for _ in range(3):
        harness.client.memory.count_facts(ACTOR)
    assert harness.control.get_harness_calls == 1


def test_episode_record_carries_the_fact_count(harness, tmp_path):
    """The saved record is the audit artefact. Isolation evidence that
    lives only in stdout is not evidence anyone can re-examine."""
    harness.facts = 2
    ep = _run(harness)
    with open(ep.save(str(tmp_path))) as f:
        saved = json.load(f)
    assert saved["memory_facts"] == 2
    assert saved["actor_id"] == ACTOR
    assert saved["customer_id"] == "CUST-100"


def test_the_policy_engine_is_shipped_before_the_model_reasons(harness):
    """Our calculator lands on the microVM first, without the model in
    the loop. A sandbox does not make arithmetic deterministic if the
    model writes the arithmetic."""
    _run(harness)
    assert harness.log[0] == "command"           # before any invoke
    body = harness.commands[0].split("<<'B64'\n")[1].split("\nB64")[0]
    assert "def compute_refund" in base64.b64decode(body).decode()


def test_streamed_messages_do_not_run_together(harness, capsys):
    """Two turns must not render as one line.

    Unfixed, this prints "Order 4711 is delivered.Anything else?" -- which
    reads on camera as a rendering bug rather than as two messages.
    """
    _run(harness, verbose=True)
    out = capsys.readouterr().out
    assert "Order 4711 is delivered.\nAnything else?" in out
    assert "delivered.Anything" not in out


def test_the_library_is_silent_without_an_observer(harness, capsys):
    _run(harness)
    assert capsys.readouterr().out == ""


def test_the_gate_still_fires_inside_the_loop(harness):
    """The offline harness must not quietly bypass the gate: run_sql is
    executed by OUR process, and the trace records both sides."""
    ep = _run(harness)
    sql = ep.gated_calls[-1]
    assert sql["tool"] == "run_sql" and sql["side"] == "client"
    assert sql["result"]["blocked"] is False and sql["result"]["row_count"] == 1
    assert [t["side"] for t in ep.trace] == ["harness", "client"]


def test_the_episode_runs_gates_as_the_session_customer(harness):
    """CUST-200's session cannot see order 4711, even asked by id."""
    ep = _run(harness, customer_id="CUST-200")
    assert ep.gated_calls[-1]["result"]["row_count"] == 0
