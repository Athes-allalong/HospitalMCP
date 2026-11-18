"""Public directory: hospital info, doctors, departments, availability. Also the availability engine."""
import re
from datetime import date, datetime, time, timedelta
from typing import List, Optional

from ..authz import authorize
from ..errors import Codes, HospitalError
from .common import DAYS, bad, clean, inr, not_found, page_args, parse_date, to_minor

GRID_MIN = 15
TYPE_MINUTES = {"new": 30, "followup": 15, "teleconsult": 15}
MAX_RANGE_DAYS = 14


def _rx(s: str) -> dict:
    return {"$regex": re.escape(s.strip()), "$options": "i"}


def get_active_doctor(ctx, staff_id: str) -> dict:
    d = ctx.t.doctors.find_one({"staff_id": staff_id})
    if not d:
        raise not_found(f"Doctor '{staff_id}'", hint="Use search_doctors to find the right staff_id.")
    if d.get("active") is False:
        raise HospitalError(Codes.DOCTOR_INACTIVE, f"{d['name']} is no longer taking appointments.",
                            hint="Search for another doctor in the same department.")
    return d


# --------------------------------------------------------------------------
# Availability engine: weekly schedule - holidays - leave - booked appointments - the past
# --------------------------------------------------------------------------
def open_slots(ctx, doc: dict, d0: date, d1: date, appt_type: str, exclude_appt: Optional[str] = None) -> List[datetime]:
    dur = timedelta(minutes=TYPE_MINUTES[appt_type])
    s0, s1 = d0.isoformat(), d1.isoformat()
    holidays = {h["date"] for h in ctx.t.holidays.find({"date": {"$gte": s0, "$lte": s1}})}
    leaves = list(ctx.t.leaves.find({"doctor_id": doc["staff_id"], "start_date": {"$lte": s1}, "end_date": {"$gte": s0}}))
    q = {"doctor_id": doc["staff_id"], "status": "booked",
         "slot_start": {"$lt": datetime.combine(d1 + timedelta(days=1), time.min)},
         "slot_end": {"$gt": datetime.combine(d0, time.min)}}
    if exclude_appt:
        q["appointment_id"] = {"$ne": exclude_appt}
    booked = [(a["slot_start"], a["slot_end"]) for a in ctx.t.appointments.find(q)]
    cutoff, out, d = ctx.clock(), [], d0
    while d <= d1:
        iso = d.isoformat()
        if iso not in holidays and not any(lv["start_date"] <= iso <= lv["end_date"] for lv in leaves):
            for start_s, end_s in doc.get("schedule", {}).get(DAYS[d.weekday()], []):
                t = datetime.combine(d, time.fromisoformat(start_s))
                end = datetime.combine(d, time.fromisoformat(end_s))
                while t + dur <= end:
                    if t > cutoff and not any(t < be and t + dur > bs for bs, be in booked):
                        out.append(t)
                    t += timedelta(minutes=GRID_MIN)
        d += timedelta(days=1)
    return out


def explain_unavailable(ctx, doc: dict, start: datetime, appt_type: str) -> HospitalError:
    """Pick the most specific reason a slot is not bookable, plus nearby alternatives (plan s.8)."""
    day = start.date().isoformat()
    if start <= ctx.clock():
        return HospitalError(Codes.INVALID_INPUT, "That time is in the past.", hint="Choose a future slot.")
    if ctx.t.leaves.find_one({"doctor_id": doc["staff_id"], "start_date": {"$lte": day}, "end_date": {"$gte": day}}):
        err_code, msg = Codes.DOCTOR_ON_LEAVE, f"{doc['name']} is on leave on {day}."
    elif ctx.t.holidays.find_one({"date": day}):
        err_code, msg = Codes.SLOT_UNAVAILABLE, f"The hospital is closed on {day} (holiday)."
    elif not doc.get("schedule", {}).get(DAYS[start.weekday()]):
        err_code, msg = Codes.SLOT_UNAVAILABLE, f"{doc['name']} has no OPD hours on {DAYS[start.weekday()].title()}."
    else:
        err_code, msg = Codes.SLOT_UNAVAILABLE, "That slot is not available (booked, off the 15-minute grid, or outside OPD hours)."
    alts = open_slots(ctx, doc, start.date(), start.date() + timedelta(days=3), appt_type)
    alts = sorted(alts, key=lambda s: abs((s - start).total_seconds()))[:5]
    return HospitalError(err_code, msg, hint="Offer one of the alternative slots, or call check_availability.",
                         data={"alternatives": sorted(a.isoformat() for a in alts)})


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------
def get_hospital_info(ctx) -> dict:
    authorize(ctx.ident, "get_hospital_info")
    h = ctx.raw.hospitals.find_one({"hospital_id": ctx.ident.hospital_id}) or {}
    keep = ("name", "tagline", "address", "phone", "email", "visiting_hours", "new_patient_info", "emergency",
            "website")
    return clean({k: h[k] for k in keep if k in h})


def search_doctors(ctx, specialty: Optional[str] = None, department: Optional[str] = None,
                   name: Optional[str] = None, max_fee_inr: Optional[float] = None,
                   limit: int = 20, page: int = 1) -> dict:
    authorize(ctx.ident, "search_doctors")
    limit, skip = page_args(limit, page)
    q: dict = {"active": {"$ne": False}}
    if specialty:
        q["$or"] = [{"specialty": _rx(specialty)}, {"focus_areas": _rx(specialty)}]
    if name:
        q["name"] = _rx(name)
    if department:
        dept_ids = [d["department_id"] for d in ctx.t.departments.find(
            {"$or": [{"department_id": department}, {"name": _rx(department)}]})]
        q["department_id"] = {"$in": dept_ids}
    if max_fee_inr is not None:
        q["consultation_fee_minor"] = {"$lte": to_minor(max_fee_inr)}
    total = ctx.t.doctors.count_documents(q)
    rows = ctx.t.doctors.find(q).sort([("department_name", 1), ("name", 1)]).skip(skip).limit(limit)
    return {
        "total": total, "page": page,
        "doctors": [{"staff_id": d["staff_id"], "name": d["name"], "specialty": d.get("specialty"),
                     "department": d.get("department_name"), "experience_years": d.get("experience_years"),
                     "consultation_fee_inr": inr(d.get("consultation_fee_minor"))} for d in rows],
    }


def get_doctor_profile(ctx, staff_id: str) -> dict:
    authorize(ctx.ident, "get_doctor_profile")
    d = get_active_doctor(ctx, staff_id)
    keep = ("staff_id", "name", "specialty", "focus_areas", "department_id", "department_name", "qualifications",
            "experience_years", "languages", "room", "schedule", "visit_types")
    out = {k: d[k] for k in keep if k in d}
    out["consultation_fee_inr"] = inr(d.get("consultation_fee_minor"))
    # free text is data, never instructions (plan s.10)
    out["bio"] = {"untrusted_text": d.get("bio", "")}
    return clean(out)


def check_availability(ctx, doctor_id: str, start_date: str, end_date: str, type: str = "new") -> dict:
    authorize(ctx.ident, "check_availability")
    if type not in TYPE_MINUTES:
        raise bad(f"type must be one of {', '.join(TYPE_MINUTES)}.")
    doc = get_active_doctor(ctx, doctor_id)
    d0, d1 = parse_date(start_date, "start_date"), parse_date(end_date, "end_date")
    if d1 < d0 or (d1 - d0).days > MAX_RANGE_DAYS:
        raise bad(f"The date range must be ordered and span at most {MAX_RANGE_DAYS} days.")
    slots = open_slots(ctx, doc, d0, d1, type)
    return {"doctor_id": doctor_id, "doctor_name": doc["name"], "type": type,
            "duration_minutes": TYPE_MINUTES[type], "slot_count": len(slots),
            "slots": [s.isoformat() for s in slots[:60]],
            "truncated": len(slots) > 60}


def list_departments(ctx) -> list:
    authorize(ctx.ident, "list_departments")
    out = []
    for d in ctx.t.departments.find().sort("name", 1):
        out.append(clean({k: d.get(k) for k in ("department_id", "name", "kind", "location", "phone_ext",
                                                   "head_staff_id", "head_name", "services", "hours")}))
    return out
