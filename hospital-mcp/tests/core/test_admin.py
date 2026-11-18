import unittest
from datetime import datetime, timedelta

from hospital_mcp.errors import Codes, HospitalError
from hospital_mcp.services.admin import AdminService, schedule_covers, validate_schedule
from hospital_mcp.services.pending import PendingActions
from tests.core.fakes import (ADMIN, DOCTOR, NOW, RECEPTION, AuditRecorder, FakeRepo, Ident, MemoryPendingStore)

SCHED = {"mon": [["09:00", "13:00"]], "wed": [["09:00", "13:00"]]}
DOC = {"staff_id": "D100", "name": "Dr. Test", "department_id": "CARD", "specialty": "Cardiology",
       "consultation_fee_inr": 1200, "room": "A-201", "schedule": SCHED}


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


class Base(unittest.TestCase):
    def setUp(self):
        self.repo, self.audit, self.clock = FakeRepo(), AuditRecorder(), Clock()
        self.pending = PendingActions(MemoryPendingStore(), clock=self.clock)
        self.svc = AdminService(self.repo, self.pending, self.audit, clock=self.clock)

    def commit(self, fn, *a, **kw):
        """Preview, then commit with the returned id. Returns (preview, committed)."""
        p = fn(*a, **kw)
        self.assertEqual(p["status"], "preview")
        return p, fn(*a, **kw, confirmation_id=p["confirmation_id"])

    def code(self, fn, *a, **kw):
        with self.assertRaises(HospitalError) as cm:
            fn(*a, **kw)
        return cm.exception.code

    def seed_doctor(self, sid="D100"):
        self.commit(self.svc.create_doctor, ADMIN, {**DOC, "staff_id": sid})


class PreviewCommitTests(Base):
    def test_preview_writes_nothing_commit_writes(self):
        p = self.svc.create_doctor(ADMIN, DOC)
        self.assertEqual(self.repo.doctors, {})
        self.assertEqual(p["preview"]["staff_id"], "D100")
        c = self.svc.create_doctor(ADMIN, DOC, confirmation_id=p["confirmation_id"])
        self.assertEqual(c["status"], "committed")
        self.assertEqual(self.repo.doctors["D100"]["consultation_fee_minor"], 120000)
        self.assertEqual(self.repo.doctors["D100"]["department_name"], "Cardiology")

    def test_changed_arguments_rejected(self):
        p = self.svc.create_doctor(ADMIN, DOC)
        tampered = {**DOC, "consultation_fee_inr": 1}
        self.assertEqual(self.code(self.svc.create_doctor, ADMIN, tampered, confirmation_id=p["confirmation_id"]),
                         Codes.CONFIRMATION_INVALID)
        self.assertEqual(self.repo.doctors, {})

    def test_confirmation_is_single_use(self):
        p = self.svc.create_doctor(ADMIN, DOC)
        self.svc.create_doctor(ADMIN, DOC, confirmation_id=p["confirmation_id"])
        self.assertIn(self.code(self.svc.create_doctor, ADMIN, DOC, confirmation_id=p["confirmation_id"]),
                      (Codes.CONFIRMATION_INVALID, Codes.DUPLICATE))
        self.assertEqual(len(self.repo.doctors), 1)

    def test_other_caller_cannot_use_confirmation(self):
        self.repo.staff["A002"] = {"subject": "A002", "role": "admin", "active": True}
        p = self.svc.create_doctor(ADMIN, DOC)
        self.assertEqual(self.code(self.svc.create_doctor, Ident("admin", "A002"), DOC,
                                   confirmation_id=p["confirmation_id"]), Codes.CONFIRMATION_INVALID)

    def test_expired_confirmation(self):
        p = self.svc.create_doctor(ADMIN, DOC)
        self.clock.t = NOW + timedelta(minutes=6)
        self.assertEqual(self.code(self.svc.create_doctor, ADMIN, DOC, confirmation_id=p["confirmation_id"]),
                         Codes.CONFIRMATION_EXPIRED)

    def test_made_up_confirmation_id(self):
        self.assertEqual(self.code(self.svc.create_doctor, ADMIN, DOC, confirmation_id="CNF-fake"),
                         Codes.CONFIRMATION_INVALID)

    def test_commit_revalidates_current_state(self):
        p = self.svc.create_doctor(ADMIN, DOC)
        self.repo.insert_doctor({"staff_id": "D100", "active": True})  # someone else created it meanwhile
        self.assertEqual(self.code(self.svc.create_doctor, ADMIN, DOC, confirmation_id=p["confirmation_id"]),
                         Codes.DUPLICATE)


class AuthzAndAuditTests(Base):
    def test_non_admins_are_denied_and_it_is_audited(self):
        for who in (RECEPTION, DOCTOR, Ident("nurse", "N1"), Ident("billing", "B1"), Ident("public", "anon")):
            self.assertEqual(self.code(self.svc.create_doctor, who, DOC), Codes.FORBIDDEN)
        self.assertEqual(self.repo.doctors, {})
        denied = [r for r in self.audit.rows if r["outcome"] == "denied"]
        self.assertEqual(len(denied), 5)
        self.assertEqual(denied[0]["error_code"], Codes.FORBIDDEN)

    def test_update_records_before_and_after(self):
        self.seed_doctor()
        self.commit(self.svc.update_doctor_profile, ADMIN, "D100", {"consultation_fee_inr": 1500, "room": "B-1"})
        row = [r for r in self.audit.rows if r["tool"] == "update_doctor_profile" and r["outcome"] == "ok"][0]
        self.assertEqual(row["before"], {"consultation_fee_minor": 120000, "room": "A-201"})
        self.assertEqual(row["after"], {"consultation_fee_minor": 150000, "room": "B-1"})
        self.assertEqual(self.repo.doctors["D100"]["consultation_fee_minor"], 150000)

    def test_fee_note_in_preview(self):
        self.seed_doctor()
        p = self.svc.update_doctor_profile(ADMIN, "D100", {"consultation_fee_inr": 1500})
        self.assertIn("future bookings", p["preview"]["note"])


class ValidationTests(Base):
    def test_create_doctor_rules(self):
        cases = [
            ({**DOC, "department_id": "NOPE"}, Codes.NOT_FOUND),
            ({**DOC, "staff_id": "bad id!"}, Codes.INVALID_INPUT),
            ({**DOC, "consultation_fee_inr": -5}, Codes.INVALID_INPUT),
            ({**DOC, "consultation_fee_inr": "abc"}, Codes.INVALID_INPUT),
            ({k: v for k, v in DOC.items() if k != "room"}, Codes.INVALID_INPUT),
            ({**DOC, "experience_years": True}, Codes.INVALID_INPUT),
        ]
        for args, code in cases:
            self.assertEqual(self.code(self.svc.create_doctor, ADMIN, args), code, args)

    def test_duplicate_staff_id_even_if_inactive(self):
        self.seed_doctor()
        self.assertEqual(self.code(self.svc.create_doctor, ADMIN, DOC), Codes.DUPLICATE)
        self.commit(self.svc.deactivate_doctor, ADMIN, "D100")
        self.assertEqual(self.code(self.svc.create_doctor, ADMIN, DOC), Codes.DUPLICATE)

    def test_schedule_validation(self):
        for bad in ({}, {"funday": [["09:00", "10:00"]]}, {"mon": [["10:00", "09:00"]]},
                    {"mon": [["09:07", "10:00"]]}, {"mon": [["09:00", "11:00"], ["10:30", "12:00"]]},
                    {"mon": [["9:00", "10:00"]]}, {"mon": []}, {"mon": [["09:00"]]}):
            with self.assertRaises(HospitalError, msg=str(bad)):
                validate_schedule(bad)
        ok = validate_schedule({"mon": [["14:00", "16:00"], ["09:00", "13:00"]]})
        self.assertEqual(ok["mon"], [["09:00", "13:00"], ["14:00", "16:00"]])

    def test_schedule_covers(self):
        mon = datetime(2026, 11, 16, 9, 0)  # Monday
        self.assertTrue(schedule_covers(SCHED, mon, mon + timedelta(minutes=30)))
        self.assertFalse(schedule_covers(SCHED, mon + timedelta(hours=3, minutes=45), mon + timedelta(hours=4, minutes=15)))
        self.assertFalse(schedule_covers(SCHED, mon + timedelta(days=1), mon + timedelta(days=1, minutes=30)))  # Tuesday


class ScheduleAndLeaveTests(Base):
    def setUp(self):
        super().setUp()
        self.seed_doctor()
        mon, wed = datetime(2026, 11, 23, 10, 0), datetime(2026, 11, 25, 10, 0)
        self.repo.add_appt("D100", mon)
        self.repo.add_appt("D100", wed)

    def test_schedule_change_that_orphans_bookings_is_blocked_not_cancelled(self):
        with self.assertRaises(HospitalError) as cm:
            self.svc.update_doctor_schedule(ADMIN, "D100", {"mon": [["09:00", "13:00"]]})  # drops Wednesday
        e = cm.exception
        self.assertEqual(e.code, Codes.SCHEDULE_CONFLICT)
        self.assertEqual(len(e.data["appointments"]), 1)
        self.assertNotIn("mrn", e.data["appointments"][0])  # no patient identifiers for admins
        self.assertEqual(sum(a["status"] == "booked" for a in self.repo.appts), 2)

    def test_compatible_schedule_change_commits(self):
        new = {"mon": [["09:00", "14:00"]], "wed": [["09:00", "14:00"]], "fri": [["09:00", "12:00"]]}
        self.commit(self.svc.update_doctor_schedule, ADMIN, "D100", new)
        self.assertIn("fri", self.repo.doctors["D100"]["schedule"])

    def test_leave_lists_affected_appointments_but_cancels_nothing(self):
        p, c = self.commit(self.svc.add_schedule_exception, ADMIN, "D100", "2026-11-23", "2026-11-24", "Conference")
        self.assertEqual(len(p["preview"]["affected_appointments"]), 1)
        self.assertEqual(len(self.repo.leaves), 1)
        self.assertEqual(sum(a["status"] == "booked" for a in self.repo.appts), 2)

    def test_leave_validation(self):
        for s, e in (("2026-11-10", "2026-11-12"), ("2026-11-20", "2026-11-19"), ("2026-11-20", "2027-03-01"), ("x", "y")):
            self.assertEqual(self.code(self.svc.add_schedule_exception, ADMIN, "D100", s, e, "Leave"),
                             Codes.INVALID_INPUT, (s, e))

    def test_deactivate_blocked_with_future_appointments(self):
        self.assertEqual(self.code(self.svc.deactivate_doctor, ADMIN, "D100"), Codes.HAS_FUTURE_APPOINTMENTS)
        self.assertTrue(self.repo.doctors["D100"]["active"])

    def test_deactivate_ok_without_future_appointments(self):
        self.repo.appts.clear()
        self.commit(self.svc.deactivate_doctor, ADMIN, "D100")
        self.assertFalse(self.repo.doctors["D100"]["active"])
        self.assertIsNone(self.repo.get_doctor("D100"))  # hidden from search and booking

    def test_cancelled_appointments_do_not_block(self):
        for a in self.repo.appts:
            a["status"] = "cancelled"
        self.commit(self.svc.deactivate_doctor, ADMIN, "D100")

    def test_cannot_deactivate_department_head(self):
        self.repo.appts.clear()
        self.commit(self.svc.set_department_head, ADMIN, "CARD", "D100")
        self.assertEqual(self.code(self.svc.deactivate_doctor, ADMIN, "D100"), Codes.DEPARTMENT_HEAD)


class DepartmentTests(Base):
    def test_create_department_and_duplicate(self):
        self.commit(self.svc.create_department, ADMIN, "ORTH", "Orthopedics", "Block D")
        self.assertEqual(self.repo.departments["ORTH"]["name"], "Orthopedics")
        self.assertEqual(self.code(self.svc.create_department, ADMIN, "ORTH", "Again"), Codes.DUPLICATE)

    def test_head_must_be_active_doctor_in_that_department(self):
        self.seed_doctor()
        self.assertEqual(self.code(self.svc.set_department_head, ADMIN, "NEUR", "D100"), Codes.INVALID_INPUT)
        self.assertEqual(self.code(self.svc.set_department_head, ADMIN, "NEUR", "NOBODY"), Codes.NOT_FOUND)
        self.commit(self.svc.set_department_head, ADMIN, "CARD", "D100")
        self.assertEqual(self.repo.departments["CARD"]["head_staff_id"], "D100")


class StaffUserTests(Base):
    def test_create_and_duplicate(self):
        self.commit(self.svc.create_staff_user, ADMIN, "R001", "Rita@Example.com", "receptionist")
        self.assertEqual(self.repo.staff["R001"]["email"], "rita@example.com")
        self.assertEqual(self.code(self.svc.create_staff_user, ADMIN, "R001", "x@y.com", "receptionist"), Codes.DUPLICATE)

    def test_validation(self):
        for args in (("R2", "bad-email", "receptionist"), ("R2", "a@b.com", "wizard"),
                     ("R2", "a@b.com", "patient"), ("R2", "a@b.com", "public"), ("x y", "a@b.com", "nurse")):
            self.assertEqual(self.code(self.svc.create_staff_user, ADMIN, *args), Codes.INVALID_INPUT, args)

    def test_doctor_user_needs_real_doctor(self):
        self.assertEqual(self.code(self.svc.create_staff_user, ADMIN, "D100", "d@x.com", "doctor"), Codes.INVALID_INPUT)
        self.seed_doctor()
        self.commit(self.svc.create_staff_user, ADMIN, "D100", "d@x.com", "doctor")

    def test_last_admin_cannot_be_demoted_or_deactivated(self):
        self.assertEqual(self.code(self.svc.change_staff_role, ADMIN, "A001", "nurse"), Codes.LAST_ADMIN)
        self.assertEqual(self.code(self.svc.deactivate_staff_user, ADMIN, "A001"), Codes.LAST_ADMIN)
        self.assertEqual(self.repo.staff["A001"]["role"], "admin")

    def test_second_admin_allows_demotion(self):
        self.commit(self.svc.create_staff_user, ADMIN, "A002", "a2@x.com", "admin")
        self.commit(self.svc.change_staff_role, ADMIN, "A001", "nurse")
        self.assertEqual(self.repo.staff["A001"]["role"], "nurse")

    def test_deactivation_revokes_tokens_immediately(self):
        self.commit(self.svc.create_staff_user, ADMIN, "R001", "r@x.com", "receptionist")
        self.commit(self.svc.deactivate_staff_user, ADMIN, "R001")
        self.assertFalse(self.repo.staff["R001"]["active"])
        self.assertIn("R001", self.repo.revoked)

    def test_role_change_revokes_tokens(self):
        self.commit(self.svc.create_staff_user, ADMIN, "R001", "r@x.com", "receptionist")
        self.commit(self.svc.change_staff_role, ADMIN, "R001", "billing")
        self.assertIn("R001", self.repo.revoked)


if __name__ == "__main__":
    unittest.main()
