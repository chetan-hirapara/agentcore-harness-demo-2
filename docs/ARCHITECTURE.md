# AgentCore Harness Demo — Architecture Flow

End-to-end flow for working one returns/refund ticket. The **trust boundary** is the
core idea: anything that can move money or leak data is an `inline_function`, so the
harness **pauses** and hands control back to code we own (the gates) instead of the
model's environment.

```mermaid
flowchart TB
    User["👤 Caller / CI<br/>task: 'Order #4711 arrived damaged…'<br/>actor_id (memory boundary)<br/>customer_id (data boundary)"]

    subgraph OURS["🟦 OUR PROCESS (code we own — the trust boundary)"]
        direction TB
        Run["run.py → DemoContext<br/>HarnessClient.run_episode(task, actor_id, customer_id)"]
        Seed["① Seed DB (relative dates) + ② Seed sandbox<br/>base64 refund_calc.py → /tmp/refund_calc.py"]
        Prompt["build_system_prompt(db, CALC_REMOTE_PATH)<br/>live schema_card() + process steps"]
        Tools["build_tools(calc_path)<br/>run_sql·inline_function<br/>issue_refund·inline_function<br/>code_interpreter·agentcore server tool"]
        Drain["stream.drain(stream, ep, gated_names, observer)<br/>split: text · server-tool trace · PENDING gated calls"]

        subgraph GATES["🔒 GatedToolbox.dispatch — bound to customer_id (never from model)"]
            direction TB
            SqlGate["run_sql gate (L1 read-only)<br/>1 Pydantic: query ^SELECT, max_rows≤1000<br/>2 connect_ro mode=ro<br/>3 authorizer: deny main.* / sqlite_*<br/>4 per-customer TEMP VIEWs"]
            RefundGate["issue_refund gate (L3 money)<br/>params: order_id·reason·amount_usd·days_since_delivery<br/>0 scope: order.customer_id == customer_id<br/>1 Pydantic schema<br/>2 independent recompute (ignore model number)<br/>3 idempotency key = SHA256(order,reason,cents)<br/>4 two-phase commit + rollback"]
        end

        Budget["BudgetMeter<br/>cost≤$0.50 · tool_calls≤40 · stop-reason watch<br/>→ EpisodeAborted (bounded failure)"]
        Episode["Episode audit record<br/>trace[{tool, side: client|harness, input, result}]<br/>save → episodes/{session_id}.json"]
    end

    subgraph DATA["🗄️ Data layer (SQLite, our side)"]
        direction TB
        DB["SupportDb<br/>customers · orders · refund_intents<br/>connect_scoped() = views + authorizer"]
        Ledger["RefundLedger (idempotent)<br/>begin→PENDING · apply_refund · commit→COMMITTED<br/>PRIMARY KEY(key) = the guarantee · rollback→ROLLED_BACK"]
        Calc["policy/refund_calc.py (deterministic, stdlib only)<br/>POLICY_VERSION 2026.02 · 30-day window<br/>restocking · shipping · 8.75% tax"]
    end

    subgraph AWS["🟧 AWS Bedrock AgentCore"]
        direction TB
        Control["bedrock-agentcore-control (control plane)<br/>list_harnesses · get_harness · get_memory"]
        Harness["AgentCore Harness (data plane)<br/>invoke_harness(harnessArn, runtimeSessionId,<br/>actorId, systemPrompt, tools, maxIterations, messages)<br/>streams: contentBlockStart/Delta · messageStop · metadata"]
        Model["🤖 Foundation Model<br/>reasons · emits toolUse · writes final answer"]
        Sandbox["Sandbox microVM<br/>code_interpreter runs python /tmp/refund_calc.py<br/>--item-price --shipping --days --reason --tier<br/>seeded via WSS shell (SigV4, v1.command…)"]
        Memory["Managed Memory (scoped by actorId)<br/>WRITE event (sync) → EXTRACT facts (async)<br/>RETRIEVE from /strategies/{sid}/actors/{actorId}/<br/>list_memory_records → count_facts (isolation audit)"]
    end

    User --> Run
    Run --> Seed
    Seed -->|"① seed calculator (no model)"| Sandbox
    Run --> Prompt --> Tools
    Run -->|"resolve_harness_arn()"| Control
    Control -.ARN + memory/strategy config.-> Run

    Tools -->|"invoke_harness (streaming)"| Harness
    Harness --> Model
    Memory -. facts loaded first .-> Model

    Model -->|"toolUse"| Harness
    Harness -->|"server-side tool: code_interpreter"| Sandbox
    Sandbox -->|"JSON {total, eligible, breakdown}"| Harness
    Sandbox -. imports byte-identical .- Calc

    Harness -->|"stopReason=tool_use — LOOP PAUSES<br/>stream back to us"| Drain
    Drain -->|"PENDING gated calls"| GATES
    Drain -->|"server-tool observed"| Episode

    SqlGate --> DB
    RefundGate --> DB
    RefundGate -->|"recompute & verify"| Calc
    RefundGate --> Ledger
    GATES -->|"meter each call"| Budget

    GATES -->|"toolResult (json) → continue loop"| Harness
    Harness -->|"final answer (end_turn)"| Drain
    Drain --> Episode
    Episode -->|"count_facts(actor_id)"| Memory
    Episode -->|"scorecard + {session_id}.json"| User

    classDef ours fill:#e3f0ff,stroke:#2b6cb0,color:#1a365d;
    classDef aws fill:#fff0e0,stroke:#dd6b20,color:#7b341e;
    classDef data fill:#eafaf0,stroke:#2f855a,color:#1c4532;
    classDef gate fill:#fff5f5,stroke:#c53030,color:#742a2a;
    class Run,Seed,Prompt,Tools,Drain,Budget,Episode ours;
    class Control,Harness,Model,Sandbox,Memory aws;
    class DB,Ledger,Calc data;
    class SqlGate,RefundGate gate;
```

## How to read it

- **Blue = our process** (the harness client, gates, episode). **Orange = AWS AgentCore**
  (control plane, harness loop, model, sandbox, managed memory). **Green = our data layer**
  (SQLite + deterministic policy). **Red = the gates** where every money/data action is decided.
- **The pause is the point.** When the model calls a gated `inline_function`
  (`run_sql`, `issue_refund`), the harness returns `stopReason=tool_use` and the loop
  pauses in our code. Nothing the model says reaches the database or ledger except through
  a gate we wrote.
- `code_interpreter` is a **server-side** AgentCore tool — it runs on the sandbox microVM
  and we only observe it (`side: "harness"`), but it runs *our* byte-identical calculator
  that we seeded before any model reasoning.
- `customer_id` (data boundary) and `actor_id` (memory boundary) come from the **calling
  application**, never from the model.

## AWS surface used

| Plane | Service · API | Purpose |
|------|----------------|---------|
| Control | `bedrock-agentcore-control:list_harnesses` / `get_harness` / `get_memory` | Resolve harness ARN, memory + strategy config |
| Data | `bedrock-agentcore:invoke_harness` (streaming) | The reason→tool→continue loop |
| Data | `bedrock-agentcore` runtime WSS shell (`v1.command.agentcore.aws.dev`, SigV4) | Seed `refund_calc.py` to the sandbox microVM |
| Data | `bedrock-agentcore:list_memory_records` | Count facts per `actorId` (isolation audit) |
| IAM | `iam:PassRole` → `AgentCoreHarnessLabRole` | Harness execution role |
```
