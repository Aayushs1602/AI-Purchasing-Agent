# Agent design

## The principle: the LLM proposes, deterministic code disposes

An LLM is good at deciding what to look at, weighing signals that do not fit a formula (a PO
note saying a shipment is on hold, sales that contradict the forecast, a supplier note that
tries to give orders) and explaining a decision. It is bad at arithmetic and must not hold the
final say over spending. So:

| The LLM decides | Code decides |
|---|---|
| Which evidence to gather and what it means | Every number: inventory position, projected stock, need, cover, cost |
| Whether to trust a held PO or a drifting forecast | Hard limits: MOQ, case pack, budget, storage |
| accept / modify / reject / investigate, and the quantity | Whether a human must approve (approval policy) |
| How to react when the supplier answers differently | PO state transitions, budget commitment, idempotency |
| The rationale and key factors | Whether the decision matches what was done and cites real numbers |

## What the agent knows

The agent reviews one recommendation per run. Before it may act, it must read seven sources
(the **evidence gate** refuses every action until it has):

| Evidence | Tool | Why it matters |
|---|---|---|
| Inventory | `get_inventory` | On hand, reserved, safety stock, age of the last count |
| Storage | `get_storage_capacity` | The capacity a delivery must fit into |
| Forecast | `get_demand_forecast` | Demand over lead time + review period |
| Sales | `get_sales_history` | Actuals, to check whether the forecast still holds |
| Open POs | `get_open_purchase_orders` | Stock already on the way, ETAs, overdue flags, notes |
| Supplier terms | `get_supplier_terms` | Lead time, MOQ, case pack, price, reliability, portal notes |
| Budget | `get_budget` | What is left this month for the category |

Plus `get_recommendation` (the quantity, how it was computed and from which inputs) and two
analysis tools: `check_recommendation_inputs` (what changed since the recommendation was made)
and `evaluate_order_quantity` (the what-if: project any quantity and check it against every
constraint and approval rule, and report the order limits, including `order_for_need`, the
smallest valid order that covers the need; once the run has its own PO, a candidate is
evaluated as a replacement for it).

No tool takes a SKU, store or supplier: every tool answers for the recommendation under review,
so the agent cannot drift onto other products (**scope lock by construction**).

## What the agent can do

| Action | Alone | Needs a human | Never |
|---|---|---|---|
| Read data, run what-if checks | ✓ | | |
| Create, amend or cancel its own draft PO | ✓ | | |
| Submit the recommended quantity within policy | ✓ | | |
| Submit a PO the approval policy flags (below) | | ✓ run pauses | |
| Accept a supplier deviation that leaves the PO needing approval | | ✓ escalate | |
| Escalate to a human with questions and options | ✓ | | |
| Submit a PO that breaks MOQ, case pack, budget or storage | | | ✗ refused |
| Touch POs it did not create, other SKUs, budgets or supplier terms | | | ✗ no such tool |
| Approve its own PO | | | ✗ no such tool |

**Approval policy** (`PolicyConfig` in `backend/app/engine/policy.py`; thresholds are code, not
prompt):

- **any quantity other than the recommended one**: the agent may place the recommendation as
  it stands, but every *modify* (and every amend that changes the quantity) goes to a buyer
  (`quantity_change_needs_approval`)
- order value above **$3,000**
- quantity more than **20% above** the recommendation
- quantity more than **20% below** the computed need (under-coverage)
- cover after receipt above **25 days** (overstock), or no net need at all
- a changed assumption: an open PO excluded, or recent sales used instead of the forecast
- a data-quality flag: forecast and sales differ by more than **30%**, an overdue open PO, a
  stock count older than **14 days**, no sales history
- the agent's own confidence below **0.7**

The specific rules still report alongside the quantity-change rule, so the buyer sees why a
change is risky (say, under-coverage), not only that the quantity changed.

## Guardrails, all enforced in code

| Guardrail | Where | What it stops |
|---|---|---|
| Evidence gate | `tools/actions.py` | Acting before reading the seven sources |
| Scope lock | tools take no SKU/store args | Working on anything but the recommendation |
| Ownership | `own_po()` | Changing POs created by people or other runs |
| Strict arguments | `ToolArgs` (`extra="forbid"`) | Silently ignored or invented arguments |
| Hard constraints | `PurchaseOrderService` | Any PO that breaks MOQ, case pack, budget or storage |
| Approval gate | `PurchaseOrderService.submit` | Sending a flagged PO without a human |
| Idempotency | key = run + tool-call id | A replayed call creating a second PO |
| One live PO per recommendation | `PurchaseOrderService.create_draft` | Duplicate orders |
| No new run while an order is live | `AgentRunner.create_run` | Re-reviewing a recommendation whose own earlier order then reads as stock on the way (and the new run as a rejection) |
| Expectation vs outcome | `agent/checks.py` | Carrying on after the world did something else |
| Circuit breaker | `record_supplier_outcome()` | Looping on a supplier that will not budge |
| Finalize consistency and grounding | `agent/checks.py` | A decision that contradicts the database or cites invented numbers |
| Model-call budget, one nudge | `orchestrator.py` | Runaway or stalled runs |
| Untrusted text | `untrusted()` wrapper + prompt | Instructions hidden in supplier or PO notes |
| Degraded mode | `orchestrator.py` | An LLM outage leaving no trace or acting blindly |

## How results are validated

Validation happens at four points, from before the agent acts until after it has finished.

1. **Before acting: the what-if check.** `evaluate_order_quantity` projects stock day by day
   for any candidate quantity and returns hard violations, soft warnings, data-quality flags and
   approval reasons. The agent is told never to compute these itself.
2. **Every action: expectation vs outcome.** `create`, `amend` and `submit` require the agent to
   state what it expects (quantity, maximum cost, optionally arrival date and cover range).
   The system reads the PO back, as stored or as the supplier confirmed it, re-validates it
   against current data and returns an `outcome_check`: each difference, any constraint now
   broken, any approval reason nobody signed off, and the allowed next actions.
3. **After the supplier answers: reconciliation.** A price rise that breaks the budget, or a
   partial fill that leaves the order under-covered, is an `UNEXPECTED_OUTCOME`. The agent must
   amend and resubmit, cancel, or escalate. It may "accept the deviation" only if nothing is
   broken and nothing needs approval. After two failed repairs the circuit breaker escalates
   in code and blocks further PO changes.
4. **At the end: finalize checks.** The decision must match what the run left in the database
   (accept means a placed PO for exactly the recommended quantity, modify a placed PO for
   another quantity, reject no PO, investigate an escalation; no leftover drafts; the PO left
   in place must not break a constraint unless escalated). Every number in the key factors must
   be one a tool returned. One retry with the problems listed, then the run is flagged
   `needs_review` for a human.

On top of this, the evaluation graders (see [evaluation.md](evaluation.md)) re-check every run's
final state independently of the agent.

## Human in the loop

- **Approval pause.** When a submit needs approval the loop holds the result back, stores the
  model's turn and loop state, and waits. The buyer approves or declines in the UI (or CLI);
  the PO service re-validates and sends the order, and the run resumes with the outcome as the
  result of that same tool call. A run can resume in a different process.
- **Escalations** carry the agent's questions and the options it sees; a buyer resolves them
  with a note.
- **Degraded mode.** If the LLM fails, code proposes the rules-only decision to a human
  (an escalation) and takes no action on it.

## LLM integration

- Provider-neutral interface (`agent/llm/base.py`): tool calls in, tool results out. The loop
  never sees provider types.
- **Gemini** (`agent/llm/gemini.py`) via `generate_content` with manual function calling. The
  model's turns are kept verbatim so thought signatures survive, including across a pause that
  stores the history in the database. Calls are spaced for the free tier and retried with
  backoff on 429/5xx; an exhausted daily quota fails fast with a clear message.
- **Record and replay** (`agent/llm/recording.py`). Live runs are saved as the model's turns plus
  a signature of how the tools answered. Replay plays them back while the tools and every
  validator run for real on fresh data, and stops if the tools now answer differently.
- **Scripted** client for tests and the golden trajectories.

Default model: `gemini-3.5-flash-lite` (free tier, about 15 requests/minute). A run takes
5-8 model calls and roughly 20-35 seconds, most of it the deliberate spacing between calls.
