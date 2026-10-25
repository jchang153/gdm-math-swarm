"""Researcher-only incident report: a readable, model-written account of one run (owner request,
October 21, 2026).

One report per run, modelled on a misalignment incident report: the whole story up front, two to
five case-study storylines with labelled verbatim excerpts, what did not happen, candid
uncertainty and what to try next; every excerpt carries its agent, run-relative and absolute time and
Inspect View link (no per-agent section or "where to look" appendix since October 25, 2026). It is
model-generated and post-hoc and is labelled so; the deterministic results report (``results.html``) is
unchanged.

Modules: ``digest`` (deterministic run facts and item locators, no model), ``prompts`` (plan,
extract and write prompts), ``verify`` (host check that every excerpt is verbatim), ``render``
(report.md / report.html / evidence.json), ``mock`` (offline responder) and ``pipeline`` (the
stages, unit cache and budget). The CLI is ``scripts/run_incident_report.py``. Model calls go
through the behavior judge's ``JudgeClient`` (OpenRouter, Anthropic only, ledger ``analysis``
scope, one per-invocation run limit).

Nothing in this package may reach an experimental agent: never copy, mount or quote it, its
prompts or its outputs into agent images, containers, workspaces, prompts, tool results or the
conference library.
"""
