"""Pure numeric kernels for training-load computation.

Three functions, no I/O:

- ``compute_normalized_power(watts)`` — Coggan NP with spec-compliant gap
  handling (no synthetic zeros).
- ``compute_power_tss(np_watts, ftp, duration_seconds)`` — Coggan TSS.
- ``compute_hr_tss(avg_hr, lthr, duration_seconds)`` — TrainingPeaks hrTSS.

The NP path differs from ``stream_analysis.normalized_power`` (which treats
``None`` as zero — fine for power-curve work, wrong for TSS). For TSS we
must not invent samples that weren't recorded; the IF→TSS relationship is
sensitive to data integrity in a way that the power curve is not.
"""

from __future__ import annotations

from typing import Any

NP_WINDOW_SECONDS = 30
# Gaps <10 samples interpolate linearly between flanking values.
# Gaps ≥10 samples are left as null and excluded from any rolling window.
GAP_INTERP_THRESHOLD = 10
# If total gap time exceeds this fraction of the activity, emit a warning.
GAP_WARNING_FRACTION = 0.05


def compute_normalized_power(
    watts: list[float | int | None],
    sample_interval_s: int = 1,
) -> tuple[float | None, dict[str, Any]]:
    """Coggan normalized power with spec-compliant gap handling.

    Returns ``(np_watts | None, info)``. ``info`` always populated::

        {
            "valid_samples": int,         # non-null after gap handling
            "small_gap_seconds": int,     # interpolated
            "large_gap_seconds": int,     # excluded
            "rolling_windows": int,       # complete 30s windows used
            "warnings": list[str],
        }

    Returns ``(None, info)`` if fewer than 30 valid samples or no complete
    30s window free of large gaps.
    """
    info: dict[str, Any] = {
        "valid_samples": 0,
        "small_gap_seconds": 0,
        "large_gap_seconds": 0,
        "rolling_windows": 0,
        "warnings": [],
    }

    if not watts:
        info["warnings"].append("watts stream is empty")
        return None, info

    processed = _handle_gaps(list(watts), info)
    info["valid_samples"] = sum(1 for w in processed if w is not None)

    total_samples = len(processed)
    total_gap_samples = info["small_gap_seconds"] + info["large_gap_seconds"]
    if total_samples > 0:
        gap_fraction = total_gap_samples / total_samples
        if gap_fraction > GAP_WARNING_FRACTION:
            total_gap_s = total_gap_samples * sample_interval_s
            activity_s = total_samples * sample_interval_s
            info["warnings"].append(
                f"gap duration {total_gap_s}s is {gap_fraction:.1%} of "
                f"activity duration {activity_s}s (>5% threshold)"
            )

    if info["valid_samples"] < NP_WINDOW_SECONDS:
        info["warnings"].append(
            f"only {info['valid_samples']} valid samples; need at least "
            f"{NP_WINDOW_SECONDS} for normalized-power computation"
        )
        return None, info

    rolling = _rolling_avg_skipping_nulls(processed, NP_WINDOW_SECONDS)
    info["rolling_windows"] = len(rolling)
    if not rolling:
        info["warnings"].append(
            "no complete 30-second windows free of large gaps"
        )
        return None, info

    mean_fourth = sum(r**4 for r in rolling) / len(rolling)
    return mean_fourth**0.25, info


def _handle_gaps(
    watts: list[float | int | None],
    info: dict[str, Any],
) -> list[float | None]:
    """Walk the series, interpolate short gaps, leave long gaps as None.

    Mutates ``info`` to accumulate ``small_gap_seconds`` and
    ``large_gap_seconds``. Returns a new list of floats / Nones.
    """
    n = len(watts)
    out: list[float | None] = [
        float(w) if w is not None else None for w in watts
    ]

    i = 0
    while i < n:
        if out[i] is not None:
            i += 1
            continue
        gap_start = i
        while i < n and out[i] is None:
            i += 1
        gap_end = i  # exclusive
        gap_len = gap_end - gap_start

        if gap_len < GAP_INTERP_THRESHOLD:
            left = out[gap_start - 1] if gap_start > 0 else None
            right = out[gap_end] if gap_end < n else None
            if left is not None and right is not None:
                step = (right - left) / (gap_len + 1)
                for j in range(gap_len):
                    out[gap_start + j] = left + step * (j + 1)
                info["small_gap_seconds"] += gap_len
            else:
                # Edge-of-stream small gap — can't interpolate one-sided;
                # treat as large for safety.
                info["large_gap_seconds"] += gap_len
        else:
            info["large_gap_seconds"] += gap_len

    return out


def _rolling_avg_skipping_nulls(
    processed: list[float | None],
    window: int,
) -> list[float]:
    """30-sec rolling averages; emit one only when the window contains no None.

    Maintains a sliding sum + null count for O(n) total work. Windows that
    straddle a large gap produce no output for that position — the window
    moves on without contributing.
    """
    n = len(processed)
    if n < window:
        return []

    rolling: list[float] = []
    none_count = sum(1 for x in processed[:window] if x is None)
    window_sum = sum(x for x in processed[:window] if x is not None)

    if none_count == 0:
        rolling.append(window_sum / window)

    for i in range(window, n):
        out_v = processed[i - window]
        in_v = processed[i]
        if out_v is None:
            none_count -= 1
        else:
            window_sum -= out_v
        if in_v is None:
            none_count += 1
        else:
            window_sum += in_v
        if none_count == 0:
            rolling.append(window_sum / window)

    return rolling


def compute_power_tss(
    np_watts: float, ftp: float, duration_seconds: int
) -> tuple[float, float]:
    """Coggan TSS: ``(seconds * NP * IF) / (FTP * 3600) * 100``.

    Returns ``(tss, intensity_factor)``. Caller is responsible for ensuring
    ``ftp > 0`` and ``np_watts >= 0``.
    """
    intensity_factor = np_watts / ftp
    tss = (duration_seconds * np_watts * intensity_factor) / (ftp * 3600) * 100
    return tss, intensity_factor


def compute_hr_tss(
    avg_hr: float, lthr: float, duration_seconds: int
) -> tuple[float, float]:
    """TrainingPeaks hrTSS: ``(seconds * IF^2 * 100) / 3600`` where
    ``IF = avg_hr / LTHR``.

    Uses the activity's per-activity ``average_heartrate`` scalar from
    Strava — this is by design. Re-computing avg HR from the time-series
    would silently disagree with Strava's UI without buying any accuracy.

    Returns ``(tss, intensity_factor)``.
    """
    intensity_factor = avg_hr / lthr
    tss = (duration_seconds * intensity_factor**2 * 100) / 3600
    return tss, intensity_factor
