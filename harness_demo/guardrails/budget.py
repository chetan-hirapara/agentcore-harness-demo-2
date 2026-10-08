"""Runtime budgets: two layers, deliberately.

  1. Harness-native caps (maxIterations, maxTokens, timeoutSeconds)
     passed on invoke_harness -- AWS enforces these inside the loop and
     reports breaches as stopReason values like "max_iterations_exceeded".
  2. This client-side ExecutionBudget, for what the harness cannot see:
     cumulative dollar cost across continuation calls and the total
     number of gated-tool invocations.

If either layer trips, the episode aborts as a *bounded, traced* failure
(EpisodeAborted) instead of an unbounded one.
"""
from dataclasses import dataclass, field

from harness_demo.errors import EpisodeAborted

HARNESS_LIMIT_STOP_REASONS = ("max_iterations_exceeded", "timeout_exceeded",
                              "max_output_tokens_exceeded")


@dataclass
class ExecutionBudget:
    max_cost_usd: float = 0.50
    max_iterations: int = 25       # passed through to the harness too
    max_tool_calls: int = 40

    # Update to your model's current rate card before quoting numbers.
    usd_per_1k_input: float = 0.003
    usd_per_1k_output: float = 0.015


@dataclass
class BudgetMeter:
    budget: ExecutionBudget
    cost_usd: float = 0.0
    tool_calls: int = 0
    stop_reasons: list = field(default_factory=list)

    def record_usage(self, usage: dict) -> None:
        if not usage:
            return
        self.cost_usd += (
            usage.get("inputTokens", 0) / 1000 * self.budget.usd_per_1k_input
            + usage.get("outputTokens", 0) / 1000 * self.budget.usd_per_1k_output
        )
        if self.cost_usd > self.budget.max_cost_usd:
            raise EpisodeAborted(
                f"BUDGET_EXCEEDED: cost ${self.cost_usd:.3f} "
                f"> ${self.budget.max_cost_usd:.2f}")

    def record_tool_call(self) -> None:
        self.tool_calls += 1
        if self.tool_calls > self.budget.max_tool_calls:
            raise EpisodeAborted(
                f"BUDGET_EXCEEDED: {self.tool_calls} tool calls "
                f"> {self.budget.max_tool_calls}")

    def record_stop_reason(self, reason: str) -> None:
        self.stop_reasons.append(reason)
        if reason in HARNESS_LIMIT_STOP_REASONS:
            raise EpisodeAborted(f"HARNESS_LIMIT: {reason}")
