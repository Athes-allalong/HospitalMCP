"""authorize(): role check from the matrix, then the record-level check. Fails closed."""
from dataclasses import dataclass
from typing import List, Optional, Protocol

from ..errors import Codes, HospitalError
from .matrix import MATRIX, ROLES


@dataclass(frozen=True)
class Decision:
    tool: str
    role: str
    view: str
    restrict_doctor_id: Optional[str] = None      # list calls: only this doctor's own / treated records
    restrict_department_id: Optional[str] = None  # list calls: only this nurse's department


class RecordContext(Protocol):
    def doctor_treats(self, staff_id: str, mrn: str) -> bool: ...
    def nurse_serves_patient(self, department_id: Optional[str], mrn: str) -> bool: ...
    def appointment_in_department(self, department_id: Optional[str], appointment: dict) -> bool: ...


def _forbid(role: str, tool: str, why: str = "") -> HospitalError:
    msg = f"Role '{role}' is not permitted to use {tool}."
    return HospitalError(Codes.FORBIDDEN, f"{msg} {why}".strip(),
                         hint="Explain this to the user. Do not retry with another tool to work around it.")


def authorize(ident, tool: str, *, mrn: Optional[str] = None, doctor_id: Optional[str] = None,
              appointment: Optional[dict] = None, ctx: Optional[RecordContext] = None,
              listing: bool = False) -> Decision:
    """Raise HospitalError(FORBIDDEN) unless `ident` may call `tool` on this record.

    mrn          patient the call is about ('treating' / 'department' scopes)
    doctor_id    doctor the record belongs to ('own' scope)
    appointment  the appointment the call is about (nurse department scope on appointments)
    listing      True for list/search calls: instead of a yes/no on one record, the Decision says
                 how to restrict the results (restrict_doctor_id / restrict_department_id)
    """
    role = getattr(ident, "role", None)
    subject = getattr(ident, "subject", None)
    dept = getattr(ident, "department_id", None)
    if role not in ROLES:
        raise _forbid(str(role), tool)
    rule = MATRIX.get(tool, {}).get(role)
    if rule is None:
        raise _forbid(role, tool)

    if listing:
        if rule.scope in ("own", "treating"):
            return Decision(tool, role, rule.view, restrict_doctor_id=subject)
        if rule.scope == "department":
            if not dept:
                raise _forbid(role, tool, "Your account has no department assigned.")
            return Decision(tool, role, rule.view, restrict_department_id=dept)
        return Decision(tool, role, rule.view)

    if rule.scope == "own":
        if doctor_id is None or doctor_id != subject:
            raise _forbid(role, tool, "You can only access your own appointments.")
    elif rule.scope == "treating":
        if not mrn or ctx is None or not ctx.doctor_treats(subject, mrn):
            raise _forbid(role, tool, "No care relationship with this patient.")
    elif rule.scope == "department":
        if appointment is not None:
            ok = ctx is not None and ctx.appointment_in_department(dept, appointment)
        else:
            ok = bool(mrn) and ctx is not None and ctx.nurse_serves_patient(dept, mrn)
        if not ok:
            raise _forbid(role, tool, "This patient is outside your department.")
    return Decision(tool, role, rule.view)


def tools_for_role(role: str) -> List[str]:
    """Tools this role may see in tools/list. A convenience only: authorize() still runs on every call."""
    return sorted(t for t, rules in MATRIX.items() if role in rules)
