"""Deterministic purchasing engine.

Pure functions over an in-memory snapshot of one SKU at one node: no database, no LLM.
The agent relies on this package for every number it cites, and the validators use it to
re-check the agent's work, so the same arithmetic serves both decision and verification.
"""
