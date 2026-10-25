import os
import sqlite3
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS departments(
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS doctors(
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    department_id INTEGER NOT NULL REFERENCES departments(id),
    specialty TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS schedules(
    id INTEGER PRIMARY KEY,
    doctor_id INTEGER NOT NULL REFERENCES doctors(id),
    weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    slot_minutes INTEGER NOT NULL CHECK(slot_minutes BETWEEN 5 AND 240)
);
CREATE TABLE IF NOT EXISTS blocked_slots(
    doctor_id INTEGER NOT NULL REFERENCES doctors(id),
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    PRIMARY KEY(doctor_id, date, time)
);
CREATE TABLE IF NOT EXISTS appointments(
    id INTEGER PRIMARY KEY,
    doctor_id INTEGER NOT NULL REFERENCES doctors(id),
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    patient_name TEXT NOT NULL,
    patient_phone TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'booked' CHECK(status IN ('booked','cancelled')),
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_slot ON appointments(doctor_id, date, time) WHERE status='booked';
CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "clinic_name": "CityCare Hospital",
    "agent_role": "You are the appointment assistant for CityCare Hospital. You help patients book appointments with our doctors.",
    "conversation_style": "Warm, calm and concise. Plain language. No emojis. Ask at most two questions per message.",
    "supported_services": "Booking new appointments with doctors in the listed departments, and checking doctor availability. NOT supported in this chat: cancellations, rescheduling, test results, prescriptions, billing, medical questions.",
    "appointment_rules": "Appointments can only be booked in available slots. Full name and phone number are required. Emergency and walk-in care are not handled in this chat.",
    "request_handling_instructions": "If the patient does not know which department to choose, ask them to describe their concern in their own words and suggest the best-matching department from the list by general body area only (for example heart -> Cardiology, skin -> Dermatology), saying the doctor will assess them. For cancellation, rescheduling or any unsupported request, politely give the contact info.",
    "contact_info": "Front desk +91-00000-00000, 09:00-18:00. In an emergency call your local emergency number.",
    "welcome_message": "Hello! I can help you book an appointment. What can I help you with today?",
    "min_notice_hours": "2",
    "max_days_ahead": "30",
    "max_active_bookings_per_phone": "3",
    "temperature": "0.2",
}


def get_db(db_path: str | None = None) -> sqlite3.Connection:
    if db_path is None:
        db_path = os.getenv("DB_PATH", "data/hospital.db")
    if db_path != ":memory:":
        dir_name = os.path.dirname(db_path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as count FROM doctors")
    if cur.fetchone()["count"] == 0:
        seed_db(conn)


def seed_db(conn: sqlite3.Connection) -> None:
    # Seed default settings
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, str(v)))

    # Seed departments
    depts = [
        ("Cardiology", "Heart and cardiovascular care"),
        ("Dermatology", "Skin, hair and nail conditions"),
        ("General Medicine", "Primary adult healthcare"),
        ("Pediatrics", "Infant, child and adolescent health"),
    ]
    dept_ids = {}
    for name, desc in depts:
        cur = conn.execute("INSERT INTO departments (name, description) VALUES (?, ?)", (name, desc))
        dept_ids[name] = cur.lastrowid

    # Seed doctors & schedules: Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6
    doctors_data = [
        ("Dr. Anil Shah", dept_ids["Cardiology"], "Interventional Cardiology", [0, 1, 2, 3, 4], "09:00", "13:00", 30),
        ("Dr. Meera Iyer", dept_ids["Cardiology"], "Preventive Cardiology", [1, 3], "14:00", "18:00", 30),
        ("Dr. Rohan Mehta", dept_ids["Dermatology"], "Clinical Dermatology", [0, 2, 4], "10:00", "16:00", 20),
        ("Dr. Kavita Desai", dept_ids["General Medicine"], "Internal Medicine", [0, 1, 2, 3, 4, 5], "09:00", "17:00", 15),
        ("Dr. Sana Khan", dept_ids["Pediatrics"], "Child Health", [0, 1, 2, 3, 4], "10:00", "14:00", 20),
    ]
    for name, d_id, spec, weekdays, start, end, slot_min in doctors_data:
        cur = conn.execute("INSERT INTO doctors (name, department_id, specialty, active) VALUES (?, ?, ?, 1)", (name, d_id, spec))
        doc_id = cur.lastrowid
        for day in weekdays:
            conn.execute(
                "INSERT INTO schedules (doctor_id, weekday, start_time, end_time, slot_minutes) VALUES (?, ?, ?, ?, ?)",
                (doc_id, day, start, end, slot_min),
            )


def get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    cur = conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    return row["value"] if row else default


def get_settings(conn: sqlite3.Connection) -> dict[str, str]:
    cur = conn.execute("SELECT key, value FROM settings")
    return {row["key"]: row["value"] for row in cur.fetchall()}


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
