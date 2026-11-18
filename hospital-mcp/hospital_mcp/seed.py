"""Seed data: two hospitals (tenants) so isolation can be demonstrated.

H001 Dummy Hospital  - directory copied from the public site (dummyhospital.dev.aiwillbuild.com, read
                       16 Nov 2026); everything the site does not publish is GENERATED and labelled so.
H002 Sunrise Clinic  - fully synthetic second tenant. Uses the same MRN numbers and staff codes on
                       purpose: the tests prove the two hospitals never see each other's records.

Patients, appointments, labs, billing, etc. are synthetic (fixed random seed). No real patient data.
"""
import random
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional

from .services.common import DAYS, local_now, to_minor
from .services.slots import lock_keys
from .tenancy import TenantDB

SITE_URL = "http://dummyhospital.dev.aiwillbuild.com"
SNAPSHOT_DATE = "2026-11-16"

# ----------------------------------------------------------------------------------------------
# Snapshot of the public site (the scraper's job; kept here as the demo-day fallback, plan s.9.3)
# ----------------------------------------------------------------------------------------------
SITE = {
    "hospital": {
        "name": "Dummy Hospital", "tagline": "Compassionate Care, Advanced Medicine",
        "visiting_hours": "10:00 AM to 8:00 PM daily",
        "new_patient_info": "Bring a photo ID, your insurance card and a list of current medicines. "
                            "Arrive 15 minutes before your appointment.",
        "emergency": "24/7 Emergency Room, Ground Floor, Block A",
        "website": SITE_URL,
    },
    "departments": [
        ("CARD", "Cardiology", "Block A, Floor 2", "Dr. Ananya Sharma", "101",
         ["Cardiac Catheterization Lab", "Echocardiography Suite", "Stress Testing Center", "Electrophysiology Lab"]),
        ("NEUR", "Neurology", "Block C, Floor 3", "Dr. Sarah D'Souza", "301",
         ["EEG & EMG Diagnostic Labs", "Stereotactic Neurosurgery Suite", "Stroke Rehabilitation Unit",
          "Sleep Study Center"]),
        ("PED", "Pediatrics", "Block B, Floor 1", "Dr. Rajesh Iyer", "201",
         ["Level III Neonatal ICU (NICU)", "Pediatric Vaccination Center", "Lactation Support Clinic",
          "Child Development Center"]),
        ("ORTH", "Orthopedics", "Block D, Floor 1", "Dr. Vikram Malhotra", "401",
         ["Joint Replacement Suite", "Sports Injury Rehab", "Pediatric Orthopedic Clinic", "Advanced Fracture Care"]),
        ("DERM", "Dermatology", "Block E, Floor 1", "Dr. Meera Nair", "501",
         ["Laser Therapy Unit", "Dermatosurgery Suite", "Allergy Testing Lab", "Aesthetic Skin Care"]),
        ("ENDO", "Endocrinology", "Block F, Floor 2", "Dr. Amit Verma", "601",
         ["Diabetes Education Center", "Thyroid Function Lab", "Metabolic Screening", "Hormone Replacement Therapy"]),
    ],
    "support_units": [
        ("ER", "Emergency", "Ground Floor, Block A", "Open 24/7, 365 days a year"),
        ("ICU", "ICU", "Block A, Floor 3", "Advanced life support and continuous monitoring"),
        ("OPD", "OPD", "Block B, Ground Floor", "General and specialist outpatient consultations"),
        ("LAB", "Laboratory", "Block C, Floor 1", "Automated blood, urine and tissue analysis; accepts outside referrals"),
    ],
    # (name, department code, experience years, focus areas)
    "doctors": [
        ("Dr. Ananya Sharma", "CARD", 14, ["Interventional cardiology", "Heart failure"]),
        ("Dr. Julian Thorne", "CARD", 12, ["Electrophysiology", "Cardiac arrhythmias"]),
        ("Dr. Rajesh Iyer", "PED", 9, ["Neonatal care", "Adolescent health"]),
        ("Dr. Elena Rossi", "PED", 11, ["Pediatric nutrition", "Developmental milestones"]),
        ("Dr. Sarah D'Souza", "NEUR", 18, ["Stroke rehabilitation", "Multiple sclerosis", "Chronic pain"]),
        ("Dr. Marcus Webb", "NEUR", 15, ["Epilepsy", "Sleep disorders", "EEG diagnostics"]),
        ("Dr. Vikram Malhotra", "ORTH", 11, ["Sports medicine", "Joint replacement"]),
        ("Dr. Sofia Chen", "ORTH", 13, ["Spinal surgery", "Corrective osteotomy"]),
        ("Dr. Meera Nair", "DERM", 7, ["Medical and cosmetic dermatology", "Laser therapy"]),
        ("Dr. Oscar Wilde", "DERM", 10, ["Dermato-oncology", "Skin cancer screening"]),
        ("Dr. Amit Verma", "ENDO", 15, ["Diabetes", "Thyroid disorders"]),
        ("Dr. Clara Moon", "ENDO", 12, ["Adrenal and pituitary disorders"]),
    ],
    # nurses and allied staff listed on the Doctors page (name, role, department code)
    "staff": [
        ("Nurse Sarah", "Cardiac Care", "CARD"), ("Nurse Amy", "Neonatal Care", "PED"),
        ("Nurse Jen", "Neuro-Care", "NEUR"), ("Nurse Mia", "Post-Op Care", "ORTH"),
        ("Nurse Zoe", "Clinical Aid", "DERM"), ("Nurse Ben", "Diabetes Ed", "ENDO"),
    ],
    "live_dashboard": {"er_wait_minutes": 45, "icu_beds_available": 4, "ward_occupancy_percent": 85},
}

SPECIALTY = {"CARD": "Cardiology", "NEUR": "Neurology", "PED": "Pediatrics", "ORTH": "Orthopedics",
             "DERM": "Dermatology", "ENDO": "Endocrinology", "GEN": "General Medicine"}
QUALS = {"CARD": ["MBBS", "MD (Medicine)", "DM (Cardiology)"], "NEUR": ["MBBS", "MD (Medicine)", "DM (Neurology)"],
         "PED": ["MBBS", "MD (Pediatrics)"], "ORTH": ["MBBS", "MS (Orthopaedics)"],
         "DERM": ["MBBS", "MD (Dermatology)"], "ENDO": ["MBBS", "MD (Medicine)", "DM (Endocrinology)"],
         "GEN": ["MBBS", "MD (General Medicine)"]}
FIRST = ["Aarav", "Diya", "Vihaan", "Ananya", "Ishaan", "Saanvi", "Arjun", "Meera", "Kabir", "Aditi", "Rohan",
         "Kavya", "Siddharth", "Nisha", "Rahul", "Pooja", "Aditya", "Sneha", "Varun", "Lakshmi", "Farhan", "Zoya",
         "Joseph", "Mary", "Harpreet", "Gurleen", "Suresh", "Divya", "Manoj", "Asha"]
LAST = ["Patel", "Reddy", "Nair", "Iyer", "Sharma", "Gupta", "Khan", "Das", "Menon", "Rao", "Singh", "Joshi",
        "Kulkarni", "Fernandes", "Bose", "Mehta", "Pillai", "Hegde", "Shetty", "Chopra"]
INSURERS = [
    ("STAR", "Star Health", True, True, ["consultation", "surgery", "diagnostics", "maternity"], ["cosmetic"]),
    ("HDFC", "HDFC ERGO", True, True, ["consultation", "surgery", "diagnostics"], ["cosmetic", "maternity"]),
    ("ICICI", "ICICI Lombard", True, False, ["surgery", "diagnostics"], ["cosmetic"]),
    ("NIVA", "Niva Bupa", True, True, ["consultation", "surgery", "diagnostics", "maternity"], ["cosmetic"]),
    ("QCK", "QuickCover Mutual", False, False, ["surgery"], ["cosmetic", "diagnostics"]),
]
DIAGNOSES = {
    "CARD": [("Hypertension", ["Amlodipine 5 mg OD"]), ("Stable angina", ["Aspirin 75 mg OD", "Atorvastatin 20 mg HS"]),
             ("Palpitations, evaluated", ["Metoprolol 25 mg BD"])],
    "NEUR": [("Migraine", ["Sumatriptan 50 mg SOS"]), ("Focal epilepsy", ["Levetiracetam 500 mg BD"]),
             ("Insomnia", ["Sleep hygiene counselling"])],
    "PED": [("Viral fever", ["Paracetamol syrup 250 mg SOS"]), ("Routine vaccination", []),
            ("Iron deficiency", ["Iron syrup 5 ml OD"])],
    "ORTH": [("Knee osteoarthritis", ["Physiotherapy", "Diclofenac gel"]), ("Ankle sprain", ["Rest, ice, compression"]),
             ("Low back pain", ["Physiotherapy", "Paracetamol 650 mg SOS"])],
    "DERM": [("Atopic dermatitis", ["Moisturiser", "Hydrocortisone 1% cream"]), ("Acne vulgaris", ["Adapalene gel HS"]),
             ("Mole check, benign", [])],
    "ENDO": [("Type 2 diabetes", ["Metformin 500 mg BD"]), ("Hypothyroidism", ["Levothyroxine 50 mcg OD"]),
             ("Prediabetes", ["Diet and exercise plan"])],
    "GEN": [("Upper respiratory infection", ["Paracetamol 650 mg SOS"]), ("Gastritis", ["Pantoprazole 40 mg OD"])],
}
LABS = [("CBC", "Complete blood count", {"Hb (g/dL)": (11.0, 16.0), "WBC (10^3/uL)": (4.0, 11.0)}),
        ("HBA1C", "HbA1c", {"HbA1c (%)": (5.0, 9.5)}),
        ("TSH", "Thyroid stimulating hormone", {"TSH (mIU/L)": (0.4, 8.0)}),
        ("LIPID", "Lipid profile", {"LDL (mg/dL)": (70, 190), "HDL (mg/dL)": (35, 70)})]


def _schedule(rng: random.Random, morning_days: List[str], afternoon_days: List[str]) -> Dict[str, list]:
    sched: Dict[str, list] = {}
    for d in morning_days:
        sched.setdefault(d, []).append(["10:00", "13:00"])
    for d in afternoon_days:
        sched.setdefault(d, []).append(["14:00", "17:00"])
    return sched


def _reset(db: Any, hospital_ids: List[str]) -> None:
    for name in db.list_collection_names():
        if name == "hospitals":
            db[name].delete_many({"hospital_id": {"$in": hospital_ids}})
        else:
            db[name].delete_many({"hospital_id": {"$in": hospital_ids + [None]}}
                                 if name == "staff_users" else {"hospital_id": {"$in": hospital_ids}})


# ----------------------------------------------------------------------------------------------
def seed_all(db: Any, today: Optional[date] = None, admin_email: Optional[str] = None,
             platform_email: Optional[str] = None, reset: bool = True) -> dict:
    """Create both hospitals. admin_email / platform_email let you sign in with your real Auth0 e-mail."""
    today = today or local_now().date()
    if reset:
        _reset(db, ["H001", "H002"])
        db.staff_users.delete_many({"role": "platform_admin"})
    db.staff_users.insert_one({"hospital_id": None, "subject": "PLAT-001", "name": "Platform Owner",
                               "email": (platform_email or "platform@hospital-mcp.dev").lower(),
                               "role": "platform_admin", "department_id": None, "active": True, "auth_sub": None,
                               "created_by": "seed", "created_at": datetime.combine(today, time(8))})
    a = seed_dummy_hospital(db, today, admin_email)
    b = seed_sunrise_clinic(db, today)
    return {"H001": a, "H002": b}


def _staff(t: TenantDB, today: date, rows: List[tuple]) -> None:
    for subject, name, email, role, dept in rows:
        t.staff_users.insert_one({"subject": subject, "name": name, "email": email.lower(), "role": role,
                                  "department_id": dept, "active": True, "auth_sub": None, "created_by": "seed",
                                  "created_at": datetime.combine(today, time(8))})


def seed_dummy_hospital(db: Any, today: date, admin_email: Optional[str] = None) -> dict:
    rng = random.Random(42)
    hid = "H001"
    db.hospitals.insert_one({"hospital_id": hid, "slug": "dummy-hospital", "active": True, **SITE["hospital"],
                             "address": "Generated: 12 Demo Road, Bengaluru 560064", "phone": "+91-80-0000-0101",
                             "data_source": {"site": SITE_URL, "snapshot": SNAPSHOT_DATE,
                                             "generated": ["address", "phone"]}})
    t = TenantDB(db, hid)
    # departments: copied from the site; hours generated (site lists none)
    for code, name, loc, head, ext, services in SITE["departments"]:
        t.departments.insert_one({"department_id": code, "name": name, "kind": "clinical", "location": loc,
                                  "phone_ext": ext, "head_name": head, "head_staff_id": None, "services": services,
                                  "hours": "Mon-Sat 10:00-17:00 (generated)", "avg_consult_minutes": 15,
                                  "data_source": {"site": ["name", "location", "head", "ext", "services"],
                                                  "generated": ["hours", "avg_consult_minutes"]}})
    for code, name, loc, desc in SITE["support_units"]:
        t.departments.insert_one({"department_id": code, "name": name, "kind": "support", "location": loc,
                                  "services": [desc], "hours": "24/7" if code in ("ER", "ICU") else "Mon-Sat 08:00-20:00",
                                  "avg_consult_minutes": 15, "data_source": {"site": ["name", "location", "services"]}})
    # doctors: name / department / experience / focus from the site; the rest generated
    counters: Dict[str, int] = {}
    doctors = []
    day_sets = [(["mon", "wed", "fri"], ["mon", "tue", "wed", "thu", "fri"]),
                (["tue", "thu", "sat"], ["tue", "thu"]), (["mon", "tue", "wed", "thu", "fri", "sat"], ["mon", "wed"])]
    for i, (name, dept, years, focus) in enumerate(SITE["doctors"]):
        counters[dept] = counters.get(dept, 0) + 1
        staff_id = f"DOC-{dept}-{counters[dept]:03d}"
        dept_doc = next(d for d in SITE["departments"] if d[0] == dept)
        mornings, afternoons = day_sets[i % 3]
        if name == "Dr. Ananya Sharma":  # scenario 1 of the plan books her at 3 PM, so she has afternoons daily
            mornings, afternoons = ["mon", "wed", "fri"], ["mon", "tue", "wed", "thu", "fri", "sat"]
        doc = {"staff_id": staff_id, "name": name, "department_id": dept, "department_name": SPECIALTY[dept],
               "specialty": SPECIALTY[dept], "focus_areas": focus, "experience_years": years,
               "qualifications": QUALS[dept], "languages": rng.sample(["English", "Hindi", "Kannada", "Tamil",
                                                                        "Malayalam", "Telugu"], 2),
               "consultation_fee_minor": to_minor(rng.choice([700, 800, 900, 1000, 1200, 1500])),
               "room": f"{dept_doc[2].split(',')[0].replace('Block ', '')}{dept_doc[2][-1]}-{10 + counters[dept]}",
               "schedule": _schedule(rng, mornings, afternoons), "visit_types": ["new", "followup", "teleconsult"],
               "bio": f"{name} has {years} years of experience in {', '.join(focus).lower()}.",
               "active": True, "created_by": "seed",
               "data_source": {"site": ["name", "department", "experience_years", "focus_areas"],
                               "generated": ["staff_id", "fee", "room", "qualifications", "languages", "schedule"]}}
        t.doctors.insert_one(doc)
        doctors.append(doc)
    for code, _, _, head, _, _ in SITE["departments"]:
        sid = next(d["staff_id"] for d in doctors if d["name"] == head)
        t.departments.update_one({"department_id": code}, {"$set": {"head_staff_id": sid}})
    _staff(t, today, [
        ("ADM-001", "Priya Raman", admin_email or "admin@dummyhospital.dev", "admin", None),
        ("REC-001", "Kavya Rao", "reception@dummyhospital.dev", "receptionist", None),
        ("DOC-CARD-001", "Dr. Ananya Sharma", "ananya.sharma@dummyhospital.dev", "doctor", "CARD"),
        ("DOC-NEUR-002", "Dr. Marcus Webb", "marcus.webb@dummyhospital.dev", "doctor", "NEUR"),
        ("NUR-CARD-01", "Nurse Sarah", "sarah.nurse@dummyhospital.dev", "nurse", "CARD"),
        ("BIL-001", "Arjun Mehta", "billing@dummyhospital.dev", "billing", None),
    ])
    _insurers(t)
    counts = _clinical(t, rng, today, doctors, n_patients=30, avoid=[("DOC-CARD-001", 1, time(15))])
    # live numbers from the site's Live Dashboard; the rest generated
    stamp = datetime.combine(today, time(9))
    dash = SITE["live_dashboard"]
    t.facilities.insert_one({"facility": "icu", "total_beds": 20, "occupied": 20 - dash["icu_beds_available"],
                             "ventilators_total": 10, "ventilators_in_use": 6, "updated_at": stamp,
                             "updated_by": "seed"})
    t.facilities.insert_one({"facility": "er", "wait_minutes": dash["er_wait_minutes"], "triage_queue": 7,
                             "diversion": False, "updated_at": stamp, "updated_by": "seed"})
    t.facilities.insert_one({"facility": "ward", "occupancy_percent": dash["ward_occupancy_percent"],
                             "updated_at": stamp, "updated_by": "seed"})
    _blood(t, rng, stamp)
    _roster(t, today, doctors, SITE["staff"])
    _ot(t, rng, today, [d for d in doctors if d["department_id"] in ("ORTH", "CARD", "NEUR")])
    _holidays(t, today)
    webb = next(d for d in doctors if d["name"] == "Dr. Marcus Webb")
    t.leaves.insert_one({"doctor_id": webb["staff_id"], "start_date": (today + timedelta(days=10)).isoformat(),
                         "end_date": (today + timedelta(days=12)).isoformat(), "reason": "Conference",
                         "created_by": "seed", "created_at": stamp})
    return {"doctors": len(doctors), **counts}


def seed_sunrise_clinic(db: Any, today: date) -> dict:
    rng = random.Random(7)
    hid = "H002"
    db.hospitals.insert_one({"hospital_id": hid, "slug": "sunrise-clinic", "active": True, "name": "Sunrise Clinic",
                             "tagline": "Your neighbourhood family clinic (synthetic demo tenant)",
                             "address": "Generated: 4 Lake View Road, Mysuru", "phone": "+91-821-000-0202",
                             "visiting_hours": "9:00 AM to 7:00 PM, Monday to Saturday",
                             "new_patient_info": "Bring a photo ID and any previous prescriptions.",
                             "emergency": "No emergency room. For emergencies call 108."})
    t = TenantDB(db, hid)
    for code, name, loc in (("GEN", "General Medicine", "Ground Floor"), ("PED", "Pediatrics", "First Floor")):
        t.departments.insert_one({"department_id": code, "name": name, "kind": "clinical", "location": loc,
                                  "services": [], "hours": "Mon-Sat 09:00-19:00", "avg_consult_minutes": 15})
    doctors = []
    for sid, name, dept, years in (("DOC-GEN-001", "Dr. Kiran Bhat", "GEN", 20), ("DOC-GEN-002", "Dr. Leela Kamath", "GEN", 8),
                                   ("DOC-PED-001", "Dr. Naveen Gowda", "PED", 12)):
        doc = {"staff_id": sid, "name": name, "department_id": dept, "department_name": SPECIALTY[dept],
               "specialty": SPECIALTY[dept], "focus_areas": [SPECIALTY[dept]], "experience_years": years,
               "qualifications": QUALS[dept], "languages": ["English", "Kannada"],
               "consultation_fee_minor": to_minor(500), "room": f"S-{len(doctors) + 1}",
               "schedule": _schedule(rng, ["mon", "tue", "wed", "thu", "fri", "sat"], ["mon", "wed", "fri"]),
               "visit_types": ["new", "followup"], "bio": f"{name}, family physician.", "active": True}
        t.doctors.insert_one(doc)
        doctors.append(doc)
    _staff(t, today, [
        ("ADM-001", "Rekha Shenoy", "admin@sunrise.dev", "admin", None),
        ("REC-001", "Manju Prasad", "reception@sunrise.dev", "receptionist", None),
        ("DOC-GEN-001", "Dr. Kiran Bhat", "kiran.bhat@sunrise.dev", "doctor", "GEN"),
    ])
    _insurers(t, only=("STAR", "NIVA"))
    counts = _clinical(t, rng, today, doctors, n_patients=10)
    stamp = datetime.combine(today, time(9))
    t.facilities.insert_one({"facility": "icu", "total_beds": 0, "occupied": 0, "updated_at": stamp})
    t.facilities.insert_one({"facility": "er", "wait_minutes": None, "triage_queue": 0, "diversion": True,
                             "updated_at": stamp})
    _blood(t, rng, stamp)
    _holidays(t, today)
    return {"doctors": len(doctors), **counts}


# ----------------------------------------------------------------------------------------------
def _insurers(t: TenantDB, only: tuple = ()) -> None:
    for pid, name, accepted, cashless, covered, excl in INSURERS:
        if only and pid not in only:
            continue
        t.insurers.insert_one({"provider_id": pid, "name": name, "accepted": accepted, "cashless": cashless,
                               "covered": covered, "exclusions": excl, "preauth_above_minor": to_minor(50_000),
                               "tpa_contact": f"tpa@{pid.lower()}.example"})


def _clinical(t: TenantDB, rng: random.Random, today: date, doctors: List[dict], n_patients: int,
              avoid: List[tuple] = ()) -> dict:
    """Patients with insurance, past visits (+ notes, prescriptions, labs, invoices), future bookings, admissions."""
    insurer_ids = [i["provider_id"] for i in t.insurers.find()]
    taken_doc, taken_pat = set(), set()
    blocked = {(sid, datetime.combine(today + timedelta(days=dd), tm) + timedelta(minutes=15 * k))
               for sid, dd, tm in avoid for k in (-1, 0, 1)}
    counts = {"patients": 0, "past_visits": 0, "future_appointments": 0}
    created = datetime.combine(today - timedelta(days=120), time(9))
    pats = []
    for i in range(n_patients):
        mrn = f"MRN-{1001 + i}"
        name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        p = {"mrn": mrn, "name": name, "sex": rng.choice("MF"),
             "dob": date(rng.randint(1950, 2018), rng.randint(1, 12), rng.randint(1, 28)).isoformat(),
             "phone": f"+9198{rng.randint(10_000_000, 99_999_999)}",
             "email": f"{name.split()[0].lower()}.{mrn[-4:]}@example.com",
             "blood_group": rng.choice(["A+", "B+", "O+", "AB+", "O-", "A-", "B-"]),
             "allergies": rng.choice([[], [], ["Penicillin"], ["Sulfa drugs"], ["Peanuts"]]),
             "address": f"{rng.randint(1, 200)}, {rng.choice(['MG Road', 'Jayanagar', 'Indiranagar', 'Yelahanka'])}",
             "insurance": None, "synthetic": True, "created_at": created}
        if insurer_ids and rng.random() < 0.6:
            pid = rng.choice(insurer_ids)
            pol = f"{pid}-{rng.randint(100000, 999999)}"
            p["insurance"] = {"provider_id": pid, "policy_no": pol}
            t.policies.insert_one({"provider_id": pid, "policy_no": pol, "mrn": mrn,
                                   "status": rng.choice(["active", "active", "active", "lapsed"]),
                                   "valid_till": (today + timedelta(days=rng.randint(-30, 400))).isoformat(),
                                   "copay_percent": rng.choice([0, 10, 20]),
                                   "sum_insured_minor": to_minor(rng.choice([300_000, 500_000, 1_000_000]))})
        t.patients.insert_one(p)
        pats.append(p)
        counts["patients"] += 1

    def free_slot(doc: dict, day: date, mrn: str, minutes: int) -> Optional[datetime]:
        wins = doc["schedule"].get(DAYS[day.weekday()], [])
        options = []
        for s, e in wins:
            st = datetime.combine(day, time.fromisoformat(s))
            while st + timedelta(minutes=minutes) <= datetime.combine(day, time.fromisoformat(e)):
                options.append(st)
                st += timedelta(minutes=30)
        rng.shuffle(options)
        for st in options:
            cells = [st + timedelta(minutes=15 * k) for k in range(minutes // 15)]
            if any((doc["staff_id"], c) in blocked for c in cells):
                continue
            if any((doc["staff_id"], c) in taken_doc or (mrn, c) in taken_pat for c in cells):
                continue
            for c in cells:
                taken_doc.add((doc["staff_id"], c))
                taken_pat.add((mrn, c))
            return st
        return None

    n_appt = 0
    for idx, p in enumerate(pats):
        # first patient always sees the first doctor (so the doctor demo account has patients)
        own = [doctors[0]] if idx == 0 else []
        for doc in own + rng.sample(doctors, k=min(len(doctors), rng.randint(1, 2))):
            for _ in range(rng.randint(1, 2)):  # past visits
                day = today - timedelta(days=rng.randint(3, 90))
                st = free_slot(doc, day, p["mrn"], 30)
                if not st:
                    continue
                n_appt += 1
                aid = f"APT-S{t.hospital_id[-1]}{n_appt:05d}"
                diag, meds = rng.choice(DIAGNOSES[doc["department_id"]])
                t.appointments.insert_one({"appointment_id": aid, "mrn": p["mrn"], "patient_name": p["name"],
                                           "doctor_id": doc["staff_id"], "doctor_name": doc["name"],
                                           "department_id": doc["department_id"], "type": "new", "slot_start": st,
                                           "slot_end": st + timedelta(minutes=30), "status": "completed",
                                           "fee_inr": doc["consultation_fee_minor"] / 100, "lock_gen": 0,
                                           "created_by": "seed", "created_at": st - timedelta(days=3)})
                t.visits.insert_one({"visit_id": f"VIS-{aid[5:]}", "appointment_id": aid, "mrn": p["mrn"],
                                     "doctor_id": doc["staff_id"], "department_id": doc["department_id"],
                                     "date": st.date().isoformat(), "diagnosis": diag,
                                     "summary": f"Seen for {diag.lower()}. Advised follow-up as needed."})
                if meds:
                    t.prescriptions.insert_one({"rx_id": f"RX-{aid[5:]}", "mrn": p["mrn"], "doctor_id": doc["staff_id"],
                                                "appointment_id": aid, "items": meds, "issued_at": st})
                if rng.random() < 0.5:
                    code, test_name, ranges = rng.choice(LABS)
                    t.lab_results.insert_one({
                        "test_id": f"LAB-{aid[5:]}", "mrn": p["mrn"], "ordered_by": doc["staff_id"],
                        "code": code, "test_name": test_name, "status": "final", "reported_at": st + timedelta(hours=6),
                        "results": {k: round(rng.uniform(lo, hi), 1) for k, (lo, hi) in ranges.items()}})
                if rng.random() < 0.4:
                    fee = doc["consultation_fee_minor"]
                    paid = rng.choice([0, fee])
                    inv_id = f"INV-{st:%Y%m%d}-{aid[-5:]}"
                    t.invoices.insert_one({"invoice_id": inv_id, "mrn": p["mrn"], "appointment_id": aid,
                                           "items": [{"description": "Consultation", "quantity": 1, "unit_minor": fee,
                                                      "line_minor": fee}],
                                           "total_minor": fee, "paid_minor": paid,
                                           "status": "paid" if paid else "issued", "created_by": "seed",
                                           "created_at": st})
                    if p["insurance"]:
                        t.claims.insert_one({"invoice_id": inv_id, "mrn": p["mrn"],
                                             "provider_id": p["insurance"]["provider_id"],
                                             "status": rng.choice(["submitted", "approved", "not_submitted"]),
                                             "created_at": st})
                counts["past_visits"] += 1
        if rng.random() < 0.55 or idx < 3:  # a future booking in the next week
            doc = doctors[0] if idx == 0 else rng.choice(doctors)
            for _ in range(10):
                day = today + timedelta(days=rng.randint(1, 7))
                st = free_slot(doc, day, p["mrn"], 30)
                if st:
                    break
            if st:
                n_appt += 1
                aid = f"APT-S{t.hospital_id[-1]}{n_appt:05d}"
                t.appointments.insert_one({"appointment_id": aid, "mrn": p["mrn"], "patient_name": p["name"],
                                           "doctor_id": doc["staff_id"], "doctor_name": doc["name"],
                                           "department_id": doc["department_id"], "type": "new", "slot_start": st,
                                           "slot_end": st + timedelta(minutes=30), "status": "booked",
                                           "reason": "Follow-up consultation",
                                           "fee_inr": doc["consultation_fee_minor"] / 100, "lock_gen": 0,
                                           "created_by": "seed", "created_at": datetime.combine(today, time(9))})
                for key in lock_keys(doc["staff_id"], p["mrn"], st, st + timedelta(minutes=30)):
                    t.slot_locks.insert_one({"_id": f"{t.hospital_id}|{key}", "owner": f"{aid}:0",
                                             "expires_at": st + timedelta(minutes=30)})
                counts["future_appointments"] += 1
    for p in pats[3:6]:  # a few inpatients
        doc = rng.choice(doctors)
        t.admissions.insert_one({"mrn": p["mrn"], "status": "admitted", "ward": f"{doc['department_name']} Ward",
                                 "bed": f"B-{rng.randint(1, 40):02d}", "department_id": doc["department_id"],
                                 "doctor_id": doc["staff_id"],
                                 "admitted_at": datetime.combine(today - timedelta(days=rng.randint(1, 5)), time(11))})
        t.appointments.insert_one({"appointment_id": f"APT-ADM-{p['mrn'][-4:]}", "mrn": p["mrn"],
                                   "patient_name": p["name"], "doctor_id": doc["staff_id"], "doctor_name": doc["name"],
                                   "department_id": doc["department_id"], "type": "new", "status": "completed",
                                   "slot_start": datetime.combine(today - timedelta(days=6), time(9)),
                                   "slot_end": datetime.combine(today - timedelta(days=6), time(9, 30)),
                                   "lock_gen": 0, "created_by": "seed"})
    return counts


def _blood(t: TenantDB, rng: random.Random, stamp: datetime) -> None:
    for g in ("A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"):
        units = rng.randint(2, 6) if g.endswith("-") else rng.randint(8, 40)
        t.blood_inventory.insert_one({"blood_group": g, "units": units, "updated_at": stamp, "updated_by": "seed"})


def _roster(t: TenantDB, today: date, doctors: List[dict], staff: List[tuple]) -> None:
    for k in range(-1, 8):
        day = today + timedelta(days=k)
        for d in doctors:
            if d["schedule"].get(DAYS[day.weekday()]):
                t.duty_roster.insert_one({"department_id": d["department_id"], "staff_id": d["staff_id"],
                                          "name": d["name"], "role": "doctor",
                                          "shift_start": datetime.combine(day, time(9)),
                                          "shift_end": datetime.combine(day, time(18))})
        for i, (name, role, dept) in enumerate(staff):
            t.duty_roster.insert_one({"department_id": dept, "staff_id": f"NUR-{dept}-01", "name": name,
                                      "role": f"nurse ({role})", "shift_start": datetime.combine(day, time(8)),
                                      "shift_end": datetime.combine(day, time(20))})


def _ot(t: TenantDB, rng: random.Random, today: date, surgeons: List[dict]) -> None:
    procs = {"ORTH": ["Total knee replacement", "ACL reconstruction"], "CARD": ["Coronary angioplasty"],
             "NEUR": ["Deep brain stimulation (stage 1)"]}
    for k in range(0, 4):
        day = today + timedelta(days=k)
        for slot, theatre in ((time(8), "OT-1"), (time(13), "OT-2")):
            doc = rng.choice(surgeons)
            t.ot_schedule.insert_one({"date": day.isoformat(), "theatre": theatre, "start": slot.strftime("%H:%M"),
                                      "duration_minutes": rng.choice([90, 120, 180]),
                                      "procedure": rng.choice(procs[doc["department_id"]]),
                                      "surgeon_id": doc["staff_id"], "surgeon": doc["name"], "status": "scheduled"})


def _holidays(t: TenantDB, today: date) -> None:
    for d, name in (("2026-12-25", "Christmas"), ("2027-01-26", "Republic Day"), ("2027-08-15", "Independence Day"),
                    ("2027-10-02", "Gandhi Jayanti")):
        if d >= today.isoformat():
            t.holidays.insert_one({"date": d, "name": name})
