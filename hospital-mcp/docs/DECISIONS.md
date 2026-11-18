# Decision log

Answers to the plan's "open decisions for week 1" (section 16), plus the choices made in this rebuild.

| # | Decision | Chosen | Why |
|---|---|---|---|
| 1 | Identity provider | **Auth0** (free tier); `AUTH_MODE=dev` for local work | Hosted, supports Dynamic Client Registration and RFC 8707 resource indicators, which Claude's connector flow needs. No custom OAuth server (plan s.3.4). |
| 2 | Multi-tenancy | **Shared database, `hospital_id` on every record** | One deployment serves many hospitals. Enforced by `tenancy.TenantDB` under every tool; the caller's hospital comes from their staff record, never from arguments. Unique indexes start with `hospital_id`. |
| 3 | Tool names | The plan's (`search_doctors`, `check_availability`) | One set, used by MCP, the web portal and the assistant. |
| 4 | Client targets | Custom assistant (Groq) **and** Claude remote connector | Claude's connector also covers the mobile app. ChatGPT connectors use the same OAuth flow (untested). |
| 5 | LLM for the in-app assistant | **Groq** (free) | Anthropic API is optional. The assistant calls the same tool catalogue in-process with the signed-in user's context, so every check still applies. |
| 6 | Web UI | **Kept Flask** (teammate's templates and styling), rebuilt on the service layer | Pages no longer read MongoDB directly; they call the same tools as MCP. |
| 7 | Patients | Data only, as decided | `patient` role not in the matrix (denied everywhere). Adding it later = one matrix column. |
| 8 | Break-glass access | Deferred | Small to add on top of `authorize()`; not in core scope. |
| 9 | Clinical ML | Deferred | As planned. |
| 10 | Compliance framing | DPDP as design basis, HIPAA as checklist | Synthetic data only. |
| 11 | Public chatbot scope | Directory, hospital info, availability, facility **summaries**, insurer accepted yes/no | See `docs/PERMISSIONS.md`, `public` column. |
| 12 | Unauthorized record access | **FORBIDDEN for scoped roles whether or not the MRN exists** | The care-relationship check runs before the lookup, so a doctor cannot probe which MRNs exist. A role-level denial happens before any service code runs. |
| 13 | Cancellation window | Allowed until the slot starts; under 2 hours it is recorded as `late_cancellation` | Simple, visible, tested. |
| 14 | Slot grid | 15-minute grid; `new` = 30 min, `followup`/`teleconsult` = 15 min | Overlaps blocked by per-cell locks for doctor **and** patient, plus unique partial indexes. |
| 15 | Preview-then-commit | All admin and platform tools, book / reschedule / cancel | Contact updates and facility numbers are single-step (low risk), with before/after in the audit log. In the web assistant the confirmation id never reaches the model. |
| 16 | Time storage | Naive hospital-local time (as the original code) | Deviation from the plan's "store UTC". Inputs with an offset are converted. Switching to UTC is contained in `services/common.py` if needed. |
| 17 | MCP SDK version | Works with SDK 2.x (`MCPServer`) and 1.x (`FastMCP`) | Verified end to end on 2.3 over streamable HTTP and stdio. |

## Not done yet (honest list)

- **Scraper rewrite.** The old `scraper.py` could not read the live site (wrong CSS selectors) and fell back to
  invented departments. The seed now uses an accurate snapshot of the site taken 16 Nov 2026
  (`hospital_mcp/seed.py`, `SITE`), with every generated field labelled in `data_source`. A scraper that
  refreshes this snapshot (`trigger_rescrape`, `list_scrape_runs`) is the next Person 1 task.
- **Website reads MongoDB.** The dummy site is still a separate static site.
- **Integration test against a real MongoDB** (tests use the in-memory store, which implements the same unique
  and partial-index semantics). Run `docker compose up` and the CLI seed before demo day.
- **Rate limits and chat history live in process memory.** Fine for one instance; use Redis for several.
- Second remote connector (ChatGPT), break-glass, waitlist, field-level encryption: stretch items.
