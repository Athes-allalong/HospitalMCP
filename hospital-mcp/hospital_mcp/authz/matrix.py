"""The permission matrix (plan section 4), as data. Deny by default: a (tool, role) pair that is not
listed is forbidden. tests/test_matrix.py walks every cell of it.

Rule.view   how much data the role gets (the service shapes its output with it)
Rule.scope  record-level check on top of the role check:
    own         doctor may only touch their own appointments (doctor_id == subject)
    treating    doctor must have a care relationship with the patient
    department  nurse must serve the patient (department scope)

Hospital isolation is NOT in this table: it is enforced underneath every tool by tenancy.TenantDB.
'patient' is intentionally absent (patients are data only in this phase). Adding patient
self-service later is one more column with an own-records rule.
"""
from dataclasses import dataclass
from typing import Dict, Optional

STAFF_ROLES = ("receptionist", "nurse", "doctor", "billing", "admin")
ROLES = ("public",) + STAFF_ROLES + ("platform_admin",)


@dataclass(frozen=True)
class Rule:
    view: str = "full"
    scope: Optional[str] = None


ALLOW = Rule()


def _all(*roles, rule: Rule = ALLOW) -> Dict[str, Rule]:
    return {r: rule for r in roles}


EVERYONE = ("public",) + STAFF_ROLES
OPS_STAFF = ("receptionist", "nurse", "doctor", "admin")  # not billing, not public

MATRIX: Dict[str, Dict[str, Rule]] = {
    # ---- directory (everyone, including the anonymous public chatbot) ----
    "get_hospital_info": _all(*EVERYONE),
    "search_doctors": _all(*EVERYONE),
    "get_doctor_profile": _all(*EVERYONE),
    "check_availability": _all(*EVERYONE),
    "list_departments": _all(*EVERYONE),
    # ---- facility status (public gets a summary only) ----
    "get_er_status": {**_all(*OPS_STAFF), "public": Rule("summary")},
    "get_icu_occupancy": {**_all(*OPS_STAFF), "public": Rule("summary")},
    "get_blood_inventory": {**_all(*OPS_STAFF), "public": Rule("summary")},
    "get_department_status": {**_all(*OPS_STAFF), "public": Rule("summary")},
    "get_ot_schedule": _all("nurse", "doctor", "admin"),
    "update_facility_status": _all("nurse", "admin"),
    # ---- patient records ----
    "search_patients": {
        "receptionist": Rule("demographic"),
        "nurse": Rule("full", "department"),
        "doctor": Rule("full", "treating"),
        "billing": Rule("masked"),
        "admin": Rule("masked"),
    },
    "register_patient": _all("receptionist", "admin"),
    "get_patient_summary": {
        "receptionist": Rule("demographic"),
        "nurse": Rule("full", "department"),
        "doctor": Rule("full", "treating"),
        "billing": Rule("masked"),
        "admin": Rule("masked"),
    },
    "get_admission_status": {
        "receptionist": ALLOW,
        "nurse": Rule("full", "department"),
        "doctor": Rule("full", "treating"),
    },
    "get_patient_history": {"nurse": Rule("full", "department"), "doctor": Rule("full", "treating")},
    "list_prescriptions": {"nurse": Rule("full", "department"), "doctor": Rule("full", "treating")},
    "get_lab_results": {"nurse": Rule("full", "department"), "doctor": Rule("full", "treating")},
    "add_clinical_note": {"doctor": Rule("full", "own")},
    "update_patient_contact": _all("receptionist", "admin"),
    # ---- appointments ----
    "list_appointments": {
        "receptionist": ALLOW, "nurse": Rule("full", "department"), "doctor": Rule("full", "own"), "admin": ALLOW,
    },
    "get_appointment": {
        "receptionist": ALLOW, "nurse": Rule("full", "department"), "doctor": Rule("full", "own"), "admin": ALLOW,
    },
    "get_appointment_history": {
        "receptionist": ALLOW, "nurse": Rule("full", "department"), "doctor": Rule("full", "own"), "admin": ALLOW,
    },
    "book_appointment": {"receptionist": ALLOW, "doctor": Rule("full", "own"), "admin": ALLOW},
    "reschedule_appointment": {"receptionist": ALLOW, "doctor": Rule("full", "own"), "admin": ALLOW},
    "cancel_appointment": {"receptionist": ALLOW, "doctor": Rule("full", "own"), "admin": ALLOW},
    # ---- billing and insurance ----
    "get_invoice": {"receptionist": Rule("status"), "billing": ALLOW, "admin": ALLOW},
    "get_claim_status": _all("billing", "admin"),
    "create_invoice": _all("billing"),
    "record_payment": _all("billing"),
    "submit_preauthorization": _all("doctor", "billing"),
    "check_insurance_coverage": {
        "public": Rule("accepted_only"), "receptionist": ALLOW, "billing": ALLOW,
    },
    # ---- admin: directory and people management (one hospital) ----
    "create_doctor": _all("admin"),
    "update_doctor_profile": _all("admin"),
    "update_doctor_schedule": _all("admin"),
    "add_schedule_exception": _all("admin"),
    "deactivate_doctor": _all("admin"),
    "create_department": _all("admin"),
    "update_department": _all("admin"),
    "set_department_head": _all("admin"),
    "list_staff_users": _all("admin"),
    "create_staff_user": _all("admin"),
    "change_staff_role": _all("admin"),
    "deactivate_staff_user": _all("admin"),
    "get_audit_log": _all("admin"),
    # ---- platform (multi-tenancy): onboarding hospitals ----
    "list_hospitals": _all("platform_admin"),
    "create_hospital": _all("platform_admin"),
    "set_hospital_active": _all("platform_admin"),
}

# Admins manage the system; they get no clinical record access (least privilege).
CLINICAL_TOOLS = ("get_patient_history", "list_prescriptions", "get_lab_results", "add_clinical_note")
ADMIN_ONLY_TOOLS = tuple(t for t, r in MATRIX.items() if set(r) == {"admin"})
PLATFORM_TOOLS = tuple(t for t, r in MATRIX.items() if set(r) == {"platform_admin"})
