"""ROC AUC and threshold sweep of monitor scores (pure functions; scores are integers 0-100).

The decision rule is "block when score >= threshold". For threshold t: FPR = share of negatives with score >= t,
recall = share of positives with score >= t. Unscored calls (parse failure, monitor error) are let through by the
live rule, so they count as score -1 (never blocked) in every rate here; their number is reported separately.
"""
from __future__ import annotations

import statistics

UNSCORED = -1


def roc_auc(pos, neg):
    """Mann-Whitney AUC (ties count one half); None when either side is empty."""
    if not pos or not neg:
        return None
    neg_sorted = sorted(neg)
    import bisect
    total = 0.0
    for p in pos:
        lo = bisect.bisect_left(neg_sorted, p)
        hi = bisect.bisect_right(neg_sorted, p)
        total += lo + 0.5 * (hi - lo)
    return total / (len(pos) * len(neg))


def sweep(pos, neg, thresholds=range(0, 101)):
    rows = []
    for t in thresholds:
        fp = sum(1 for s in neg if s >= t)
        tp = sum(1 for s in pos if s >= t)
        rows.append({"threshold": t, "fpr": fp / len(neg) if neg else None,
                     "recall": tp / len(pos) if pos else None, "fp": fp, "tp": tp})
    return rows


def lowest_threshold(rows, max_fpr):
    """The lowest integer threshold whose FPR is at most ``max_fpr`` (None when none is)."""
    for row in rows:
        if row["fpr"] is not None and row["fpr"] <= max_fpr:
            return row
    return None


def distribution(scores):
    scored = [s for s in scores if s != UNSCORED]
    if not scored:
        return {"n": len(scores), "scored": 0}
    deciles = statistics.quantiles(scored, n=10, method="inclusive") if len(scored) > 1 else [scored[0]] * 9
    buckets = {f"{lo}-{lo + 9 if lo < 90 else 100}": sum(1 for s in scored if lo <= s <= (lo + 9 if lo < 90 else 100))
               for lo in range(0, 100, 10)}
    return {"n": len(scores), "scored": len(scored), "mean": round(statistics.fmean(scored), 2),
            "median": statistics.median(scored), "p90": deciles[8], "max": max(scored), "min": min(scored),
            "histogram": buckets}
