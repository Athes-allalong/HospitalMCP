"""Shared fixtures. Everything runs against the in-memory database with a fixed clock:
Monday 16 November 2026, 09:00 hospital time. No MongoDB, network or API keys needed."""
import os
from datetime import date, datetime

import pytest

os.environ.setdefault("AUTH_MODE", "dev")
os.environ.setdefault("MONGO_URI", "memory://")

from hospital_mcp.config import get_settings  # noqa: E402
from hospital_mcp.db import ensure_indexes  # noqa: E402
from hospital_mcp.errors import HospitalError  # noqa: E402
from hospital_mcp.identity import resolve_identity  # noqa: E402
from hospital_mcp.memdb import MemoryDatabase  # noqa: E402
from hospital_mcp.seed import seed_all  # noqa: E402
from hospital_mcp.services.context import Ctx, rate_limiter  # noqa: E402
from hospital_mcp.tools import REGISTRY, use_ctx  # noqa: E402

TODAY = date(2026, 11, 16)


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock():
    return Clock(datetime(2026, 11, 16, 9, 0))


@pytest.fixture
def db():
    d = MemoryDatabase()
    ensure_indexes(d)
    seed_all(d, today=TODAY)
    return d


@pytest.fixture(autouse=True)
def _no_rate_limits():
    old = dict(rate_limiter.limits)
    rate_limiter.limits.update({k: 10_000 for k in old})
    rate_limiter._hits.clear()
    yield
    rate_limiter.limits.update(old)
    rate_limiter._hits.clear()


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def as_user(db, clock, settings):
    """as_user("reception@dummyhospital.dev") -> Ctx for that staff login."""
    def make(email: str) -> Ctx:
        ident = resolve_identity(db, {"sub": f"dev|{email}", settings.email_claim: email}, settings)
        return Ctx(db, ident, clock=clock)
    return make


def call(c: Ctx, tool: str, **kwargs):
    with use_ctx(c):
        return REGISTRY[tool].call(**kwargs)


def code_of(fn, *a, **k) -> str:
    try:
        fn(*a, **k)
    except HospitalError as e:
        return e.code
    raise AssertionError("expected a HospitalError")


def two_step(c: Ctx, tool: str, **kwargs) -> dict:
    """Preview, then commit with the confirmation id (what a confirming user does)."""
    p = call(c, tool, **kwargs)
    assert p["status"] == "preview", p
    return call(c, tool, **kwargs, confirmation_id=p["confirmation_id"])


RECEPTION = "reception@dummyhospital.dev"
ADMIN = "admin@dummyhospital.dev"
ANANYA = "ananya.sharma@dummyhospital.dev"      # DOC-CARD-001
WEBB = "marcus.webb@dummyhospital.dev"          # DOC-NEUR-002
NURSE = "sarah.nurse@dummyhospital.dev"         # cardiology
BILLING = "billing@dummyhospital.dev"
PLATFORM = "platform@hospital-mcp.dev"
SUN_ADMIN = "admin@sunrise.dev"
SUN_RECEPTION = "reception@sunrise.dev"
