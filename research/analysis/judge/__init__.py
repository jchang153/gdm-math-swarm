"""Researcher-only transcript behavior judge (docs/plan.md, October 17, 2026).

Nothing in this package may enter experimental agent images, containers,
workspaces, prompts, tool results or the conference library.

Modules: ``history`` (full per-agent history reconstruction, exposure ledger and
host context from a saved run), ``windows`` (screening windows), and the model
client, prompts, schema, validation, cohort and label modules that consume them;
``pipeline`` orchestrates the stages, ``estimate`` plans and prices one judging
invocation and sets its run limit (judge revision v3), ``review`` and ``report``
build the evidence browser and the report. The host-only statement-defect table is
``private/research/analysis/judge/statement_defects.json`` (Git-ignored). See README.md in this directory.
"""

# The prompts are researcher-only and live host-only in the git-ignored private/ directory
# (research/analysis/private_material.py); ``from . import prompts`` keeps working.
from ..private_material import install_module as _install_private_module

_install_private_module(__name__ + ".prompts", "research/analysis/judge/prompts.py")
