import re
from typing import Literal, Optional, Any
from pydantic import BaseModel, Field, field_validator

IntentType = Literal[
    "book_appointment",
    "check_availability",
    "cancel_appointment",
    "reschedule_appointment",
    "general_inquiry",
    "out_of_scope",
]

BookingStatusType = Literal["not_started", "in_progress", "confirmed", "failed"]


class AgentTurn(BaseModel):
    reply: str = Field(..., min_length=1, max_length=1500)
    intent: IntentType
    patient_name: Optional[str] = None
    patient_phone: Optional[str] = None
    reason: Optional[str] = None
    department: Optional[str] = None
    doctor: Optional[str] = None
    date: Optional[str] = None
    time: Optional[str] = None

    @field_validator("date")
    @classmethod
    def validate_date_fmt(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            raise ValueError("date must be in YYYY-MM-DD format")
        return v

    @field_validator("time")
    @classmethod
    def validate_time_fmt(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not re.match(r"^\d{2}:\d{2}$", v):
            raise ValueError("time must be in HH:MM format")
        return v


class PatientInfo(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None


class AppointmentSlot(BaseModel):
    date: Optional[str] = None
    time: Optional[str] = None


class BookingOutput(BaseModel):
    intent: IntentType
    patient: PatientInfo
    reason: Optional[str] = None
    department: Optional[str] = None
    doctor: Optional[str] = None
    appointment: AppointmentSlot
    booking_status: BookingStatusType
    appointment_id: Optional[int] = None
    missing_information: list[str]


# Chat API schemas
class ChatRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=64)
    message: str = Field(..., min_length=1, max_length=1000)


class ChatResponse(BaseModel):
    reply: str
    data: BookingOutput
    trace: list[dict[str, Any]]


# Admin API schemas
class DepartmentPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = ""


class DoctorPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    department_id: int
    specialty: Optional[str] = ""
    active: Optional[int] = 1


class SchedulePayload(BaseModel):
    doctor_id: int
    weekday: int = Field(..., ge=0, le=6)
    start_time: str
    end_time: str
    slot_minutes: int = Field(..., ge=5, le=240)


class BlockedSlotPayload(BaseModel):
    doctor_id: int
    date: str
    time: str


class SettingsPayload(BaseModel):
    clinic_name: Optional[str] = Field(None, max_length=2000)
    agent_role: Optional[str] = Field(None, max_length=2000)
    conversation_style: Optional[str] = Field(None, max_length=2000)
    supported_services: Optional[str] = Field(None, max_length=2000)
    appointment_rules: Optional[str] = Field(None, max_length=2000)
    request_handling_instructions: Optional[str] = Field(None, max_length=2000)
    contact_info: Optional[str] = Field(None, max_length=2000)
    welcome_message: Optional[str] = Field(None, max_length=2000)
    min_notice_hours: Optional[int] = Field(None, ge=0, le=72)
    max_days_ahead: Optional[int] = Field(None, ge=1, le=180)
    max_active_bookings_per_phone: Optional[int] = Field(None, ge=1, le=10)
    temperature: Optional[float] = Field(None, ge=0.0, le=1.0)
