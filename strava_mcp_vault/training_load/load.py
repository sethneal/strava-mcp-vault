"""Per-activity training-load orchestrator.

``compute_activity_load`` ties together:

- the activity record from the vault (date, moving_time, average_heartrate, …)
- the resolved athlete config at the activity's date
- streams from Strava (only fetched when power is the candidate method)
- the pure kernels in ``calc.py``

Results are cached in ``activity_load`` keyed by ``(activity_id, inputs_hash)``
so retroactive FTP / LTHR changes produce a new cache row beside the old one
— the audit trail is the whole point of the inputs_hash design.

Method selection (in order):

1. ``power`` — activity has ``average_watts`` AND a watts stream AND
   ``ftp_watts`` is resolved at the activity date.
2. ``hr`` — activity has ``has_heartrate`` AND non-null ``average_heartrate``
   AND ``lthr_bpm`` is resolved at the activity date.
3. ``none`` — neither — numeric fields are null, warnings explain why.

The MCP design principle "does not replicate Strava's UI numbers" applies
here: if there's no watts stream, we fall through to HR or none — we do
NOT borrow Strava's ``weighted_average_watts`` scalar as a substitute NP.
That value gets its own passthrough tool in Phase 4.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import date, timedelta
from typing import Any

import aiosqlite

from strava_mcp_vault.exceptions import NoMatchingStreamsError
from strava_mcp_vault.training_load import calc, config as tl_config, curve as tl_curve


def _inputs_hash(inputs_used: dict[str, Any]) -> str:
    """Stable 16-hex-char hash of the inputs dict.

    Two computations with the same effective inputs (same method, same
    resolved config values + effective_from dates) collide intentionally
    — that's how the cache hit works. Retroactive config changes produce
    a different hash and therefore a new cache row.
    """
    canonical = json.dumps(inputs_used, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _build_result(
    activity_id: int,
    date: str,
    duration_seconds: int,
    method: str,
    tss: float | None,
    np_watts: float | None,
    intensity_factor: float | None,
    inputs_used: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    return {
        "activity_id": activity_id,
        "date": date,
        "duration_seconds": duration_seconds,
        "tss": tss,
        "np_watts": np_watts,
        "intensity_factor": intensity_factor,
        "method": method,
        "inputs_used": inputs_used,
        "warnings": warnings,
    }


async def _read_cache(
    conn: aiosqlite.Connection, activity_id: int, inputs_hash: str
) -> dict[str, Any] | None:
    cursor = await conn.execute(
        """
        SELECT date, duration_seconds, tss, np_watts, intensity_factor,
               method, inputs_used, warnings
        FROM activity_load
        WHERE activity_id = ? AND inputs_hash = ?
        """,
        (activity_id, inputs_hash),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return {
        "activity_id": activity_id,
        "date": row[0],
        "duration_seconds": row[1],
        "tss": row[2],
        "np_watts": row[3],
        "intensity_factor": row[4],
        "method": row[5],
        "inputs_used": json.loads(row[6]),
        "warnings": json.loads(row[7]),
    }


async def _write_cache(
    conn: aiosqlite.Connection,
    user_id: int,
    inputs_hash: str,
    result: dict[str, Any],
) -> None:
    await conn.execute(
        """
        INSERT OR REPLACE INTO activity_load
            (activity_id, inputs_hash, user_id, date, duration_seconds,
             tss, np_watts, intensity_factor, method, inputs_used,
             warnings, computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            result["activity_id"],
            inputs_hash,
            user_id,
            result["date"],
            result["duration_seconds"],
            result["tss"],
            result["np_watts"],
            result["intensity_factor"],
            result["method"],
            json.dumps(result["inputs_used"]),
            json.dumps(result["warnings"]),
            time.time(),
        ),
    )
    await conn.commit()


def _explain_none(
    activity: dict[str, Any],
    ftp: float | None,
    lthr: float | None,
) -> list[str]:
    reasons = []
    has_power_field = activity.get("average_watts") is not None
    if not has_power_field:
        reasons.append("activity has no recorded power")
    elif ftp is None:
        reasons.append("no FTP set for activity date — use strava_set_athlete_ftp")
    if not activity.get("has_heartrate") or not activity.get("average_heartrate"):
        reasons.append("activity has no recorded heart rate")
    elif lthr is None:
        reasons.append("no LTHR set for activity date — use strava_set_athlete_lthr")
    return reasons or ["unable to compute load — no usable method"]


async def compute_activity_load(
    conn: aiosqlite.Connection,
    manager: Any,  # CacheManager (avoid circular import)
    activity_id: int,
    user_id: int,
) -> dict[str, Any]:
    """Compute TSS / NP / IF for one activity, with cache.

    The returned dict is the spec result shape exactly. Caching keys on
    ``(activity_id, inputs_hash)`` so the same activity can have multiple
    cached rows reflecting different FTP/LTHR snapshots over time.
    """
    activity = await manager.get_activity(activity_id)

    start_local = activity.get("start_date_local") or activity.get("start_date") or ""
    date = start_local[:10]
    duration_seconds = int(activity.get("moving_time") or 0)

    if not date:
        return _build_result(
            activity_id, "", duration_seconds,
            method="none", tss=None, np_watts=None, intensity_factor=None,
            inputs_used={"method": "none"},
            warnings=["activity is missing start_date — cannot resolve config"],
        )

    cfg = await tl_config.get_config_at(conn, user_id, date)
    ftp = cfg.get("ftp_watts")
    lthr = cfg.get("lthr_bpm")

    # Candidate methods, ordered by preference. Each entry is
    # (method_str, inputs_used_dict). We cache-check each in order, so a
    # second call after a config change for the chosen method recomputes
    # cleanly, while a config change for the OTHER (unused) method does
    # not invalidate the existing cache row.
    candidates: list[tuple[str, dict[str, Any]]] = []
    if activity.get("average_watts") is not None and ftp is not None:
        candidates.append((
            "power",
            {
                "method": "power",
                "ftp_watts": ftp,
                "ftp_effective_from": cfg.get("ftp_effective_from"),
            },
        ))
    avg_hr = activity.get("average_heartrate")
    if activity.get("has_heartrate") and avg_hr and lthr is not None:
        candidates.append((
            "hr",
            {
                "method": "hr",
                "lthr_bpm": lthr,
                "lthr_effective_from": cfg.get("lthr_effective_from"),
            },
        ))

    for method, inputs_used in candidates:
        h = _inputs_hash(inputs_used)
        cached = await _read_cache(conn, activity_id, h)
        if cached is not None:
            return cached

        if method == "power":
            try:
                streams = await manager.get_streams_normalized(
                    activity_id, "watts"
                )
            except NoMatchingStreamsError:
                # Activity claimed average_watts but the stream is missing.
                # Skip power; try the next candidate (HR if available).
                continue
            watts = streams.get("watts") or []
            np_watts, info = calc.compute_normalized_power(watts)
            warnings = list(info.get("warnings", []))
            if np_watts is None:
                result = _build_result(
                    activity_id, date, duration_seconds,
                    method="power", tss=None, np_watts=None,
                    intensity_factor=None,
                    inputs_used=inputs_used, warnings=warnings,
                )
            else:
                tss, if_ = calc.compute_power_tss(np_watts, ftp, duration_seconds)
                result = _build_result(
                    activity_id, date, duration_seconds,
                    method="power", tss=tss, np_watts=np_watts,
                    intensity_factor=if_,
                    inputs_used=inputs_used, warnings=warnings,
                )
        else:  # method == "hr"
            tss, if_ = calc.compute_hr_tss(
                float(avg_hr), float(lthr), duration_seconds
            )
            result = _build_result(
                activity_id, date, duration_seconds,
                method="hr", tss=tss, np_watts=None, intensity_factor=if_,
                inputs_used=inputs_used, warnings=[],
            )

        await _write_cache(conn, user_id, h, result)
        return result

    # No candidate produced a result.
    inputs_used = {"method": "none"}
    h = _inputs_hash(inputs_used)
    cached = await _read_cache(conn, activity_id, h)
    if cached is not None:
        return cached
    warnings = _explain_none(activity, ftp, lthr)
    result = _build_result(
        activity_id, date, duration_seconds,
        method="none", tss=None, np_watts=None, intensity_factor=None,
        inputs_used=inputs_used, warnings=warnings,
    )
    await _write_cache(conn, user_id, h, result)
    return result


# ── Phase 3: time-series orchestrators ──────────────────────────────────


async def _walk_activities_aggregate(
    conn: aiosqlite.Connection,
    manager: Any,
    user_id: int,
    after: str,
    before_exclusive: str,
) -> tuple[dict[str, float], dict[str, int]]:
    """Paginate through vault activities in ``[after, before_exclusive)``,
    compute load for each (using the per-activity cache), return
    ``(tss_by_date, count_by_date)``.

    Loads with method=none contribute 0 TSS but still count as activities.
    """
    tss_by_date: dict[str, float] = {}
    count_by_date: dict[str, int] = {}

    batch_size = 200
    offset = 0
    while True:
        page = await manager.db.get_vault_activities(
            limit=batch_size,
            offset=offset,
            after=after,
            before=before_exclusive,
        )
        if not page:
            break
        for activity in page:
            activity_id = activity.get("id")
            if activity_id is None:
                continue
            result = await compute_activity_load(
                conn, manager, int(activity_id), user_id
            )
            iso_date = result.get("date")
            if not iso_date:
                continue
            tss = result.get("tss")
            if tss is not None and tss > 0:
                tss_by_date[iso_date] = tss_by_date.get(iso_date, 0.0) + tss
            count_by_date[iso_date] = count_by_date.get(iso_date, 0) + 1
        if len(page) < batch_size:
            break
        offset += batch_size

    return tss_by_date, count_by_date


async def compute_fitness_curve(
    conn: aiosqlite.Connection,
    manager: Any,
    user_id: int,
    start_date: str,
    end_date: str,
    warmup_days: int = tl_curve.DEFAULT_WARMUP_DAYS,
) -> list[dict[str, Any]]:
    """Build the daily CTL/ATL/TSB series for ``[start_date, end_date]``.

    Walks every vault activity in ``[start_date - warmup_days, end_date]``,
    computes load per activity (each one cached by ``(activity_id,
    inputs_hash)``), aggregates per-day TSS, then runs EWMA.

    First-run cost scales with number of activities × stream-fetch latency
    (~0.1-1s per power-method activity). Subsequent runs read from the
    activity_load cache and are fast. Per-tool MCP timeout should reflect
    the worst case (300s default).
    """
    warmup_start_d = date.fromisoformat(start_date) - timedelta(days=warmup_days)
    end_d = date.fromisoformat(end_date) + timedelta(days=1)
    tss_by_date, count_by_date = await _walk_activities_aggregate(
        conn, manager, user_id,
        after=warmup_start_d.isoformat(),
        before_exclusive=end_d.isoformat(),
    )
    return tl_curve.compute_series(
        tss_by_date, count_by_date,
        start_date, end_date, warmup_days,
    )


async def get_training_load_today(
    conn: aiosqlite.Connection,
    manager: Any,
    user_id: int,
    forecast_days: int = 7,
    warmup_days: int = tl_curve.DEFAULT_WARMUP_DAYS,
) -> dict[str, Any]:
    """Return today's CTL/ATL/TSB plus an N-day rest forecast.

    ``forecast_days`` defaults to 7 — answers "if I take this whole week
    off, where will my form land by Sunday?" Each forecast day shows the
    projected CTL/ATL/TSB assuming zero TSS from tomorrow on.
    """
    today = date.today().isoformat()
    series = await compute_fitness_curve(
        conn, manager, user_id, today, today, warmup_days=warmup_days
    )
    if not series:
        # Shouldn't happen in practice (today is always >= today), but
        # be defensive.
        return {
            "today": today,
            "tss": 0.0, "ctl": 0.0, "atl": 0.0, "tsb": 0.0,
            "activity_count": 0,
            "forecast_days": forecast_days,
            f"forecast_{forecast_days}_day": [],
        }
    last = series[-1]
    forecast = tl_curve.forecast_decay(last["ctl"], last["atl"], forecast_days)
    return {
        **last,
        "forecast_days": forecast_days,
        f"forecast_{forecast_days}_day": forecast,
    }


async def get_load_summary(
    conn: aiosqlite.Connection,
    manager: Any,
    user_id: int,
    period: str = "week",
    warmup_days: int = tl_curve.DEFAULT_WARMUP_DAYS,
) -> dict[str, Any]:
    """Period totals + peak ATL + CTL change.

    ``period``: ``"week"`` (last 7 days), ``"month"`` (last 30), or
    ``"year"`` (last 365). End date is today.
    """
    period_days = {"week": 7, "month": 30, "year": 365}.get(period)
    if period_days is None:
        raise ValueError(
            f"period must be 'week', 'month', or 'year' — got {period!r}"
        )
    end_d = date.today()
    start_d = end_d - timedelta(days=period_days - 1)  # inclusive both ends
    series = await compute_fitness_curve(
        conn, manager, user_id,
        start_d.isoformat(), end_d.isoformat(),
        warmup_days=warmup_days,
    )
    summary = tl_curve.summarize_period(series)
    return {
        "period": period,
        "start_date": start_d.isoformat(),
        "end_date": end_d.isoformat(),
        **summary,
    }
