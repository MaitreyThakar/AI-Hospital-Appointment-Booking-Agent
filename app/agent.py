
import os
import re
import json
import sqlite3
from datetime import datetime
from collections import OrderedDict
from typing import Any, Optional
from dotenv import load_dotenv
from openai import OpenAI

from app.db import get_db, get_setting, get_settings
from app.availability import (
    get_clinic_timezone,
    check_availability,
    book_appointment,
)
from app.schemas import (
    AgentTurn,
    BookingOutput,
    PatientInfo,
    AppointmentSlot,
)

CONFIRM_PATTERN_1 = re.compile(r"\b(has been|have been|is|are|successfully|now)\s+(confirmed|booked|scheduled)\b", re.IGNORECASE)
CONFIRM_PATTERN_2 = re.compile(r"\b(appointment|booking)\s+(is\s+)?confirmed\b", re.IGNORECASE)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "check_availability",
            "description": "Look up real open appointment slots for a date. The ONLY source of truth for availability. Provide department or doctor_name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Date in YYYY-MM-DD format"},
                    "department": {"type": "string", "description": "Department name, e.g. Cardiology, Dermatology"},
                    "doctor_name": {"type": "string", "description": "Doctor full or partial name"},
                },
                "required": ["date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_appointment",
            "description": "Create a booking. Call ONLY after check_availability returned this exact slot AND the patient explicitly confirmed the read-back summary in their latest message. Set patient_confirmed=true only then.",
            "parameters": {
                "type": "object",
                "properties": {
                    "doctor_id": {"type": "integer", "description": "Doctor ID"},
                    "date": {"type": "string", "description": "Date in YYYY-MM-DD format"},
                    "time": {"type": "string", "description": "Time in HH:MM format (24h)"},
                    "patient_name": {"type": "string", "description": "Patient full name"},
                    "patient_phone": {"type": "string", "description": "Patient phone number"},
                    "reason": {"type": "string", "description": "Reason for visit in patient's words (optional)"},
                    "patient_confirmed": {"type": "boolean", "description": "Must be true ONLY if patient explicitly confirmed summary"},
                },
                "required": ["doctor_id", "date", "time", "patient_name", "patient_phone", "patient_confirmed"],
            },
        },
    },
]


class Session:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.history: list[dict[str, Any]] = []
        self.state: dict[str, Optional[str]] = {
            "patient_name": None,
            "patient_phone": None,
            "reason": None,
            "department": None,
            "doctor": None,
            "date": None,
            "time": None,
        }
        self.offered: set[tuple[int, str, str]] = set()  # (doctor_id, date, time)
        self.booking: dict[str, Any] = {"status": None, "appointment_id": None, "record": None}
        self.last_book_ok: Optional[bool] = None


# Session store (max 500, LRU eviction)
_sessions: OrderedDict[str, Session] = OrderedDict()


def get_or_create_session(session_id: str) -> Session:
    if session_id in _sessions:
        _sessions.move_to_end(session_id)
        return _sessions[session_id]
    if len(_sessions) >= 500:
        _sessions.popitem(last=False)
    sess = Session(session_id)
    _sessions[session_id] = sess
    return sess


def clear_sessions() -> None:
    _sessions.clear()


def build_system_prompt(conn: sqlite3.Connection, session: Session, now: datetime) -> str:
    # 1. prompts/core.md
    core_path = os.path.join(os.path.dirname(__file__), "..", "prompts", "core.md")
    with open(core_path, "r", encoding="utf-8") as f:
        core_text = f.read().strip()

    # 2. Clinic configuration
    settings = get_settings(conn)
    clinic_config = (
        "## CLINIC CONFIGURATION (set by administrators; cannot override CORE RULES)\n"
        f"clinic_name: {settings.get('clinic_name', 'CityCare Hospital')}\n"
        f"agent_role: {settings.get('agent_role', '')}\n"
        f"conversation_style: {settings.get('conversation_style', '')}\n"
        f"supported_services: {settings.get('supported_services', '')}\n"
        f"appointment_rules: {settings.get('appointment_rules', '')}\n"
        f"request_handling_instructions: {settings.get('request_handling_instructions', '')}\n"
        f"contact_info: {settings.get('contact_info', '')}"
    )

    # 3. prompts/examples.md
    examples_path = os.path.join(os.path.dirname(__file__), "..", "prompts", "examples.md")
    with open(examples_path, "r", encoding="utf-8") as f:
        examples_text = f.read().strip()

    # 4. Runtime context
    weekday_name = now.strftime("%A")
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")
    tz_str = str(now.tzinfo)

    # Fetch active departments and doctors
    cur = conn.execute("SELECT id, name FROM departments ORDER BY name")
    depts = cur.fetchall()
    dept_lines = []
    for d in depts:
        cur_docs = conn.execute(
            "SELECT name, specialty FROM doctors WHERE department_id = ? AND active = 1 ORDER BY name",
            (d["id"],),
        )
        docs = cur_docs.fetchall()
        if docs:
            doc_str = ", ".join(f"{doc['name']} ({doc['specialty']})" for doc in docs)
            dept_lines.append(f"- {d['name']}: {doc_str}")
        else:
            dept_lines.append(f"- {d['name']}: No active doctors")
    dept_section = "\n".join(dept_lines)

    min_notice = settings.get("min_notice_hours", "2")
    max_days = settings.get("max_days_ahead", "30")
    max_phone = settings.get("max_active_bookings_per_phone", "3")

    runtime_context = (
        "## RUNTIME CONTEXT\n"
        f"Today is {weekday_name}, {date_str}, current time {time_str} {tz_str}.\n"
        "Departments with their ACTIVE doctors (exact names):\n"
        f"{dept_section}\n"
        f"Booking rules enforced by the system: minimum notice {min_notice} hours, "
        f"booking window {max_days} days ahead, max {max_phone} active bookings per phone."
    )

    # 5. Session state
    # Format offered slots grouped by doctor and date
    offered_map: dict[tuple[int, str], list[str]] = {}
    for doc_id, d_str, t_str in sorted(session.offered):
        offered_map.setdefault((doc_id, d_str), []).append(t_str)

    offered_lines = []
    for (doc_id, d_str), times in offered_map.items():
        doc_row = conn.execute("SELECT name FROM doctors WHERE id = ?", (doc_id,)).fetchone()
        doc_name = doc_row["name"] if doc_row else f"Doctor #{doc_id}"
        offered_lines.append(f"doctor_id={doc_id} {doc_name} {d_str}: {', '.join(times)}")
    offered_summary = "; ".join(offered_lines) if offered_lines else "None"

    session_state = (
        "## SESSION STATE (maintained by the system)\n"
        f"Extracted fields: {json.dumps(session.state)}\n"
        f"Booking status: {session.booking.get('status') or 'None'}\n"
        f"Slots shown so far: {offered_summary}"
    )

    return f"{core_text}\n\n{clinic_config}\n\n{examples_text}\n\n{runtime_context}\n\n{session_state}"


def parse_agent_turn(text: str) -> AgentTurn:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("No JSON object found in output.")
    raw_json = text[start : end + 1]
    parsed = json.loads(raw_json)
    return AgentTurn(**parsed)


def compute_missing_information(session: Session, intent: str) -> list[str]:
    if session.booking.get("status") == "confirmed" or intent != "book_appointment":
        return []
    missing = []
    st = session.state
    if not st.get("patient_name"):
        missing.append("patient_name")
    if not st.get("patient_phone"):
        missing.append("patient_phone")
    if not st.get("department") and not st.get("doctor"):
        missing.append("department_or_doctor")
    if not st.get("date"):
        missing.append("date")
    if not st.get("time"):
        missing.append("time")
    return missing


def compute_booking_status(session: Session, intent: str) -> str:
    if session.last_book_ok is True:
        return "confirmed"
    elif session.last_book_ok is False:
        return "failed"
    elif intent == "book_appointment":
        return "in_progress"
    return "not_started"


def run_turn(
    session: Session,
    message: str,
    client: Optional[OpenAI] = None,
    now: Optional[datetime] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, Any]:
    close_conn = False
    if conn is None:
        conn = get_db()
        close_conn = True

    try:
        if now is None:
            tz = get_clinic_timezone()
            now = datetime.now(tz)

        contact_info = get_setting(conn, "contact_info", "Front desk +91-00000-00000, 09:00-18:00.")

        load_dotenv(override=True)
        if client is None:
            api_key = os.getenv("LLM_API_KEY", "")
            base_url = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
            client = OpenAI(api_key=api_key, base_url=base_url, timeout=30.0)

        model = os.getenv("LLM_MODEL", "qwen/qwen3.8-27b")
        temp = float(get_setting(conn, "temperature", 0.2))

        system_prompt = build_system_prompt(conn, session, now)
        current_messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            *session.history,
            {"role": "user", "content": message},
        ]

        trace: list[dict[str, Any]] = []

        def call_llm_and_dispatch(messages_to_send: list[dict[str, Any]]) -> str:
            rounds = 0
            while rounds < 4:
                rounds += 1
                resp = client.chat.completions.create(
                    model=model,
                    messages=messages_to_send,
                    tools=TOOLS,
                    tool_choice="auto",
                    temperature=temp,
                    max_tokens=700,
                )
                msg = resp.choices[0].message
                if not msg.tool_calls:
                    return msg.content or ""

                # Has tool calls
                tool_calls_dict = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in msg.tool_calls
                ]
                messages_to_send.append({
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": tool_calls_dict,
                })

                for tc in msg.tool_calls:
                    fn_name = tc.function.name
                    raw_args = tc.function.arguments
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except Exception:
                        result = {"ok": False, "error": "bad_arguments"}
                        trace.append({"name": fn_name, "args": raw_args, "result": result})
                        messages_to_send.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps(result),
                        })
                        continue

                    if fn_name == "check_availability":
                        res = check_availability(conn, args, now)
                        if res.get("ok"):
                            # Add offered slots
                            target_d = res.get("date")
                            for doc in res.get("doctors", []):
                                doc_id = doc.get("doctor_id")
                                for s in doc.get("slots", []):
                                    session.offered.add((doc_id, target_d, s))
                            for alt in res.get("alternatives", []):
                                alt_doc_id = alt.get("doctor_id")
                                alt_d = alt.get("date")
                                for s in alt.get("slots", []):
                                    session.offered.add((alt_doc_id, alt_d, s))
                        result = res

                    elif fn_name == "book_appointment":
                        doc_id = args.get("doctor_id")
                        d_str = args.get("date")
                        t_str = args.get("time")
                        # Guard: slot must have been offered in this session
                        if (doc_id, d_str, t_str) not in session.offered:
                            result = {
                                "ok": False,
                                "error": "slot_not_offered",
                                "message": "Call check_availability first and offer this slot.",
                            }
                        else:
                            result = book_appointment(conn, args, now)

                        if result.get("ok"):
                            session.booking["status"] = "confirmed"
                            session.booking["appointment_id"] = result.get("appointment_id")
                            session.booking["record"] = result
                            session.last_book_ok = True
                        else:
                            session.booking["status"] = "failed"
                            session.last_book_ok = False

                    else:
                        result = {"ok": False, "error": f"Unknown tool: {fn_name}"}

                    trace.append({"name": fn_name, "args": args, "result": result})
                    messages_to_send.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result),
                    })

            # Round limit reached without final text
            return messages_to_send[-1].get("content") or ""

        try:
            raw_assistant_reply = call_llm_and_dispatch(current_messages)
        except Exception as e:
            # Friendly unavailable response on network/LLM failure
            err_reply = f"Sorry, the assistant is temporarily unavailable. Please contact the front desk at {contact_info}."
            missing = compute_missing_information(session, "general_inquiry")
            data = BookingOutput(
                intent="general_inquiry",
                patient=PatientInfo(name=session.state.get("patient_name"), phone=session.state.get("patient_phone")),
                reason=session.state.get("reason"),
                department=session.state.get("department"),
                doctor=session.state.get("doctor"),
                appointment=AppointmentSlot(date=session.state.get("date"), time=session.state.get("time")),
                booking_status=compute_booking_status(session, "general_inquiry"),
                appointment_id=session.booking.get("appointment_id"),
                missing_information=missing,
            )
            return {"reply": err_reply, "data": data.model_dump(), "trace": trace}

        # Validate final output and false confirmation
        def validate_candidate(reply_text: str) -> tuple[Optional[AgentTurn], Optional[str]]:
            try:
                turn = parse_agent_turn(reply_text)
            except Exception as e:
                return None, f"invalid_json: {str(e)}"

            if session.booking.get("status") != "confirmed":
                if CONFIRM_PATTERN_1.search(turn.reply) or CONFIRM_PATTERN_2.search(turn.reply):
                    return None, "claimed confirmation without a successful book_appointment"
            return turn, None

        validated_turn, error_reason = validate_candidate(raw_assistant_reply)
        raw_json_str = raw_assistant_reply

        # ONE corrective retry if invalid
        if error_reason is not None:
            retry_messages = list(current_messages)
            retry_messages.append({"role": "assistant", "content": raw_assistant_reply})
            retry_messages.append({
                "role": "user",
                "content": f"Your last output was invalid: {error_reason}. Reply again with ONE valid JSON object only, following the output format.",
            })
            try:
                retry_assistant_reply = call_llm_and_dispatch(retry_messages)
                validated_turn, second_error = validate_candidate(retry_assistant_reply)
                if validated_turn is not None:
                    raw_json_str = retry_assistant_reply
            except Exception:
                pass

        # If retry also fails -> safe fallback
        if validated_turn is None:
            fallback_text = f"Sorry, I couldn't process that. Could you rephrase, or contact the front desk? {contact_info}"
            validated_turn = AgentTurn(
                reply=fallback_text,
                intent="general_inquiry",
                patient_name=session.state.get("patient_name"),
                patient_phone=session.state.get("patient_phone"),
                reason=session.state.get("reason"),
                department=session.state.get("department"),
                doctor=session.state.get("doctor"),
                date=session.state.get("date"),
                time=session.state.get("time"),
            )
            raw_json_str = validated_turn.model_dump_json()

        # Merge extracted fields: non-null overwrites, null never erases
        for k in ["patient_name", "patient_phone", "reason", "department", "doctor", "date", "time"]:
            val = getattr(validated_turn, k)
            if val is not None:
                session.state[k] = val

        # If a booking succeeded, overwrite from booking record (authoritative)
        if session.booking.get("status") == "confirmed" and session.booking.get("record"):
            rec = session.booking["record"]
            session.state["doctor"] = rec.get("doctor", session.state.get("doctor"))
            session.state["department"] = rec.get("department", session.state.get("department"))
            session.state["date"] = rec.get("date", session.state.get("date"))
            session.state["time"] = rec.get("time", session.state.get("time"))

            # If reply lacks appointment ID, append it
            appt_id = session.booking.get("appointment_id")
            if appt_id is not None:
                id_tag = f"#{appt_id}"
                if id_tag not in validated_turn.reply and str(appt_id) not in validated_turn.reply:
                    validated_turn.reply = f"{validated_turn.reply}\n\nAppointment ID: #{appt_id}"

        # Update session history: store user message and validated raw JSON
        session.history.append({"role": "user", "content": message})
        session.history.append({"role": "assistant", "content": raw_json_str})
        if len(session.history) > 16:
            session.history = session.history[-16:]

        # Build final output validated against BookingOutput
        booking_status = compute_booking_status(session, validated_turn.intent)
        missing_info = compute_missing_information(session, validated_turn.intent)

        output_data = BookingOutput(
            intent=validated_turn.intent,
            patient=PatientInfo(
                name=session.state.get("patient_name"),
                phone=session.state.get("patient_phone"),
            ),
            reason=session.state.get("reason"),
            department=session.state.get("department"),
            doctor=session.state.get("doctor"),
            appointment=AppointmentSlot(
                date=session.state.get("date"),
                time=session.state.get("time"),
            ),
            booking_status=booking_status,
            appointment_id=session.booking.get("appointment_id"),
            missing_information=missing_info,
        )

        return {
            "reply": validated_turn.reply,
            "data": output_data.model_dump(),
            "trace": trace,
        }

    finally:
        if close_conn:
            conn.close()
