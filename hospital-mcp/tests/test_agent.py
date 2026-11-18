"""The in-app assistant with a scripted fake LLM: previews become confirmation cards, the model never
handles confirmation ids, and it only gets the tools of the signed-in role."""
import json

from hospital_mcp.agent import chat, confirm_pending, run_tool, tool_schemas
from hospital_mcp.identity import public_identity
from hospital_mcp.services.context import Ctx
from tests.conftest import ADMIN, RECEPTION


def scripted(*steps):
    """Fake LLM: returns the given messages in order and records what it was sent."""
    seen = []

    def llm(messages, tools):
        seen.append({"messages": json.loads(json.dumps(messages, default=str)), "tools": [t["function"]["name"] for t in tools]})
        return steps[min(len(seen) - 1, len(steps) - 1)]
    llm.seen = seen
    return llm


def tool_call(tool_name, **args):
    return {"content": "", "tool_calls": [{"id": "c1", "type": "function",
                                           "function": {"name": tool_name, "arguments": json.dumps(args)}}]}


def test_booking_through_the_assistant_needs_the_users_click(db, as_user):
    rec = as_user(RECEPTION)
    llm = scripted(tool_call("book_appointment", mrn="MRN-1001", doctor_id="DOC-CARD-001",
                             slot_start="2026-11-17T15:00:00"),
                   {"content": "Please confirm the booking below."})
    out = chat(rec, [], "Book Dr. Sharma tomorrow 3 PM for MRN-1001", api_key=None, model="m",
               hospital_name="Dummy Hospital", llm=llm)
    card = out["pending"]
    assert card and card["tool"] == "book_appointment" and card["preview"]["doctor"] == "Dr. Ananya Sharma"
    assert db.appointments.count_documents({"hospital_id": "H001", "status": "booked", "mrn": "MRN-1001",
                                            "doctor_id": "DOC-CARD-001"}) == 1  # only the seeded one so far
    sent = json.dumps(llm.seen)
    assert card["confirmation_id"] not in sent           # the model never saw the id
    res = confirm_pending(rec, card)
    assert res["status"] == "committed"
    again = confirm_pending(rec, card)                     # double click: replayed, not booked twice
    assert again.get("replayed") is True and again["result"] == res["result"]


def test_model_cannot_pass_confirmation_ids_or_use_other_roles_tools(as_user):
    rec = as_user(RECEPTION)
    assert run_tool(rec, "book_appointment", {"mrn": "MRN-1001", "doctor_id": "DOC-CARD-001",
                                              "slot_start": "2026-11-17T15:00:00",
                                              "confirmation_id": "CNF-guess"})["error"] == "INVALID_INPUT"
    assert run_tool(rec, "create_doctor", {})["error"] == "FORBIDDEN"
    names = {t["function"]["name"] for t in tool_schemas("receptionist")}
    assert "create_doctor" not in names and "book_appointment" in names
    params = next(t for t in tool_schemas("receptionist") if t["function"]["name"] == "book_appointment")
    assert "confirmation_id" not in params["function"]["parameters"]["properties"]


def test_public_chat_only_offers_public_tools(db):
    pub = Ctx(db, public_identity("H001"))
    llm = scripted(tool_call("get_icu_occupancy"), {"content": "There are 4 ICU beds available."})
    out = chat(pub, [], "Are ICU beds available?", api_key=None, model="m", hospital_name="Dummy Hospital", llm=llm)
    assert "4" in out["reply"] and out["tools_used"] == ["get_icu_occupancy"]
    assert "get_lab_results" not in llm.seen[0]["tools"]
    assert run_tool(pub, "get_lab_results", {"mrn": "MRN-1002"})["error"] == "FORBIDDEN"


def test_admin_change_also_goes_through_a_card(as_user):
    adm = as_user(ADMIN)
    llm = scripted(tool_call("create_department", department_id="ONCO", name="Oncology", location="Block G"),
                   {"content": "Confirm below."})
    out = chat(adm, [], "Add an oncology department", api_key=None, model="m", hospital_name="H", llm=llm)
    assert out["pending"]["tool"] == "create_department"
    assert confirm_pending(adm, out["pending"])["status"] == "committed"


def test_schemas_are_valid_for_every_role():
    for role in ("public", "receptionist", "nurse", "doctor", "billing", "admin", "platform_admin"):
        for t in tool_schemas(role):
            assert "$ref" not in json.dumps(t)
