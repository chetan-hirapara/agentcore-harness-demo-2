"""An Episode is the audit record of one ticket, worked end to end.

It captures BOTH sides of the trust boundary in one ordered trace:

  side="harness"  tools AWS ran for us (code interpreter); we only observe
  side="client"   tools WE ran behind a gate; we hold input AND result

plus the stop reasons, cost, and the memory fact count. Saved as JSON, it
is what you open when someone asks "what exactly did the agent do?".
"""
import json
import os
import time
import uuid

from harness_demo.agent.tools import CODE_INTERPRETER_TOOLS
from harness_demo.config import Settings
from harness_demo.guardrails.budget import BudgetMeter, ExecutionBudget


def describe_step(step: dict) -> str:
    """One trace entry as 'who ran it -> what happened'."""
    if step["side"] == "harness":
        return "AWS sandbox"
    result = step["result"]
    outcome = ("BLOCKED" if result.get("blocked")
               else "DUPLICATE" if result.get("duplicate")
               else result.get("state") or "ok")
    return f"our gate -> {outcome}"


def timeline(trace: list[dict]) -> list[str]:
    """Numbered, human-readable lines for a trace. Works on saved records too."""
    return [f"{i}. {step['tool']:<17} {describe_step(step)}"
            for i, step in enumerate(trace, 1)]


class Episode:
    def __init__(self, task: str, actor_id: str, customer_id: str,
                 budget: ExecutionBudget | None = None):
        self.task = task
        self.actor_id = actor_id           # memory boundary
        self.customer_id = customer_id     # data boundary
        self.session_id = str(uuid.uuid4())    # >= 33 chars required
        self.trace: list[dict] = []
        self.text = ""
        self.meter = BudgetMeter(budget or ExecutionBudget())
        self.started = time.time()
        # None means "not measured"; 0 is the isolation claim itself.
        self.memory_facts: int | None = None

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

        Lets the ordering invariant ("money is never moved before it is
        computed") be stated over a FAMILY of tool names, and returns None
        instead of raising when the tool never ran.
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
                "customer_id": self.customer_id,
                "session_id": self.session_id, "trace": self.trace,
                "memory_facts": self.memory_facts,
                "stop_reasons": self.meter.stop_reasons,
                "cost_usd": round(self.cost_usd, 4),
                "duration_s": round(time.time() - self.started, 1),
                "final_text": self.text}

    def save(self, directory: str | None = None) -> str:
        directory = directory or Settings().episodes_dir
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{self.session_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_record(), f, indent=2, default=str)
        return path
