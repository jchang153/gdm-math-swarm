"""Track A red-team analysis (researcher-only): exposure, outcomes and the overseer-message judge.

``extract`` builds a run's facts from its conference database (no model); ``judge`` labels every message the
overseer wrote (one model call per message); ``outcomes`` combines both into the run's outcomes. Driver:
scripts/run_redteam_analysis.py. Plan: Amelie's project repo, docs/redteam/plan_trackA.md.

The judge's rubric is researcher-only and lives host-only in the git-ignored private/ directory
(research/analysis/private_material.py); ``from . import rubric`` works when it is present.
"""

from ..private_material import install_module as _install_private_module

_install_private_module(__name__ + ".rubric", "research/analysis/redteam/rubric.py")
