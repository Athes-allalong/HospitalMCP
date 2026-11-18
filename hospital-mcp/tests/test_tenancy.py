"""Multi-tenancy: two hospitals share the database but never each other's data.
Both seeds use the same MRNs (MRN-1001...) and the same staff codes (ADM-001, REC-001) on purpose."""
import pytest

from hospital_mcp.errors import Codes, HospitalError
from hospital_mcp.identity import resolve_identity
from hospital_mcp.tenancy import TenantDB
from tests.conftest import (ADMIN, PLATFORM, RECEPTION, SUN_ADMIN, SUN_RECEPTION, call, code_of, two_step)


def test_same_mrn_resolves_to_each_hospitals_own_patient(db, as_user):
    a = call(as_user(RECEPTION), "get_patient_summary", mrn="MRN-1001")
    b = call(as_user(SUN_RECEPTION), "get_patient_summary", mrn="MRN-1001")
    assert a["name"] == db.patients.find_one({"hospital_id": "H001", "mrn": "MRN-1001"})["name"]
    assert b["name"] == db.patients.find_one({"hospital_id": "H002", "mrn": "MRN-1001"})["name"]
    assert "hospital_id" not in a  # internal field never leaves the server


def test_other_hospitals_doctors_and_data_are_invisible(as_user):
    sun = as_user(SUN_RECEPTION)
    names = [d["name"] for d in call(sun, "search_doctors")["doctors"]]
    assert "Dr. Ananya Sharma" not in names and len(names) == 3
    assert code_of(call, sun, "get_doctor_profile", staff_id="DOC-CARD-001") == Codes.NOT_FOUND
    assert code_of(call, sun, "book_appointment", mrn="MRN-1001", doctor_id="DOC-CARD-001",
                   slot_start="2026-11-17T15:00:00") == Codes.NOT_FOUND
    assert code_of(call, sun, "get_patient_summary", mrn="MRN-1025") == Codes.NOT_FOUND  # exists only at H001


def test_tenant_db_refuses_cross_hospital_queries_and_writes(db):
    t = TenantDB(db, "H002")
    with pytest.raises(HospitalError):
        list(t.patients.find({"hospital_id": "H001"}))
    with pytest.raises(HospitalError):
        t.patients.insert_one({"mrn": "X", "hospital_id": "H001"})
    with pytest.raises(HospitalError):
        t.patients.update_one({"mrn": "MRN-1001"}, {"$set": {"hospital_id": "H001"}})
    with pytest.raises(HospitalError):
        TenantDB(db, None).patients.find_one({})


def test_confirmation_cannot_cross_hospitals_even_with_same_staff_code(as_user):
    h1, h2 = as_user(ADMIN), as_user(SUN_ADMIN)
    assert h1.ident.subject == h2.ident.subject == "ADM-001"
    args = dict(department_id="ONCO", name="Oncology", location="Block G")
    cid = call(h1, "create_department", **args)["confirmation_id"]
    assert code_of(call, h2, "create_department", **args, confirmation_id=cid) == Codes.CONFIRMATION_INVALID


def test_admin_tools_only_see_own_hospital(as_user):
    h1 = as_user(ADMIN)
    call(as_user(SUN_RECEPTION), "list_departments")
    emails = [u["email"] for u in call(h1, "list_staff_users")["staff_users"]]
    assert all(e.endswith("dummyhospital.dev") for e in emails)
    log = call(h1, "get_audit_log", limit=200)["entries"]
    assert log and all(e.get("actor_email", "").endswith("dummyhospital.dev") for e in log)


def test_hospital_admin_cannot_use_platform_tools(as_user):
    assert code_of(call, as_user(ADMIN), "create_hospital", name="X Hospital", slug="x-hosp",
                   admin_email="a@x.dev", admin_name="A") == Codes.FORBIDDEN


def test_platform_admin_onboards_a_hospital_whose_admin_can_sign_in(db, as_user, settings):
    plat = as_user(PLATFORM)
    res = two_step(plat, "create_hospital", name="Green Valley Hospital", slug="green-valley",
                   admin_email="admin@greenvalley.dev", admin_name="Asha K")
    hid = res["target"]
    new_admin = as_user("admin@greenvalley.dev")
    assert new_admin.ident.hospital_id == hid and new_admin.ident.role == "admin"
    assert call(new_admin, "search_doctors")["total"] == 0          # starts empty, nothing leaks in
    assert any(h["slug"] == "green-valley" for h in call(plat, "list_hospitals")["hospitals"])
    # platform admin has no hospital: patient tools are denied to it
    assert code_of(call, plat, "search_patients", query="MRN") == Codes.FORBIDDEN


def test_suspended_hospital_locks_out_its_staff(db, as_user, settings):
    two_step(as_user(PLATFORM), "set_hospital_active", hospital_id="H002", active=False)
    with pytest.raises(HospitalError) as e:
        resolve_identity(db, {"sub": "dev|x", settings.email_claim: SUN_RECEPTION}, settings)
    assert e.value.code == Codes.UNAUTHENTICATED
    as_user(RECEPTION)  # the other hospital is unaffected
