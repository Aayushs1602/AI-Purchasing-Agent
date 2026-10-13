# AI Purchasing Agent

An AI agent that reviews purchase recommendations for a quick-commerce buyer. It investigates inventory, demand, open purchase orders, supplier terms, budget and storage, then decides whether to **accept, modify, reject or escalate** the recommendation. It acts on that decision and **validates the result**.

> Status: work in progress. Built phase by phase; see the commit history.

## Scope

Implements **Scenario 1: Purchase Recommendation Review** end-to-end. The replenishment system recommends buying 800 units. The agent treats that recommendation as a claim to verify, not an instruction.

## Design in one line

**The LLM proposes, deterministic code disposes.** The LLM gathers evidence, weighs trade-offs and explains its decision. Plain code does every calculation, enforces hard constraints and the approval policy, and checks the outcome of every action against what the agent expected.

## Repository layout (planned)

```
backend/    FastAPI service: purchasing engine, PO lifecycle, mock supplier, agent, evals
frontend/   React + Vite UI: recommendations inbox, run timeline, approvals
docs/       Architecture, agent design, evaluation
```

Setup instructions, the architecture diagram, the evaluation approach and a demo walkthrough will be added as the build progresses.
