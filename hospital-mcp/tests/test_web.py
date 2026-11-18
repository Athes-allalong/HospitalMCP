"""Web app: every page renders for every role, forms go through preview -> confirm, CSRF is enforced,
pages a role may not use are refused, and the public chatbot works without a login."""
import json
import re
from dataclasses import replace

import pytest

from hospital_mcp import agent
from tests.conftest import ADMIN, ANANYA, BILLING, NURSE, PLATFORM, RECEPTION, SUN_RECEPTION
from web.app import create_app

ROLES = {RECEPTION: "receptionist", ADMIN: "admin", ANANYA: "doctor", NURSE: "nurse", BILLING: "billing"}


@pytest.fixture
def app(db, settings, clock):
    a = create_app(db=db, settings=replace(settings, groq_api_key="test-key"), clock=clock)
    a.config["TESTING"] = True
    return a


@pytest.fixture
def client(app):
    return app.test_client()


def login(client, email):
    client.get("/login")
    with client.session_transaction() as s:
        csrf = s["csrf"]
    r = client.post("/login/dev", data={"email": email, "csrf": csrf})
    assert r.status_code == 302, r.data
    client.get("/")  # the session is regenerated at sign-in, so fetch the new CSRF token
    return csrf_of(client)


def csrf_of(client):
    with client.session_transaction() as s:
        return s["csrf"]


def test_anonymous_is_sent_to_sign_in(client):
    assert client.get("/patients").status_code == 302
    assert client.get("/").headers["Location"].endswith("/signed-out")
    assert client.get("/signed-out").status_code == 200


@pytest.mark.parametrize("email", list(ROLES))
def test_every_page_renders_for_every_role(client, email):
    login(client, email)
    role = ROLES[email]
    for path in ("/", "/doctors", "/doctors/DOC-CARD-001", "/departments", "/assistant"):
        assert client.get(path).status_code == 200, path
    expect = {"/patients": role != "public", "/appointments": role != "billing",
              "/admin": role == "admin", "/facilities": role != "billing"}
    for path, allowed in expect.items():
        code = client.get(path).status_code
        assert (code == 200) == allowed, (path, role, code)
    assert client.get("/patients/MRN-1001").status_code in (200, 302)


def test_role_aware_patient_page(client):
    login(client, ADMIN)
    page = client.get("/patients/MRN-1001").get_data(as_text=True)
    assert "masked" in page and "Clinical records are not available" in page
    client.get("/logout")
    login(client, ANANYA)
    page = client.get("/patients/MRN-1001").get_data(as_text=True)
    assert "Visit history" in page


def test_booking_form_needs_confirmation(client, db):
    csrf = login(client, RECEPTION)
    r = client.post("/actions/book_appointment", data={"csrf": csrf, "doctor_id": "DOC-CARD-001", "type": "new",
                                                       "mrn": "MRN-1003", "slot_start": "2026-11-17T15:00:00"})
    assert r.status_code == 200 and b"Please confirm" in r.data
    assert not db.appointments.find_one({"mrn": "MRN-1003", "slot_start": {"$exists": True}, "hospital_id": "H001",
                                         "doctor_id": "DOC-CARD-001", "status": "booked", "reason": ""})
    pid = re.search(rb"/actions/confirm/([0-9a-f]+)", r.data).group(1).decode()
    r = client.post(f"/actions/confirm/{pid}", data={"csrf": csrf, "decision": "confirm"})
    assert r.status_code == 302
    from datetime import datetime
    assert db.appointments.find_one({"hospital_id": "H001", "mrn": "MRN-1003", "doctor_id": "DOC-CARD-001",
                                     "slot_start": datetime(2026, 11, 17, 15), "status": "booked"})
    # the same confirmation cannot be replayed
    r = client.post(f"/actions/confirm/{pid}", data={"csrf": csrf, "decision": "confirm"}, follow_redirects=True)
    assert b"expired" in r.data


def test_csrf_is_required(client):
    login(client, RECEPTION)
    r = client.post("/actions/book_appointment", data={"doctor_id": "DOC-CARD-001", "mrn": "MRN-1001",
                                                       "slot_start": "2026-11-17T15:00:00"})
    assert r.status_code == 400


def test_forms_cannot_reach_tools_the_role_lacks(client):
    csrf = login(client, RECEPTION)
    assert client.post("/actions/create_staff_user", data={"csrf": csrf, "subject": "X1", "email": "x@x.dev",
                                                           "role": "admin"}).status_code == 403


def test_admin_adds_staff_user_through_preview(client, db):
    csrf = login(client, ADMIN)
    r = client.post("/actions/create_staff_user", data={"csrf": csrf, "subject": "REC-002", "name": "New Desk",
                                                        "email": "desk2@dummyhospital.dev", "role": "receptionist"})
    pid = re.search(rb"/actions/confirm/([0-9a-f]+)", r.data).group(1).decode()
    client.post(f"/actions/confirm/{pid}", data={"csrf": csrf, "decision": "confirm"})
    assert db.staff_users.find_one({"email": "desk2@dummyhospital.dev"})["hospital_id"] == "H001"


def test_platform_console(client):
    login(client, PLATFORM)
    assert client.get("/").headers["Location"].endswith("/platform")
    page = client.get("/platform").get_data(as_text=True)
    assert "Dummy Hospital" in page and "Sunrise Clinic" in page
    assert client.get("/patients").status_code == 403


def test_tenant_isolation_in_pages(client):
    login(client, SUN_RECEPTION)
    assert b"Ananya" not in client.get("/doctors").data
    assert client.get("/doctors/DOC-CARD-001", follow_redirects=True).status_code == 200  # flashes not found


def test_public_page_and_chatbot(client, monkeypatch):
    page = client.get("/h/dummy-hospital")
    assert page.status_code == 200 and b"Cardiology" in page.data
    assert client.get("/h/no-such-place").status_code == 404
    seen = []

    def fake_groq(key, model, messages, tools, timeout=60):
        seen.append([t["function"]["name"] for t in tools])
        if len(seen) == 1:
            return {"content": "", "tool_calls": [{"id": "1", "function": {"name": "get_icu_occupancy", "arguments": "{}"}}]}
        return {"content": "Yes, 4 ICU beds are available."}
    monkeypatch.setattr(agent, "call_groq", fake_groq)
    r = client.post("/api/public/dummy-hospital/chat", json={"message": "ICU beds?"},
                    headers={"X-CSRF-Token": csrf_of(client)})
    assert r.json["reply"].startswith("Yes")
    assert "get_lab_results" not in seen[0] and "book_appointment" not in seen[0]


def test_staff_assistant_card_flow(client, db, monkeypatch):
    csrf = login(client, RECEPTION)
    steps = iter([
        {"content": "", "tool_calls": [{"id": "1", "function": {"name": "book_appointment", "arguments": json.dumps(
            {"mrn": "MRN-1003", "doctor_id": "DOC-CARD-001", "slot_start": "2026-11-17T15:00:00"})}}]},
        {"content": "Please confirm the booking below."}])
    monkeypatch.setattr(agent, "call_groq", lambda *a, **k: next(steps))
    r = client.post("/api/chat", json={"message": "book it"}, headers={"X-CSRF-Token": csrf}).json
    assert r["card"]["title"] == "Book appointment" and "confirmation_id" not in json.dumps(r)
    done = client.post(f"/api/confirm/{r['card']['id']}", json={"decision": "confirm"},
                       headers={"X-CSRF-Token": csrf}).json
    assert done["ok"] and "reference APT-" in done["message"], done
