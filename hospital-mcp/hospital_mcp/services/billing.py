"""Billing and insurance. The payment gateway and the insurer are mocks (plan s.1)."""
import re
import uuid
from typing import List, Optional

from ..authz import authorize, invoice_status_only
from ..db import DuplicateKeyError
from ..errors import Codes, HospitalError
from .common import bad, clean, inr, not_found, to_minor


def _invoice(ctx, invoice_id: str) -> dict:
    inv = ctx.t.invoices.find_one({"invoice_id": invoice_id})
    if not inv:
        raise not_found(f"Invoice '{invoice_id}'")
    return inv


def get_invoice(ctx, invoice_id: str) -> dict:
    d = authorize(ctx.ident, "get_invoice")
    inv = _invoice(ctx, invoice_id)
    ctx.audit("get_invoice", invoice_id)
    if d.view == "status":
        return invoice_status_only(inv)
    p = ctx.t.patients.find_one({"mrn": inv["mrn"]}) or {}
    out = clean({k: inv.get(k) for k in ("invoice_id", "mrn", "appointment_id", "items", "status", "created_at")})
    out["patient_name"] = p.get("name")  # billing gets name only; demographics stay masked
    out.update(total_inr=inr(inv["total_minor"]), paid_inr=inr(inv["paid_minor"]),
               balance_inr=inr(inv["total_minor"] - inv["paid_minor"]))
    for item in out.get("items", []):
        item["unit_inr"] = inr(item.pop("unit_minor", 0))
        item["line_inr"] = inr(item.pop("line_minor", 0))
    return out


def get_claim_status(ctx, invoice_id: str) -> dict:
    authorize(ctx.ident, "get_claim_status")
    _invoice(ctx, invoice_id)
    claim = ctx.t.claims.find_one({"invoice_id": invoice_id})
    ctx.audit("get_claim_status", invoice_id)
    return clean(claim) if claim else {"invoice_id": invoice_id, "status": "no_claim"}


def create_invoice(ctx, mrn: str, items: List[dict], appointment_id: Optional[str] = None,
                   request_id: Optional[str] = None) -> dict:
    """items: [{"description": "Consultation", "amount_inr": 800, "quantity": 1}]"""
    authorize(ctx.ident, "create_invoice")
    from .pending import idempotent

    def run():
        p = ctx.t.patients.find_one({"mrn": mrn})
        if not p:
            raise not_found(f"Patient '{mrn}'")
        if appointment_id:
            a = ctx.t.appointments.find_one({"appointment_id": appointment_id})
            if not a or a["mrn"] != mrn:
                raise bad("appointment_id does not belong to this patient.")
        if not isinstance(items, list) or not 1 <= len(items) <= 50:
            raise bad("items must be a list of 1-50 lines.")
        lines = []
        for i in items:
            desc, amount, qty = i.get("description"), i.get("amount_inr"), i.get("quantity", 1)
            if not isinstance(desc, str) or not 2 <= len(desc) <= 200:
                raise bad("each item needs a description of 2-200 characters.")
            if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not 0 < amount <= 10_000_000:
                raise bad("amount_inr must be a positive number.")
            if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= 1000:
                raise bad("quantity must be 1-1000.")
            unit = to_minor(amount)
            lines.append({"description": desc, "quantity": qty, "unit_minor": unit, "line_minor": unit * qty})
        inv = {"invoice_id": f"INV-{ctx.clock():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}", "mrn": mrn,
               "appointment_id": appointment_id, "items": lines, "total_minor": sum(l["line_minor"] for l in lines),
               "paid_minor": 0, "status": "issued", "created_by": ctx.ident.subject, "created_at": ctx.clock()}
        ctx.t.invoices.insert_one(dict(inv))
        if p.get("insurance"):
            ctx.t.claims.insert_one({"invoice_id": inv["invoice_id"], "mrn": mrn,
                                     "provider_id": p["insurance"]["provider_id"], "status": "not_submitted",
                                     "created_at": ctx.clock()})
        ctx.audit("create_invoice", inv["invoice_id"], after={"total_inr": inr(inv["total_minor"])})
        return {"invoice_id": inv["invoice_id"], "total_inr": inr(inv["total_minor"]), "status": "issued"}

    return idempotent(ctx, "create_invoice", request_id, run)


def mock_gateway_verify(gateway_ref: str) -> dict:
    """Mock gateway: PAY-<paise> succeeds for that amount (PAY-150000 -> INR 1500.00), anything else fails."""
    m = re.fullmatch(r"PAY-(\d{1,10})", gateway_ref or "")
    return {"status": "captured", "amount_minor": int(m.group(1))} if m else {"status": "failed", "amount_minor": 0}


def record_payment(ctx, invoice_id: str, gateway_ref: str) -> dict:
    authorize(ctx.ident, "record_payment")
    inv = _invoice(ctx, invoice_id)
    if inv["status"] == "paid":
        raise bad("Invoice is already fully paid.")
    gw = mock_gateway_verify(gateway_ref)
    if gw["status"] != "captured":
        raise bad("The payment gateway did not confirm this payment.", hint="Mock format: PAY-<amount in paise>.")
    balance = inv["total_minor"] - inv["paid_minor"]
    if gw["amount_minor"] > balance:
        raise bad(f"Payment exceeds the balance of INR {inr(balance):.2f}.")
    try:
        ctx.t.payments.insert_one({"gateway_ref": gateway_ref, "invoice_id": invoice_id,
                                   "amount_minor": gw["amount_minor"], "recorded_by": ctx.ident.subject,
                                   "at": ctx.clock()})
    except DuplicateKeyError:
        raise HospitalError(Codes.DUPLICATE, "This gateway_ref was already recorded.")
    res = ctx.t.invoices.find_one_and_update(
        {"invoice_id": invoice_id, "paid_minor": inv["paid_minor"]},
        {"$inc": {"paid_minor": gw["amount_minor"]}}, return_document=True)
    if not res:
        ctx.t.payments.delete_one({"gateway_ref": gateway_ref})
        raise HospitalError(Codes.IN_PROGRESS, "The invoice changed meanwhile; retry.")
    status = "paid" if res["paid_minor"] == res["total_minor"] else "partial"
    ctx.t.invoices.update_one({"invoice_id": invoice_id}, {"$set": {"status": status}})
    ctx.audit("record_payment", invoice_id, after={"paid_inr": inr(res["paid_minor"]), "status": status})
    return {"invoice_id": invoice_id, "status": status, "paid_inr": inr(res["paid_minor"]),
            "balance_inr": inr(res["total_minor"] - res["paid_minor"])}


def check_insurance_coverage(ctx, provider: str, policy_no: Optional[str] = None,
                             category: Optional[str] = None) -> dict:
    """provider: insurer id or name. Public callers only learn whether the insurer is accepted."""
    d = authorize(ctx.ident, "check_insurance_coverage")
    rx = {"$regex": f"^{re.escape(provider.strip())}", "$options": "i"}
    ins = ctx.t.insurers.find_one({"$or": [{"provider_id": provider}, {"name": rx}]})
    if not ins:
        return {"provider": provider, "accepted": False, "note": "This insurer is not on the hospital's list."}
    out = {"provider_id": ins["provider_id"], "provider_name": ins["name"], "accepted": ins.get("accepted", False),
           "cashless": ins.get("cashless", False)}
    if category:
        cat = category.lower()
        out["category"] = cat
        out["category_covered"] = cat in ins.get("covered", []) and cat not in ins.get("exclusions", [])
    if d.view == "accepted_only":
        out["note"] = "For policy-specific details, please contact the billing desk."
        return out
    out.update(covered_categories=ins.get("covered", []), exclusions=ins.get("exclusions", []),
               preauth_required_above_inr=inr(ins.get("preauth_above_minor")), tpa_contact=ins.get("tpa_contact"))
    if policy_no:
        pol = ctx.t.policies.find_one({"provider_id": ins["provider_id"], "policy_no": policy_no})
        if not pol:
            out["policy_found"] = False
        else:
            out.update(policy_found=True, policy_active=pol.get("status") == "active" and
                       pol.get("valid_till", "") >= ctx.clock().date().isoformat(),
                       valid_till=pol.get("valid_till"), copay_percent=pol.get("copay_percent", 0),
                       sum_insured_inr=inr(pol.get("sum_insured_minor")))
    ctx.audit("check_insurance_coverage", ins["provider_id"])
    return out


PROCEDURES = {
    "APP001": ("Appendectomy", 6_000_000, "surgery"),
    "CAT001": ("Cataract surgery", 4_000_000, "surgery"),
    "MRI001": ("MRI brain", 800_000, "diagnostics"),
    "KNE001": ("Knee replacement", 25_000_000, "surgery"),
    "ANG001": ("Coronary angioplasty", 30_000_000, "surgery"),
    "COS001": ("Cosmetic laser therapy", 5_000_000, "cosmetic"),
}


def mock_insurer_decide(provider: dict, policy: dict, code: str, today: str) -> dict:
    if code not in PROCEDURES:
        return {"decision": "denied", "reason": f"Unknown procedure code. Known: {', '.join(PROCEDURES)}"}
    name, est, cat = PROCEDURES[code]
    if policy.get("status") != "active" or policy.get("valid_till", "") < today:
        return {"decision": "denied", "reason": "Policy not active"}
    if cat in provider.get("exclusions", []) or cat not in provider.get("covered", []):
        return {"decision": "denied", "reason": f"{cat} is not covered"}
    if est > policy.get("sum_insured_minor", 0):
        return {"decision": "denied", "reason": "Exceeds sum insured"}
    if est > 10_000_000:
        return {"decision": "pending_review", "reason": "High value: manual review", "procedure": name}
    return {"decision": "approved", "approved_inr": inr(est), "procedure": name}


def submit_preauthorization(ctx, mrn: str, procedure_code: str) -> dict:
    authorize(ctx.ident, "submit_preauthorization", mrn=mrn, ctx=ctx)
    p = ctx.t.patients.find_one({"mrn": mrn})
    if not p:
        raise not_found(f"Patient '{mrn}'")
    if not p.get("insurance"):
        raise bad("No insurance on file for this patient.")
    ins = ctx.t.insurers.find_one({"provider_id": p["insurance"]["provider_id"], "accepted": True})
    pol = ctx.t.policies.find_one({"provider_id": p["insurance"]["provider_id"],
                                   "policy_no": p["insurance"]["policy_no"]})
    if not ins or not pol:
        raise bad("Insurer or policy not recognised.")
    decision = mock_insurer_decide(ins, pol, procedure_code.upper(), ctx.clock().date().isoformat())
    rec = {"preauth_id": f"PA-{uuid.uuid4().hex[:8].upper()}", "mrn": mrn, "procedure_code": procedure_code.upper(),
           "provider_id": ins["provider_id"], "submitted_by": ctx.ident.subject, "submitted_at": ctx.clock(),
           **decision}
    ctx.t.preauths.insert_one(dict(rec))
    ctx.audit("submit_preauthorization", rec["preauth_id"], after={"decision": decision["decision"]})
    return clean(rec)
