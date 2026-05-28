"""EWMA fitness curve, forecast, and period summary — pure kernels.

Standard Coggan / TrainingPeaks model:

- CTL (chronic training load) = 42-day exponentially-weighted TSS average.
  "Fitness."
- ATL (acute training load) = 7-day EWMA. "Fatigue."
- TSB (training stress balance) = yesterday's CTL minus yesterday's ATL.
  "Form" coming into today.

The EWMA constants are computed as ``k = 1 - exp(-1/τ)`` so the response
matches the standard continuous-time first-order impulse response with
time constant τ days.

This module is I/O free. The orchestrator that walks activities, computes
loads, and aggregates per-day TSS lives in ``load.py`` so the EWMA math
stays trivially testable.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

TAU_CTL_DAYS = 42
TAU_ATL_DAYS = 7

K_CTL = 1 - math.exp(-1 / TAU_CTL_DAYS)  # ≈ 0.02353
K_ATL = 1 - math.exp(-1 / TAU_ATL_DAYS)  # ≈ 0.13307

# Conservative default: 180 days at τ=42 is ~4.3 time constants → >98%
# convergence from a zero seed. The earlier "Path A" project used 60 days
# (~1.4τ) which caused cold-start CTL inaccuracy.
DEFAULT_WARMUP_DAYS = 180


def ewma_step(prev: float, tss: float, k: float) -> float:
    """One EWMA update: ``prev * (1 - k) + tss * k``."""
    return prev * (1 - k) + tss * k


def date_range_inclusive(start: str, end: str):
    """Yield ISO date strings from ``start`` to ``end``, both inclusive."""
    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)
    d = start_d
    while d <= end_d:
        yield d.isoformat()
        d = d + timedelta(days=1)


def compute_series(
    tss_by_date: dict[str, float],
    activities_by_date: dict[str, int],
    start_date: str,
    end_date: str,
    warmup_days: int = DEFAULT_WARMUP_DAYS,
) -> list[dict[str, Any]]:
    """Build the daily CTL/ATL/TSB series for ``[start_date, end_date]``.

    Pre-pends ``warmup_days`` before ``start_date`` so CTL/ATL converge
    from a zero seed; the warmup days are discarded from the return value.

    TSB convention: ``TSB[d] = CTL[d-1] - ATL[d-1]`` (yesterday's form
    going into today). TSB on the very first day of the warmup window is 0.

    Inputs are dicts of date → value so callers can build them however
    they want (real activities, synthetic test data, future scenarios).
    """
    warmup_start_d = date.fromisoformat(start_date) - timedelta(days=warmup_days)
    warmup_start = warmup_start_d.isoformat()

    series: list[dict[str, Any]] = []
    # ctl / atl hold yesterday's values at the top of each iteration —
    # which IS the right basis for today's TSB (form coming into today).
    ctl = 0.0
    atl = 0.0

    for iso in date_range_inclusive(warmup_start, end_date):
        tss = tss_by_date.get(iso, 0.0)
        count = activities_by_date.get(iso, 0)
        tsb = ctl - atl  # yesterday's CTL minus yesterday's ATL

        ctl = ewma_step(ctl, tss, K_CTL)
        atl = ewma_step(atl, tss, K_ATL)

        if iso >= start_date:
            series.append({
                "date": iso,
                "tss": tss,
                "ctl": ctl,
                "atl": atl,
                "tsb": tsb,
                "activity_count": count,
            })
    return series


def forecast_decay(
    last_ctl: float,
    last_atl: float,
    days: int,
) -> list[dict[str, Any]]:
    """Project ``days`` of zero-TSS rest forward from the given state.

    Useful for ``get_training_load_today``: "if I rest a full week,
    where does my form land by Sunday?" Returns one entry per day with
    ``day_offset`` (1..days), projected ``ctl``, ``atl``, ``tsb``.

    TSB convention matches ``compute_series``: yesterday's CTL/ATL diff.
    """
    out: list[dict[str, Any]] = []
    ctl, atl = last_ctl, last_atl

    for i in range(1, days + 1):
        tsb = ctl - atl  # yesterday's CTL minus yesterday's ATL
        ctl = ewma_step(ctl, 0.0, K_CTL)
        atl = ewma_step(atl, 0.0, K_ATL)
        out.append({
            "day_offset": i,
            "ctl": ctl,
            "atl": atl,
            "tsb": tsb,
        })
    return out


def summarize_period(series: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate a slice of the daily series into period totals.

    Returns total TSS, average per day, total activity count, CTL at
    start and end (and the delta), peak ATL and the date it occurred on.
    Empty input returns a zeros dict with date fields nulled.
    """
    if not series:
        return {
            "days": 0,
            "total_tss": 0.0,
            "avg_tss_per_day": 0.0,
            "total_activities": 0,
            "ctl_start": 0.0,
            "ctl_end": 0.0,
            "ctl_change": 0.0,
            "peak_atl": 0.0,
            "peak_atl_date": None,
        }
    total_tss = sum(d["tss"] for d in series)
    total_acts = sum(d["activity_count"] for d in series)
    peak_day = max(series, key=lambda d: d["atl"])
    return {
        "days": len(series),
        "total_tss": total_tss,
        "avg_tss_per_day": total_tss / len(series),
        "total_activities": total_acts,
        "ctl_start": series[0]["ctl"],
        "ctl_end": series[-1]["ctl"],
        "ctl_change": series[-1]["ctl"] - series[0]["ctl"],
        "peak_atl": peak_day["atl"],
        "peak_atl_date": peak_day["date"],
    }
