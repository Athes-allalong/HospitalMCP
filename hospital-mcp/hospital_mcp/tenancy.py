"""Multi-tenancy: every hospital's data lives in the same collections, tagged with hospital_id.

Services never touch a raw collection for tenant data. They use TenantDB, which:
  * adds {"hospital_id": <caller's hospital>} to every query, update, count and delete,
  * stamps hospital_id on every insert,
  * refuses any attempt to read or write another hospital's id.
The caller's hospital comes from the verified login (staff_users row), never from tool arguments,
so a prompt or a crafted argument cannot switch hospitals.
"""
from typing import Any, Optional

from .errors import Codes, HospitalError


def _guard(value: Any, hospital_id: str) -> None:
    if value is not None and value != hospital_id:
        raise HospitalError(Codes.FORBIDDEN, "Cross-hospital access is not allowed.")


class TenantCollection:
    def __init__(self, coll: Any, hospital_id: str):
        if not hospital_id:
            raise HospitalError(Codes.FORBIDDEN, "No hospital in scope for this caller.")
        self._c, self.hospital_id = coll, hospital_id

    def _q(self, query: Optional[dict]) -> dict:
        q = dict(query or {})
        _guard(q.get("hospital_id"), self.hospital_id)
        q["hospital_id"] = self.hospital_id
        return q

    def _u(self, update: dict) -> dict:
        for op in ("$set", "$setOnInsert", "$unset"):
            if "hospital_id" in update.get(op, {}):
                _guard(update[op]["hospital_id"], self.hospital_id)
        return update

    def _doc(self, doc: dict) -> dict:
        _guard(doc.get("hospital_id"), self.hospital_id)
        doc["hospital_id"] = self.hospital_id
        return doc

    # reads
    def find(self, query: Optional[dict] = None, *a, **k):
        return self._c.find(self._q(query), *a, **k)

    def find_one(self, query: Optional[dict] = None, *a, **k):
        return self._c.find_one(self._q(query), *a, **k)

    def count_documents(self, query: Optional[dict] = None, **k) -> int:
        return self._c.count_documents(self._q(query), **k)

    def distinct(self, key: str, query: Optional[dict] = None):
        return self._c.distinct(key, self._q(query))

    # writes
    def insert_one(self, doc: dict):
        return self._c.insert_one(self._doc(doc))

    def insert_many(self, docs):
        return self._c.insert_many([self._doc(d) for d in docs])

    def update_one(self, query: dict, update: dict, **k):
        return self._c.update_one(self._q(query), self._u(update), **k)

    def update_many(self, query: dict, update: dict, **k):
        return self._c.update_many(self._q(query), self._u(update), **k)

    def find_one_and_update(self, query: dict, update: dict, **k):
        return self._c.find_one_and_update(self._q(query), self._u(update), **k)

    def delete_one(self, query: dict):
        return self._c.delete_one(self._q(query))

    def delete_many(self, query: dict):
        return self._c.delete_many(self._q(query))

    def find_one_and_delete(self, query: dict, **k):
        return self._c.find_one_and_delete(self._q(query), **k)


class TenantDB:
    """db-like object whose collections are all scoped to one hospital."""

    def __init__(self, db: Any, hospital_id: str):
        self._db, self.hospital_id = db, hospital_id

    def __getitem__(self, name: str) -> TenantCollection:
        return TenantCollection(self._db[name], self.hospital_id)

    def __getattr__(self, name: str) -> TenantCollection:
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]
