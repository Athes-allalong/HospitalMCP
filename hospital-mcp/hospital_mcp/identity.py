"""Who is calling. Identity comes from a verified token, never from tool arguments.

Two token sources:
  auth0  RS256 access tokens issued by Auth0 for our API (audience = the MCP URL). Checked for
         signature (JWKS), expiry, issuer and audience. Used by the web app and by Claude's
         remote connector (desktop, web and phone).
  dev    HS256 tokens signed with DEV_JWT_SECRET. Same checks. For local development and tests only.

A valid token is not enough: the token's user must map to an ACTIVE row in staff_users, and that
row's hospital must be active. Role, department and hospital are read from that row on every call,
so deactivating a user or changing a role takes effect on their very next call.
"""
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Optional

import jwt

from .config import Settings
from .errors import Codes, HospitalError

DEV_ISSUER = "hospital-mcp-dev"
DEV_AUDIENCE = "hospital-mcp"


@dataclass(frozen=True)
class Identity:
    hospital_id: Optional[str]      # None only for platform_admin
    role: str
    subject: str                    # staff_id / user code inside the hospital, "anonymous" for public
    email: str = ""
    name: str = ""
    department_id: Optional[str] = None

    @property
    def is_public(self) -> bool:
        return self.role == "public"


def public_identity(hospital_id: str, visitor_id: str = "anonymous") -> Identity:
    """Anonymous website visitor. visitor_id only separates rate-limit buckets; it grants nothing."""
    return Identity(hospital_id=hospital_id, role="public", subject=f"visitor:{visitor_id[:16]}",
                    name="Website visitor")


def _unauth(msg: str) -> HospitalError:
    return HospitalError(Codes.UNAUTHENTICATED, msg, hint="Sign in again.")


@lru_cache(maxsize=4)
def _jwks_client(domain: str) -> "jwt.PyJWKClient":
    return jwt.PyJWKClient(f"https://{domain}/.well-known/jwks.json", cache_keys=True, lifespan=3600)


class TokenVerifier:
    def __init__(self, settings: Settings):
        self.s = settings

    def verify(self, token: str) -> dict:
        """Return the token's claims, or raise UNAUTHENTICATED."""
        if not token:
            raise _unauth("A bearer token is required.")
        try:
            if self.s.auth_mode == "auth0":
                key = _jwks_client(self.s.auth0_domain).get_signing_key_from_jwt(token).key
                return jwt.decode(token, key, algorithms=["RS256"], audience=self.s.auth0_audience,
                                  issuer=f"https://{self.s.auth0_domain}/",
                                  options={"require": ["exp", "iss", "aud", "sub"]})
            return jwt.decode(token, self.s.dev_jwt_secret, algorithms=["HS256"], audience=DEV_AUDIENCE,
                              issuer=DEV_ISSUER, options={"require": ["exp", "iss", "aud", "sub"]})
        except jwt.ExpiredSignatureError:
            raise _unauth("Your session has expired.")
        except jwt.PyJWTError as e:
            raise _unauth(f"Invalid token ({type(e).__name__}).")

    def issue_dev_token(self, email: str, ttl_seconds: int = 8 * 3600) -> str:
        if self.s.auth_mode != "dev":
            raise RuntimeError("Dev tokens are disabled when AUTH_MODE=auth0.")
        now = int(time.time())
        claims = {"sub": f"dev|{email.lower()}", self.s.email_claim: email.lower(), "iss": DEV_ISSUER,
                  "aud": DEV_AUDIENCE, "iat": now, "exp": now + ttl_seconds}
        return jwt.encode(claims, self.s.dev_jwt_secret, algorithm="HS256")


def resolve_identity(db: Any, claims: dict, settings: Settings) -> Identity:
    """Map verified claims to an active staff user (first login links the IdP subject by e-mail)."""
    sub = claims.get("sub")
    email = (claims.get(settings.email_claim) or claims.get("email") or "").lower()
    dev = bool(sub) and sub.startswith("dev|")
    user = db.staff_users.find_one({"auth_sub": sub}) if sub and not dev else None
    if user is None and email:
        user = db.staff_users.find_one({"email": email})
        if user is not None and not dev:
            if user.get("auth_sub") not in (None, sub):
                raise _unauth("This e-mail is linked to a different sign-in account.")
            db.staff_users.update_one({"_id": user["_id"]}, {"$set": {"auth_sub": sub}})
    if user is None:
        raise _unauth("Your account is not registered as staff at any hospital on this platform. "
                      "Ask your hospital admin to add you.")
    if not user.get("active"):
        raise _unauth("Your staff account is deactivated.")
    if user["role"] != "platform_admin":
        hosp = db.hospitals.find_one({"hospital_id": user.get("hospital_id")})
        if not hosp or not hosp.get("active", True):
            raise _unauth("Your hospital's account is not active on this platform.")
    return Identity(hospital_id=user.get("hospital_id"), role=user["role"], subject=user["subject"],
                    email=user["email"], name=user.get("name", ""), department_id=user.get("department_id"))


def authenticate(db: Any, verifier: TokenVerifier, token: str) -> Identity:
    return resolve_identity(db, verifier.verify(token), verifier.s)
