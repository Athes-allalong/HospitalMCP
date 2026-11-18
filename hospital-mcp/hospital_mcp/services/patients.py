"""Patient records (synthetic data only). Role check + record-level check on every call.

Existence is not leaked: for doctors and nurses, "no such MRN" and "not your patient" give the same
FORBIDDEN answer, because the care-relationship check runs before the lookup.
"""
import re
import uuid
from typing import Optional

from ..authz import authorize, shape_patient_summary
from ..errors import Codes, HospitalError
from .common import bad, clean, not_found, parse_date

PHONE_RE = re.compile(r"^\+?[0-9]{10,13}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _patient(ctx, mrn: str) -> dict:
    p = ctx.t.patients.find_one({"mrn": mrn})
    if not p:
        raise not_found(f"Patient '{mrn}'", hint="Use search_patients to find the MRN.")
    return p


def search_patients(ctx, query: str, limit: int = 10) -> dict:
    """Name, phone or MRN. Results are restricted and masked per role."""
    d = authorize(ctx.ident, "search_patients", listing=True, ctx=ctx)
    if not isinstance(query, str) or len(query.strip()) < 2:
        raise bad("query must be at least 2 characters (name, phone or MRN).")
    if not 1 <= limit <= 20:
        raise bad("limit must be 1-20.")
    rx = {"$regex": re.escape(query.strip()), "$options": "i"}
    q: dict = {"$or": [{"name": rx}, {"phone": rx}, {"mrn": rx}]}
    if d.restrict_doctor_id or d.restrict_department_id:
        field, value = (("doctor_id", d.restrict_doctor_id) if d.restrict_doctor_id
                        else ("department_id", d.restrict_department_id))
        q["mrn"] = {"$in": ctx.t.appointments.distinct("mrn", {field: value})}
    rows = list(ctx.t.patients.find(q).sort("name", 1).limit(limit))
    ctx.audit("search_patients", None, args={"query": query})
    return {"count": len(rows), "patients": [shape_patient_summary(p, d.view) for p in rows]}


def register_patient(ctx, name: str, phone: str, sex: str, dob: str, email: Optional[str] = None,
                     blood_group: Optional[str] = None) -> dict:
    authorize(ctx.ident, "register_patient")
    if not isinstance(name, str) or not 2 <= len(name.strip()) <= 100:
        raise bad("name must be 2-100 characters.")
    if not PHONE_RE.match(phone or ""):
        raise bad("phone must be 10-13 digits, optionally starting with +.")
    if sex not in ("M", "F", "O"):
        raise bad("sex must be M, F or O.")
    born = parse_date(dob, "dob")
    if born > ctx.clock().date():
        raise bad("dob is in the future.")
    if email and not EMAIL_RE.match(email):
        raise bad("email is not valid.")
    dup = ctx.t.patients.find_one({"phone": phone, "name": {"$regex": f"^{re.escape(name.strip())}$", "$options": "i"}})
    if dup:
        raise HospitalError(Codes.DUPLICATE, f"This patient already exists as {dup['mrn']}.")
    n = ctx.t.patients.count_documents({}) + 1001
    while ctx.t.patients.find_one({"mrn": f"MRN-{n}"}):
        n += 1
    p = {"mrn": f"MRN-{n}", "name": name.strip(), "phone": phone, "sex": sex, "dob": born.isoformat(),
         "email": email, "blood_group": blood_group, "allergies": [], "insurance": None,
         "synthetic": True, "created_by": ctx.ident.subject, "created_at": ctx.clock()}
    ctx.t.patients.insert_one(dict(p))
    ctx.audit("register_patient", p["mrn"])
    return {"mrn": p["mrn"], "name": p["name"], "status": "registered"}


def get_patient_summary(ctx, mrn: str) -> dict:
    d = authorize(ctx.ident, "get_patient_summary", mrn=mrn, ctx=ctx)
    p = _patient(ctx, mrn)
    ctx.audit("get_patient_summary", mrn)
    return clean(shape_patient_summary(p, d.view))


def get_admission_status(ctx, mrn: str) -> dict:
    authorize(ctx.ident, "get_admission_status", mrn=mrn, ctx=ctx)
    _patient(ctx, mrn)
    adm = ctx.t.admissions.find_one({"mrn": mrn, "status": "admitted"})
    ctx.audit("get_admission_status", mrn)
    if not adm:
        return {"mrn": mrn, "admitted": False}
    return clean({"mrn": mrn, "admitted": True, "ward": adm.get("ward"), "bed": adm.get("bed"),
                  "admitted_at": adm.get("admitted_at"), "attending_doctor_id": adm.get("doctor_id")})


def get_patient_history(ctx, mrn: str) -> dict:
    authorize(ctx.ident, "get_patient_history", mrn=mrn, ctx=ctx)
    _patient(ctx, mrn)
    visits = list(ctx.t.visits.find({"mrn": mrn}).sort("date", -1).limit(50))
    notes = list(ctx.t.clinical_notes.find({"mrn": mrn}).sort("created_at", -1).limit(50))
    ctx.audit("get_patient_history", mrn)
    return clean({"mrn": mrn, "visits": visits,
                  "clinical_notes": [{"note_id": n["note_id"], "doctor_id": n["doctor_id"],
                                      "created_at": n["created_at"], "untrusted_text": n["note"]} for n in notes]})


def list_prescriptions(ctx, mrn: str) -> dict:
    authorize(ctx.ident, "list_prescriptions", mrn=mrn, ctx=ctx)
    _patient(ctx, mrn)
    rows = list(ctx.t.prescriptions.find({"mrn": mrn}).sort("issued_at", -1).limit(50))
    ctx.audit("list_prescriptions", mrn)
    return clean({"mrn": mrn, "prescriptions": rows})


def get_lab_results(ctx, mrn: str, test_id: Optional[str] = None) -> dict:
    authorize(ctx.ident, "get_lab_results", mrn=mrn, ctx=ctx)
    _patient(ctx, mrn)
    q: dict = {"mrn": mrn}
    if test_id:
        q["test_id"] = test_id
    rows = list(ctx.t.lab_results.find(q).sort("reported_at", -1).limit(100))
    ctx.audit("get_lab_results", mrn)
    return clean({"mrn": mrn, "lab_results": rows})


def update_patient_contact(ctx, mrn: str, phone: Optional[str] = None, email: Optional[str] = None,
                           address: Optional[str] = None, emergency_contact_name: Optional[str] = None,
                           emergency_contact_phone: Optional[str] = None) -> dict:
    """Low-risk edit: no preview step (plan s.7.4), but before/after go to the audit log."""
    authorize(ctx.ident, "update_patient_contact")
    p = _patient(ctx, mrn)
    upd: dict = {}
    if phone is not None:
        if not PHONE_RE.match(phone):
            raise bad("phone must be 10-13 digits, optionally starting with +.")
        upd["phone"] = phone
    if email is not None:
        if not EMAIL_RE.match(email):
            raise bad("email is not valid.")
        upd["email"] = email
    if address is not None:
        if not 5 <= len(address.strip()) <= 300:
            raise bad("address must be 5-300 characters.")
        upd["address"] = address.strip()
    if emergency_contact_name or emergency_contact_phone:
        if not (emergency_contact_name and emergency_contact_phone and PHONE_RE.match(emergency_contact_phone)):
            raise bad("emergency contact needs both a name and a valid phone.")
        upd["emergency_contact"] = {"name": emergency_contact_name.strip(), "phone": emergency_contact_phone}
    if not upd:
        raise bad("No fields supplied.")
    before = {k: p.get(k) for k in upd}
    ctx.t.patients.update_one({"mrn": mrn}, {"$set": {**upd, "contact_updated_at": ctx.clock()}})
    ctx.audit("update_patient_contact", mrn, before=sorted(before), after=sorted(upd))
    return {"mrn": mrn, "updated_fields": sorted(upd)}


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"
