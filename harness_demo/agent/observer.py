"""Observers: how a caller watches an episode.

The library never prints. It reports events to an observer, so the same
code can drive a console demo, a log line, a test, or a web UI.
"""


class EpisodeObserver:
    """Silent by default. Override the events you care about."""

    def on_text(self, chunk: str) -> None:
        """A streamed fragment of the model's reply."""

    def on_message_end(self) -> None:
        """One model message finished (a turn can hold several)."""

    def on_gate(self, tool: str, result: dict) -> None:
        """A gated tool ran in OUR process and produced `result`."""

    def on_note(self, message: str) -> None:
        """A status line, for example from the memory store."""

    def on_episode_end(self, episode) -> None:
        """The episode finished and its record is complete."""


class ConsoleObserver(EpisodeObserver):
    def __init__(self) -> None:
        self._mid_line = False

    def on_text(self, chunk: str) -> None:
        print(chunk, end="", flush=True)
        self._mid_line = not chunk.endswith("\n")

    def on_message_end(self) -> None:
        # Without this, consecutive messages run together
        # ("...damagedThe calculator returned") and read as a rendering bug.
        if self._mid_line:
            print(flush=True)
            self._mid_line = False

    def on_gate(self, tool: str, result: dict) -> None:
        tag = ("BLOCKED" if result.get("blocked")
               else "DUPLICATE" if result.get("duplicate") else "ok")
        reason = f": {result['reason']}" if result.get("reason") else ""
        print(f"\n  [gate] {tool} -> {tag}{reason}")

    def on_note(self, message: str) -> None:
        print(f"  {message}")

    def on_episode_end(self, episode) -> None:
        print(f"\n--- episode: {len(episode.trace)} tool call(s) "
              f"({len(episode.gated_calls)} gated), "
              f"{len(episode.blocked_events)} blocked, "
              f"~${episode.cost_usd:.3f}, stops={episode.meter.stop_reasons}")
