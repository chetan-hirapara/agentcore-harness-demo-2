"""Run the six-beat demo.

    python demo_run.py                # the recording
    python demo_run.py --pause        # wait for Enter before each beat
    python demo_run.py --log-level INFO   # also show the operational log

Needs a harness (see setup.ps1 / setup.sh). The ARN is taken from
HARNESS_ARN if set, otherwise looked up by name. The database is reseeded
every run, so order 4711 starts 'delivered' with an empty ledger -- without
that, beat 2 would replay against a warm ledger and report duplicate=True,
a correct result that tells the wrong story.
"""
import argparse
import logging
import sys

from harness_demo.agent.client import HarnessClient
from harness_demo.demo import beats
from harness_demo.demo.narration import DemoCheckFailed, banner
from harness_demo.errors import EpisodeAborted, HarnessError

BEATS = (
    beats.beat_read_only_gate,
    beats.beat_agent_works,
    beats.beat_amount_gate,
    beats.beat_retry_storm,
    beats.beat_memory_recall,
    beats.beat_memory_isolation,
)


def _intro(ctx: beats.DemoContext, harness_arn: str) -> None:
    banner("SUPPORT-AGENT HARNESS DEMO -- one refund ticket, six safety beats", [
        "A support agent is asked to refund a damaged order. Around it sits a",
        "harness that gates every risky action. Each beat shows one guarantee",
        "holding, and each is also proved offline in evals/test_invariants.py.",
        "",
        "Four words you will see:",
        "  harness  the managed AgentCore loop that calls the model and tools",
        "  gate     OUR code that can refuse a tool call the model asked for",
        "  episode  one ticket worked end to end, saved as an audit record",
        "  actor    the memory boundary: one customer = one actorId",
        "",
        *(f"  Beat {s.number}  {s.name}" for s in beats.SPECS.values()),
    ])
    print(f"\n  TICKET:  {beats.TICKET}")
    print(f"  CUSTOMER {ctx.customer_id} (data boundary)   "
          f"ACTOR {ctx.actor} (memory boundary)")
    print(f"  HARNESS: {harness_arn}")


def _scorecard(results: list[tuple[int, str]]) -> None:
    banner(f"ALL {len(results)} BEATS PASSED", [
        f"Beat {n}  {beats.SPECS[n].name:<24}{outcome}"
        for n, outcome in results])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--pause", action="store_true",
                        help="wait for Enter before each beat")
    parser.add_argument("--log-level", default="WARNING",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(errors="replace")   # model text may not fit cp1252
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    ctx = beats.DemoContext(HarnessClient(), pause=args.pause)
    results: list[tuple[int, str]] = []
    try:
        # Resolve the harness first: fail before beat 1, not halfway through.
        harness_arn = ctx.client.harness_arn
        ctx.db.seed()
        _intro(ctx, harness_arn)
        for number, beat in enumerate(BEATS, 1):
            results.append((number, beat(ctx)))
    except EpisodeAborted as e:
        print(f"\nEPISODE ABORTED (bounded failure): {e.reason}")
        return 2
    except DemoCheckFailed as e:
        print(f"\nGUARANTEE FAILED: {e}")
        return 1
    except HarnessError as e:
        print(f"\nSETUP PROBLEM: {e}")
        return 3
    _scorecard(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
