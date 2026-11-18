"""Appointment flow: preview-then-commit, idempotency, double booking, concurrency (plan s.7.4, s.8, s.12)."""
import threading
from datetime import timedelta

from hospital_mcp.errors import Codes, HospitalError
from tests.conftest import ANANYA, RECEPTION, WEBB, call, code_of, two_step

SLOT = "2026-11-17T15:00:00+05:30"   # tomorrow 3 PM, Dr. Sharma (scenario 1)


def test_scenario_1_preview_then_commit(db, as_user):
    rec = as_user(RECEPTION)
    found = call(rec, "search_doctors", name="Sharma")["doctors"]
    assert found[0]["staff_id"] == "DOC-CARD-001"
    assert "2026-11-17T15:00:00" in call(rec, "check_availability", doctor_id="DOC-CARD-001",
                                         start_date="2026-11-17", end_date="2026-11-17")["slots"]
    p = call(rec, "book_appointment", mrn="MRN-1001", doctor_id="DOC-CARD-001", slot_start=SLOT)
    assert p["status"] == "preview" and p["preview"]["doctor"] == "Dr. Ananya Sharma"
    assert db.appointments.count_documents({"doctor_id": "DOC-CARD-001", "slot_start": p_dt(),
                                            "status": "booked"}) == 0  # a preview writes nothing
    done = call(rec, "book_appointment", mrn="MRN-1001", doctor_id="DOC-CARD-001", slot_start=SLOT,
                confirmation_id=p["confirmation_id"])
    assert done["status"] == "committed"
    appt_id = done["result"]["appointment_id"]
    assert db.appointments.find_one({"appointment_id": appt_id})["status"] == "booked"
    outcomes = [e["outcome"] for e in db.audit_log.find({"tool": "book_appointment", "target": done["target"]})]
    assert outcomes == ["previewed", "ok"]


def p_dt():
    from datetime import datetime
    return datetime(2026, 11, 17, 15, 0)


def test_confirmation_is_bound_to_args_caller_and_single_use(as_user, clock):
    rec, admin = as_user(RECEPTION), as_user("admin@dummyhospital.dev")
    args = dict(mrn="MRN-1001", doctor_id="DOC-CARD-001", slot_start=SLOT)
    cid = call(rec, "book_appointment", **args)["confirmation_id"]
    # changed arguments
    assert code_of(call, rec, "book_appointment", **{**args, "mrn": "MRN-1002"},
                   confirmation_id=cid) == Codes.CONFIRMATION_INVALID
    # used up even though it failed
    assert code_of(call, rec, "book_appointment", **args, confirmation_id=cid) == Codes.CONFIRMATION_INVALID
    # different caller
    cid = call(rec, "book_appointment", **args)["confirmation_id"]
    assert code_of(call, admin, "book_appointment", **args, confirmation_id=cid) == Codes.CONFIRMATION_INVALID
    # expired
    cid = call(rec, "book_appointment", **args)["confirmation_id"]
    clock.now += timedelta(minutes=6)
    assert code_of(call, rec, "book_appointment", **args, confirmation_id=cid) == Codes.CONFIRMATION_EXPIRED


def test_request_id_makes_retried_commit_safe(db, as_user):
    rec = as_user(RECEPTION)
    args = dict(mrn="MRN-1001", doctor_id="DOC-CARD-001", slot_start=SLOT, request_id="req-42")
    cid = call(rec, "book_appointment", **args)["confirmation_id"]
    first = call(rec, "book_appointment", **args, confirmation_id=cid)
    again = call(rec, "book_appointment", **args, confirmation_id=cid)   # network retry of the same commit
    assert again["replayed"] is True and again["result"] == first["result"]
    assert db.appointments.count_documents({"appointment_id": first["result"]["appointment_id"]}) == 1


def test_scenario_2_fifty_parallel_bookings_one_wins(db, as_user):
    rec = as_user(RECEPTION)
    args = dict(doctor_id="DOC-CARD-001", slot_start=SLOT)
    free = []
    for i in range(30):
        try:
            call(rec, "book_appointment", mrn=f"MRN-{1001 + i}", **args)
            free.append(f"MRN-{1001 + i}")
        except HospitalError:
            pass   # that patient is busy at 3 PM tomorrow
    assert len(free) >= 20
    mrns = [free[i % len(free)] for i in range(50)]
    previews = [call(rec, "book_appointment", mrn=m, **args)["confirmation_id"] for m in mrns]
    barrier, results = threading.Barrier(50), []

    def worker(m, cid):
        barrier.wait()
        try:
            call(rec, "book_appointment", mrn=m, **args, confirmation_id=cid)
            results.append("ok")
        except HospitalError as e:
            results.append(e.code)

    threads = [threading.Thread(target=worker, args=(m, c)) for m, c in zip(mrns, previews)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 1
    assert set(results) - {"ok"} <= {Codes.SLOT_UNAVAILABLE}
    assert db.appointments.count_documents({"hospital_id": "H001", "doctor_id": "DOC-CARD-001",
                                            "slot_start": p_dt(), "status": "booked"}) == 1


def test_same_patient_same_time_two_doctors(as_user):
    rec = as_user(RECEPTION)
    two_step(rec, "book_appointment", mrn="MRN-1010", doctor_id="DOC-CARD-001", slot_start=SLOT)
    err = code_of(two_step, rec, "book_appointment", mrn="MRN-1010", doctor_id="DOC-CARD-002",
                  slot_start="2026-11-17T15:15:00")
    assert err in (Codes.SLOT_UNAVAILABLE, Codes.INVALID_INPUT)


def test_partial_overlap_is_blocked_and_alternatives_offered(as_user):
    rec = as_user(RECEPTION)
    two_step(rec, "book_appointment", mrn="MRN-1010", doctor_id="DOC-CARD-001", slot_start=SLOT)  # 15:00-15:30
    try:
        call(rec, "book_appointment", mrn="MRN-1011", doctor_id="DOC-CARD-001",
             slot_start="2026-11-17T15:15:00", type="followup")
        raise AssertionError("overlap accepted")
    except HospitalError as e:
        assert e.code == Codes.SLOT_UNAVAILABLE and e.data["alternatives"]


def test_cancel_frees_the_slot_and_reschedule_moves_it(db, as_user):
    rec = as_user(RECEPTION)
    appt = two_step(rec, "book_appointment", mrn="MRN-1010", doctor_id="DOC-CARD-001", slot_start=SLOT)["result"]["appointment_id"]
    free = [s for s in call(rec, "check_availability", doctor_id="DOC-CARD-001", start_date="2026-11-18",
                            end_date="2026-11-18")["slots"]]
    two_step(rec, "reschedule_appointment", appointment_id=appt, new_slot_start=free[0])
    assert db.appointments.find_one({"appointment_id": appt})["slot_start"].isoformat() == free[0]
    # old slot is free again for someone else
    two_step(rec, "book_appointment", mrn="MRN-1011", doctor_id="DOC-CARD-001", slot_start=SLOT)
    two_step(rec, "cancel_appointment", appointment_id=appt, reason="Patient travelling")
    two_step(rec, "book_appointment", mrn="MRN-1012", doctor_id="DOC-CARD-001", slot_start=free[0])


def test_doctor_may_only_book_own_appointments(as_user):
    doc = as_user(ANANYA)
    assert two_step(doc, "book_appointment", mrn="MRN-1001", doctor_id="DOC-CARD-001", slot_start=SLOT)["status"] == "committed"
    assert code_of(call, doc, "book_appointment", mrn="MRN-1001", doctor_id="DOC-CARD-002",
                   slot_start=SLOT) == Codes.FORBIDDEN


def test_rejections_have_specific_codes(as_user):
    rec = as_user(RECEPTION)
    webb_leave = "2026-11-26T10:00:00"   # Dr. Webb's leave: today+10 .. today+12
    assert code_of(call, rec, "book_appointment", mrn="MRN-1001", doctor_id="DOC-NEUR-002",
                   slot_start=webb_leave) == Codes.DOCTOR_ON_LEAVE
    assert code_of(call, rec, "book_appointment", mrn="MRN-1001", doctor_id="DOC-CARD-001",
                   slot_start="2026-11-15T15:00:00") == Codes.INVALID_INPUT          # past
    assert code_of(call, rec, "book_appointment", mrn="MRN-9999", doctor_id="DOC-CARD-001",
                   slot_start=SLOT) == Codes.NOT_FOUND
    assert code_of(call, rec, "book_appointment", mrn="MRN-1001", doctor_id="DOC-CARD-001",
                   slot_start="2026-12-25T15:00:00") == Codes.SLOT_UNAVAILABLE       # holiday


def test_late_cancellation_is_flagged(db, as_user, clock):
    rec = as_user(RECEPTION)
    appt = two_step(rec, "book_appointment", mrn="MRN-1010", doctor_id="DOC-CARD-001", slot_start=SLOT)["result"]["appointment_id"]
    from datetime import datetime
    clock.now = datetime(2026, 11, 17, 14, 0)
    res = two_step(rec, "cancel_appointment", appointment_id=appt, reason="Unwell")
    assert res["result"]["late_cancellation"] is True


def test_rate_limit(as_user):
    from hospital_mcp.services.context import rate_limiter
    rate_limiter.limits["read"] = 3
    rec = as_user(RECEPTION)
    for _ in range(3):
        call(rec, "list_departments")
    assert code_of(call, rec, "list_departments") == Codes.RATE_LIMITED
