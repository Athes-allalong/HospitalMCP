"""Service layer: plain Python business logic plus the authorization checks (plan s.2).
Every function takes a Ctx first. It is independent of MCP, Flask and the LLM, so no caller can
bypass the checks."""
