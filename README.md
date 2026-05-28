# strava-mcp-vault

[![CI](https://github.com/<owner>/strava-mcp-vault/actions/workflows/ci.yml/badge.svg)](https://github.com/<owner>/strava-mcp-vault/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/<owner>/strava-mcp-vault/branch/main/graph/badge.svg)](https://codecov.io/gh/<owner>/strava-mcp-vault)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An unofficial, custom-built MCP server that lets your AI assistant talk to your Strava data. Connect it to Claude Code (or any MCP-compatible client) and ask questions like "how far did I run this week?" or "show me my ride stats for January." It pulls your activities, stats, and streams from Strava's API and stores everything in a local SQLite vault so you're not hitting the API every time.

This is not affiliated with or endorsed by Strava. It's a personal project built to scratch an itch.

## Supported clients

This MCP server targets **Claude Desktop** and **Claude Code**. It is not designed for claude.ai web chat because it caches data in a local SQLite database (managed via Docker) that requires local filesystem access from the model's tool runtime.

<!-- TODO: Replace with a real screenshot or GIF showing Claude asking a question
     and the tool returning a formatted activity table. Drop the asset into
     docs/images/ and update the path below. -->
![Demo placeholder — record a short GIF of Claude using a strava_* tool and save to docs/images/demo.gif](docs/images/demo.gif)

## What it does

- Connects your AI to Strava through the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/)
- Caches your activity data locally in SQLite so repeat queries are instant
- Handles OAuth token refresh automatically (Strava tokens expire every 6 hours)
- Formats output with sport-specific stats, emoji labels, and markdown tables
- Supports bulk sync to pull your full activity history into the local vault
- Runs as a Docker container with Streamable HTTP transport for network-wide access

## Why a server instead of running locally?

Most MCP servers run on your local machine alongside your AI client. This one is designed to run on a separate server (a homelab box, a VPS, anything with Docker) for a few reasons:

- **Access from any machine.** Connect from your laptop, your desktop, or any device running Claude Code. One server, multiple clients.
- **Your vault stays put.** The SQLite database lives on the server in a Docker volume. You don't lose your cached data when you reimage a laptop or switch machines.
- **Always running.** Token refresh happens in the background even when your laptop is off. Your data stays fresh.
- **Backups are simpler.** One database file on one server. Back it up however you back up everything else.

If you only use one machine, this works fine running locally too. The Docker setup is the same either way.

## Why not just use the Strava API directly?

Strava's rate limits are tight: 100 requests per 15 minutes, 1,000 per day. Every time your AI asks a question, it burns API calls. Other Strava MCP servers exist, but they're thin API wrappers that proxy every request, don't cache anything, and break when tokens expire.

strava-mcp-vault takes a different approach:

- **Cache-aside architecture:** check SQLite first, hit the API only on cache miss
- **Automatic token management:** tokens stored in SQLite, refreshed before expiration
- **Bulk sync:** paginated import pulls entire activity histories without manual intervention
- **Offline access:** anything previously cached works without an internet connection
- **Hit/miss tracking:** see exactly how the cache is performing and how much API budget remains

For a simpler setup that just wraps the existing npm package in Docker, see [strava-mcp-docker](https://github.com/pete-builds/strava-mcp-docker).

## Tools

| Tool | Description | Cache TTL |
|------|-------------|-----------|
| `strava_get_recent_activities` | List recent activities with distance, time, HR, and power (when recorded). Supports `sport_type` filter (single type, comma list, or category alias like `"rides"`), `has_power` filter, `offset` pagination, and `compact` table view. | 1 hour (API fallback) |
| `strava_get_activity` | Full activity detail including a ⚡ Power section for rides with power data (avg, weighted-avg, max, kJ, source) | 24 hours |
| `strava_get_activity_streams` | Time-series data (heart rate, power, elevation, GPS) | 7 days |
| `strava_get_athlete_profile` | Authenticated athlete info | 24 hours |
| `strava_get_athlete_stats` | YTD and all-time totals | 1 day |
| `strava_get_cache_stats` | Cache hit/miss rates and API rate limit status | — |
| `strava_get_activities_near` | Find vault activities that started within N miles of a location. Supports the same `sport_type` aliases as `get_recent_activities`, plus `limit`/`offset` pagination. | — |
| `strava_query_vault` | Filter and aggregate vault activities by date, sport type, and power-data presence. Returns totals for distance, time, elevation, plus work (kJ), avg weighted power, and a power-meter ride count when matching activities recorded power. | — |
| `strava_set_activity_location` | Manually set (or clear) a display location on a vault activity | — |
| `strava_delete_vault_activity` | Remove one or more activities from the local vault (does not affect Strava) | — |
| `strava_sync_activities` | Sync activities into the vault. First run pulls full history; subsequent runs are incremental. | — |
| `strava_get_zone_distribution` | Time spent in each HR / power zone (uses Strava-configured zones). Returns a small computed result. | 24 hours |
| `strava_get_power_curve` | Best mean-max power at standard durations (5s, 30s, 1m, 5m, 20m, 1h, ...). Foundation for fitness comparison. | 24 hours |
| `strava_get_cardiac_drift` | First-half vs second-half HR comparison, with optional Pa:HR decoupling when power is present. Requires ≥20 min activity. | 24 hours |
| `strava_get_hr_power_decoupling` | Pa:HR decoupling ratio between two segments. Requires both heartrate and watts streams. | 24 hours |
| `strava_health_check` | Fast (<5s) probe of auth + DB. Returns per-probe ok/error, token TTL, and Strava rate-limit headroom. Use to detect a hung server before queuing real work. | — |
| `strava_set_athlete_ftp` / `_lthr` / `_weight` | Set FTP, LTHR, or weight effective from a date. Forward-only — closes the current open row, opens a new one. Required for training-load tools. | — |
| `strava_set_athlete_ftp_historical` / `_lthr_historical` / `_weight_historical` | Backfill a closed `[effective_from, effective_to)` window for historical periods that pre-date your use of this MCP. Must not overlap existing rows. | — |
| `strava_get_athlete_config` | Resolve effective FTP / LTHR / weight as of a date (default today). Each field's value comes with the `effective_from` of the row that supplied it. | — |
| `strava_get_athlete_config_history` | Full audit trail for one field (FTP, LTHR, or weight), newest first. | — |
| `strava_compute_activity_load` | Compute TSS / NP / IF for one activity using Coggan power-TSS (with spec-compliant gap handling) or TrainingPeaks hrTSS as fallback. Returns the inputs used (with `effective_from` dates) so you can audit. | per-activity cache keyed by `(activity_id, inputs_hash)` |
| `strava_compute_fitness_curve` | Daily CTL / ATL / TSB series for a date range. 180-day warmup by default for cold-start convergence. | per-activity cache hit on subsequent runs |
| `strava_get_training_load_today` | Today's CTL / ATL / TSB plus an N-day rest forecast (default 7) showing where form lands if you do zero training. | — |
| `strava_get_load_summary` | Period totals: TSS, avg/day, activity count, CTL change, peak ATL. `period` = `"week"` (7d), `"month"` (30d), or `"year"` (365d). | — |
| `strava_get_strava_suffer_score` | Strava's raw `suffer_score` (Relative Effort) for one activity. Passthrough — no computation. Use to compare against this MCP's computed TSS. | — |
| `strava_get_strava_relative_effort_summary` | Sum Strava's `suffer_score` across a date range. Mirrors Strava's "Weekly Relative Effort" chart math. | — |

All read tools accept a `response_format` parameter: `"markdown"` (default) for human-readable output or `"json"` for programmatic use.

### Sport-type filter

`sport_type` accepts three forms on `get_recent_activities`, `get_activities_near`, and `query_vault`:

- **A single Strava type:** `"Ride"`, `"GravelRide"`, `"Run"` — matched literally.
- **A comma-separated list:** `"Ride,GravelRide,MountainBikeRide"`.
- **A lowercase category alias:** `"rides"` / `"cycling"` (all ride types), `"running"`, `"swims"`, `"walks"`, `"hikes"`, `"snow"`, `"ski"`. Aliases are case-sensitive on the lowercase form — CamelCase always stays literal.

> **Known limit: surface type.** Strava's `sport_type` only distinguishes `Ride` / `GravelRide` / `MountainBikeRide` / `VirtualRide`, and Strava itself only records surface when the user manually tags an upload as `GravelRide`. If you ride a single bike on both pavement and dirt, every ride comes back as `Ride` with no surface info, so questions like "how much of my volume is road vs gravel?" can't be answered from `sport_type` alone. Workarounds: tag gravel rides on Strava, or maintain a personal gear-id → surface mapping client-side.

## Training load (TSS, CTL, ATL, TSB)

This MCP includes a complete Coggan / TrainingPeaks-style training-load model for cycling — per-activity TSS, plus the EWMA chronic / acute / balance (CTL / ATL / TSB) time series, plus passthroughs of Strava's own Relative Effort numbers for comparison.

**The numbers will NOT match Strava's Fitness & Freshness chart.** That's intentional. Strava's UI numbers come from their proprietary algorithms (different rolling window for NP, different intensity weighting); this MCP follows the published Coggan methodology. The `strava_get_strava_*` passthrough tools exist exactly so you can compare the two and see they track together (e.g. on a real 30-day window: TSS sum 2334 vs Strava RE sum 1430 — same shape, different scale).

### Methodology

- **Power TSS:** standard Coggan. `NP = (mean of (30-sec rolling avg of watts)^4)^(1/4)`, `IF = NP / FTP`, `TSS = (sec × NP × IF) / (FTP × 3600) × 100`. Gap handling: stream nulls under 10 samples are linearly interpolated between flanking values; nulls of 10+ samples are excluded from the rolling window (no synthetic zeros). Activities with gap duration > 5% of total emit a warning. Minimum 30 valid samples required.
- **HR TSS (hrTSS):** TrainingPeaks formula. `IF = avg_hr / LTHR`, `TSS = (sec × IF² × 100) / 3600`. Uses Strava's per-activity `average_heartrate` scalar, not stream re-computation.
- **Method selection:** power when watts stream + FTP both available; else HR when avg HR + LTHR both available; else `method="none"` with warnings explaining what's missing. **The MCP does NOT borrow Strava's `weighted_average_watts` as a substitute NP** — if the watts stream is unavailable, it falls through to HR or none rather than blend in a number computed by a different algorithm.
- **EWMA time constants:** CTL τ = 42 days, ATL τ = 7 days, `k = 1 - exp(-1/τ)`. Default 180-day warmup before any requested range so CTL/ATL converge from a zero seed (>4 time constants, ≈98% converged). The earlier "Path A" attempt used 60d (~1.4τ), which produced visible cold-start CTL inaccuracy.
- **TSB convention:** yesterday's CTL minus yesterday's ATL ("form" coming into today, matching TrainingPeaks display).

### Athlete config: physiological inputs are first-class data

FTP, LTHR, and weight all change over time. The MCP applies the value that was effective on each activity's date, not today's value retroactively. Values live in `athlete_config_history` with `effective_from` / `effective_to` windows. Every computed metric returns the inputs that were resolved for it (value + `effective_from`) so when numbers look off you can see why without re-reading code.

Validation: FTP 50–500 W, LTHR 100–210 bpm, weight 30–200 kg. Out-of-range values are rejected, never coerced. Weight tools require `unit="kg"` or `unit="lb"` explicitly — never guessed.

**Backfill is supported via the `*_historical` tools.** If you've been on Strava for years, use `strava_set_athlete_ftp_historical(value, effective_from, effective_to)` to record your past FTP windows so TSS for old rides resolves the right value. Inserts must not overlap existing rows (modify the existing row first if you need to splice).

### Caching and audit trail

`strava_compute_activity_load` caches results in the `activity_load` table keyed by `(activity_id, inputs_hash)`. If you change FTP retroactively for a past date, the next compute produces a **new** cache row alongside the old one — the old result stays queryable for debugging "why did my CTL change?"

First-run `compute_fitness_curve` over a 180-day window can be slow (every uncached power-method activity fetches its watts stream from Strava). Subsequent runs read from the cache and are fast.

### Out of scope (intentionally)

- Matching Strava's Fitness & Freshness chart values (different math, proprietary).
- FTP auto-estimation from rides (eFTP / pFTP).
- HRV integration.
- Sport-specific FTP (running, swimming).
- Power balance, torque effectiveness, or other Garmin Connect IQ metrics.
- Strength training load modeling.
- Subjective wellness inputs (sleep, RPE, mood).

### Quickstart for training-load tools

```
# 1. Seed your physiological config (you only do this once, plus updates)
strava_set_athlete_ftp(value=260, effective_from="2024-01-01")
strava_set_athlete_lthr(value=171, effective_from="2024-01-01")
strava_set_athlete_weight(value=245, effective_from="2024-01-01", unit="lb")

# (optional) backfill past values
strava_set_athlete_ftp_historical(value=240, effective_from="2022-01-01", effective_to="2024-01-01")

# 2. Sync activities into the vault if you haven't yet
strava_sync_activities()

# 3. Use the load tools
strava_compute_activity_load(activity_id=18654899126)
strava_get_training_load_today()                  # today + 7-day rest forecast
strava_get_load_summary(period="month")           # last 30 days
strava_compute_fitness_curve(start_date="2026-05-01", end_date="2026-05-27")

# 4. Compare to Strava's own numbers
strava_get_strava_suffer_score(activity_id=18654899126)
strava_get_strava_relative_effort_summary(start_date="2026-05-01", end_date="2026-05-27")
```

## Working with stream data

The `strava_get_activity_streams` tool returns time-series data (HR, power, time, altitude, etc). By default it returns the full dataset, but the Claude harness caps tool results at ~1MB. For longer activities, this can be exceeded.

**Three ways to handle it:**

1. **Use the derived-metric tools** — `strava_get_zone_distribution`, `strava_get_power_curve`, `strava_get_cardiac_drift`, `strava_get_hr_power_decoupling`. Each computes its result server-side and returns a few KB. Prefer these whenever the question is "how was my X" rather than "give me the raw stream".

2. **Downsample with `max_points`** — pass an integer to get evenly-spaced samples. Rule of thumb: ~500 for shape/trend, ~2000 for peak detection. All streams downsample with the same step so indices line up.

3. **Export to disk with `export_path`** — pass a writable absolute path (or `""` for the default `~/.strava-mcp-vault/exports/` location). The full dataset is written to that file as JSON; the tool returns a small pointer. Read the file with the model's python tool. Bypasses the size guard entirely.

If you call `strava_get_activity_streams` without `max_points` or `export_path` and the response would exceed ~800KB, the tool returns a structured error with a recommended `max_points` value to retry with. No data is returned silently truncated.

## Example Output

Ask your AI "show me my recent activities" and you'll get formatted, sport-specific cards:

```
## 🏃 Recent Activities (3)

### 🚴 Morning Commute
Ride | Mar 10, 2026 3:45 PM

📏 Distance: 5.50 mi | 🚀 Speed: 12.3 mph | ⏱️ Time: 0:27:34 | ⛰️ Elevation: 245 ft
❤️ Avg HR: 145 bpm | 💓 Max HR: 167 bpm | 🔥 Calories: 450
⚡ Avg: 195 W | NP: 220 W | Work: 1,247 kJ

### 🏃 Evening Run
Run | Mar 9, 2026 6:15 PM

📏 Distance: 3.20 mi | 🏃 Pace: 8:59/mi | ⏱️ Time: 0:28:45 | ⛰️ Elevation: 125 ft
❤️ Avg HR: 152 bpm | 💓 Max HR: 175 bpm
```

Or ask for a compact table view with `compact: true`:

```
## 📋 Activities (5)

| # | Date   | Type | Name            | Distance | Time    | Elev   | HR  | Avg W | NP  |
|---|--------|------|-----------------|----------|---------|--------|-----|-------|-----|
| 1 | Mar 10 | 🚴   | Morning Commute | 5.5mi    | 0:27:34 | 245 ft | 145 | 195   | 220 |
| 2 | Mar 9  | 🏃   | Evening Run     | 3.2mi    | 0:28:45 | 125 ft | 152 | —     | —   |
| 3 | Mar 8  | 🏊   | Pool Swim       | 1500yd   | 0:32:10 | N/A    | 128 | —     | —   |
```

Use `query_vault` to get aggregated stats from your cached data without hitting the API:

```
## 🔍 Vault Query Results

Filter: type=rides, after 2026-01-01
Total Activities: 24

📏 Distance: 342.5 mi | ⏱️ Time: 28.4 hours | ⛰️ Elevation: 12,450 ft

### ⚡ Power

- Power-meter rides: 18
- Total Work: 22,140 kJ
- Avg Weighted Power: 212 W
```

## Prerequisites

- A Strava account
- A Strava API application (see below)
- **Either** Docker + Docker Compose (recommended), **or** Python 3.10+ for local development
- A way to expose the server publicly if you plan to use it from Claude.ai (web), Cowork, or any client not on the same machine/network as the server. See [Connecting to Claude](#connecting-to-claude) for tunneling options (Cloudflare Tunnel, Tailscale, etc).

## Setup

### Create a Strava API application

1. Go to <https://www.strava.com/settings/api>
2. Fill in the form:
   - **Application Name:** Whatever you want (e.g., "My MCP Server")
   - **Category:** Choose any
   - **Club:** Leave blank
   - **Website:** Any URL you own (e.g., `https://example.com`)
   - **Authorization Callback Domain:** A domain you own (e.g., `example.com`). This cannot be `localhost`. It doesn't need to be running a web server or have anything to do with this project. You're only using it as a redirect target to grab an authorization code (explained below).
3. After creating the app, you'll see your **Client ID** and **Client Secret** on the app settings page. You'll need both for the next steps.

> **Heads up: one Strava API app per account.** Strava limits each account to a single API application. If you already use this `client_id` for another service (e.g. your own website), both can share the same Strava app — see [Sharing one Strava app across multiple services](#sharing-one-strava-app-across-multiple-services) below. The original concern that two services would race on refresh-token rotation does not appear to apply in practice — Strava returns a stable `refresh_token` across refresh calls — but you should still expect to re-run OAuth if a refresh ever returns 401.

### OAuth: Get your access tokens

This is the trickiest part, and Strava's docs don't make it easy. Here's what actually works.

**Step 1: Build the authorization URL**

> **CRITICAL: You MUST include `activity:read_all` AND `profile:read_all` in the scope parameter.** The default `read` scope only gives a sparse summary profile. Without `activity:read_all`, every activity request returns a 401 with `"field": "activity:read_permission", "code": "missing"`. Without `profile:read_all`, `strava_get_zone_distribution` returns 401 on `/athlete/zones` and `strava_get_athlete_profile` is missing FTP, weight, gear (bikes/shoes), and measurement preference. These are the #1 and #2 gotchas and Strava's docs barely mention them.

```
https://www.strava.com/oauth/authorize?client_id=YOUR_CLIENT_ID&redirect_uri=https://YOUR_DOMAIN&response_type=code&scope=read,activity:read_all,profile:read_all
```

Replace `YOUR_CLIENT_ID` with the Client ID from your app settings, and `YOUR_DOMAIN` with the callback domain you entered when creating the app.

**Step 2: Authorize and grab the code**

Open that URL in your browser. Authorize the app. Strava will redirect to your callback domain.

**Here's the trick:** The redirect page will 404 (or show your unrelated website). This is expected and totally fine. You don't need a working web server at that domain. The only thing you need is the **authorization code in your browser's address bar**.

After the redirect, your browser URL will look something like:

```
https://yourdomain.com/?state=&code=abc123def456ghi789&scope=read,activity:read_all,profile:read_all
```

Copy the value between `code=` and `&scope` (in this example, `abc123def456ghi789`). That's your one-time authorization code for the next step. If the URL shows only `scope=read` (or is missing `profile:read_all`), the wrong scope was sent — start over with the URL above.

**Step 3: Exchange the code for tokens**

```bash
curl -X POST https://www.strava.com/oauth/token \
  -d client_id=YOUR_CLIENT_ID \
  -d client_secret=YOUR_CLIENT_SECRET \
  -d code=YOUR_CODE \
  -d grant_type=authorization_code
```

Copy `access_token` and `refresh_token` from the JSON response into your `.env` file. After first boot, the server manages token refresh automatically in SQLite. You won't need to do this again.

### Sharing one Strava app across multiple services

Strava allows only one API application per account, so if you have an existing service (a coach site, a daily-stats dashboard, etc.) already using your `client_id`, this MCP server has to share it. The pattern that works:

1. **One OAuth dance.** Run the OAuth flow above *once*. The resulting `access_token` and `refresh_token` will work for all consumers.
2. **Copy the tokens into every service's config.** Same `STRAVA_ACCESS_TOKEN` and `STRAVA_REFRESH_TOKEN` in each service's environment. No central token store needed.
3. **Let each service refresh independently.** Each one calls `POST https://www.strava.com/oauth/token` when its short-lived access_token expires and caches the new one locally. Strava returns a stable `refresh_token`, so the services don't interfere with each other.
4. **Watch your shared rate limit.** The 100 req / 15 min and 1,000 req / day quotas are pooled across every consumer of the `client_id`. Heavy usage in one service can starve another.
5. **If a refresh ever returns 401, re-run OAuth once and update every service.** This shouldn't happen during normal operation. If it does — for example, you clicked "Revoke access" in Strava's settings — repeat the OAuth flow and copy the new tokens into every service's env again.

Strava's webhook subscription is also one-per-app, so if you want push events, only one of your services can receive them; the others will need to poll or proxy.

## Quick Start

**The fast path — interactive setup script:**

```bash
git clone https://github.com/sethneal/strava-mcp-vault.git
cd strava-mcp-vault
./setup.sh        # generates secrets, prompts for Strava creds, writes .env
docker compose up -d
```

`setup.sh` requires Python 3.10+ and will generate `MCP_AUTH_TOKEN` and `TOKEN_ENCRYPTION_KEY` for you, then prompt for your Strava credentials. It prints both generated secrets at the end — save them somewhere safe (the encryption key cannot be recovered if lost).

**Or the manual path:**

```bash
git clone https://github.com/sethneal/strava-mcp-vault.git
cd strava-mcp-vault
cp .env.example .env
chmod 600 .env  # contains secrets — lock it down

# Generate a bearer token (set as MCP_AUTH_TOKEN in .env):
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# Optional: generate a Fernet key for encrypting tokens at rest
# (set as TOKEN_ENCRYPTION_KEY in .env):
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Fill in the rest of .env (Strava credentials, etc.) then:
docker compose up -d
```

The server starts on port 18201 by default. Change it with `STRAVA_MCP_PORT` in your `.env`.

> ⚠️ **Save your `TOKEN_ENCRYPTION_KEY` somewhere safe.** If you lose it, the tokens stored in SQLite are unrecoverable and you'll have to redo the OAuth flow.

## Connecting to Claude

The MCP endpoint is `http://YOUR_SERVER_HOST:18201/mcp` (Streamable HTTP transport). How you wire it up depends on which Claude product you're using.

| Client | Network requirement | Config |
| --- | --- | --- |
| **Claude.ai (web)** | Public HTTPS URL | Custom Connector in the Claude.ai UI |
| **Cowork** | Public HTTPS URL | MCP server in Cowork settings |
| **Claude Desktop** (consumer app) | Same machine OR public URL | `claude_desktop_config.json` |
| **Claude Code** (CLI) | Same machine, LAN, or Tailscale | `claude mcp add` CLI |

> Previously used the HTTP+SSE transport (`/sse` endpoint) which was deprecated in the MCP spec 2025-03-26. Migrated to Streamable HTTP (MCP spec 2025-06-18) in v0.2.0. Existing clients must re-register with the new URL and transport.

### Exposing the server publicly (for Claude.ai and Cowork)

Claude.ai (web) and Cowork run in the cloud — they can't reach `127.0.0.1` or your LAN. You need a public HTTPS URL pointing at the MCP server. Pick one:

- **Cloudflare Tunnel sidecar (easiest)**: this repo ships an opt-in [`docker-compose.override.example.yml`](docker-compose.override.example.yml) that runs `cloudflared` alongside the MCP server. Copy it to `docker-compose.override.yml` and `docker compose up -d` — `docker compose logs cloudflared` will print a public `https://*.trycloudflare.com` URL. See the file's comments for upgrading to a stable named tunnel.
- **[Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)** standalone if you'd rather not use the sidecar. Run `cloudflared tunnel --url http://localhost:18201`.
- **[Tailscale Funnel](https://tailscale.com/kb/1223/funnel)** (free for personal use). `tailscale funnel 18201` exposes the local port at `https://<machine>.<tailnet>.ts.net`.
- **A reverse proxy you control** (Caddy, nginx, Traefik) terminating TLS in front of the server.

> ⚠️ **Security:** Your endpoint is now reachable from the internet. **Always set `MCP_AUTH_TOKEN` in `.env`** before exposing it — without auth, anyone who finds the URL gets read/write access to your Strava data and the local vault. The server refuses to start without either a bearer token or an explicit `MCP_ALLOW_UNAUTHENTICATED=1` opt-out.

### From Claude.ai (web)

1. Settings → **Connectors** → **Add custom connector**.
2. **URL:** your public HTTPS endpoint with `/mcp` (e.g. `https://strava.example.com/mcp`).
3. **Authentication:** Bearer token. Paste the value of `MCP_AUTH_TOKEN`.
4. Save. The 11 `strava_*` tools should appear in your tool picker on any new chat.

### From Cowork

1. Open the Cowork workspace settings → **MCP servers** → **Add**.
2. Use the same public URL + bearer token as the Claude.ai instructions above.
3. Save and start a session; the tools become available to all agents in that workspace.

### From Claude Desktop (consumer app, macOS/Windows)

Claude Desktop reads `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS or `%APPDATA%\Claude\claude_desktop_config.json` on Windows. Most versions need the [`mcp-remote`](https://github.com/geelen/mcp-remote) bridge to talk to HTTP MCP servers:

```json
{
  "mcpServers": {
    "strava": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "http://127.0.0.1:18201/mcp",
        "--header",
        "Authorization:Bearer YOUR_MCP_AUTH_TOKEN"
      ]
    }
  }
}
```

Replace `127.0.0.1` with your server's hostname/IP if it's not on the same machine. Restart Claude Desktop after editing. The first call may take ~5s while `npx` downloads `mcp-remote`.

### From Claude Code (CLI)

Use the IP of the machine running the server (or `127.0.0.1` if local). Tailscale IPs work great here.

```bash
# With auth token (recommended):
claude mcp add strava http://YOUR_SERVER_IP:18201/mcp --transport http \
  -H "Authorization: Bearer YOUR_MCP_AUTH_TOKEN"

# Without auth (only safe on a loopback / trusted LAN):
claude mcp add strava http://YOUR_SERVER_IP:18201/mcp --transport http
```

Or edit the MCP config JSON directly:

```json
{
  "mcpServers": {
    "strava": {
      "type": "http",
      "url": "http://YOUR_SERVER_IP:18201/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_MCP_AUTH_TOKEN"
      }
    }
  }
}
```

### Verify it works

After connecting, ask Claude something like *"What are my recent Strava activities?"* If the connection is healthy, Claude will call the `strava_get_recent_activities` tool and return your data. You can also ask Claude to run `strava_get_cache_stats` to confirm the server is responding.

## Cache Behavior

Each data type has its own TTL, tuned to how often the underlying data changes:

- **Activity lists** refresh every hour (new activities show up)
- **Individual activities** cache for 24 hours (they rarely change after upload)
- **Stream data** (heart rate, GPS, elevation) caches for 7 days (immutable once recorded)
- **Athlete stats** refresh daily (YTD totals update with each activity)

Run `sync_activities` after first setup to pull your recent history into the cache. This makes subsequent queries fast and avoids burning API calls on data you've already fetched.

Cached data persists across container restarts through a Docker volume (`strava-data`). Use `get_cache_stats` to check hit/miss rates and see how much of your API budget remains.

## Development

Running locally without Docker:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env, set VAULT_DB_PATH=./data/vault.db
python -m strava_mcp_vault.server
```

Requires Python 3.10+ (3.13 used in CI).

## Troubleshooting

**401 Authorization Error on `/athlete/activities`**: Token is missing `activity:read_all`. Re-run OAuth with `scope=read,activity:read_all,profile:read_all`. See [OAuth: Get your access tokens](#oauth-get-your-access-tokens).

**401 Authorization Error on `/athlete/zones` (or sparse `strava_get_athlete_profile` output)**: Token is missing `profile:read_all`. The error tools surface this with a "missing 'profile:read_all'" hint. Re-run OAuth with the full scope above. Without `profile:read_all` the profile endpoint returns only name/id/premium — FTP, weight, gear, and measurement preference are not exposed.

**429 Rate Limit**: Strava caps at 100 requests per 15 minutes, 1,000 per day. Wait and retry. Use `sync_activities` to bulk-cache data and reduce future API calls.

**Container keeps restarting**: Check logs with `docker logs strava-mcp-vault`. Usually a missing or invalid `.env` variable.

**Token expired**: The server refreshes tokens automatically before they expire. If refresh fails (revoked app, changed password), re-run the OAuth flow and update your `.env` with fresh tokens.

**Tool call hangs or times out**: Each tool has a per-call timeout budget (10s for `health_check`/`get_cache_stats`, 60–90s for most, 300s for `sync_activities`). If a tool returns a `timed out after Ns` message, run `strava_health_check` to probe auth + DB health. If it also fails, restart the server.

## Strava attribution

This project consumes data from the Strava API but is not affiliated with, endorsed by, or certified by Strava. Anyone deploying this server — especially in a configuration where its output is shown to other people — is responsible for following Strava's [API Agreement](https://www.strava.com/legal/api) and [Brand Guidelines](https://developers.strava.com/guidelines/), which include rules on attribution ("Powered by Strava"), logo usage, and how Strava data may be displayed.

If you fork this for anything more than personal use, read those documents before you ship.

## License

MIT

