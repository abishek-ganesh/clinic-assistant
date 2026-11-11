"""The mock appointment schedule: the only module that touches the schedule tables.

Open slots are computed in Postgres from clinic hours (see the schedule migration), so this
module is two RPC calls and the slot id format.
"""

import datetime
import re
from dataclasses import dataclass

from app.db import get_db
from app.models import Slot

VISIT_TYPES = ("returning", "physical", "telehealth")

# A slot id is "<provider slug>:<clinic-local start>", for example patel:2026-12-01T09:40
SLOT_ID_PATTERN = re.compile(r"([a-z]{1,20}):(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})")

# What to tell the caller for each reason book_slot() in Postgres can refuse.
REFUSALS = {
    "invalid_slot": "That is not a slot the clinic offers.",
    "slot_taken": "That slot was just booked by someone else.",
    "booking_limit": "This visitor has reached today's booking limit.",
}


class BookingError(Exception):
    """A booking that did not happen. reason is invalid_slot, slot_taken or booking_limit."""

    def __init__(self, reason: str):
        super().__init__(REFUSALS.get(reason, reason))
        self.reason = reason


@dataclass
class Confirmation:
    """A booked appointment, as book() returns it."""

    confirmation_code: str
    slot_id: str
    visit_type: str
    display: str


def parse_slot_id(slot_id: str) -> tuple[str, datetime.datetime]:
    """Split a slot id into provider slug and local start time, or raise BookingError."""
    match = SLOT_ID_PATTERN.fullmatch(slot_id) if isinstance(slot_id, str) else None
    if not match:
        raise BookingError("invalid_slot")
    try:
        starts = datetime.datetime.fromisoformat(match[2])
    except ValueError:  # the right shape but not a real date, like 2026-13-45
        raise BookingError("invalid_slot") from None
    return match[1], starts


async def check_availability(
    visit_type: str | None = None,
    on_or_after: datetime.date | None = None,
    limit: int = 5,
) -> list[Slot]:
    """Open slots, soonest first. Starts tomorrow and covers two weeks, in clinic time."""
    db = await get_db()
    result = await db.rpc(
        "available_slots",
        {
            "p_visit_type": visit_type,
            "p_from": on_or_after.isoformat() if on_or_after else None,
            "p_limit": limit,
        },
    ).execute()
    return [Slot.model_validate(row) for row in result.data]


async def book(slot_id: str, ip_hash: str) -> Confirmation:
    """Book one slot. Raises BookingError when the slot is invalid, taken or over the limit."""
    provider, starts = parse_slot_id(slot_id)
    db = await get_db()
    # Postgres checks the slot against the generated schedule and inserts in one call. The
    # unique constraint on (provider, starts_at) settles races between two visitors.
    result = await db.rpc(
        "book_slot",
        {
            "p_provider": provider,
            "p_starts_local": starts.isoformat(),
            "p_ip_hash": ip_hash,
        },
    ).execute()
    row = result.data
    if not row["booked"]:
        raise BookingError(row["reason"])
    return Confirmation(
        confirmation_code=row["confirmation_code"],
        slot_id=row["slot_id"],
        visit_type=row["visit_type"],
        display=row["display"],
    )
