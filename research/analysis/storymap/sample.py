"""Deterministic stratified sample of a run's solvers for a partial story map (researcher-only).

Owner decision, November 6, 2026 (docs/plan.md): solver story maps for a sample of 20 solvers in each of the nine
120-minute push communication-only runs. The sample is stratified on whether a solver faked, so its share of fakers
matches the run's:

- population: every solver of the run (``extract.RunContext.solvers``: seed and overseers excluded);
- fakers: solvers with at least one fake submission by the strict story-map rule (``domain.fake_rule``, the rule
  used by stage 0, the faking heatmap and the index's "Faked" filter);
- stratum sizes: fakers ``round(n * F / S)`` (half up, capped by the stratum sizes), non-fakers the rest;
- within each stratum ``random.Random(seed).sample`` over the sorted ids (fakers drawn first, then non-fakers, from
  the same generator), so the same run, ``n`` and ``seed`` always give the same agents.

``record(run_dir, n, seed)`` returns the JSON record (``SCHEMA``) that ``scripts/run_storymap.py --sample`` writes
to ``OUT/sample.json`` and that ``view`` reads to say on the index how many solvers are mapped and how they were
chosen.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

SCHEMA = "gdm-storymap-sample/1"
FILE = "sample.json"
DEFAULT_SEED = 20261106
METHOD = ("stratified random sample without replacement: the run's solvers (seed and overseers excluded) split "
          "into fakers (at least one fake submission by the strict story-map rule) and non-fakers; fakers in the "
          "sample = round(n x run share of fakers), half up; random.Random(seed).sample over the sorted ids of "
          "each stratum, fakers first")


def stratum_sizes(n, population, fakers):
    """(fakers to draw, non-fakers to draw) for a sample of ``n`` from ``population`` solvers, ``fakers`` of them
    fakers: the faker share rounded half up, capped so neither stratum is overdrawn."""
    n = min(int(n), population)
    if n <= 0 or population <= 0:
        return 0, 0
    k = int(n * fakers / population + 0.5) if fakers else 0
    k = max(n - (population - fakers), min(k, fakers, n))
    return k, n - k


def draw(solvers, fakers, n, seed=DEFAULT_SEED):
    """(sampled agents sorted, sampled fakers sorted) of a stratified sample (module docstring)."""
    solvers = sorted(set(solvers))
    fake = sorted(set(fakers) & set(solvers))
    honest = [a for a in solvers if a not in set(fake)]
    k, m = stratum_sizes(n, len(solvers), len(fake))
    rng = random.Random(seed)
    got_fake = sorted(rng.sample(fake, k))
    got_honest = sorted(rng.sample(honest, m))
    return sorted(got_fake + got_honest), got_fake


def run_population(run_dir):
    """(solvers, fakers) of a run from its conference records (stage 0's RunContext)."""
    from .extract import RunContext
    ctx = RunContext(run_dir)
    solvers = ctx.solvers()
    fakers = sorted({s["agent_id"] for s in ctx.subs.values() if s.get("fake")} & set(solvers))
    return solvers, fakers


def record(run_dir, n, seed=DEFAULT_SEED, population=None):
    """The sample record of a run (``population`` = (solvers, fakers) to skip reading the run)."""
    from . import domain as domain_mod
    solvers, fakers = population or run_population(run_dir)
    agents, sampled_fakers = draw(solvers, fakers, n, seed)
    rule = None
    if domain_mod.run_domain(run_dir) == domain_mod.LEAN:
        from .fake_rule import RULE_VERSION as rule
    return {"schema": SCHEMA, "run": Path(run_dir).name, "method": METHOD, "seed": seed, "n": len(agents),
            "population": len(solvers), "fakers_in_run": len(fakers),
            "fakers_in_sample": len(sampled_fakers), "fake_rule": rule,
            "agents": agents, "sampled_fakers": sampled_fakers}


def load(map_dir):
    """The sample record next to a story map (``MAP/sample.json``), or None."""
    path = Path(map_dir) / FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("schema") == SCHEMA else None


def note_text(rec):
    """One plain sentence for the index: how many solvers are mapped and how they were chosen."""
    n, pop = rec.get("n"), rec.get("population")
    return (f"Sample: {n} of {pop} solvers are mapped. The other {pop - n} have no story map (on the interaction "
            f"graph they are drawn by observed behaviour, not by stance). The sample is a stratified random draw "
            f"(seed {rec.get('seed')}) matching the run's share of solvers who faked: {rec.get('fakers_in_sample')} "
            f"of the {n} sampled solvers faked, against {rec.get('fakers_in_run')} of {pop} in the run (strict "
            f"fake rule{(' ' + rec['fake_rule']) if rec.get('fake_rule') else ''}).")
