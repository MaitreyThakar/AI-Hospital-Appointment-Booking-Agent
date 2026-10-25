import os
import secrets
import sqlite3
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Any, Optional
from dotenv import load_dotenv
from fastapi import FastAPI, Depends, HTTPException, status, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

load_dotenv()

from app.db import get_db, init_db, get_setting, get_settings, set_setting
from app.availability import (
    get_clinic_timezone,
    slot_status,
    check_schedule_overlap,
    parse_time_to_minutes,
)
from app.schemas import (
    ChatRequest,
    ChatResponse,
    BookingOutput,
    SettingsPayload,
)
from app.agent import run_turn, get_or_create_session

# Check ADMIN_PASSWORD at startup
def verify_admin_password():
    pwd = os.getenv("ADMIN_PASSWORD")
    if not pwd or pwd == "change-me":
        raise RuntimeError("ADMIN_PASSWORD is unset or equals 'change-me'. Refusing to start.")

verify_admin_password()

@asynccontextmanager
async def lifespan(app: FastAPI):
    verify_admin_password()
    conn = get_db()
    init_db(conn)
    conn.close()
    yield

app = FastAPI(lifespan=lifespan)
security = HTTPBasic()


def admin_auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    correct_user = os.getenv("ADMIN_USER", "admin")
    correct_pass = os.getenv("ADMIN_PASSWORD", "")
    is_user_ok = secrets.compare_digest(credentials.username.encode("utf8"), correct_user.encode("utf8"))
    is_pass_ok = secrets.compare_digest(credentials.password.encode("utf8"), correct_pass.encode("utf8"))
    if not (is_user_ok and is_pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def get_html_content(filepath: str) -> str:
    with open(filepath, "r", encoding="utf-8") as f:
        return f.read()


# Public Routes
@app.get("/", response_class=HTMLResponse)
def index():
    path = os.path.join(os.path.dirname(__file__), "..", "static", "chat.html")
    return HTMLResponse(content=get_html_content(path))


@app.get("/api/public-config")
def public_config():
    conn = get_db()
    try:
        clinic_name = get_setting(conn, "clinic_name", "CityCare Hospital")
        welcome = get_setting(conn, "welcome_message", "Hello! I can help you book an appointment.")
        return {"clinic_name": clinic_name, "welcome_message": welcome}
    finally:
        conn.close()


@app.post("/api/chat", response_model=ChatResponse)
def chat_endpoint(payload: ChatRequest):
    sess = get_or_create_session(payload.session_id)
    res = run_turn(sess, payload.message)
    return ChatResponse(reply=res["reply"], data=BookingOutput(**res["data"]), trace=res["trace"])


# Admin UI Route
@app.get("/admin", response_class=HTMLResponse)
def admin_page(_user: str = Depends(admin_auth)):
    path = os.path.join(os.path.dirname(__file__), "..", "static", "admin.html")
    return HTMLResponse(content=get_html_content(path))


# Generic CRUD Helper
TABLE_CONFIG = {
    "departments": {"cols": ["name", "description"], "table": "departments"},
    "doctors": {"cols": ["name", "department_id", "specialty", "active"], "table": "doctors"},
    "schedules": {"cols": ["doctor_id", "weekday", "start_time", "end_time", "slot_minutes"], "table": "schedules"},
}


def generic_list(table: str, where_clause: str = "", params: tuple = ()):
    conn = get_db()
    try:
        sql = f"SELECT * FROM {table} {where_clause}"
        cur = conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def generic_insert(table: str, data: dict[str, Any]):
    cols = TABLE_CONFIG[table]["cols"]
    keys = [c for c in cols if c in data]
    placeholders = ", ".join(["?"] * len(keys))
    sql = f"INSERT INTO {table} ({', '.join(keys)}) VALUES ({placeholders})"
    conn = get_db()
    try:
        cur = conn.execute(sql, [data[k] for k in keys])
        return cur.lastrowid
    except sqlite3.IntegrityError as e:
        raise HTTPException(status_code=409, detail=f"Database conflict: {str(e)}")
    finally:
        conn.close()


def generic_update(table: str, item_id: int, data: dict[str, Any]):
    cols = TABLE_CONFIG[table]["cols"]
    keys = [c for c in cols if c in data]
    if not keys:
        return
    set_clause = ", ".join(f"{k} = ?" for k in keys)
    sql = f"UPDATE {table} SET {set_clause} WHERE id = ?"
    conn = get_db()
    try:
        cur = conn.execute(sql, [data[k] for k in keys] + [item_id])
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Item not found")
    except sqlite3.IntegrityError as e:
        raise HTTPException(status_code=409, detail=f"Database conflict: {str(e)}")
    finally:
        conn.close()


# Admin Departments
@app.get("/api/admin/departments")
def list_departments(_user: str = Depends(admin_auth)):
    return generic_list("departments", "ORDER BY name")


@app.post("/api/admin/departments")
def create_department(payload: dict[str, Any], _user: str = Depends(admin_auth)):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="Department name is required")
    new_id = generic_insert("departments", {"name": name, "description": payload.get("description", "")})
    return {"id": new_id, "name": name}


@app.put("/api/admin/departments/{dept_id}")
def update_department(dept_id: int, payload: dict[str, Any], _user: str = Depends(admin_auth)):
    generic_update("departments", dept_id, payload)
    return {"ok": True}


@app.delete("/api/admin/departments/{dept_id}")
def delete_department(dept_id: int, _user: str = Depends(admin_auth)):
    conn = get_db()
    try:
        cur = conn.execute("SELECT COUNT(*) as cnt FROM doctors WHERE department_id = ?", (dept_id,))
        if cur.fetchone()["cnt"] > 0:
            raise HTTPException(status_code=409, detail="Cannot delete department with assigned doctors")
        conn.execute("DELETE FROM departments WHERE id = ?", (dept_id,))
        return {"ok": True}
    finally:
        conn.close()


# Admin Doctors
@app.get("/api/admin/doctors")
def list_doctors(_user: str = Depends(admin_auth)):
    conn = get_db()
    try:
        cur = conn.execute(
            "SELECT d.*, dep.name as department_name FROM doctors d "
            "JOIN departments dep ON d.department_id = dep.id ORDER BY d.id"
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


@app.post("/api/admin/doctors")
def create_doctor(payload: dict[str, Any], _user: str = Depends(admin_auth)):
    new_id = generic_insert("doctors", payload)
    return {"id": new_id}


@app.put("/api/admin/doctors/{doc_id}")
def update_doctor(doc_id: int, payload: dict[str, Any], _user: str = Depends(admin_auth)):
    generic_update("doctors", doc_id, payload)
    return {"ok": True}


@app.delete("/api/admin/doctors/{doc_id}")
def delete_doctor(doc_id: int, _user: str = Depends(admin_auth)):
    conn = get_db()
    try:
        today_str = datetime.now(get_clinic_timezone()).date().isoformat()
        cur = conn.execute(
            "SELECT COUNT(*) as cnt FROM appointments WHERE doctor_id = ? AND date >= ? AND status = 'booked'",
            (doc_id, today_str),
        )
        if cur.fetchone()["cnt"] > 0:
            raise HTTPException(status_code=409, detail="Cannot deactivate doctor with future booked appointments")
        conn.execute("UPDATE doctors SET active = 0 WHERE id = ?", (doc_id,))
        return {"ok": True}
    finally:
        conn.close()


# Admin Schedules
@app.get("/api/admin/schedules")
def list_schedules(doctor_id: Optional[int] = None, _user: str = Depends(admin_auth)):
    if doctor_id:
        return generic_list("schedules", "WHERE doctor_id = ? ORDER BY weekday, start_time", (doctor_id,))
    return generic_list("schedules", "ORDER BY doctor_id, weekday, start_time")


def validate_schedule_payload(conn: sqlite3.Connection, p: dict[str, Any], exclude_id: Optional[int] = None):
    try:
        weekday = int(p["weekday"])
        slot_min = int(p["slot_minutes"])
        start = p["start_time"].strip()
        end = p["end_time"].strip()
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid schedule fields format")

    if not (0 <= weekday <= 6):
        raise HTTPException(status_code=422, detail="Weekday must be between 0 and 6")
    if not (5 <= slot_min <= 240):
        raise HTTPException(status_code=422, detail="Slot minutes must be between 5 and 240")

    s_min = parse_time_to_minutes(start)
    e_min = parse_time_to_minutes(end)
    if s_min >= e_min:
        raise HTTPException(status_code=422, detail="start_time must be before end_time")
    if s_min + slot_min > e_min:
        raise HTTPException(status_code=422, detail="Schedule must contain at least one full slot")

    doc_id = p["doctor_id"]
    if check_schedule_overlap(conn, doc_id, weekday, start, end, exclude_id):
        raise HTTPException(status_code=409, detail="Schedule overlaps with existing schedule for this doctor and day")


@app.post("/api/admin/schedules")
def create_schedule(payload: dict[str, Any], _user: str = Depends(admin_auth)):
    conn = get_db()
    try:
        validate_schedule_payload(conn, payload)
    finally:
        conn.close()
    new_id = generic_insert("schedules", payload)
    return {"id": new_id}


@app.put("/api/admin/schedules/{sched_id}")
def update_schedule(sched_id: int, payload: dict[str, Any], _user: str = Depends(admin_auth)):
    conn = get_db()
    try:
        cur = conn.execute("SELECT doctor_id FROM schedules WHERE id = ?", (sched_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Schedule not found")
        p = {**dict(row), **payload}
        validate_schedule_payload(conn, p, exclude_id=sched_id)
    finally:
        conn.close()
    generic_update("schedules", sched_id, payload)
    return {"ok": True}


@app.delete("/api/admin/schedules/{sched_id}")
def delete_schedule(sched_id: int, _user: str = Depends(admin_auth)):
    conn = get_db()
    try:
        conn.execute("DELETE FROM schedules WHERE id = ?", (sched_id,))
        return {"ok": True}
    finally:
        conn.close()


# Admin Slots & Blocked Slots
@app.get("/api/admin/slots")
def get_admin_slots(doctor_id: int = Query(...), date: str = Query(...), _user: str = Depends(admin_auth)):
    conn = get_db()
    try:
        now = datetime.now(get_clinic_timezone())
        return slot_status(conn, doctor_id, date, now)
    finally:
        conn.close()


@app.post("/api/admin/blocked-slots")
def block_slot(payload: dict[str, Any], _user: str = Depends(admin_auth)):
    doc_id = payload.get("doctor_id")
    date_str = payload.get("date")
    time_str = payload.get("time")
    if not (doc_id and date_str and time_str):
        raise HTTPException(status_code=422, detail="doctor_id, date, and time are required")

    conn = get_db()
    try:
        cur = conn.execute(
            "SELECT id FROM appointments WHERE doctor_id = ? AND date = ? AND time = ? AND status = 'booked'",
            (doc_id, date_str, time_str),
        )
        if cur.fetchone():
            raise HTTPException(status_code=409, detail="Cannot block a slot that is already booked")
        conn.execute(
            "INSERT OR REPLACE INTO blocked_slots (doctor_id, date, time) VALUES (?, ?, ?)",
            (doc_id, date_str, time_str),
        )
        return {"ok": True}
    finally:
        conn.close()


@app.delete("/api/admin/blocked-slots")
def unblock_slot(doctor_id: int = Query(...), date: str = Query(...), time: str = Query(...), _user: str = Depends(admin_auth)):
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM blocked_slots WHERE doctor_id = ? AND date = ? AND time = ?",
            (doctor_id, date, time),
        )
        return {"ok": True}
    finally:
        conn.close()


# Admin Appointments
@app.get("/api/admin/appointments")
def list_appointments(date: Optional[str] = None, status: Optional[str] = None, _user: str = Depends(admin_auth)):
    conn = get_db()
    try:
        query = (
            "SELECT a.*, d.name as doctor_name, dep.name as department_name "
            "FROM appointments a "
            "JOIN doctors d ON a.doctor_id = d.id "
            "JOIN departments dep ON d.department_id = dep.id "
            "WHERE 1=1"
        )
        params: list[Any] = []
        if date:
            query += " AND a.date = ?"
            params.append(date)
        if status:
            query += " AND a.status = ?"
            params.append(status)
        query += " ORDER BY a.date DESC, a.time DESC"
        cur = conn.execute(query, params)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


@app.post("/api/admin/appointments/{appt_id}/cancel")
def cancel_appointment(appt_id: int, _user: str = Depends(admin_auth)):
    conn = get_db()
    try:
        cur = conn.execute("UPDATE appointments SET status = 'cancelled' WHERE id = ?", (appt_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Appointment not found")
        return {"ok": True}
    finally:
        conn.close()


# Admin Settings
@app.get("/api/admin/settings")
def get_settings_endpoint(_user: str = Depends(admin_auth)):
    conn = get_db()
    try:
        return get_settings(conn)
    finally:
        conn.close()


@app.put("/api/admin/settings")
def update_settings(payload: SettingsPayload, _user: str = Depends(admin_auth)):
    data = payload.model_dump(exclude_unset=True)
    conn = get_db()
    try:
        for k, v in data.items():
            if v is not None:
                set_setting(conn, k, str(v))
        return {"ok": True}
    finally:
        conn.close()
