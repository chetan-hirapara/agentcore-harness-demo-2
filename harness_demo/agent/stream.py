"""Read one event stream from the harness.

The stream interleaves three things:

  text            the model talking; forwarded to the observer
  server tools    the code interpreter; recorded as observed trace entries
  gated tools     run_sql / issue_refund; returned as PENDING calls so the
                  caller can run them behind a gate and answer the harness
"""
import json

from harness_demo.agent.episode import Episode
from harness_demo.agent.observer import EpisodeObserver
from harness_demo.errors import EpisodeAborted


def drain(stream, ep: Episode, gated_names: frozenset[str],
          observer: EpisodeObserver) -> list[dict]:
    """Consume `stream`; return the gated tool calls the model emitted.

    The model can emit several tool calls in ONE turn (parallel tool use).
    Input deltas are only distinguishable by contentBlockIndex, so they are
    accumulated per block -- a shared buffer would concatenate two JSON
    objects and json.loads would die with "Extra data".
    """
    blocks: dict[int, dict] = {}      # contentBlockIndex -> gated call
    pending: list[dict] = []
    last_idx = 0
    for event in stream:
        if "contentBlockStart" in event:
            block = event["contentBlockStart"]
            last_idx = block.get("contentBlockIndex", last_idx + 1)
            tool_use = block.get("start", {}).get("toolUse")
            if tool_use:
                if tool_use["name"] in gated_names:
                    call = {"toolUseId": tool_use["toolUseId"],
                            "name": tool_use["name"], "raw": ""}
                    blocks[last_idx] = call
                    pending.append(call)
                else:
                    # Server-side tool: we observe, AWS executes.
                    ep.trace.append({"tool": tool_use["name"],
                                     "side": "harness",
                                     "input": None, "result": {}})
        elif "contentBlockDelta" in event:
            block = event["contentBlockDelta"]
            idx = block.get("contentBlockIndex", last_idx)
            delta = block.get("delta", {})
            if "text" in delta:
                ep.text += delta["text"]
                observer.on_text(delta["text"])
            if "toolUse" in delta and idx in blocks:
                blocks[idx]["raw"] += delta["toolUse"].get("input", "")
        elif "messageStop" in event:
            observer.on_message_end()
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
