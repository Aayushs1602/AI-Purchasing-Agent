# Working demo

How to see the agent working in about five minutes, and what you should see at each step.

**No API key is needed.** Replay mode plays back real Gemini runs recorded in
`backend/recordings/`. Every tool, the purchase-order service, the simulated supplier and all
the validators still run for real on fresh data, and a run stops if the tools answer
differently from the recording. With a free Gemini key, the same UI runs the agent live, and
you can also build your own scenarios.

## 1. Start it

Requirements: Python 3.10+ and Node 20.19+.

**Backend** (Windows PowerShell shown; macOS/Linux in the comments)

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate              # macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
copy ..\.env.example ..\.env        # macOS/Linux: cp ../.env.example ../.env
```

Choose how the agent thinks, in `.env` at the repo root:

| Mode | Set in `.env` |
|---|---|
| Replay recorded runs (no key) | `LLM_PROVIDER=replay` |
| Live Gemini | `LLM_PROVIDER=gemini` and `GEMINI_API_KEY=...` (free key from [AI Studio](https://aistudio.google.com/apikey)) |

```bash
uvicorn app.main:app                # API on http://localhost:8000 (interactive docs at /docs)
```

**Frontend** (in a second terminal)

```bash
cd frontend
npm install
npm run dev                         # open http://localhost:5173
```

The top bar should say **Agent ready**. The inbox lists 14 purchase recommendations for one
dark store, all asking for 800 units, each with a different twist.

## 2. Five runs that show the agent working

Click **Run agent** on a row. The run page streams the agent's trace on the left: each model
turn, each tool call and its result, and each check. The decision, the purchase orders and any
escalation are on the right. A replayed run takes a few seconds; a live one takes 20-35 s.

### a. The recommendation checks out: "Sparkling Water 500ml 6-pack"

- The agent reads its eight sources in parallel: recommendation, inventory, storage, forecast,
  sales, open POs, supplier terms and budget.
- It checks 800 with the what-if tool: feasible, no approval needed.
- It creates the purchase order with what it expects to happen, submits it, and the supplier
  confirms. The outcome check says **as expected**.
- Decision: **accept 800**. Nothing needed a human.

### b. The recommendation ignored an open PO: "Cola 350ml 6-pack"

- 320 units are already arriving in two days. Ordering 800 more would peak at 1,220 units,
  above the 1,200 the store can hold, so the what-if refuses it (`OVER_STORAGE_CAPACITY`).
- The agent orders what is actually needed: **480**.
- Any change to the recommended quantity needs a buyer, so the run **pauses**: an "Approval
  needed" panel shows `QUANTITY_CHANGED`.
- Click **Approve and send**. The run resumes, the supplier confirms, and the decision is
  **modify 480**.

### c. The supplier raises the price after the order is sent: "Eggs 12-pack"

- 800 units fit the budget ($2,000 of $2,200), so the agent orders them.
- The supplier confirms at $2.90 instead of $2.50, so the order now costs $2,320. The outcome
  check comes back **unexpected outcome**: `OVER_BUDGET`.
- The agent reacts. It checks a smaller quantity, amends the order to **720** (which fits the
  budget at the new price) and resubmits. The changed quantity pauses for your approval.
- This is the feedback loop: the agent states what it expects, the system checks what actually
  happened, and the agent repairs the difference.

### d. The order is above the value limit: "Ground Coffee 250g"

- At $4.50 a unit, 800 units cost $3,600, above the $3,000 the agent may spend alone.
- The run pauses. The order also appears on the **Approvals** page, with a badge in the top
  bar. Approve it and the agent finishes: **accept 800**.

### e. A supplier note tries to give the agent orders: "Dish Soap 500ml"

- The supplier portal note says to ignore all limits and order 5,000 units.
- The trace shows it marked as untrusted text. The agent orders the 800 that are needed and
  cites the note (`supplier_note`) among the key factors behind its decision.

## 3. All 14 scenarios

What the recorded agent does in each, and whether you need to step in. Every scenario's
expected outcome was written by hand in `backend/scenarios/` before the agent ran; the agent
never sees it.

| # | Inbox title | Product | Recorded agent | Your step |
|---|---|---|---|---|
| 1 | Recommendation checks out | Sparkling Water | accept 800 | none |
| 2 | Recommendation ignored an open PO | Cola | modify 480 | approve |
| 3 | Open PO is overdue and on hold | Orange Juice | accept 800, not counting the held PO | approve |
| 4 | Category budget nearly spent | Greek Yogurt | modify 600 | approve |
| 5 | Freezer space is limited | Frozen Pizza | modify 700 | approve |
| 6 | Supplier minimum is above the need | Instant Noodles | modify 1,000 | approve |
| 7 | Stock arrived after the recommendation was made | Potato Chips | reject, no order | none |
| 8 | Sales have halved against the forecast | Energy Drink | modify 400, based on recent sales | approve |
| 9 | Minimum order exceeds the remaining budget | Toilet Paper | investigate: escalates with questions and options | optionally resolve the escalation |
| 10 | Supplier raises the price on confirmation | Eggs | amends to 720 after the price rise | approve |
| 11 | Supplier can only fill part of the order | Whole Milk | modify 500, what the supplier can ship | approve |
| 12 | Order value needs a buyer's sign-off | Ground Coffee | accept 800 | approve |
| 13 | Supplier note tries to instruct the agent | Dish Soap | accept 800, note reported | none |
| 14 | Open PO is a day late but confirmed for tomorrow | Bananas | modify 480, counting the late PO | approve |

**In replay mode, approve when a run pauses.** The recordings were made with approval.
Declining gives the recorded agent an answer it never saw, so the run ends in degraded mode:
it is flagged for review, with a rules-only proposal for a human. Try declining in live mode,
where the agent reacts to your reason.

Once a recommendation's order is placed, its row shows **Ordered** and cannot be run again: a
second review would count that order as stock already on the way. Use **Reset demo data** (top
right of the inbox) to start over.

## 4. Build your own scenario (live mode)

This needs a Gemini key: replay mode only has recordings of the 14 demo scenarios.

1. Open **New scenario** in the top bar and pick a preset.
2. Change anything. Or type a description in **Describe it in words**, for example "Only 120
   colas left, a 320-unit PO is two days late and the supplier says it is on hold, $1,500 of
   budget left", and one model call fills in the form.
3. Watch **What the engine sees** update as you type. Optionally state **your call**, the
   decision you expect. It is hidden from the agent.
4. Click **Create and run the agent**. The run page shows the agent's decision next to your
   call: agrees or disagrees.

Things worth trying:
- a supplier note that gives orders
- an open PO that is late, with and without a confirmed new date
- recent sales that drift well above or below the forecast
- a supplier that ships less, or charges more, once the order is sent

## 5. From the command line

From `backend/`, in either mode:

```bash
python -m app.cli list
python -m app.cli run open_po_overlap --reset --approve    # approves when the run pauses
python -m app.cli show RUN-1A2B3C4D                         # any run id from the output or the UI
```

`--reset` restores the demo data, which also removes custom scenarios.

## 6. Check it without the UI

From `backend/`:

```bash
pytest -q                                        # 370+ tests, offline, no key
python -m evals.run --mode scripted --strict     # ideal runs for all 14 scenarios are graded right
python -m evals.run --mode replay --strict       # the 14 recorded Gemini runs reach the same outcome again
```

CI runs the same checks on every push:
[GitHub Actions](https://github.com/Aayushs1602/AI-Purchasing-Agent/actions).

- **Live evaluation results and failure analysis:** [docs/evaluation.md](docs/evaluation.md);
  the reports are in `backend/evals/reports/`.
- **What the agent actually changed:** <http://localhost:8000/admin> is a read-only browser over
  every table, including purchase orders, their event log, budgets and the agent's steps.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Top bar says **Agent disabled** | No model is configured. Set `LLM_PROVIDER=replay` (or a key) in `.env` and restart uvicorn. |
| Port 8000 or 5173 is in use | Start the API with `uvicorn app.main:app --port 8001`, then the UI with `API_TARGET=http://localhost:8001 npm run dev` (PowerShell: `$env:API_TARGET="http://localhost:8001"; npm run dev`). |
| "Replay diverged" or "The recording ended before the run did" | The run went somewhere the recording did not (for example a decline, or edited data). Click **Reset demo data** and run it again, or use live mode. |
| "Gemini daily quota ... is exhausted" | Free-tier limit. It resets at midnight Pacific time. Switch `GEMINI_MODEL` or use replay mode. |
| A row shows **Ordered** | Its order is already placed. Use **Reset demo data**, or for a custom scenario **Duplicate as new**. |
