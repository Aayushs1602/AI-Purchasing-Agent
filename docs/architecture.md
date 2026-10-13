# Architecture

One FastAPI process serves the API, runs agent runs on background workers and talks to a
SQLite database. A React app is the buyer's console. The LLM sits inside a loop whose every
side effect goes through deterministic code.

## Components

```mermaid
flowchart LR
  UI["React console<br/>inbox · run trace · approvals"] -->|"REST, polling"| API["FastAPI"]
  CLI["CLI<br/>python -m app.cli"] --> Runner
  Evals["Evaluation harness<br/>python -m evals.run"] --> Runner
  API -->|"background worker"| Runner["AgentRunner<br/>tool-use loop, pause/resume"]
  Runner <-->|"tool calls / results"| LLM["LLM client<br/>Gemini · replay · scripted"]
  Runner --> Tools["Tools<br/>read · what-if · actions"]
  Tools --> Engine["Purchasing engine<br/>projection · constraints · approval policy"]
  Tools --> POs["PO service<br/>state machine · budget · idempotency"]
  POs --> Supplier["Supplier gateway<br/>(mock: confirm · price rise · partial fill)"]
  Tools --> Checks["Checks<br/>expectation vs outcome · finalize consistency · grounding"]
  Engine --> DB[("SQLite<br/>scenarios seeded from YAML")]
  POs --> DB
  Runner -->|"trace, state"| DB
  Admin["/admin<br/>read-only DB browser"] --> DB
```

| Layer | Code | Responsibility |
|---|---|---|
| Engine | `backend/app/engine/` | Pure functions: inventory position, day-by-day projection, order rounding, constraint findings, approval policy, rules-only reference decision. No I/O, no LLM. |
| Services | `backend/app/services/` | Build the engine's snapshot from the DB; the PO lifecycle; the supplier gateway. |
| Agent | `backend/app/agent/` | LLM clients, tools, prompt, the orchestrator loop and the checks that close the feedback loop. |
| API | `backend/app/api/` | Recommendations, runs, approvals, escalations, POs, demo reset. |
| Evaluation | `backend/evals/` | Harness, graders, golden trajectories, reports. |
| UI | `frontend/src/` | Inbox, live run trace, approval panel, approvals queue. |

## One run, end to end

```mermaid
sequenceDiagram
  autonumber
  actor Buyer
  participant UI
  participant API
  participant Runner as AgentRunner
  participant LLM
  participant Tools
  participant PO as PO service
  participant Sup as Supplier

  Buyer->>UI: Run agent on REC-1012
  UI->>API: POST /recommendations/REC-1012/runs
  API->>Runner: execute(run) on a worker
  API-->>UI: 202 run_id (UI polls /runs/{id}?steps_after=n)
  Runner->>LLM: system prompt + kickoff
  LLM-->>Runner: 9 parallel read calls
  Runner->>Tools: execute (evidence recorded)
  Runner->>LLM: results
  LLM-->>Runner: evaluate_order_quantity(800)
  LLM-->>Runner: create_purchase_order(800, expectation)
  Tools->>PO: create draft (re-validated, idempotent)
  LLM-->>Runner: submit_purchase_order(PO-5001, expectation)
  Tools->>PO: submit
  PO-->>Tools: PENDING_APPROVAL (value above limit)
  Note over Runner: pause: store model turn, other results, loop state
  Buyer->>UI: Approve
  UI->>API: POST /purchase-orders/PO-5001/approve
  API->>Runner: resume(run, approve) on a worker
  Runner->>PO: approve (re-validated first)
  PO->>Sup: send order
  Sup-->>PO: acknowledgement
  Runner->>LLM: result of the held submit call: APPROVED_AND_SENT + outcome check
  LLM-->>Runner: finalize_decision(accept, 800, key factors)
  Runner->>Tools: consistency + grounding checks
  Note over Runner: settle: completed
```

## The feedback loop

```mermaid
flowchart TD
  A["Recommendation: buy 800"] --> B["Investigate: 8 read tools in parallel"]
  B --> G{"Evidence gate<br/>all 7 sources read?"}
  G -- "no: actions refused" --> B
  G -- yes --> C["What-if: evaluate_order_quantity<br/>projection, violations, approval reasons"]
  C --> D{"Decide"}
  D -- reject --> F
  D -- investigate --> E["escalate_to_human<br/>questions + options"]
  D -- "accept / modify" --> P["create / amend PO<br/>+ expectation"]
  P --> V{"Read back:<br/>as expected?"}
  V -- "no: UNEXPECTED_OUTCOME" --> P
  V -- yes --> S["submit_purchase_order<br/>+ expectation"]
  S --> H{"Approval policy"}
  H -- "needs a human" --> W["Run pauses → human decides → run resumes"]
  H -- auto --> K["Supplier acknowledgement"]
  W --> K
  K --> R{"Re-validate confirmed PO<br/>vs expectation"}
  R -- "deviation, repairs left" --> P
  R -- "deviation, 2 repairs used" --> X["Circuit breaker:<br/>code escalates, PO changes blocked"]
  R -- "deviation needing approval" --> E
  R -- ok --> F["finalize_decision"]
  X --> F
  E --> F
  F --> Q{"Consistent with what happened?<br/>Numbers from tool outputs?"}
  Q -- "no, first time" --> D
  Q -- "no, again" --> NR["Recorded, run flagged needs_review"]
  Q -- yes --> Z["Run settles: completed / escalated"]
```

## Purchase order states

```mermaid
stateDiagram-v2
  [*] --> draft: create (validated)
  draft --> pending_approval: submit, policy needs a human
  draft --> submitted: submit, within policy
  pending_approval --> submitted: approve (re-validated)
  pending_approval --> declined: decline
  submitted --> confirmed: supplier ack
  submitted --> partially_confirmed: supplier ack (short)
  confirmed --> draft: amend (new revision)
  partially_confirmed --> draft: amend (new revision)
  pending_approval --> draft: amend
  draft --> cancelled
  pending_approval --> cancelled
  submitted --> cancelled
  confirmed --> cancelled
  partially_confirmed --> cancelled
  declined --> [*]
  cancelled --> [*]
```

Budget is committed when a PO is sent, adjusted to what the supplier confirms (even past the
budget, so re-validation sees the truth) and released on amend or cancel. Every transition is
written to `purchase_order_events`.

## Data

SQLite (WAL mode) with SQLAlchemy 2. Tables: products, nodes, suppliers, supplier terms,
inventory, daily forecasts, sales history, budgets, purchase orders and their events,
recommendations, agent runs, agent steps (the trace) and escalations. All dates come from a
fixed simulated clock (`SIM_TODAY`), so scenarios are reproducible. The schema carries a
version stamp; a database from an older version is rebuilt from the scenario files on start
(demo data only; production would use migrations).

Scenarios live in `backend/scenarios/*.yaml`: one baseline world where buying 800 is exactly
right, and 14 files that each override one thing and state the hand-labelled expected outcome.
