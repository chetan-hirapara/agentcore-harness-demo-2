"""Long-term memory, scoped by actorId.

THE LIFECYCLE (the part that surprises people):

  1. WRITE     every harness turn is stored as an event. Synchronous.
  2. EXTRACT   AWS turns events into semantic FACTS. Asynchronous: it can
               take minutes, and until it finishes recall comes back cold.
  3. RETRIEVE  facts are read from a namespace that contains the actorId:
                 /strategies/<semantic-strategy-id>/actors/<actorId>/
               Two actors share nothing, because the path IS the boundary.

We measure isolation at step 3. Asking the model what it remembers proves
nothing -- it will say "I don't retain memory between sessions" even when
it does -- so we COUNT the facts the store returns and compare.
"""
import logging
import time
from collections.abc import Callable

from harness_demo.config import Settings

log = logging.getLogger(__name__)


class MemoryStore:
    def __init__(self, settings: Settings, harness_arn: Callable[[], str], *,
                 control=None, data=None):
        self.settings = settings
        self._harness_arn = harness_arn
        self._control = control
        self._data = data
        self._memory_id: str | None = None
        self._strategy_id: str | None = None

    @property
    def control(self):
        self._control = self._control or self.settings.client(
            "bedrock-agentcore-control")
        return self._control

    @property
    def data(self):
        self._data = self._data or self.settings.client("bedrock-agentcore")
        return self._data

    def _resolve(self) -> None:
        """Find the memory store behind the harness and its fact strategy, once."""
        if self._memory_id:
            return
        harness_id = self._harness_arn().split("/")[-1]
        memory = self.control.get_harness(harnessId=harness_id)["harness"]["memory"]
        cfg = (memory.get("agentCoreMemoryConfiguration")
               or memory["managedMemoryConfiguration"])
        memory_id = cfg["arn"].split("/")[-1]
        strategies = self.control.get_memory(
            memoryId=memory_id)["memory"]["strategies"]
        self._strategy_id = next(s["strategyId"] for s in strategies
                                 if s["type"] == "SEMANTIC")
        self._memory_id = memory_id

    def namespace(self, actor_id: str) -> str:
        self._resolve()
        return f"/strategies/{self._strategy_id}/actors/{actor_id}/"

    def count_facts(self, actor_id: str, limit: int = 100) -> int:
        """How many extracted facts the store will return for this actor.

        Counting stops at `limit`, which is fine for a comparison against
        zero: no cap can hide a non-empty result.
        """
        namespace = self.namespace(actor_id)
        return len(self.data.list_memory_records(
            memoryId=self._memory_id, namespace=namespace,
            maxResults=limit)["memoryRecordSummaries"])

    def wait_for_facts(self, actor_id: str, timeout_s: int = 600,
                       poll_s: int = 10,
                       on_progress: Callable[[str], None] = log.info) -> bool:
        """Block until extraction has produced facts for this actor.

        Ask "what did we refund last time" ten seconds after the refund
        and the agent answers "who are you?": the events are stored, the
        facts are not built yet. Wait for the extraction, not the clock.
        """
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            found = self.count_facts(actor_id)
            if found:
                on_progress(f"[memory] {found} fact(s) extracted for {actor_id}")
                return True
            on_progress(f"[memory] extraction still running for {actor_id}...")
            time.sleep(poll_s)
        on_progress(f"[memory] no facts after {timeout_s}s -- recall may be cold")
        return False
