"""In-memory stand-ins for Mongo, so the business rules run without a database."""
import threading
from datetime import datetime, timedelta

from hospital_mcp.errors import Codes, HospitalError


class Ident:
    def __init__(self, role, subject):
        self.role, self.subject = role, subject


ADMIN = Ident("admin", "A001")
RECEPTION = Ident("receptionist", "R001")
DOCTOR = Ident("doctor", "D001")
NOW = datetime(2026, 11, 16, 9, 0)  # a Monday


class FakeRepo:
    def __init__(self):
        self.doctors, self.departments, self.appts, self.leaves = {}, {}, [], []
        self.staff, self.revoked = {}, []
        self.departments["CARD"] = {"department_id": "CARD", "name": "Cardiology", "head_staff_id": None}
        self.departments["NEUR"] = {"department_id": "NEUR", "name": "Neurology", "head_staff_id": None}
        self.staff["A001"] = {"subject": "A001", "role": "admin", "active": True}

    def email_in_use(self, email):
        return any(u.get("email") == email for u in self.staff.values())

    def get_doctor(self, staff_id, include_inactive=False):
        d = self.doctors.get(staff_id)
        return d if d and (include_inactive or d.get("active", True)) else None

    def insert_doctor(self, doc):
        if doc["staff_id"] in self.doctors:
            raise HospitalError(Codes.DUPLICATE, "dup")
        self.doctors[doc["staff_id"]] = doc

    def update_doctor(self, staff_id, fields):
        self.doctors[staff_id].update(fields)

    def get_department(self, department_id):
        return self.departments.get(department_id)

    def insert_department(self, doc):
        if doc["department_id"] in self.departments:
            raise HospitalError(Codes.DUPLICATE, "dup")
        self.departments[doc["department_id"]] = doc

    def update_department(self, department_id, fields):
        self.departments[department_id].update(fields)

    def future_booked_appointments(self, doctor_id, after):
        return sorted((a for a in self.appts if a["doctor_id"] == doctor_id and a["status"] == "booked"
                       and a["slot_start"] > after), key=lambda a: a["slot_start"])

    def insert_leave(self, doc):
        self.leaves.append(doc)

    def get_staff_user(self, subject):
        return self.staff.get(subject)

    def insert_staff_user(self, doc):
        if doc["subject"] in self.staff:
            raise HospitalError(Codes.DUPLICATE, "dup")
        self.staff[doc["subject"]] = doc

    def update_staff_user(self, subject, fields):
        self.staff[subject].update(fields)

    def count_active_admins(self):
        return sum(1 for u in self.staff.values() if u["role"] == "admin" and u.get("active"))

    def revoke_tokens(self, subject):
        self.revoked.append(subject)

    # test helper
    def add_appt(self, doctor_id, start, minutes=15, status="booked", n=[0]):
        n[0] += 1
        self.appts.append({"appointment_id": f"APT-{n[0]}", "doctor_id": doctor_id, "mrn": f"MRN-{n[0]}",
                           "slot_start": start, "slot_end": start + timedelta(minutes=minutes), "status": status})


class MemoryPendingStore:
    def __init__(self):
        self.d, self.l = {}, threading.Lock()

    def put(self, doc):
        self.d[doc["_id"]] = doc

    def take(self, cid):
        with self.l:
            return self.d.pop(cid, None)


class MemoryLockStore:
    """try_lock is atomic, like a unique _id insert in MongoDB."""

    def __init__(self):
        self.d, self.l = {}, threading.Lock()

    def try_lock(self, key, owner, expires_at):
        with self.l:
            if key in self.d:
                return False
            self.d[key] = owner
            return True

    def release(self, owner):
        with self.l:
            for k in [k for k, v in self.d.items() if v == owner]:
                del self.d[k]


class AuditRecorder:
    def __init__(self):
        self.rows = []

    def __call__(self, ident, tool, target=None, *, outcome="ok", before=None, after=None, error_code=None):
        self.rows.append({"subject": ident.subject, "role": ident.role, "tool": tool, "target": target,
                          "outcome": outcome, "before": before, "after": after, "error_code": error_code})
