"""Web app: staff portal, in-app assistant, admin console, platform console, public website chatbot.

    python -m web.app          (http://localhost:5000)

The pages never touch the database directly. Every page calls the same tool functions as the MCP
server, as the signed-in user, so role checks, record-level checks, masking, hospital isolation and
auditing are identical everywhere.
"""
import logging
import secrets
import threading
import time
import uuid
from datetime import date, timedelta
from functools import wraps
from typing import Any, Optional
from urllib.parse import urlencode

import requests
from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request, session, url_for)

from hospital_mcp import agent
from hospital_mcp.authz import tools_for_role
from hospital_mcp.config import get_settings
from hospital_mcp.db import get_db
from hospital_mcp.errors import Codes, HospitalError
from hospital_mcp.identity import TokenVerifier, authenticate, public_identity
from hospital_mcp.llm import LLMPool
from hospital_mcp.services.context import Ctx
from hospital_mcp.tools import REGISTRY, use_ctx

log = logging.getLogger("hospital-web")

# Tools the HTML forms may submit (each still goes through the full authorization path).
FORM_TOOLS = {"book_appointment", "cancel_appointment", "reschedule_appointment", "register_patient",
              "update_facility_status", "create_staff_user", "change_staff_role", "deactivate_staff_user",
              "create_hospital", "set_hospital_active", "add_schedule_exception"}


class TTLStore:
    """Server-side storage for chat history and pending confirmations (one process).
    With several web workers, swap for Redis or a Mongo collection."""

    def __init__(self, ttl_seconds: int = 3600):
        self.ttl, self._d, self._lock = ttl_seconds, {}, threading.Lock()

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            item = self._d.get(key)
            if not item or item[0] < time.time():
                self._d.pop(key, None)
                return default
            return item[1]

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if len(self._d) > 5000:
                now = time.time()
                for k in [k for k, (exp, _) in self._d.items() if exp < now]:
                    del self._d[k]
            self._d[key] = (time.time() + self.ttl, value)

    def pop(self, key: str) -> Any:
        with self._lock:
            item = self._d.pop(key, None)
            return item[1] if item and item[0] >= time.time() else None


def create_app(db: Any = None, settings=None, clock=None) -> Flask:
    settings = settings or get_settings()
    app = Flask(__name__)
    secret = settings.web_secret
    if not secret:
        if not settings.is_dev:
            raise RuntimeError("Set WEB_SECRET_KEY (a long random string) before running with AUTH_MODE=auth0.")
        secret = "dev-only-" + secrets.token_hex(16)
    app.config.update(SECRET_KEY=secret, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=settings.web_base_url.startswith("https://"),
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=8))
    verifier = TokenVerifier(settings)
    store = TTLStore()
    pool = LLMPool.from_settings(settings)

    def llm_for_turn():
        return pool.turn() if pool.configured else None
    get_database = (lambda: db) if db is not None else get_db

    def make_ctx(ident) -> Ctx:
        kw = {"clock": clock} if clock else {}
        return Ctx(get_database(), ident, request_id=uuid.uuid4().hex[:12], **kw)

    # ------------------------------------------------------------------ helpers
    def sid() -> str:
        if "sid" not in session:
            session["sid"] = secrets.token_urlsafe(16)
        return session["sid"]

    def csrf_token() -> str:
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return session["csrf"]

    def check_csrf() -> None:
        sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf")
        if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Missing or invalid CSRF token. Reload the page and try again.")

    def run(tool_name: str, /, **kwargs) -> tuple:
        """Call a tool as the current user. Returns (result, error_dict)."""
        try:
            with use_ctx(g.ctx):
                return REGISTRY[tool_name].call(**kwargs), None
        except HospitalError as e:
            return None, e.to_dict()

    def can(tool: str) -> bool:
        return getattr(g, "ctx", None) is not None and tool in g.tools

    @app.context_processor
    def inject():
        ident = getattr(g, "ctx", None) and g.ctx.ident
        return {"csrf_token": csrf_token, "can": can, "me": ident, "hospital": getattr(g, "hospital", None),
                "auth_mode": settings.auth_mode, "today": (clock() if clock else None)}

    @app.before_request
    def load_user():
        csrf_token()
        g.ctx = None
        g.tools = set()
        token = session.get("token")
        if not token:
            return
        try:
            ident = authenticate(get_database(), verifier, token)
        except HospitalError as e:
            session.pop("token", None)
            flash(e.message, "warning")
            return
        g.ctx = make_ctx(ident)
        g.tools = set(tools_for_role(ident.role))
        g.hospital = get_database().hospitals.find_one({"hospital_id": ident.hospital_id}) if ident.hospital_id else None

    def login_required(fn):
        @wraps(fn)
        def wrapper(*a, **k):
            if g.ctx is None:
                return redirect(url_for("login", next=request.path))
            if request.method == "POST":
                check_csrf()
            return fn(*a, **k)
        return wrapper

    def show_error(err: Optional[dict]) -> None:
        if err:
            flash(f"{err['message']}" + (f" ({err.get('hint')})" if err.get("hint") else ""),
                  "warning" if err["error"] in (Codes.FORBIDDEN, Codes.NOT_FOUND) else "danger")

    # ------------------------------------------------------------------ auth
    @app.route("/login")
    def login():
        if g.ctx:
            return redirect(url_for("home"))
        nxt = request.args.get("next", "/")
        session["next"] = nxt if nxt.startswith("/") and not nxt.startswith("//") else "/"
        if settings.auth_mode == "auth0":
            state = secrets.token_urlsafe(24)
            session["oauth_state"] = state
            q = {"response_type": "code", "client_id": settings.auth0_client_id,
                 "redirect_uri": f"{settings.web_base_url}/callback", "scope": "openid profile email",
                 "audience": settings.auth0_audience, "state": state}
            return redirect(f"https://{settings.auth0_domain}/authorize?{urlencode(q)}")
        users = list(get_database().staff_users.find({"active": True}).sort([("hospital_id", 1), ("role", 1)]))
        hospitals = {h["hospital_id"]: h["name"] for h in get_database().hospitals.find()}
        return render_template("login.html", users=users, hospitals=hospitals)

    @app.route("/login/dev", methods=["POST"])
    def login_dev():
        if not settings.is_dev:
            abort(404)
        check_csrf()
        email = request.form.get("email", "").strip().lower()
        token = verifier.issue_dev_token(email)
        try:
            authenticate(get_database(), verifier, token)
        except HospitalError as e:
            flash(e.message, "danger")
            return redirect(url_for("login"))
        nxt = session.get("next", "/")
        session.clear()   # new session at sign-in (no session fixation)
        session["token"] = token
        return redirect(nxt or "/")

    @app.route("/callback")
    def callback():
        if settings.auth_mode != "auth0":
            abort(404)
        if request.args.get("error"):
            flash(request.args.get("error_description", "Sign-in was cancelled."), "warning")
            return redirect(url_for("signed_out"))
        if not secrets.compare_digest(request.args.get("state", ""), session.pop("oauth_state", "") or "x"):
            abort(400, "Sign-in state mismatch. Please try again.")
        r = requests.post(f"https://{settings.auth0_domain}/oauth/token", timeout=15, json={
            "grant_type": "authorization_code", "client_id": settings.auth0_client_id,
            "client_secret": settings.auth0_client_secret, "code": request.args.get("code", ""),
            "redirect_uri": f"{settings.web_base_url}/callback"})
        if r.status_code != 200:
            log.warning("Auth0 token exchange failed: %s", r.text[:200])
            abort(400, "Sign-in failed while exchanging the code.")
        token = r.json().get("access_token", "")
        nxt = session.get("next", "/")
        session.clear()
        try:
            authenticate(get_database(), verifier, token)
        except HospitalError as e:
            flash(e.message, "danger")
            return redirect(url_for("signed_out"))
        session["token"] = token
        return redirect(nxt or "/")

    @app.route("/logout")
    def logout():
        session.clear()
        if settings.auth_mode == "auth0":
            q = {"client_id": settings.auth0_client_id, "returnTo": f"{settings.web_base_url}/signed-out"}
            return redirect(f"https://{settings.auth0_domain}/v2/logout?{urlencode(q)}")
        return redirect(url_for("signed_out"))

    @app.route("/signed-out")
    def signed_out():
        hospitals = list(get_database().hospitals.find({"active": True}).sort("name", 1))
        return render_template("signed_out.html", hospitals=hospitals)

    # ------------------------------------------------------------------ staff pages
    @app.route("/")
    def home():
        if g.ctx is None:
            return redirect(url_for("signed_out"))
        if g.ctx.ident.role == "platform_admin":
            return redirect(url_for("platform"))
        cards = {}
        for tool in ("get_icu_occupancy", "get_er_status", "get_blood_inventory"):
            if can(tool):
                cards[tool], _ = run(tool)
        today = g.ctx.clock().date()
        appts = run("list_appointments", date_from=today.isoformat(), date_to=(today + timedelta(days=2)).isoformat(),
                    limit=10)[0] if can("list_appointments") else None
        depts = run("list_departments")[0] or []
        doctors = run("search_doctors", limit=1)[0] or {"total": 0}
        return render_template("dashboard.html", cards=cards, appts=appts, depts=depts, doctor_total=doctors["total"])

    @app.route("/doctors")
    @login_required
    def doctors():
        f = {k: request.args.get(k) or None for k in ("name", "department", "specialty")}
        page = max(int(request.args.get("page", 1) or 1), 1)
        res, err = run("search_doctors", **f, page=page)
        show_error(err)
        return render_template("doctors.html", res=res or {"doctors": [], "total": 0}, f=f, page=page,
                               depts=run("list_departments")[0] or [])

    @app.route("/doctors/<staff_id>")
    @login_required
    def doctor_detail(staff_id):
        prof, err = run("get_doctor_profile", staff_id=staff_id)
        if err:
            show_error(err)
            return redirect(url_for("doctors"))
        start = date.fromisoformat(request.args["from"]) if request.args.get("from") else g.ctx.clock().date()
        typ = request.args.get("type", "new")
        avail, err = run("check_availability", doctor_id=staff_id, start_date=start.isoformat(),
                         end_date=(start + timedelta(days=6)).isoformat(), type=typ)
        show_error(err)
        by_day: dict = {}
        for s in (avail or {}).get("slots", []):
            by_day.setdefault(s[:10], []).append(s)
        return render_template("doctor_detail.html", d=prof, by_day=by_day, typ=typ, start=start,
                               prefill_mrn=request.args.get("mrn", ""))

    @app.route("/departments")
    @login_required
    def departments():
        depts, err = run("list_departments")
        show_error(err)
        status = {}
        for d in depts or []:
            if d.get("kind") == "clinical" and can("get_department_status"):
                status[d["department_id"]] = run("get_department_status", department_id=d["department_id"])[0]
        return render_template("departments.html", depts=depts or [], status=status)

    @app.route("/patients")
    @login_required
    def patients():
        if not can("search_patients"):
            abort(403)
        q = request.args.get("q", "").strip()
        res, err = run("search_patients", query=q, limit=20) if len(q) >= 2 else (None, None)
        show_error(err)
        return render_template("patients.html", q=q, res=res)

    @app.route("/patients/<mrn>")
    @login_required
    def patient_detail(mrn):
        sections = {}
        for tool in ("get_patient_summary", "get_admission_status", "get_appointment_history", "get_patient_history",
                     "list_prescriptions", "get_lab_results"):
            if can(tool):
                sections[tool], err = run(tool, mrn=mrn)
                if err and tool == "get_patient_summary":
                    show_error(err)
                    return redirect(url_for("patients"))
        return render_template("patient_detail.html", mrn=mrn, s=sections)

    @app.route("/appointments")
    @login_required
    def appointments():
        if not can("list_appointments"):
            abort(403)
        today = g.ctx.clock().date()
        d0 = request.args.get("from") or today.isoformat()
        d1 = request.args.get("to") or (today + timedelta(days=7)).isoformat()
        status = request.args.get("status", "booked")
        res, err = run("list_appointments", date_from=d0, date_to=d1, status=status or None, limit=100)
        show_error(err)
        return render_template("appointments.html", res=res, d0=d0, d1=d1, status=status)

    @app.route("/facilities", methods=["GET"])
    @login_required
    def facilities():
        if not can("get_icu_occupancy"):
            abort(403)
        data = {t: run(t)[0] for t in ("get_icu_occupancy", "get_er_status", "get_blood_inventory") if can(t)}
        ot = run("get_ot_schedule", date=g.ctx.clock().date().isoformat())[0] if can("get_ot_schedule") else None
        return render_template("facilities.html", data=data, ot=ot)

    # ------------------------------------------------------------------ admin + platform
    @app.route("/admin")
    @login_required
    def admin():
        if not can("list_staff_users"):
            abort(403)
        users, err = run("list_staff_users", include_inactive=True)
        show_error(err)
        flt = {k: request.args.get(k) or None for k in ("actor", "tool", "outcome", "date_from", "date_to")}
        log_res, err = run("get_audit_log", **flt, limit=100)
        show_error(err)
        return render_template("admin.html", users=(users or {}).get("staff_users", []),
                               log=(log_res or {}).get("entries", []), flt=flt,
                               depts=run("list_departments")[0] or [])

    @app.route("/platform")
    @login_required
    def platform():
        if not can("list_hospitals"):
            abort(403)
        res, err = run("list_hospitals")
        show_error(err)
        return render_template("platform.html", hospitals=(res or {}).get("hospitals", []))

    # ------------------------------------------------------------------ forms: preview -> confirm card
    @app.route("/actions/<tool>", methods=["POST"])
    @login_required
    def action(tool):
        if tool not in FORM_TOOLS or not can(tool):
            abort(403)
        back = request.form.get("next") or request.referrer or "/"
        raw = {k: v for k, v in request.form.items() if k not in ("csrf", "next") and v != ""}
        if tool == "update_facility_status":
            fac = raw.get("facility")
            fields = {k[6:]: (v == "true" if v in ("true", "false") else int(v))
                      for k, v in raw.items() if k.startswith("field_") and v.lstrip("-").isdigit() or
                      (k.startswith("field_") and v in ("true", "false"))}
            raw = {"facility": fac, "fields": fields}
        result = agent.run_tool(g.ctx, tool, raw)
        if result.get("error"):
            show_error({"error": result["error"], "message": result.get("message", ""), "hint": result.get("hint")})
            return redirect(back)
        if result.get("status") == "preview":
            pid = uuid.uuid4().hex
            store.set(f"pending:{sid()}:{pid}", {"id": pid, "tool": tool, "args": raw,
                                                 "confirmation_id": result["confirmation_id"],
                                                 "request_id": uuid.uuid4().hex, "back": back})
            return render_template("confirm.html", pid=pid, tool=tool, preview=result.get("preview", {}),
                                   title=REGISTRY[tool].title, back=back)
        flash(f"{REGISTRY[tool].title}: done.", "success")
        return redirect(back)

    @app.route("/actions/confirm/<pid>", methods=["POST"])
    @login_required
    def action_confirm(pid):
        pending = store.pop(f"pending:{sid()}:{pid}")
        if not pending:
            flash("That confirmation expired. Please start again.", "warning")
            return redirect("/")
        if request.form.get("decision") != "confirm":
            flash("Cancelled. Nothing was changed.", "info")
            return redirect(pending["back"])
        res = agent.confirm_pending(g.ctx, pending)
        if res.get("error"):
            show_error({"error": res["error"], "message": res.get("message", ""), "hint": res.get("hint")})
        else:
            flash(f"{REGISTRY[pending['tool']].title}: done.", "success")
        return redirect(pending["back"])

    # ------------------------------------------------------------------ staff assistant
    @app.route("/assistant")
    @login_required
    def assistant():
        history = store.get(f"chat:{sid()}", [])
        return render_template("assistant.html", history=history, llm_ready=pool.configured)

    @app.route("/api/chat", methods=["POST"])
    @login_required
    def api_chat():
        msg = (request.get_json(silent=True) or {}).get("message", "").strip()
        if not msg:
            return jsonify({"error": "Empty message"}), 400
        key = f"chat:{sid()}"
        out = agent.chat(g.ctx, store.get(key, []), msg, llm=llm_for_turn(),
                         hospital_name=(g.hospital or {}).get("name", "the hospital"))
        store.set(key, out["history"])
        card = None
        if out["pending"]:
            p = out["pending"]
            store.set(f"pending:{sid()}:{p['id']}", {**p, "back": "/assistant"})
            card = {"id": p["id"], "title": REGISTRY[p["tool"]].title, "preview": p["preview"]}
        return jsonify({"reply": out["reply"], "card": card, "tools_used": out["tools_used"]})

    @app.route("/api/confirm/<pid>", methods=["POST"])
    @login_required
    def api_confirm(pid):
        pending = store.pop(f"pending:{sid()}:{pid}")
        if not pending:
            return jsonify({"ok": False, "message": "This confirmation expired. Ask again."}), 410
        if (request.get_json(silent=True) or {}).get("decision") != "confirm":
            msg = "Okay, I did not make that change."
        else:
            res = agent.confirm_pending(g.ctx, pending)
            if res.get("error"):
                return jsonify({"ok": False, "message": res.get("message", "Failed."), "hint": res.get("hint")})
            r = res.get("result") or {}
            ref = r.get("appointment_id") if isinstance(r, dict) else None
            msg = f"Done: {REGISTRY[pending['tool']].title.lower()}" + (f" (reference {ref})." if ref else ".")
        history = store.get(f"chat:{sid()}", [])
        store.set(f"chat:{sid()}", history + [{"role": "assistant", "content": msg}])
        return jsonify({"ok": True, "message": msg})

    @app.route("/api/llm-status")
    @login_required
    def api_llm_status():
        return jsonify({"models": pool.status()})

    @app.route("/api/chat/reset", methods=["POST"])
    @login_required
    def api_chat_reset():
        store.pop(f"chat:{sid()}")
        return jsonify({"ok": True})

    # ------------------------------------------------------------------ public website chatbot
    def public_ctx(slug: str) -> Ctx:
        h = get_database().hospitals.find_one({"slug": slug, "active": True})
        if not h:
            abort(404)
        g.hospital = h
        return make_ctx(public_identity(h["hospital_id"], sid()))

    @app.route("/h/<slug>")
    def public_page(slug):
        c = public_ctx(slug)
        with use_ctx(c):
            info = REGISTRY["get_hospital_info"].call()
            depts = REGISTRY["list_departments"].call()
            icu = REGISTRY["get_icu_occupancy"].call()
            er = REGISTRY["get_er_status"].call()
        return render_template("public.html", slug=slug, info=info, depts=depts, icu=icu, er=er,
                               llm_ready=pool.configured)

    @app.route("/api/public/<slug>/chat", methods=["POST"])
    def api_public_chat(slug):
        check_csrf()
        c = public_ctx(slug)
        msg = (request.get_json(silent=True) or {}).get("message", "").strip()[:1000]
        if not msg:
            return jsonify({"error": "Empty message"}), 400
        key = f"pubchat:{slug}:{sid()}"
        out = agent.chat(c, store.get(key, []), msg, llm=llm_for_turn(), hospital_name=g.hospital["name"])
        store.set(key, out["history"])
        return jsonify({"reply": out["reply"]})

    @app.errorhandler(403)
    def forbidden(_e):
        return render_template("error.html", code=403, message="Your role does not have access to this page."), 403

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("error.html", code=404, message="Page not found."), 404

    @app.errorhandler(400)
    def bad_request(e):
        return render_template("error.html", code=400, message=getattr(e, "description", "Bad request.")), 400

    @app.template_filter("dt")
    def fmt_dt(v):
        from datetime import datetime
        try:
            return datetime.fromisoformat(str(v)).strftime("%a %d %b, %I:%M %p")
        except ValueError:
            return v

    @app.template_filter("day")
    def fmt_day(v):
        try:
            return date.fromisoformat(str(v)[:10]).strftime("%a %d %b")
        except ValueError:
            return v

    @app.template_filter("label")
    def label(v):
        v = str(v)
        if v.endswith("_inr"):
            return v[:-4].replace("_", " ").capitalize() + " (INR)"
        return v.replace("_", " ").capitalize()

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    s = get_settings()
    app = create_app(settings=s)
    host = "0.0.0.0" if not s.is_dev else "127.0.0.1"
    app.run(host=host, port=s.web_port, debug=False)


if __name__ == "__main__":
    main()
