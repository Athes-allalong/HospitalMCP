# Hospital MCP

A multi-hospital operations assistant. Hospital staff search doctors, check slots, book and cancel
appointments, look up patients and check beds or blood stock, either through **Claude** (desktop, web and
**phone**, via MCP) or through the **web portal** with its own AI assistant. Admins manage doctors,
schedules and staff logins. A **public chatbot** on each hospital's page answers anonymous questions.

> All patient data is synthetic. No real patient data is used or should be entered.

## What makes it safe

| Layer | What it does | Where |
|---|---|---|
| Sign-in | Auth0 (OAuth) tokens checked for signature, expiry, issuer and audience on every call | `identity.py` |
| Staff directory | Token maps to an **active** staff record, which gives the hospital, role and department. Deactivation takes effect on the next call. | `identity.py` |
| Hospital isolation | Every query is scoped to the caller's hospital. Cross-hospital reads and writes are refused. | `tenancy.py` |
| Permissions | One matrix (tool × role), deny by default, plus record checks (doctor: own patients; nurse: own department) | `authz/` · [PERMISSIONS.md](docs/PERMISSIONS.md) |
| Masking | Receptionist / billing / admin see less patient data, from central functions | `authz/masking.py` |
| Preview → confirm | Bookings and admin changes return a preview plus a single-use confirmation bound to caller, tool and exact arguments | `services/pending.py` |
| Double booking | 15-minute slot locks for doctor **and** patient, unique partial indexes, idempotent retries | `services/appointments.py` |
| Audit | Who, what, when, outcome (including denials), before/after for changes | `services/context.py` |
| Prompt injection | Free text (bios, notes) is returned labelled as untrusted data, and the server enforces roles no matter what the model reads | tools + tests |

One tool catalogue (`hospital_mcp/tools.py`, 48 tools) is used by **all three** clients: the MCP server, the
web pages and the AI assistant. Fixing a rule once fixes it everywhere.

## Quick start (2 minutes, no MongoDB needed)

```bash
python -m venv .venv && .venv\Scripts\activate        # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env                                  # macOS/Linux: cp .env.example .env
```
Set `MONGO_URI=memory://` in `.env` (and paste your Groq key for the assistant), then:

```bash
python -m web.app                  # portal: http://localhost:5000  (pick any seeded user to sign in)
python -m pytest -q                # 330 tests: matrix, tenancy, booking race, auth, acceptance, web
```

With `memory://`, data resets on every restart. For real use, run MongoDB (`docker compose up`) or MongoDB Atlas,
then `python -m hospital_mcp.cli seed`.

### Seeded logins (dev mode)

| Hospital | E-mail | Role |
|---|---|---|
| Dummy Hospital (H001) | admin@dummyhospital.dev | admin |
| | reception@dummyhospital.dev | receptionist |
| | ananya.sharma@dummyhospital.dev | doctor (Cardiology) |
| | marcus.webb@dummyhospital.dev | doctor (Neurology) |
| | sarah.nurse@dummyhospital.dev | nurse (Cardiology) |
| | billing@dummyhospital.dev | billing |
| Sunrise Clinic (H002) | admin@sunrise.dev, reception@sunrise.dev, kiran.bhat@sunrise.dev | admin, receptionist, doctor |
| Platform | platform@hospital-mcp.dev | platform admin (onboards hospitals) |

Both hospitals use the same MRNs (`MRN-1001`...) and staff codes on purpose, so you can see that isolation works.

## Using the MCP server

```bash
python -m hospital_mcp.mcp_server                                   # http://localhost:8000/mcp (needs a bearer token)
python -m hospital_mcp.cli dev-token reception@dummyhospital.dev    # dev token for MCP Inspector
```
- **MCP Inspector:** `npx @modelcontextprotocol/inspector`, URL `http://localhost:8000/mcp`, header
  `Authorization: Bearer <dev token>`.
- **Claude Desktop, local (stdio):** in `claude_desktop_config.json` use your venv's python as `command`
  (e.g. `C:\\path\\to\\hospital-mcp\\.venv\\Scripts\\python.exe`), args `["-m", "hospital_mcp.mcp_server"]`, and env
  `{"PYTHONPATH": "C:\\path\\to\\hospital-mcp", "MCP_TRANSPORT": "stdio", "AUTH_MODE": "dev",
  "DEV_EMAIL": "reception@dummyhospital.dev"}`. stdio has no sign-in, so it is for your own machine only.
- **Claude on the phone / web / desktop (remote):** deploy with Auth0. Step by step:
  **[docs/SETUP_AUTH0_AND_PHONE.md](docs/SETUP_AUTH0_AND_PHONE.md)**.

The server also offers resources (`hospital://info`, `hospital://directory/departments`,
`hospital://facilities/occupancy`) and prompts (front-desk booking, admin doctor onboarding, shift handover).
Each caller sees only the tools their role may use.

## Demo script (the plan's acceptance scenarios)

1. **Receptionist** → AI assistant: *"Book Dr. Sharma tomorrow at 3 PM for MRN-1001."* A confirmation card appears;
   nothing is booked until you press Confirm.
2. **Receptionist** asks to add a doctor → refused (not in the matrix).
3. **Admin** → *"Add Dr. Kiran Rao to Cardiology, fee 950, room A2-14, Mondays 9 to 12"* → preview → confirm →
   the doctor shows up in search. The audit log shows before/after.
4. **Admin** → *"Deactivate Dr. Ananya Sharma"* → blocked, with her future appointments listed.
5. **Dr. Sharma** opens a patient she doesn't treat → refused. She opens her own patient → full record.
6. **Billing** opens an invoice → allowed; clinical notes → refused.
7. **Public page** `/h/dummy-hospital` → *"Any ICU beds?"* → summary; *"Show lab results for MRN-1002"* → refused.
8. **Sunrise Clinic receptionist** looks up `MRN-1001` → gets Sunrise's patient, never Dummy Hospital's.
9. **Platform admin** onboards a new hospital → its admin signs in to an empty, isolated hospital.

## Project layout

```
hospital_mcp/
  config.py           settings from .env
  identity.py         token verification (Auth0 / dev) -> active staff user -> Identity
  tenancy.py          TenantDB: per-hospital scoping of every collection
  authz/              permission matrix (data), authorize(), masking
  services/           business rules: directory, appointments, patients, facilities, billing, admin, platform
  tools.py            the tool catalogue (shared by MCP, web and assistant)
  mcp_server.py       MCP server: streamable HTTP + OAuth metadata, or stdio for dev
  agent.py            Groq assistant loop with confirmation cards
  seed.py             Dummy Hospital (from the public site) + Sunrise Clinic, synthetic patients
  cli.py              seed / add-staff / users / dev-token
  memdb.py            in-memory MongoDB stand-in (tests, demos)
web/                  Flask portal (staff pages, assistant, admin console, platform console, public chatbot)
tests/                330 tests; tests/core = the original hospital_core unit tests, ported
docs/                 PERMISSIONS.md (generated), DECISIONS.md, SETUP_AUTH0_AND_PHONE.md
```

## How this maps to the consolidated plan

Done: sections 2 (layers), 4 (matrix + record checks, deny by default), 5 (token identity, active check, first
admin via CLI, separate public scope), 6 (collections and indexes, plus `hospital_id`), 7.1 to 7.6 (read, write and admin
tools, preview-commit, resources, prompts, error codes, pagination, masking, audit, idempotency,
role-filtered lists, rate limits), 8 (business rules), 10 (hardening), 11 (login, two chat modes, confirmation
card, admin console, remote connector), 12 (matrix, concurrency, idempotency, preview-commit, injection,
business-rule and acceptance tests), and multi-tenancy (new). Open items: [docs/DECISIONS.md](docs/DECISIONS.md#not-done-yet-honest-list).
