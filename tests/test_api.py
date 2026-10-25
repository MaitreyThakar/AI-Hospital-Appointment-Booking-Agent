import os
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.db import get_db, init_db


@pytest.fixture
def client(test_db):
    conn = get_db(test_db)
    init_db(conn)
    conn.close()
    with TestClient(app) as c:
        yield c


def test_refuse_start_if_password_default(monkeypatch):
    from app.main import verify_admin_password
    monkeypatch.setenv("ADMIN_PASSWORD", "change-me")
    with pytest.raises(RuntimeError):
        verify_admin_password()


def test_public_config(client):
    res = client.get("/api/public-config")
    assert res.status_code == 200
    data = res.json()
    assert data["clinic_name"] == "CityCare Hospital"
    assert "welcome_message" in data


def test_chat_html_and_admin_auth(client):
    # Public chat page returns 200
    res_chat = client.get("/")
    assert res_chat.status_code == 200
    assert "Hospital Appointment Assistant" in res_chat.text

    # Admin without auth returns 401
    res_unauth = client.get("/admin")
    assert res_unauth.status_code == 401

    # Admin with correct auth returns 200
    res_admin = client.get("/admin", auth=("admin", "test-secret-123"))
    assert res_admin.status_code == 200
    assert "Hospital Admin Panel" in res_admin.text


def test_admin_crud_departments(client):
    auth = ("admin", "test-secret-123")
    # List depts
    res = client.get("/api/admin/departments", auth=auth)
    assert res.status_code == 200
    assert len(res.json()) >= 4

    # Create dept
    res_create = client.post("/api/admin/departments", json={"name": "Orthopedics", "description": "Bones and joints"}, auth=auth)
    assert res_create.status_code == 200
    dept_id = res_create.json()["id"]

    # Delete dept with doctors -> 409
    res_del_cardio = client.delete("/api/admin/departments/1", auth=auth)
    assert res_del_cardio.status_code == 409

    # Delete empty dept -> 200
    res_del_ortho = client.delete(f"/api/admin/departments/{dept_id}", auth=auth)
    assert res_del_ortho.status_code == 200


def test_admin_schedules_validation_and_overlap(client):
    auth = ("admin", "test-secret-123")
    # Overlapping schedule -> 409
    # Dr. 1 on Mon (0) has 09:00-13:00
    res_overlap = client.post(
        "/api/admin/schedules",
        json={"doctor_id": 1, "weekday": 0, "start_time": "10:00", "end_time": "12:00", "slot_minutes": 30},
        auth=auth,
    )
    assert res_overlap.status_code == 409

    # Invalid time start >= end -> 422
    res_bad = client.post(
        "/api/admin/schedules",
        json={"doctor_id": 1, "weekday": 0, "start_time": "14:00", "end_time": "13:00", "slot_minutes": 30},
        auth=auth,
    )
    assert res_bad.status_code == 422


def test_admin_settings_update_instant(client):
    auth = ("admin", "test-secret-123")
    res = client.put("/api/admin/settings", json={"clinic_name": "Apollo Care", "temperature": 0.5}, auth=auth)
    assert res.status_code == 200

    cfg = client.get("/api/public-config").json()
    assert cfg["clinic_name"] == "Apollo Care"
