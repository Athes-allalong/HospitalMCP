"""Authentication: tokens are verified (signature, expiry, issuer, audience) and mapped to an ACTIVE staff
user every time (plan s.5, scenarios 10 and 12). Auth0 is simulated with a locally generated RSA key."""
import time
from dataclasses import replace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from hospital_mcp import identity as ident_mod
from hospital_mcp.errors import Codes, HospitalError
from hospital_mcp.identity import TokenVerifier, authenticate
from tests.conftest import ADMIN, RECEPTION, two_step


def _code(fn):
    with pytest.raises(HospitalError) as e:
        fn()
    return e.value.code


def test_dev_token_round_trip(db, settings):
    v = TokenVerifier(settings)
    who = authenticate(db, v, v.issue_dev_token(RECEPTION))
    assert (who.hospital_id, who.role, who.subject) == ("H001", "receptionist", "REC-001")


def test_scenario_10_expired_tampered_or_foreign_tokens_are_rejected(db, settings):
    v = TokenVerifier(settings)
    good = v.issue_dev_token(RECEPTION)
    tampered = good[:-4] + ("AAAA" if not good.endswith("AAAA") else "BBBB")
    expired = v.issue_dev_token(RECEPTION, ttl_seconds=-10)
    claims = jwt.decode(good, options={"verify_signature": False})
    elevated = jwt.encode({**claims, "role": "admin"}, "guessed-secret-guessed-secret-guessed", algorithm="HS256")
    wrong_aud = jwt.encode({**claims, "aud": "someone-else"}, settings.dev_jwt_secret, algorithm="HS256")
    for bad in (tampered, expired, elevated, wrong_aud, "", "not-a-jwt"):
        assert _code(lambda: authenticate(db, v, bad)) == Codes.UNAUTHENTICATED


def test_unknown_user_gets_clear_error(db, settings):
    v = TokenVerifier(settings)
    with pytest.raises(HospitalError) as e:
        authenticate(db, v, v.issue_dev_token("stranger@gmail.com"))
    assert e.value.code == Codes.UNAUTHENTICATED and "not registered" in e.value.message


def test_scenario_12_deactivated_user_fails_on_next_call(db, settings, as_user):
    v = TokenVerifier(settings)
    token = v.issue_dev_token(RECEPTION)
    authenticate(db, v, token)
    two_step(as_user(ADMIN), "deactivate_staff_user", subject="REC-001")
    assert _code(lambda: authenticate(db, v, token)) == Codes.UNAUTHENTICATED


def test_role_comes_from_database_not_token(db, settings, as_user):
    v = TokenVerifier(settings)
    token = v.issue_dev_token(RECEPTION)
    two_step(as_user(ADMIN), "change_staff_role", subject="REC-001", new_role="billing")
    assert authenticate(db, v, token).role == "billing"   # same token, new role immediately


# ---------------------------------------------------------------- Auth0 (RS256 + JWKS), simulated
@pytest.fixture
def auth0(settings, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    s = replace(settings, auth_mode="auth0", auth0_domain="tenant.example.auth0.com",
                auth0_audience="https://mcp.example.com/mcp")

    class FakeJWKS:
        def get_signing_key_from_jwt(self, token):
            return type("K", (), {"key": key.public_key()})()

    monkeypatch.setattr(ident_mod, "_jwks_client", lambda domain: FakeJWKS())

    def mint(email=ADMIN, sub="auth0|abc123", **over):
        now = int(time.time())
        claims = {"sub": sub, s.email_claim: email, "iss": f"https://{s.auth0_domain}/", "aud": s.auth0_audience,
                  "iat": now, "exp": now + 600, **over}
        return jwt.encode(claims, key, algorithm="RS256")
    return s, mint


def test_auth0_token_links_by_email_then_by_subject(db, auth0):
    s, mint = auth0
    v = TokenVerifier(s)
    who = authenticate(db, v, mint())
    assert who.role == "admin" and who.hospital_id == "H001"
    assert db.staff_users.find_one({"email": ADMIN})["auth_sub"] == "auth0|abc123"
    # later tokens without the email claim still work through the linked subject
    assert authenticate(db, v, mint(email=None)).subject == "ADM-001"
    # another IdP account claiming the same e-mail cannot take over the linked account
    assert _code(lambda: authenticate(db, v, mint(sub="auth0|attacker"))) == Codes.UNAUTHENTICATED


def test_auth0_checks_issuer_audience_and_expiry(db, auth0):
    s, mint = auth0
    v = TokenVerifier(s)
    for bad in (mint(iss="https://evil.example/"), mint(aud="https://other-api/"), mint(exp=int(time.time()) - 5)):
        assert _code(lambda: authenticate(db, v, bad)) == Codes.UNAUTHENTICATED
    with pytest.raises(RuntimeError):
        v.issue_dev_token(ADMIN)   # no dev tokens in auth0 mode
