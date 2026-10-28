"""Simplified behavior judge (researcher-only): one model call per agent over a code-built case file.

Owner request, 2026-10-24: no prefilter, candidate or verification stages, no miss-rate sampling, no live
judging and no restart or rescore machinery. See README.md in this folder.
"""

# The prompts are researcher-only and live host-only in the git-ignored private/ directory
# (research/analysis/private_material.py); ``from . import prompts`` keeps working.
from ..private_material import install_module as _install_private_module

_install_private_module(__name__ + ".prompts", "research/analysis/simple_judge/prompts.py")
