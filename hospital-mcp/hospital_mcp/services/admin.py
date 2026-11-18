"""Admin business rules: doctors, schedules, departments, staff users.

Every method follows the same flow (see AdminService._run):
  1. authorize()                        deny by default; denials are audited
  2. prepare()                          validate input and current state; builds a plan, writes nothing
  3. no confirmation_id -> preview      returns what would change plus a single-use confirmation_id
     confirmation_id    -> commit       consumes the confirmation, then applies; audit stores before/after
Validation runs in both phases, so a commit re-checks the world as it is now.
"""
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

from ..authz import STAFF_ROLES, authorize
from ..errors import Codes, HospitalError
from .common import local_now, to_minor
from .pending import PendingActions

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
GRID_MIN = 15
MAX_LEAVE_DAYS = 90
ID_RE = re.compile(r"[A-Za-z0-9_-]{2,20}")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
HHMM_RE = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")


def bad(msg: str, **kw) -> HospitalError:
    return HospitalError(Codes.INVALID_INPUT, msg, **kw)


# --------------------------------------------------------------------------
# Repository the service needs. mongo_repos.MongoRepo implements it; tests use a fake.
# --------------------------------------------------------------------------
class Repo(Protocol):
    def get_doctor(self, staff_id: str, include_inactive: bool = False) -> Optional[dict]: ...
    def insert_doctor(self, doc: dict) -> None: ...          # raises HospitalError(DUPLICATE)
    def update_doctor(self, staff_id: str, fields: dict) -> None: ...
    def get_department(self, department_id: str) -> Optional[dict]: ...
    def insert_department(self, doc: dict) -> None: ...      # raises HospitalError(DUPLICATE)
    def update_department(self, department_id: str, fields: dict) -> None: ...
    def future_booked_appointments(self, doctor_id: str, after: datetime) -> List[dict]: ...
    def insert_leave(self, doc: dict) -> None: ...
    def get_staff_user(self, subject: str) -> Optional[dict]: ...
    def insert_staff_user(self, doc: dict) -> None: ...      # raises HospitalError(DUPLICATE)
    def update_staff_user(self, subject: str, fields: dict) -> None: ...
    def count_active_admins(self) -> int: ...
    def revoke_tokens(self, subject: str) -> None: ...
    def email_in_use(self, email: str) -> bool: ...


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------
def _minutes(hhmm: Any) -> int:
    m = HHMM_RE.fullmatch(hhmm) if isinstance(hhmm, str) else None
    if not m:
        raise bad(f"Times must be HH:MM (24h), got {hhmm!r}.")
    return int(m.group(1)) * 60 + int(m.group(2))


def validate_schedule(raw: Any) -> Dict[str, List[List[str]]]:
    """{'mon': [['09:00','13:00']], ...}: valid days, 15-minute grid, no overlaps, end after start."""
    if not isinstance(raw, dict) or not raw:
        raise bad("schedule must be a non-empty object like {'mon': [['09:00','13:00']]}.")
    out: Dict[str, List[List[str]]] = {}
    for day, windows in raw.items():
        if day not in DAYS:
            raise bad(f"Unknown day '{day}'. Use {', '.join(DAYS)}.")
        if not isinstance(windows, list) or not windows:
            raise bad(f"{day}: give at least one [start, end] window.")
        parsed = []
        for w in windows:
            if not (isinstance(w, (list, tuple)) and len(w) == 2):
                raise bad(f"{day}: each window is [start, end].")
            s, e = _minutes(w[0]), _minutes(w[1])
            if e <= s:
                raise bad(f"{day}: {w[0]}-{w[1]} ends before it starts.")
            if s % GRID_MIN or e % GRID_MIN:
                raise bad(f"{day}: times must fall on a {GRID_MIN}-minute grid.")
            parsed.append((s, e))
        parsed.sort()
        for (s1, e1), (s2, e2) in zip(parsed, parsed[1:]):
            if s2 < e1:
                raise bad(f"{day}: windows overlap.")
        out[day] = [[f"{s // 60:02d}:{s % 60:02d}", f"{e // 60:02d}:{e % 60:02d}"] for s, e in parsed]
    return out


def schedule_covers(schedule: dict, start: datetime, end: datetime) -> bool:
    if end.date() != start.date():
        return False
    for s, e in schedule.get(DAYS[start.weekday()], []):
        if time.fromisoformat(s) <= start.time() and end.time() <= time.fromisoformat(e):
            return True
    return False


def _name(v):
    if not isinstance(v, str) or not 2 <= len(v.strip()) <= 100:
        raise bad("name must be 2-100 characters.")
    return "name", v.strip()


def _specialty(v):
    if not isinstance(v, str) or not 2 <= len(v.strip()) <= 100:
        raise bad("specialty must be 2-100 characters.")
    return "specialty", v.strip()


def _fee(v):
    try:
        ok = 0 < float(v) <= 100_000 and not isinstance(v, bool)
    except (TypeError, ValueError):
        ok = False
    if not ok:
        raise bad("consultation_fee_inr must be between 0 and 100000.")
    return "consultation_fee_minor", to_minor(v)


def _room(v):
    if not isinstance(v, str) or not 1 <= len(v.strip()) <= 30:
        raise bad("room must be 1-30 characters.")
    return "room", v.strip()


def _bio(v):
    if not isinstance(v, str) or len(v) > 1000:
        raise bad("bio must be text up to 1000 characters.")
    return "bio", v.strip()


def _str_list(key):
    def f(v):
        if not isinstance(v, list) or len(v) > 20 or not all(isinstance(x, str) and 1 <= len(x) <= 100 for x in v):
            raise bad(f"{key} must be a list of up to 20 short strings.")
        return key, [x.strip() for x in v]
    return f


def _years(v):
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 70:
        raise bad("experience_years must be a whole number from 0 to 70.")
    return "experience_years", v


# api field name -> validator returning (db field, db value)
PROFILE_FIELDS: Dict[str, Callable] = {
    "name": _name, "specialty": _specialty, "consultation_fee_inr": _fee, "room": _room, "bio": _bio,
    "qualifications": _str_list("qualifications"), "languages": _str_list("languages"),
    "experience_years": _years,
}


def _parse_date(v: Any, field: str) -> date:
    try:
        return date.fromisoformat(v)
    except (TypeError, ValueError):
        raise bad(f"{field} must be YYYY-MM-DD.")


def _brief(a: dict) -> dict:
    """Appointment summary for admins: no patient identifiers."""
    return {"appointment_id": a["appointment_id"], "slot_start": a["slot_start"].isoformat()}


@dataclass
class Plan:
    target: str
    summary: dict
    apply: Callable[[], Tuple[Optional[dict], Optional[dict]]]  # returns (before, after) for the audit log


def _noop_audit(*a, **k):
    return None


class AdminService:
    """audit signature: audit(ident, tool, target=None, *, outcome='ok', before=None, after=None, error_code=None)"""

    def __init__(self, repo: Repo, pending: PendingActions, audit: Callable = _noop_audit,
                 clock: Callable[[], datetime] = local_now):
        self.repo, self.pending, self.audit, self.clock = repo, pending, audit, clock

    # ---- the shared two-phase flow ----------------------------------------
    def _run(self, ident, tool: str, args: dict, confirmation_id: Optional[str], prepare: Callable[[], Plan]) -> dict:
        try:
            authorize(ident, tool)
        except HospitalError as e:
            self.audit(ident, tool, None, outcome="denied", error_code=e.code)
            raise
        try:
            plan = prepare()
            if confirmation_id is None:
                conf = self.pending.create(ident, tool, args)
                self.audit(ident, tool, plan.target, outcome="previewed")
                return {"status": "preview", "tool": tool, "target": plan.target, "preview": plan.summary, **conf,
                        "next_step": "Show this preview to the user. Only after they confirm, call the tool again "
                                     "with the same arguments plus this confirmation_id."}
            self.pending.consume(ident, tool, args, confirmation_id)
            before, after = plan.apply()
            self.audit(ident, tool, plan.target, outcome="ok", before=before, after=after)
            return {"status": "committed", "tool": tool, "target": plan.target, "result": after}
        except HospitalError as e:
            self.audit(ident, tool, None, outcome="error", error_code=e.code)
            raise

    def _active_doctor(self, staff_id: str) -> dict:
        d = self.repo.get_doctor(staff_id)
        if not d:
            raise HospitalError(Codes.NOT_FOUND, f"Doctor '{staff_id}' not found or inactive.")
        return d

    # ---- doctors ----------------------------------------------------------
    def create_doctor(self, ident, args: dict, confirmation_id: Optional[str] = None) -> dict:
        def prepare() -> Plan:
            missing = [k for k in ("staff_id", "name", "department_id", "specialty",
                                   "consultation_fee_inr", "room", "schedule") if args.get(k) in (None, "")]
            if missing:
                raise bad(f"Missing required fields: {', '.join(missing)}.")
            sid = args["staff_id"]
            if not isinstance(sid, str) or not ID_RE.fullmatch(sid):
                raise bad("staff_id must be 2-20 letters, digits, '_' or '-'.")
            if self.repo.get_doctor(sid, include_inactive=True):
                raise HospitalError(Codes.DUPLICATE, f"A doctor with staff_id '{sid}' already exists.")
            dept = self.repo.get_department(args["department_id"])
            if not dept:
                raise HospitalError(Codes.NOT_FOUND, f"Department '{args['department_id']}' does not exist.",
                                    hint="Call list_departments, or create_department first.")
            doc = {"staff_id": sid, "department_id": dept["department_id"], "department_name": dept["name"],
                   "qualifications": [], "languages": [], "experience_years": 0, "bio": "",
                   "schedule": validate_schedule(args["schedule"]), "active": True,
                   "created_by": ident.subject, "created_at": self.clock()}
            for api_field, check in PROFILE_FIELDS.items():
                if api_field in args and args[api_field] is not None:
                    k, v = check(args[api_field])
                    doc[k] = v
            summary = {"action": "create_doctor", "staff_id": sid, "name": doc["name"],
                       "department": dept["name"], "specialty": doc["specialty"],
                       "consultation_fee_inr": doc["consultation_fee_minor"] / 100, "room": doc["room"],
                       "schedule": doc["schedule"]}

            def apply():
                self.repo.insert_doctor(dict(doc))
                return None, {k: doc[k] for k in ("staff_id", "name", "department_id", "specialty", "schedule")}
            return Plan(sid, summary, apply)
        return self._run(ident, "create_doctor", args, confirmation_id, prepare)

    def update_doctor_profile(self, ident, staff_id: str, changes: dict, confirmation_id: Optional[str] = None) -> dict:
        args = {"staff_id": staff_id, "changes": changes}

        def prepare() -> Plan:
            d = self._active_doctor(staff_id)
            if not isinstance(changes, dict) or not changes:
                raise bad("changes must be a non-empty object.")
            unknown = set(changes) - set(PROFILE_FIELDS)
            if unknown:
                raise bad(f"Cannot change: {', '.join(sorted(unknown))}. Allowed: {', '.join(PROFILE_FIELDS)}.")
            upd = dict(PROFILE_FIELDS[f](v) for f, v in changes.items())
            before, after = {k: d.get(k) for k in upd}, dict(upd)
            summary = {"action": "update_doctor_profile", "staff_id": staff_id, "before": before, "after": after}
            if "consultation_fee_minor" in upd:
                summary["note"] = "A new fee applies to future bookings only."

            def apply():
                self.repo.update_doctor(staff_id, {**upd, "updated_at": self.clock(), "updated_by": ident.subject})
                return before, after
            return Plan(staff_id, summary, apply)
        return self._run(ident, "update_doctor_profile", args, confirmation_id, prepare)

    def update_doctor_schedule(self, ident, staff_id: str, schedule: dict, confirmation_id: Optional[str] = None) -> dict:
        args = {"staff_id": staff_id, "schedule": schedule}

        def prepare() -> Plan:
            d = self._active_doctor(staff_id)
            new = validate_schedule(schedule)
            conflicts = [a for a in self.repo.future_booked_appointments(staff_id, self.clock())
                         if not schedule_covers(new, a["slot_start"], a["slot_end"])]
            if conflicts:
                raise HospitalError(
                    Codes.SCHEDULE_CONFLICT,
                    f"{len(conflicts)} booked appointment(s) fall outside the new schedule. Nothing was changed.",
                    hint="Reschedule or cancel those appointments first, or widen the schedule.",
                    data={"appointments": [_brief(a) for a in conflicts]})
            summary = {"action": "update_doctor_schedule", "staff_id": staff_id,
                       "before": d.get("schedule"), "after": new}

            def apply():
                self.repo.update_doctor(staff_id, {"schedule": new, "updated_at": self.clock(), "updated_by": ident.subject})
                return {"schedule": d.get("schedule")}, {"schedule": new}
            return Plan(staff_id, summary, apply)
        return self._run(ident, "update_doctor_schedule", args, confirmation_id, prepare)

    def add_schedule_exception(self, ident, staff_id: str, start_date: str, end_date: str, reason: str,
                               confirmation_id: Optional[str] = None) -> dict:
        args = {"staff_id": staff_id, "start_date": start_date, "end_date": end_date, "reason": reason}

        def prepare() -> Plan:
            self._active_doctor(staff_id)
            sd, ed = _parse_date(start_date, "start_date"), _parse_date(end_date, "end_date")
            now = self.clock()
            if ed < sd:
                raise bad("end_date is before start_date.")
            if sd < now.date():
                raise bad("start_date is in the past.")
            if (ed - sd).days + 1 > MAX_LEAVE_DAYS:
                raise bad(f"An exception can span at most {MAX_LEAVE_DAYS} days.")
            if not isinstance(reason, str) or not 2 <= len(reason.strip()) <= 200:
                raise bad("reason must be 2-200 characters.")
            after = max(now, datetime.combine(sd, time.min))
            affected = [a for a in self.repo.future_booked_appointments(staff_id, after)
                        if a["slot_start"].date() <= ed]
            leave = {"doctor_id": staff_id, "start_date": sd.isoformat(), "end_date": ed.isoformat(),
                     "reason": reason.strip(), "created_by": ident.subject, "created_at": now}
            summary = {"action": "add_schedule_exception", "staff_id": staff_id, "start_date": leave["start_date"],
                       "end_date": leave["end_date"], "reason": leave["reason"],
                       "affected_appointments": [_brief(a) for a in affected],
                       "note": ("These booked appointments are NOT cancelled automatically; reschedule or cancel them."
                                if affected else "No booked appointments are affected.")}

            def apply():
                self.repo.insert_leave(dict(leave))
                return None, {k: leave[k] for k in ("doctor_id", "start_date", "end_date", "reason")}
            return Plan(f"{staff_id}:{leave['start_date']}", summary, apply)
        return self._run(ident, "add_schedule_exception", args, confirmation_id, prepare)

    def deactivate_doctor(self, ident, staff_id: str, confirmation_id: Optional[str] = None) -> dict:
        args = {"staff_id": staff_id}

        def prepare() -> Plan:
            d = self._active_doctor(staff_id)
            future = self.repo.future_booked_appointments(staff_id, self.clock())
            if future:
                raise HospitalError(
                    Codes.HAS_FUTURE_APPOINTMENTS,
                    f"Dr. {d['name']} has {len(future)} future booked appointment(s). Nothing was changed.",
                    hint="Reschedule or cancel them first.", data={"appointments": [_brief(a) for a in future]})
            dept = self.repo.get_department(d.get("department_id")) if d.get("department_id") else None
            if dept and dept.get("head_staff_id") == staff_id:
                raise HospitalError(Codes.DEPARTMENT_HEAD,
                                    f"{d['name']} is head of {dept['name']}.", hint="Use set_department_head first.")
            summary = {"action": "deactivate_doctor", "staff_id": staff_id, "name": d["name"],
                       "note": "The doctor is hidden from search and booking. The record is kept (soft delete)."}

            def apply():
                self.repo.update_doctor(staff_id, {"active": False, "deactivated_at": self.clock(),
                                                   "deactivated_by": ident.subject})
                return {"active": True}, {"active": False}
            return Plan(staff_id, summary, apply)
        return self._run(ident, "deactivate_doctor", args, confirmation_id, prepare)

    # ---- departments ------------------------------------------------------
    def create_department(self, ident, department_id: str, name: str, location: str = "",
                          confirmation_id: Optional[str] = None) -> dict:
        args = {"department_id": department_id, "name": name, "location": location}

        def prepare() -> Plan:
            if not isinstance(department_id, str) or not ID_RE.fullmatch(department_id):
                raise bad("department_id must be 2-20 letters, digits, '_' or '-'.")
            if not isinstance(name, str) or not 2 <= len(name.strip()) <= 100:
                raise bad("name must be 2-100 characters.")
            if self.repo.get_department(department_id):
                raise HospitalError(Codes.DUPLICATE, f"Department '{department_id}' already exists.")
            doc = {"department_id": department_id, "name": name.strip(), "location": (location or "").strip()[:100],
                   "head_staff_id": None, "avg_consult_minutes": 15, "created_by": ident.subject}
            summary = {"action": "create_department", "department_id": department_id, "name": doc["name"],
                       "location": doc["location"]}

            def apply():
                self.repo.insert_department(dict(doc))
                return None, {k: doc[k] for k in ("department_id", "name", "location")}
            return Plan(department_id, summary, apply)
        return self._run(ident, "create_department", args, confirmation_id, prepare)

    def update_department(self, ident, department_id: str, changes: dict, confirmation_id: Optional[str] = None) -> dict:
        args = {"department_id": department_id, "changes": changes}
        allowed = {"name": (2, 100), "location": (0, 100), "phone_ext": (0, 20), "hours": (0, 100)}

        def prepare() -> Plan:
            dept = self.repo.get_department(department_id)
            if not dept:
                raise HospitalError(Codes.NOT_FOUND, f"Department '{department_id}' not found.")
            if not isinstance(changes, dict) or not changes:
                raise bad("changes must be a non-empty object.")
            unknown = set(changes) - set(allowed) - {"services", "avg_consult_minutes"}
            if unknown:
                raise bad(f"Cannot change: {', '.join(sorted(unknown))}. Allowed: name, location, phone_ext, hours, "
                          "services, avg_consult_minutes.")
            upd = {}
            for k, v in changes.items():
                if k in allowed:
                    lo, hi = allowed[k]
                    if not isinstance(v, str) or not lo <= len(v.strip()) <= hi:
                        raise bad(f"{k} must be text of {lo}-{hi} characters.")
                    upd[k] = v.strip()
                elif k == "services":
                    if not isinstance(v, list) or len(v) > 20 or not all(isinstance(x, str) for x in v):
                        raise bad("services must be a list of up to 20 strings.")
                    upd[k] = [x.strip() for x in v]
                else:
                    if isinstance(v, bool) or not isinstance(v, int) or not 5 <= v <= 120:
                        raise bad("avg_consult_minutes must be 5-120.")
                    upd[k] = v
            before = {k: dept.get(k) for k in upd}
            summary = {"action": "update_department", "department_id": department_id, "before": before, "after": upd}

            def apply():
                self.repo.update_department(department_id, upd)
                return before, dict(upd)
            return Plan(department_id, summary, apply)
        return self._run(ident, "update_department", args, confirmation_id, prepare)

    def set_department_head(self, ident, department_id: str, staff_id: str, confirmation_id: Optional[str] = None) -> dict:
        args = {"department_id": department_id, "staff_id": staff_id}

        def prepare() -> Plan:
            dept = self.repo.get_department(department_id)
            if not dept:
                raise HospitalError(Codes.NOT_FOUND, f"Department '{department_id}' not found.")
            d = self._active_doctor(staff_id)
            if d.get("department_id") != department_id:
                raise bad(f"Dr. {d['name']} belongs to a different department.")
            summary = {"action": "set_department_head", "department": dept["name"],
                       "before": dept.get("head_staff_id"), "after": staff_id}

            def apply():
                self.repo.update_department(department_id, {"head_staff_id": staff_id})
                return {"head_staff_id": dept.get("head_staff_id")}, {"head_staff_id": staff_id}
            return Plan(department_id, summary, apply)
        return self._run(ident, "set_department_head", args, confirmation_id, prepare)

    # ---- staff users ------------------------------------------------------
    def create_staff_user(self, ident, subject: str, email: str, role: str, department_id: Optional[str] = None,
                          confirmation_id: Optional[str] = None, name: str = "") -> dict:
        args = {"subject": subject, "email": email, "role": role, "department_id": department_id, "name": name}

        def prepare() -> Plan:
            if not isinstance(subject, str) or not ID_RE.fullmatch(subject):
                raise bad("subject must be 2-20 letters, digits, '_' or '-'.")
            if role not in STAFF_ROLES:
                raise bad(f"role must be one of: {', '.join(STAFF_ROLES)}.")
            if not isinstance(email, str) or not EMAIL_RE.fullmatch(email):
                raise bad("email is not valid.")
            if role == "doctor" and not self.repo.get_doctor(subject):
                raise bad("A doctor user's subject must be an active doctor's staff_id.",
                          hint="Create the doctor first with create_doctor.")
            if department_id and not self.repo.get_department(department_id):
                raise HospitalError(Codes.NOT_FOUND, f"Department '{department_id}' does not exist.")
            if role == "nurse" and not department_id:
                raise bad("A nurse needs a department_id (nurses see their own department's patients).")
            if self.repo.get_staff_user(subject):
                raise HospitalError(Codes.DUPLICATE, f"A staff user '{subject}' already exists.")
            if self.repo.email_in_use(email.lower()):
                raise HospitalError(Codes.DUPLICATE, "That e-mail is already registered on the platform.")
            doc = {"subject": subject, "email": email.lower(), "name": (name or "").strip()[:100], "role": role,
                   "department_id": department_id, "active": True, "auth_sub": None,
                   "created_by": ident.subject, "created_at": self.clock()}
            summary = {"action": "create_staff_user", "subject": subject, "email": doc["email"], "role": role,
                       "department_id": department_id}

            def apply():
                self.repo.insert_staff_user(dict(doc))
                return None, {k: doc[k] for k in ("subject", "role", "department_id")}
            return Plan(subject, summary, apply)
        return self._run(ident, "create_staff_user", args, confirmation_id, prepare)

    def _last_admin_guard(self, user: dict) -> None:
        if user["role"] == "admin" and user.get("active") and self.repo.count_active_admins() <= 1:
            raise HospitalError(Codes.LAST_ADMIN, "This is the last active admin. Nothing was changed.",
                                hint="Create or promote another admin first.")

    def change_staff_role(self, ident, subject: str, new_role: str, confirmation_id: Optional[str] = None) -> dict:
        args = {"subject": subject, "new_role": new_role}

        def prepare() -> Plan:
            u = self.repo.get_staff_user(subject)
            if not u or not u.get("active"):
                raise HospitalError(Codes.NOT_FOUND, f"Active staff user '{subject}' not found.")
            if new_role not in STAFF_ROLES:
                raise bad(f"new_role must be one of: {', '.join(STAFF_ROLES)}.")
            if new_role == u["role"]:
                raise bad(f"'{subject}' already has role '{new_role}'.")
            if new_role != "admin":
                self._last_admin_guard(u)
            if new_role == "doctor" and not self.repo.get_doctor(subject):
                raise bad("A doctor user's subject must be an active doctor's staff_id.")
            summary = {"action": "change_staff_role", "subject": subject, "before": u["role"], "after": new_role,
                       "note": "The new role applies from the user's next action."}

            def apply():
                self.repo.update_staff_user(subject, {"role": new_role, "updated_by": ident.subject})
                self.repo.revoke_tokens(subject)
                return {"role": u["role"]}, {"role": new_role}
            return Plan(subject, summary, apply)
        return self._run(ident, "change_staff_role", args, confirmation_id, prepare)

    def deactivate_staff_user(self, ident, subject: str, confirmation_id: Optional[str] = None) -> dict:
        args = {"subject": subject}

        def prepare() -> Plan:
            u = self.repo.get_staff_user(subject)
            if not u or not u.get("active"):
                raise HospitalError(Codes.NOT_FOUND, f"Active staff user '{subject}' not found.")
            self._last_admin_guard(u)
            summary = {"action": "deactivate_staff_user", "subject": subject, "role": u["role"],
                       "note": "Access ends immediately: every call checks that the account is active."}

            def apply():
                self.repo.update_staff_user(subject, {"active": False, "deactivated_by": ident.subject,
                                                      "deactivated_at": self.clock()})
                self.repo.revoke_tokens(subject)
                return {"active": True}, {"active": False}
            return Plan(subject, summary, apply)
        return self._run(ident, "deactivate_staff_user", args, confirmation_id, prepare)
