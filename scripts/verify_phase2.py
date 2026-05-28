"""One-shot end-to-end verification for Phases 2+3.

Phase 2: compute_activity_load on a specified (or latest) activity.
Phase 3: get_training_load_today + get_load_summary("month") + a small
fitness curve slice for the past 30 days.

Assumes FTP/LTHR/weight already seeded (verify_phase2 sets FTP=260 on
2024-01-01 if no history exists).

Run: .venv/bin/python scripts/verify_phase2.py [activity_id]
"""

import asyncio
import json
import os
import sys

from dotenv import load_dotenv

from strava_mcp_vault.cache.db import CacheDB
from strava_mcp_vault.cache.manager import CacheManager
from strava_mcp_vault.clients.strava import StravaClient
from strava_mcp_vault.training_load import config as tl_config
from strava_mcp_vault.training_load import load as tl_load


DEFAULT_ACTIVITY_ID = 18654899126  # May 25, 2026 ride


async def main():
    load_dotenv()
    activity_id = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ACTIVITY_ID

    # Use the live DB (so we share state with the running server)
    db_path = os.getenv("VAULT_DB_PATH", "./data/vault.db")
    db = CacheDB(db_path)
    await db.init()

    client = StravaClient(
        client_id=os.getenv("STRAVA_CLIENT_ID"),
        client_secret=os.getenv("STRAVA_CLIENT_SECRET"),
        cache_db=db,
    )
    await client.init_tokens()
    manager = CacheManager(db, client)

    # Resolve real user_id (Strava athlete_id)
    profile = await manager.get_athlete_profile()
    user_id = int(profile["id"])
    print(f"user_id (Strava athlete_id): {user_id}")

    # Idempotent FTP seed: only insert if no row covers the activity date
    # (which we'll fetch shortly). For simplicity, seed if no history at all.
    history = await tl_config.get_history(db._db, user_id, "ftp_watts")
    if not history:
        print("Seeding FTP=260 W effective from 2024-01-01...")
        await tl_config.set_field(
            db._db, user_id, "ftp_watts", 260, "2024-01-01"
        )
    else:
        print(f"FTP history already present ({len(history)} entries); skipping seed.")

    # Compute load
    print(f"\n=== Phase 2: compute_activity_load({activity_id}) ===")
    result = await tl_load.compute_activity_load(
        db._db, manager, activity_id, user_id
    )
    print(json.dumps(result, indent=2, default=str))

    print("\n=== Phase 3: get_training_load_today() ===")
    today = await tl_load.get_training_load_today(
        db._db, manager, user_id, forecast_days=7
    )
    print(json.dumps(today, indent=2, default=str))

    print("\n=== Phase 3: get_load_summary('month') ===")
    summary = await tl_load.get_load_summary(
        db._db, manager, user_id, period="month"
    )
    print(json.dumps(summary, indent=2, default=str))

    await db.close()
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
