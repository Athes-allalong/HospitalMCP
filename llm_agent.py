"""
LLM Agent Booking Service for Hospital MCP.
Uses Groq API with tool-calling capabilities to interact with hospital MongoDB data
and execute automated appointment bookings on behalf of clients.
"""

import os
import json
import uuid
import logging
from datetime import datetime, date, timedelta, time
from typing import Dict, Any, List, Optional

from groq import Groq
from server import (
    db,
    now,
    to_minor,
    inr,
    open_slots,
    get_doctor,
    TYPE_MINUTES,
    clean,
)

log = logging.getLogger("llm-agent")
log.setLevel(logging.INFO)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
MODEL_NAME = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

client: Optional[Groq] = None
if GROQ_API_KEY:
    try:
        client = Groq(api_key=GROQ_API_KEY)
    except Exception as e:
        log.error(f"Failed to initialize Groq client: {e}")

# Tool definitions for LLM Function Calling
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup_patient",
            "description": "Find an existing patient by MRN, phone number, or full name. Always verify patient before booking.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "MRN (e.g. MRN-84920), phone number (+1555...), or patient name."
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "register_patient",
            "description": "Register a new patient if they do not already exist in the hospital records.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Full name of the patient (e.g. 'Alex Smith')"},
                    "phone": {"type": "string", "description": "Phone number (e.g. '1234567890' or '+15550192000')"},
                    "email": {"type": "string", "description": "Email address (optional)"},
                    "dob": {"type": "string", "description": "Date of birth in YYYY-MM-DD"},
                    "gender": {"type": "string", "enum": ["M", "F", "Other"], "description": "Biological sex/gender"},
                    "blood_group": {"type": "string", "description": "Blood group e.g. O+, A+, B+, etc."}
                },
                "required": ["name", "phone"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_doctors",
            "description": "Search active hospital doctors by specialty (e.g. Cardiology, Orthopedics, Pediatrics), department name, or doctor name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "specialty": {"type": "string", "description": "Doctor specialty e.g. Cardiology, Neurology, Orthopedics"},
                    "department": {"type": "string", "description": "Department name or code"},
                    "name": {"type": "string", "description": "Doctor name keyword"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_doctor_availability",
            "description": "Check open appointment slots for a specific doctor within a given date range (up to 14 days).",
            "parameters": {
                "type": "object",
                "properties": {
                    "doctor_id": {"type": "string", "description": "Staff ID of the doctor (e.g. DOC-CARD-001)"},
                    "start_date": {"type": "string", "description": "Start date YYYY-MM-DD"},
                    "end_date": {"type": "string", "description": "End date YYYY-MM-DD"},
                    "appointment_type": {
                        "type": "string",
                        "enum": ["new", "followup", "teleconsult"],
                        "description": "Type of appointment: new (30 min), followup (15 min), teleconsult (15 min)"
                    }
                },
                "required": ["doctor_id", "start_date", "end_date"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "book_appointment",
            "description": "Book a verified appointment slot in MongoDB for a patient with a doctor.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mrn": {"type": "string", "description": "Patient MRN"},
                    "doctor_id": {"type": "string", "description": "Doctor staff ID"},
                    "slot_start": {"type": "string", "description": "Slot start time in ISO format (e.g. 2026-11-16T10:00:00)"},
                    "appointment_type": {
                        "type": "string",
                        "enum": ["new", "followup", "teleconsult"],
                        "description": "Type of appointment"
                    },
                    "reason": {"type": "string", "description": "Reason for consultation / symptoms"}
                },
                "required": ["mrn", "doctor_id", "slot_start"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_patient_appointments",
            "description": "Retrieve current and past booked appointments for a patient.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mrn": {"type": "string", "description": "Patient MRN"}
                },
                "required": ["mrn"]
            }
        }
    }
]

# Tool implementations
def tool_lookup_patient(query: str) -> Dict[str, Any]:
    query = query.strip()
    import re
    rx = {"$regex": re.escape(query), "$options": "i"}
    p = db.patients.find_one({"$or": [{"mrn": rx}, {"phone": rx}, {"name": rx}]})
    if not p:
        return {"status": "not_found", "message": f"No patient record found matching '{query}'"}
    return {
        "status": "found",
        "patient": {
            "mrn": p.get("mrn"),
            "name": p.get("name"),
            "dob": p.get("dob"),
            "phone": p.get("phone"),
            "email": p.get("email"),
            "blood_group": p.get("blood_group"),
            "insurance": p.get("insurance")
        }
    }

def tool_register_patient(name: str, phone: str, email: str = "", dob: str = "1990-01-01", gender: str = "M", blood_group: str = "O+") -> Dict[str, Any]:
    existing = db.patients.find_one({"phone": phone})
    if existing:
        return {
            "status": "already_exists",
            "message": f"Patient with phone {phone} already registered.",
            "patient": {"mrn": existing["mrn"], "name": existing["name"]}
        }
    
    # Generate new MRN
    count = db.patients.count_documents({}) + 1
    new_mrn = f"MRN-{count + 10000}"
    doc = {
        "mrn": new_mrn,
        "name": name,
        "phone": phone,
        "email": email,
        "dob": dob,
        "sex": gender,
        "blood_group": blood_group,
        "allergies": [],
        "insurance": None,
        "created_at": now()
    }
    db.patients.insert_one(doc)
    return {
        "status": "created",
        "mrn": new_mrn,
        "name": name,
        "message": f"Patient registered successfully with MRN {new_mrn}"
    }

def tool_search_doctors(specialty: Optional[str] = None, department: Optional[str] = None, name: Optional[str] = None) -> List[Dict[str, Any]]:
    import re
    q: Dict[str, Any] = {"active": {"$ne": False}}
    rx = lambda s: {"$regex": re.escape(s), "$options": "i"}
    if specialty:
        q["$or"] = [{"specialty": rx(specialty)}, {"bio": rx(specialty)}]
    if department:
        q["$or"] = [{"department_id": rx(department)}, {"department_name": rx(department)}]
    if name:
        q["name"] = rx(name)
    
    results = []
    for d in db.doctors.find(q).limit(10):
        results.append({
            "staff_id": d["staff_id"],
            "name": d["name"],
            "specialty": d.get("specialty"),
            "department": d.get("department_name") or d.get("department_id"),
            "consultation_fee_inr": inr(d.get("consultation_fee_minor", 0)),
            "experience_years": d.get("experience_years"),
            "qualifications": d.get("qualifications")
        })
    return results

def tool_check_doctor_availability(doctor_id: str, start_date: str, end_date: str, appointment_type: str = "new") -> Dict[str, Any]:
    try:
        doc = get_doctor(doctor_id)
        d0 = date.fromisoformat(start_date)
        d1 = date.fromisoformat(end_date)
        if (d1 - d0).days > 14:
            d1 = d0 + timedelta(days=14)
        slots = open_slots(doc, d0, d1, appointment_type)
        return {
            "doctor_id": doctor_id,
            "doctor_name": doc["name"],
            "appointment_type": appointment_type,
            "duration_minutes": TYPE_MINUTES.get(appointment_type, 30),
            "available_slots": [s.isoformat() for s in slots[:20]]
        }
    except Exception as e:
        return {"error": str(e)}

def tool_book_appointment(mrn: str, doctor_id: str, slot_start: str, appointment_type: str = "new", reason: str = "") -> Dict[str, Any]:
    try:
        p = db.patients.find_one({"mrn": mrn})
        if not p:
            return {"error": f"Patient with MRN '{mrn}' not found. Please verify or register patient."}
        
        doc = get_doctor(doctor_id)
        start = datetime.fromisoformat(slot_start).replace(tzinfo=None)
        
        # Check slot availability
        available = open_slots(doc, start.date(), start.date(), appointment_type)
        if start not in available:
            # Suggest next available slots
            next_slots = open_slots(doc, start.date(), start.date() + timedelta(days=3), appointment_type)
            return {
                "error": "The selected slot is unavailable or in the past.",
                "suggested_slots": [s.isoformat() for s in next_slots[:5]]
            }
        
        dur = timedelta(minutes=TYPE_MINUTES.get(appointment_type, 30))
        appt_id = f"APT-{uuid.uuid4().hex[:8].upper()}"
        appt_doc = {
            "appointment_id": appt_id,
            "mrn": mrn,
            "doctor_id": doctor_id,
            "doctor_name": doc["name"],
            "patient_name": p["name"],
            "department_id": doc.get("department_id"),
            "type": appointment_type,
            "slot_start": start,
            "slot_end": start + dur,
            "status": "booked",
            "booked_by": "llm-ai-agent",
            "reason": reason,
            "created_at": now()
        }
        db.appointments.insert_one(appt_doc)
        
        # Log audit trail
        db.audit_log.insert_one({
            "at": now(),
            "role": "ai_agent",
            "subject": mrn,
            "tool": "llm_book_appointment",
            "target": appt_id
        })
        
        return {
            "status": "success",
            "appointment_id": appt_id,
            "patient_name": p["name"],
            "doctor_name": doc["name"],
            "department": doc.get("department_name") or doc.get("department_id"),
            "slot_start": start.isoformat(),
            "duration_minutes": TYPE_MINUTES.get(appointment_type, 30),
            "fee_inr": inr(doc.get("consultation_fee_minor", 0))
        }
    except Exception as e:
        return {"error": str(e)}

def tool_get_patient_appointments(mrn: str) -> List[Dict[str, Any]]:
    appts = list(db.appointments.find({"mrn": mrn}).sort("slot_start", -1).limit(10))
    cleaned = []
    for a in appts:
        cleaned.append({
            "appointment_id": a.get("appointment_id"),
            "doctor_id": a.get("doctor_id"),
            "doctor_name": a.get("doctor_name"),
            "slot_start": a.get("slot_start").isoformat() if isinstance(a.get("slot_start"), datetime) else str(a.get("slot_start")),
            "status": a.get("status"),
            "type": a.get("type")
        })
    return cleaned

TOOL_MAP = {
    "lookup_patient": lambda args: tool_lookup_patient(**args),
    "register_patient": lambda args: tool_register_patient(**args),
    "search_doctors": lambda args: tool_search_doctors(**args),
    "check_doctor_availability": lambda args: tool_check_doctor_availability(**args),
    "book_appointment": lambda args: tool_book_appointment(**args),
    "get_patient_appointments": lambda args: tool_get_patient_appointments(**args),
}

SYSTEM_PROMPT = """You are an intelligent, empathetic, and efficient Hospital AI Appointment Coordinator for Dummy Hospital.
Your goal is to converse with clients, understand their medical needs, check schedules, and automate appointment booking in MongoDB.

Current hospital reference datetime: {current_time}

Workflow Guidelines:
1. Patient Identification: Always check if the patient exists using `lookup_patient` (by MRN, name, or phone number). If not found and they want to book, register them using `register_patient`.
2. Doctor & Specialty Discovery: If the user describes symptoms (e.g. chest pain -> Cardiology, bone fracture -> Orthopedics, child illness -> Pediatrics), recommend the right specialty and use `search_doctors` to present matching doctors.
3. Availability Check: Check open slots using `check_doctor_availability` with dates starting from tomorrow onwards. Present a few clean, friendly time options to the user.
4. Booking & MongoDB Persistence: Once the user confirms the doctor and time, call `book_appointment`. Always confirm the appointment ID, doctor name, patient name, date, and fee.
5. Be concise, polite, helpful, and transparent. Never invent non-existent slots—always verify via the availability tool.
"""

def process_agent_message(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Core function that runs the LLM loop with tool-calling until a final assistant response is generated.
    Returns: {"response": str, "messages": list, "tool_calls_executed": list}
    """
    if not client:
        return {
            "response": "LLM client is not configured. Please ensure GROQ_API_KEY environment variable is set.",
            "messages": messages,
            "tool_calls_executed": []
        }

    sys_message = {
        "role": "system",
        "content": SYSTEM_PROMPT.format(current_time=now().strftime("%Y-%m-%d %H:%M:%S (%A)"))
    }

    conversation = [sys_message] + messages
    tool_calls_executed = []
    
    # Allow up to 5 tool-calling turns
    for _ in range(5):
        try:
            resp = client.chat.completions.create(
                model=MODEL_NAME,
                messages=conversation,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.2,
                max_tokens=800
            )
        except Exception as api_err:
            log.warning(f"Error calling {MODEL_NAME}: {api_err}. Trying fallback model...")
            # Fallback to secondary model if primary encounters format or rate limit error
            fallback_model = "openai/gpt-oss-20b" if MODEL_NAME != "openai/gpt-oss-20b" else "qwen/qwen3.8-27b"
            resp = client.chat.completions.create(
                model=fallback_model,
                messages=conversation,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.2,
                max_tokens=800
            )
        
        choice = resp.choices[0]
        msg = choice.message
        
        # Check if model wants to call tools
        if msg.tool_calls:
            # Append assistant message with tool calls
            conversation.append({
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments
                        }
                    } for tc in msg.tool_calls
                ]
            })
            
            # Execute each tool
            for tc in msg.tool_calls:
                fn_name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments)
                except Exception:
                    args = {}
                
                log.info(f"Executing LLM Tool '{fn_name}' with args {args}")
                handler = TOOL_MAP.get(fn_name)
                if handler:
                    try:
                        res = handler(args)
                    except Exception as err:
                        res = {"error": str(err)}
                else:
                    res = {"error": f"Unknown tool: {fn_name}"}
                
                tool_calls_executed.append({"tool": fn_name, "args": args, "result": res})
                
                conversation.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(res)
                })
        else:
            # Final text response
            final_content = msg.content or ""
            return {
                "response": final_content,
                "messages": conversation[1:],  # exclude system prompt from returned history
                "tool_calls_executed": tool_calls_executed
            }

    return {
        "response": msg.content or "Appointment flow completed.",
        "messages": conversation[1:],
        "tool_calls_executed": tool_calls_executed
    }
