"""The harness client: invoke -> stream -> gate -> continue.

FIVE TOOLS, TWO SIDES OF A TRUST BOUNDARY.

  Runs on AWS's microVM (we OBSERVE it in the trace):
    - agentcore_code_interpreter : executes our shipped calculator
    - managed memory             : per-actor history, loaded before reasoning

  Runs in OUR process (we INTERCEPT and gate it):
    - run_sql       (inline_function, L1 read-only)
    - issue_refund  (inline_function, L3 external write)

That split is the architectural decision, not an accident. Anything
that can move money or leak data is an inline function, because an
inline function pauses the loop and hands control back to code we own.
Everything else runs server-side where AWS handles isolation.

Memory is scoped by actorId. Two customers share nothing, and the
isolation is asserted in evals/test_trajectory.py.
"""
import json
import os
import sqlite3
import time
import uuid
from datetime import date

import boto3

from budget import ExecutionBudget, BudgetMeter, EpisodeAborted
from gates import run_sql, issue_refund, DB_PATH

REGION = os.environ.get("AWS_REGION", "us-east-1")
HARNESS_ARN = os.environ.get("HARNESS_ARN", "")
CALC_REMOTE_PATH = "/tmp/refund_calc.py"

# Tools we gate ourselves. The harness pauses for these.
GATED_TOOLS = {"run_sql": run_sql, "issue_refund": issue_refund}

# We DECLARE the code interpreter as "code_interpreter"; the harness
# reports the sub-tool the model actually invoked, which is "shell".
# Asserting on the declared name therefore never matches a real trace --
# it fails open in evals and reads as "the calculator never ran". Match
# on the family instead, and keep the raw name in the trace so the
# audit record still says what happened.
CODE_INTERPRETER_TOOLS = {"code_interpreter", "shell",
                          "execute_code", "read_files", "write_files"}

TOOLS = [
    {"type": "inline_function", "name": "run_sql",
     "config": {"inlineFunction": {
         "description": ("Run a READ-ONLY SQL SELECT against the support "
                         "database (customers, orders, refund_intents). "
                         "Mutations are blocked."),
         "inputSchema": {
             "type": "object",
             "properties": {"query": {"type": "string"},
                            "max_rows": {"type": "integer"}},
             "required": ["query"]}}}},
    {"type": "inline_function", "name": "issue_refund",
     "config": {"inlineFunction": {
         "description": ("Issue a refund. amount_usd MUST come from "
                         f"running {CALC_REMOTE_PATH} in the code "
                         "interpreter -- never from your own arithmetic. "
                         "The request is independently recomputed and "
                         "rejected on mismatch."),
         "inputSchema": {
             "type": "object",
             "properties": {
                 "order_id": {"type": "integer"},
                 "reason": {"type": "string",
                            "enum": ["damaged", "defective",
                                     "wrong_item", "changed_mind"]},
                 "amount_usd": {"type": "number"},
                 "days_since_delivery": {"type": "integer"}},
             "required": ["order_id", "reason", "amount_usd",
                          "days_since_delivery"]}}}},
    {"type": "agentcore_code_interpreter", "name": "code_interpreter"},
]

def _schema_card(db_path: str = DB_PATH) -> str:
    """Read the real column names out of the database.

    Hand-copying DDL into a prompt is how the prompt drifts from the
    table. Derive it and the two cannot disagree.

    Without this the agent guesses -- item_price for amount_usd,
    delivered_date for delivered_on -- then reaches for PRAGMA to
    discover the schema, which the read-only gate blocks because PRAGMA
    is not a SELECT. Those blocks are noise: they demonstrate a naming
    mismatch, not the money-safety property the gate exists to show.
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return ""                      # not seeded yet; run gates.py
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return "\n".join(
            "  {}({})".format(t, ", ".join(
                f"{c[1]} {c[2]}" for c in conn.execute(f"PRAGMA table_info({t})")))
            for t in tables)
    except sqlite3.Error:
        return ""
    finally:
        conn.close()


SYSTEM_PROMPT = [{"text": (
    "You are a returns and refunds support agent.\n"
    "Process: (1) look up the order and customer with run_sql; "
    "(2) compute the refund by running "
    f"`python {CALC_REMOTE_PATH} --item-price X --shipping Y --days N "
    "--reason R --tier T` in the code interpreter -- never calculate "
    "refund amounts yourself; (3) call issue_refund with the total the "
    "calculator printed.\n"
    "If a tool is blocked, read the structured error, correct, and "
    "continue. State plainly anything you could not do.\n\n"
    "The support database has exactly these tables and columns. Use "
    "these table and column names verbatim -- do not invent column "
    "names, and do not try to discover them: run_sql permits SELECT "
    "only, so PRAGMA and other introspection are blocked.\n"
    f"{_schema_card()}\n"
    f"Today's date is {date.today().isoformat()}. Use it -- do not "
    "assume a date from your training. orders.delivered_on is "
    "'YYYY-MM-DD'; days_since_delivery is the number of days from it to "
    "today, and the gate recomputes it from the row before issuing any "
    "refund, so a guess is refused rather than quietly accepted. "
    "customers.tier is one of gold, "
    "standard, platinum. refund_intents is the committed-refund ledger "
    "and has no reason column -- join it to orders on order_id.\n\n"
    "The caller is ALREADY AUTHENTICATED. The session is bound to one "
    "customer by the calling application, and any memory you carry from "
    "earlier sessions belongs to that same customer. So do not ask them "
    "to identify themselves -- if remembered context answers the "
    "question, answer from it."
)}]


class Episode:
    """An auditable record of one run: full trajectory, both sides of
    the trust boundary, stop reasons, cost."""

    def __init__(self, task: str, actor_id: str):
        self.task = task
        self.actor_id = actor_id
        self.session_id = str(uuid.uuid4())    # >= 33 chars required
        self.trace: list[dict] = []
        self.text = ""
        self.meter = BudgetMeter(ExecutionBudget())
        self.started = time.time()

    @property
    def calls(self) -> list[str]:
        return [t["tool"] for t in self.trace]

    @property
    def gated_calls(self) -> list[dict]:
        return [t for t in self.trace if t["side"] == "client"]

    @property
    def blocked_events(self) -> list[dict]:
        return [t for t in self.gated_calls if t["result"].get("blocked")]

    @property
    def refunds(self) -> list[dict]:
        return [t for t in self.gated_calls if t["tool"] == "issue_refund"
                and not t["result"].get("blocked")]

    def first_index(self, *names: str) -> int | None:
        """Index in `calls` of the first call to any of `names`, or None.

        Used instead of `calls.index(...)` so the ordering invariant
        ("money is never moved before it is computed") can be stated
        over a FAMILY of tool names -- see CODE_INTERPRETER_TOOLS -- and
        returns None rather than raising when the tool never ran.
        """
        for i, call in enumerate(self.calls):
            if call in names:
                return i
        return None

    @property
    def code_interpreter_index(self) -> int | None:
        return self.first_index(*CODE_INTERPRETER_TOOLS)

    @property
    def cost_usd(self) -> float:
        return self.meter.cost_usd

    def to_record(self) -> dict:
        return {"task": self.task, "actor_id": self.actor_id,
                "session_id": self.session_id, "trace": self.trace,
                "stop_reasons": self.meter.stop_reasons,
                "cost_usd": round(self.cost_usd, 4),
                "duration_s": round(time.time() - self.started, 1),
                "final_text": self.text}

    def save(self, directory: str = "episodes") -> str:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{self.session_id}.json")
        with open(path, "w") as f:
            json.dump(self.to_record(), f, indent=2)
        return path


def seed_calculator(client, session_id: str) -> None:
    """Put OUR calculator on the microVM before the agent reasons.

    Lab 07: InvokeAgentRuntimeCommand runs on the VM without the model
    in the loop. This is the whole point -- the policy engine is
    shipped, not model-generated. A sandbox does not make arithmetic
    deterministic if the model writes the arithmetic.
    """
    source = open("refund_calc.py").read()
    heredoc = f"cat > {CALC_REMOTE_PATH} <<'PYEOF'\n{source}\nPYEOF"
    client.invoke_agent_runtime_command(
        agentRuntimeArn=HARNESS_ARN,
        runtimeSessionId=session_id,
        body={"command": heredoc},
    )


def wait_for_memory(actor_id: str, timeout_s: int = 180) -> bool:
    """Block until managed memory has EXTRACTED facts for this actor.

    Writing events is synchronous; extracting them into semantic records
    is not. Ask the agent "what did we refund last time" ten seconds
    after the refund and it answers "who are you?" -- the events are
    there, the facts are not built yet. That reads on camera as broken
    memory, so wait for the extraction rather than for the clock.
    """
    ctl = boto3.client("bedrock-agentcore-control", region_name=REGION)
    data = boto3.client("bedrock-agentcore", region_name=REGION)
    mem = ctl.get_harness(harnessId=HARNESS_ARN.split("/")[-1])["harness"]
    mem_arn = mem["memory"]["managedMemoryConfiguration"]["arn"]
    mem_id, namespace = mem_arn.split("/")[-1], f"/actors/{actor_id}/facts/"

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        found = data.list_memory_records(
            memoryId=mem_id, namespace=namespace, maxResults=5
        )["memoryRecordSummaries"]
        if found:
            print(f"  [memory] {len(found)} fact(s) extracted for {actor_id}")
            return True
        time.sleep(10)
    print(f"  [memory] no facts after {timeout_s}s -- recall may be cold")
    return False


def _drain(stream, ep: Episode):
    """Read one event stream. Records harness-side tool use as observed
    trace entries; returns the pending calls for tools WE gate, in the
    order the model emitted them.

    The model can emit several tool calls in ONE turn (parallel tool
    use). Input deltas are only distinguishable by contentBlockIndex, so
    accumulate per block -- a single shared buffer concatenates two JSON
    objects and json.loads dies with "Extra data".
    """
    blocks: dict[int, dict] = {}      # contentBlockIndex -> gated call
    pending: list[dict] = []
    last_idx = 0
    for event in stream:
        if "contentBlockStart" in event:
            block = event["contentBlockStart"]
            last_idx = block.get("contentBlockIndex", last_idx + 1)
            start = block.get("start", {})
            if "toolUse" in start:
                name = start["toolUse"]["name"]
                if name in GATED_TOOLS:
                    call = {"toolUseId": start["toolUse"]["toolUseId"],
                            "name": name, "raw": ""}
                    blocks[last_idx] = call
                    pending.append(call)
                else:
                    # Server-side tool: we observe, AWS executes.
                    ep.trace.append({"tool": name, "side": "harness",
                                     "input": None, "result": {}})
        elif "contentBlockDelta" in event:
            block = event["contentBlockDelta"]
            idx = block.get("contentBlockIndex", last_idx)
            delta = block.get("delta", {})
            if "text" in delta:
                ep.text += delta["text"]
                print(delta["text"], end="", flush=True)
            if "toolUse" in delta and idx in blocks:
                blocks[idx]["raw"] += delta["toolUse"].get("input", "")
        elif "messageStop" in event:
            ep.meter.record_stop_reason(
                event["messageStop"].get("stopReason", "unknown"))
        elif "metadata" in event:
            ep.meter.record_usage(event["metadata"].get("usage", {}))
        elif "runtimeClientError" in event:
            raise EpisodeAborted(
                f"RUNTIME_ERROR: {event['runtimeClientError']['message']}")
    for call in pending:
        raw = call.pop("raw").strip()
        call["input"] = json.loads(raw) if raw else {}
    return pending


def run_episode(task: str, actor_id: str, session_id: str | None = None,
                verbose: bool = True) -> Episode:
    """One ticket. Pass an existing session_id to continue a
    conversation; pass the same actor_id across sessions to exercise
    long-term memory."""
    client = boto3.client("bedrock-agentcore", region_name=REGION)
    ep = Episode(task, actor_id)
    if session_id:
        ep.session_id = session_id

    seed_calculator(client, ep.session_id)

    kwargs = dict(
        harnessArn=HARNESS_ARN,
        runtimeSessionId=ep.session_id,
        actorId=actor_id,                 # memory isolation boundary
        systemPrompt=SYSTEM_PROMPT,
        tools=TOOLS,
        maxIterations=ep.meter.budget.max_iterations,
    )

    response = client.invoke_harness(
        **kwargs, messages=[{"role": "user", "content": [{"text": task}]}])

    while True:
        pending = _drain(response["stream"], ep)
        if not pending:
            break

        # ---- THE GATE FIRES HERE, IN OUR PROCESS ----
        # One turn can carry several calls; every one gets gated, and
        # every toolUse must come back with its matching toolResult.
        tool_uses, tool_results = [], []
        for call in pending:
            ep.meter.record_tool_call()
            result = GATED_TOOLS[call["name"]](call["input"])
            ep.trace.append({"tool": call["name"], "side": "client",
                             "input": call["input"], "result": result})
            if verbose:
                tag = "BLOCKED" if result.get("blocked") else (
                    "DUPLICATE" if result.get("duplicate") else "ok")
                print(f"\n  [gate] {call['name']} -> {tag}"
                      f"{': ' + result['reason'] if result.get('reason') else ''}")
            tool_uses.append({"toolUse": {
                "toolUseId": call["toolUseId"], "name": call["name"],
                "input": call["input"]}})
            tool_results.append({"toolResult": {
                "toolUseId": call["toolUseId"],
                "content": [{"text": json.dumps(result, default=str)}],
                "status": "success"}})

        # The harness requires BOTH messages: the assistant toolUse and
        # our toolResult. A stored toolUse with no result corrupts the
        # session.
        response = client.invoke_harness(**kwargs, messages=[
            {"role": "assistant", "content": tool_uses},
            {"role": "user", "content": tool_results},
        ])

    if verbose:
        print(f"\n--- episode: {len(ep.trace)} tool call(s) "
              f"({len(ep.gated_calls)} gated), "
              f"{len(ep.blocked_events)} blocked, ~${ep.cost_usd:.3f}, "
              f"stops={ep.meter.stop_reasons}")
    return ep
