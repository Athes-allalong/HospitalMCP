"""Hospital MCP server (official `mcp` Python SDK / FastMCP + MongoDB).

Transports:
  stdio (default)        -> dev only; identity comes from DEV_ROLE / DEV_SUBJECT
                            when ALLOW_DEV_IDENTITY=1
  streamable-http        -> bearer token verified against db.tokens; the token
                            carries the role and subject (staff_id or MRN)

Run:   python server.py                      (stdio)
       MCP_TRANSPORT=streamable-http python server.py

All times are naive hospital-local time (HOSPITAL_TZ, default Asia/Kolkata).
Money is stored in integer paise (`*_minor`) and shown in INR.
"""
import hashlib
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo

from bson import ObjectId
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl, BaseModel, Field
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

log = logging.getLogger("hospital-mcp")

# --------------------------------------------------------------------------
# Config / DB
# --------------------------------------------------------------------------
TZ = ZoneInfo(os.getenv("HOSPITAL_TZ", "Asia/Kolkata"))
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "hospital")]


def now() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)


def ensure_indexes() -> None:
    db.patients.create_index("mrn", unique=True)
    db.doctors.create_index("staff_id", unique=True)
    db.appointments.create_index("appointment_id", unique=True)
    # DB-level double-booking guard: one *booked* appointment per doctor+start.
    db.appointments.create_index(
        [("doctor_id", ASCENDING), ("slot_start", ASCENDING)],
        unique=True,
        partialFilterExpression={"status": "booked"},
    )
    db.appointments.create_index([("mrn", ASCENDING), ("slot_start", DESCENDING)])
    db.invoices.create_index("invoice_id", unique=True)
    db.payments.create_index("gateway_ref", unique=True)
    db.leaves.create_index([("doctor_id", ASCENDING), ("start_date", ASCENDING)])
    db.holidays.create_index("date", unique=True)
    db.audit_log.create_index([("at", DESCENDING)])


# --------------------------------------------------------------------------
# Identity (from the token, never from tool arguments)
# --------------------------------------------------------------------------
ROLES = {"patient", "doctor", "nurse", "admin", "receptionist", "billing"}
CLINICAL = {"doctor", "nurse", "admin"}           # may see unmasked clinical data
FRONT_DESK = {"receptionist", "nurse", "admin"}   # may book on behalf of patients
FINANCE = {"billing", "admin"}


@dataclass(frozen=True)
class Identity:
    role: str
    subject: str  # staff_id for staff, MRN for patients


class DbTokenVerifier(TokenVerifier):
    """Looks up sha256(token) in db.tokens: {_id, role, subject, expires_at}."""

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        doc = db.tokens.find_one({"_id": hashlib.sha256(token.encode()).hexdigest()})
        if not doc or doc.get("expires_at", 0) < now().timestamp():
            return None
        return AccessToken(
            token=token,
            client_id=doc["subject"],
            scopes=[f"role:{doc['role']}"],
            expires_at=int(doc["expires_at"]),
        )


def identity() -> Identity:
    tok = None
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token

        tok = get_access_token()
    except Exception:  # not running under an authenticated transport
        pass
    if tok:
        role = next((s[5:] for s in tok.scopes if s.startswith("role:")), None)
        if role not in ROLES:
            raise ToolError("Token carries no valid role.")
        return Identity(role, tok.client_id)
    if os.getenv("ALLOW_DEV_IDENTITY") == "1":
        role, subject = os.getenv("DEV_ROLE"), os.getenv("DEV_SUBJECT")
        if role in ROLES and subject:
            return Identity(role, subject)
    raise ToolError("Unauthenticated: a valid bearer token is required.")


def require_roles(ident: Identity, *roles: str) -> None:
    if ident.role not in set(roles):
        raise ToolError(f"Role '{ident.role}' is not permitted to use this tool.")


def require_patient_access(ident: Identity, mrn: str, staff_roles: set) -> None:
    """Patients: own MRN only. Staff: only if their role is in staff_roles."""
    if ident.role == "patient":
        if ident.subject != mrn:
            raise ToolError("Patients can only access their own records.")
    elif ident.role not in staff_roles:
        raise ToolError(f"Role '{ident.role}' cannot access this patient data.")


def audit(ident: Identity, tool: str, target: Optional[str] = None) -> None:
    """Audit trail: who/what/when/target only; never PHI payloads."""
    db.audit_log.insert_one(
        {"at": now(), "role": ident.role, "subject": ident.subject, "tool": tool, "target": target}
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def clean(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items() if k != "_id"}
    if isinstance(obj, list):
        return [clean(v) for v in obj]
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, ObjectId):
        return str(obj)
    return obj


def inr(minor: int) -> float:
    return float(Decimal(minor) / 100)


def to_minor(rupees: float) -> int:
    return int((Decimal(str(rupees)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_dt(s: str, field: str = "datetime") -> datetime:
    try:
        return datetime.fromisoformat(s).replace(tzinfo=None)
    except ValueError:
        raise ToolError(f"{field} must be ISO-8601, e.g. 2026-11-16T10:30:00")


def parse_date(s: str, field: str = "date") -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise ToolError(f"{field} must be YYYY-MM-DD")


def mask_phone(p: Optional[str]) -> Optional[str]:
    return None if not p else "*" * max(len(p) - 4, 0) + p[-4:]


def mask_email(e: Optional[str]) -> Optional[str]:
    if not e or "@" not in e:
        return None
    user, domain = e.split("@", 1)
    return f"{user[:1]}***@{domain}"


def idempotent(ident: Identity, tool: str, request_id: str, fn):
    """Run fn() at most once per (caller, tool, request_id); replays return the stored result."""
    if not request_id or len(request_id) > 128:
        raise ToolError("request_id is required (<=128 chars).")
    key = f"{ident.subject}:{tool}:{request_id}"
    try:
        db.idempotency.insert_one({"_id": key, "state": "pending", "at": now()})
    except DuplicateKeyError:
        prior = db.idempotency.find_one({"_id": key})
        if prior.get("state") == "done":
            return {**prior["result"], "replayed": True}
        raise ToolError("A request with this request_id is still in progress.")
    try:
        result = clean(fn())
    except Exception:
        db.idempotency.delete_one({"_id": key})  # let the caller retry
        raise
    db.idempotency.update_one({"_id": key}, {"$set": {"state": "done", "result": result}})
    return result


# --------------------------------------------------------------------------
# Availability engine
# --------------------------------------------------------------------------
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
GRID_MIN = 15
TYPE_MINUTES = {"new": 30, "followup": 15, "teleconsult": 15}
ApptType = Literal["new", "followup", "teleconsult"]


class DateRange(BaseModel):
    start: str = Field(description="YYYY-MM-DD (inclusive)")
    end: str = Field(description="YYYY-MM-DD (inclusive, max 14 days after start)")


def open_slots(doc: dict, d0: date, d1: date, appt_type: str, exclude_appt: Optional[str] = None) -> list:
    """Weekly schedule minus holidays, leave and booked appointments, minus the past."""
    dur = timedelta(minutes=TYPE_MINUTES[appt_type])
    s0, s1 = d0.isoformat(), d1.isoformat()
    holidays = {h["date"] for h in db.holidays.find({"date": {"$gte": s0, "$lte": s1}})}
    leaves = list(
        db.leaves.find({"doctor_id": doc["staff_id"], "start_date": {"$lte": s1}, "end_date": {"$gte": s0}})
    )
    q = {
        "doctor_id": doc["staff_id"],
        "status": "booked",
        "slot_start": {"$lt": datetime.combine(d1 + timedelta(days=1), time.min)},
        "slot_end": {"$gt": datetime.combine(d0, time.min)},
    }
    if exclude_appt:
        q["appointment_id"] = {"$ne": exclude_appt}
    booked = [(a["slot_start"], a["slot_end"]) for a in db.appointments.find(q)]
    cutoff, out, d = now(), [], d0
    while d <= d1:
        iso = d.isoformat()
        if iso not in holidays and not any(l["start_date"] <= iso <= l["end_date"] for l in leaves):
            for start_s, end_s in doc.get("schedule", {}).get(DAYS[d.weekday()], []):
                t = datetime.combine(d, time.fromisoformat(start_s))
                end = datetime.combine(d, time.fromisoformat(end_s))
                while t + dur <= end:
                    if t > cutoff and not any(t < be and t + dur > bs for bs, be in booked):
                        out.append(t)
                    t += timedelta(minutes=GRID_MIN)
        d += timedelta(days=1)
    return out


def get_doctor(staff_id: str) -> dict:
    doc = db.doctors.find_one({"staff_id": staff_id, "active": {"$ne": False}})
    if not doc:
        raise ToolError(f"Doctor '{staff_id}' not found.")
    return doc


def get_appt(appointment_id: str) -> dict:
    a = db.appointments.find_one({"appointment_id": appointment_id})
    if not a:
        raise ToolError(f"Appointment '{appointment_id}' not found.")
    return a


def appt_access(ident: Identity, a: dict, staff_roles: set) -> None:
    if ident.role == "doctor":
        if a["doctor_id"] != ident.subject:
            raise ToolError("Doctors can only access their own appointments.")
    else:
        require_patient_access(ident, a["mrn"], staff_roles)


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------
_kwargs: dict = {}
if os.getenv("MCP_TRANSPORT", "stdio") != "stdio":
    _kwargs = dict(
        token_verifier=DbTokenVerifier(),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(os.getenv("AUTH_ISSUER_URL", "http://localhost:9000")),
            resource_server_url=AnyHttpUrl(os.getenv("RESOURCE_URL", "http://localhost:8000")),
        ),
    )
mcp = FastMCP("hospital", **_kwargs)

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)

# ========================== 7.1 READ TOOLS ================================


@mcp.tool(annotations=READ)
def search_doctors(
    specialty: Optional[str] = None,
    department: Optional[str] = None,
    name: Optional[str] = None,
    max_fee: Optional[float] = None,
) -> list:
    """Search active doctors. department matches department id or name; max_fee is INR."""
    ident = identity()
    q: dict = {"active": {"$ne": False}}
    rx = lambda s: {"$regex": re.escape(s), "$options": "i"}
    if specialty:
        q["specialty"] = rx(specialty)
    if name:
        q["name"] = rx(name)
    if department:
        q["$or"] = [{"department_id": department}, {"department_name": rx(department)}]
    if max_fee is not None:
        q["consultation_fee_minor"] = {"$lte": to_minor(max_fee)}
    audit(ident, "search_doctors")
    return [
        {
            "staff_id": d["staff_id"],
            "name": d["name"],
            "specialty": d.get("specialty"),
            "department": d.get("department_name"),
            "consultation_fee_inr": inr(d.get("consultation_fee_minor", 0)),
        }
        for d in db.doctors.find(q).limit(25)
    ]


@mcp.tool(annotations=READ)
def get_doctor_profile(staff_id: str) -> dict:
    """Public profile of a doctor (no personal contact details)."""
    ident = identity()
    d = get_doctor(staff_id)
    audit(ident, "get_doctor_profile", staff_id)
    keep = ("staff_id", "name", "specialty", "department_id", "department_name", "qualifications",
            "experience_years", "languages", "bio", "schedule")
    out = {k: d[k] for k in keep if k in d}
    out["consultation_fee_inr"] = inr(d.get("consultation_fee_minor", 0))
    return clean(out)


@mcp.tool(annotations=READ)
def check_availability(doctor_id: str, date_range: DateRange, type: ApptType = "new") -> dict:
    """Open slots for a doctor after subtracting bookings, leave and holidays."""
    ident = identity()
    doc = get_doctor(doctor_id)
    d0, d1 = parse_date(date_range.start, "start"), parse_date(date_range.end, "end")
    if d1 < d0 or (d1 - d0).days > 14:
        raise ToolError("date_range must be ordered and span at most 14 days.")
    slots = open_slots(doc, d0, d1, type)
    audit(ident, "check_availability", doctor_id)
    return {
        "doctor_id": doctor_id,
        "type": type,
        "duration_minutes": TYPE_MINUTES[type],
        "slots": [s.isoformat() for s in slots],
    }


@mcp.tool(annotations=READ)
def list_departments() -> list:
    """All departments."""
    audit(identity(), "list_departments")
    return [
        {"department_id": d["department_id"], "name": d["name"], "location": d.get("location")}
        for d in db.departments.find()
    ]


@mcp.tool(annotations=READ)
def get_department_status(department_id: str) -> dict:
    """OPD queue length, estimated wait, and staff currently on duty."""
    ident = identity()
    dept = db.departments.find_one({"department_id": department_id})
    if not dept:
        raise ToolError("Department not found.")
    t = now()
    day_start = datetime.combine(t.date(), time.min)
    queue = db.appointments.count_documents(
        {"department_id": department_id, "status": "checked_in",
         "slot_start": {"$gte": day_start, "$lt": day_start + timedelta(days=1)}}
    )
    on_duty = [
        {"staff_id": r["staff_id"], "name": r["name"], "role": r["role"]}
        for r in db.duty_roster.find(
            {"department_id": department_id, "shift_start": {"$lte": t}, "shift_end": {"$gt": t}}
        )
    ]
    audit(ident, "get_department_status", department_id)
    return {
        "department_id": department_id,
        "name": dept["name"],
        "opd_queue_length": queue,
        "estimated_wait_minutes": queue * dept.get("avg_consult_minutes", 15),
        "staff_on_duty": on_duty,
    }


@mcp.tool(annotations=READ)
def get_patient_summary(mrn: str) -> dict:
    """Patient demographics. Clinical roles and the patient see full detail; others get masked data."""
    ident = identity()
    require_patient_access(ident, mrn, CLINICAL | {"receptionist", "billing"})
    p = db.patients.find_one({"mrn": mrn})
    if not p:
        raise ToolError("Patient not found.")
    audit(ident, "get_patient_summary", mrn)
    full = ident.role in CLINICAL or ident.role == "patient"
    return clean({
        "mrn": mrn,
        "name": p["name"],
        "sex": p.get("sex"),
        "dob": p.get("dob") if full else (p.get("dob") or "")[:4],  # year only when masked
        "blood_group": p.get("blood_group") if full else None,
        "allergies": p.get("allergies", []) if full else None,
        "phone": p.get("phone") if full else mask_phone(p.get("phone")),
        "email": p.get("email") if full else mask_email(p.get("email")),
        "insurance": (
            {"provider_id": p["insurance"]["provider_id"],
             "policy_no": p["insurance"]["policy_no"] if full else mask_phone(p["insurance"]["policy_no"])}
            if p.get("insurance") else None
        ),
        "masked": not full,
    })


@mcp.tool(annotations=READ)
def get_patient_history(mrn: str) -> dict:
    """Past visit summaries, diagnoses and medications. Caller identity comes from the token."""
    ident = identity()
    require_patient_access(ident, mrn, CLINICAL)
    if ident.role == "doctor" and not db.appointments.find_one({"mrn": mrn, "doctor_id": ident.subject}):
        raise ToolError("No care relationship with this patient.")
    visits = list(db.visits.find({"mrn": mrn}).sort("date", DESCENDING).limit(50))
    audit(ident, "get_patient_history", mrn)
    return clean({"mrn": mrn, "visits": visits})


@mcp.tool(annotations=READ)
def get_appointment_history(mrn: str) -> list:
    """All appointments for a patient, newest first."""
    ident = identity()
    require_patient_access(ident, mrn, CLINICAL | {"receptionist"})
    q: dict = {"mrn": mrn}
    if ident.role == "doctor":
        q["doctor_id"] = ident.subject
    audit(ident, "get_appointment_history", mrn)
    return clean(list(db.appointments.find(q).sort("slot_start", DESCENDING).limit(100)))


@mcp.tool(annotations=READ)
def get_appointment(appointment_id: str) -> dict:
    ident = identity()
    a = get_appt(appointment_id)
    appt_access(ident, a, CLINICAL | {"receptionist"})
    audit(ident, "get_appointment", appointment_id)
    return clean(a)


@mcp.tool(annotations=READ)
def list_prescriptions(mrn: str) -> list:
    ident = identity()
    require_patient_access(ident, mrn, CLINICAL)
    audit(ident, "list_prescriptions", mrn)
    return clean(list(db.prescriptions.find({"mrn": mrn}).sort("issued_at", DESCENDING).limit(50)))


@mcp.tool(annotations=READ)
def get_admission_status(mrn: str) -> dict:
    ident = identity()
    require_patient_access(ident, mrn, CLINICAL | {"receptionist"})
    adm = db.admissions.find_one({"mrn": mrn, "status": "admitted"})
    audit(ident, "get_admission_status", mrn)
    if not adm:
        return {"mrn": mrn, "admitted": False}
    return clean({"mrn": mrn, "admitted": True, "ward": adm.get("ward"), "bed": adm.get("bed"),
                  "admitted_at": adm.get("admitted_at"), "attending_doctor_id": adm.get("doctor_id")})


@mcp.tool(annotations=READ)
def get_lab_results(mrn: str, test_id: Optional[str] = None) -> list:
    """Lab results for a patient; optionally restricted to one test_id."""
    ident = identity()
    require_patient_access(ident, mrn, CLINICAL)
    q: dict = {"mrn": mrn}
    if test_id:
        q["test_id"] = test_id
    audit(ident, "get_lab_results", mrn)
    return clean(list(db.lab_results.find(q).sort("reported_at", DESCENDING).limit(100)))


@mcp.tool(annotations=READ)
def get_icu_occupancy() -> dict:
    ident = identity()
    require_roles(ident, "doctor", "nurse", "admin", "receptionist")
    f = db.facilities.find_one({"_id": "icu"}) or {}
    audit(ident, "get_icu_occupancy")
    total, occ = f.get("total_beds", 0), f.get("occupied", 0)
    return clean({"total_beds": total, "occupied": occ, "available": total - occ,
                  "units": f.get("units", []), "updated_at": f.get("updated_at")})


@mcp.tool(annotations=READ)
def get_er_status() -> dict:
    ident = identity()
    f = db.facilities.find_one({"_id": "er"}) or {}
    audit(ident, "get_er_status")
    return clean({"wait_minutes": f.get("wait_minutes"), "triage_queue": f.get("triage_queue"),
                  "on_diversion": f.get("diversion", False), "updated_at": f.get("updated_at")})


@mcp.tool(annotations=READ)
def get_ot_schedule(date: str) -> list:
    """Operating theatre bookings for a date (YYYY-MM-DD)."""
    ident = identity()
    require_roles(ident, "doctor", "nurse", "admin")
    d = parse_date(date)
    audit(ident, "get_ot_schedule", d.isoformat())
    return clean(list(db.ot_schedule.find({"date": d.isoformat()}).sort("start", ASCENDING)))


@mcp.tool(annotations=READ)
def get_blood_inventory(blood_group: Optional[str] = None) -> list:
    ident = identity()
    require_roles(ident, "doctor", "nurse", "admin")
    q = {"blood_group": blood_group.upper()} if blood_group else {}
    audit(ident, "get_blood_inventory")
    return clean(list(db.blood_inventory.find(q)))


@mcp.tool(annotations=READ)
def get_invoice(invoice_id: str) -> dict:
    ident = identity()
    inv = db.invoices.find_one({"invoice_id": invoice_id})
    if not inv:
        raise ToolError("Invoice not found.")
    require_patient_access(ident, inv["mrn"], FINANCE | {"receptionist"})
    audit(ident, "get_invoice", invoice_id)
    inv["total_inr"], inv["paid_inr"] = inr(inv["total_minor"]), inr(inv["paid_minor"])
    inv["balance_inr"] = inr(inv["total_minor"] - inv["paid_minor"])
    return clean(inv)


@mcp.tool(annotations=READ)
def get_claim_status(invoice_id: str) -> dict:
    ident = identity()
    inv = db.invoices.find_one({"invoice_id": invoice_id})
    if not inv:
        raise ToolError("Invoice not found.")
    require_patient_access(ident, inv["mrn"], FINANCE)
    claim = db.claims.find_one({"invoice_id": invoice_id})
    audit(ident, "get_claim_status", invoice_id)
    return clean(claim) if claim else {"invoice_id": invoice_id, "status": "no_claim"}


@mcp.tool(annotations=READ)
def check_insurance_coverage(provider_id: str, policy_no: str) -> dict:
    """Whether the insurer is accepted, the policy is active, and what is covered."""
    ident = identity()
    ins = db.insurers.find_one({"provider_id": provider_id})
    if not ins or not ins.get("accepted", False):
        return {"provider_id": provider_id, "accepted": False}
    pol = db.policies.find_one({"provider_id": provider_id, "policy_no": policy_no})
    audit(ident, "check_insurance_coverage", provider_id)
    if not pol:
        return {"provider_id": provider_id, "accepted": True, "policy_found": False}
    active = pol.get("status") == "active" and pol.get("valid_till", "0000") >= now().date().isoformat()
    return clean({
        "provider_id": provider_id, "provider_name": ins["name"], "accepted": True,
        "policy_found": True, "policy_active": active, "valid_till": pol.get("valid_till"),
        "covered_categories": ins.get("covered", []), "exclusions": ins.get("exclusions", []),
        "copay_percent": pol.get("copay_percent", 0),
    })


# ========================== 7.2 WRITE TOOLS ===============================


def _book_authz(ident: Identity, mrn: str) -> None:
    if ident.role == "patient":
        if ident.subject != mrn:
            raise ToolError("Patients can only book for themselves.")
    elif ident.role not in FRONT_DESK:
        raise ToolError(f"Role '{ident.role}' cannot book appointments.")


@mcp.tool(annotations=WRITE)
def book_appointment(mrn: str, doctor_id: str, slot_start: str, type: ApptType, request_id: str) -> dict:
    """Book a slot returned by check_availability. Idempotent on request_id."""
    ident = identity()
    _book_authz(ident, mrn)

    def run():
        if not db.patients.find_one({"mrn": mrn}):
            raise ToolError("Patient not found.")
        doc = get_doctor(doctor_id)
        start = parse_dt(slot_start, "slot_start")
        if start not in open_slots(doc, start.date(), start.date(), type):
            raise ToolError("Slot is not available. Call check_availability for current open slots.")
        a = {
            "appointment_id": f"APT-{uuid.uuid4().hex[:10].upper()}",
            "mrn": mrn, "doctor_id": doctor_id, "department_id": doc.get("department_id"),
            "type": type, "slot_start": start,
            "slot_end": start + timedelta(minutes=TYPE_MINUTES[type]),
            "status": "booked", "booked_by": ident.subject, "created_at": now(),
        }
        try:
            db.appointments.insert_one(dict(a))
        except DuplicateKeyError:  # lost a race with another booking
            raise ToolError("Slot was just taken by another booking.")
        audit(ident, "book_appointment", a["appointment_id"])
        return a

    return idempotent(ident, "book_appointment", request_id, run)


@mcp.tool(annotations=WRITE)
def reschedule_appointment(appointment_id: str, new_slot_start: str, request_id: str) -> dict:
    ident = identity()

    def run():
        a = get_appt(appointment_id)
        appt_access(ident, a, FRONT_DESK)
        if ident.role == "doctor":
            raise ToolError("Doctors cannot reschedule; ask the front desk.")
        if a["status"] != "booked":
            raise ToolError(f"Only booked appointments can be rescheduled (status: {a['status']}).")
        start = parse_dt(new_slot_start, "new_slot_start")
        doc = get_doctor(a["doctor_id"])
        if start not in open_slots(doc, start.date(), start.date(), a["type"], exclude_appt=appointment_id):
            raise ToolError("New slot is not available.")
        try:
            res = db.appointments.find_one_and_update(
                {"appointment_id": appointment_id, "status": "booked"},
                {"$set": {"slot_start": start, "slot_end": start + timedelta(minutes=TYPE_MINUTES[a["type"]]),
                          "rescheduled_at": now()},
                 "$push": {"history": {"from": a["slot_start"], "by": ident.subject, "at": now()}}},
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            raise ToolError("New slot was just taken by another booking.")
        if not res:
            raise ToolError("Appointment changed concurrently; retry.")
        audit(ident, "reschedule_appointment", appointment_id)
        return res

    return idempotent(ident, "reschedule_appointment", request_id, run)


@mcp.tool(annotations=DESTRUCTIVE)
def cancel_appointment(appointment_id: str, reason: str, request_id: str) -> dict:
    ident = identity()

    def run():
        a = get_appt(appointment_id)
        appt_access(ident, a, FRONT_DESK)
        if a["status"] != "booked":
            raise ToolError(f"Only booked appointments can be cancelled (status: {a['status']}).")
        if a["slot_start"] <= now():
            raise ToolError("Past appointments cannot be cancelled.")
        res = db.appointments.find_one_and_update(
            {"appointment_id": appointment_id, "status": "booked"},
            {"$set": {"status": "cancelled", "cancel_reason": reason[:500],
                      "cancelled_by": ident.subject, "cancelled_at": now()}},
            return_document=ReturnDocument.AFTER,
        )
        if not res:
            raise ToolError("Appointment changed concurrently; retry.")
        audit(ident, "cancel_appointment", appointment_id)
        return res

    return idempotent(ident, "cancel_appointment", request_id, run)


@mcp.tool(annotations=WRITE)
def add_clinical_note(appointment_id: str, note: str) -> dict:
    """Doctor only, and only on their own appointments. Notes are append-only."""
    ident = identity()
    require_roles(ident, "doctor")
    a = get_appt(appointment_id)
    if a["doctor_id"] != ident.subject:
        raise ToolError("You can only annotate your own appointments.")
    if a["status"] == "cancelled":
        raise ToolError("Cannot add notes to a cancelled appointment.")
    if not note.strip() or len(note) > 10_000:
        raise ToolError("Note must be 1-10000 characters.")
    n = {"note_id": f"NOTE-{uuid.uuid4().hex[:10].upper()}", "appointment_id": appointment_id,
         "mrn": a["mrn"], "doctor_id": ident.subject, "note": note.strip(), "created_at": now()}
    db.clinical_notes.insert_one(dict(n))
    audit(ident, "add_clinical_note", appointment_id)
    return clean({"note_id": n["note_id"], "appointment_id": appointment_id, "created_at": n["created_at"]})


CONTACT_FIELDS = {"phone": r"^\+?[0-9]{10,13}$", "email": r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
                  "address": r"^.{5,300}$", "emergency_contact": None}


class ContactFields(BaseModel):
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    emergency_contact: Optional[dict] = Field(default=None, description="{name, phone, relation}")


@mcp.tool(annotations=WRITE)
def update_patient_contact(mrn: str, fields: ContactFields) -> dict:
    """Update phone/email/address/emergency_contact. Patient (self), receptionist or admin."""
    ident = identity()
    require_patient_access(ident, mrn, {"receptionist", "admin"})
    updates = fields.model_dump(exclude_none=True)
    if not updates:
        raise ToolError("No fields supplied.")
    for k, v in updates.items():
        pattern = CONTACT_FIELDS[k]
        if pattern and not re.match(pattern, v):
            raise ToolError(f"Invalid {k}.")
        if k == "emergency_contact" and not {"name", "phone"} <= set(v):
            raise ToolError("emergency_contact needs name and phone.")
    res = db.patients.update_one({"mrn": mrn}, {"$set": {**updates, "contact_updated_at": now()}})
    if not res.matched_count:
        raise ToolError("Patient not found.")
    audit(ident, "update_patient_contact", mrn)
    return {"mrn": mrn, "updated_fields": sorted(updates)}


FACILITY_FIELDS = {
    "icu": {"total_beds", "occupied", "units"},
    "er": {"wait_minutes", "triage_queue", "diversion"},
    "blood": {"units"},
    "department": {"avg_consult_minutes"},
}


@mcp.tool(annotations=WRITE)
def update_facility_status(
    facility: Literal["icu", "er", "blood", "department"],
    fields: dict,
    key: Optional[str] = None,
) -> dict:
    """Nurse/admin. key = blood_group for 'blood', department_id for 'department'. Allowed fields per facility are fixed."""
    ident = identity()
    require_roles(ident, "nurse", "admin")
    bad = set(fields) - FACILITY_FIELDS[facility]
    if bad or not fields:
        raise ToolError(f"Allowed fields for {facility}: {sorted(FACILITY_FIELDS[facility])}")
    if any(isinstance(v, (int, float)) and not isinstance(v, bool) and v < 0 for v in fields.values()):
        raise ToolError("Numeric values cannot be negative.")
    upd = {**fields, "updated_at": now(), "updated_by": ident.subject}
    if facility in ("icu", "er"):
        cur = db.facilities.find_one({"_id": facility}) or {}
        if facility == "icu":
            total, occ = fields.get("total_beds", cur.get("total_beds", 0)), fields.get("occupied", cur.get("occupied", 0))
            if occ > total:
                raise ToolError("occupied cannot exceed total_beds.")
        db.facilities.update_one({"_id": facility}, {"$set": upd}, upsert=True)
    elif facility == "blood":
        if not key:
            raise ToolError("key (blood_group) is required.")
        db.blood_inventory.update_one({"blood_group": key.upper()}, {"$set": upd}, upsert=True)
    else:
        if not key or not db.departments.update_one({"department_id": key}, {"$set": upd}).matched_count:
            raise ToolError("Department not found (pass department_id as key).")
    audit(ident, "update_facility_status", f"{facility}:{key or ''}")
    return {"facility": facility, "key": key, "updated": sorted(fields)}


class InvoiceItem(BaseModel):
    description: str = Field(min_length=2, max_length=200)
    amount_inr: float = Field(gt=0, description="Unit price in INR")
    quantity: int = Field(default=1, ge=1, le=1000)


@mcp.tool(annotations=WRITE)
def create_invoice(mrn: str, items: list[InvoiceItem], request_id: str,
                   appointment_id: Optional[str] = None) -> dict:
    """Billing/admin/receptionist. Idempotent on request_id."""
    ident = identity()
    require_roles(ident, "billing", "admin", "receptionist")

    def run():
        p = db.patients.find_one({"mrn": mrn})
        if not p:
            raise ToolError("Patient not found.")
        if appointment_id and get_appt(appointment_id)["mrn"] != mrn:
            raise ToolError("appointment_id does not belong to this patient.")
        lines = [{"description": i.description, "quantity": i.quantity, "unit_minor": to_minor(i.amount_inr),
                  "line_minor": to_minor(i.amount_inr) * i.quantity} for i in items]
        inv = {"invoice_id": f"INV-{now():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}", "mrn": mrn,
               "appointment_id": appointment_id, "items": lines,
               "total_minor": sum(l["line_minor"] for l in lines), "paid_minor": 0,
               "status": "issued", "created_by": ident.subject, "created_at": now()}
        db.invoices.insert_one(dict(inv))
        if p.get("insurance"):
            db.claims.insert_one({"invoice_id": inv["invoice_id"], "mrn": mrn,
                                  "provider_id": p["insurance"]["provider_id"],
                                  "status": "not_submitted", "created_at": now()})
        audit(ident, "create_invoice", inv["invoice_id"])
        inv["total_inr"] = inr(inv["total_minor"])
        return inv

    return idempotent(ident, "create_invoice", request_id, run)


def mock_gateway_verify(gateway_ref: str) -> dict:
    """Mock payment gateway. PAY-<paise> succeeds for that amount, anything else fails.
    Example: PAY-150000 -> INR 1500.00. Replace with the real gateway lookup."""
    m = re.fullmatch(r"PAY-(\d+)", gateway_ref)
    return {"status": "captured", "amount_minor": int(m.group(1))} if m else {"status": "failed", "amount_minor": 0}


@mcp.tool(annotations=WRITE)
def record_payment(invoice_id: str, gateway_ref: str) -> dict:
    """Verify gateway_ref with the (mock) gateway and apply it. gateway_ref is unique, so replays are safe."""
    ident = identity()
    require_roles(ident, "billing", "admin", "receptionist")
    inv = db.invoices.find_one({"invoice_id": invoice_id})
    if not inv:
        raise ToolError("Invoice not found.")
    if inv["status"] == "paid":
        raise ToolError("Invoice is already fully paid.")
    gw = mock_gateway_verify(gateway_ref)
    if gw["status"] != "captured":
        raise ToolError("Gateway did not confirm this payment.")
    balance = inv["total_minor"] - inv["paid_minor"]
    if gw["amount_minor"] > balance:
        raise ToolError(f"Payment exceeds balance of INR {inr(balance):.2f}.")
    try:
        db.payments.insert_one({"gateway_ref": gateway_ref, "invoice_id": invoice_id,
                                "amount_minor": gw["amount_minor"], "recorded_by": ident.subject, "at": now()})
    except DuplicateKeyError:
        raise ToolError("This gateway_ref was already recorded.")
    new_paid = inv["paid_minor"] + gw["amount_minor"]
    status = "paid" if new_paid == inv["total_minor"] else "partial"
    db.invoices.update_one({"invoice_id": invoice_id}, {"$set": {"paid_minor": new_paid, "status": status}})
    audit(ident, "record_payment", invoice_id)
    return {"invoice_id": invoice_id, "status": status, "paid_inr": inr(new_paid),
            "balance_inr": inr(inv["total_minor"] - new_paid)}


# Mock insurer catalogue: code -> (name, estimated cost in paise, category)
PROCEDURES = {
    "APP001": ("Appendectomy", 6_000_000, "surgery"),
    "CAT001": ("Cataract surgery", 4_000_000, "surgery"),
    "MRI001": ("MRI brain", 800_000, "diagnostics"),
    "KNE001": ("Knee replacement", 25_000_000, "surgery"),
    "COS001": ("Cosmetic rhinoplasty", 20_000_000, "cosmetic"),
}


def mock_insurer_decide(provider: dict, policy: dict, code: str) -> dict:
    """Mock insurer service: deterministic approve / pending_review / denied."""
    if code not in PROCEDURES:
        return {"decision": "denied", "reason": "Unknown procedure code"}
    name, est, cat = PROCEDURES[code]
    if policy.get("status") != "active":
        return {"decision": "denied", "reason": "Policy not active"}
    if cat in provider.get("exclusions", []):
        return {"decision": "denied", "reason": f"{cat} is excluded"}
    if cat not in provider.get("covered", []):
        return {"decision": "denied", "reason": f"{cat} is not covered"}
    if est > policy.get("sum_insured_minor", 0):
        return {"decision": "denied", "reason": "Exceeds sum insured"}
    if est > 10_000_000:
        return {"decision": "pending_review", "reason": "High value: manual review required"}
    return {"decision": "approved", "approved_minor": est}


@mcp.tool(annotations=WRITE)
def submit_preauthorization(mrn: str, procedure_code: str) -> dict:
    """Submit a pre-authorization to the (mock) insurer using the patient's policy on file."""
    ident = identity()
    require_roles(ident, "doctor", "billing", "admin")
    p = db.patients.find_one({"mrn": mrn})
    if not p:
        raise ToolError("Patient not found.")
    if not p.get("insurance"):
        raise ToolError("No insurance on file for this patient.")
    ins = db.insurers.find_one({"provider_id": p["insurance"]["provider_id"], "accepted": True})
    pol = db.policies.find_one(p["insurance"])
    if not ins or not pol:
        raise ToolError("Insurer or policy not recognised.")
    decision = mock_insurer_decide(ins, pol, procedure_code)
    rec = {"preauth_id": f"PA-{uuid.uuid4().hex[:8].upper()}", "mrn": mrn, "procedure_code": procedure_code,
           "provider_id": ins["provider_id"], "submitted_by": ident.subject, "submitted_at": now(), **decision}
    db.preauths.insert_one(dict(rec))
    audit(ident, "submit_preauthorization", rec["preauth_id"])
    return clean(rec)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ensure_indexes()
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport != "stdio":
        mcp.settings.host = os.getenv("HOST", "127.0.0.1")
        mcp.settings.port = int(os.getenv("PORT", "8000"))
    mcp.run(transport=transport)