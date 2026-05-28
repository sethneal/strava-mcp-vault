"""Phase 4 tests: Strava-native value passthroughs (suffer_score)."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from strava_mcp_vault.cache.db import CacheDB
from strava_mcp_vault.training_load import strava_passthrough as sp


@pytest_asyncio.fixture
async def conn(tmp_path):
    cdb = CacheDB(str(tmp_path / "test.db"))
    await cdb.init()
    yield cdb
    await cdb.close()


# ── get_suffer_score ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_suffer_score_present_returned_as_is():
    manager = AsyncMock()
    manager.get_activity = AsyncMock(return_value={
        "id": 1, "suffer_score": 103.0, "has_heartrate": True,
    })
    result = await sp.get_suffer_score(manager, 1)
    assert result["suffer_score"] == 103.0
    assert result["has_heartrate"] is True
    assert result["note"] is None


@pytest.mark.asyncio
async def test_suffer_score_null_no_hr_explains():
    manager = AsyncMock()
    manager.get_activity = AsyncMock(return_value={
        "id": 1, "suffer_score": None, "has_heartrate": False,
    })
    result = await sp.get_suffer_score(manager, 1)
    assert result["suffer_score"] is None
    assert "no heart rate" in result["note"]


@pytest.mark.asyncio
async def test_suffer_score_null_with_hr_different_note():
    manager = AsyncMock()
    manager.get_activity = AsyncMock(return_value={
        "id": 1, "suffer_score": None, "has_heartrate": True,
    })
    result = await sp.get_suffer_score(manager, 1)
    assert result["suffer_score"] is None
    assert "no heart rate" not in result["note"]


# ── sum_suffer_scores ────────────────────────────────────────────────────


async def _insert_activity(conn, activity_id, date_str, suffer_score=None, sport_type="Ride"):
    """Insert one row into the activities table for the walk to find."""
    activity_json = json.dumps({
        "id": activity_id,
        "start_date_local": date_str,
        "suffer_score": suffer_score,
    })
    await conn._db.execute(
        "INSERT INTO activities (id, data, start_date, start_date_local, "
        "sport_type, synced_at) VALUES (?, ?, ?, ?, ?, ?)",
        (activity_id, activity_json, date_str, date_str, sport_type, time.time()),
    )
    await conn._db.commit()


@pytest.mark.asyncio
async def test_sum_empty_range(conn):
    result = await sp.sum_suffer_scores(conn, "2026-01-01", "2026-02-01")
    assert result["total_suffer_score"] == 0.0
    assert result["activities_total"] == 0


@pytest.mark.asyncio
async def test_sum_with_mixed_null_and_present(conn):
    await _insert_activity(conn, 1, "2026-01-05", suffer_score=50.0)
    await _insert_activity(conn, 2, "2026-01-10", suffer_score=None)
    await _insert_activity(conn, 3, "2026-01-15", suffer_score=75.0)
    await _insert_activity(conn, 4, "2026-01-20", suffer_score=None)

    result = await sp.sum_suffer_scores(conn, "2026-01-01", "2026-02-01")
    assert result["total_suffer_score"] == 125.0
    assert result["activities_total"] == 4
    assert result["activities_with_score"] == 2
    assert result["activities_without_score"] == 2


@pytest.mark.asyncio
async def test_sum_respects_date_range(conn):
    await _insert_activity(conn, 1, "2025-12-31", suffer_score=100.0)  # before
    await _insert_activity(conn, 2, "2026-01-05", suffer_score=50.0)   # in
    await _insert_activity(conn, 3, "2026-02-01", suffer_score=999.0)  # at exclusive end

    result = await sp.sum_suffer_scores(conn, "2026-01-01", "2026-02-01")
    # Only #2 should be included.
    assert result["total_suffer_score"] == 50.0
    assert result["activities_total"] == 1


@pytest.mark.asyncio
async def test_sum_respects_sport_type_filter(conn):
    await _insert_activity(conn, 1, "2026-01-05", suffer_score=50.0, sport_type="Ride")
    await _insert_activity(conn, 2, "2026-01-06", suffer_score=80.0, sport_type="Run")

    rides_only = await sp.sum_suffer_scores(
        conn, "2026-01-01", "2026-02-01", sport_type="Ride"
    )
    assert rides_only["total_suffer_score"] == 50.0
    assert rides_only["activities_total"] == 1
