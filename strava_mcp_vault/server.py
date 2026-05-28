import asyncio
import functools
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from typing import Literal

from dotenv import load_dotenv
from mcp.server.fastmcp import Context, FastMCP

from strava_mcp_vault import stream_analysis
from strava_mcp_vault.cache.db import CacheDB
from strava_mcp_vault.cache.geocode import forward_geocode, reverse_geocode_many
from strava_mcp_vault.cache.manager import CacheManager
from strava_mcp_vault.clients.strava import StravaClient
from strava_mcp_vault.exceptions import RateLimitError, StravaAPIError, VaultError
from strava_mcp_vault.training_load import config as tl_config
from strava_mcp_vault.training_load import load as tl_load
from strava_mcp_vault.training_load import strava_passthrough as tl_strava
from strava_mcp_vault.formatters import (
    format_activities_near,
    format_activity_detail,
    format_activity_streams,
    format_athlete_profile,
    format_athlete_stats,
    format_cache_stats,
    format_cardiac_drift,
    format_decoupling,
    format_delete_activities,
    format_power_curve,
    format_recent_activities,
    format_recent_activities_compact,
    format_sync_result,
    format_vault_query,
    format_zone_distribution,
)

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _jsonify(obj) -> str:
    """Serialize tool output as pretty JSON. Non-serializable values stringify."""
    return json.dumps(obj, indent=2, default=str)


def _with_timeout(seconds: float):
    """Wrap an async tool body in asyncio.wait_for.

    Turns silent hangs (stuck upstream call, DB lock, async deadlock) into
    a clear, actionable error string after `seconds` instead of letting the
    client wait minutes for its own timeout. functools.wraps preserves the
    wrapped function's signature so FastMCP's parameter introspection still
    sees the real arguments.
    """
    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await asyncio.wait_for(fn(*args, **kwargs), timeout=seconds)
            except asyncio.TimeoutError:
                logger.warning("Tool %s exceeded %ss budget", fn.__name__, seconds)
                return (
                    f"Tool {fn.__name__} timed out after {seconds}s. Possible "
                    "causes: stuck upstream Strava call, DB lock, or server hang. "
                    "Try strava_health_check to verify server state; restart "
                    "the server if it persists."
                )
        return wrapper
    return decorator


def _tool_error(tool_name: str, e: Exception) -> str:
    """Map an exception to an actionable error string for MCP clients.

    Specific Strava errors get tailored guidance (rate limit, auth, not found);
    other VaultErrors stringify directly; anything else logs a traceback and
    returns the type+message.
    """
    if isinstance(e, RateLimitError):
        return f"Strava rate limit hit. Wait a few minutes before retrying. ({e})"
    if isinstance(e, StravaAPIError):
        if e.status_code == 404:
            return f"Strava API: resource not found. Check the ID. ({e.path})"
        if e.status_code in (401, 403):
            detail_lower = (e.detail or "").lower()
            # Strava returns this field in its errors[] body when the token
            # was minted without activity:read_all. This is the most common
            # 401 cause in practice and warrants a more specific message.
            if "activity:read_permission" in detail_lower:
                return (
                    f"Strava API: insufficient scope on {e.path}. The current "
                    "tokens are missing 'activity:read_all'. Re-run the OAuth "
                    "flow with scope=read,activity:read_all,profile:read_all "
                    "and update STRAVA_ACCESS_TOKEN / STRAVA_REFRESH_TOKEN. "
                    "See README#oauth-get-your-access-tokens."
                )
            # /athlete/zones requires profile:read_all. Strava sometimes
            # returns a profile:read_permission marker, sometimes an empty
            # body — so detect by marker OR by path.
            if (
                "profile:read_permission" in detail_lower
                or e.path.startswith("/athlete/zones")
            ):
                return (
                    f"Strava API: insufficient scope on {e.path}. The current "
                    "tokens are missing 'profile:read_all' (required for "
                    "/athlete/zones and the detailed /athlete profile with "
                    "FTP, weight, and gear). Re-run the OAuth flow with "
                    "scope=read,activity:read_all,profile:read_all and update "
                    "STRAVA_ACCESS_TOKEN / STRAVA_REFRESH_TOKEN. "
                    "See README#oauth-get-your-access-tokens."
                )
            return (
                f"Strava API: unauthorized ({e.path}). The access token may be "
                "expired or revoked; reseed STRAVA_ACCESS_TOKEN / STRAVA_REFRESH_TOKEN."
            )
        if e.status_code == 429:
            return f"Strava API: rate limited. Wait before retrying. ({e.path})"
        return f"Strava API error: {e}"
    if isinstance(e, VaultError):
        return f"Error: {e}"
    logger.exception("Unexpected error in %s", tool_name)
    return f"Unexpected error: {type(e).__name__}: {e}"


# Globals initialized in lifespan
manager: CacheManager | None = None


async def _startup():
    """Initialize DB, client, and cache manager on server start."""
    global manager

    # Validate required env vars
    required = ["STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET"]
    missing = [v for v in required if not os.getenv(v)]
    if missing:
        logger.error("Missing required env vars: %s", ", ".join(missing))
        sys.exit(1)

    # Refuse to start unauthenticated unless explicitly opted in. The MCP
    # endpoint exposes private Strava data, so accidentally running without
    # MCP_AUTH_TOKEN on a routable interface is a foot-gun.
    if not os.getenv("MCP_AUTH_TOKEN") and not os.getenv("MCP_ALLOW_UNAUTHENTICATED"):
        logger.error(
            "Refusing to start without authentication. "
            "Set MCP_AUTH_TOKEN=<bearer-token> to enable auth, or "
            "MCP_ALLOW_UNAUTHENTICATED=1 to opt out (only safe behind a "
            "trusted network like Tailscale or with localhost-only port binding)."
        )
        sys.exit(1)

    # Init database. /app/data is the Docker path; for local runs default
    # to ./data so `python server.py` works out of the box.
    default_db_path = "/app/data/vault.db" if os.path.exists("/.dockerenv") else "./data/vault.db"
    db_path = os.getenv("VAULT_DB_PATH", default_db_path)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    db = CacheDB(db_path)
    await db.init()

    # Init Strava client
    client = StravaClient(
        client_id=os.getenv("STRAVA_CLIENT_ID"),
        client_secret=os.getenv("STRAVA_CLIENT_SECRET"),
        cache_db=db,
    )
    await client.init_tokens()

    # If no tokens in DB, seed from env vars (first boot)
    if client._access_token is None:
        access_token = os.getenv("STRAVA_ACCESS_TOKEN")
        refresh_token = os.getenv("STRAVA_REFRESH_TOKEN")
        if not access_token or not refresh_token:
            logger.error("First boot: STRAVA_ACCESS_TOKEN and STRAVA_REFRESH_TOKEN required")
            sys.exit(1)
        # Set expires_at to 0 to force immediate refresh
        await db.set_tokens(access_token, refresh_token, 0)
        client._access_token = access_token
        client._refresh_token = refresh_token
        client._expires_at = 0
        logger.info("Seeded tokens from env vars (will refresh on first request)")

    manager = CacheManager(db, client)
    logger.info("strava-mcp-vault initialized")


@asynccontextmanager
async def lifespan(server):
    await _startup()
    yield


port = int(os.getenv("STRAVA_MCP_PORT", "18201"))
mcp = FastMCP("strava_mcp", host="0.0.0.0", port=port, lifespan=lifespan)


@mcp.tool(
    name="strava_get_recent_activities",
    annotations={
        "title": "List recent Strava activities",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@_with_timeout(90)
async def get_recent_activities(
    count: int = 10,
    offset: int = 0,
    sport_type: str | None = None,
    after: str | None = None,
    before: str | None = None,
    has_power: bool | None = None,
    compact: bool = False,
    response_format: Literal["json", "markdown"] = "markdown",
) -> str:
    """List recent Strava activities with distance, time, and stats.

    Args:
        count: Number of activities to return (default 10, max 200).
        offset: Skip the first N activities for pagination (default 0).
        sport_type: Filter by Strava sport_type or category alias. Accepts a
            single type ("Ride"), a comma-separated list ("Ride,Run"), or a
            category alias ("rides", "running", "cycling", "snow",
            "walks", "swims"). Aliases match case-insensitively.
        after: Only activities on or after this date (ISO format, e.g. "2026-01-01").
        before: Only activities before this date (ISO format, e.g. "2026-04-01").
        has_power: If true, return only activities that recorded power data
            (avg watts, kJ, etc.). If false, return only activities without
            power data. Useful for filtering training-quality rides.
        compact: If true, return a compact one-line-per-activity table instead of full cards.
        response_format: "markdown" (default, human-readable) or "json" (machine-readable).
    """
    try:
        results = await manager.get_recent_activities(
            count,
            offset=offset,
            sport_type=sport_type,
            after=after,
            before=before,
            has_power=has_power,
        )
        if response_format == "json":
            total = await manager.db.get_vault_activity_count(
                sport_type=sport_type, after=after, before=before, has_power=has_power
            )
            return _jsonify(
                {
                    "total": total,
                    "count": len(results),
                    "offset": offset,
                    "items": results,
                    "has_more": offset + len(results) < total,
                    "next_offset": offset + len(results) if offset + len(results) < total else None,
                }
            )
        if compact:
            return format_recent_activities_compact(results)
        return format_recent_activities(results)
    except Exception as e:
        return _tool_error("get_recent_activities", e)


@mcp.tool(
    name="strava_query_vault",
    annotations={
        "title": "Summarize vault activities",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_timeout(90)
async def query_vault(
    sport_type: str | None = None,
    after: str | None = None,
    before: str | None = None,
    has_power: bool | None = None,
    response_format: Literal["json", "markdown"] = "markdown",
) -> str:
    """Query the activity vault for counts and totals with optional filters.

    Returns total count, distance, time, elevation, and breakdown by activity
    type. When any matching activities have power data, also returns total
    work (kJ), average weighted power, and a count of power-meter rides.
    Much lighter than fetching full activity lists.

    Args:
        sport_type: Filter by Strava sport_type or category alias. Accepts a
            single type ("Ride"), a comma-separated list ("Ride,Run"), or a
            category alias ("rides", "running", "cycling", etc.).
        after: Only activities on or after this date (ISO format, e.g. "2026-01-01").
        before: Only activities before this date (ISO format, e.g. "2026-04-01").
        has_power: If true, only activities that recorded power data.
            If false, only activities without power data.
        response_format: "markdown" (default, human-readable) or "json" (machine-readable).
    """
    try:
        result = await manager.query_vault(
            sport_type=sport_type,
            after=after,
            before=before,
            has_power=has_power,
        )
        if response_format == "json":
            return _jsonify(result)
        return format_vault_query(result)
    except Exception as e:
        return _tool_error("query_vault", e)


@mcp.tool(
    name="strava_get_activity",
    annotations={
        "title": "Get full Strava activity detail",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@_with_timeout(60)
async def get_activity(
    activity_id: int, response_format: Literal["json", "markdown"] = "markdown"
) -> str:
    """Get full details for a specific Strava activity.

    Args:
        activity_id: The Strava activity ID.
        response_format: "markdown" (default, human-readable) or "json" (machine-readable).
    """
    try:
        result = await manager.get_activity(activity_id)
        if response_format == "json":
            return _jsonify(result)
        return format_activity_detail(result)
    except Exception as e:
        return _tool_error("get_activity", e)


@mcp.tool(
    name="strava_get_activity_streams",
    annotations={
        "title": "Get activity time-series streams",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@_with_timeout(90)
async def get_activity_streams(
    activity_id: int,
    stream_types: str = "heartrate,distance,altitude",
    response_format: Literal["json", "markdown"] = "markdown",
    max_points: int | None = None,
    export_path: str | None = None,
) -> str:
    """Get time-series data for an activity (heart rate, elevation, etc).

    Args:
        activity_id: The Strava activity ID.
        stream_types: Comma-separated stream types (e.g. heartrate,distance,altitude,watts).
        response_format: "markdown" (default, human-readable summary + downsampled preview)
            or "json" (machine-readable: {downsample: {...}, streams: {...}}).
        max_points: If set, downsample each stream to at most this many evenly-spaced
            points. Picking a value: for trend/shape analysis use ~500; for peak detection
            use ~2000; for full data use None (but be aware of the ~1MB tool-result cap).
        export_path: If set, write the full dataset to this path as JSON and return
            a small pointer ({path, size_bytes, original_points, streams_written,
            schema_version}). Bypasses the size guard — disk has no 1MB cap.
            Defaults to ~/.strava-mcp-vault/exports/{activity_id}-{streams}-{epoch}.json
            if you pass an empty string. Useful in Claude Desktop / Claude Code where
            the model's python tool can read the file directly.

    For computed metrics (zones, drift, power curve, decoupling), prefer the
    purpose-built tools — they return small results and avoid the size cap entirely.
    """
    try:
        streams = await manager.get_streams_normalized(activity_id, stream_types)
        # Belt-and-suspenders: filter to requested keys again
        requested = {t.strip() for t in stream_types.split(",")}
        streams = {k: v for k, v in streams.items() if k in requested}

        if not streams:
            return _tool_error(
                "get_activity_streams",
                ValueError(f"no requested streams available for activity {activity_id}"),
            )

        # Export path bypasses size guard — disk has no cap
        if export_path is not None:
            import time as _time
            from pathlib import Path

            if export_path == "":
                default_dir = Path.home() / ".strava-mcp-vault" / "exports"
                default_dir.mkdir(parents=True, exist_ok=True)
                stream_key = "-".join(sorted(streams.keys()))
                epoch = int(_time.time())
                target = default_dir / f"{activity_id}-{stream_key}-{epoch}.json"
            else:
                target = Path(export_path).expanduser()
                target.parent.mkdir(parents=True, exist_ok=True)

            original_points = max(len(v) for v in streams.values() if isinstance(v, list))
            file_payload = {
                "schema_version": "1",
                "downsample": {
                    "original_points": original_points,
                    "returned_points": original_points,
                    "step": 1,
                    "reason": "none",
                },
                "streams": streams,
            }
            target.write_text(_jsonify(file_payload))
            return _jsonify({
                "path": str(target),
                "size_bytes": target.stat().st_size,
                "original_points": original_points,
                "streams_written": sorted(streams.keys()),
                "schema_version": "1",
            })

        # Pre-flight size guard
        SIZE_GUARD_BYTES = 800_000
        if max_points is None:
            estimated = stream_analysis.estimate_response_bytes(streams)
            if estimated > SIZE_GUARD_BYTES:
                rec = stream_analysis.recommended_max_points(streams, target_bytes=SIZE_GUARD_BYTES)
                original_points = max(len(v) for v in streams.values() if isinstance(v, list))
                return _jsonify({
                    "error": "response_too_large",
                    "original_points": original_points,
                    "estimated_bytes": estimated,
                    "recommended_max_points": rec,
                    "message": (
                        f"Response would exceed 1MB ({estimated:,} bytes estimated). "
                        f"Retry with max_points={rec} for downsampled data, "
                        f"or use the purpose-built tools (strava_get_zone_distribution, "
                        f"strava_get_power_curve, strava_get_cardiac_drift, "
                        f"strava_get_hr_power_decoupling) which return small computed results."
                    ),
                })

        downsampled, downsample_meta = stream_analysis.downsample(streams, max_points)
        payload = {"downsample": downsample_meta, "streams": downsampled}
        if response_format == "json":
            return _jsonify(payload)
        return format_activity_streams(payload, activity_id)
    except Exception as e:
        return _tool_error("get_activity_streams", e)


@mcp.tool(
    name="strava_get_athlete_profile",
    annotations={
        "title": "Get authenticated athlete profile",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@_with_timeout(60)
async def get_athlete_profile(response_format: Literal["json", "markdown"] = "markdown") -> str:
    """Get the authenticated Strava athlete's profile.

    Args:
        response_format: "markdown" (default, human-readable) or "json" (machine-readable).
    """
    try:
        result = await manager.get_athlete_profile()
        if response_format == "json":
            return _jsonify(result)
        return format_athlete_profile(result)
    except Exception as e:
        return _tool_error("get_athlete_profile", e)


@mcp.tool(
    name="strava_get_athlete_stats",
    annotations={
        "title": "Get athlete YTD and all-time stats",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@_with_timeout(60)
async def get_athlete_stats(response_format: Literal["json", "markdown"] = "markdown") -> str:
    """Get year-to-date and all-time activity statistics.

    Args:
        response_format: "markdown" (default, human-readable) or "json" (machine-readable).
    """
    try:
        result = await manager.get_athlete_stats()
        if response_format == "json":
            return _jsonify(result)
        return format_athlete_stats(result)
    except Exception as e:
        return _tool_error("get_athlete_stats", e)


def _validate_radius_miles(radius_miles: float) -> str | None:
    if radius_miles <= 0:
        return "radius_miles must be greater than 0."
    if radius_miles > 250:
        return "radius_miles is too large. Use 250 miles or less."
    return None


@mcp.tool(
    name="strava_get_cache_stats",
    annotations={
        "title": "Show cache and rate-limit stats",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_timeout(10)
async def get_cache_stats(response_format: Literal["json", "markdown"] = "markdown") -> str:
    """Show cache hit/miss rates, stored items, and API rate limit status.

    Args:
        response_format: "markdown" (default, human-readable) or "json" (machine-readable).
    """
    try:
        stats = await manager.get_cache_stats()
        if response_format == "json":
            return _jsonify(stats)
        return format_cache_stats(stats)
    except Exception as e:
        return _tool_error("get_cache_stats", e)


@mcp.tool(
    name="strava_get_activities_near",
    annotations={
        "title": "Find vault activities near a location",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@_with_timeout(60)
async def get_activities_near(
    location: str,
    radius_miles: float = 20.0,
    sport_type: str | None = None,
    after: str | None = None,
    before: str | None = None,
    limit: int = 50,
    offset: int = 0,
    response_format: Literal["json", "markdown"] = "markdown",
) -> str:
    """Find vault activities that started near a given location.

    Geocodes the location name, then searches the local vault for activities
    that started within the specified radius. No Strava API calls are made.

    Args:
        location: Place name to search near (e.g. "Syracuse, NY", "Central Park").
        radius_miles: Search radius in miles (default 20, max 250).
        sport_type: Filter by activity type (e.g. "Ride", "Run", "GravelRide").
        after: Only activities on or after this date (ISO format, e.g. "2025-01-01").
        before: Only activities before this date (ISO format, e.g. "2026-01-01").
        limit: Maximum activities to return (default 50, max 500).
        offset: Skip the first N results for pagination (default 0).
        response_format: "markdown" (default, human-readable) or "json" (machine-readable).
    """
    location = (location or "").strip()
    if not location:
        return "Location is required. Example: 'Syracuse, NY'."

    radius_error = _validate_radius_miles(radius_miles)
    if radius_error:
        return radius_error

    if limit < 1 or limit > 500:
        return "limit must be between 1 and 500."
    if offset < 0:
        return "offset must be >= 0."

    coords = await forward_geocode(location)
    if coords is None:
        return f"Could not geocode '{location}'. Try a more specific place name."
    lat, lon = coords
    all_results = await manager.db.get_activities_near_location(
        lat,
        lon,
        radius_miles=radius_miles,
        sport_type=sport_type,
        after=after,
        before=before,
    )
    total = len(all_results)
    results = all_results[offset : offset + limit]
    if results:
        activity_coords = [
            (a["start_latlng"][0], a["start_latlng"][1])
            for a in results
            if a.get("start_latlng") and len(a["start_latlng"]) == 2
        ]
        location_map = await reverse_geocode_many(activity_coords)
        for a in results:
            if a.get("_location_override"):
                a["_location"] = a["_location_override"]
            else:
                coords_key = tuple(a["start_latlng"][:2]) if a.get("start_latlng") else None
                a["_location"] = location_map.get(coords_key, "") if coords_key else ""

    if response_format == "json":
        return _jsonify(
            {
                "total": total,
                "count": len(results),
                "offset": offset,
                "items": results,
                "has_more": offset + len(results) < total,
                "next_offset": offset + len(results) if offset + len(results) < total else None,
                "location": location,
                "radius_miles": radius_miles,
            }
        )
    return format_activities_near(results, location, radius_miles)


@mcp.tool(
    name="strava_set_activity_location",
    annotations={
        "title": "Set or clear display location for a vault activity",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_timeout(30)
async def set_activity_location(activity_id: int, location: str | None = None) -> str:
    """Manually set (or clear) the display location for a vault activity.

    Useful for activities recorded indoors or without GPS where no location
    can be reverse geocoded. Pass location=None to clear an override.

    Args:
        activity_id: The Strava activity ID to update.
        location: Location string to display (e.g. "Ithaca, NY"). Pass null to clear.
    """
    try:
        found = await manager.db.set_location_override(activity_id, location)
        if not found:
            return f"Activity {activity_id} not found in vault."
        if location:
            return f'✅ Location for activity {activity_id} set to "{location}".'
        return f"✅ Location override cleared for activity {activity_id}."
    except Exception as e:
        return _tool_error("set_activity_location", e)


@mcp.tool(
    name="strava_delete_vault_activity",
    annotations={
        "title": "Delete activities from local vault",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_timeout(30)
async def delete_vault_activity(activity_ids: list[int]) -> str:
    """Delete one or more activities from the local vault by Strava activity ID.

    This only removes activities from the local database — it does not delete
    them from Strava. Useful for removing duplicates or unwanted entries.

    Args:
        activity_ids: List of Strava activity IDs to delete (e.g. [12345, 67890]).
    """
    if not activity_ids:
        return "No activity IDs provided. Pass one or more IDs, e.g. [12345]."

    try:
        deleted = await manager.db.delete_activities(activity_ids)
        return format_delete_activities(deleted, activity_ids)
    except Exception as e:
        return _tool_error("delete_vault_activity", e)


@mcp.tool(
    name="strava_sync_activities",
    annotations={
        "title": "Sync Strava activities into local vault",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
@_with_timeout(300)
async def sync_activities(days_back: int = 0, ctx: Context | None = None) -> str:
    """Sync Strava activities into the local vault.

    Smart sync behavior:
    - First run (empty vault): pulls ALL historical activities
    - Subsequent runs: only fetches activities newer than the latest stored
    - With days_back > 0: fetches a specific time window (useful for refreshing)

    Activities are stored permanently in the vault. No data expires.
    Typically takes 1-3 API calls for a full sync (~200 activities).

    Args:
        days_back: 0 = auto (incremental or full). >0 = fetch last N days (max 3650).
    """
    if days_back < 0 or days_back > 3650:
        return "days_back must be between 0 and 3650 (10 years)."
    try:
        result = await manager.sync_activities(days_back, ctx=ctx)
        return format_sync_result(result)
    except Exception as e:
        return _tool_error("sync_activities", e)


@mcp.tool(
    name="strava_get_zone_distribution",
    annotations={
        "title": "Time spent in each HR / power zone",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@_with_timeout(90)
async def get_zone_distribution(
    activity_id: int,
    zone_type: Literal["hr", "power", "both"] = "both",
    response_format: Literal["json", "markdown"] = "markdown",
) -> str:
    """Compute time spent in each HR and/or power zone for an activity.

    Returns a small computed result (no raw streams) — safe against the 1MB cap.

    Zones come from your Strava athlete zones config (cached 24h). If a zone
    type isn't configured on Strava, that side of the response is null with a reason.
    """
    try:
        keys = []
        if zone_type in ("hr", "both"):
            keys.append("heartrate")
        if zone_type in ("power", "both"):
            keys.append("watts")
        keys.append("time")
        stream_types = ",".join(keys)

        streams = await manager.get_streams_normalized(activity_id, stream_types)
        zones_raw = await manager.get_athlete_zones()

        hr_zones = (zones_raw or {}).get("heart_rate", {}).get("zones") if zone_type in ("hr", "both") else None
        power_zones = (zones_raw or {}).get("power", {}).get("zones") if zone_type in ("power", "both") else None

        result = stream_analysis.compute_zone_distribution(
            streams, hr_zones=hr_zones, power_zones=power_zones
        )
        result["activity_id"] = activity_id

        if response_format == "json":
            return _jsonify(result)
        return format_zone_distribution(result, activity_id)
    except Exception as e:
        return _tool_error("get_zone_distribution", e)


@mcp.tool(
    name="strava_get_power_curve",
    annotations={
        "title": "Power curve (mean-max power at standard durations)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@_with_timeout(90)
async def get_power_curve(
    activity_id: int,
    durations: str = "5,15,30,60,300,600,1200,3600",
    response_format: Literal["json", "markdown"] = "markdown",
) -> str:
    """Best mean-max power at each requested duration (seconds, comma-separated).

    Foundation for fitness comparison across activities. Returns a small computed
    result. Requires the activity to have a `watts` stream (power meter).
    """
    try:
        streams = await manager.get_streams_normalized(activity_id, "watts")
        try:
            durations_list = [int(d.strip()) for d in durations.split(",") if d.strip()]
        except ValueError as ve:
            return _tool_error("get_power_curve", ve)

        result = stream_analysis.compute_power_curve(streams, durations_list)
        result["activity_id"] = activity_id
        if response_format == "json":
            return _jsonify(result)
        return format_power_curve(result, activity_id)
    except Exception as e:
        return _tool_error("get_power_curve", e)


@mcp.tool(
    name="strava_get_hr_power_decoupling",
    annotations={
        "title": "Pa:HR decoupling between two segments",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@_with_timeout(90)
async def get_hr_power_decoupling(
    activity_id: int,
    segment_minutes: int | None = None,
    response_format: Literal["json", "markdown"] = "markdown",
) -> str:
    """Pa:HR decoupling — change in NP/HR between two segments.

    Requires both heartrate and watts streams. If segment_minutes is None,
    splits the activity in half; otherwise compares first N minutes vs last
    N minutes. |decoupling| > 5% is the conventional threshold for aerobic
    decoupling.
    """
    try:
        streams = await manager.get_streams_normalized(activity_id, "heartrate,watts")
        result = stream_analysis.compute_decoupling(streams, segment_minutes)
        result["activity_id"] = activity_id
        if response_format == "json":
            return _jsonify(result)
        return format_decoupling(result, activity_id)
    except Exception as e:
        return _tool_error("get_hr_power_decoupling", e)


@mcp.tool(
    name="strava_get_cardiac_drift",
    annotations={
        "title": "Cardiac drift across an activity",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@_with_timeout(90)
async def get_cardiac_drift(
    activity_id: int,
    response_format: Literal["json", "markdown"] = "markdown",
) -> str:
    """First-half vs second-half HR drift, with optional Pa:HR decoupling if power present.

    Requires heartrate stream; uses watts if available. Errors if activity is
    shorter than 20 minutes.
    """
    try:
        streams = await manager.get_streams_normalized(activity_id, "heartrate,watts")
        result = stream_analysis.compute_cardiac_drift(streams)
        result["activity_id"] = activity_id
        if response_format == "json":
            return _jsonify(result)
        return format_cardiac_drift(result, activity_id)
    except Exception as e:
        return _tool_error("get_cardiac_drift", e)


@mcp.tool(
    name="strava_health_check",
    annotations={
        "title": "Probe auth + DB health",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@_with_timeout(10)
async def health_check(response_format: Literal["json", "markdown"] = "markdown") -> str:
    """Fast probe (<5s) that exercises Strava auth and the local DB.

    Use this to detect a hung or misconfigured server before queuing real
    tool calls. Reports per-probe ok/error, current access-token TTL, and
    Strava rate-limit headroom (if any prior call has populated it).
    """
    auth_ok, auth_error = True, None
    try:
        await asyncio.wait_for(manager.client._ensure_valid_token(), timeout=4)
    except Exception as e:
        auth_ok = False
        auth_error = _tool_error("health_check", e)

    db_ok, db_error = True, None
    try:
        await asyncio.wait_for(manager.db.ping(), timeout=2)
    except Exception as e:
        db_ok = False
        db_error = f"{type(e).__name__}: {e}"

    expires_at = manager.client._expires_at or 0
    token_ttl = max(0, int(expires_at - time.time()))
    rate_limit = manager.client.rate_limit_remaining

    status = "healthy" if (auth_ok and db_ok) else "degraded"

    result = {
        "status": status,
        "auth": {"ok": auth_ok, "error": auth_error},
        "db": {"ok": db_ok, "error": db_error},
        "token_expires_in_seconds": token_ttl,
        "rate_limit": rate_limit,
    }

    if response_format == "json":
        return _jsonify(result)

    lines = ["## 🩺 Health Check", ""]
    lines.append(f"- **Status:** {status}")
    if auth_ok:
        lines.append("- **Auth:** OK")
    else:
        lines.append(f"- **Auth:** FAILED — {auth_error}")
    if db_ok:
        lines.append("- **Database:** OK")
    else:
        lines.append(f"- **Database:** FAILED — {db_error}")
    if token_ttl:
        hrs, rem = divmod(token_ttl, 3600)
        mins = rem // 60
        lines.append(f"- **Token TTL:** {hrs}h {mins}m")
    if rate_limit:
        short = rate_limit["short"]
        lng = rate_limit["long"]
        lines.append(
            f"- **Rate limit:** short {short['usage']}/{short['limit']}, "
            f"long {lng['usage']}/{lng['limit']}"
        )
    return "\n".join(lines)


# ── Training-load: athlete configuration (Phase 1) ─────────────────────
#
# user_id is the real Strava athlete_id (resolved at tool-call time from
# the cached /athlete profile). Single-tenant in practice, but the DB
# already keys rows by user_id so future multi-tenant deployments need no
# schema change. Date inputs are ISO YYYY-MM-DD; the resolver uses string
# comparison, which is correct for ISO dates.


async def _user_id() -> int:
    """Return the current Strava athlete_id. Cached via manager (24h TTL)."""
    profile = await manager.get_athlete_profile()
    return int(profile["id"])


def _today_iso() -> str:
    from datetime import date
    return date.today().isoformat()


def _format_config(cfg: dict, as_of: str) -> str:
    lines = [f"## 🏋️  Athlete Config (effective {as_of})", ""]
    pairs = [
        ("FTP", cfg["ftp_watts"], cfg["ftp_effective_from"], "W"),
        ("LTHR", cfg["lthr_bpm"], cfg["lthr_effective_from"], "bpm"),
        ("Weight", cfg["weight_kg"], cfg["weight_effective_from"], "kg"),
    ]
    for label, value, eff_from, unit in pairs:
        if value is None:
            lines.append(f"- **{label}:** — *(no value set)*")
        else:
            lines.append(f"- **{label}:** {value:g} {unit} *(since {eff_from})*")
    return "\n".join(lines)


def _format_history(field_name: str, rows: list[dict]) -> str:
    if not rows:
        return f"## 📜 {field_name} history\n\n*(no entries)*"
    lines = [f"## 📜 {field_name} history ({len(rows)} entries, newest first)", ""]
    lines.append("| Value | Effective from | Effective to |")
    lines.append("|-------|----------------|--------------|")
    for r in rows:
        eff_to = r["effective_to"] or "*(open)*"
        lines.append(f"| {r['value']:g} | {r['effective_from']} | {eff_to} |")
    return "\n".join(lines)


@mcp.tool(
    name="strava_set_athlete_ftp",
    annotations={
        "title": "Set FTP (functional threshold power) effective on a date",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
@_with_timeout(10)
async def set_athlete_ftp(value: float, effective_from: str) -> str:
    """Set FTP (functional threshold power) in watts, effective on a date.

    Inputs:
    - value: integer or float watts. Range 50–500. Rejected if outside.
    - effective_from: ISO date (YYYY-MM-DD). Must be strictly later than the
      current open row's effective_from — use `strava_set_athlete_ftp_historical`
      to backfill closed windows in the past.

    Behavior: closes the current open FTP row at `effective_from`, inserts a
    new open row with `value`. Future training-load computations resolve
    through this history so retroactive changes propagate automatically.
    """
    try:
        tl_config.validate_value("ftp_watts", value)
        await tl_config.set_field(
            manager.db._db, await _user_id(), "ftp_watts", value, effective_from
        )
        return f"FTP set to {value:g} W effective {effective_from}."
    except tl_config.ValidationError as e:
        return f"Validation error: {e}"
    except Exception as e:
        return _tool_error("set_athlete_ftp", e)


@mcp.tool(
    name="strava_set_athlete_lthr",
    annotations={
        "title": "Set LTHR (lactate threshold heart rate) effective on a date",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
@_with_timeout(10)
async def set_athlete_lthr(value: int, effective_from: str) -> str:
    """Set LTHR (lactate threshold heart rate) in bpm, effective on a date.

    Inputs:
    - value: integer bpm. Range 100–210. Rejected if outside.
    - effective_from: ISO date (YYYY-MM-DD). Must be strictly later than the
      current open row's effective_from — use
      `strava_set_athlete_lthr_historical` for backfill.

    Used by HR-based TSS (hrTSS) for activities lacking a watts stream.
    """
    try:
        tl_config.validate_value("lthr_bpm", value)
        await tl_config.set_field(
            manager.db._db, await _user_id(), "lthr_bpm", value, effective_from
        )
        return f"LTHR set to {value:g} bpm effective {effective_from}."
    except tl_config.ValidationError as e:
        return f"Validation error: {e}"
    except Exception as e:
        return _tool_error("set_athlete_lthr", e)


@mcp.tool(
    name="strava_set_athlete_weight",
    annotations={
        "title": "Set body weight effective on a date",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
@_with_timeout(10)
async def set_athlete_weight(
    value: float,
    effective_from: str,
    unit: Literal["kg", "lb"],
) -> str:
    """Set body weight, effective on a date.

    Inputs:
    - value: numeric weight in the unit specified by `unit`.
    - effective_from: ISO date (YYYY-MM-DD), strictly after current open row.
      Use `strava_set_athlete_weight_historical` for backfill.
    - unit: REQUIRED — "kg" or "lb". Ask the user if you're unsure;
      never guess.

    Stored in kg. Range check after conversion: 30–200 kg.
    """
    try:
        kg_value = tl_config.to_kg(value, unit)
        tl_config.validate_value("weight_kg", kg_value)
        await tl_config.set_field(
            manager.db._db, await _user_id(), "weight_kg", kg_value, effective_from
        )
        return f"Weight set to {kg_value:.2f} kg effective {effective_from}."
    except tl_config.ValidationError as e:
        return f"Validation error: {e}"
    except Exception as e:
        return _tool_error("set_athlete_weight", e)


@mcp.tool(
    name="strava_set_athlete_ftp_historical",
    annotations={
        "title": "Backfill a closed historical FTP window",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
@_with_timeout(10)
async def set_athlete_ftp_historical(
    value: float, effective_from: str, effective_to: str
) -> str:
    """Insert a closed historical FTP window for dates that pre-date your
    use of this MCP (or fill a gap between existing rows).

    Inputs:
    - value: integer watts. Range 50–500.
    - effective_from: ISO date (YYYY-MM-DD), inclusive.
    - effective_to: ISO date (YYYY-MM-DD), exclusive. Must be strictly
      after effective_from.

    The window [effective_from, effective_to) must not overlap any existing
    row for FTP. If you need to splice into an existing window, modify the
    existing row first. Use this to record FTP history that predates your
    use of this server (e.g. "I had 240W from Jan–Jun 2024, then 260W").
    """
    try:
        tl_config.validate_value("ftp_watts", value)
        await tl_config.set_field_historical(
            manager.db._db, await _user_id(), "ftp_watts",
            value, effective_from, effective_to,
        )
        return (
            f"FTP set to {value:g} W for [{effective_from}, {effective_to})."
        )
    except tl_config.ValidationError as e:
        return f"Validation error: {e}"
    except Exception as e:
        return _tool_error("set_athlete_ftp_historical", e)


@mcp.tool(
    name="strava_set_athlete_lthr_historical",
    annotations={
        "title": "Backfill a closed historical LTHR window",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
@_with_timeout(10)
async def set_athlete_lthr_historical(
    value: int, effective_from: str, effective_to: str
) -> str:
    """Insert a closed historical LTHR window. Same semantics as
    `strava_set_athlete_ftp_historical`. Range 100–210 bpm.
    """
    try:
        tl_config.validate_value("lthr_bpm", value)
        await tl_config.set_field_historical(
            manager.db._db, await _user_id(), "lthr_bpm",
            value, effective_from, effective_to,
        )
        return (
            f"LTHR set to {value:g} bpm for [{effective_from}, {effective_to})."
        )
    except tl_config.ValidationError as e:
        return f"Validation error: {e}"
    except Exception as e:
        return _tool_error("set_athlete_lthr_historical", e)


@mcp.tool(
    name="strava_set_athlete_weight_historical",
    annotations={
        "title": "Backfill a closed historical weight window",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
@_with_timeout(10)
async def set_athlete_weight_historical(
    value: float,
    effective_from: str,
    effective_to: str,
    unit: Literal["kg", "lb"],
) -> str:
    """Insert a closed historical weight window. Same semantics as
    `strava_set_athlete_ftp_historical`. `unit` is REQUIRED — ask the user
    if you're unsure. Stored in kg, range 30–200 kg after conversion.
    """
    try:
        kg_value = tl_config.to_kg(value, unit)
        tl_config.validate_value("weight_kg", kg_value)
        await tl_config.set_field_historical(
            manager.db._db, await _user_id(), "weight_kg",
            kg_value, effective_from, effective_to,
        )
        return (
            f"Weight set to {kg_value:.2f} kg for "
            f"[{effective_from}, {effective_to})."
        )
    except tl_config.ValidationError as e:
        return f"Validation error: {e}"
    except Exception as e:
        return _tool_error("set_athlete_weight_historical", e)


@mcp.tool(
    name="strava_get_athlete_config",
    annotations={
        "title": "Get athlete config (FTP, LTHR, weight) as of a date",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_timeout(10)
async def get_athlete_config(
    date: str | None = None,
    response_format: Literal["json", "markdown"] = "markdown",
) -> str:
    """Resolve effective FTP, LTHR, and weight as of a date.

    Inputs:
    - date: ISO date (YYYY-MM-DD). Defaults to today.
    - response_format: "markdown" (default) or "json".

    Returns each field's value and the `effective_from` date of the row
    that supplied it. Fields with no covering row return null — no
    defaults are ever substituted. Use `strava_get_athlete_config_history`
    to see the full audit trail for one field.

    No caching: this is a single DB read.
    """
    try:
        as_of = date or _today_iso()
        cfg = await tl_config.get_config_at(manager.db._db, await _user_id(), as_of)
        if response_format == "json":
            return _jsonify({"as_of": as_of, **cfg})
        return _format_config(cfg, as_of)
    except Exception as e:
        return _tool_error("get_athlete_config", e)


@mcp.tool(
    name="strava_get_athlete_config_history",
    annotations={
        "title": "List full history for one config field (FTP/LTHR/weight)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_timeout(10)
async def get_athlete_config_history(
    field_name: Literal["ftp_watts", "lthr_bpm", "weight_kg"],
    response_format: Literal["json", "markdown"] = "markdown",
) -> str:
    """Return the full history of one config field, newest first.

    Inputs:
    - field_name: "ftp_watts", "lthr_bpm", or "weight_kg".
    - response_format: "markdown" (default) or "json".

    Each entry contains value, effective_from, effective_to (null = currently
    open), and created_at. Use this to audit why a past TSS computation used
    a particular FTP value.
    """
    try:
        rows = await tl_config.get_history(
            manager.db._db, await _user_id(), field_name
        )
        if response_format == "json":
            return _jsonify({"field_name": field_name, "entries": rows})
        return _format_history(field_name, rows)
    except tl_config.ValidationError as e:
        return f"Validation error: {e}"
    except Exception as e:
        return _tool_error("get_athlete_config_history", e)


# ── Training-load: per-activity TSS / NP / IF (Phase 2) ────────────────


def _format_load_result(r: dict) -> str:
    """Markdown rendering of compute_activity_load output."""
    method_emoji = {"power": "⚡", "hr": "❤️", "none": "⚠️"}.get(r["method"], "•")
    lines = [
        f"## {method_emoji} Activity {r['activity_id']} load — method: {r['method']}",
        "",
        f"- **Date:** {r['date']}",
        f"- **Duration:** {r['duration_seconds']}s "
        f"({r['duration_seconds'] // 60} min)",
    ]
    if r["tss"] is not None:
        lines.append(f"- **TSS:** {r['tss']:.1f}")
        lines.append(f"- **IF:** {r['intensity_factor']:.3f}")
    if r["np_watts"] is not None:
        lines.append(f"- **NP:** {r['np_watts']:.0f} W")
    if r["inputs_used"]:
        lines.append("")
        lines.append("**Inputs used:**")
        for k, v in r["inputs_used"].items():
            lines.append(f"- `{k}`: {v}")
    if r["warnings"]:
        lines.append("")
        lines.append("**Warnings:**")
        for w in r["warnings"]:
            lines.append(f"- {w}")
    return "\n".join(lines)


@mcp.tool(
    name="strava_compute_activity_load",
    annotations={
        "title": "Compute TSS / NP / IF for one activity",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@_with_timeout(90)
async def compute_activity_load(
    activity_id: int,
    response_format: Literal["json", "markdown"] = "markdown",
) -> str:
    """Compute training-load metrics for one Strava activity.

    Picks the best available method given activity data + athlete config
    at the activity's date:

    1. ``power`` — needs a watts stream AND an FTP value effective on the
       activity date. Computes Coggan NP (with spec-compliant gap handling
       — interpolates gaps <10s, excludes gaps ≥10s, warns at >5% gap),
       then ``IF = NP / FTP`` and ``TSS = (sec * NP * IF) / (FTP * 3600) * 100``.
    2. ``hr`` — needs activity.has_heartrate, average_heartrate, AND an
       LTHR value at the activity date. ``IF = avg_hr / LTHR``,
       ``TSS = (sec * IF^2 * 100) / 3600`` (TrainingPeaks hrTSS).
    3. ``none`` — neither method applicable. Numeric fields are null;
       ``warnings`` lists what's missing.

    Returns the inputs_used (with effective_from dates) so you can audit
    why a particular FTP / LTHR was applied. Result is cached by
    ``(activity_id, inputs_hash)`` — changing FTP or LTHR for a past date
    produces a NEW cache row alongside the old one (audit trail).

    Does NOT use Strava's ``weighted_average_watts`` scalar as NP — if
    the watts stream is unavailable, we fall through to HR or none rather
    than borrow a value computed by a different method.
    """
    try:
        user_id = await _user_id()
        result = await tl_load.compute_activity_load(
            manager.db._db, manager, activity_id, user_id
        )
        if response_format == "json":
            return _jsonify(result)
        return _format_load_result(result)
    except Exception as e:
        return _tool_error("compute_activity_load", e)


# ── Training-load: time-series (Phase 3) ───────────────────────────────


def _format_fitness_curve(series: list[dict]) -> str:
    if not series:
        return "## 📈 Fitness Curve\n\n*(no days in range)*"
    lines = [
        f"## 📈 Fitness Curve ({len(series)} days)",
        "",
        f"From {series[0]['date']} to {series[-1]['date']}",
        "",
        "| Date | TSS | CTL | ATL | TSB | # |",
        "|------|----:|----:|----:|----:|--:|",
    ]
    for d in series:
        lines.append(
            f"| {d['date']} | {d['tss']:.0f} | {d['ctl']:.1f} | "
            f"{d['atl']:.1f} | {d['tsb']:+.1f} | {d['activity_count']} |"
        )
    return "\n".join(lines)


def _format_today(payload: dict) -> str:
    lines = [
        f"## 🎯 Training Load — {payload['date']}",
        "",
        f"- **TSS today:** {payload['tss']:.0f} "
        f"({payload['activity_count']} activities)",
        f"- **CTL (fitness):** {payload['ctl']:.1f}",
        f"- **ATL (fatigue):** {payload['atl']:.1f}",
        f"- **TSB (form):** {payload['tsb']:+.1f}",
    ]
    forecast_key = f"forecast_{payload['forecast_days']}_day"
    if payload.get(forecast_key):
        last_fc = payload[forecast_key][-1]
        lines.append("")
        lines.append(
            f"**If you rest {payload['forecast_days']} days:** "
            f"CTL→{last_fc['ctl']:.1f}, ATL→{last_fc['atl']:.1f}, "
            f"TSB→{last_fc['tsb']:+.1f}"
        )
    return "\n".join(lines)


def _format_summary(s: dict) -> str:
    return "\n".join([
        f"## 📊 Load Summary — {s['period']} ({s['start_date']} → {s['end_date']})",
        "",
        f"- **Total TSS:** {s['total_tss']:.0f}",
        f"- **Avg TSS/day:** {s['avg_tss_per_day']:.1f}",
        f"- **Activities:** {s['total_activities']} over {s['days']} days",
        f"- **CTL change:** {s['ctl_start']:.1f} → {s['ctl_end']:.1f} "
        f"({s['ctl_change']:+.1f})",
        f"- **Peak ATL:** {s['peak_atl']:.1f} on {s['peak_atl_date']}",
    ])


@mcp.tool(
    name="strava_compute_fitness_curve",
    annotations={
        "title": "Daily CTL / ATL / TSB series for a date range",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@_with_timeout(300)
async def compute_fitness_curve(
    start_date: str,
    end_date: str,
    warmup_days: int = 180,
    response_format: Literal["json", "markdown"] = "markdown",
) -> str:
    """Daily fitness (CTL), fatigue (ATL), and form (TSB) for a date range.

    Walks every vault activity in ``[start_date - warmup_days, end_date]``,
    computes load per activity (uses the activity_load cache), aggregates
    per-day TSS, runs EWMA, returns only days in the requested range.

    Inputs:
    - start_date / end_date: ISO YYYY-MM-DD (both inclusive).
    - warmup_days: prepend this many days before start so CTL/ATL
      converge from zero seed. Default 180 (>4 time constants, ~98%
      converged). Use 0 for cold-start testing.

    First call may take a while (every activity in the window needs a
    load computation, which for power-method activities fetches the watts
    stream from Strava). Subsequent calls hit the cache and are fast.
    Per-tool timeout: 300s.

    TSB convention: ``CTL[d-1] - ATL[d-1]`` ("form" coming into today).
    """
    try:
        user_id = await _user_id()
        series = await tl_load.compute_fitness_curve(
            manager.db._db, manager, user_id,
            start_date, end_date, warmup_days,
        )
        if response_format == "json":
            return _jsonify(series)
        return _format_fitness_curve(series)
    except Exception as e:
        return _tool_error("compute_fitness_curve", e)


@mcp.tool(
    name="strava_get_training_load_today",
    annotations={
        "title": "Today's CTL / ATL / TSB + 7-day rest forecast",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@_with_timeout(120)
async def get_training_load_today(
    forecast_days: int = 7,
    response_format: Literal["json", "markdown"] = "markdown",
) -> str:
    """Today's fitness state plus an N-day rest projection.

    Returns today's TSS / CTL / ATL / TSB plus ``forecast_N_day`` showing
    where CTL / ATL / TSB land if you do zero training for the next N
    days. Useful for "should I rest this week?" calls.

    Uses 180-day warmup. First call may be slow if many activities are
    uncached; subsequent calls fast.
    """
    try:
        user_id = await _user_id()
        result = await tl_load.get_training_load_today(
            manager.db._db, manager, user_id, forecast_days=forecast_days,
        )
        if response_format == "json":
            return _jsonify(result)
        return _format_today(result)
    except Exception as e:
        return _tool_error("get_training_load_today", e)


@mcp.tool(
    name="strava_get_load_summary",
    annotations={
        "title": "Period totals + peak ATL + CTL change",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@_with_timeout(120)
async def get_load_summary(
    period: Literal["week", "month", "year"] = "week",
    response_format: Literal["json", "markdown"] = "markdown",
) -> str:
    """Aggregate training load over the last week / month / year.

    Returns total TSS, average TSS/day, total activity count, CTL at
    start vs end (and delta), peak ATL with its date. End date is today.

    Periods: ``week`` (7 days), ``month`` (30), ``year`` (365). First
    call for ``year`` may be slow on a large vault.
    """
    try:
        user_id = await _user_id()
        result = await tl_load.get_load_summary(
            manager.db._db, manager, user_id, period=period,
        )
        if response_format == "json":
            return _jsonify(result)
        return _format_summary(result)
    except Exception as e:
        return _tool_error("get_load_summary", e)


# ── Training-load: Strava-native passthroughs (Phase 4) ────────────────


@mcp.tool(
    name="strava_get_strava_suffer_score",
    annotations={
        "title": "Strava's raw suffer_score (Relative Effort) for one activity",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@_with_timeout(30)
async def get_strava_suffer_score(
    activity_id: int,
    response_format: Literal["json", "markdown"] = "markdown",
) -> str:
    """Return Strava's raw ``suffer_score`` for one activity, with no
    computation on top.

    Strava renamed this metric to "Relative Effort" in their UI but
    kept the API field name. It's an HR-based intensity score; activities
    without HR data have a null suffer_score and this tool surfaces that
    explicitly rather than returning 0.

    Use to sanity-check Strava's Relative Effort against this MCP's
    computed TSS. The two won't equal each other (different methods, one
    based purely on HR, the other on power + HR) — but they should
    track together over time.
    """
    try:
        result = await tl_strava.get_suffer_score(manager, activity_id)
        if response_format == "json":
            return _jsonify(result)
        if result["suffer_score"] is None:
            return (
                f"## ❤️ Suffer Score — activity {activity_id}\n\n"
                f"- **suffer_score:** null\n"
                f"- {result['note']}"
            )
        return (
            f"## ❤️ Suffer Score — activity {activity_id}\n\n"
            f"- **suffer_score:** {result['suffer_score']:.0f}\n"
            f"- has_heartrate: {result['has_heartrate']}"
        )
    except Exception as e:
        return _tool_error("get_strava_suffer_score", e)


@mcp.tool(
    name="strava_get_strava_relative_effort_summary",
    annotations={
        "title": "Sum of Strava's suffer_score across a date range",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
@_with_timeout(60)
async def get_strava_relative_effort_summary(
    start_date: str,
    end_date: str,
    sport_type: str | None = None,
    response_format: Literal["json", "markdown"] = "markdown",
) -> str:
    """Sum Strava's ``suffer_score`` (Relative Effort) across vault
    activities in ``[start_date, end_date]``.

    Mirrors the math behind Strava's "Weekly Relative Effort" chart.
    Useful for sanity-checking against this MCP's TSS sum from
    ``strava_get_load_summary`` — both should trend together even though
    they're different metrics.

    Activities with null suffer_score (no HR) are excluded from the sum
    but counted separately so you can see how much of the period had
    Strava's metric available.
    """
    try:
        from datetime import date as _d, timedelta as _td
        before_exclusive = (
            _d.fromisoformat(end_date) + _td(days=1)
        ).isoformat()
        result = await tl_strava.sum_suffer_scores(
            manager.db, start_date, before_exclusive, sport_type=sport_type
        )
        # Replace technical bounds with user-friendly date range in the
        # returned payload for the json/markdown view.
        result["start_date"] = start_date
        result["end_date"] = end_date
        del result["after"]
        del result["before_exclusive"]
        if response_format == "json":
            return _jsonify(result)
        return (
            f"## 📊 Strava Relative Effort — "
            f"{result['start_date']} → {result['end_date']}\n\n"
            f"- **Total suffer_score:** {result['total_suffer_score']:.0f}\n"
            f"- **Activities:** {result['activities_total']} "
            f"({result['activities_with_score']} with score, "
            f"{result['activities_without_score']} without)"
        )
    except Exception as e:
        return _tool_error("get_strava_relative_effort_summary", e)


def main() -> None:
    import uvicorn

    from strava_mcp_vault.auth import maybe_add_auth, maybe_add_origin_check

    # Streamable HTTP transport (MCP spec 2025-06-18). Replaces the
    # deprecated HTTP+SSE transport from 2024-11-05. Single /mcp endpoint
    # that serves POST (client -> server) and GET (server -> client SSE
    # stream) on the same path.
    app = mcp.streamable_http_app()
    app = maybe_add_auth(app)
    app = maybe_add_origin_check(app)
    # Inside Docker, always bind 0.0.0.0 — docker-compose.yml's port
    # publish already restricts which host interface is reachable (via
    # MCP_BIND_HOST). Outside Docker (local Python dev), default to
    # loopback for safety; users can override with MCP_BIND_HOST.
    in_docker = os.path.exists("/.dockerenv")
    host = "0.0.0.0" if in_docker else os.getenv("MCP_BIND_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
