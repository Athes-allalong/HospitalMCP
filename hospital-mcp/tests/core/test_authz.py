import itertools
import unittest

from hospital_mcp.authz import (ADMIN_ONLY_TOOLS, CLINICAL_TOOLS, MATRIX, ROLES, authorize,
                                 shape_patient_summary, tools_for_role)
from hospital_mcp.errors import Codes, HospitalError
from tests.core.fakes import Ident

# Every tool that exists in server.py today. If a tool is added there, add it to the matrix too.
SERVER_TOOLS = """search_doctors get_doctor_profile check_availability list_departments get_department_status
get_patient_summary get_patient_history get_appointment_history get_appointment list_prescriptions
get_admission_status get_lab_results get_icu_occupancy get_er_status get_ot_schedule get_blood_inventory
get_invoice get_claim_status check_insurance_coverage book_appointment reschedule_appointment cancel_appointment
add_clinical_note update_patient_contact update_facility_status create_invoice record_payment
submit_preauthorization""".split()


class Yes:
    def doctor_treats(self, *a): return True
    def nurse_serves_patient(self, *a): return True


class No:
    def doctor_treats(self, *a): return False
    def nurse_serves_patient(self, *a): return False


def call(role, tool, **kw):
    return authorize(Ident(role, "S1"), tool, **kw)


class MatrixTests(unittest.TestCase):
    def test_every_tool_x_role(self):
        """The matrix is the single source of truth: allowed iff listed, otherwise FORBIDDEN."""
        for tool, role in itertools.product(MATRIX, ROLES):
            ident = Ident(role, "S1")
            kw = dict(mrn="MRN-1", doctor_id="S1", ctx=Yes())
            if role in MATRIX[tool]:
                self.assertEqual(authorize(ident, tool, **kw).role, role, f"{role}/{tool}")
            else:
                with self.assertRaises(HospitalError, msg=f"{role}/{tool}") as cm:
                    authorize(ident, tool, **kw)
                self.assertEqual(cm.exception.code, Codes.FORBIDDEN)

    def test_all_server_tools_are_in_matrix(self):
        self.assertEqual([t for t in SERVER_TOOLS if t not in MATRIX], [])

    def test_deny_by_default(self):
        for bad in ("not_a_tool", ""):
            with self.assertRaises(HospitalError):
                call("admin", bad)
        for role in ("patient", "superuser", None, ""):
            with self.assertRaises(HospitalError):
                call(role, "search_doctors")

    def test_admin_has_no_clinical_access(self):
        for tool in CLINICAL_TOOLS:
            self.assertNotIn("admin", MATRIX[tool], tool)
        with self.assertRaises(HospitalError):
            call("admin", "get_patient_history", mrn="MRN-1", ctx=Yes())

    def test_admin_tools_are_admin_only(self):
        self.assertIn("create_doctor", ADMIN_ONLY_TOOLS)
        for tool in ADMIN_ONLY_TOOLS:
            for role in ROLES:
                if role != "admin":
                    with self.assertRaises(HospitalError, msg=f"{role}/{tool}"):
                        call(role, tool)

    def test_public_gets_summaries_only(self):
        self.assertEqual(call("public", "get_icu_occupancy").view, "summary")
        self.assertEqual(call("public", "check_insurance_coverage").view, "accepted_only")
        for tool in ("get_patient_summary", "get_lab_results", "book_appointment", "create_invoice"):
            with self.assertRaises(HospitalError):
                call("public", tool)

    def test_views(self):
        self.assertEqual(call("receptionist", "get_patient_summary").view, "demographic")
        self.assertEqual(call("billing", "get_patient_summary").view, "masked")
        self.assertEqual(call("receptionist", "get_invoice").view, "status")

    def test_doctor_treating_scope(self):
        with self.assertRaises(HospitalError):
            call("doctor", "get_patient_history", mrn="MRN-1", ctx=No())
        with self.assertRaises(HospitalError):  # fails closed with no context or no mrn
            call("doctor", "get_patient_history", mrn="MRN-1")
        with self.assertRaises(HospitalError):
            call("doctor", "get_patient_history", ctx=Yes())
        self.assertEqual(call("doctor", "get_patient_history", mrn="MRN-1", ctx=Yes()).view, "full")

    def test_nurse_department_scope(self):
        with self.assertRaises(HospitalError):
            call("nurse", "get_lab_results", mrn="MRN-1", ctx=No())
        call("nurse", "get_lab_results", mrn="MRN-1", ctx=Yes())

    def test_doctor_own_scope(self):
        with self.assertRaises(HospitalError):
            call("doctor", "cancel_appointment", doctor_id="OTHER")
        call("doctor", "cancel_appointment", doctor_id="S1")
        d = call("doctor", "get_appointment_history", listing=True)  # list call: results are filtered, not refused
        self.assertEqual(d.restrict_doctor_id, "S1")
        self.assertIsNone(call("receptionist", "get_appointment_history", listing=True).restrict_doctor_id)

    def test_tools_for_role(self):
        self.assertIn("create_doctor", tools_for_role("admin"))
        self.assertNotIn("get_patient_history", tools_for_role("admin"))
        self.assertNotIn("create_doctor", tools_for_role("receptionist"))
        self.assertEqual(set(tools_for_role("public")) & set(ADMIN_ONLY_TOOLS), set())
        self.assertEqual(tools_for_role("patient"), [])


class MaskingTests(unittest.TestCase):
    P = {"mrn": "MRN-1", "name": "A B", "sex": "F", "dob": "1990-05-04", "phone": "+919876543210",
         "email": "alice@example.com", "blood_group": "O+", "allergies": ["penicillin"],
         "insurance": {"provider_id": "STAR", "policy_no": "POL123456"}}

    def test_full(self):
        s = shape_patient_summary(self.P, "full")
        self.assertEqual((s["phone"], s["blood_group"], s["allergies"]), ("+919876543210", "O+", ["penicillin"]))

    def test_demographic_hides_clinical_only(self):
        s = shape_patient_summary(self.P, "demographic")
        self.assertEqual(s["phone"], "+919876543210")
        self.assertNotIn("blood_group", s)
        self.assertNotIn("allergies", s)

    def test_masked(self):
        s = shape_patient_summary(self.P, "masked")
        self.assertEqual((s["dob"], s["phone"], s["email"]), ("1990", "*********3210", "a***@example.com"))
        self.assertEqual(s["insurance"]["policy_no"], "*****3456")
        self.assertNotIn("blood_group", s)


if __name__ == "__main__":
    unittest.main()
