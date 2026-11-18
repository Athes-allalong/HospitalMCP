"""Authorization matrix test generated from the matrix itself (plan s.12): for EVERY tool and EVERY role,
a role that is not in the matrix row is refused before any service code runs, and the role-filtered
tool list matches the matrix exactly."""
import inspect
import typing

import pytest

from hospital_mcp.authz import MATRIX, ROLES, tools_for_role
from hospital_mcp.errors import Codes, HospitalError
from hospital_mcp.identity import Identity
from hospital_mcp.services.context import Ctx
from hospital_mcp.tools import REGISTRY, use_ctx


def dummy_args(fn) -> dict:
    out = {}
    for p in inspect.signature(fn).parameters.values():
        if p.default is not inspect.Parameter.empty:
            continue
        ann = p.annotation
        origin = typing.get_origin(ann)
        if ann in (int, float):
            out[p.name] = 1
        elif ann is bool:
            out[p.name] = True
        elif origin in (dict, typing.Dict) or ann is dict:
            out[p.name] = {}
        elif origin in (list, typing.List):
            out[p.name] = []
        elif origin is typing.Literal:
            out[p.name] = typing.get_args(ann)[0]
        else:
            out[p.name] = "X-1"
    return out


CASES = [(tool, role) for tool in sorted(MATRIX) for role in ROLES if role not in MATRIX[tool]]


@pytest.mark.parametrize("tool,role", CASES)
def test_denied_cells_are_forbidden(db, tool, role):
    hid = None if role == "platform_admin" else "H001"
    c = Ctx(db, Identity(hospital_id=hid, role=role, subject="TEST-1", department_id="CARD"))
    with use_ctx(c), pytest.raises(HospitalError) as e:
        REGISTRY[tool].call(**dummy_args(REGISTRY[tool].fn))
    assert e.value.code == Codes.FORBIDDEN
    assert db.audit_log.find_one({"tool": tool, "outcome": "denied", "role": role})  # denials are audited


@pytest.mark.parametrize("role", ROLES)
def test_tool_list_matches_matrix(role):
    assert set(tools_for_role(role)) == {t for t, rules in MATRIX.items() if role in rules}


def test_every_tool_has_a_row_and_vice_versa():
    assert set(REGISTRY) == set(MATRIX)


def test_admin_has_no_clinical_access():
    for t in ("get_patient_history", "list_prescriptions", "get_lab_results", "add_clinical_note"):
        assert "admin" not in MATRIX[t]


def test_public_tools_are_read_only_directory_and_summaries():
    assert set(tools_for_role("public")) == {
        "get_hospital_info", "search_doctors", "get_doctor_profile", "check_availability", "list_departments",
        "get_er_status", "get_icu_occupancy", "get_blood_inventory", "get_department_status",
        "check_insurance_coverage"}
    assert all(REGISTRY[t].kind == "read" for t in tools_for_role("public"))
