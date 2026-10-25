import os
import tempfile
import pytest
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

# Set test environment defaults before module imports
os.environ.setdefault("ADMIN_USER", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "test-secret-123")
os.environ.setdefault("CLINIC_TIMEZONE", "Asia/Kolkata")
os.environ.setdefault("LLM_MODEL", "llama-3.3-70b-versatile")

from app.db import get_db, init_db
from app.availability import get_clinic_timezone


@pytest.fixture(autouse=True)
def test_db(monkeypatch):
    """Creates a temporary sqlite database file for each test, initializes and seeds it."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_path = f.name
    
    monkeypatch.setenv("DB_PATH", temp_path)
    monkeypatch.setenv("ADMIN_PASSWORD", "test-secret-123")
    conn = get_db(temp_path)
    init_db(conn)
    conn.close()

    yield temp_path

    if os.path.exists(temp_path):
        try:
            os.remove(temp_path)
        except Exception:
            pass


@pytest.fixture
def db_conn(test_db):
    conn = get_db(test_db)
    yield conn
    conn.close()


def next_weekday(n: int, from_date: date | None = None) -> str:
    """Returns a date string YYYY-MM-DD on weekday n that is >= 3 days ahead."""
    tz = get_clinic_timezone()
    base = from_date if from_date is not None else datetime.now(tz).date()
    cand = base + timedelta(days=3)
    while cand.weekday() != n:
        cand += timedelta(days=1)
    return cand.strftime("%Y-%m-%d")


@pytest.fixture
def fixed_now():
    """A fixed Monday 10:00 AM datetime in clinic timezone."""
    tz = get_clinic_timezone()
    return datetime(2026, 10, 26, 10, 0, 0, tzinfo=tz)
