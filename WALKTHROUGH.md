# Presenter walkthrough

A guide for a live code walkthrough of the harness. About 45 minutes: 10
on concepts, 20 reading code, 15 on the live demo. Audience: engineers,
some new to AI agents.

## Before you start

```powershell
python -m pytest evals/test_invariants.py -q    # green, offline
python demo_run.py --pause                      # dry run once, so memory has warmed up
```

Each demo run uses a fresh actor, so Beat 5 needs a few minutes of memory
extraction. Rehearse the timing once and talk through the wait.

## 1. The three concepts (10 min)

**Harness.** The agent is a loop: the model reasons, asks for a tool, the
tool runs, the result goes back. AgentCore runs that loop for us. The one
property that matters: for tools we declare as `inline_function`, the loop
**pauses** and our process answers. Everything below depends on that pause.

**Episode.** One ticket, recorded as JSON: every tool call, who ran it,
the result, cost, stop reasons. Open [docs/sample_episode.json](docs/sample_episode.json)
and read it aloud: two lookups, the calculator, then the refund. This is the
answer to "what did the agent actually do?".

**Memory.** Three steps, and the middle one surprises people:

| Step | When | Where |
|---|---|---|
| Write | every turn, instant | harness |
| Extract | minutes later, asynchronous | AWS |
| Retrieve | next session, by `actorId` | namespace path |

The namespace is `/strategies/<id>/actors/<actorId>/`. Two actors share
nothing because the path is the boundary.

## 2. Code reading order (20 min)

| # | File | Say this |
|---|---|---|
| 1 | [harness_demo/agent/tools.py](harness_demo/agent/tools.py) | Five capabilities, two sides of a trust boundary. Money and data are ours; compute and memory are AWS's. |
| 2 | [harness_demo/agent/client.py](harness_demo/agent/client.py) | The loop diagram in the docstring. Point at `_answer_gated_calls`: this is where the gate fires. |
| 3 | [harness_demo/guardrails/sql_gate.py](harness_demo/guardrails/sql_gate.py) | Four layers, each catching what the last can miss. |
| 4 | [harness_demo/db.py](harness_demo/db.py) | The scoping trick: temp views shadow the tables, an authorizer blocks the real ones. The customer id comes from the app, never the model. |
| 5 | [harness_demo/guardrails/refund_gate.py](harness_demo/guardrails/refund_gate.py) | The model proposes; this code decides. Walk the numbered gates. |
| 6 | [harness_demo/guardrails/ledger.py](harness_demo/guardrails/ledger.py) | Idempotency is a `PRIMARY KEY`, not clever code. |
| 7 | [harness_demo/policy/refund_calc.py](harness_demo/policy/refund_calc.py) | Shipped, not model-written. Same file runs in the sandbox and in the gate. |
| 8 | [harness_demo/agent/memory.py](harness_demo/agent/memory.py) | We measure isolation by counting facts, not by asking the model. |
| 9 | [evals/test_invariants.py](evals/test_invariants.py) | The harness is ordinary software, so it gets ordinary tests. |

Questions to expect:

- *Why not just tell the model not to write?* A prompt is a request. A gate is a guarantee.
- *Why recompute a number the sandbox already computed?* The model transcribed it.
- *Why is the day count recomputed too?* It decides eligibility. Checking the total without it checks the arithmetic, not the decision.
- *Why count facts instead of asking the agent?* It will say "I don't retain memory between sessions" even when it does.

## 3. Live demo (15 min)

`python demo_run.py --pause` waits for Enter before each beat, so you can
read the **WHAT WE TEST / HOW IT HOLDS / WATCH FOR** text aloud first.

| Beat | Proves | Say this |
|---|---|---|
| 1 Read-only gate | Writes are refused | "Driven by us, not the agent: the guarantee doesn't depend on the model misbehaving on cue." Point out the stacked query fails with a *different* error. |
| 2 Agent works | The happy path, end to end | Show `[gate]` lines (control returned to us), then the timeline: which steps ran on AWS and which behind our gate. |
| 3 Amount gate | A wrong figure moves no money | Read the refusal aloud; it names both figures. The correct amount comes back `DUPLICATE`, because beat 2 already did it. |
| 4 Retry storm | Replays cannot double-refund | Three replays, one ledger row. |
| 5 Memory recall | A new session remembers the customer | While waiting for extraction, explain write / extract / retrieve. Zero database lookups means the answer came from memory. |
| 6 Isolation | Nothing leaks between customers | Three proofs: facts per actor, ledger rows per session, then the model's answer. "Zero" only counts because the other side is non-zero. |

The closing scorecard lists the outcome of every beat.

## 4. After the demo

Run `python tools/episode_report.py` to list the episodes just recorded,
and `python tools/episode_report.py --show <session prefix>` to replay one
step by step. Then open [IMPROVEMENTS.md](IMPROVEMENTS.md): the honest list of
what would change before this runs against real customers.

### Showing different trajectory paths

The model chooses its own route, so one ticket takes several paths. The
point to make: the route varies, the guarantees do not.

```powershell
python tools/episode_report.py --groups          # every distinct path, how often, how it ended
python tools/episode_report.py --show <prefix>   # replay one path step by step
```

Pick the `--show` prefix from the `replay:` line under each path. Good
contrasts: the common path (lookup, lookup, calculator, refund), a
lookup-only path that never refunds (an incomplete run, not an unsafe one),
and a long path where the agent re-ran the calculator before refunding.
To build a bigger sample, run the live evals:
`$env:EVAL_RUNS = 20; python -m pytest evals/test_trajectory.py -v -s`
saves every episode and prints the path breakdown at the end.

## If something goes wrong

| Symptom | Cause |
|---|---|
| `SETUP PROBLEM: No harness named ...` | Run `setup.ps1`, or set `HARNESS_ARN` |
| `could not seed the calculator` | Caller lacks `InvokeAgentRuntimeCommandShell`, or the network blocks WebSockets |
| Beat 5 times out waiting for facts | Extraction is slow today. Stop and retake: recall would be cold |
| `EPISODE ABORTED` | A budget or harness limit tripped. That is the bounded-failure path working |
| `GUARANTEE FAILED` | A gate stopped gating. Treat it as a real bug, not a demo glitch |
