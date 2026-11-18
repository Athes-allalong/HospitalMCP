"""A small in-memory stand-in for MongoDB, used by the tests and by MONGO_URI=memory:// demos.

It supports the subset of the PyMongo API this project uses, including unique (and partial unique)
indexes, so double-booking and concurrency tests behave like they would on a real server.
It is thread-safe (one lock per database). Data is lost when the process exits.
"""
import copy
import re
import threading
import uuid
from typing import Any, Iterable, List, Optional


class DuplicateKeyError(Exception):
    pass


_MISSING = object()


def _get(doc: dict, path: str) -> Any:
    cur: Any = doc
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _set(doc: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    cur = doc
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def _unset(doc: dict, path: str) -> None:
    parts = path.split(".")
    cur = doc
    for p in parts[:-1]:
        cur = cur.get(p, {})
    if isinstance(cur, dict):
        cur.pop(parts[-1], None)


def _eq(val: Any, arg: Any) -> bool:
    if isinstance(val, list) and not isinstance(arg, list):
        return arg in val
    return val == arg


def _cmp(val: Any, arg: Any, op) -> bool:
    if val is None:
        return False
    try:
        return op(val, arg)
    except TypeError:
        return False


def _match_val(val: Any, cond: Any) -> bool:
    if isinstance(cond, dict) and cond and all(k.startswith("$") for k in cond):
        for op, arg in cond.items():
            if op == "$eq" and not _eq(val, arg):
                return False
            if op == "$ne" and _eq(val, arg):
                return False
            if op == "$gt" and not _cmp(val, arg, lambda a, b: a > b):
                return False
            if op == "$gte" and not _cmp(val, arg, lambda a, b: a >= b):
                return False
            if op == "$lt" and not _cmp(val, arg, lambda a, b: a < b):
                return False
            if op == "$lte" and not _cmp(val, arg, lambda a, b: a <= b):
                return False
            if op == "$in" and not any(_eq(val, a) for a in arg):
                return False
            if op == "$nin" and any(_eq(val, a) for a in arg):
                return False
            if op == "$exists" and (val is not None) != bool(arg):
                return False
            if op == "$type" and not isinstance(val, {"string": str, "date": object}.get(arg, object)):
                return False
            if op == "$regex":
                flags = re.I if "i" in cond.get("$options", "") else 0
                if not isinstance(val, str) or not re.search(arg, val, flags):
                    return False
        return True
    return _eq(val, cond)


def match(doc: dict, query: Optional[dict]) -> bool:
    for key, cond in (query or {}).items():
        if key == "$or":
            if not any(match(doc, q) for q in cond):
                return False
        elif key == "$and":
            if not all(match(doc, q) for q in cond):
                return False
        elif not _match_val(_get(doc, key), cond):
            return False
    return True


class _Result:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Cursor:
    def __init__(self, docs: List[dict]):
        self._docs = docs
        self._sort: List = []
        self._skip = 0
        self._limit = 0

    def sort(self, key, direction: int = 1):
        self._sort = key if isinstance(key, list) else [(key, direction)]
        return self

    def skip(self, n: int):
        self._skip = n
        return self

    def limit(self, n: int):
        self._limit = n
        return self

    def _materialize(self) -> List[dict]:
        docs = list(self._docs)
        for key, direction in reversed(self._sort):
            present = [d for d in docs if _get(d, key) is not None]
            missing = [d for d in docs if _get(d, key) is None]
            present.sort(key=lambda d: _get(d, key), reverse=direction < 0)
            docs = (missing + present) if direction > 0 else (present + missing)
        docs = docs[self._skip:]
        if self._limit:
            docs = docs[: self._limit]
        return [copy.deepcopy(d) for d in docs]

    def __iter__(self):
        return iter(self._materialize())


class Collection:
    def __init__(self, name: str, lock: threading.RLock):
        self.name = name
        self._docs: List[dict] = []
        self._lock = lock
        self._indexes: List[tuple] = []  # (fields, partial filter or None)

    # ---- indexes ----------------------------------------------------------
    def create_index(self, keys, unique: bool = False, partialFilterExpression: Optional[dict] = None, **_):
        if isinstance(keys, str):
            keys = [(keys, 1)]
        if unique:
            fields = tuple(k for k, _ in keys)
            if (fields, partialFilterExpression) not in self._indexes:
                self._indexes.append((fields, partialFilterExpression))
        return "_".join(k for k, _ in keys)

    def _check_unique(self, doc: dict, ignore: Optional[dict] = None) -> None:
        if any(d is not ignore and d.get("_id") == doc.get("_id") for d in self._docs):
            raise DuplicateKeyError(f"{self.name}: duplicate _id {doc.get('_id')!r}")
        for fields, partial in self._indexes:
            if partial is not None and not match(doc, partial):
                continue
            key = tuple(_get(doc, f) for f in fields)
            for other in self._docs:
                if other is ignore or (partial is not None and not match(other, partial)):
                    continue
                if tuple(_get(other, f) for f in fields) == key:
                    raise DuplicateKeyError(f"{self.name}: duplicate key {dict(zip(fields, key))}")

    # ---- writes -----------------------------------------------------------
    def insert_one(self, doc: dict):
        with self._lock:
            new = copy.deepcopy(doc)
            new.setdefault("_id", uuid.uuid4().hex)
            self._check_unique(new)
            self._docs.append(new)
            doc.setdefault("_id", new["_id"])
            return _Result(inserted_id=new["_id"])

    def insert_many(self, docs: Iterable[dict]):
        return _Result(inserted_ids=[self.insert_one(d).inserted_id for d in docs])

    @staticmethod
    def _apply(doc: dict, update: dict, inserting: bool = False) -> None:
        for op, fields in update.items():
            if op == "$set" or (op == "$setOnInsert" and inserting):
                for k, v in fields.items():
                    _set(doc, k, copy.deepcopy(v))
            elif op == "$unset":
                for k in fields:
                    _unset(doc, k)
            elif op == "$inc":
                for k, v in fields.items():
                    _set(doc, k, (_get(doc, k) or 0) + v)
            elif op == "$push":
                for k, v in fields.items():
                    cur = _get(doc, k) or []
                    _set(doc, k, cur + [copy.deepcopy(v)])
            elif op == "$setOnInsert":
                pass
            else:
                raise ValueError(f"memdb: unsupported update operator {op}")

    def _update(self, query: dict, update: dict, many: bool, upsert: bool):
        with self._lock:
            matched = [d for d in self._docs if match(d, query)]
            if not many:
                matched = matched[:1]
            for d in matched:
                trial = copy.deepcopy(d)
                self._apply(trial, update)
                self._check_unique(trial, ignore=d)
                d.clear()
                d.update(trial)
            if not matched and upsert:
                new = {k: v for k, v in query.items() if not k.startswith("$") and not isinstance(v, dict)}
                self._apply(new, update, inserting=True)
                self.insert_one(new)
                return _Result(matched_count=0, modified_count=0, upserted_id=new.get("_id"))
            return _Result(matched_count=len(matched), modified_count=len(matched), upserted_id=None)

    def update_one(self, query: dict, update: dict, upsert: bool = False):
        return self._update(query, update, many=False, upsert=upsert)

    def update_many(self, query: dict, update: dict, upsert: bool = False):
        return self._update(query, update, many=True, upsert=upsert)

    def find_one_and_update(self, query: dict, update: dict, return_document: Any = False,
                            upsert: bool = False, sort=None, **_):
        with self._lock:
            before = self.find_one(query, sort=sort)
            if before is None and not upsert:
                return None
            if before is None:
                self._update(query, update, many=False, upsert=True)
                return self.find_one(query) if return_document else None
            self._update({"_id": before["_id"]}, update, many=False, upsert=False)
            return self.find_one({"_id": before["_id"]}) if return_document else before

    def delete_one(self, query: dict):
        with self._lock:
            for i, d in enumerate(self._docs):
                if match(d, query):
                    del self._docs[i]
                    return _Result(deleted_count=1)
            return _Result(deleted_count=0)

    def delete_many(self, query: dict):
        with self._lock:
            keep = [d for d in self._docs if not match(d, query)]
            n = len(self._docs) - len(keep)
            self._docs[:] = keep
            return _Result(deleted_count=n)

    def find_one_and_delete(self, query: dict, **_):
        with self._lock:
            doc = self.find_one(query)
            if doc is not None:
                self.delete_one({"_id": doc["_id"]})
            return doc

    # ---- reads ------------------------------------------------------------
    def find(self, query: Optional[dict] = None, projection: Optional[dict] = None, **_):
        with self._lock:
            return Cursor([d for d in self._docs if match(d, query)])

    def find_one(self, query: Optional[dict] = None, projection: Optional[dict] = None, sort=None, **_):
        cur = self.find(query)
        if sort:
            cur.sort(sort)
        for d in cur.limit(1):
            return d
        return None

    def count_documents(self, query: Optional[dict] = None, **_) -> int:
        with self._lock:
            return sum(1 for d in self._docs if match(d, query))

    def distinct(self, key: str, query: Optional[dict] = None) -> list:
        out = []
        for d in self.find(query):
            v = _get(d, key)
            for x in (v if isinstance(v, list) else [v]):
                if x is not None and x not in out:
                    out.append(x)
        return out

    def drop(self) -> None:
        with self._lock:
            self._docs.clear()


class MemoryDatabase:
    def __init__(self, name: str = "memory"):
        self.name = name
        self._lock = threading.RLock()
        self._collections: dict = {}

    def __getitem__(self, name: str) -> Collection:
        with self._lock:
            if name not in self._collections:
                self._collections[name] = Collection(name, self._lock)
            return self._collections[name]

    def __getattr__(self, name: str) -> Collection:
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]

    def list_collection_names(self) -> List[str]:
        return list(self._collections)
