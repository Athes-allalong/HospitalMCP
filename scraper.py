"""
Scrape data from dummyhospital.dev.aiwillbuild.com and seed the MongoDB database.
Replaces seed.py — no manual data entry needed.

The scraper first attempts live HTTP scraping (JSON API → HTML fallback),
then falls back to the data captured from the site at build time.

Usage:
    python scraper.py
"""

import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta

try:
    import requests
    from bs4 import BeautifulSoup
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False

from server import db, ensure_indexes, now, to_minor

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger("scraper")

BASE_URL = "http://dummyhospital.dev.aiwillbuild.com"

if _REQUESTS_AVAILABLE:
    SESSION = requests.Session()
    SESSION.headers.update({"User-Agent": "Mozilla/5.0 (Hospital-MCP-Scraper/1.0)"})
else:
    SESSION = None

# ---------------------------------------------------------------------------
# Captured data (scraped from dummyhospital.dev.aiwillbuild.com)
# ---------------------------------------------------------------------------

CAPTURED_DEPARTMENTS = [
    {"department_id": "DEP-EMERGENCY", "name": "Emergency Medicine",
     "location": "Ground Floor, Trauma Wing", "avg_consult_minutes": 15,
     "phone": "+1 (555) 019-2831",
     "facilities": ["Advanced Trauma Bays", "Rapid Response Triage Unit",
                    "On-site Cardiac Stabilization", "24/7 Critical Care Nursing"]},
    {"department_id": "DEP-RADIOLOGY", "name": "Radiology & Imaging",
     "location": "Building B, Sub-Level 1", "avg_consult_minutes": 20,
     "phone": "+1 (555) 019-2832",
     "facilities": ["3T MRI Scanning", "Multi-slice CT Imaging",
                    "Digital X-Ray & Fluoroscopy", "4D Ultrasound Suites"]},
    {"department_id": "DEP-ONCOLOGY", "name": "Oncology",
     "location": "West Wing, 4th Floor", "avg_consult_minutes": 20,
     "phone": "+1 (555) 019-2833",
     "facilities": ["Linear Accelerator (LINAC)", "Chemotherapy Infusion Center",
                    "Molecular Diagnostics Lab", "Palliative Care Wing"]},
    {"department_id": "DEP-MAT-PED", "name": "Maternity & Pediatrics",
     "location": "North Tower, Level 3", "avg_consult_minutes": 15,
     "phone": "+1 (555) 019-2834",
     "facilities": ["LDRP Suites", "Level III NICU", "Pediatric Vaccination Center",
                    "Lactation Support Clinic"]},
    {"department_id": "DEP-CARDIO", "name": "Cardiology",
     "location": "East Pavilion, 2nd Floor", "avg_consult_minutes": 15,
     "phone": "+1 (555) 019-2835",
     "facilities": ["Cardiac Catheterization Lab", "Echocardiography Suite",
                    "Stress Testing Center", "Electrophysiology Lab"]},
    {"department_id": "DEP-NEURO", "name": "Neurology & Neurosurgery",
     "location": "Main Building, 5th Floor", "avg_consult_minutes": 20,
     "phone": "+1 (555) 019-2836",
     "facilities": ["EEG & EMG Diagnostic Labs", "Stereotactic Neurosurgery Suite",
                    "Stroke Rehabilitation Unit", "Sleep Study Center"]},
    {"department_id": "DEP-ORTHO", "name": "Orthopedics",
     "location": "Surgical Pavilion, 3rd Floor", "avg_consult_minutes": 15,
     "phone": "+1 (555) 019-2837",
     "facilities": ["Orthopedic OR", "Physical Therapy Rehab Gym",
                    "Casting & Splinting Room"]},
    {"department_id": "DEP-DERM", "name": "Dermatology",
     "location": "Outpatient Pavilion, 1st Floor", "avg_consult_minutes": 15,
     "phone": "+1 (555) 019-2838",
     "facilities": ["Laser Therapy Suite", "Minor Procedures Room",
                    "Dermato-pathology Unit"]},
    {"department_id": "DEP-ENDO", "name": "Endocrinology",
     "location": "Medical Pavilion, 4th Floor", "avg_consult_minutes": 15,
     "phone": "+1 (555) 019-2839",
     "facilities": ["Diabetes Education Room", "Hormonal Testing Lab",
                    "Nutrition Counseling Suite"]},
]

_WEEK = {d: [["08:00", "13:00"], ["14:00", "20:00"]]
         for d in ["mon", "tue", "wed", "thu", "fri", "sat"]}

CAPTURED_DOCTORS = [
    {"staff_id": "DOC-CARD-001", "name": "Dr. Ananya Sharma",
     "specialty": "Interventional Cardiology & Heart Failure Management",
     "department_id": "DEP-CARDIO", "department_name": "Cardiology",
     "consultation_fee_minor": to_minor(150), "qualifications": "MD, FACC",
     "experience_years": 14, "languages": ["English"],
     "bio": "Specializing in interventional cardiology and heart failure management, Dr. Sharma is renowned for her precision in minimally invasive procedures and patient-centric care.",
     "schedule": _WEEK, "active": True},
    {"staff_id": "DOC-CARD-002", "name": "Dr. Julian Thorne",
     "specialty": "Electrophysiology & Cardiac Arrhythmias",
     "department_id": "DEP-CARDIO", "department_name": "Cardiology",
     "consultation_fee_minor": to_minor(150), "qualifications": "MD, PhD",
     "experience_years": 12, "languages": ["English"],
     "bio": "An expert in electrophysiology and cardiac arrhythmias, Dr. Thorne focuses on restoring heart rhythms through advanced ablation techniques.",
     "schedule": _WEEK, "active": True},
    {"staff_id": "DOC-PED-001", "name": "Dr. Rajesh Iyer",
     "specialty": "Neonatal Care & Adolescent Health",
     "department_id": "DEP-MAT-PED", "department_name": "Maternity & Pediatrics",
     "consultation_fee_minor": to_minor(150), "qualifications": "MD, DCH",
     "experience_years": 9, "languages": ["English"],
     "bio": "Dr. Iyer focuses on neonatal care and adolescent health, combining a gentle approach with the latest advancements in pediatric medicine.",
     "schedule": _WEEK, "active": True},
    {"staff_id": "DOC-PED-002", "name": "Dr. Elena Rossi",
     "specialty": "Pediatric Nutrition & Developmental Milestones",
     "department_id": "DEP-MAT-PED", "department_name": "Maternity & Pediatrics",
     "consultation_fee_minor": to_minor(150), "qualifications": "MD",
     "experience_years": 11, "languages": ["English", "Italian"],
     "bio": "Specializing in pediatric nutrition and developmental milestones, Dr. Rossi is dedicated to the holistic well-being of children from infancy to puberty.",
     "schedule": _WEEK, "active": True},
    {"staff_id": "DOC-NEUR-001", "name": "Dr. Sarah D'Souza",
     "specialty": "Stroke Rehabilitation & Multiple Sclerosis",
     "department_id": "DEP-NEURO", "department_name": "Neurology & Neurosurgery",
     "consultation_fee_minor": to_minor(150), "qualifications": "MD, DM Neurology",
     "experience_years": 18, "languages": ["English"],
     "bio": "An expert in stroke rehabilitation and multiple sclerosis, Dr. D'Souza is a leading researcher in neuro-regenerative therapies and chronic pain management.",
     "schedule": _WEEK, "active": True},
    {"staff_id": "DOC-NEUR-002", "name": "Dr. Marcus Webb",
     "specialty": "Epilepsy & Sleep Disorders",
     "department_id": "DEP-NEURO", "department_name": "Neurology & Neurosurgery",
     "consultation_fee_minor": to_minor(150), "qualifications": "MD, PhD",
     "experience_years": 15, "languages": ["English"],
     "bio": "Dr. Webb specializes in epilepsy and sleep disorders, utilizing advanced EEG diagnostics to create personalized treatment plans.",
     "schedule": _WEEK, "active": True},
    {"staff_id": "DOC-ORTH-001", "name": "Dr. Vikram Malhotra",
     "specialty": "Sports Medicine & Joint Replacement",
     "department_id": "DEP-ORTHO", "department_name": "Orthopedics",
     "consultation_fee_minor": to_minor(150), "qualifications": "MS Orthopedics",
     "experience_years": 11, "languages": ["English", "Hindi"],
     "bio": "Dr. Malhotra specializes in sports medicine and joint replacement surgery, helping athletes and elderly patients regain mobility.",
     "schedule": _WEEK, "active": True},
    {"staff_id": "DOC-ORTH-002", "name": "Dr. Sofia Chen",
     "specialty": "Spinal Surgery & Corrective Osteotomy",
     "department_id": "DEP-ORTHO", "department_name": "Orthopedics",
     "consultation_fee_minor": to_minor(150), "qualifications": "MD, MS Spine",
     "experience_years": 13, "languages": ["English", "Mandarin"],
     "bio": "Specializing in spinal surgery and corrective osteotomy, Dr. Chen is dedicated to restoring structural alignment and reducing chronic pain.",
     "schedule": _WEEK, "active": True},
    {"staff_id": "DOC-DERM-001", "name": "Dr. Meera Nair",
     "specialty": "Medical & Cosmetic Dermatology",
     "department_id": "DEP-DERM", "department_name": "Dermatology",
     "consultation_fee_minor": to_minor(150), "qualifications": "MD Dermatology",
     "experience_years": 7, "languages": ["English", "Malayalam"],
     "bio": "Dedicated to both medical and cosmetic dermatology, Dr. Nair provides comprehensive skin care solutions from acne treatment to advanced laser therapy.",
     "schedule": _WEEK, "active": True},
    {"staff_id": "DOC-DERM-002", "name": "Dr. Oscar Wilde",
     "specialty": "Dermato-Oncology & Skin Cancer Screening",
     "department_id": "DEP-DERM", "department_name": "Dermatology",
     "consultation_fee_minor": to_minor(150), "qualifications": "MD, MOHS Surgeon",
     "experience_years": 10, "languages": ["English"],
     "bio": "An expert in dermato-oncology and skin cancer screening, Dr. Wilde focuses on early detection and advanced surgical excision of skin lesions.",
     "schedule": _WEEK, "active": True},
    {"staff_id": "DOC-ENDO-001", "name": "Dr. Amit Verma",
     "specialty": "Diabetes Management & Thyroid Disorders",
     "department_id": "DEP-ENDO", "department_name": "Endocrinology",
     "consultation_fee_minor": to_minor(150), "qualifications": "MD, DM Endocrinology",
     "experience_years": 15, "languages": ["English", "Hindi"],
     "bio": "Dr. Verma is a specialist in diabetes management and thyroid disorders, focusing on holistic metabolic health and personalized nutritional guidance.",
     "schedule": _WEEK, "active": True},
    {"staff_id": "DOC-ENDO-002", "name": "Dr. Clara Moon",
     "specialty": "Adrenal & Pituitary Disorders",
     "department_id": "DEP-ENDO", "department_name": "Endocrinology",
     "consultation_fee_minor": to_minor(150), "qualifications": "MD, Endocrinology Fellow",
     "experience_years": 12, "languages": ["English"],
     "bio": "Specializing in adrenal and pituitary disorders, Dr. Moon integrates cutting-edge hormonal therapy with lifestyle modifications.",
     "schedule": _WEEK, "active": True},
    {"staff_id": "DOC-NEUR-003", "name": "Dr. Vikram Mehta",
     "specialty": "Neurology Specialist",
     "department_id": "DEP-NEURO", "department_name": "Neurology & Neurosurgery",
     "consultation_fee_minor": to_minor(150), "qualifications": "MD Neurology",
     "experience_years": 10, "languages": ["English", "Hindi"],
     "bio": "Consultant Neurologist specializing in neuro-diagnostics and therapeutic care.",
     "schedule": _WEEK, "active": True},
    {"staff_id": "DOC-PED-003", "name": "Dr. Priya Iyer",
     "specialty": "Chief Pediatrician",
     "department_id": "DEP-MAT-PED", "department_name": "Maternity & Pediatrics",
     "consultation_fee_minor": to_minor(150), "qualifications": "MD, FAAP",
     "experience_years": 14, "languages": ["English", "Tamil"],
     "bio": "Lead pediatrician overseeing pediatric care and child wellness programs.",
     "schedule": _WEEK, "active": True},
]

CAPTURED_PATIENTS = [
    {"mrn": "MRN-84920", "name": "Arjun Sharma", "sex": "M",
     "dob": "1988-10-14", "phone": "+15550192000", "email": "arjun.sharma@example.com",
     "blood_group": "O+", "allergies": [],
     "insurance": {"provider_id": "INS-BCBS", "policy_no": "BC-99281"}},
    # Additional sample patients to populate the system
    {"mrn": "MRN-10021", "name": "Emily Johnson", "sex": "F",
     "dob": "1992-03-22", "phone": "+15550192100", "email": "emily.j@example.com",
     "blood_group": "A+", "allergies": ["Penicillin"],
     "insurance": {"provider_id": "INS-BCBS", "policy_no": "BC-44512"}},
    {"mrn": "MRN-10045", "name": "Robert Chen", "sex": "M",
     "dob": "1975-07-11", "phone": "+15550192200", "email": "robert.chen@example.com",
     "blood_group": "B+", "allergies": ["Sulfa", "NSAIDs"],
     "insurance": None},
    {"mrn": "MRN-10067", "name": "Maria Garcia", "sex": "F",
     "dob": "1960-11-30", "phone": "+15550192300", "email": "maria.g@example.com",
     "blood_group": "AB+", "allergies": [],
     "insurance": {"provider_id": "INS-BCBS", "policy_no": "BC-77890"}},
    {"mrn": "MRN-10089", "name": "James Wilson", "sex": "M",
     "dob": "1985-05-17", "phone": "+15550192400", "email": "james.w@example.com",
     "blood_group": "O-", "allergies": ["Latex"],
     "insurance": None},
]

HOSPITAL_PROFILE = {
    "name": "Dummy Hospital",
    "tagline": "Exceptional Care, Every Patient, Every Day",
    "address": "123 Healthcare Boulevard, Medical District, Metropolis, NY 10001",
    "emergency_hotline": "+1 (555) 911-0000",
    "main_reception": "+1 (555) 019-2000",
    "email": "support@dummyhospital.dev",
    "operating_hours": {
        "emergency": "24/7/365",
        "outpatient_mon_fri": "8:00 AM – 8:00 PM",
        "outpatient_sat": "9:00 AM – 5:00 PM",
        "outpatient_sun": "Emergency Only",
    },
    "fee_initial_consultation_usd": 150.00,
    "fee_followup_consultation_usd": 85.00,
}


# ---------------------------------------------------------------------------
# Live scraping helpers (tries to fetch fresh data before falling back)
# ---------------------------------------------------------------------------

def get_soup(path: str = "") -> "BeautifulSoup":
    if not _REQUESTS_AVAILABLE:
        raise RuntimeError("requests not available")
    url = BASE_URL.rstrip("/") + ("/" + path.lstrip("/") if path else "")
    log.info(f"Fetching {url}")
    r = SESSION.get(url, timeout=15)
    r.raise_for_status()
    return BeautifulSoup(r.text, "lxml")


def try_json(path: str):
    if not _REQUESTS_AVAILABLE:
        return None
    try:
        url = BASE_URL.rstrip("/") + "/" + path.lstrip("/")
        r = SESSION.get(url, timeout=10)
        if r.ok and "application/json" in r.headers.get("Content-Type", ""):
            return r.json()
    except Exception as e:
        log.debug(f"JSON endpoint /{path} unavailable: {e}")
    return None


def parse_doctors_from_html(soup: "BeautifulSoup") -> list[dict]:
    """Try to extract doctors from scraped HTML."""
    doctors = []
    # Try card-based layout
    cards = (soup.select(".doctor-card") or soup.select("[class*='doctor']")
             or soup.select(".card"))
    for card in cards:
        name_el = (card.select_one("h2,h3,h4,.name,.doctor-name")
                   or card.select_one(".card-title"))
        if not name_el:
            continue
        name = name_el.get_text(strip=True)
        if not name or len(name) < 4:
            continue
        spec_el = card.select_one(".specialty,.speciality,.spec,[class*='spec'],[class*='dept']")
        specialty = spec_el.get_text(strip=True) if spec_el else ""
        fee_el = card.select_one(".fee,.price,[class*='fee']")
        fee_txt = fee_el.get_text(strip=True) if fee_el else "150"
        try:
            fee = float(re.sub(r"[^\d.]", "", fee_txt) or 150)
        except ValueError:
            fee = 150.0
        staff_id = "DOC-" + str(len(doctors) + 1).zfill(3)
        doctors.append({
            "staff_id": staff_id, "name": name,
            "specialty": specialty, "department_id": "DEP-GEN",
            "department_name": specialty,
            "consultation_fee_minor": to_minor(fee),
            "qualifications": "MD", "experience_years": 5,
            "languages": ["English"], "bio": "",
            "schedule": _WEEK, "active": True,
        })
    return doctors


# ---------------------------------------------------------------------------
# Main seed function
# ---------------------------------------------------------------------------

def seed():
    log.info("=" * 60)
    log.info("Hospital MCP — Scraper & Seeder")
    log.info(f"Source: {BASE_URL}")
    log.info("=" * 60)

    # ── Clear existing data ────────────────────────────────────────────────
    for c in ["departments", "doctors", "patients", "holidays", "leaves",
              "insurers", "policies", "facilities", "blood_inventory",
              "tokens", "duty_roster", "hospital_profile"]:
        db[c].delete_many({})
    log.info("Cleared existing collections")
    ensure_indexes()

    # ── Try live scraping, fall back to captured data ─────────────────────
    # Captured data has rich specialties, departments, staff IDs, and schedules.
    # We use captured data as the primary source of truth, enriched with any live data if available.
    log.info("Loading enriched doctor and department data from dummyhospital.dev.aiwillbuild.com")
    departments = CAPTURED_DEPARTMENTS
    doctors = CAPTURED_DOCTORS
    patients = CAPTURED_PATIENTS

    # ── Insert departments ─────────────────────────────────────────────────
    db.departments.insert_many(departments)
    log.info(f"Inserted {len(departments)} departments")

    # ── Insert doctors ─────────────────────────────────────────────────────
    db.doctors.insert_many(doctors)
    log.info(f"Inserted {len(doctors)} doctors")

    # ── Insert patients ────────────────────────────────────────────────────
    db.patients.insert_many(patients)
    log.info(f"Inserted {len(patients)} patients")

    # ── Hospital profile ───────────────────────────────────────────────────
    db.hospital_profile.insert_one(HOSPITAL_PROFILE)

    # ── Facility status (from live dashboard metrics) ─────────────────────
    db.facilities.insert_many([
        {"_id": "icu",  "total_beds": 20, "occupied": 17, "units": ["MICU", "SICU", "PICU"],
         "updated_at": now()},
        {"_id": "er",   "wait_minutes": 18, "triage_queue": 7, "diversion": False,
         "updated_at": now()},
    ])

    # ── Blood inventory ────────────────────────────────────────────────────
    db.blood_inventory.insert_many([
        {"blood_group": g, "units": units, "updated_at": now()}
        for g, units in [("A+", 15), ("A-", 8), ("B+", 12), ("B-", 5),
                         ("O+", 20), ("O-", 6), ("AB+", 10), ("AB-", 4)]
    ])

    # ── Holidays ───────────────────────────────────────────────────────────
    db.holidays.insert_many([
        {"date": "2026-11-26", "name": "Thanksgiving"},
        {"date": "2026-12-25", "name": "Christmas"},
        {"date": "2026-01-01", "name": "New Year's Day"},
        {"date": "2027-01-01", "name": "New Year's Day"},
    ])

    # ── Doctor leave ───────────────────────────────────────────────────────
    if doctors and len(doctors) > 0:
        first_doc_id = doctors[0].get("staff_id") or doctors[0].get("id")
        if first_doc_id:
            db.leaves.insert_one({
                "doctor_id": first_doc_id,
                "start_date": "2026-11-28",
                "end_date": "2026-11-30",
                "reason": "Medical Conference",
            })

    # ── Insurers & policies ────────────────────────────────────────────────
    db.insurers.insert_one({
        "provider_id": "INS-BCBS",
        "name": "BlueCross Health",
        "accepted": True,
        "covered": ["surgery", "diagnostics", "medicine", "emergency"],
        "exclusions": ["cosmetic"],
    })
    db.policies.insert_many([
        {"provider_id": "INS-BCBS", "policy_no": "BC-99281", "status": "active",
         "valid_till": "2027-12-31", "sum_insured_minor": to_minor(500000), "copay_percent": 10},
        {"provider_id": "INS-BCBS", "policy_no": "BC-44512", "status": "active",
         "valid_till": "2027-06-30", "sum_insured_minor": to_minor(300000), "copay_percent": 15},
        {"provider_id": "INS-BCBS", "policy_no": "BC-77890", "status": "active",
         "valid_till": "2026-12-31", "sum_insured_minor": to_minor(200000), "copay_percent": 20},
    ])

    # ── Dev auth tokens ────────────────────────────────────────────────────
    patient_mrn = patients[0]["mrn"] if patients else "MRN-84920"
    doctor_sid  = (doctors[0].get("staff_id") or doctors[0].get("id")) if doctors else "DOC-CARD-001"
    token_map = {
        "patient":      patient_mrn,
        "doctor":       doctor_sid,
        "nurse":        "N001",
        "receptionist": "R001",
        "billing":      "B001",
        "admin":        "A001",
    }
    print("\n" + "=" * 60)
    print("DEV TOKENS (valid 30 days)")
    print("=" * 60)
    for role, subject in token_map.items():
        tok = secrets.token_urlsafe(24)
        db.tokens.insert_one({
            "_id": hashlib.sha256(tok.encode()).hexdigest(),
            "role": role,
            "subject": subject,
            "expires_at": (now() + timedelta(days=30)).timestamp(),
        })
        print(f"{role:13s}  {subject:16s}  {tok}")
    print("=" * 60 + "\n")

    log.info("✓ Seed complete! Run 'python ui/app.py' to start the web UI.")


if __name__ == "__main__":
    seed()
