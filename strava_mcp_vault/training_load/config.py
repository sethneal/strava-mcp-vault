"""Athlete configuration layer — FTP, LTHR, weight with effective-date history.

Physiological inputs are first-class data, not constants. Every value lives in
``athlete_config_history`` with an ``effective_from``/``effective_to`` window,
and the resolver picks the row that covers a given date. This means a TSS
computed against an activity from 6 months ago uses the FTP that was in
effect 6 months ago, not today's FTP applied retroactively.

Public API
----------
- ``get_config_at(conn, user_id, date)`` — resolver
- ``set_field(conn, user_id, field_name, value, effective_from)`` — writes
  a new value, closing out the current open row at the same date
- ``set_field_historical(conn, user_id, field_name, value, effective_from,
  effective_to)`` — inserts a closed historical row (backfill) that must
  not overlap with any existing row for the same field
- ``get_history(conn, user_id, field_name)`` — full audit trail for a field
- ``delete_field_row(conn, user_id, field_name, effective_from)`` — delete one
  row by stable identity; may leave a gap (allowed)
- ``edit_field_row(conn, user_id, field_name, effective_from, ...)`` — edit one
  row's value and/or dates, re-validating overlap and single-open invariants
- ``to_kg(value, unit)`` — trivial kg/lb conversion; unit is required at the
  tool surface so the caller (LLM or human) commits to a unit before
  reaching this layer
- ``validate_value(field_name, value)`` — range check
- ``ValidationError`` — raised on bad input
"""

from __future__ import annotations

import time
from typing import Literal

import aiosqlite

FieldName = Literal["ftp_watts", "lthr_bpm", "weight_kg"]
FIELD_NAMES: tuple[FieldName, ...] = ("ftp_watts", "lthr_bpm", "weight_kg")
WeightUnit = Literal["kg", "lb"]

# Inclusive validation ranges. Stored values must fall within.
RANGES: dict[FieldName, tuple[float, float]] = {
    "ftp_watts": (50, 500),
    "lthr_bpm": (100, 210),
    "weight_kg": (30, 200),
}

LB_TO_KG = 0.45359237

# Sentinel distinguishing "leave effective_to unchanged" from "set it to NULL
# (make the row open)" in edit_field_row, since None is a meaningful value.
UNSET = object()


class ValidationError(ValueError):
    """Raised when a config write fails validation.

    Surfaced to MCP callers as a clear error string by the tool wrapper.
    """


def validate_value(field_name: FieldName, value: float) -> None:
    """Raise ValidationError if ``value`` is outside the field's allowed range."""
    if field_name not in RANGES:
        raise ValidationError(f"unknown field_name: {field_name!r}")
    low, high = RANGES[field_name]
    if not (low <= value <= high):
        raise ValidationError(
            f"{field_name}={value} out of range [{low}, {high}]. "
            f"Reject rather than coerce — verify the input."
        )


def to_kg(value: float, unit: WeightUnit) -> float:
    """Convert weight to kg. ``unit`` is required at the tool surface so
    we never have to guess what 180 means — the caller already committed."""
    if unit == "kg":
        return float(value)
    if unit == "lb":
        return float(value) * LB_TO_KG
    # Defensive: Literal typing should catch this upstream, but if the
    # tool layer is bypassed (e.g., direct Python use), give a clear error.
    raise ValidationError(f"unit must be 'kg' or 'lb', got {unit!r}")


def _effective_key(field_name: FieldName) -> str:
    """Map ``ftp_watts`` → ``ftp_effective_from`` (and likewise lthr, weight)."""
    short = field_name.split("_")[0]
    return f"{short}_effective_from"


async def get_config_at(
    conn: aiosqlite.Connection,
    user_id: int,
    date: str,
) -> dict[str, float | str | None]:
    """Resolve effective config values at ``date`` (ISO ``YYYY-MM-DD``).

    For each field, picks the row where ``effective_from <= date`` and
    ``effective_to IS NULL OR effective_to > date`` (half-open interval).

    Returns a dict with all six keys::

        {
            "ftp_watts": float | None,
            "ftp_effective_from": str | None,
            "lthr_bpm": float | None,
            "lthr_effective_from": str | None,
            "weight_kg": float | None,
            "weight_effective_from": str | None,
        }

    Fields with no covering row return ``None``. There are no defaults —
    callers must decide what to do with missing values.
    """
    result: dict[str, float | str | None] = {}
    for field in FIELD_NAMES:
        cursor = await conn.execute(
            """
            SELECT value, effective_from FROM athlete_config_history
            WHERE user_id = ? AND field_name = ?
              AND effective_from <= ?
              AND (effective_to IS NULL OR effective_to > ?)
            ORDER BY effective_from DESC
            LIMIT 1
            """,
            (user_id, field, date, date),
        )
        row = await cursor.fetchone()
        if row is None:
            result[field] = None
            result[_effective_key(field)] = None
        else:
            result[field] = row[0]
            result[_effective_key(field)] = row[1]
    return result


async def set_field(
    conn: aiosqlite.Connection,
    user_id: int,
    field_name: FieldName,
    value: float,
    effective_from: str,
) -> None:
    """Close the current open row for ``field_name`` at ``effective_from``,
    then insert a new open row with ``value`` taking effect at that date.

    Caller is responsible for upstream validation (range check, weight unit
    conversion). This function only enforces the temporal invariant: the
    new ``effective_from`` must be strictly after any existing open row's
    ``effective_from`` (no backfill in v1).
    """
    cursor = await conn.execute(
        """
        SELECT effective_from FROM athlete_config_history
        WHERE user_id = ? AND field_name = ? AND effective_to IS NULL
        """,
        (user_id, field_name),
    )
    open_row = await cursor.fetchone()
    if open_row is not None and open_row[0] >= effective_from:
        raise ValidationError(
            f"{field_name}: cannot set value with effective_from={effective_from}; "
            f"current open row started on {open_row[0]} (later or equal). "
            f"Backfill of historical values is not supported in v1 — "
            f"set values forward in time only."
        )

    await conn.execute(
        """
        UPDATE athlete_config_history
        SET effective_to = ?
        WHERE user_id = ? AND field_name = ? AND effective_to IS NULL
        """,
        (effective_from, user_id, field_name),
    )
    await conn.execute(
        """
        INSERT INTO athlete_config_history
            (user_id, field_name, value, effective_from, effective_to, created_at)
        VALUES (?, ?, ?, ?, NULL, ?)
        """,
        (user_id, field_name, value, effective_from, time.time()),
    )
    await conn.commit()


async def set_field_historical(
    conn: aiosqlite.Connection,
    user_id: int,
    field_name: FieldName,
    value: float,
    effective_from: str,
    effective_to: str,
) -> None:
    """Insert a closed historical row for backfill.

    The window ``[effective_from, effective_to)`` must:
    - have ``effective_from < effective_to`` (positive duration)
    - not overlap with any existing row for this user + field

    Two rows overlap if and only if::

        R.effective_from < new.effective_to
        AND (R.effective_to IS NULL OR R.effective_to > new.effective_from)

    This is the standard half-open interval overlap predicate. If you need
    to splice into an existing window, delete or split the existing rows
    first — this function refuses to touch other rows.
    """
    if effective_from >= effective_to:
        raise ValidationError(
            f"{field_name}: effective_from ({effective_from}) must be "
            f"strictly before effective_to ({effective_to})."
        )

    cursor = await conn.execute(
        """
        SELECT effective_from, effective_to FROM athlete_config_history
        WHERE user_id = ? AND field_name = ?
          AND effective_from < ?
          AND (effective_to IS NULL OR effective_to > ?)
        LIMIT 1
        """,
        (user_id, field_name, effective_to, effective_from),
    )
    overlap = await cursor.fetchone()
    if overlap is not None:
        existing_from, existing_to = overlap
        existing_to_str = existing_to or "open"
        raise ValidationError(
            f"{field_name}: window [{effective_from}, {effective_to}) overlaps "
            f"with existing row [{existing_from}, {existing_to_str}). "
            f"Historical inserts must land in gaps; modify the existing row first "
            f"if you need to splice."
        )

    await conn.execute(
        """
        INSERT INTO athlete_config_history
            (user_id, field_name, value, effective_from, effective_to, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (user_id, field_name, value, effective_from, effective_to, time.time()),
    )
    await conn.commit()


async def get_history(
    conn: aiosqlite.Connection,
    user_id: int,
    field_name: FieldName,
) -> list[dict[str, float | str | None]]:
    """Return all rows for one field, newest ``effective_from`` first.

    Used by the audit / "show me the history" tool. Each row is a dict
    with ``value``, ``effective_from``, ``effective_to``, ``created_at``.
    """
    if field_name not in FIELD_NAMES:
        raise ValidationError(
            f"field_name must be one of {FIELD_NAMES}, got {field_name!r}"
        )
    cursor = await conn.execute(
        """
        SELECT value, effective_from, effective_to, created_at
        FROM athlete_config_history
        WHERE user_id = ? AND field_name = ?
        ORDER BY effective_from DESC
        """,
        (user_id, field_name),
    )
    rows = await cursor.fetchall()
    return [
        {
            "value": r[0],
            "effective_from": r[1],
            "effective_to": r[2],
            "created_at": r[3],
        }
        for r in rows
    ]


async def delete_field_row(
    conn: aiosqlite.Connection,
    user_id: int,
    field_name: FieldName,
    effective_from: str,
) -> dict[str, float | str | None]:
    """Delete a single config row identified by (field_name, effective_from).

    Returns the deleted row's values so the caller can re-add it via
    ``set_field_historical`` if the deletion was a mistake. Deleting a row
    may leave a gap in the timeline; that is allowed — the resolver returns
    ``None`` for uncovered dates.
    """
    if field_name not in FIELD_NAMES:
        raise ValidationError(
            f"field_name must be one of {FIELD_NAMES}, got {field_name!r}"
        )
    cursor = await conn.execute(
        """
        SELECT id, value, effective_to FROM athlete_config_history
        WHERE user_id = ? AND field_name = ? AND effective_from = ?
        """,
        (user_id, field_name, effective_from),
    )
    row = await cursor.fetchone()
    if row is None:
        raise ValidationError(
            f"no {field_name} row with effective_from={effective_from} to delete. "
            f"Check strava_get_athlete_config_history for the exact start date."
        )
    row_id, value, effective_to = row
    await conn.execute(
        "DELETE FROM athlete_config_history WHERE id = ?", (row_id,)
    )
    await conn.commit()
    return {
        "field_name": field_name,
        "value": value,
        "effective_from": effective_from,
        "effective_to": effective_to,
    }


# A date string guaranteed to sort after any real ISO date, used as the upper
# bound when an edited row is open (effective_to IS NULL) so the half-open
# overlap predicate works unchanged.
_OPEN_UPPER_BOUND = "9999-12-31"


async def edit_field_row(
    conn: aiosqlite.Connection,
    user_id: int,
    field_name: FieldName,
    effective_from: str,
    *,
    new_value: float | None = None,
    new_effective_from: str | None = None,
    new_effective_to: str | None | object = UNSET,
) -> dict[str, dict[str, float | str | None]]:
    """Edit a single config row located by (field_name, effective_from).

    Only supplied fields change:
    - ``new_value``: new value (already converted to the stored unit, e.g. kg).
      ``None`` means unchanged. Range-checked via ``validate_value``.
    - ``new_effective_from``: new start date. ``None`` means unchanged.
    - ``new_effective_to``: ``UNSET`` (default) means unchanged; ``None`` means
      make the row open (effective_to = NULL); a date string sets a closed end.

    Re-validates invariants against neighbouring rows (overlap, single-open,
    positive duration), excluding the row being edited. Returns
    ``{"before": {...}, "after": {...}}`` for an easy manual revert.

    Identity is the ``(user_id, field_name, effective_from)`` triple. This is
    unique in practice because the write path rejects overlaps (two rows for
    one field can't share a start date without overlapping), so the lookup's
    ``fetchone()`` is unambiguous.
    """
    if field_name not in FIELD_NAMES:
        raise ValidationError(
            f"field_name must be one of {FIELD_NAMES}, got {field_name!r}"
        )
    cursor = await conn.execute(
        """
        SELECT id, value, effective_from, effective_to
        FROM athlete_config_history
        WHERE user_id = ? AND field_name = ? AND effective_from = ?
        """,
        (user_id, field_name, effective_from),
    )
    target = await cursor.fetchone()
    if target is None:
        raise ValidationError(
            f"no {field_name} row with effective_from={effective_from} to edit. "
            f"Check strava_get_athlete_config_history for the exact start date."
        )
    row_id, cur_value, cur_from, cur_to = target

    value_final = cur_value if new_value is None else new_value
    from_final = cur_from if new_effective_from is None else new_effective_from
    to_final = cur_to if new_effective_to is UNSET else new_effective_to

    if new_value is not None:
        validate_value(field_name, value_final)

    if to_final is not None and from_final >= to_final:
        raise ValidationError(
            f"{field_name}: effective_from ({from_final}) must be strictly "
            f"before effective_to ({to_final})."
        )

    # Overlap check, excluding the row being edited.
    to_bound = to_final if to_final is not None else _OPEN_UPPER_BOUND
    cursor = await conn.execute(
        """
        SELECT effective_from, effective_to FROM athlete_config_history
        WHERE user_id = ? AND field_name = ? AND id != ?
          AND effective_from < ?
          AND (effective_to IS NULL OR effective_to > ?)
        LIMIT 1
        """,
        (user_id, field_name, row_id, to_bound, from_final),
    )
    overlap = await cursor.fetchone()
    if overlap is not None:
        existing_from, existing_to = overlap
        existing_to_str = existing_to or "open"
        to_str = to_final or "open"
        raise ValidationError(
            f"{field_name}: edited window [{from_final}, {to_str}) overlaps "
            f"with existing row [{existing_from}, {existing_to_str})."
        )

    # Single-open check, excluding the row being edited.
    if to_final is None:
        cursor = await conn.execute(
            """
            SELECT effective_from FROM athlete_config_history
            WHERE user_id = ? AND field_name = ? AND id != ?
              AND effective_to IS NULL
            LIMIT 1
            """,
            (user_id, field_name, row_id),
        )
        other_open = await cursor.fetchone()
        if other_open is not None:
            raise ValidationError(
                f"{field_name}: another open row already starts on "
                f"{other_open[0]}. Close it first — only one open row allowed."
            )

    await conn.execute(
        """
        UPDATE athlete_config_history
        SET value = ?, effective_from = ?, effective_to = ?
        WHERE id = ?
        """,
        (value_final, from_final, to_final, row_id),
    )
    await conn.commit()
    return {
        "before": {
            "value": cur_value,
            "effective_from": cur_from,
            "effective_to": cur_to,
        },
        "after": {
            "value": value_final,
            "effective_from": from_final,
            "effective_to": to_final,
        },
    }
