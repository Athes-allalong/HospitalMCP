"""Ctx: everything a service call needs, built once per request from the verified identity.

  ctx.ident   who is calling (from the token)
  ctx.t       this caller's hospital's collections (tenancy.TenantDB) -- the only door to tenant data
  ctx.raw     the unscoped database: used only by platform tools and the audit log
  ctx.audit() audit entry (who, tool, target, outcome, before/after); never raises
"""
import hashlib
import json
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from typing import Any, Callable, Optional

from ..errors import Codes, HospitalError
from ..identity import Identity
from ..tenancy import TenantDB
from .common import local_now


class Ctx:
    def __init__(self, raw_db: Any, ident: Identity, clock: Callable[[], datetime] = local_now,
                 request_id: Optional[str] = None):
        self.raw, self.ident, self.clock, self.request_id = raw_db, ident, clock, request_id
        self._t: Optional[TenantDB] = None

    @property
    def t(self) -> TenantDB:
        if self._t is None:
            self._t = TenantDB(self.raw, self.ident.hospital_id)
        return self._t

    # ---- record-level questions for authz.authorize() --------------------
    def doctor_treats(self, staff_id: str, mrn: str) -> bool:
        return self.t.appointments.find_one({"mrn": mrn, "doctor_id": staff_id}) is not None

    def nurse_serves_patient(self, department_id: Optional[str], mrn: str) -> bool:
        if not department_id:
            return False
        return (self.t.appointments.find_one({"mrn": mrn, "department_id": department_id}) is not None
                or self.t.admissions.find_one({"mrn": mrn, "department_id": department_id,
                                               "status": "admitted"}) is not None)

    def appointment_in_department(self, department_id: Optional[str], appointment: dict) -> bool:
        return bool(department_id) and appointment.get("department_id") == department_id

    # ---- audit -----------------------------------------------------------
    def audit(self, tool: str, target: Optional[str] = None, *, outcome: str = "ok", before: Any = None,
              after: Any = None, error_code: Optional[str] = None, args: Optional[dict] = None) -> None:
        """Who / which tool / when / outcome. Arguments are stored as a hash, not raw text (plan s.10)."""
        doc = {"at": self.clock(), "hospital_id": self.ident.hospital_id, "role": self.ident.role,
               "actor": self.ident.subject, "actor_email": self.ident.email, "tool": tool, "target": target,
               "outcome": outcome, "request_id": self.request_id}
        if args is not None:
            doc["args_hash"] = hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:16]
        if error_code:
            doc["error_code"] = error_code
        if before is not None or after is not None:
            doc["before"], doc["after"] = before, after
        try:
            self.raw.audit_log.insert_one(doc)
        except Exception:  # auditing must never break the call itself
            pass


# --------------------------------------------------------------------------
# Rate limiting (plan s.10): per caller and per tool, tighter for writes and admin tools.
# In-process sliding window. With several server processes, move this to Redis or the gateway.
# --------------------------------------------------------------------------
LIMITS_PER_MINUTE = {"read": 60, "write": 20, "destructive": 10, "admin": 15, "public": 20}


class RateLimiter:
    def __init__(self, limits: dict = LIMITS_PER_MINUTE, clock: Callable[[], float] = time.monotonic):
        self.limits, self.clock = limits, clock
        self._hits: dict = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, ident: Identity, tool: str, kind: str, caller_key: Optional[str] = None) -> None:
        bucket = "public" if ident.is_public else kind
        limit = self.limits.get(bucket, 60)
        key = (ident.hospital_id, caller_key or ident.subject, tool)
        now = self.clock()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= limit:
                raise HospitalError(Codes.RATE_LIMITED, f"Too many {tool} calls. Limit is {limit} per minute.",
                                    hint="Wait a minute and try again.")
            q.append(now)


rate_limiter = RateLimiter()
