"""Phase 3 tests: EWMA fitness curve, forecast, summary, end-to-end synthetic."""

from __future__ import annotations

import math
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from strava_mcp_vault.cache.db import CacheDB
from strava_mcp_vault.training_load import curve, load


USER_ID = 12110346


@pytest_asyncio.fixture
async def conn(tmp_path):
    cdb = CacheDB(str(tmp_path / "test.db"))
    await cdb.init()
    yield cdb._db
    await cdb.close()


# ── Pure EWMA kernels ────────────────────────────────────────────────────


def test_ewma_step_zero_input_decays_toward_zero():
    """With TSS=0, prev decays by (1-k) each step."""
    prev = 100.0
    new = curve.ewma_step(prev, tss=0.0, k=curve.K_CTL)
    assert abs(new - prev * (1 - curve.K_CTL)) < 1e-9


def test_ewma_step_constant_input_converges_to_input():
    """With constant TSS, prev approaches TSS — at infinity equals TSS."""
    val = 100.0
    for _ in range(1000):
        val = curve.ewma_step(val, tss=100.0, k=curve.K_CTL)
    assert abs(val - 100.0) < 0.01


def test_k_constants_match_spec():
    """k = 1 - exp(-1/τ) per spec. CTL τ=42, ATL τ=7."""
    assert abs(curve.K_CTL - (1 - math.exp(-1 / 42))) < 1e-12
    assert abs(curve.K_ATL - (1 - math.exp(-1 / 7))) < 1e-12


# ── compute_series ───────────────────────────────────────────────────────


def test_series_empty_inputs_zero_throughout():
    """No activities → CTL/ATL/TSB all 0."""
    series = curve.compute_series({}, {}, "2026-01-01", "2026-01-03", warmup_days=0)
    for day in series:
        assert day["tss"] == 0.0
        assert day["ctl"] == 0.0
        assert day["atl"] == 0.0
        assert day["tsb"] == 0.0
        assert day["activity_count"] == 0


def test_series_trims_to_requested_range():
    series = curve.compute_series({}, {}, "2026-01-05", "2026-01-08", warmup_days=10)
    dates = [d["date"] for d in series]
    assert dates == ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]


def test_series_single_activity_appears_on_correct_day():
    series = curve.compute_series(
        {"2026-01-05": 50.0},
        {"2026-01-05": 1},
        "2026-01-05", "2026-01-05",
        warmup_days=0,
    )
    assert len(series) == 1
    assert series[0]["tss"] == 50.0
    assert series[0]["activity_count"] == 1


# ── End-to-end reference: 90 days of TSS=100 from cold start ─────────────


def test_90_days_constant_tss_matches_closed_form():
    """Hand-computed reference: 90 days of TSS=100 from CTL=ATL=0.

    Closed form: CTL[N] = 100 * (1 - exp(-N/42)).
        CTL[90] = 100 * (1 - exp(-90/42)) ≈ 88.25
        ATL[90] = 100 * (1 - exp(-90/7)) ≈ 100 (fully converged)
        TSB[90] = CTL[89] - ATL[89] ≈ 87.97 - 100 = -12.03
    """
    tss_by_date = {}
    count_by_date = {}
    start_d = date(2026, 1, 1)
    for i in range(90):
        d = (start_d + timedelta(days=i)).isoformat()
        tss_by_date[d] = 100.0
        count_by_date[d] = 1

    end_d = start_d + timedelta(days=89)
    series = curve.compute_series(
        tss_by_date, count_by_date,
        start_d.isoformat(), end_d.isoformat(),
        warmup_days=0,  # cold start
    )

    last = series[-1]
    expected_ctl = 100.0 * (1 - math.exp(-90 / 42))
    expected_atl = 100.0 * (1 - math.exp(-90 / 7))
    expected_tsb = (
        100.0 * (1 - math.exp(-89 / 42))
        - 100.0 * (1 - math.exp(-89 / 7))
    )
    assert abs(last["ctl"] - expected_ctl) < 0.01
    assert abs(last["atl"] - expected_atl) < 0.01
    assert abs(last["tsb"] - expected_tsb) < 0.01
    # Sanity-check the reference numbers themselves.
    assert abs(expected_ctl - 88.25) < 0.05
    assert expected_atl > 99.99
    assert abs(expected_tsb - (-12.03)) < 0.05


def test_warmup_lets_ctl_converge_before_start():
    """With long warmup of constant TSS, CTL is already near steady at start."""
    tss = {}
    count = {}
    full_range_start = date(2025, 1, 1)
    for i in range(365):
        d = (full_range_start + timedelta(days=i)).isoformat()
        tss[d] = 100.0
        count[d] = 1

    # Request only Dec 31 2025; with 180-day warmup, CTL should be very near 100.
    series = curve.compute_series(
        tss, count, "2025-12-31", "2025-12-31", warmup_days=180,
    )
    assert len(series) == 1
    assert series[0]["ctl"] > 98.5  # ~98.6% converged after 180d


# ── forecast_decay ───────────────────────────────────────────────────────


def test_forecast_decay_zero_state_stays_zero():
    out = curve.forecast_decay(0.0, 0.0, days=7)
    assert len(out) == 7
    assert all(d["ctl"] == 0.0 and d["atl"] == 0.0 for d in out)


def test_forecast_decay_atl_falls_faster_than_ctl():
    """7-day τ means ATL decays much faster than 42-day τ CTL."""
    out = curve.forecast_decay(100.0, 100.0, days=7)
    # After 7 days, ATL should be ~37% of start; CTL ~85%.
    assert out[-1]["atl"] < 50.0
    assert out[-1]["ctl"] > 80.0
    # TSB on day 1 = previous (last_ctl) - (last_atl) = 0.
    assert abs(out[0]["tsb"]) < 1e-9
    # By day 7, TSB is positive (resting raises form).
    assert out[-1]["tsb"] > 0


def test_forecast_decay_matches_closed_form():
    """N days of zero TSS from (ctl0, atl0): value[N] = value[0] * (1-k)^N."""
    out = curve.forecast_decay(100.0, 50.0, days=14)
    expected_ctl_14 = 100.0 * (1 - curve.K_CTL) ** 14
    expected_atl_14 = 50.0 * (1 - curve.K_ATL) ** 14
    assert abs(out[-1]["ctl"] - expected_ctl_14) < 0.01
    assert abs(out[-1]["atl"] - expected_atl_14) < 0.01


# ── summarize_period ─────────────────────────────────────────────────────


def test_summary_empty_series_returns_zeros():
    s = curve.summarize_period([])
    assert s["days"] == 0
    assert s["total_tss"] == 0.0
    assert s["peak_atl_date"] is None


def test_summary_basic_aggregation():
    series = [
        {"date": "2026-01-01", "tss": 50.0, "ctl": 5.0, "atl": 10.0, "tsb": 0.0, "activity_count": 1},
        {"date": "2026-01-02", "tss": 100.0, "ctl": 8.0, "atl": 22.0, "tsb": -5.0, "activity_count": 2},
        {"date": "2026-01-03", "tss": 0.0, "ctl": 9.0, "atl": 19.0, "tsb": -14.0, "activity_count": 0},
    ]
    s = curve.summarize_period(series)
    assert s["days"] == 3
    assert s["total_tss"] == 150.0
    assert abs(s["avg_tss_per_day"] - 50.0) < 1e-9
    assert s["total_activities"] == 3
    assert s["ctl_start"] == 5.0
    assert s["ctl_end"] == 9.0
    assert s["ctl_change"] == 4.0
    assert s["peak_atl"] == 22.0
    assert s["peak_atl_date"] == "2026-01-02"


# ── Orchestrator (compute_fitness_curve with synthetic activities) ───────


async def _insert_activity_load(conn, activity_id, user_id, date_str, tss):
    """Insert a pre-computed activity_load row so the walk hits cache."""
    import json
    import time as _time
    inputs_used = {"method": "synthetic"}
    h = load._inputs_hash(inputs_used)
    await conn.execute("""
        INSERT INTO activity_load
            (activity_id, inputs_hash, user_id, date, duration_seconds,
             tss, np_watts, intensity_factor, method, inputs_used,
             warnings, computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        activity_id, h, user_id, date_str, 3600,
        tss, None, None, "power",
        json.dumps(inputs_used), json.dumps([]), _time.time(),
    ))
    # Also need a row in activities so the walk finds it.
    activity_json = json.dumps({"id": activity_id, "start_date_local": date_str})
    await conn.execute("""
        INSERT INTO activities
            (id, data, start_date, start_date_local, sport_type, synced_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (activity_id, activity_json, date_str, date_str, "Ride", _time.time()))
    await conn.commit()


@pytest.mark.asyncio
async def test_compute_fitness_curve_end_to_end_90_days(conn):
    """End-to-end: 90 synthetic activities (TSS=100 each) → CTL[90]≈88.25."""
    # Patch compute_activity_load to return synthetic results without
    # needing a real CacheManager. The cache row pattern lets us also
    # exercise the activity-walking path.
    start_d = date(2026, 1, 1)
    for i in range(90):
        d = (start_d + timedelta(days=i)).isoformat()
        activity_id = 100000 + i
        await _insert_activity_load(conn, activity_id, USER_ID, d, 100.0)

    # Mock manager.db to point at our test conn, and stub
    # get_athlete_profile (not used here but defensive).
    manager = AsyncMock()
    manager.db = AsyncMock()

    # The walk uses manager.db.get_vault_activities, which our test DB
    # implements naturally. Bind it directly to the real method.
    cdb = CacheDB(":memory:")  # placeholder
    cdb._db = conn  # point at our fixture conn
    manager.db.get_vault_activities = cdb.get_vault_activities

    # Patch compute_activity_load to bypass the manager.get_activity /
    # streams path and return the cached row's TSS directly.
    async def fake_compute(conn_arg, mgr_arg, activity_id, user_id):
        return {
            "activity_id": activity_id,
            "date": (start_d + timedelta(days=activity_id - 100000)).isoformat(),
            "duration_seconds": 3600,
            "tss": 100.0,
            "np_watts": None,
            "intensity_factor": None,
            "method": "synthetic",
            "inputs_used": {"method": "synthetic"},
            "warnings": [],
        }

    with patch.object(load, "compute_activity_load", fake_compute):
        end_d = start_d + timedelta(days=89)
        series = await load.compute_fitness_curve(
            conn, manager, USER_ID,
            start_d.isoformat(), end_d.isoformat(),
            warmup_days=0,
        )

    assert len(series) == 90
    last = series[-1]
    expected_ctl = 100.0 * (1 - math.exp(-90 / 42))
    expected_atl = 100.0 * (1 - math.exp(-90 / 7))
    assert abs(last["ctl"] - expected_ctl) < 0.01
    assert abs(last["atl"] - expected_atl) < 0.01
    assert last["activity_count"] == 1
    assert last["tss"] == 100.0


# ── get_training_load_today + get_load_summary smoke tests ───────────────


@pytest.mark.asyncio
async def test_today_returns_forecast(conn):
    """No activities → today/forecast all zeros."""
    manager = AsyncMock()
    manager.db = AsyncMock()
    cdb = CacheDB(":memory:")
    cdb._db = conn
    manager.db.get_vault_activities = cdb.get_vault_activities

    async def fake_compute(*args, **kwargs):
        return None  # never called

    with patch.object(load, "compute_activity_load", fake_compute):
        result = await load.get_training_load_today(
            conn, manager, USER_ID, forecast_days=7, warmup_days=0,
        )
    assert "forecast_7_day" in result
    assert len(result["forecast_7_day"]) == 7
    assert result["ctl"] == 0.0


@pytest.mark.asyncio
async def test_summary_bad_period_rejected(conn):
    manager = AsyncMock()
    manager.db = AsyncMock()
    with pytest.raises(ValueError, match="period"):
        await load.get_load_summary(conn, manager, USER_ID, period="decade")
