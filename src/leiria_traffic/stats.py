"""Distribution summaries over observed travel times."""
from __future__ import annotations

import math
import statistics


def percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolation percentile (same as numpy's default) on pre-sorted data."""
    if not sorted_values:
        raise ValueError("no values")
    if not 0 <= q <= 100:
        raise ValueError("q must be in [0, 100]")
    pos = (len(sorted_values) - 1) * q / 100
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def summarize(values: list[float]) -> dict | None:
    """P50/P75/P90 plus mean, std, min, max and n. None when there is no data."""
    if not values:
        return None
    s = sorted(values)
    return {
        "n": len(s),
        "p50": percentile(s, 50),
        "p75": percentile(s, 75),
        "p90": percentile(s, 90),
        "mean": statistics.fmean(s),
        "std": statistics.stdev(s) if len(s) > 1 else 0.0,
        "min": s[0],
        "max": s[-1],
    }
