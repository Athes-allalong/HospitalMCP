"""Database connection and indexes.

MONGO_URI=memory://  -> in-memory store (tests, quick demo without MongoDB)
anything else        -> PyMongo (local replica set, or MongoDB Atlas)

Every tenant-owned collection has hospital_id as the first field of its unique indexes, so the
same MRN or staff ID can exist in two hospitals without clashing.
"""
from functools import lru_cache
from typing import Any

from .memdb import DuplicateKeyError as MemDuplicateKeyError, MemoryDatabase

try:  # PyMongo is optional only so the test-suite can run without it
    from pymongo import ASCENDING, DESCENDING, MongoClient
    from pymongo.errors import DuplicateKeyError as MongoDuplicateKeyError
except ImportError:  # pragma: no cover
    ASCENDING, DESCENDING, MongoClient = 1, -1, None

    class MongoDuplicateKeyError(Exception):  # type: ignore[no-redef]
        pass

DuplicateKeyError = (MemDuplicateKeyError, MongoDuplicateKeyError)
ASC, DESC = ASCENDING, DESCENDING


def connect(uri: str, name: str) -> Any:
    if uri.startswith("memory://"):
        return MemoryDatabase(name)
    if MongoClient is None:
        raise RuntimeError("pymongo is not installed: pip install -r requirements.txt")
    client = MongoClient(uri, tz_aware=False, serverSelectionTimeoutMS=8000)
    return client[name]


@lru_cache(maxsize=1)
def get_db() -> Any:
    from .config import get_settings
    s = get_settings()
    db = connect(s.mongo_uri, s.mongo_db)
    ensure_indexes(db)
    if isinstance(db, MemoryDatabase):  # demo mode: nothing persists, so start with the seed data
        import os
        from .seed import seed_all
        seed_all(db, admin_email=os.getenv("SEED_ADMIN_EMAIL"), platform_email=os.getenv("SEED_PLATFORM_EMAIL"))
    return db


def ensure_indexes(db) -> None:
    H = ("hospital_id", ASC)
    booked = {"status": "booked"}
    db.hospitals.create_index("hospital_id", unique=True)
    db.hospitals.create_index("slug", unique=True)
    # one login belongs to one hospital
    db.staff_users.create_index("email", unique=True)
    db.staff_users.create_index([H, ("subject", ASC)], unique=True)
    db.staff_users.create_index("auth_sub", unique=True, partialFilterExpression={"auth_sub": {"$type": "string"}})
    db.patients.create_index([H, ("mrn", ASC)], unique=True)
    db.doctors.create_index([H, ("staff_id", ASC)], unique=True)
    db.departments.create_index([H, ("department_id", ASC)], unique=True)
    db.appointments.create_index([H, ("appointment_id", ASC)], unique=True)
    # double-booking guards (plan section 6): same doctor or same patient, same start, while booked
    db.appointments.create_index([H, ("doctor_id", ASC), ("slot_start", ASC)], unique=True,
                                 partialFilterExpression=booked)
    db.appointments.create_index([H, ("mrn", ASC), ("slot_start", ASC)], unique=True,
                                 partialFilterExpression=booked)
    db.appointments.create_index([H, ("mrn", ASC), ("slot_start", DESC)])
    db.invoices.create_index([H, ("invoice_id", ASC)], unique=True)
    db.payments.create_index([H, ("gateway_ref", ASC)], unique=True)
    db.lab_results.create_index([H, ("test_id", ASC)], unique=True)
    db.holidays.create_index([H, ("date", ASC)], unique=True)
    db.leaves.create_index([H, ("doctor_id", ASC), ("start_date", ASC)])
    db.insurers.create_index([H, ("provider_id", ASC)], unique=True)
    db.facilities.create_index([H, ("facility", ASC)], unique=True)
    db.blood_inventory.create_index([H, ("blood_group", ASC)], unique=True)
    db.audit_log.create_index([H, ("at", DESC)])
    if not isinstance(db, MemoryDatabase):  # TTL clean-up for short-lived records (MongoDB only)
        db.pending_actions.create_index("expires_at", expireAfterSeconds=0)
        db.idempotency.create_index("created_at", expireAfterSeconds=24 * 3600)
        db.slot_locks.create_index("expires_at", expireAfterSeconds=7 * 24 * 3600)
