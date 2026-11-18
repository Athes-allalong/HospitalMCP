"""Admin tools (one hospital) and platform tools (onboarding hospitals).

The two-phase admin business rules live in services/admin.py (AdminService); this module binds
them to the caller's hospital and adds the read-only admin tools.
"""
import re
from datetime import datetime, time, timedelta
from typing import Any, List, Optional

from ..authz import STAFF_ROLES, authorize
from ..db import DuplicateKeyError
from ..errors import Codes, HospitalError
from .admin import AdminService
from .common import bad, clean, parse_date
from .pending import Plan, pending_for, two_phase


class TenantRepo:
    """admin.Repo over the caller's hospital (tenancy.TenantDB)."""

    def __init__(self, ctx):
        self.ctx, self.t = ctx, ctx.t

    def _insert(self, coll, doc: dict, what: str) -> None:
        try:
            coll.insert_one(doc)
        except DuplicateKeyError:
            raise HospitalError(Codes.DUPLICATE, f"{what} already exists.")

    def get_doctor(self, staff_id: str, include_inactive: bool = False) -> Optional[dict]:
        q: dict = {"staff_id": staff_id}
        if not include_inactive:
            q["active"] = {"$ne": False}
        return self.t.doctors.find_one(q)

    def insert_doctor(self, doc: dict) -> None:
        self._insert(self.t.doctors, doc, f"Doctor '{doc['staff_id']}'")

    def update_doctor(self, staff_id: str, fields: dict) -> None:
        self.t.doctors.update_one({"staff_id": staff_id}, {"$set": fields})

    def get_department(self, department_id: str) -> Optional[dict]:
        return self.t.departments.find_one({"department_id": department_id})

    def insert_department(self, doc: dict) -> None:
        self._insert(self.t.departments, doc, f"Department '{doc['department_id']}'")

    def update_department(self, department_id: str, fields: dict) -> None:
        self.t.departments.update_one({"department_id": department_id}, {"$set": fields})
        if "name" in fields:
            self.t.doctors.update_many({"department_id": department_id}, {"$set": {"department_name": fields["name"]}})
        if "head_staff_id" in fields:
            d = self.get_doctor(fields["head_staff_id"]) or {}
            self.t.departments.update_one({"department_id": department_id}, {"$set": {"head_name": d.get("name")}})

    def future_booked_appointments(self, doctor_id: str, after: datetime) -> List[dict]:
        return list(self.t.appointments.find({"doctor_id": doctor_id, "status": "booked",
                                              "slot_start": {"$gt": after}}).sort("slot_start", 1))

    def insert_leave(self, doc: dict) -> None:
        self.t.leaves.insert_one(doc)

    def get_staff_user(self, subject: str) -> Optional[dict]:
        return self.t.staff_users.find_one({"subject": subject})

    def insert_staff_user(self, doc: dict) -> None:
        self._insert(self.t.staff_users, doc, f"Staff user '{doc['subject']}' (or that e-mail)")

    def update_staff_user(self, subject: str, fields: dict) -> None:
        self.t.staff_users.update_one({"subject": subject}, {"$set": fields})

    def count_active_admins(self) -> int:
        return self.t.staff_users.count_documents({"role": "admin", "active": True})

    def revoke_tokens(self, subject: str) -> None:
        """Nothing to revoke: tokens are short-lived JWTs and every call re-checks staff_users.active."""

    def email_in_use(self, email: str) -> bool:
        return self.ctx.raw.staff_users.find_one({"email": email}) is not None


def admin_service(ctx) -> AdminService:
    def audit(ident, tool, target=None, **kw):
        ctx.audit(tool, target, **kw)
    return AdminService(TenantRepo(ctx), pending_for(ctx), audit, clock=ctx.clock)


# --------------------------------------------------------------------------
# Read-only admin tools
# --------------------------------------------------------------------------
def list_staff_users(ctx, role: Optional[str] = None, include_inactive: bool = False) -> dict:
    authorize(ctx.ident, "list_staff_users")
    q: dict = {}
    if role:
        if role not in STAFF_ROLES:
            raise bad(f"role must be one of {', '.join(STAFF_ROLES)}.")
        q["role"] = role
    if not include_inactive:
        q["active"] = True
    rows = ctx.t.staff_users.find(q).sort([("role", 1), ("subject", 1)])
    ctx.audit("list_staff_users")
    return {"staff_users": [clean({k: u.get(k) for k in ("subject", "name", "email", "role", "department_id",
                                                           "active", "created_at")}) for u in rows]}


def get_audit_log(ctx, actor: Optional[str] = None, tool: Optional[str] = None, date_from: Optional[str] = None,
                  date_to: Optional[str] = None, outcome: Optional[str] = None, limit: int = 50) -> dict:
    authorize(ctx.ident, "get_audit_log")
    if not 1 <= limit <= 200:
        raise bad("limit must be 1-200.")
    q: dict = {"hospital_id": ctx.ident.hospital_id}
    if actor:
        q["actor"] = actor
    if tool:
        q["tool"] = tool
    if outcome:
        q["outcome"] = outcome
    if date_from or date_to:
        rng: dict = {}
        if date_from:
            rng["$gte"] = datetime.combine(parse_date(date_from, "date_from"), time.min)
        if date_to:
            rng["$lt"] = datetime.combine(parse_date(date_to, "date_to") + timedelta(days=1), time.min)
        q["at"] = rng
    rows = list(ctx.raw.audit_log.find(q).sort("at", -1).limit(limit))
    return {"count": len(rows), "entries": clean(rows)}


# --------------------------------------------------------------------------
# Platform tools: hospitals (tenants). platform_admin only; never a hospital admin.
# --------------------------------------------------------------------------
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,38}[a-z0-9])$")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def list_hospitals(ctx) -> dict:
    authorize(ctx.ident, "list_hospitals")
    out = []
    for h in ctx.raw.hospitals.find().sort("name", 1):
        hid = h["hospital_id"]
        out.append({"hospital_id": hid, "name": h["name"], "slug": h["slug"], "active": h.get("active", True),
                    "staff_users": ctx.raw.staff_users.count_documents({"hospital_id": hid}),
                    "doctors": ctx.raw.doctors.count_documents({"hospital_id": hid})})
    return {"hospitals": out}


def create_hospital(ctx, name: str, slug: str, admin_email: str, admin_name: str,
                    confirmation_id: Optional[str] = None) -> dict:
    """Creates the tenant and its first admin (bootstrap, plan s.5). The admin then signs in and adds the rest."""
    authorize(ctx.ident, "create_hospital")
    args = {"name": name, "slug": slug, "admin_email": admin_email, "admin_name": admin_name}

    def prepare() -> Plan:
        if not isinstance(name, str) or not 3 <= len(name.strip()) <= 100:
            raise bad("name must be 3-100 characters.")
        if not isinstance(slug, str) or not SLUG_RE.match(slug):
            raise bad("slug must be 3-40 lowercase letters, digits or '-', e.g. 'sunrise-clinic'.")
        if not EMAIL_RE.fullmatch(admin_email or ""):
            raise bad("admin_email is not valid.")
        if ctx.raw.hospitals.find_one({"slug": slug}):
            raise HospitalError(Codes.DUPLICATE, f"Slug '{slug}' is taken.")
        if ctx.raw.staff_users.find_one({"email": admin_email.lower()}):
            raise HospitalError(Codes.DUPLICATE, "That e-mail is already registered on the platform.")
        n = ctx.raw.hospitals.count_documents({}) + 1
        while ctx.raw.hospitals.find_one({"hospital_id": f"H{n:03d}"}):
            n += 1
        hid = f"H{n:03d}"
        summary = {"action": "create_hospital", "hospital_id": hid, "name": name.strip(), "slug": slug,
                   "first_admin": admin_email.lower()}

        def apply():
            ctx.raw.hospitals.insert_one({"hospital_id": hid, "name": name.strip(), "slug": slug, "active": True,
                                          "created_by": ctx.ident.email, "created_at": ctx.clock()})
            ctx.raw.staff_users.insert_one({"hospital_id": hid, "subject": "ADM-001", "email": admin_email.lower(),
                                            "name": admin_name.strip(), "role": "admin", "department_id": None,
                                            "active": True, "auth_sub": None, "created_by": ctx.ident.email,
                                            "created_at": ctx.clock()})
            return None, summary
        return Plan(hid, summary, apply)

    return _platform_two_phase(ctx, "create_hospital", args, confirmation_id, prepare)


def set_hospital_active(ctx, hospital_id: str, active: bool, confirmation_id: Optional[str] = None) -> dict:
    authorize(ctx.ident, "set_hospital_active")
    args = {"hospital_id": hospital_id, "active": active}

    def prepare() -> Plan:
        h = ctx.raw.hospitals.find_one({"hospital_id": hospital_id})
        if not h:
            raise HospitalError(Codes.NOT_FOUND, f"Hospital '{hospital_id}' not found.")
        summary = {"action": "set_hospital_active", "hospital": h["name"], "before": h.get("active", True),
                   "after": bool(active),
                   "note": "When inactive, none of this hospital's staff can sign in or call tools."}

        def apply():
            ctx.raw.hospitals.update_one({"hospital_id": hospital_id}, {"$set": {"active": bool(active)}})
            return {"active": h.get("active", True)}, {"active": bool(active)}
        return Plan(hospital_id, summary, apply)

    return _platform_two_phase(ctx, "set_hospital_active", args, confirmation_id, prepare)


class _PlatformCtx:
    """Platform admins have no hospital; their confirmations live under a reserved tenant id."""

    def __init__(self, ctx):
        from ..identity import Identity
        from ..tenancy import TenantDB
        self.__dict__.update(ctx.__dict__)
        self._ctx = ctx
        self.t = TenantDB(ctx.raw, "__platform__")
        self.ident = ctx.ident

    def audit(self, *a, **k):
        return self._ctx.audit(*a, **k)


def _platform_two_phase(ctx, tool, args, confirmation_id, prepare) -> dict:
    return two_phase(_PlatformCtx(ctx), tool, args, confirmation_id, prepare)
