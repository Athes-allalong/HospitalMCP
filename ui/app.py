"""
Hospital MCP – Simple Web UI
Runs alongside the MCP server and talks directly to MongoDB.

Usage:
    python ui/app.py
Then open http://localhost:5000
"""
import os
import sys

# allow importing server helpers for DB / helpers
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_cors import CORS
from server import db, now, inr, to_minor, TZ
from bson import ObjectId

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "hospital-ui-dev-secret")
CORS(app)


def clean_doc(obj):
    """Recursively make MongoDB docs JSON-serialisable."""
    if isinstance(obj, dict):
        return {k: clean_doc(v) for k, v in obj.items() if k != "_id"}
    if isinstance(obj, list):
        return [clean_doc(v) for v in obj]
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


# ─────────────────────────── DASHBOARD ──────────────────────────────────────

@app.route("/")
def dashboard():
    stats = {
        "patients":     db.patients.count_documents({}),
        "doctors":      db.doctors.count_documents({"active": {"$ne": False}}),
        "departments":  db.departments.count_documents({}),
        "appointments": db.appointments.count_documents({"status": "booked"}),
    }
    icu  = db.facilities.find_one({"_id": "icu"}) or {}
    er   = db.facilities.find_one({"_id": "er"})  or {}
    blood = list(db.blood_inventory.find())
    recent_appts = list(
        db.appointments.find({"status": "booked"})
        .sort("slot_start", 1)
        .limit(5)
    )
    return render_template(
        "dashboard.html",
        stats=stats,
        icu=icu,
        er=er,
        blood=blood,
        recent_appts=recent_appts,
        now=now(),
    )


# ─────────────────────────── DEPARTMENTS ────────────────────────────────────

@app.route("/departments")
def departments():
    depts = list(db.departments.find())
    for d in depts:
        d["doctor_count"] = db.doctors.count_documents(
            {"department_id": d["department_id"], "active": {"$ne": False}}
        )
        t = now()
        day_start = datetime(t.year, t.month, t.day)
        d["queue"] = db.appointments.count_documents({
            "department_id": d["department_id"],
            "status": "checked_in",
            "slot_start": {"$gte": day_start, "$lt": day_start + timedelta(days=1)},
        })
    return render_template("departments.html", departments=depts)


# ─────────────────────────── DOCTORS ────────────────────────────────────────

@app.route("/doctors")
def doctors():
    specialty = request.args.get("specialty", "")
    dept      = request.args.get("dept", "")
    q: dict = {"active": {"$ne": False}}
    if specialty:
        import re
        q["specialty"] = {"$regex": re.escape(specialty), "$options": "i"}
    if dept:
        q["$or"] = [{"department_id": dept}, {"department_name": {"$regex": dept, "$options": "i"}}]
    docs = list(db.doctors.find(q).limit(50))
    for d in docs:
        d["fee_inr"] = inr(d.get("consultation_fee_minor", 0))
    specialties = db.doctors.distinct("specialty", {"active": {"$ne": False}})
    depts = list(db.departments.find({}, {"department_id": 1, "name": 1}))
    return render_template("doctors.html", doctors=docs,
                           specialties=sorted(s for s in specialties if s),
                           departments=depts,
                           sel_specialty=specialty, sel_dept=dept)


@app.route("/doctors/<staff_id>")
def doctor_detail(staff_id):
    doc = db.doctors.find_one({"staff_id": staff_id})
    if not doc:
        flash("Doctor not found", "danger")
        return redirect(url_for("doctors"))
    doc["fee_inr"] = inr(doc.get("consultation_fee_minor", 0))
    upcoming = list(
        db.appointments.find({"doctor_id": staff_id, "status": "booked",
                              "slot_start": {"$gte": now()}})
        .sort("slot_start", 1).limit(10)
    )
    return render_template("doctor_detail.html", doctor=doc, appointments=upcoming)


# ─────────────────────────── PATIENTS ────────────────────────────────────────

@app.route("/patients")
def patients():
    q_str = request.args.get("q", "")
    q: dict = {}
    if q_str:
        import re
        rx = {"$regex": re.escape(q_str), "$options": "i"}
        q["$or"] = [{"name": rx}, {"mrn": rx}, {"phone": rx}]
    pts = list(db.patients.find(q).limit(50))
    return render_template("patients.html", patients=pts, q=q_str)


@app.route("/patients/<mrn>")
def patient_detail(mrn):
    p = db.patients.find_one({"mrn": mrn})
    if not p:
        flash("Patient not found", "danger")
        return redirect(url_for("patients"))
    appointments = list(
        db.appointments.find({"mrn": mrn}).sort("slot_start", -1).limit(20)
    )
    for a in appointments:
        doc = db.doctors.find_one({"staff_id": a.get("doctor_id")}, {"name": 1})
        a["doctor_name"] = doc["name"] if doc else a.get("doctor_id", "")
    invoices = list(db.invoices.find({"mrn": mrn}).sort("created_at", -1).limit(10))
    for inv in invoices:
        inv["total_inr"]   = inr(inv.get("total_minor", 0))
        inv["paid_inr"]    = inr(inv.get("paid_minor", 0))
        inv["balance_inr"] = inr(inv.get("total_minor", 0) - inv.get("paid_minor", 0))
    return render_template("patient_detail.html", patient=p,
                           appointments=appointments, invoices=invoices)


# ─────────────────────────── APPOINTMENTS ───────────────────────────────────

@app.route("/appointments")
def appointments():
    status   = request.args.get("status", "booked")
    date_str = request.args.get("date", now().date().isoformat())
    q: dict  = {}
    if status:
        q["status"] = status
    if date_str:
        try:
            d = datetime.fromisoformat(date_str)
            q["slot_start"] = {"$gte": d, "$lt": d + timedelta(days=1)}
        except ValueError:
            pass
    appts = list(db.appointments.find(q).sort("slot_start", 1).limit(100))
    for a in appts:
        doc = db.doctors.find_one({"staff_id": a.get("doctor_id")}, {"name": 1})
        a["doctor_name"] = doc["name"] if doc else a.get("doctor_id", "")
        pt  = db.patients.find_one({"mrn": a.get("mrn")}, {"name": 1})
        a["patient_name"] = pt["name"] if pt else a.get("mrn", "")
    return render_template("appointments.html", appointments=appts,
                           sel_status=status, sel_date=date_str)


# ─────────────────────────── AI APPOINTMENT COORDINATOR ─────────────────────

@app.route("/ai-assistant")
def ai_assistant():
    return render_template("ai_assistant.html")


@app.route("/api/ai/chat", methods=["POST"])
def api_ai_chat():
    from llm_agent import process_agent_message
    data = request.get_json() or {}
    messages = data.get("messages", [])
    if not messages:
        return jsonify({"error": "No messages provided"}), 400
    try:
        result = process_agent_message(messages)
        return jsonify({
            "response": result["response"],
            "messages": result["messages"],
            "tool_calls": result["tool_calls_executed"]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/recent-bookings")
def api_recent_bookings():
    appts = list(db.appointments.find().sort("created_at", -1).limit(6))
    return jsonify(clean_doc(appts))


# ─────────────────────────── API (JSON) ─────────────────────────────────────

@app.route("/api/stats")
def api_stats():
    return jsonify({
        "patients":     db.patients.count_documents({}),
        "doctors":      db.doctors.count_documents({"active": {"$ne": False}}),
        "departments":  db.departments.count_documents({}),
        "appointments": db.appointments.count_documents({"status": "booked"}),
        "icu":  clean_doc(db.facilities.find_one({"_id": "icu"}) or {}),
        "er":   clean_doc(db.facilities.find_one({"_id": "er"})  or {}),
    })


@app.route("/api/doctors")
def api_doctors():
    docs = list(db.doctors.find({"active": {"$ne": False}}).limit(50))
    for d in docs:
        d["consultation_fee_inr"] = inr(d.get("consultation_fee_minor", 0))
    return jsonify(clean_doc(docs))


@app.route("/api/patients")
def api_patients():
    return jsonify(clean_doc(list(db.patients.find().limit(50))))


@app.route("/api/departments")
def api_departments():
    return jsonify(clean_doc(list(db.departments.find())))


if __name__ == "__main__":
    port = int(os.getenv("UI_PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
