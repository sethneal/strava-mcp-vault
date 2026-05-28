"""One-shot end-to-end verification for Phase 2.

Seeds FTP=260 effective from 2024-01-01 (covers all of Seth's recent rides),
then runs compute_activity_load on a target activity using the LIVE vault DB
and a LIVE CacheManager (auth + streams from Strava as needed).

Run: .venv/bin/python scripts/verify_phase2.py [activity_id]
Default activity_id is the user's most recent ride id.
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
    print(f"\nComputing load for activity {activity_id}...")
    result = await tl_load.compute_activity_load(
        db._db, manager, activity_id, user_id
    )
    print(json.dumps(result, indent=2, default=str))

    await db.close()
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
