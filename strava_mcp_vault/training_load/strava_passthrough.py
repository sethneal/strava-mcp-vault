"""Strava-native value passthroughs — NO computation on top.

These exist exactly so users can compare this MCP's computed TSS / CTL
output against Strava's own UI numbers (Relative Effort, Suffer Score).
The two won't match — they're different metrics — but they should track
together over time.

Per the project's design principle: where Strava exposes a value
directly, surface it separately rather than blending it into our
computed values. The tool-naming convention encodes this:
``compute_*`` does math, ``get_strava_*`` reads Strava's number as-is.

Public API
----------
- ``get_suffer_score(manager, activity_id)`` — pulls one activity's
  ``suffer_score`` field (Strava's "Relative Effort" since the rename).
- ``sum_suffer_scores(db, after, before_exclusive)`` — aggregates the
  field across vault activities in a date range.
"""

from __future__ import annotations

from typing import Any

import aiosqlite


async def get_suffer_score(
    manager: Any, activity_id: int
) -> dict[str, Any]:
    """Return one activity's raw ``suffer_score`` from Strava.

    Strava computes suffer_score from heart-rate data, so activities
    without HR have a null score. Returns ``note`` to surface that
    distinction explicitly rather than silently returning 0.
    """
    activity = await manager.get_activity(activity_id)
    suffer = activity.get("suffer_score")
    has_hr = bool(activity.get("has_heartrate"))
    note: str | None = None
    if suffer is None:
        if not has_hr:
            note = (
                "suffer_score is null because this activity has no heart "
                "rate data; Strava cannot compute Relative Effort without HR"
            )
        else:
            note = (
                "suffer_score is null even though HR is present — Strava "
                "may not have computed it yet, or the activity is too short"
            )
    return {
        "activity_id": activity_id,
        "suffer_score": suffer,
        "has_heartrate": has_hr,
        "note": note,
    }


async def sum_suffer_scores(
    db: Any,  # CacheDB
    after: str,
    before_exclusive: str,
    sport_type: str | None = None,
) -> dict[str, Any]:
    """Sum ``suffer_score`` across vault activities in
    ``[after, before_exclusive)``.

    Mirrors Strava's "Weekly Relative Effort" chart math (their own UI
    sums suffer_score the same way). Activities with null suffer_score
    are excluded from the sum but counted separately so the caller can
    see how much of the period had usable Relative Effort data.
    """
    total = 0.0
    activities_total = 0
    activities_with_score = 0

    batch_size = 200
    offset = 0
    while True:
        page = await db.get_vault_activities(
            limit=batch_size, offset=offset,
            after=after, before=before_exclusive,
            sport_type=sport_type,
        )
        if not page:
            break
        for activity in page:
            activities_total += 1
            score = activity.get("suffer_score")
            if score is not None:
                total += float(score)
                activities_with_score += 1
        if len(page) < batch_size:
            break
        offset += batch_size

    return {
        "after": after,
        "before_exclusive": before_exclusive,
        "total_suffer_score": total,
        "activities_total": activities_total,
        "activities_with_score": activities_with_score,
        "activities_without_score": activities_total - activities_with_score,
    }
