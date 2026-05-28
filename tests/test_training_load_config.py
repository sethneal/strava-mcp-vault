"""Phase 1 tests: athlete config layer — resolver edge cases + validation."""

from __future__ import annotations

import pytest
import pytest_asyncio

from strava_mcp_vault.cache.db import CacheDB
from strava_mcp_vault.training_load import config


USER_ID = 1


@pytest_asyncio.fixture
async def conn(tmp_path):
    """Return an initialized aiosqlite connection with the real schema."""
    cdb = CacheDB(str(tmp_path / "test.db"))
    await cdb.init()
    yield cdb._db
    await cdb.close()


# ── Resolver edge cases ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolver_no_rows_returns_all_none(conn):
    """Empty history → every field is None. No defaults, ever."""
    result = await config.get_config_at(conn, USER_ID, "2026-05-23")
    assert result == {
        "ftp_watts": None,
        "ftp_effective_from": None,
        "lthr_bpm": None,
        "lthr_effective_from": None,
        "weight_kg": None,
        "weight_effective_from": None,
    }


@pytest.mark.asyncio
async def test_resolver_single_value_history_after_effective_from(conn):
    """One open row, date after effective_from → returns value + date."""
    await config.set_field(conn, USER_ID, "ftp_watts", 260, "2026-01-01")
    result = await config.get_config_at(conn, USER_ID, "2026-05-23")
    assert result["ftp_watts"] == 260
    assert result["ftp_effective_from"] == "2026-01-01"


@pytest.mark.asyncio
async def test_resolver_single_value_history_before_effective_from(conn):
    """One open row, date before effective_from → None (no coverage)."""
    await config.set_field(conn, USER_ID, "ftp_watts", 260, "2026-01-01")
    result = await config.get_config_at(conn, USER_ID, "2025-12-31")
    assert result["ftp_watts"] is None
    assert result["ftp_effective_from"] is None


@pytest.mark.asyncio
async def test_resolver_multi_value_history_picks_correct_window(conn):
    """Two rows; date in older window → older value; date in newer → newer."""
    await config.set_field(conn, USER_ID, "ftp_watts", 240, "2024-01-01")
    await config.set_field(conn, USER_ID, "ftp_watts", 260, "2025-06-01")

    older = await config.get_config_at(conn, USER_ID, "2024-12-01")
    newer = await config.get_config_at(conn, USER_ID, "2025-12-01")

    assert older["ftp_watts"] == 240
    assert older["ftp_effective_from"] == "2024-01-01"
    assert newer["ftp_watts"] == 260
    assert newer["ftp_effective_from"] == "2025-06-01"


@pytest.mark.asyncio
async def test_resolver_boundary_inclusive_lower_exclusive_upper(conn):
    """effective_from is inclusive; effective_to is exclusive (half-open)."""
    await config.set_field(conn, USER_ID, "ftp_watts", 240, "2024-01-01")
    await config.set_field(conn, USER_ID, "ftp_watts", 260, "2025-06-01")

    # On the transition date itself → newer value (effective_from <= date,
    # older row's effective_to == "2025-06-01" so older row's window is
    # [2024-01-01, 2025-06-01)).
    on_boundary = await config.get_config_at(conn, USER_ID, "2025-06-01")
    assert on_boundary["ftp_watts"] == 260


@pytest.mark.asyncio
async def test_resolver_date_in_gap_returns_none(conn):
    """Two closed rows with a gap between → date in gap → None."""
    import time

    # Direct DB inserts to create a true gap (set_field can't produce one).
    now = time.time()
    await conn.execute(
        """
        INSERT INTO athlete_config_history
            (user_id, field_name, value, effective_from, effective_to, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (USER_ID, "ftp_watts", 240, "2024-01-01", "2024-06-01", now),
    )
    await conn.execute(
        """
        INSERT INTO athlete_config_history
            (user_id, field_name, value, effective_from, effective_to, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (USER_ID, "ftp_watts", 260, "2025-01-01", None, now),
    )
    await conn.commit()

    result = await config.get_config_at(conn, USER_ID, "2024-09-15")
    assert result["ftp_watts"] is None
    assert result["ftp_effective_from"] is None


@pytest.mark.asyncio
async def test_resolver_date_after_open_row_returns_open_row(conn):
    """Open row's window extends indefinitely; far-future date still resolves."""
    await config.set_field(conn, USER_ID, "ftp_watts", 260, "2026-01-01")
    result = await config.get_config_at(conn, USER_ID, "2099-12-31")
    assert result["ftp_watts"] == 260
    assert result["ftp_effective_from"] == "2026-01-01"


@pytest.mark.asyncio
async def test_resolver_fields_independent(conn):
    """Setting FTP doesn't affect LTHR or weight resolution."""
    await config.set_field(conn, USER_ID, "ftp_watts", 260, "2026-01-01")
    result = await config.get_config_at(conn, USER_ID, "2026-05-23")
    assert result["ftp_watts"] == 260
    assert result["lthr_bpm"] is None
    assert result["weight_kg"] is None


# ── set_field temporal invariant ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_field_closes_current_open_row(conn):
    """Adding a new row sets the previous open row's effective_to."""
    await config.set_field(conn, USER_ID, "ftp_watts", 240, "2024-01-01")
    await config.set_field(conn, USER_ID, "ftp_watts", 260, "2025-06-01")

    history = await config.get_history(conn, USER_ID, "ftp_watts")
    assert len(history) == 2
    # Newest first
    assert history[0]["value"] == 260
    assert history[0]["effective_to"] is None
    assert history[1]["value"] == 240
    assert history[1]["effective_to"] == "2025-06-01"


@pytest.mark.asyncio
async def test_set_field_rejects_backfill_before_open_row(conn):
    """Cannot insert a value with effective_from <= existing open row's."""
    await config.set_field(conn, USER_ID, "ftp_watts", 260, "2025-06-01")
    with pytest.raises(config.ValidationError, match="(?i)backfill"):
        await config.set_field(conn, USER_ID, "ftp_watts", 240, "2024-01-01")


@pytest.mark.asyncio
async def test_set_field_rejects_same_date_as_open_row(conn):
    """effective_from must be strictly later than open row's."""
    await config.set_field(conn, USER_ID, "ftp_watts", 260, "2025-06-01")
    with pytest.raises(config.ValidationError):
        await config.set_field(conn, USER_ID, "ftp_watts", 270, "2025-06-01")


# ── Validation ───────────────────────────────────────────────────────────


def test_validate_ftp_too_low():
    with pytest.raises(config.ValidationError, match="out of range"):
        config.validate_value("ftp_watts", 49)


def test_validate_ftp_too_high():
    with pytest.raises(config.ValidationError):
        config.validate_value("ftp_watts", 501)


def test_validate_ftp_boundary_accepts_50_and_500():
    config.validate_value("ftp_watts", 50)
    config.validate_value("ftp_watts", 500)


def test_validate_lthr_too_low():
    with pytest.raises(config.ValidationError):
        config.validate_value("lthr_bpm", 99)


def test_validate_lthr_too_high():
    with pytest.raises(config.ValidationError):
        config.validate_value("lthr_bpm", 211)


def test_validate_weight_too_low():
    with pytest.raises(config.ValidationError):
        config.validate_value("weight_kg", 29)


def test_validate_weight_too_high():
    with pytest.raises(config.ValidationError):
        config.validate_value("weight_kg", 201)


def test_validate_unknown_field():
    with pytest.raises(config.ValidationError, match="unknown field_name"):
        config.validate_value("bogus_field", 100)  # type: ignore[arg-type]


# ── Weight unit conversion ───────────────────────────────────────────────


def test_to_kg_passthrough():
    assert config.to_kg(75.0, "kg") == 75.0


def test_to_kg_lb_conversion():
    # 150 lb = 68.04 kg
    result = config.to_kg(150.0, "lb")
    assert abs(result - 68.04) < 0.05


def test_to_kg_bad_unit_rejected():
    """Literal type catches this at the MCP layer; defensive check for direct use."""
    with pytest.raises(config.ValidationError, match="must be 'kg' or 'lb'"):
        config.to_kg(75.0, "stone")  # type: ignore[arg-type]


# ── History readback ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_history_empty(conn):
    history = await config.get_history(conn, USER_ID, "ftp_watts")
    assert history == []


@pytest.mark.asyncio
async def test_get_history_returns_all_rows_newest_first(conn):
    await config.set_field(conn, USER_ID, "ftp_watts", 240, "2024-01-01")
    await config.set_field(conn, USER_ID, "ftp_watts", 250, "2025-01-01")
    await config.set_field(conn, USER_ID, "ftp_watts", 260, "2026-01-01")
    history = await config.get_history(conn, USER_ID, "ftp_watts")
    values = [h["value"] for h in history]
    assert values == [260, 250, 240]


@pytest.mark.asyncio
async def test_get_history_invalid_field_rejected(conn):
    with pytest.raises(config.ValidationError):
        await config.get_history(conn, USER_ID, "bogus")  # type: ignore[arg-type]


# ── Multi-user isolation (forward-compat) ────────────────────────────────


@pytest.mark.asyncio
async def test_resolver_isolates_by_user_id(conn):
    """user_id is stored — different users see different values."""
    await config.set_field(conn, 1, "ftp_watts", 260, "2026-01-01")
    await config.set_field(conn, 2, "ftp_watts", 300, "2026-01-01")
    user1 = await config.get_config_at(conn, 1, "2026-05-23")
    user2 = await config.get_config_at(conn, 2, "2026-05-23")
    assert user1["ftp_watts"] == 260
    assert user2["ftp_watts"] == 300


# ── set_field_historical (backfill) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_historical_into_empty_history(conn):
    """Insert a closed window into an empty history."""
    await config.set_field_historical(
        conn, USER_ID, "ftp_watts", 240, "2024-01-01", "2024-06-01"
    )
    in_window = await config.get_config_at(conn, USER_ID, "2024-03-15")
    before = await config.get_config_at(conn, USER_ID, "2023-12-31")
    after = await config.get_config_at(conn, USER_ID, "2024-06-01")
    assert in_window["ftp_watts"] == 240
    assert before["ftp_watts"] is None
    assert after["ftp_watts"] is None  # effective_to is exclusive


@pytest.mark.asyncio
async def test_historical_fills_gap_between_two_closed_rows(conn):
    """The natural use case: backfill the gap between two existing periods."""
    import time
    now = time.time()
    # Pre-existing: 240W until June 2024, then a gap, then 260W from Jan 2025.
    await conn.execute(
        "INSERT INTO athlete_config_history (user_id, field_name, value, "
        "effective_from, effective_to, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (USER_ID, "ftp_watts", 240, "2024-01-01", "2024-06-01", now),
    )
    await conn.execute(
        "INSERT INTO athlete_config_history (user_id, field_name, value, "
        "effective_from, effective_to, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (USER_ID, "ftp_watts", 260, "2025-01-01", None, now),
    )
    await conn.commit()
    # Backfill the gap with 250W.
    await config.set_field_historical(
        conn, USER_ID, "ftp_watts", 250, "2024-06-01", "2025-01-01"
    )
    mid_gap = await config.get_config_at(conn, USER_ID, "2024-09-15")
    assert mid_gap["ftp_watts"] == 250


@pytest.mark.asyncio
async def test_historical_rejects_overlap_with_closed_row(conn):
    """Insert that overlaps an existing closed window → rejected."""
    await config.set_field_historical(
        conn, USER_ID, "ftp_watts", 240, "2024-01-01", "2024-06-01"
    )
    with pytest.raises(config.ValidationError, match="overlap"):
        await config.set_field_historical(
            conn, USER_ID, "ftp_watts", 250, "2024-03-01", "2024-09-01"
        )


@pytest.mark.asyncio
async def test_historical_rejects_overlap_with_open_row(conn):
    """Insert that overlaps the current open row → rejected."""
    await config.set_field(conn, USER_ID, "ftp_watts", 260, "2025-01-01")
    with pytest.raises(config.ValidationError, match="overlap"):
        await config.set_field_historical(
            conn, USER_ID, "ftp_watts", 240, "2024-06-01", "2025-06-01"
        )


@pytest.mark.asyncio
async def test_historical_rejects_zero_duration(conn):
    """effective_from must be strictly before effective_to."""
    with pytest.raises(config.ValidationError, match="strictly before"):
        await config.set_field_historical(
            conn, USER_ID, "ftp_watts", 240, "2024-01-01", "2024-01-01"
        )


@pytest.mark.asyncio
async def test_historical_rejects_reversed_dates(conn):
    """effective_from > effective_to → rejected."""
    with pytest.raises(config.ValidationError, match="strictly before"):
        await config.set_field_historical(
            conn, USER_ID, "ftp_watts", 240, "2024-06-01", "2024-01-01"
        )


@pytest.mark.asyncio
async def test_historical_abuts_closed_row_no_overlap(conn):
    """Inserting a window that starts exactly where a closed window ends is OK
    — half-open intervals don't overlap at the boundary."""
    await config.set_field_historical(
        conn, USER_ID, "ftp_watts", 240, "2024-01-01", "2024-06-01"
    )
    await config.set_field_historical(
        conn, USER_ID, "ftp_watts", 250, "2024-06-01", "2024-12-01"
    )
    boundary = await config.get_config_at(conn, USER_ID, "2024-06-01")
    assert boundary["ftp_watts"] == 250
