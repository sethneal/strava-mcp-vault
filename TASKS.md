Next up: Watch tomorrow's ~06:00 run to confirm the daily 429s are actually gone in production.

## Open
- [ ] Confirm no 429s in the 2026-09-15 morning window (the fix is verified in-session but hasn't run unattended yet) (2026-09-14 added)
- [ ] Consider relabeling the `unknown` cache-stats category — absent keys can never record a hit, so 0%/7294 misses reads as a bug but is an artifact (2026-09-14 added)
- [ ] Open PR for fix/strava-read-rate-limits (2026-09-14 added)
- [ ] Run /mcp → strava-cloud → Authenticate so its tools bridge into CLI sessions (2026-09-10)
- [ ] Finish the comparison table: claude.ai Strava connector vs strava-cloud vs strava-vault tools (2026-09-10)

## Done
- [x] Stop refetching immutable activity detail in the fitness curve — read from permanent vault instead of the 24h TTL cache; detail calls went 1481 → 0 (2026-09-14)
- [x] Parse X-ReadRateLimit-* and report the binding limit; read budget is 100/15min, 1000/day, half the overall (2026-09-14)
- [x] Pre-flight read-budget guard so a burst stops cleanly instead of spraying 429s that cache nothing (2026-09-14)
- [x] 429 backoff honoring the 15-min window reset; RateLimitError now carries retry_after (2026-09-14)
- [x] Verified gear-lookup RateLimitError was ALREADY caught (manager.py:404) — log tracebacks are exc_info warnings, no fix needed (2026-09-14)
- [x] Diagnosed daily Strava 429s: read-limit blindness + daily detail refetch, NOT a shared/saturated token (2026-09-14)
- [x] LaunchAgent com.sethneal.strava-vault installed; vault auto-starts and self-heals (2026-09-06)
- [x] Re-added strava-cloud CLI MCP entry (it was not a duplicate of the claude.ai connector) (2026-09-09)
