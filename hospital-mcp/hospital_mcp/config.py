"""Settings from environment variables (and a local .env file, if present).

Secrets never live in code. Copy .env.example to .env and fill it in.
"""
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Tiny .env loader (KEY=VALUE lines). Real environment variables win over the file."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    # database: a mongodb:// or mongodb+srv:// URI, or "memory://" for the in-memory store (tests, quick demos)
    mongo_uri: str
    mongo_db: str
    tz: str
    # auth_mode: "auth0" (real sign-in) or "dev" (local signed tokens, never for deployment)
    auth_mode: str
    auth0_domain: Optional[str]
    auth0_audience: Optional[str]          # the API identifier in Auth0 = public URL of the MCP endpoint
    auth0_client_id: Optional[str]         # web app (Regular Web Application) client
    auth0_client_secret: Optional[str]
    email_claim: str                       # custom claim the Auth0 Action adds to access tokens
    dev_jwt_secret: str
    dev_email: Optional[str]               # stdio dev identity
    # MCP server
    mcp_transport: str
    mcp_host: str
    mcp_port: int
    mcp_public_url: str                    # e.g. https://hospital-mcp.onrender.com
    # web app
    web_secret: str
    web_base_url: str
    web_port: int
    # LLM
    groq_api_key: Optional[str]
    openrouter_api_key: Optional[str]
    llm_models: str                        # comma list of provider:model; empty = defaults for the keys present

    @property
    def is_dev(self) -> bool:
        return self.auth_mode == "dev"


def get_settings() -> Settings:
    load_dotenv()
    e = os.getenv
    auth_mode = e("AUTH_MODE", "dev").lower()
    if auth_mode not in ("dev", "auth0"):
        raise RuntimeError("AUTH_MODE must be 'dev' or 'auth0'.")
    mcp_public = e("MCP_PUBLIC_URL", f"http://localhost:{e('MCP_PORT', '8000')}").rstrip("/")
    s = Settings(
        mongo_uri=e("MONGO_URI", "mongodb://localhost:27017/?replicaSet=rs0"),
        mongo_db=e("MONGO_DB", "hospital_mcp"),
        tz=e("HOSPITAL_TZ", "Asia/Kolkata"),
        auth_mode=auth_mode,
        auth0_domain=e("AUTH0_DOMAIN"),
        auth0_audience=e("AUTH0_AUDIENCE", f"{mcp_public}/mcp"),
        auth0_client_id=e("AUTH0_CLIENT_ID"),
        auth0_client_secret=e("AUTH0_CLIENT_SECRET"),
        email_claim=e("AUTH0_EMAIL_CLAIM", "https://hospital-mcp/email"),
        dev_jwt_secret=e("DEV_JWT_SECRET", "dev-only-secret-change-me-0123456789abcdef"),
        dev_email=e("DEV_EMAIL"),
        mcp_transport=e("MCP_TRANSPORT", "streamable-http"),
        mcp_host=e("MCP_HOST", "127.0.0.1"),
        mcp_port=int(e("PORT", e("MCP_PORT", "8000"))),
        mcp_public_url=mcp_public,
        web_secret=e("WEB_SECRET_KEY", ""),
        web_base_url=e("WEB_BASE_URL", f"http://localhost:{e('WEB_PORT', '5000')}").rstrip("/"),
        web_port=int(e("WEB_PORT", "5000")),
        groq_api_key=e("GROQ_API_KEY"),
        openrouter_api_key=e("OPENROUTER_API_KEY"),
        llm_models=e("LLM_MODELS", ""),
    )
    if s.auth_mode == "auth0" and not s.auth0_domain:
        raise RuntimeError("AUTH_MODE=auth0 needs AUTH0_DOMAIN (and AUTH0_AUDIENCE).")
    return s
