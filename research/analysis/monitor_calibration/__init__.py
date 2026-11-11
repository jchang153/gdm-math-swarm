"""Offline threshold calibration of the top-down monitor (docs/plan.md, "Top-down monitor arms (owner decisions,
November 10, 2026)"): replay ``research_swarm.monitor`` on an existing control run's tool-call decisions.

* ``points``: decision points (an agent model response with tool calls) from a run's Inspect log, their request
  history and pending calls, and the labels (fake submission / fake publication positive, everything else
  negative; finer labels kept).
* ``metrics``: ROC AUC and the per-threshold false-positive rate and recall.
"""
