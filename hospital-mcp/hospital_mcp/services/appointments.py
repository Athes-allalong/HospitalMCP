"""Appointments: list, read, book, reschedule, cancel, clinical notes.

Booking safety, in layers:
  1. open_slots() check (schedule, leave, holidays, existing bookings)
  2. slot locks: one lock per 15-minute cell for the doctor AND the patient (no partial overlaps)
  3. unique partial indexes on (hospital, doctor, start) and (hospital, mrn, start) while booked
  4. preview-then-commit + request_id idempotency
"""
import uuid
from datetime import date, datetime, time, timedelta
from typing import Any, Optional

from ..authz import authorize
from ..db import DuplicateKeyError
from ..errors import Codes, HospitalError
from .common import bad, clean, inr, not_found, parse_date, parse_dt
from .directory import TYPE_MINUTES, explain_unavailable, get_active_doctor, open_slots
from .pending import Plan, idempotent, two_phase
from .slots import acquire, release

LATE_CANCEL_HOURS = 2


class TenantLockStore:
    """slots.LockStore over the tenant-scoped slot_locks collection (keys carry the hospital id too)."""

    def __init__(self, ctx):
        self.c, self.h = ctx.t.slot_locks, ctx.ident.hospital_id

    def try_lock(self, key: str, owner: str, expires_at: datetime) -> bool:
        try:
            self.c.insert_one({"_id": f"{self.h}|{key}", "owner": owner, "expires_at": expires_at})
            return True
        except DuplicateKeyError:
            return False

    def release(self, owner: str) -> None:
        self.c.delete_many({"owner": owner})


def _get_appt(ctx, appointment_id: str) -> dict:
    a = ctx.t.appointments.find_one({"appointment_id": appointment_id})
    if not a:
        raise not_found(f"Appointment '{appointment_id}'")
    return a


def _authz_appt(ctx, tool: str, a: dict):
    return authorize(ctx.ident, tool, doctor_id=a["doctor_id"], mrn=a["mrn"], appointment=a, ctx=ctx)


def _patient_busy(ctx, mrn: str, start: datetime, end: datetime, exclude: Optional[str] = None) -> None:
    q: dict = {"mrn": mrn, "status": "booked", "slot_start": {"$lt": end}, "slot_end": {"$gt": start}}
    if exclude:
        q["appointment_id"] = {"$ne": exclude}
    clash = ctx.t.appointments.find_one(q)
    if clash:
        raise HospitalError(Codes.SLOT_UNAVAILABLE,
                            f"The patient already has an appointment at {clash['slot_start']:%H:%M} that day "
                            f"({clash.get('doctor_name', clash['doctor_id'])}).",
                            hint="Pick a time that does not overlap, or reschedule the other appointment.")


def _view(a: dict) -> dict:
    keep = ("appointment_id", "mrn", "patient_name", "doctor_id", "doctor_name", "department_id", "type",
            "slot_start", "slot_end", "status", "reason", "cancel_reason", "late_cancellation", "fee_inr")
    return clean({k: a[k] for k in keep if k in a})


# ---------------------------------------------------------------- reads
def list_appointments(ctx, date_from: Optional[str] = None, date_to: Optional[str] = None,
                      doctor_id: Optional[str] = None, status: Optional[str] = "booked", limit: int = 50) -> dict:
    d = authorize(ctx.ident, "list_appointments", listing=True, ctx=ctx)
    d0 = parse_date(date_from, "date_from") if date_from else ctx.clock().date()
    d1 = parse_date(date_to, "date_to") if date_to else d0 + timedelta(days=7)
    if d1 < d0 or (d1 - d0).days > 31:
        raise bad("date range must be ordered and at most 31 days.")
    if not 1 <= limit <= 100:
        raise bad("limit must be 1-100.")
    q: dict = {"slot_start": {"$gte": datetime.combine(d0, time.min),
                              "$lt": datetime.combine(d1 + timedelta(days=1), time.min)}}
    if status:
        q["status"] = status
    if doctor_id:
        q["doctor_id"] = doctor_id
    if d.restrict_doctor_id:
        if doctor_id and doctor_id != d.restrict_doctor_id:
            raise HospitalError(Codes.FORBIDDEN, "Doctors can only list their own appointments.")
        q["doctor_id"] = d.restrict_doctor_id
    if d.restrict_department_id:
        q["department_id"] = d.restrict_department_id
    rows = list(ctx.t.appointments.find(q).sort("slot_start", 1).limit(limit))
    ctx.audit("list_appointments", f"{d0}..{d1}")
    return {"from": d0.isoformat(), "to": d1.isoformat(), "count": len(rows), "appointments": [_view(a) for a in rows]}


def get_appointment(ctx, appointment_id: str) -> dict:
    a = _get_appt(ctx, appointment_id)
    _authz_appt(ctx, "get_appointment", a)
    ctx.audit("get_appointment", appointment_id)
    out = _view(a)
    if ctx.ident.role == "doctor":
        out["clinical_notes"] = clean(list(ctx.t.clinical_notes.find({"appointment_id": appointment_id})
                                           .sort("created_at", 1)))
    return out


def get_appointment_history(ctx, mrn: str) -> dict:
    if ctx.ident.role == "doctor":
        d = authorize(ctx.ident, "get_appointment_history", listing=True)
        q = {"mrn": mrn, "doctor_id": d.restrict_doctor_id}
    elif ctx.ident.role == "nurse":
        authorize(ctx.ident, "get_appointment_history", mrn=mrn, ctx=ctx)
        q = {"mrn": mrn, "department_id": ctx.ident.department_id}
    else:
        authorize(ctx.ident, "get_appointment_history")
        if not ctx.t.patients.find_one({"mrn": mrn}):
            raise not_found(f"Patient '{mrn}'")
        q = {"mrn": mrn}
    rows = list(ctx.t.appointments.find(q).sort("slot_start", -1).limit(100))
    ctx.audit("get_appointment_history", mrn)
    return {"mrn": mrn, "appointments": [_view(a) for a in rows]}


# ---------------------------------------------------------------- book
def book_appointment(ctx, mrn: str, doctor_id: str, slot_start: str, type: str = "new",
                     reason: Optional[str] = None, request_id: Optional[str] = None,
                     confirmation_id: Optional[str] = None) -> dict:
    tool = "book_appointment"
    authorize(ctx.ident, tool, doctor_id=doctor_id)
    args = {"mrn": mrn, "doctor_id": doctor_id, "slot_start": slot_start, "type": type, "reason": reason}

    def prepare() -> Plan:
        if type not in TYPE_MINUTES:
            raise bad(f"type must be one of {', '.join(TYPE_MINUTES)}.")
        p = ctx.t.patients.find_one({"mrn": mrn})
        if not p:
            raise HospitalError(Codes.NOT_FOUND, f"Patient '{mrn}' not found.",
                                hint="Use search_patients, or register_patient for a new patient.")
        doc = get_active_doctor(ctx, doctor_id)
        if type not in doc.get("visit_types", list(TYPE_MINUTES)):
            raise bad(f"{doc['name']} does not offer '{type}' visits.")
        start = parse_dt(slot_start, "slot_start")
        if start not in open_slots(ctx, doc, start.date(), start.date(), type):
            raise explain_unavailable(ctx, doc, start, type)
        end = start + timedelta(minutes=TYPE_MINUTES[type])
        _patient_busy(ctx, mrn, start, end)
        a = {"appointment_id": f"APT-{uuid.uuid4().hex[:8].upper()}", "mrn": mrn, "patient_name": p["name"],
             "doctor_id": doctor_id, "doctor_name": doc["name"], "department_id": doc.get("department_id"),
             "type": type, "slot_start": start, "slot_end": end, "status": "booked",
             "reason": (reason or "")[:300], "fee_inr": inr(doc.get("consultation_fee_minor")),
             "lock_gen": 0, "created_by": ctx.ident.subject, "created_at": ctx.clock()}
        summary = {"action": "book_appointment", "patient": f"{p['name']} ({mrn})", "doctor": doc["name"],
                   "department": doc.get("department_name"), "start": start.isoformat(), "end": end.isoformat(),
                   "type": type, "fee_inr": a["fee_inr"], "room": doc.get("room")}

        def apply():
            owner = f"{a['appointment_id']}:0"
            acquire(TenantLockStore(ctx), owner, doctor_id, mrn, start, end)
            try:
                ctx.t.appointments.insert_one(dict(a))
            except DuplicateKeyError:
                release(TenantLockStore(ctx), owner)
                raise explain_unavailable(ctx, doc, start, type)
            return None, _view(a)
        return Plan(f"{doctor_id}@{start.isoformat()}", summary, apply)

    return idempotent(ctx, tool, request_id if confirmation_id else None,
                      lambda: two_phase(ctx, tool, args, confirmation_id, prepare))


# ---------------------------------------------------------------- reschedule
def reschedule_appointment(ctx, appointment_id: str, new_slot_start: str, request_id: Optional[str] = None,
                           confirmation_id: Optional[str] = None) -> dict:
    tool = "reschedule_appointment"
    a0 = _get_appt(ctx, appointment_id)
    _authz_appt(ctx, tool, a0)
    args = {"appointment_id": appointment_id, "new_slot_start": new_slot_start}

    def prepare() -> Plan:
        a = _get_appt(ctx, appointment_id)
        if a["status"] != "booked":
            raise bad(f"Only booked appointments can be rescheduled (status: {a['status']}).")
        doc = get_active_doctor(ctx, a["doctor_id"])
        start = parse_dt(new_slot_start, "new_slot_start")
        if start == a["slot_start"]:
            raise bad("That is the current time of this appointment.")
        if start not in open_slots(ctx, doc, start.date(), start.date(), a["type"], exclude_appt=appointment_id):
            raise explain_unavailable(ctx, doc, start, a["type"])
        end = start + timedelta(minutes=TYPE_MINUTES[a["type"]])
        _patient_busy(ctx, a["mrn"], start, end, exclude=appointment_id)
        summary = {"action": "reschedule_appointment", "appointment_id": appointment_id,
                   "patient": a.get("patient_name"), "doctor": doc["name"],
                   "from": a["slot_start"].isoformat(), "to": start.isoformat()}

        def apply():
            gen = a.get("lock_gen", 0)
            locks = TenantLockStore(ctx)
            acquire(locks, f"{appointment_id}:{gen + 1}", a["doctor_id"], a["mrn"], start, end)
            try:
                res = ctx.t.appointments.find_one_and_update(
                    {"appointment_id": appointment_id, "status": "booked", "lock_gen": gen},
                    {"$set": {"slot_start": start, "slot_end": end, "lock_gen": gen + 1, "updated_at": ctx.clock()},
                     "$push": {"history": {"from": a["slot_start"], "to": start, "by": ctx.ident.subject,
                                           "at": ctx.clock()}}},
                    return_document=True)
            except DuplicateKeyError:
                res = None
            if not res:
                release(locks, f"{appointment_id}:{gen + 1}")
                raise HospitalError(Codes.SLOT_UNAVAILABLE, "The appointment or the slot changed meanwhile.",
                                    hint="Check availability again and retry.")
            release(locks, f"{appointment_id}:{gen}")
            return {"slot_start": a["slot_start"].isoformat()}, {"slot_start": start.isoformat()}
        return Plan(appointment_id, summary, apply)

    return idempotent(ctx, tool, request_id if confirmation_id else None,
                      lambda: two_phase(ctx, tool, args, confirmation_id, prepare))


# ---------------------------------------------------------------- cancel
def cancel_appointment(ctx, appointment_id: str, reason: str, request_id: Optional[str] = None,
                       confirmation_id: Optional[str] = None) -> dict:
    tool = "cancel_appointment"
    a0 = _get_appt(ctx, appointment_id)
    _authz_appt(ctx, tool, a0)
    args = {"appointment_id": appointment_id, "reason": reason}

    def prepare() -> Plan:
        a = _get_appt(ctx, appointment_id)
        if a["status"] != "booked":
            raise bad(f"Only booked appointments can be cancelled (status: {a['status']}).")
        now = ctx.clock()
        if a["slot_start"] <= now:
            raise bad("Past appointments cannot be cancelled.")
        if not isinstance(reason, str) or not 2 <= len(reason.strip()) <= 300:
            raise bad("reason must be 2-300 characters.")
        late = a["slot_start"] - now < timedelta(hours=LATE_CANCEL_HOURS)
        summary = {"action": "cancel_appointment", "appointment_id": appointment_id,
                   "patient": a.get("patient_name"), "doctor": a.get("doctor_name"),
                   "start": a["slot_start"].isoformat(), "reason": reason.strip(),
                   "late_cancellation": late}
        if late:
            summary["note"] = f"Less than {LATE_CANCEL_HOURS} hours before the slot: recorded as a late cancellation."

        def apply():
            res = ctx.t.appointments.find_one_and_update(
                {"appointment_id": appointment_id, "status": "booked"},
                {"$set": {"status": "cancelled", "cancel_reason": reason.strip(), "late_cancellation": late,
                          "cancelled_by": ctx.ident.subject, "cancelled_at": now}},
                return_document=True)
            if not res:
                raise HospitalError(Codes.CONFIRMATION_INVALID, "The appointment changed meanwhile; nothing done.")
            release(TenantLockStore(ctx), f"{appointment_id}:{a.get('lock_gen', 0)}")
            return {"status": "booked"}, {"status": "cancelled", "late_cancellation": late}
        return Plan(appointment_id, summary, apply)

    return idempotent(ctx, tool, request_id if confirmation_id else None,
                      lambda: two_phase(ctx, tool, args, confirmation_id, prepare))


# ---------------------------------------------------------------- notes
def add_clinical_note(ctx, appointment_id: str, note: str) -> dict:
    a = _get_appt(ctx, appointment_id)
    _authz_appt(ctx, "add_clinical_note", a)
    if a["status"] == "cancelled":
        raise bad("Cannot add notes to a cancelled appointment.")
    if not isinstance(note, str) or not note.strip() or len(note) > 10_000:
        raise bad("note must be 1-10000 characters.")
    n = {"note_id": f"NOTE-{uuid.uuid4().hex[:8].upper()}", "appointment_id": appointment_id, "mrn": a["mrn"],
         "doctor_id": ctx.ident.subject, "note": note.strip(), "created_at": ctx.clock()}
    ctx.t.clinical_notes.insert_one(dict(n))
    ctx.audit("add_clinical_note", appointment_id)
    return {"note_id": n["note_id"], "appointment_id": appointment_id, "created_at": n["created_at"].isoformat()}
