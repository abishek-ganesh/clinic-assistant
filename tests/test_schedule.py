"""Slot ids and the two schedule RPCs, with the Supabase client replaced by a fake."""

import datetime

import pytest

from app import schedule
from app.schedule import BookingError, book, check_availability, parse_slot_id

ROW = {
    "slot_id": "patel:2026-12-01T09:40",
    "provider": "Dr. Patel",
    "visit_type": "returning",
    "starts_at": "2026-12-01T09:40:00-05:00",
    "display": "Tue Dec 1, 9:40 AM with Dr. Patel (Main Street)",
}


class FakeDb:
    """Records each rpc call and answers with the data queued for that function."""

    def __init__(self, **data):
        self.data = data
        self.calls = []

    def rpc(self, name, params):
        self.calls.append((name, params))
        return self

    async def execute(self):
        name, _ = self.calls[-1]
        return type("Result", (), {"data": self.data[name]})()


@pytest.fixture
def use_db(monkeypatch):
    def _use(**data) -> FakeDb:
        db = FakeDb(**data)

        async def get_db():
            return db

        monkeypatch.setattr(schedule, "get_db", get_db)
        return db

    return _use


def test_parse_slot_id_splits_provider_and_local_start():
    assert parse_slot_id("patel:2026-12-01T09:40") == (
        "patel",
        datetime.datetime(2026, 12, 1, 9, 40),
    )


@pytest.mark.parametrize(
    "slot_id",
    [
        "",
        "patel",
        "patel:2026-12-01",
        "patel:2026-12-01T09:40:00",
        "patel:2026-12-01T09:40-05:00",
        "patel:2026-13-45T09:40",
        "patel:2026-12-01T25:00",
        "Dr. Patel:2026-12-01T09:40",
        "patel:2026-12-01T09:40; drop table appointments",
        " patel:2026-12-01T09:40",
        None,
        42,
    ],
)
def test_parse_slot_id_rejects_anything_else(slot_id):
    with pytest.raises(BookingError) as error:
        parse_slot_id(slot_id)
    assert error.value.reason == "invalid_slot"


async def test_check_availability_calls_the_rpc_and_returns_slots(use_db):
    db = use_db(available_slots=[ROW])
    slots = await check_availability("returning", datetime.date(2026, 12, 1), 3)
    assert db.calls == [
        ("available_slots", {"p_visit_type": "returning", "p_from": "2026-12-01", "p_limit": 3})
    ]
    assert [slot.slot_id for slot in slots] == ["patel:2026-12-01T09:40"]
    assert slots[0].display.endswith("(Main Street)")


async def test_check_availability_defaults_leave_the_dates_to_postgres(use_db):
    db = use_db(available_slots=[])
    assert await check_availability() == []
    assert db.calls[0][1] == {"p_visit_type": None, "p_from": None, "p_limit": 5}


async def test_book_sends_provider_and_local_time_and_returns_the_confirmation(use_db):
    booked = {
        "booked": True,
        "confirmation_code": "FC-3F9A2C",
        "slot_id": ROW["slot_id"],
        "visit_type": "returning",
        "display": ROW["display"],
    }
    db = use_db(book_slot=booked)
    confirmation = await book(ROW["slot_id"], "iphash")
    assert db.calls == [
        (
            "book_slot",
            {"p_provider": "patel", "p_starts_local": "2026-12-01T09:40:00", "p_ip_hash": "iphash"},
        )
    ]
    assert confirmation.confirmation_code == "FC-3F9A2C"
    assert confirmation.display == ROW["display"]


@pytest.mark.parametrize("reason", ["slot_taken", "invalid_slot", "booking_limit"])
async def test_book_raises_with_the_reason_postgres_gave(use_db, reason):
    use_db(book_slot={"booked": False, "reason": reason})
    with pytest.raises(BookingError) as error:
        await book(ROW["slot_id"], "iphash")
    assert error.value.reason == reason
    assert str(error.value) == schedule.REFUSALS[reason]


async def test_book_never_reaches_the_database_with_a_malformed_slot_id(use_db):
    db = use_db(book_slot={"booked": True})
    with pytest.raises(BookingError):
        await book("patel:tomorrow", "iphash")
    assert db.calls == []
