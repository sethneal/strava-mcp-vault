import asyncio
import logging
import time

import httpx

from strava_mcp_vault.clients.base import BaseClient
from strava_mcp_vault.exceptions import RateLimitError, StravaAPIError

logger = logging.getLogger(__name__)

# Strava's 15-minute rate limit windows reset on the clock quarter hour.
RATE_LIMIT_WINDOW = 900

# Longest we'll block a request waiting for a window reset. Tool calls run
# under their own timeout budget (90-300s), so a full 15-minute wait would
# blow that; past this we raise and let the caller retry later.
MAX_RATE_LIMIT_WAIT = 30


def _parse_limit_pair(value: str | None) -> tuple[int, int] | None:
    """Parse a Strava "short,long" rate limit header into ints."""
    if not value:
        return None
    try:
        short, long = value.split(",")[:2]
        return int(short), int(long)
    except (IndexError, ValueError):
        return None


class StravaClient(BaseClient):
    """Strava API v3 client with OAuth token management and rate limit tracking."""

    def __init__(self, client_id: str, client_secret: str, cache_db):
        super().__init__("https://www.strava.com/api/v3")
        self.client_id = client_id
        self.client_secret = client_secret
        self._cache_db = cache_db
        self._token_lock = asyncio.Lock()
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: int = 0
        self._rate_limit_usage: str | None = None  # "usage,limit" from header
        self._rate_limit_limit: str | None = None
        # Strava applies a second, stricter budget to read requests
        # (100/15min, 1000/day vs the overall 200/2000). Tracking only the
        # overall headers makes the client think it has headroom while
        # Strava is already returning 429.
        self._read_rate_limit_usage: str | None = None
        self._read_rate_limit_limit: str | None = None
        self.max_rate_limit_wait = MAX_RATE_LIMIT_WAIT

    async def init_tokens(self):
        """Load tokens from cache_db.

        If None is returned (or stored tokens are unreadable), this is treated
        as first boot and the caller must seed tokens from environment variables.
        """
        try:
            tokens = await self._cache_db.get_tokens()
        except Exception as e:
            logger.error("Stored tokens unreadable (%s); falling back to env-var seed", e)
            return
        if tokens is None:
            logger.info("No cached tokens found; caller must seed from env vars")
            return
        self._access_token = tokens["access_token"]
        self._refresh_token = tokens["refresh_token"]
        self._expires_at = tokens["expires_at"]
        logger.info("Loaded tokens from cache (expires_at=%d)", self._expires_at)

    async def _ensure_valid_token(self):
        """Refresh the access token if it expires within 5 minutes.

        Uses a lock so only one coroutine refreshes at a time.
        """
        if self._expires_at - time.time() > 300:
            return
        async with self._token_lock:
            # Double-check after acquiring the lock; another coroutine may
            # have already refreshed while we waited.
            if self._expires_at - time.time() > 300:
                return
            await self._refresh_token_request()

    async def _refresh_token_request(self):
        """POST to Strava's OAuth endpoint to get a fresh access token.

        Strava may return a new refresh_token with every refresh, so we
        always persist whatever comes back.
        """
        logger.info("Refreshing Strava access token")
        resp = await self._client.post(
            "https://www.strava.com/oauth/token",
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
            },
        )
        resp.raise_for_status()
        data = resp.json()

        self._access_token = data["access_token"]
        self._refresh_token = data["refresh_token"]
        self._expires_at = data["expires_at"]

        await self._cache_db.set_tokens(
            self._access_token,
            self._refresh_token,
            self._expires_at,
        )
        logger.info("Token refreshed, new expires_at=%d", self._expires_at)

    async def _get(self, path: str, **kwargs) -> dict | list:
        """GET with automatic token refresh, auth header injection, and rate
        limit tracking.

        Retries once on transient connection errors.
        """
        await self._ensure_valid_token()

        url = f"{self.base_url}{path}"
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._access_token}"

        for attempt in range(2):
            try:
                await self._check_read_budget()
                resp = await self._client.get(url, headers=headers, **kwargs)

                # Track rate limit headers regardless of status code
                usage = resp.headers.get("X-RateLimit-Usage")
                limit = resp.headers.get("X-RateLimit-Limit")
                if usage:
                    self._rate_limit_usage = usage
                if limit:
                    self._rate_limit_limit = limit
                read_usage = resp.headers.get("X-ReadRateLimit-Usage")
                read_limit = resp.headers.get("X-ReadRateLimit-Limit")
                if read_usage:
                    self._read_rate_limit_usage = read_usage
                if read_limit:
                    self._read_rate_limit_limit = read_limit

                if resp.status_code == 429:
                    reset = self._seconds_until_window_reset()
                    if attempt == 0 and reset <= self.max_rate_limit_wait:
                        logger.info(
                            "Rate limited; waiting %ss for the window to reset", reset
                        )
                        await asyncio.sleep(reset)
                        continue
                    raise RateLimitError(self._rate_limit_message(), retry_after=reset)

                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                raise StravaAPIError(
                    status_code=e.response.status_code,
                    path=path,
                    detail=e.response.text[:200],
                ) from e
            except (httpx.RemoteProtocolError, httpx.ConnectError):
                if attempt == 0:
                    continue
                raise

    def _seconds_until_window_reset(self) -> int:
        """Seconds until Strava's 15-minute window rolls over.

        The windows are aligned to the clock quarter hour, not to when the
        first request was made.
        """
        return int(RATE_LIMIT_WINDOW - (time.time() % RATE_LIMIT_WINDOW))

    def _rate_limit_message(self) -> str:
        """Describe the limit that was actually hit.

        Reporting only the overall budget produced messages like
        "usage: 103,207, limit: 200,2000" — which reads as comfortably under
        the limit while the real (read) budget of 100 was already blown.
        """
        parts = [f"overall {self._rate_limit_usage} of {self._rate_limit_limit}"]
        read = self.read_rate_limit_remaining
        if read is not None:
            parts.append(
                f"read {read['short']['usage']}/{read['short']['limit']} in 15min, "
                f"{read['long']['usage']}/{read['long']['limit']} daily"
            )
        return "Strava rate limit exceeded (" + "; ".join(parts) + ")"

    async def _check_read_budget(self):
        """Refuse to spend a read request the read budget can't cover.

        Without this the client keeps firing into a 429 wall. Each refused
        request returns no data, so nothing is cached and the same activity
        is fetched again on the next run — which is why the failures recurred
        every day instead of converging.
        """
        read = self.read_rate_limit_remaining
        if read is None or read["short"]["usage"] < read["short"]["limit"]:
            return

        reset = self._seconds_until_window_reset()
        if reset <= self.max_rate_limit_wait:
            logger.info("Read budget spent; waiting %ss for window reset", reset)
            await asyncio.sleep(reset)
            # Stale until the next response tells us the new usage.
            self._read_rate_limit_usage = None
            return

        raise RateLimitError(
            f"Strava read rate limit reached "
            f"({read['short']['usage']}/{read['short']['limit']} in 15min); "
            f"resets in {reset}s",
            retry_after=reset,
        )

    @property
    def read_rate_limit_remaining(self) -> dict | None:
        """Parsed read-specific rate limit info, or None if not yet seen."""
        usage = _parse_limit_pair(self._read_rate_limit_usage)
        limit = _parse_limit_pair(self._read_rate_limit_limit)
        if usage is None or limit is None:
            return None
        return {
            "short": {"usage": usage[0], "limit": limit[0]},
            "long": {"usage": usage[1], "limit": limit[1]},
        }

    @property
    def rate_limit_remaining(self) -> dict | None:
        """Return parsed rate limit info, or None if no data is available yet.

        Reports whichever budget is closest to being spent — the overall one
        or the stricter read one — so callers see the limit that will
        actually refuse the next request.

        Returns a dict with short-term and long-term usage and limits:
            {
                "short": {"usage": int, "limit": int},
                "long":  {"usage": int, "limit": int},
            }
        """
        usage = _parse_limit_pair(self._rate_limit_usage)
        limit = _parse_limit_pair(self._rate_limit_limit)
        if usage is None or limit is None:
            return None

        overall = {
            "short": {"usage": usage[0], "limit": limit[0]},
            "long": {"usage": usage[1], "limit": limit[1]},
        }
        read = self.read_rate_limit_remaining
        if read is None:
            return overall

        def _binding(window: str) -> dict:
            a, b = overall[window], read[window]
            return a if a["limit"] - a["usage"] <= b["limit"] - b["usage"] else b

        return {"short": _binding("short"), "long": _binding("long")}

    # ── Strava API methods ──────────────────────────────────────────────

    async def get_athlete(self) -> dict:
        """GET /athlete - returns the authenticated athlete's profile."""
        return await self._get("/athlete")

    async def get_activities(
        self,
        page: int = 1,
        per_page: int = 30,
        after: int | None = None,
        before: int | None = None,
    ) -> list:
        """GET /athlete/activities - returns a list of the athlete's activities."""
        params = {"page": page, "per_page": per_page}
        if after is not None:
            params["after"] = after
        if before is not None:
            params["before"] = before
        return await self._get("/athlete/activities", params=params)

    async def get_activity(self, activity_id: int) -> dict:
        """GET /activities/{id} - returns a single activity by ID."""
        return await self._get(f"/activities/{activity_id}")

    async def get_activity_streams(
        self,
        activity_id: int,
        stream_types: list[str],
    ) -> dict:
        """GET /activities/{id}/streams - returns time-series data streams.

        stream_types: list of stream keys, e.g. ["time", "heartrate", "latlng"]
        """
        params = {
            "keys": ",".join(stream_types),
            "key_type": "time",
        }
        return await self._get(f"/activities/{activity_id}/streams", params=params)

    async def get_gear(self, gear_id: str) -> dict:
        """GET /gear/{id} - returns gear details (bike or shoe)."""
        return await self._get(f"/gear/{gear_id}")

    async def get_athlete_stats(self, athlete_id: int) -> dict:
        """GET /athletes/{id}/stats - returns the athlete's aggregate stats."""
        return await self._get(f"/athletes/{athlete_id}/stats")

    async def get_athlete_zones(self) -> dict:
        """GET /athlete/zones - returns HR and power zones if configured."""
        return await self._get("/athlete/zones")
