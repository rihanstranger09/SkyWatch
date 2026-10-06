"""Cross-epoch change detection for the SkyWatch ISR processing line.

Two collections of the same ground, processed by the same worker, can be compared
band-for-band: the difference between their vegetation-index grids is a screening
signal for new activity, new clearance, new water or recovery after an event.

This is a *relative* comparison - both grids must share a grid definition
(CRS, transform and shape) to be comparable, which the worker guarantees because
it writes every product from the same input profile. Mixed-resolution inputs are
rejected rather than silently resampled, so a change figure is never quietly
meaningless.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np

#: Absolute index delta that counts as a change between epochs.
CHANGE_THRESHOLD = 0.15
#: Fraction of the scene that must differ before the change is called significant.
SIGNIFICANT_PCT = 2.0


def compare(
    previous: np.ndarray,
    current: np.ndarray,
    threshold: float = CHANGE_THRESHOLD,
    significant_pct: float = SIGNIFICANT_PCT,
) -> Dict[str, Any]:
    """Compare two index grids and summarise what moved.

    Returns percentage of scene area that lost index value (``lossPct`` - e.g.
    canopy removed, ground drying), that gained (``gainPct`` - vegetation
    growth, water arriving), the mean signed delta, and a screening verdict.
    """
    a = np.asarray(previous, dtype="float32")
    b = np.asarray(current, dtype="float32")
    if a.shape != b.shape:
        raise ValueError(f"grids are not comparable: {a.shape} vs {b.shape}")

    valid = np.isfinite(a) & np.isfinite(b)
    total = int(np.count_nonzero(valid))
    if total == 0:
        return {"status": "NO OVERLAP", "changedPct": 0.0, "lossPct": 0.0, "gainPct": 0.0,
                "meanDelta": None, "threshold": threshold, "comparedPixelPct": 0.0}

    delta = np.zeros_like(a)
    delta[valid] = b[valid] - a[valid]
    loss = valid & (delta <= -threshold)
    gain = valid & (delta >= threshold)
    changed = loss | gain

    pct = lambda mask: round(100.0 * float(np.count_nonzero(mask)) / total, 2)  # noqa: E731
    changed_pct, loss_pct, gain_pct = pct(changed), pct(loss), pct(gain)
    mean_delta = round(float(delta[valid].mean()), 4)

    if changed_pct < significant_pct:
        verdict, note = "NO SIGNIFICANT CHANGE", f"{changed_pct:.1f}% of scene moved beyond ±{threshold}"
    elif loss_pct >= gain_pct * 2:
        verdict, note = "SURFACE LOSS", f"{loss_pct:.1f}% of scene lost index value"
    elif gain_pct >= loss_pct * 2:
        verdict, note = "SURFACE GAIN", f"{gain_pct:.1f}% of scene gained index value"
    else:
        verdict, note = "MIXED CHANGE", f"gain {gain_pct:.1f}% vs loss {loss_pct:.1f}%"

    return {
        "status": verdict,
        "note": note,
        "changedPct": changed_pct,
        "lossPct": loss_pct,
        "gainPct": gain_pct,
        "meanDelta": mean_delta,
        "threshold": threshold,
        "comparedPixelPct": round(100.0 * total / a.size, 2),
    }


def compare_stats(previous_stats: Dict[str, Optional[float]], current_stats: Dict[str, Optional[float]]) -> Dict[str, Any]:
    """The cheap companion to :func:`compare`: how the scene statistics moved."""
    def delta(key: str) -> Optional[float]:
        before, after = previous_stats.get(key), current_stats.get(key)
        if before is None or after is None:
            return None
        return round(float(after) - float(before), 4)

    return {
        "meanDelta": delta("mean"),
        "maxDelta": delta("max"),
        "validPixelPctDelta": delta("valid_pixel_pct"),
    }
