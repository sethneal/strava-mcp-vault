"""Phase 2 orchestrator tests: compute_activity_load with mocked manager."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from strava_mcp_vault.cache.db import CacheDB
from strava_mcp_vault.exceptions import NoMatchingStreamsError
from strava_mcp_vault.training_load import config as tl_config
from strava_mcp_vault.training_load import load


USER_ID = 12110346
ACTIVITY_ID = 18512144816


@pytest_asyncio.fixture
async def conn(tmp_path):
    cdb = CacheDB(str(tmp_path / "test.db"))
    await cdb.init()
    yield cdb._db
    await cdb.close()


def _make_manager(activity: dict, streams: dict | None = None):
    """Build an AsyncMock that mimics CacheManager.get_activity /
    get_streams_normalized. ``streams=None`` makes the stream call raise
    NoMatchingStreamsError, simulating "scalar present but stream absent"."""
    m = AsyncMock()
    m.get_activity = AsyncMock(return_value=activity)
    if streams is None:
        m.get_streams_normalized = AsyncMock(
            side_effect=NoMatchingStreamsError(
                ACTIVITY_ID, {"watts"}, {"distance", "time"}
            )
        )
    else:
        m.get_streams_normalized = AsyncMock(return_value=streams)
    return m


# ── Method selection: power ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_power_only_activity(conn):
    """Activity with watts stream + FTP → method=power, real TSS."""
    await tl_config.set_field(conn, USER_ID, "ftp_watts", 250, "2025-01-01")
    activity = {
        "id": ACTIVITY_ID,
        "start_date_local": "2026-05-15T08:00:00Z",
        "moving_time": 3600,
        "average_watts": 240,
        "has_heartrate": False,
    }
    streams = {"watts": [250] * 3600}  # 1h flat at FTP
    manager = _make_manager(activity, streams)

    result = await load.compute_activity_load(conn, manager, ACTIVITY_ID, USER_ID)

    assert result["method"] == "power"
    assert result["date"] == "2026-05-15"
    assert abs(result["tss"] - 100.0) < 0.5
    assert abs(result["np_watts"] - 250.0) < 0.5
    assert abs(result["intensity_factor"] - 1.0) < 0.01
    assert result["inputs_used"]["ftp_watts"] == 250
    assert result["inputs_used"]["ftp_effective_from"] == "2025-01-01"
    assert result["warnings"] == []


# ── Method selection: hr ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hr_only_activity(conn):
    """No watts stream, has HR + LTHR set → method=hr."""
    await tl_config.set_field(conn, USER_ID, "lthr_bpm", 160, "2025-01-01")
    activity = {
        "id": ACTIVITY_ID,
        "start_date_local": "2026-05-15T08:00:00Z",
        "moving_time": 3600,
        "has_heartrate": True,
        "average_heartrate": 128,  # IF = 0.8
        "average_watts": None,
    }
    manager = _make_manager(activity, streams=None)

    result = await load.compute_activity_load(conn, manager, ACTIVITY_ID, USER_ID)

    assert result["method"] == "hr"
    assert abs(result["intensity_factor"] - 0.8) < 0.001
    assert abs(result["tss"] - 64.0) < 0.01  # 3600 * 0.64 * 100 / 3600
    assert result["np_watts"] is None
    assert result["inputs_used"]["lthr_bpm"] == 160
    # Streams were not fetched (no power candidate).
    manager.get_streams_normalized.assert_not_called()


# ── Method selection: none ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_neither_power_nor_hr_returns_none(conn):
    """No power, no HR → method=none, warnings explain."""
    activity = {
        "id": ACTIVITY_ID,
        "start_date_local": "2026-05-15T08:00:00Z",
        "moving_time": 3600,
        "has_heartrate": False,
        "average_watts": None,
    }
    manager = _make_manager(activity, streams=None)

    result = await load.compute_activity_load(conn, manager, ACTIVITY_ID, USER_ID)

    assert result["method"] == "none"
    assert result["tss"] is None
    assert result["np_watts"] is None
    assert result["intensity_factor"] is None
    assert any("no recorded power" in w for w in result["warnings"])
    assert any("no recorded heart rate" in w for w in result["warnings"])


@pytest.mark.asyncio
async def test_no_config_set_returns_none(conn):
    """Activity has power + HR but no FTP or LTHR resolved → method=none."""
    activity = {
        "id": ACTIVITY_ID,
        "start_date_local": "2026-05-15T08:00:00Z",
        "moving_time": 3600,
        "average_watts": 240,
        "has_heartrate": True,
        "average_heartrate": 140,
    }
    manager = _make_manager(activity, streams={"watts": [240] * 3600})

    result = await load.compute_activity_load(conn, manager, ACTIVITY_ID, USER_ID)

    assert result["method"] == "none"
    assert any("no FTP set" in w for w in result["warnings"])
    assert any("no LTHR set" in w for w in result["warnings"])


# ── Critical: do NOT use Strava's weighted_average_watts as NP ───────────


@pytest.mark.asyncio
async def test_weighted_avg_watts_present_but_no_stream_falls_through(conn):
    """If Strava has weighted_average_watts but no watts stream is fetchable,
    we MUST NOT use the scalar as NP. Fall through to HR or none."""
    await tl_config.set_field(conn, USER_ID, "ftp_watts", 250, "2025-01-01")
    await tl_config.set_field(conn, USER_ID, "lthr_bpm", 160, "2025-01-01")
    activity = {
        "id": ACTIVITY_ID,
        "start_date_local": "2026-05-15T08:00:00Z",
        "moving_time": 3600,
        "average_watts": 200,  # scalar present
        "weighted_average_watts": 220,  # Strava's own NP — must be ignored
        "has_heartrate": True,
        "average_heartrate": 140,
    }
    # streams=None → NoMatchingStreamsError on fetch
    manager = _make_manager(activity, streams=None)

    result = await load.compute_activity_load(conn, manager, ACTIVITY_ID, USER_ID)

    # Falls through to HR.
    assert result["method"] == "hr"
    assert result["np_watts"] is None  # not 220 from Strava's scalar
    # IF = 140 / 160 = 0.875, TSS = 3600 * 0.766 * 100 / 3600 = 76.6
    assert abs(result["intensity_factor"] - 0.875) < 0.001


# ── Gap handling end-to-end ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_watts_gap_above_5pct_produces_warning_but_still_computes(conn):
    """Activity with a 6% gap in watts stream → method=power, tss computed,
    warning about gap duration in the warnings list."""
    await tl_config.set_field(conn, USER_ID, "ftp_watts", 250, "2025-01-01")
    # 1000-sample activity, 70s large gap (7%) at the middle.
    watts: list[int | None] = [250] * 465 + [None] * 70 + [250] * 465
    activity = {
        "id": ACTIVITY_ID,
        "start_date_local": "2026-05-15T08:00:00Z",
        "moving_time": 1000,
        "average_watts": 250,
        "has_heartrate": False,
    }
    manager = _make_manager(activity, streams={"watts": watts})

    result = await load.compute_activity_load(conn, manager, ACTIVITY_ID, USER_ID)

    assert result["method"] == "power"
    assert result["tss"] is not None
    assert any("5%" in w for w in result["warnings"])


# ── Cache behavior ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_hit_avoids_recompute(conn):
    """Second call with the same inputs hits the cache (no stream re-fetch)."""
    await tl_config.set_field(conn, USER_ID, "ftp_watts", 250, "2025-01-01")
    activity = {
        "id": ACTIVITY_ID,
        "start_date_local": "2026-05-15T08:00:00Z",
        "moving_time": 3600,
        "average_watts": 250,
        "has_heartrate": False,
    }
    streams = {"watts": [250] * 3600}
    manager = _make_manager(activity, streams)

    first = await load.compute_activity_load(conn, manager, ACTIVITY_ID, USER_ID)
    second = await load.compute_activity_load(conn, manager, ACTIVITY_ID, USER_ID)

    assert first["tss"] == second["tss"]
    # First call fetched streams once; second should not.
    assert manager.get_streams_normalized.call_count == 1


@pytest.mark.asyncio
async def test_cache_miss_after_ftp_change(conn):
    """Changing FTP produces a new inputs_hash → new cache row, recomputes."""
    await tl_config.set_field(conn, USER_ID, "ftp_watts", 240, "2025-01-01")
    activity = {
        "id": ACTIVITY_ID,
        "start_date_local": "2026-05-15T08:00:00Z",
        "moving_time": 3600,
        "average_watts": 250,
        "has_heartrate": False,
    }
    streams = {"watts": [250] * 3600}
    manager = _make_manager(activity, streams)

    first = await load.compute_activity_load(conn, manager, ACTIVITY_ID, USER_ID)
    # Change FTP for the activity's date by inserting a new effective period
    # that covers it. Since the activity is 2026-05-15 and the existing
    # row started 2025-01-01, set a new value with effective_from 2026-01-01.
    await tl_config.set_field(conn, USER_ID, "ftp_watts", 260, "2026-01-01")
    second = await load.compute_activity_load(conn, manager, ACTIVITY_ID, USER_ID)

    # FTP at 2026-05-15 is now 260, not 240.
    assert first["inputs_used"]["ftp_watts"] == 240
    assert second["inputs_used"]["ftp_watts"] == 260
    # Different TSS values (lower IF when FTP is higher).
    assert second["tss"] != first["tss"]
    # Both rows in cache now — audit trail preserved.
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM activity_load WHERE activity_id = ?",
        (ACTIVITY_ID,),
    )
    count = (await cursor.fetchone())[0]
    assert count == 2


@pytest.mark.asyncio
async def test_inputs_hash_deterministic():
    """Same input dict always hashes the same."""
    a = {"method": "power", "ftp_watts": 250, "ftp_effective_from": "2026-01-01"}
    b = {"ftp_effective_from": "2026-01-01", "ftp_watts": 250, "method": "power"}
    assert load._inputs_hash(a) == load._inputs_hash(b)


@pytest.mark.asyncio
async def test_inputs_hash_differs_on_value_change():
    """Different FTP → different hash."""
    a = {"method": "power", "ftp_watts": 250, "ftp_effective_from": "2026-01-01"}
    b = {"method": "power", "ftp_watts": 260, "ftp_effective_from": "2026-01-01"}
    assert load._inputs_hash(a) != load._inputs_hash(b)


# ── Edge: activity missing date ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_activity_missing_date_returns_none_with_warning(conn):
    activity = {
        "id": ACTIVITY_ID,
        "start_date_local": None,
        "start_date": None,
        "moving_time": 3600,
        "average_watts": 250,
    }
    manager = _make_manager(activity, streams={"watts": [250] * 3600})

    result = await load.compute_activity_load(conn, manager, ACTIVITY_ID, USER_ID)

    assert result["method"] == "none"
    assert any("start_date" in w for w in result["warnings"])
