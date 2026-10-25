import os
import re
import sqlite3
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from typing import Any
from app.db import get_setting


def get_clinic_timezone() -> ZoneInfo:
    tz_name = os.getenv("CLINIC_TIMEZONE", "Asia/Kolkata")
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("Asia/Kolkata")


def parse_time_to_minutes(time_str: str) -> int:
    h, m = map(int, time_str.split(":"))
    return h * 60 + m


def minutes_to_time(minutes: int) -> str:
    h = minutes // 60
    m = minutes % 60
    return f"{h:02d}:{m:02d}"


def generate_slots(start_time: str, end_time: str, slot_minutes: int) -> list[str]:
    start = parse_time_to_minutes(start_time)
    end = parse_time_to_minutes(end_time)
    slots = []
    curr = start
    while curr + slot_minutes <= end:
        slots.append(minutes_to_time(curr))
        curr += slot_minutes
    return slots


def get_doctor_generated_slots(conn: sqlite3.Connection, doctor_id: int, date_str: str) -> list[str]:
    dt = datetime.strptime(date_str, "%Y-%m-%d").date()
    weekday = dt.weekday()
    cur = conn.execute(
        "SELECT start_time, end_time, slot_minutes FROM schedules WHERE doctor_id = ? AND weekday = ? ORDER BY start_time",
        (doctor_id, weekday),
    )
    schedules = cur.fetchall()
    all_slots = []
    for s in schedules:
        all_slots.extend(generate_slots(s["start_time"], s["end_time"], s["slot_minutes"]))
    return sorted(list(dict.fromkeys(all_slots)))


def slot_status(conn: sqlite3.Connection, doctor_id: int, date_str: str, now: datetime) -> list[dict[str, str]]:
    all_slots = get_doctor_generated_slots(conn, doctor_id, date_str)
    if not all_slots:
        return []

    # Get booked slots
    cur = conn.execute(
        "SELECT time FROM appointments WHERE doctor_id = ? AND date = ? AND status = 'booked'",
        (doctor_id, date_str),
    )
    booked = {r["time"] for r in cur.fetchall()}

    # Get blocked slots
    cur = conn.execute(
        "SELECT time FROM blocked_slots WHERE doctor_id = ? AND date = ?",
        (doctor_id, date_str),
    )
    blocked = {r["time"] for r in cur.fetchall()}

    min_notice = int(get_setting(conn, "min_notice_hours", 2))
    cutoff = now + timedelta(hours=min_notice)
    target_date = datetime.strptime(date_str, "%Y-%m-%d").date()

    res = []
    for t_str in all_slots:
        h, m = map(int, t_str.split(":"))
        slot_dt = datetime.combine(target_date, time(h, m), tzinfo=now.tzinfo)
        if t_str in booked:
            status = "booked"
        elif t_str in blocked:
            status = "blocked"
        elif slot_dt < cutoff:
            status = "past"
        else:
            status = "available"
        res.append({"time": t_str, "status": status})
    return res


def get_slots(conn: sqlite3.Connection, doctor_id: int, date_str: str, now: datetime) -> list[str]:
    return [s["time"] for s in slot_status(conn, doctor_id, date_str, now) if s["status"] == "available"]


def validate_date(date_str: str, now: datetime, max_days_ahead: int) -> str | None:
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        return "invalid_date"
    today = now.date()
    if dt < today:
        return "date_in_past"
    if dt > today + timedelta(days=max_days_ahead):
        return "beyond_booking_window"
    return None


def normalize_phone(phone: str) -> str | None:
    if not phone or not isinstance(phone, str):
        return None
    cleaned = re.sub(r"[\s\-\(\)]", "", phone)
    if re.match(r"^\+?\d{7,15}$", cleaned):
        return cleaned
    return None


def validate_name(name: str) -> str | None:
    if not name or not isinstance(name, str):
        return None
    cleaned = name.strip()
    if 2 <= len(cleaned) <= 80 and re.search(r"[a-zA-Z]", cleaned):
        return cleaned
    return None


def check_availability(conn: sqlite3.Connection, args: dict[str, Any], now: datetime) -> dict[str, Any]:
    date_str = args.get("date")
    department = args.get("department")
    doctor_name = args.get("doctor_name")

    if not date_str:
        return {"ok": False, "error": "invalid_date", "message": "Date is required."}

    max_days = int(get_setting(conn, "max_days_ahead", 30))
    date_err = validate_date(date_str, now, max_days)
    if date_err:
        return {"ok": False, "error": date_err, "message": f"Date check failed: {date_err}"}

    if not department and not doctor_name:
        return {"ok": False, "error": "missing_filter", "message": "Provide department or doctor_name."}

    dept_id = None
    if department:
        cur = conn.execute("SELECT id, name FROM departments WHERE LOWER(name) = LOWER(?)", (department.strip(),))
        d_row = cur.fetchone()
        if not d_row:
            return {"ok": False, "error": "unknown_department", "message": f"Unknown department: {department}"}
        dept_id = d_row["id"]

    if doctor_name:
        # Strip common title prefixes and split into search terms
        raw_name = doctor_name.strip().lower()
        cleaned_search = re.sub(r"^dr\.?\s+", "", raw_name)
        terms = [t for t in cleaned_search.split() if len(t) >= 2] or [raw_name]
        
        # Build conditions: either exact substring of full string, or all search terms present
        like_clauses = " AND ".join(["LOWER(d.name) LIKE ?"] * len(terms))
        query = (
            "SELECT d.id, d.name, d.specialty, dep.name as department "
            "FROM doctors d JOIN departments dep ON d.department_id = dep.id "
            f"WHERE d.active = 1 AND ({like_clauses} OR LOWER(d.name) LIKE ?)"
        )
        params = [f"%{t}%" for t in terms] + [f"%{raw_name}%"]
        if dept_id is not None:
            query += " AND d.department_id = ?"
            params.append(dept_id)
        cur = conn.execute(query, params)
        docs = cur.fetchall()
        if not docs:
            return {"ok": False, "error": "unknown_doctor", "message": f"Unknown doctor: {doctor_name}"}

    else:
        cur = conn.execute(
            "SELECT d.id, d.name, d.specialty, dep.name as department "
            "FROM doctors d JOIN departments dep ON d.department_id = dep.id "
            "WHERE d.active = 1 AND d.department_id = ?",
            (dept_id,),
        )
        docs = cur.fetchall()

    doctors_out = []
    for doc in docs:
        cur_sched = conn.execute(
            "SELECT slot_minutes FROM schedules WHERE doctor_id = ? LIMIT 1", (doc["id"],)
        )
        s_row = cur_sched.fetchone()
        slot_min = s_row["slot_minutes"] if s_row else 30
        avail = get_slots(conn, doc["id"], date_str, now)
        if avail:
            doctors_out.append({
                "doctor_id": doc["id"],
                "name": doc["name"],
                "department": doc["department"],
                "specialty": doc["specialty"],
                "slot_minutes": slot_min,
                "slots": avail,
            })

    alternatives_out = []
    if not doctors_out:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        max_date = now.date() + timedelta(days=max_days)
        scan_date = target_date + timedelta(days=1)
        while scan_date <= max_date and len(alternatives_out) < 3:
            s_str = scan_date.strftime("%Y-%m-%d")
            for doc in docs:
                avail = get_slots(conn, doc["id"], s_str, now)
                if avail:
                    alternatives_out.append({
                        "date": s_str,
                        "doctor_id": doc["id"],
                        "name": doc["name"],
                        "slots": avail[:4],
                    })
                    if len(alternatives_out) >= 3:
                        break
            scan_date += timedelta(days=1)

    return {
        "ok": True,
        "date": date_str,
        "doctors": doctors_out,
        "alternatives": alternatives_out,
    }


def book_appointment(conn: sqlite3.Connection, args: dict[str, Any], now: datetime) -> dict[str, Any]:
    doctor_id = args.get("doctor_id")
    date_str = args.get("date")
    time_str = args.get("time")
    patient_name = args.get("patient_name")
    patient_phone = args.get("patient_phone")
    reason = args.get("reason", "") or ""
    patient_confirmed = args.get("patient_confirmed", False)

    conn.execute("BEGIN IMMEDIATE")
    try:
        # 1 not_confirmed (patient_confirmed is not true)
        if not patient_confirmed:
            conn.execute("ROLLBACK")
            return {"ok": False, "error": "not_confirmed", "message": "Patient confirmation is required."}

        # 2 invalid_name
        valid_name = validate_name(patient_name)
        if not valid_name:
            conn.execute("ROLLBACK")
            return {"ok": False, "error": "invalid_name", "message": "Name must be 2-80 characters and contain a letter."}

        # 3 invalid_phone
        norm_phone = normalize_phone(patient_phone)
        if not norm_phone:
            conn.execute("ROLLBACK")
            return {"ok": False, "error": "invalid_phone", "message": "Phone number must be 7-15 digits."}

        # 4 invalid_date / date_in_past / beyond_booking_window
        max_days = int(get_setting(conn, "max_days_ahead", 30))
        date_err = validate_date(date_str, now, max_days)
        if date_err:
            conn.execute("ROLLBACK")
            return {"ok": False, "error": date_err, "message": f"Date check failed: {date_err}"}

        # 5 unknown_doctor (missing/inactive)
        cur = conn.execute(
            "SELECT d.id, d.name, dep.name as department FROM doctors d JOIN departments dep ON d.department_id = dep.id WHERE d.id = ? AND d.active = 1",
            (doctor_id,),
        )
        doc = cur.fetchone()
        if not doc:
            conn.execute("ROLLBACK")
            return {"ok": False, "error": "unknown_doctor", "message": "Doctor not found or inactive."}

        # 6 duplicate: same normalized phone already has a booked appointment with this doctor/date/time
        cur = conn.execute(
            "SELECT id FROM appointments WHERE doctor_id = ? AND date = ? AND time = ? AND patient_phone = ? AND status = 'booked'",
            (doctor_id, date_str, time_str, norm_phone),
        )
        existing = cur.fetchone()
        if existing:
            conn.execute("ROLLBACK")
            return {
                "ok": True,
                "status": "already_booked",
                "appointment_id": existing["id"],
                "doctor": doc["name"],
                "department": doc["department"],
                "date": date_str,
                "time": time_str,
            }

        # 7 invalid_slot (time not in the generated slots for that doctor/date)
        gen_slots = get_doctor_generated_slots(conn, doctor_id, date_str)
        if time_str not in gen_slots:
            conn.execute("ROLLBACK")
            return {"ok": False, "error": "invalid_slot", "message": "Time is not a valid slot for this doctor/date."}

        # 8 too_soon (slot < now + min_notice_hours)
        min_notice = int(get_setting(conn, "min_notice_hours", 2))
        cutoff = now + timedelta(hours=min_notice)
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        h, m = map(int, time_str.split(":"))
        slot_dt = datetime.combine(target_date, time(h, m), tzinfo=now.tzinfo)
        if slot_dt < cutoff:
            conn.execute("ROLLBACK")
            return {"ok": False, "error": "too_soon", "message": "Slot is within the minimum notice window."}

        # 9 slot_taken (booked or blocked)
        cur = conn.execute(
            "SELECT id FROM appointments WHERE doctor_id = ? AND date = ? AND time = ? AND status = 'booked'",
            (doctor_id, date_str, time_str),
        )
        if cur.fetchone():
            conn.execute("ROLLBACK")
            return {"ok": False, "error": "slot_taken", "message": "Slot is already booked."}
        cur = conn.execute(
            "SELECT 1 FROM blocked_slots WHERE doctor_id = ? AND date = ? AND time = ?",
            (doctor_id, date_str, time_str),
        )
        if cur.fetchone():
            conn.execute("ROLLBACK")
            return {"ok": False, "error": "slot_taken", "message": "Slot is blocked by administration."}

        # 10 patient_conflict (same phone has a booked appointment at same date+time with another doctor)
        cur = conn.execute(
            "SELECT id FROM appointments WHERE patient_phone = ? AND date = ? AND time = ? AND doctor_id != ? AND status = 'booked'",
            (norm_phone, date_str, time_str, doctor_id),
        )
        if cur.fetchone():
            conn.execute("ROLLBACK")
            return {"ok": False, "error": "patient_conflict", "message": "Patient already has an appointment at this date and time."}

        # 11 limit_reached (phone has >= max_active_bookings_per_phone booked appointments with date >= today)
        max_active = int(get_setting(conn, "max_active_bookings_per_phone", 3))
        today_str = now.date().isoformat()
        cur = conn.execute(
            "SELECT COUNT(*) as count FROM appointments WHERE patient_phone = ? AND date >= ? AND status = 'booked'",
            (norm_phone, today_str),
        )
        if cur.fetchone()["count"] >= max_active:
            conn.execute("ROLLBACK")
            return {"ok": False, "error": "limit_reached", "message": "Maximum active bookings limit reached for this phone number."}

        # INSERT appointment
        created_at = now.isoformat()
        try:
            cur = conn.execute(
                "INSERT INTO appointments (doctor_id, date, time, patient_name, patient_phone, reason, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'booked', ?)",
                (doctor_id, date_str, time_str, valid_name, norm_phone, reason, created_at),
            )
            appt_id = cur.lastrowid
            conn.execute("COMMIT")
            return {
                "ok": True,
                "status": "confirmed",
                "appointment_id": appt_id,
                "doctor": doc["name"],
                "department": doc["department"],
                "date": date_str,
                "time": time_str,
            }
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            return {"ok": False, "error": "slot_taken", "message": "Slot was just taken by another booking."}
    except Exception:
        conn.execute("ROLLBACK")
        raise


def check_schedule_overlap(
    conn: sqlite3.Connection, doctor_id: int, weekday: int, start_time: str, end_time: str, exclude_id: int | None = None
) -> bool:
    query = "SELECT id, start_time, end_time FROM schedules WHERE doctor_id = ? AND weekday = ?"
    params: list[Any] = [doctor_id, weekday]
    if exclude_id is not None:
        query += " AND id != ?"
        params.append(exclude_id)
    cur = conn.execute(query, params)
    rows = cur.fetchall()
    new_s = parse_time_to_minutes(start_time)
    new_e = parse_time_to_minutes(end_time)
    for r in rows:
        ex_s = parse_time_to_minutes(r["start_time"])
        ex_e = parse_time_to_minutes(r["end_time"])
        if new_s < ex_e and new_e > ex_s:
            return True
    return False
