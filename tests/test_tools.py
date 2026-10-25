import concurrent.futures
from datetime import datetime, timedelta, time
from app.db import get_db
from app.availability import (
    get_doctor_generated_slots,
    slot_status,
    get_slots,
    check_availability,
    book_appointment,
    check_schedule_overlap,
)
from tests.conftest import next_weekday


def test_slot_generation_per_schedule(db_conn):
    # Dr. Anil Shah (id=1): Mon-Fri 09:00-13:00, 30 min slots
    # Next Monday (weekday 0)
    mon_date = next_weekday(0)
    slots = get_doctor_generated_slots(db_conn, 1, mon_date)
    expected = ["09:00", "09:30", "10:00", "10:30", "11:00", "11:30", "12:00", "12:30"]
    assert slots == expected


def test_booked_and_blocked_slots_hidden(db_conn, fixed_now):
    mon_date = next_weekday(0, from_date=fixed_now.date())
    # Book 09:00
    res = book_appointment(
        db_conn,
        {
            "doctor_id": 1,
            "date": mon_date,
            "time": "09:00",
            "patient_name": "Test Patient",
            "patient_phone": "9876543210",
            "patient_confirmed": True,
        },
        fixed_now,
    )
    assert res["ok"] is True

    # Block 09:30
    db_conn.execute(
        "INSERT INTO blocked_slots (doctor_id, date, time) VALUES (?, ?, ?)",
        (1, mon_date, "09:30"),
    )

    # Check status
    statuses = slot_status(db_conn, 1, mon_date, fixed_now)
    status_map = {s["time"]: s["status"] for s in statuses}
    assert status_map["09:00"] == "booked"
    assert status_map["09:30"] == "blocked"
    assert status_map["10:00"] == "available"

    # check_availability hides booked and blocked slots
    avail = check_availability(db_conn, {"date": mon_date, "doctor_name": "Shah"}, fixed_now)
    assert avail["ok"] is True
    doc_slots = avail["doctors"][0]["slots"]
    assert "09:00" not in doc_slots
    assert "09:30" not in doc_slots
    assert "10:00" in doc_slots


def test_double_booking_rejected(db_conn, fixed_now):
    mon_date = next_weekday(0, from_date=fixed_now.date())
    res1 = book_appointment(
        db_conn,
        {
            "doctor_id": 1,
            "date": mon_date,
            "time": "10:00",
            "patient_name": "Alice Smith",
            "patient_phone": "9876543210",
            "patient_confirmed": True,
        },
        fixed_now,
    )
    assert res1["ok"] is True
    assert res1["status"] == "confirmed"

    res2 = book_appointment(
        db_conn,
        {
            "doctor_id": 1,
            "date": mon_date,
            "time": "10:00",
            "patient_name": "Bob Jones",
            "patient_phone": "9876543211",
            "patient_confirmed": True,
        },
        fixed_now,
    )
    assert res2["ok"] is False
    assert res2["error"] == "slot_taken"


def test_concurrent_booking_exactly_one_succeeds(test_db, fixed_now):
    mon_date = next_weekday(0, from_date=fixed_now.date())

    def try_book(phone, name):
        conn = get_db(test_db)
        try:
            return book_appointment(
                conn,
                {
                    "doctor_id": 1,
                    "date": mon_date,
                    "time": "11:00",
                    "patient_name": name,
                    "patient_phone": phone,
                    "patient_confirmed": True,
                },
                fixed_now,
            )
        finally:
            conn.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(try_book, "9876543210", "User One")
        f2 = executor.submit(try_book, "9876543211", "User Two")
        res1 = f1.result()
        res2 = f2.result()

    results = [res1, res2]
    oks = [r for r in results if r.get("ok") is True]
    fails = [r for r in results if r.get("ok") is False]
    assert len(oks) == 1
    assert len(fails) == 1
    assert fails[0]["error"] == "slot_taken"


def test_duplicate_returns_already_booked(db_conn, fixed_now):
    mon_date = next_weekday(0, from_date=fixed_now.date())
    payload = {
        "doctor_id": 1,
        "date": mon_date,
        "time": "11:30",
        "patient_name": "Priya Patel",
        "patient_phone": "9876543210",
        "patient_confirmed": True,
    }
    res1 = book_appointment(db_conn, payload, fixed_now)
    assert res1["ok"] is True
    assert res1["status"] == "confirmed"

    # Same phone, doctor, date, time
    res2 = book_appointment(db_conn, payload, fixed_now)
    assert res2["ok"] is True
    assert res2["status"] == "already_booked"
    assert res2["appointment_id"] == res1["appointment_id"]


def test_invalid_phone_and_name(db_conn, fixed_now):
    mon_date = next_weekday(0, from_date=fixed_now.date())
    base = {
        "doctor_id": 1,
        "date": mon_date,
        "time": "12:00",
        "patient_confirmed": True,
    }
    # Invalid names
    for bad_name in ["", "   ", "a", "12345", "###"]:
        r = book_appointment(db_conn, {**base, "patient_name": bad_name, "patient_phone": "9876543210"}, fixed_now)
        assert r["ok"] is False
        assert r["error"] == "invalid_name"

    # Invalid phones
    for bad_phone in ["123", "abc", "phone-number", "12345", "123456"]:
        r = book_appointment(db_conn, {**base, "patient_name": "Valid Name", "patient_phone": bad_phone}, fixed_now)
        assert r["ok"] is False
        assert r["error"] == "invalid_phone"


def test_not_confirmed(db_conn, fixed_now):
    mon_date = next_weekday(0, from_date=fixed_now.date())
    r = book_appointment(
        db_conn,
        {
            "doctor_id": 1,
            "date": mon_date,
            "time": "09:00",
            "patient_name": "John Doe",
            "patient_phone": "9876543210",
            "patient_confirmed": False,
        },
        fixed_now,
    )
    assert r["ok"] is False
    assert r["error"] == "not_confirmed"


def test_date_in_past_and_beyond_window(db_conn, fixed_now):
    past_date = (fixed_now.date() - timedelta(days=1)).strftime("%Y-%m-%d")
    r_past = book_appointment(
        db_conn,
        {
            "doctor_id": 1,
            "date": past_date,
            "time": "09:00",
            "patient_name": "John Doe",
            "patient_phone": "9876543210",
            "patient_confirmed": True,
        },
        fixed_now,
    )
    assert r_past["ok"] is False
    assert r_past["error"] == "date_in_past"

    far_date = (fixed_now.date() + timedelta(days=35)).strftime("%Y-%m-%d")
    r_far = book_appointment(
        db_conn,
        {
            "doctor_id": 1,
            "date": far_date,
            "time": "09:00",
            "patient_name": "John Doe",
            "patient_phone": "9876543210",
            "patient_confirmed": True,
        },
        fixed_now,
    )
    assert r_far["ok"] is False
    assert r_far["error"] == "beyond_booking_window"


def test_too_soon_notice(db_conn, fixed_now):
    # fixed_now is Monday 10:00 AM. Dr Shah has slots at 09:00, 09:30, 10:00, 10:30, 11:00.
    # min_notice_hours is 2 hours -> cutoff is 12:00 PM.
    # 10:30 is < 12:00, so too_soon
    today_str = fixed_now.date().strftime("%Y-%m-%d")
    r = book_appointment(
        db_conn,
        {
            "doctor_id": 1,
            "date": today_str,
            "time": "10:30",
            "patient_name": "John Doe",
            "patient_phone": "9876543210",
            "patient_confirmed": True,
        },
        fixed_now,
    )
    assert r["ok"] is False
    assert r["error"] == "too_soon"


def test_limit_reached(db_conn, fixed_now):
    mon_date = next_weekday(0, from_date=fixed_now.date())
    phone = "9876543210"
    # Book 3 slots (max_active_bookings_per_phone default is 3)
    for slot_time in ["09:00", "09:30", "10:00"]:
        res = book_appointment(
            db_conn,
            {
                "doctor_id": 1,
                "date": mon_date,
                "time": slot_time,
                "patient_name": "Active User",
                "patient_phone": phone,
                "patient_confirmed": True,
            },
            fixed_now,
        )
        assert res["ok"] is True

    # 4th booking should hit limit_reached
    res4 = book_appointment(
        db_conn,
        {
            "doctor_id": 1,
            "date": mon_date,
            "time": "10:30",
            "patient_name": "Active User",
            "patient_phone": phone,
            "patient_confirmed": True,
        },
        fixed_now,
    )
    assert res4["ok"] is False
    assert res4["error"] == "limit_reached"


def test_patient_conflict(db_conn, fixed_now):
    mon_date = next_weekday(0, from_date=fixed_now.date())
    phone = "9876543210"
    # Book with Dr. Shah at 09:00
    res1 = book_appointment(
        db_conn,
        {
            "doctor_id": 1,
            "date": mon_date,
            "time": "09:00",
            "patient_name": "Busy Patient",
            "patient_phone": phone,
            "patient_confirmed": True,
        },
        fixed_now,
    )
    assert res1["ok"] is True

    # Attempt to book Dr. Kavita Desai (doctor_id 4) at 09:00 on the same date with same phone
    res2 = book_appointment(
        db_conn,
        {
            "doctor_id": 4,
            "date": mon_date,
            "time": "09:00",
            "patient_name": "Busy Patient",
            "patient_phone": phone,
            "patient_confirmed": True,
        },
        fixed_now,
    )
    assert res2["ok"] is False
    assert res2["error"] == "patient_conflict"


def test_admin_schedule_overlap_rejection(db_conn):
    # Dr. 1 has Mon 09:00-13:00
    # Overlapping 10:00-12:00 -> True
    assert check_schedule_overlap(db_conn, 1, 0, "10:00", "12:00") is True
    # Overlapping 08:00-10:00 -> True
    assert check_schedule_overlap(db_conn, 1, 0, "08:00", "10:00") is True
    # Non-overlapping 14:00-16:00 -> False
    assert check_schedule_overlap(db_conn, 1, 0, "14:00", "16:00") is False


def test_cancelled_appointment_frees_slot(db_conn, fixed_now):
    mon_date = next_weekday(0, from_date=fixed_now.date())
    # Book 09:00
    res = book_appointment(
        db_conn,
        {
            "doctor_id": 1,
            "date": mon_date,
            "time": "09:00",
            "patient_name": "Cancel Me",
            "patient_phone": "9876543210",
            "patient_confirmed": True,
        },
        fixed_now,
    )
    assert res["ok"] is True
    appt_id = res["appointment_id"]
    assert "09:00" not in get_slots(db_conn, 1, mon_date, fixed_now)

    # Cancel
    db_conn.execute("UPDATE appointments SET status = 'cancelled' WHERE id = ?", (appt_id,))
    assert "09:00" in get_slots(db_conn, 1, mon_date, fixed_now)

    # Re-book by another patient
    res2 = book_appointment(
        db_conn,
        {
            "doctor_id": 1,
            "date": mon_date,
            "time": "09:00",
            "patient_name": "New Patient",
            "patient_phone": "9876543212",
            "patient_confirmed": True,
        },
        fixed_now,
    )
    assert res2["ok"] is True
    assert res2["status"] == "confirmed"
