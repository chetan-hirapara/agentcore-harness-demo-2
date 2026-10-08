"""The harness client: invoke -> stream -> gate -> continue.

```mermaid
sequenceDiagram
    participant App as Our process
    participant H as AgentCore harness
    participant M as Model
    participant S as Sandbox (microVM)
    App->>S: seed OUR calculator (no model involved)
    App->>H: invoke_harness(task, actorId)
    loop until the model stops asking for tools
        H->>M: reason (memory loaded first)
        M-->>H: tool call
        alt server-side tool (code interpreter)
            H->>S: run it
            S-->>H: output
        else gated tool (run_sql / issue_refund)
            H-->>App: stopReason=tool_use  (the loop PAUSES)
            App->>App: GATE decides: allow or refuse
            App->>H: toolResult (continue the loop)
        end
    end
    H-->>App: final answer
    App->>App: save the Episode, measure memory
```

The pause is the point. While the harness waits for us, nothing the model
says can reach the database or the ledger except through code we wrote.
"""
import json
import logging
from collections.abc import Callable

from harness_demo.agent.episode import Episode
from harness_demo.agent.memory import MemoryStore
from harness_demo.agent.observer import EpisodeObserver
from harness_demo.agent.prompt import build_system_prompt
from harness_demo.agent.sandbox import CalculatorSandbox
from harness_demo.agent.stream import drain
from harness_demo.agent.tools import build_tools
from harness_demo.config import CALC_REMOTE_PATH, Settings, resolve_harness_arn
from harness_demo.db import SupportDb
from harness_demo.guardrails.budget import ExecutionBudget
from harness_demo.guardrails.toolbox import GatedToolbox

log = logging.getLogger(__name__)


class HarnessClient:
    def __init__(self, settings: Settings | None = None, *,
                 db: SupportDb | None = None,
                 budget: ExecutionBudget | None = None,
                 data_client=None,
                 memory: MemoryStore | None = None,
                 sandbox: CalculatorSandbox | None = None):
        self.settings = settings or Settings()
        self.db = db or SupportDb(self.settings.db_path)
        self.budget = budget or ExecutionBudget()
        self._data = data_client
        self._arn: str | None = None
        arn: Callable[[], str] = lambda: self.harness_arn   # noqa: E731
        self.memory = memory or MemoryStore(self.settings, arn)
        self.sandbox = sandbox or CalculatorSandbox(self.settings, arn)

    @property
    def harness_arn(self) -> str:
        """Resolved on first use, so building a client needs no network."""
        if self._arn is None:
            self._arn = resolve_harness_arn(self.settings)
        return self._arn

    @property
    def data(self):
        self._data = self._data or self.settings.client("bedrock-agentcore")
        return self._data

    def run_episode(self, task: str, *, actor_id: str, customer_id: str,
                    observer: EpisodeObserver | None = None,
                    session_id: str | None = None,
                    inject_commit_fault: bool = False) -> Episode:
        """Work one ticket.

        actor_id     memory boundary. Reuse it across sessions to exercise
                     long-term memory.
        customer_id  data boundary, bound by the calling application. It
                     scopes every SQL row and every refund.
        session_id   pass an existing one to continue a conversation.
        """
        observer = observer or EpisodeObserver()
        ep = Episode(task, actor_id, customer_id, self.budget)
        if session_id:
            ep.session_id = session_id
        toolbox = GatedToolbox(self.db, customer_id,
                               inject_commit_fault=inject_commit_fault)

        self.sandbox.seed(ep.session_id)

        kwargs = dict(
            harnessArn=self.harness_arn,
            runtimeSessionId=ep.session_id,
            actorId=actor_id,
            systemPrompt=build_system_prompt(self.db, CALC_REMOTE_PATH),
            tools=build_tools(CALC_REMOTE_PATH),
            maxIterations=self.budget.max_iterations,
        )
        log.info("episode %s start actor=%s customer=%s",
                 ep.session_id[:8], actor_id, customer_id)

        response = self.data.invoke_harness(
            **kwargs, messages=[{"role": "user", "content": [{"text": task}]}])

        while True:
            pending = drain(response["stream"], ep, toolbox.names, observer)
            if not pending:
                break
            response = self._answer_gated_calls(kwargs, ep, toolbox, pending,
                                                observer)

        self._measure_memory(ep, observer)
        log.info("episode %s done: %d calls, %d blocked, $%.3f",
                 ep.session_id[:8], len(ep.trace), len(ep.blocked_events),
                 ep.cost_usd)
        observer.on_episode_end(ep)
        return ep

    def _answer_gated_calls(self, kwargs, ep, toolbox, pending, observer):
        """THE GATE FIRES HERE, IN OUR PROCESS.

        One turn can carry several calls. Every one is gated, and every
        toolUse must come back with its matching toolResult -- a stored
        toolUse with no result corrupts the session.
        """
        tool_uses, tool_results = [], []
        for call in pending:
            ep.meter.record_tool_call()
            result = toolbox.dispatch(call["name"], call["input"])
            ep.trace.append({"tool": call["name"], "side": "client",
                             "input": call["input"], "result": result})
            observer.on_gate(call["name"], result)
            tool_uses.append({"toolUse": {
                "toolUseId": call["toolUseId"], "name": call["name"],
                "input": call["input"]}})
            tool_results.append({"toolResult": {
                "toolUseId": call["toolUseId"],
                "content": [{"text": json.dumps(result, default=str)}],
                "status": "success"}})
        return self.data.invoke_harness(**kwargs, messages=[
            {"role": "assistant", "content": tool_uses},
            {"role": "user", "content": tool_results},
        ])

    def _measure_memory(self, ep: Episode, observer: EpisodeObserver) -> None:
        """Record the retrieval-layer fact count ON the episode, so the
        isolation evidence travels with the audit record instead of only
        being printed.

        Never fatal: a store that cannot be queried is a missing
        measurement (None), not a failed episode.
        """
        try:
            ep.memory_facts = self.memory.count_facts(ep.actor_id)
        except Exception as e:                              # noqa: BLE001
            log.warning("memory fact count unavailable: %s", e)
            observer.on_note(f"[memory] fact count unavailable: "
                             f"{type(e).__name__}: {e}")
