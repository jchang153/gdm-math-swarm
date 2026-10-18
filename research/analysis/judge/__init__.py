"""Researcher-only transcript behavior judge (docs/plan.md, October 17, 2026).

Nothing in this package may enter experimental agent images, containers,
workspaces, prompts, tool results or the conference library.

Modules: ``history`` (full per-agent history reconstruction, exposure ledger and
host context from a saved run), ``windows`` (screening windows), and the model
client, prompts, schema, validation, cohort and label modules that consume them.
``statement_defects.json`` is the host-only statement-defect table.
"""
