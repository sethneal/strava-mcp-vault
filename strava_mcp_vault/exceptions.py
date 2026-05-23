"""Custom exception hierarchy for strava-mcp-vault.

All vault-specific exceptions inherit from VaultError, which lets
server.py tool functions catch errors with a single except clause.
"""


class VaultError(Exception):
    """Base exception for all vault errors."""


class RateLimitError(VaultError):
    """Strava API rate limit exceeded."""


class StravaAPIError(VaultError):
    """Strava API returned a non-success response."""

    def __init__(self, status_code: int, path: str, detail: str = ""):
        self.status_code = status_code
        self.path = path
        self.detail = detail
        msg = f"Strava API error {status_code} on {path}"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)


class VaultDatabaseError(VaultError):
    """Error accessing the local vault database."""


class GeocodingError(VaultError):
    """Error resolving a location via geocoding."""


class NoMatchingStreamsError(VaultError):
    """Strava returned stream data, but none of the requested types are present.

    Distinguishes "activity has no requested streams" (this error) from
    "activity not found" (StravaAPIError 404) — both previously surfaced as the
    same ambiguous "no data" message in derived-metric tools.
    """

    def __init__(self, activity_id: int, requested: set[str], available: set[str]):
        self.activity_id = activity_id
        self.requested = sorted(requested)
        self.available = sorted(available)
        available_str = ", ".join(self.available) if self.available else "(none)"
        super().__init__(
            f"Activity {activity_id}: requested streams [{', '.join(self.requested)}] "
            f"not available. Available streams: [{available_str}]. "
            f"This activity may not have the data you need, or the ID may not "
            f"correspond to one of your activities."
        )
