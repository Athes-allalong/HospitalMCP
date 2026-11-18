# Permission matrix

Generated from `hospital_mcp/authz/matrix.py`, the single source of truth. `tests/test_matrix.py` checks every cell. After editing the matrix, run `python scripts/gen_permissions.py`.

Legend: ✅ full · 🔒 restricted (view and/or record scope) · blank = denied. Hospital isolation applies underneath every cell.

| Tool | Kind | public | receptionist | nurse | doctor | billing | admin | platform admin |
|---|---|---|---|---|---|---|---|---|
| `get_hospital_info` | read | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |  |
| `search_doctors` | read | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |  |
| `get_doctor_profile` | read | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |  |
| `check_availability` | read | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |  |
| `list_departments` | read | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |  |
| `get_icu_occupancy` | read | 🔒 summary | ✅ | ✅ | ✅ |  | ✅ |  |
| `get_er_status` | read | 🔒 summary | ✅ | ✅ | ✅ |  | ✅ |  |
| `get_blood_inventory` | read | 🔒 summary | ✅ | ✅ | ✅ |  | ✅ |  |
| `get_department_status` | read | 🔒 summary | ✅ | ✅ | ✅ |  | ✅ |  |
| `get_ot_schedule` | read |  |  | ✅ | ✅ |  | ✅ |  |
| `update_facility_status` | write |  |  | ✅ |  |  | ✅ |  |
| `search_patients` | read |  | 🔒 demographic | 🔒 department | 🔒 treating | 🔒 masked | 🔒 masked |  |
| `register_patient` | write |  | ✅ |  |  |  | ✅ |  |
| `get_patient_summary` | read |  | 🔒 demographic | 🔒 department | 🔒 treating | 🔒 masked | 🔒 masked |  |
| `get_admission_status` | read |  | ✅ | 🔒 department | 🔒 treating |  |  |  |
| `get_patient_history` | read |  |  | 🔒 department | 🔒 treating |  |  |  |
| `list_prescriptions` | read |  |  | 🔒 department | 🔒 treating |  |  |  |
| `get_lab_results` | read |  |  | 🔒 department | 🔒 treating |  |  |  |
| `add_clinical_note` | write |  |  |  | 🔒 own |  |  |  |
| `update_patient_contact` | write |  | ✅ |  |  |  | ✅ |  |
| `list_appointments` | read |  | ✅ | 🔒 department | 🔒 own |  | ✅ |  |
| `get_appointment` | read |  | ✅ | 🔒 department | 🔒 own |  | ✅ |  |
| `get_appointment_history` | read |  | ✅ | 🔒 department | 🔒 own |  | ✅ |  |
| `book_appointment` | write |  | ✅ |  | 🔒 own |  | ✅ |  |
| `reschedule_appointment` | write |  | ✅ |  | 🔒 own |  | ✅ |  |
| `cancel_appointment` | destructive |  | ✅ |  | 🔒 own |  | ✅ |  |
| `get_invoice` | read |  | 🔒 status |  |  | ✅ | ✅ |  |
| `get_claim_status` | read |  |  |  |  | ✅ | ✅ |  |
| `create_invoice` | write |  |  |  |  | ✅ |  |  |
| `record_payment` | write |  |  |  |  | ✅ |  |  |
| `check_insurance_coverage` | read | 🔒 accepted_only | ✅ |  |  | ✅ |  |  |
| `submit_preauthorization` | write |  |  |  | ✅ | ✅ |  |  |
| `create_doctor` | admin |  |  |  |  |  | ✅ |  |
| `update_doctor_profile` | admin |  |  |  |  |  | ✅ |  |
| `update_doctor_schedule` | admin |  |  |  |  |  | ✅ |  |
| `add_schedule_exception` | admin |  |  |  |  |  | ✅ |  |
| `deactivate_doctor` | admin |  |  |  |  |  | ✅ |  |
| `create_department` | admin |  |  |  |  |  | ✅ |  |
| `update_department` | admin |  |  |  |  |  | ✅ |  |
| `set_department_head` | admin |  |  |  |  |  | ✅ |  |
| `list_staff_users` | read |  |  |  |  |  | ✅ |  |
| `create_staff_user` | admin |  |  |  |  |  | ✅ |  |
| `change_staff_role` | admin |  |  |  |  |  | ✅ |  |
| `deactivate_staff_user` | destructive |  |  |  |  |  | ✅ |  |
| `get_audit_log` | read |  |  |  |  |  | ✅ |  |
| `list_hospitals` | read |  |  |  |  |  |  | ✅ |
| `create_hospital` | admin |  |  |  |  |  |  | ✅ |
| `set_hospital_active` | admin |  |  |  |  |  |  | ✅ |
