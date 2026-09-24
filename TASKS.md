Next up: Push fix/strava-read-rate-limits so PR #4 picks up the cache-stats relabel, then merge it.

## Open
- [ ] Push fix/strava-read-rate-limits to origin to update PR #4 (2026-09-24 added)
- [ ] Finish the comparison table: claude.ai Strava connector vs strava-cloud vs strava-vault tools — decide where it lives (not this PR); note the claude.ai connector and strava-cloud expose the identical 11-tool set (2026-09-10)

## Done
- [x] Confirmed no 429s since the fix went live (server restart 2026-09-14 16:05) — zero across 09-15..09-24 with daily API traffic (2026-09-24)
- [x] Relabeled the `unknown` cache-stats row as "(not cached yet)" with no hit rate; stored key unchanged so counts stay continuous (2026-09-24)
- [x] PR for fix/strava-read-rate-limits already open as sethneal/strava-mcp-vault#4 (2026-09-24)
- [x] strava-cloud authenticated; its tools bridge into CLI sessions (health ok) (2026-09-24)
- [x] Stop refetching immutable activity detail in the fitness curve — read from permanent vault instead of the 24h TTL cache; detail calls went 1481 → 0 (2026-09-14)
- [x] Parse X-ReadRateLimit-* and report the binding limit; read budget is 100/15min, 1000/day, half the overall (2026-09-14)
- [x] Pre-flight read-budget guard so a burst stops cleanly instead of spraying 429s that cache nothing (2026-09-14)
- [x] 429 backoff honoring the 15-min window reset; RateLimitError now carries retry_after (2026-09-14)
- [x] Verified gear-lookup RateLimitError was ALREADY caught (manager.py:404) — log tracebacks are exc_info warnings, no fix needed (2026-09-14)
- [x] Diagnosed daily Strava 429s: read-limit blindness + daily detail refetch, NOT a shared/saturated token (2026-09-14)
- [x] LaunchAgent com.sethneal.strava-vault installed; vault auto-starts and self-heals (2026-09-06)
- [x] Re-added strava-cloud CLI MCP entry (it was not a duplicate of the claude.ai connector) (2026-09-09)
