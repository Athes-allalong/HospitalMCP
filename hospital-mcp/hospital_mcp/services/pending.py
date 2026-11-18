"""Server-enforced preview-then-commit (plan s.7.4), plus request_id idempotency.

PendingActions.create() stores a short-lived, single-use confirmation bound to the caller (hospital,
subject, role), the tool and the exact arguments. consume() rejects a different caller, different
arguments, an expired or reused id. Even a model that skips the "ask the user" step cannot commit
without the id from a preview call, and in the in-app assistant the id never reaches the model at all.
"""
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Optional, Protocol, Tuple

from ..errors import Codes, HospitalError
from .common import local_now

TTL_SECONDS = 300


class PendingStore(Protocol):
    def put(self, doc: dict) -> None: ...
    def take(self, confirmation_id: str) -> Optional[dict]: ...  # atomic find-and-delete


class CollectionPendingStore:
    """PendingStore over a (tenant-scoped) Mongo collection."""

    def __init__(self, coll: Any):
        self.c = coll

    def put(self, doc: dict) -> None:
        self.c.insert_one(doc)

    def take(self, confirmation_id: str) -> Optional[dict]:
        return self.c.find_one_and_delete({"_id": confirmation_id})


def args_hash(args: dict) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()


def _caller(ident) -> tuple:
    return (getattr(ident, "hospital_id", None), ident.subject, ident.role)


class PendingActions:
    def __init__(self, store: PendingStore, clock: Callable = local_now, ttl_seconds: int = TTL_SECONDS):
        self.store, self.clock, self.ttl = store, clock, ttl_seconds

    def create(self, ident, tool: str, args: dict) -> dict:
        cid = "CNF-" + secrets.token_urlsafe(12)
        expires = self.clock() + timedelta(seconds=self.ttl)
        h, subject, role = _caller(ident)
        self.store.put({"_id": cid, "confirmation_id": cid, "caller_hospital": h, "subject": subject,
                        "role": role, "tool": tool, "args_hash": args_hash(args), "expires_at": expires})
        return {"confirmation_id": cid, "expires_at": expires.isoformat()}

    def consume(self, ident, tool: str, args: dict, confirmation_id: str) -> None:
        doc = self.store.take(confirmation_id)  # single use: removed even if a check below fails
        again = "Call the tool again without confirmation_id to get a fresh preview."
        if not doc:
            raise HospitalError(Codes.CONFIRMATION_INVALID, "This confirmation is unknown or already used.", hint=again)
        if doc["expires_at"] < self.clock():
            raise HospitalError(Codes.CONFIRMATION_EXPIRED, "The confirmation expired.", hint=again)
        if (doc.get("caller_hospital"), doc["subject"], doc["role"], doc["tool"], doc["args_hash"]) != (
                *_caller(ident), tool, args_hash(args)):
            raise HospitalError(Codes.CONFIRMATION_INVALID, "This confirmation does not match the request.",
                                hint=again)


def pending_for(ctx) -> PendingActions:
    return PendingActions(CollectionPendingStore(ctx.t.pending_actions), clock=ctx.clock)


@dataclass
class Plan:
    target: str
    summary: dict
    apply: Callable[[], Tuple[Optional[dict], Optional[dict]]]  # returns (before, after) for the audit log


NEXT_STEP = ("Show this preview to the user. Only after they confirm, call the tool again with the same "
             "arguments plus this confirmation_id.")


def two_phase(ctx, tool: str, args: dict, confirmation_id: Optional[str], prepare: Callable[[], Plan]) -> dict:
    """prepare() validates against the current state and builds a Plan without writing anything.
    No confirmation_id -> preview + confirmation_id. With one -> consume it, then apply. Validation runs
    in both phases, so a commit re-checks the world as it is now."""
    plan = prepare()
    pending = pending_for(ctx)
    if not confirmation_id:
        conf = pending.create(ctx.ident, tool, args)
        ctx.audit(tool, plan.target, outcome="previewed", args=args)
        return {"status": "preview", "tool": tool, "target": plan.target, "preview": plan.summary, **conf,
                "next_step": NEXT_STEP}
    pending.consume(ctx.ident, tool, args, confirmation_id)
    before, after = plan.apply()
    ctx.audit(tool, plan.target, outcome="ok", before=before, after=after, args=args)
    return {"status": "committed", "tool": tool, "target": plan.target, "result": after}


def idempotent(ctx, tool: str, request_id: Optional[str], fn: Callable[[], dict]) -> dict:
    """Run fn() at most once per (hospital, caller, tool, request_id). A replay returns the stored result."""
    if not request_id:
        return fn()
    if len(request_id) > 128:
        raise HospitalError(Codes.INVALID_INPUT, "request_id must be at most 128 characters.")
    from ..db import DuplicateKeyError
    from .common import clean
    key = f"{ctx.ident.hospital_id}:{ctx.ident.subject}:{tool}:{request_id}"
    coll = ctx.t.idempotency
    try:
        coll.insert_one({"_id": key, "state": "pending", "created_at": ctx.clock()})
    except DuplicateKeyError:
        prior = coll.find_one({"_id": key}) or {}
        if prior.get("state") == "done":
            return {**prior["result"], "replayed": True}
        raise HospitalError(Codes.IN_PROGRESS, "A request with this request_id is still in progress.")
    try:
        result = clean(fn())
    except Exception:
        coll.delete_one({"_id": key})  # let the caller retry
        raise
    if result.get("status") == "preview":   # previews are not results; do not pin them to the request_id
        coll.delete_one({"_id": key})
    else:
        coll.update_one({"_id": key}, {"$set": {"state": "done", "result": result}})
    return result
