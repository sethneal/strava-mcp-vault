"""Phase 2 unit tests: pure numeric kernels (NP gap handling, power TSS, hrTSS)."""

from __future__ import annotations

import pytest

from strava_mcp_vault.training_load import calc


# ── compute_normalized_power ─────────────────────────────────────────────


def test_np_empty_returns_none():
    np, info = calc.compute_normalized_power([])
    assert np is None
    assert "empty" in info["warnings"][0].lower()


def test_np_constant_stream_equals_constant():
    """60s at constant 200W → NP = 200 (all 30s windows average to 200)."""
    np, info = calc.compute_normalized_power([200] * 60)
    assert np is not None
    assert abs(np - 200.0) < 0.01
    assert info["valid_samples"] == 60
    assert info["small_gap_seconds"] == 0
    assert info["large_gap_seconds"] == 0
    assert info["warnings"] == []


def test_np_too_short_returns_none():
    """Fewer than 30 valid samples → cannot compute, returns None."""
    np, info = calc.compute_normalized_power([200] * 25)
    assert np is None
    assert any("30" in w for w in info["warnings"])


def test_np_variable_higher_than_average():
    """Coggan NP weights high efforts more — variable > flat with same mean.

    Sub-window variability (1Hz oscillation) is invisible after the 30s
    rolling average. To make NP > mean, we need variation that survives
    smoothing — long blocks at different intensities.
    """
    # 5 minutes at 100W, 5 minutes at 300W. Mean 200W; NP > 200.
    watts = [100] * 300 + [300] * 300
    np, _ = calc.compute_normalized_power(watts)
    assert np is not None
    assert np > 220.0  # measurably higher than the 200W mean


def test_np_small_gap_interpolated():
    """A 5-sample gap inside otherwise-flat stream gets interpolated."""
    # 30s @ 200W, 5s None, 30s @ 200W. Interp fills with 200s. NP ~= 200.
    watts: list[int | None] = [200] * 30 + [None] * 5 + [200] * 30
    np, info = calc.compute_normalized_power(watts)
    assert np is not None
    assert abs(np - 200.0) < 0.5
    assert info["small_gap_seconds"] == 5
    assert info["large_gap_seconds"] == 0


def test_np_large_gap_excluded():
    """A 15-sample gap (≥10) is left as null; affected windows are skipped."""
    # 60s @ 200W, 15s None, 60s @ 200W. NP from valid windows ~ 200.
    watts: list[int | None] = [200] * 60 + [None] * 15 + [200] * 60
    np, info = calc.compute_normalized_power(watts)
    assert np is not None
    assert abs(np - 200.0) < 0.5
    assert info["large_gap_seconds"] == 15
    assert info["small_gap_seconds"] == 0


def test_np_gap_warning_at_5pct_threshold():
    """Total gap >5% of activity duration triggers a warning."""
    # 90s activity, 6s gap = 6.7% → warning. Use 15s gap so it's "large".
    watts: list[int | None] = [200] * 60 + [None] * 15 + [200] * 30
    np, info = calc.compute_normalized_power(watts)
    assert np is not None
    assert any("5%" in w for w in info["warnings"])


def test_np_no_gap_warning_below_threshold():
    """3% of activity in gaps → no gap warning."""
    # 300s activity, 6s small gap = 2%. Interpolated, below threshold.
    watts: list[int | None] = [200] * 60 + [None] * 6 + [200] * 234
    _, info = calc.compute_normalized_power(watts)
    assert not any("5%" in w for w in info["warnings"])


def test_np_leading_gap_treated_as_large():
    """A null run at the very start can't be interpolated → counts as large."""
    watts: list[int | None] = [None] * 5 + [200] * 60
    _, info = calc.compute_normalized_power(watts)
    # 5 leading nulls with no left flank — treated as large.
    assert info["large_gap_seconds"] == 5
    assert info["small_gap_seconds"] == 0


def test_np_all_nulls_returns_none():
    np, info = calc.compute_normalized_power([None] * 100)
    assert np is None
    assert info["valid_samples"] == 0


def test_np_does_not_treat_nulls_as_zeros():
    """Regression: nulls must not be silently coerced to 0 (would lower NP)."""
    # 30s @ 200W, 30s None (large gap excluded), 30s @ 200W.
    # If nulls were treated as zeros, NP would drop below 200.
    watts: list[int | None] = [200] * 30 + [None] * 30 + [200] * 30
    np, _ = calc.compute_normalized_power(watts)
    assert np is not None
    assert np > 195.0  # close to 200, not closer to mean-with-zeros (~133)


# ── compute_power_tss ────────────────────────────────────────────────────


def test_power_tss_1h_at_ftp_equals_100():
    """1 hour at IF=1.0 (NP=FTP) is 100 TSS by definition."""
    tss, if_ = calc.compute_power_tss(np_watts=250, ftp=250, duration_seconds=3600)
    assert abs(tss - 100.0) < 0.01
    assert abs(if_ - 1.0) < 0.001


def test_power_tss_2h_at_ftp_equals_200():
    tss, _ = calc.compute_power_tss(np_watts=250, ftp=250, duration_seconds=7200)
    assert abs(tss - 200.0) < 0.01


def test_power_tss_1h_at_half_intensity():
    """1h at IF=0.5 → TSS = (3600 * 125 * 0.5) / (250 * 3600) * 100 = 25."""
    tss, if_ = calc.compute_power_tss(np_watts=125, ftp=250, duration_seconds=3600)
    assert abs(tss - 25.0) < 0.01
    assert abs(if_ - 0.5) < 0.001


def test_power_tss_above_ftp():
    """30 min at IF=1.1 → ~60.5 TSS."""
    tss, if_ = calc.compute_power_tss(np_watts=275, ftp=250, duration_seconds=1800)
    assert abs(if_ - 1.1) < 0.001
    # 1800 * 275 * 1.1 / (250 * 3600) * 100 = 60.5
    assert abs(tss - 60.5) < 0.1


# ── compute_hr_tss ───────────────────────────────────────────────────────


def test_hr_tss_1h_at_lthr_equals_100():
    """1h at avg_hr=LTHR → IF=1.0, TSS=100."""
    tss, if_ = calc.compute_hr_tss(avg_hr=160, lthr=160, duration_seconds=3600)
    assert abs(tss - 100.0) < 0.01
    assert abs(if_ - 1.0) < 0.001


def test_hr_tss_zone_2_intensity():
    """1h at avg_hr=128 LTHR=160 → IF=0.8, TSS = 3600 * 0.64 * 100 / 3600 = 64."""
    tss, if_ = calc.compute_hr_tss(avg_hr=128, lthr=160, duration_seconds=3600)
    assert abs(if_ - 0.8) < 0.001
    assert abs(tss - 64.0) < 0.01


def test_hr_tss_short_easy_ride():
    """30 min at avg_hr=120 LTHR=160 → IF=0.75, TSS = 1800 * 0.5625 * 100 / 3600 = 28.125."""
    tss, if_ = calc.compute_hr_tss(avg_hr=120, lthr=160, duration_seconds=1800)
    assert abs(if_ - 0.75) < 0.001
    assert abs(tss - 28.125) < 0.01
