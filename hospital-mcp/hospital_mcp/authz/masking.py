"""Central PII shaping. Tools call these instead of masking ad hoc."""
from typing import Optional


def mask_phone(p: Optional[str]) -> Optional[str]:
    return None if not p else "*" * max(len(p) - 4, 0) + p[-4:]


def mask_email(e: Optional[str]) -> Optional[str]:
    if not e or "@" not in e:
        return None
    user, domain = e.split("@", 1)
    return f"{user[:1]}***@{domain}"


def shape_patient_summary(p: dict, view: str) -> dict:
    """view: 'full' (clinical roles), 'demographic' (front desk: no clinical fields), 'masked' (billing, admin)."""
    full, masked = view == "full", view == "masked"
    ins = p.get("insurance")
    out = {
        "mrn": p["mrn"],
        "name": p["name"],
        "sex": p.get("sex"),
        "dob": (p.get("dob") or "")[:4] if masked else p.get("dob"),
        "phone": mask_phone(p.get("phone")) if masked else p.get("phone"),
        "email": mask_email(p.get("email")) if masked else p.get("email"),
        "insurance": (
            {"provider_id": ins["provider_id"],
             "policy_no": mask_phone(ins["policy_no"]) if masked else ins["policy_no"]}
            if ins else None
        ),
        "view": view,
    }
    if full:
        out["blood_group"] = p.get("blood_group")
        out["allergies"] = p.get("allergies", [])
    return out


def invoice_status_only(inv: dict) -> dict:
    """Front desk sees whether an invoice is paid, not its line items."""
    return {"invoice_id": inv["invoice_id"], "status": inv.get("status")}
