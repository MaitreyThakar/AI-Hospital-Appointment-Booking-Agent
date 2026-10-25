import json
import pytest
from types import SimpleNamespace
from datetime import datetime
from app.agent import Session, run_turn
from app.db import get_db
from tests.conftest import next_weekday


class FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class FakeChoice:
    def __init__(self, message):
        self.message = message


class FakeResponse:
    def __init__(self, message):
        self.choices = [FakeChoice(message)]


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise RuntimeError("No more fake responses configured")
        return self.responses.pop(0)


def make_tool_call(call_id, name, args_dict):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(args_dict)),
    )


def test_guard_invalid_json_then_valid_retry_succeeds(db_conn, fixed_now):
    """(a) invalid JSON then valid JSON -> retry succeeds."""
    session = Session("sess-a")
    valid_obj = {
        "reply": "I can help with that. Which department would you like?",
        "intent": "general_inquiry",
        "patient_name": None,
        "patient_phone": None,
        "reason": None,
        "department": None,
        "doctor": None,
        "date": None,
        "time": None,
    }
    client = FakeClient([
        FakeResponse(FakeMessage(content="Sure! Here is my response without valid JSON")),
        FakeResponse(FakeMessage(content=json.dumps(valid_obj))),
    ])

    res = run_turn(session, "Hello", client=client, now=fixed_now, conn=db_conn)
    assert len(client.calls) == 2
    # Verify corrective instruction in second call
    retry_msg = client.calls[1]["messages"][-1]["content"]
    assert "Your last output was invalid" in retry_msg
    assert res["reply"] == valid_obj["reply"]
    assert res["data"]["intent"] == "general_inquiry"


def test_guard_false_confirmation_triggers_corrective_retry(db_conn, fixed_now):
    """(b) reply claims 'confirmed' with no booking -> corrective retry."""
    session = Session("sess-b")
    false_claim = {
        "reply": "Your appointment is confirmed for tomorrow at 10:00 with Dr. Shah!",
        "intent": "book_appointment",
        "patient_name": "Test Patient",
        "patient_phone": "9876543210",
        "reason": None,
        "department": "Cardiology",
        "doctor": "Dr. Anil Shah",
        "date": "2026-10-27",
        "time": "10:00",
    }
    corrected = {
        "reply": "I can book Dr. Shah once we confirm the slot availability.",
        "intent": "book_appointment",
        "patient_name": "Test Patient",
        "patient_phone": "9876543210",
        "reason": None,
        "department": "Cardiology",
        "doctor": "Dr. Anil Shah",
        "date": "2026-10-27",
        "time": "10:00",
    }
    client = FakeClient([
        FakeResponse(FakeMessage(content=json.dumps(false_claim))),
        FakeResponse(FakeMessage(content=json.dumps(corrected))),
    ])

    res = run_turn(session, "Confirm my appointment", client=client, now=fixed_now, conn=db_conn)
    assert len(client.calls) == 2
    retry_msg = client.calls[1]["messages"][-1]["content"]
    assert "claimed confirmation without a successful book_appointment" in retry_msg
    assert res["reply"] == corrected["reply"]
    assert res["data"]["booking_status"] != "confirmed"


def test_guard_unoffered_slot_booking_rejected_and_no_db_row(db_conn, fixed_now):
    """(c) model calls book_appointment on a never-offered slot -> slot_not_offered and NO DB row."""
    session = Session("sess-c")
    mon_date = next_weekday(0, from_date=fixed_now.date())
    tool_call = make_tool_call(
        "tc_1",
        "book_appointment",
        {
            "doctor_id": 1,
            "date": mon_date,
            "time": "09:00",
            "patient_name": "Eve Sneak",
            "patient_phone": "9876543210",
            "patient_confirmed": True,
        },
    )
    final_turn = {
        "reply": "I couldn't book that slot because it was not verified.",
        "intent": "book_appointment",
        "patient_name": "Eve Sneak",
        "patient_phone": "9876543210",
        "reason": None,
        "department": "Cardiology",
        "doctor": "Dr. Anil Shah",
        "date": mon_date,
        "time": "09:00",
    }
    client = FakeClient([
        FakeResponse(FakeMessage(content="", tool_calls=[tool_call])),
        FakeResponse(FakeMessage(content=json.dumps(final_turn))),
    ])

    res = run_turn(session, "Book without checking", client=client, now=fixed_now, conn=db_conn)
    # Check trace
    assert len(res["trace"]) == 1
    assert res["trace"][0]["result"]["error"] == "slot_not_offered"

    # Verify no row in appointments table
    cur = db_conn.execute("SELECT COUNT(*) as count FROM appointments WHERE patient_phone = '9876543210'")
    assert cur.fetchone()["count"] == 0
    assert res["data"]["booking_status"] == "failed"


def test_guard_always_invalid_json_uses_safe_fallback(db_conn, fixed_now):
    """(d) model never returns valid JSON -> fallback reply."""
    session = Session("sess-d")
    client = FakeClient([
        FakeResponse(FakeMessage(content="Bad output round 1")),
        FakeResponse(FakeMessage(content="Bad output round 2")),
    ])

    res = run_turn(session, "Hi there", client=client, now=fixed_now, conn=db_conn)
    assert len(client.calls) == 2
    assert "Sorry, I couldn't process that" in res["reply"]
    assert "Front desk" in res["reply"]
    assert res["data"]["intent"] == "general_inquiry"


def test_guard_booking_status_and_missing_information_code_computed(db_conn, fixed_now):
    """(e) booking_status and missing_information are computed by code, not by model text."""
    session = Session("sess-e")
    mon_date = next_weekday(0, from_date=fixed_now.date())
    turn = {
        "reply": "I have noted Dr. Shah on next Monday. May I have your full name and phone number?",
        "intent": "book_appointment",
        "patient_name": None,
        "patient_phone": None,
        "reason": None,
        "department": "Cardiology",
        "doctor": "Dr. Anil Shah",
        "date": mon_date,
        "time": None,
    }
    client = FakeClient([
        FakeResponse(FakeMessage(content=json.dumps(turn))),
    ])

    res = run_turn(session, "I want to see Dr. Shah next Monday", client=client, now=fixed_now, conn=db_conn)
    data = res["data"]
    # booking_status must be in_progress because intent is book_appointment and no booking completed
    assert data["booking_status"] == "in_progress"
    # missing_information computed by code: patient_name, patient_phone, time
    assert "patient_name" in data["missing_information"]
    assert "patient_phone" in data["missing_information"]
    assert "time" in data["missing_information"]
    assert "department_or_doctor" not in data["missing_information"]
    assert "date" not in data["missing_information"]
