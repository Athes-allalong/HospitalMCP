"""Overlap-proof booking locks.

The unique index on (doctor_id, slot_start) only stops two bookings that start at the same minute.
With 30-minute and 15-minute appointment types on a 15-minute grid, a 10:00-10:30 booking and a
10:15-10:30 booking would both pass it. So every booking first claims one lock per 15-minute cell it
covers, for the doctor and for the patient. Each lock key is unique, so the database refuses a second
claim on the same cell atomically, with no transaction needed.

Flow:  acquire(...) -> insert appointment -> (on cancel) release(owner)
       reschedule: acquire(new_owner) -> update appointment -> release(old_owner)
Use owner = f"{appointment_id}:{n}" so old and new locks of one appointment can coexist briefly.
"""
from datetime import datetime, timedelta
from typing import List, Protocol

from ..errors import Codes, HospitalError

GRID_MIN = 15


class LockStore(Protocol):
    def try_lock(self, key: str, owner: str, expires_at: datetime) -> bool: ...
    def release(self, owner: str) -> None: ...


def cells(start: datetime, end: datetime, grid_min: int = GRID_MIN) -> List[datetime]:
    out, t = [], start
    while t < end:
        out.append(t)
        t += timedelta(minutes=grid_min)
    return out


def lock_keys(doctor_id: str, mrn: str, start: datetime, end: datetime) -> List[str]:
    keys = []
    for c in cells(start, end):
        stamp = c.strftime("%Y-%m-%dT%H:%M")
        keys += [f"D|{doctor_id}|{stamp}", f"P|{mrn}|{stamp}"]
    return keys


def acquire(store: LockStore, owner: str, doctor_id: str, mrn: str, start: datetime, end: datetime) -> str:
    if end <= start or start.minute % GRID_MIN or start.second or start.microsecond:
        raise HospitalError(Codes.INVALID_INPUT, f"Appointments start on a {GRID_MIN}-minute grid and must end after they start.")
    for key in lock_keys(doctor_id, mrn, start, end):
        if not store.try_lock(key, owner, end):
            store.release(owner)  # leave nothing behind
            who = "The patient already has an appointment" if key.startswith("P|") else "The doctor is already booked"
            raise HospitalError(Codes.SLOT_UNAVAILABLE, f"{who} during that time.",
                                hint="Call check_availability for current open slots.")
    return owner


def release(store: LockStore, owner: str) -> None:
    store.release(owner)
