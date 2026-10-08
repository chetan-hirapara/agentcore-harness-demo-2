"""Read saved episodes: what did the agent actually do?

    python tools/episode_report.py                 # summary table, newest last
    python tools/episode_report.py --groups        # distinct trajectories
    python tools/episode_report.py --show 0241f7   # one episode, step by step

An episode is the audit record of one ticket (see harness_demo/agent/episode.py).
This tool only reads the JSON, so it also works on records saved by older
versions -- fields added later (customer_id, memory_facts) show as "-".
"""
import argparse
import glob
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness_demo.agent.episode import timeline
from harness_demo.config import Settings


def load(directory: str) -> list[dict]:
    records = []
    for path in sorted(glob.glob(os.path.join(directory, "*.json")),
                       key=os.path.getmtime):
        with open(path, encoding="utf-8") as f:
            records.append(json.load(f))
    return records


def _refunds(rec: dict) -> int:
    return sum(1 for t in rec["trace"] if t["tool"] == "issue_refund"
               and t["side"] == "client" and not t["result"].get("blocked"))


def _blocked(rec: dict) -> int:
    return sum(1 for t in rec["trace"]
               if t["side"] == "client" and t["result"].get("blocked"))


def summary(records: list[dict]) -> None:
    print(f"{'session':<9}{'steps':>6}{'blocked':>8}{'refunds':>8}"
          f"{'facts':>6}{'cost':>8}{'secs':>6}  actor")
    for r in records:
        facts = r.get("memory_facts")
        print(f"{r['session_id'][:8]:<9}{len(r['trace']):>6}{_blocked(r):>8}"
              f"{_refunds(r):>8}{'-' if facts is None else facts:>6}"
              f"{r.get('cost_usd', 0):>8.3f}{r.get('duration_s', 0):>6.0f}"
              f"  {r.get('actor_id', '-')}")
    print(f"\n{len(records)} episode(s), total "
          f"${sum(r.get('cost_usd', 0) for r in records):.3f}")


def groups(records: list[dict]) -> None:
    """Distinct trajectories for the same ticket, with how each one ended.

    The model picks its own route, so the same ticket takes several paths.
    Safety holds on all of them because the gates sit on the tools, not on
    the route; a path is only 'incomplete' when no refund was committed.
    """
    seqs = defaultdict(list)
    for r in records:
        seqs[tuple(t["tool"] for t in r["trace"])].append(r)
    print("* = ran behind our gate; the rest ran on the AWS sandbox\n")
    ranked = sorted(seqs.values(), key=len, reverse=True)
    for i, recs in enumerate(ranked, 1):
        steps = " -> ".join(t["tool"] + ("*" if t["side"] == "client" else "")
                            for t in recs[0]["trace"])
        done = sum(1 for r in recs if _refunds(r))
        cost = sum(r.get("cost_usd", 0) for r in recs) / len(recs)
        print(f"PATH {i}: {len(recs)}x ({len(recs) / len(records):.0%})  "
              f"refunded {done}/{len(recs)}  blocked "
              f"{sum(_blocked(r) for r in recs)}  avg ${cost:.3f}")
        print(f"   {steps}")
        print(f"   replay: python tools/episode_report.py --show "
              f"{recs[0]['session_id'][:8]}\n")


def show(records: list[dict], prefix: str) -> int:
    matches = [r for r in records if r["session_id"].startswith(prefix)]
    if len(matches) != 1:
        print(f"{len(matches)} episodes match {prefix!r}; need exactly one.")
        return 1
    r = matches[0]
    print(f"session   {r['session_id']}")
    print(f"actor     {r.get('actor_id', '-')}   "
          f"customer {r.get('customer_id', '-')}")
    print(f"task      {r['task']}")
    print(f"cost ${r.get('cost_usd', 0):.3f}   {r.get('duration_s', 0)}s   "
          f"stops {r.get('stop_reasons')}   memory facts "
          f"{r.get('memory_facts', '-')}\n")
    for line in timeline(r["trace"]):
        print(f"  {line}")
    print(f"\nfinal answer:\n{r.get('final_text', '').strip()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dir", default=Settings().episodes_dir)
    p.add_argument("--groups", action="store_true")
    p.add_argument("--show", metavar="SESSION_PREFIX")
    args = p.parse_args(argv)
    sys.stdout.reconfigure(errors="replace")   # model text may not fit cp1252
    records = load(args.dir)
    if not records:
        print(f"No episodes in {args.dir}")
        return 1
    if args.show:
        return show(records, args.show)
    (groups if args.groups else summary)(records)
    return 0


if __name__ == "__main__":
    sys.exit(main())
