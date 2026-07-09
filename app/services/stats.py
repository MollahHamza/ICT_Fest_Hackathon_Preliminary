"""Live per-room booking statistics.

Confirmed-booking counts and revenue are tracked incrementally so callers have
a fast path, but the bookings table is the authoritative source: ``get`` derives
its result directly from it whenever a session is available. This keeps the
stats consistent with the bookings themselves in every case, including after a
process restart (when the in-memory counters would otherwise start from zero
against an existing database).
"""
import threading

from sqlalchemy.orm import Session

from ..models import Booking

_stats: dict[int, dict] = {}
_stats_lock = threading.Lock()


def record_create(room_id: int, price_cents: int) -> None:
    with _stats_lock:
        current = _stats.get(room_id, {"count": 0, "revenue": 0})
        _stats[room_id] = {
            "count": current["count"] + 1,
            "revenue": current["revenue"] + price_cents,
        }


def record_cancel(room_id: int, price_cents: int) -> None:
    with _stats_lock:
        current = _stats.get(room_id, {"count": 0, "revenue": 0})
        _stats[room_id] = {
            "count": max(0, current["count"] - 1),
            "revenue": current["revenue"] - price_cents,
        }


def get(room_id: int, db: Session | None = None) -> dict:
    """Return a room's confirmed-booking count and summed price_cents.

    When a database session is supplied the values are derived from the
    bookings table, so they always match the bookings themselves regardless of
    process lifetime. The in-memory counters are only used as a fallback when
    no session is available.
    """
    if db is not None:
        rows = (
            db.query(Booking)
            .filter(Booking.room_id == room_id, Booking.status == "confirmed")
            .all()
        )
        return {"count": len(rows), "revenue": sum(r.price_cents for r in rows)}
    with _stats_lock:
        return _stats.get(room_id, {"count": 0, "revenue": 0})
