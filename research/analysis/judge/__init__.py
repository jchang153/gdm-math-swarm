"""Researcher-only transcript behavior judge (docs/plan.md, October 17, 2026).

Nothing in this package may enter experimental agent images, containers,
workspaces, prompts, tool results or the conference library.

Modules: ``history`` (full per-agent history reconstruction, exposure ledger and
host context from a saved run), ``windows`` (screening windows), and the model
client, prompts, schema, validation, cohort and label modules that consume them;
``pipeline`` orchestrates the stages, ``estimate`` plans and prices one judging
invocation and sets its run limit (judge revision v3), ``review`` and ``report``
build the evidence browser and the report. ``statement_defects.json`` is the
host-only statement-defect table. See README.md in this directory.
"""
