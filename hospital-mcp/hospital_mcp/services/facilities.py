"""Facility status: ICU, ER, OT, blood bank, department queues. Public callers get summaries only."""
from datetime import datetime, time, timedelta
from typing import Optional

from ..authz import authorize
from ..errors import Codes, HospitalError
from .common import bad, clean, not_found, parse_date

BLOOD_GROUPS = ("A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-")
FACILITY_FIELDS = {
    "icu": {"total_beds": int, "occupied": int, "ventilators_total": int, "ventilators_in_use": int},
    "er": {"wait_minutes": int, "triage_queue": int, "diversion": bool},
    "ward": {"occupancy_percent": int},
}


def _facility(ctx, name: str) -> dict:
    return ctx.t.facilities.find_one({"facility": name}) or {}


def get_icu_occupancy(ctx) -> dict:
    d = authorize(ctx.ident, "get_icu_occupancy")
    f = _facility(ctx, "icu")
    total, occ = f.get("total_beds", 0), f.get("occupied", 0)
    free = max(total - occ, 0)
    if d.view == "summary":
        status = "available" if free > 3 else "limited" if free > 0 else "full"
        return {"icu_beds_status": status, "beds_available": free, "updated_at": clean(f.get("updated_at"))}
    ctx.audit("get_icu_occupancy")
    ward = _facility(ctx, "ward")
    return clean({"total_beds": total, "occupied": occ, "available": free,
                  "ventilators_total": f.get("ventilators_total"), "ventilators_in_use": f.get("ventilators_in_use"),
                  "general_ward_occupancy_percent": ward.get("occupancy_percent"), "updated_at": f.get("updated_at")})


def get_er_status(ctx) -> dict:
    d = authorize(ctx.ident, "get_er_status")
    f = _facility(ctx, "er")
    if d.view == "summary":
        return clean({"estimated_wait_minutes": f.get("wait_minutes"), "on_diversion": f.get("diversion", False),
                      "updated_at": f.get("updated_at")})
    ctx.audit("get_er_status")
    return clean({"wait_minutes": f.get("wait_minutes"), "triage_queue": f.get("triage_queue"),
                  "on_diversion": f.get("diversion", False), "updated_at": f.get("updated_at"),
                  "updated_by": f.get("updated_by")})


def get_blood_inventory(ctx, blood_group: Optional[str] = None) -> dict:
    d = authorize(ctx.ident, "get_blood_inventory")
    q = {}
    if blood_group:
        if blood_group.upper() not in BLOOD_GROUPS:
            raise bad(f"blood_group must be one of {', '.join(BLOOD_GROUPS)}.")
        q["blood_group"] = blood_group.upper()
    rows = list(ctx.t.blood_inventory.find(q).sort("blood_group", 1))
    if d.view == "summary":
        return {"blood_stock": [{"blood_group": r["blood_group"],
                                 "status": "available" if r["units"] > 10 else "low" if r["units"] > 0 else "out"}
                                for r in rows]}
    ctx.audit("get_blood_inventory")
    return {"blood_stock": clean([{k: r.get(k) for k in ("blood_group", "units", "updated_at")} for r in rows])}


def get_department_status(ctx, department_id: str) -> dict:
    d = authorize(ctx.ident, "get_department_status")
    dept = ctx.t.departments.find_one({"department_id": department_id})
    if not dept:
        raise not_found(f"Department '{department_id}'", hint="Call list_departments.")
    t = ctx.clock()
    day = datetime.combine(t.date(), time.min)
    waiting = ctx.t.appointments.count_documents(
        {"department_id": department_id, "status": "booked", "slot_start": {"$gte": t - timedelta(minutes=30),
                                                                           "$lt": day + timedelta(days=1)}})
    out = {"department_id": department_id, "name": dept["name"], "opd_queue_length": waiting,
           "estimated_wait_minutes": waiting * dept.get("avg_consult_minutes", 15)}
    if d.view == "summary":
        return out
    out["staff_on_duty"] = [
        {"staff_id": r["staff_id"], "name": r["name"], "role": r["role"]}
        for r in ctx.t.duty_roster.find({"department_id": department_id, "shift_start": {"$lte": t},
                                         "shift_end": {"$gt": t}})]
    ctx.audit("get_department_status", department_id)
    return out


def get_ot_schedule(ctx, date: str) -> dict:
    authorize(ctx.ident, "get_ot_schedule")
    day = parse_date(date)
    rows = list(ctx.t.ot_schedule.find({"date": day.isoformat()}).sort("start", 1))
    ctx.audit("get_ot_schedule", day.isoformat())
    return clean({"date": day.isoformat(), "bookings": rows})


def update_facility_status(ctx, facility: str, fields: dict) -> dict:
    """facility: icu | er | ward. For blood use update_blood_units."""
    authorize(ctx.ident, "update_facility_status")
    if facility not in FACILITY_FIELDS and facility != "blood":
        raise bad("facility must be icu, er, ward or blood.")
    if facility == "blood":
        return _update_blood(ctx, fields)
    allowed = FACILITY_FIELDS[facility]
    if not isinstance(fields, dict) or not fields or set(fields) - set(allowed):
        raise bad(f"Allowed fields for {facility}: {', '.join(sorted(allowed))}.")
    for k, v in fields.items():
        typ = allowed[k]
        if typ is bool:
            if not isinstance(v, bool):
                raise bad(f"{k} must be true or false.")
        elif isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise bad(f"{k} must be a whole number of 0 or more.")
    cur = _facility(ctx, facility)
    merged = {**cur, **fields}
    if facility == "icu":
        if merged.get("occupied", 0) > merged.get("total_beds", 0):
            raise bad("occupied cannot exceed total_beds.")
        if merged.get("ventilators_in_use", 0) > merged.get("ventilators_total", 0):
            raise bad("ventilators_in_use cannot exceed ventilators_total.")
    if facility == "ward" and fields.get("occupancy_percent", 0) > 100:
        raise bad("occupancy_percent cannot exceed 100.")
    before = {k: cur.get(k) for k in fields}
    ctx.t.facilities.update_one({"facility": facility},
                                {"$set": {**fields, "updated_at": ctx.clock(), "updated_by": ctx.ident.subject}},
                                upsert=True)
    ctx.audit("update_facility_status", facility, before=before, after=fields)
    return {"facility": facility, "updated": fields}


def _update_blood(ctx, fields: dict) -> dict:
    """fields = {"O+": 12, "A-": 3}: absolute unit counts per group."""
    if not isinstance(fields, dict) or not fields:
        raise bad('For blood, fields maps blood group to units, e.g. {"O+": 12}.')
    for g, units in fields.items():
        if g.upper() not in BLOOD_GROUPS:
            raise bad(f"Unknown blood group '{g}'.")
        if isinstance(units, bool) or not isinstance(units, int) or units < 0:
            raise HospitalError(Codes.INVALID_INPUT, "Blood units cannot be negative.")
    for g, units in fields.items():
        ctx.t.blood_inventory.update_one({"blood_group": g.upper()},
                                         {"$set": {"units": units, "updated_at": ctx.clock(),
                                                   "updated_by": ctx.ident.subject}}, upsert=True)
    ctx.audit("update_facility_status", "blood", after=fields)
    return {"facility": "blood", "updated": fields}
