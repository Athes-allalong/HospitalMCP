"""Regenerate docs/PERMISSIONS.md from the permission matrix: python scripts/gen_permissions.py"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from hospital_mcp.authz import MATRIX, ROLES  # noqa: E402
from hospital_mcp.tools import REGISTRY  # noqa: E402

lines = ["# Permission matrix", "",
         "Generated from `hospital_mcp/authz/matrix.py`, the single source of truth. `tests/test_matrix.py` checks "
         "every cell. After editing the matrix, run `python scripts/gen_permissions.py`.", "",
         "Legend: ✅ full · 🔒 restricted (view and/or record scope) · blank = denied. "
         "Hospital isolation applies underneath every cell.", "",
         "| Tool | Kind | " + " | ".join(c.replace("_", " ") for c in ROLES) + " |",
         "|---|---|" + "---|" * len(ROLES)]
for t in REGISTRY:
    row = []
    for c in ROLES:
        r = MATRIX[t].get(c)
        if r is None:
            row.append("")
        elif r.view == "full" and not r.scope:
            row.append("✅")
        else:
            row.append("🔒 " + ", ".join(x for x in (r.view if r.view != "full" else "", r.scope or "") if x))
    lines.append(f"| `{t}` | {REGISTRY[t].kind} | " + " | ".join(row) + " |")
out = pathlib.Path(__file__).resolve().parent.parent / "docs" / "PERMISSIONS.md"
out.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"wrote {out}")
