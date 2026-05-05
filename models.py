"""
Pydantic Models for API request/response validation
These are separate from SQLAlchemy DB models in database.py
"""
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


# ─── Auth Models ────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    token:    str
    role:     str
    username: str

class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


# ─── User Models ────────────────────────────────────────────────

class UserCreate(BaseModel):
    name:     str = Field(..., min_length=1, max_length=100)
    rfid_uid: str = Field(..., min_length=3, max_length=50)
    role:     Optional[str] = "user"

class UserUpdate(BaseModel):
    name:     Optional[str]  = None
    active:   Optional[bool] = None
    role:     Optional[str]  = None
    rfid_uid: Optional[str]  = None

class UserResponse(BaseModel):
    id:         int
    name:       str
    rfid_uid:   str
    role:       str
    active:     bool
    created_at: datetime

    class Config:
        from_attributes = True

class WebUserCreate(BaseModel):
    username:    str = Field(..., min_length=2, max_length=50)
    password:    str = Field(..., min_length=4)
    role:        Optional[str] = "user"
    linked_rfid: Optional[str] = None

class WebUserResponse(BaseModel):
    id:          int
    username:    str
    role:        str
    linked_rfid: Optional[str]

    class Config:
        from_attributes = True


# ─── Hardware / Sensor Models ────────────────────────────────────

class MotionPayload(BaseModel):
    motion:    bool
    timestamp: Optional[int] = None

class RFIDPayload(BaseModel):
    uid: str

class HeartbeatPayload(BaseModel):
    door_open:    bool
    alarm_active: bool
    motion:       bool
    camera_angle: int
    wifi_rssi:    Optional[int] = None
    uptime_ms:    Optional[int] = None

class CameraRegisterPayload(BaseModel):
    stream_url:   str
    snapshot_url: str


# ─── Control Models ─────────────────────────────────────────────

class DoorControlRequest(BaseModel):
    action: str  # "open" or "close"

class AlarmControlRequest(BaseModel):
    action: str  # "on" or "off"

class CameraControlRequest(BaseModel):
    angle: int = Field(..., ge=0, le=180)


# ─── Log Models ─────────────────────────────────────────────────

class AccessLogResponse(BaseModel):
    id:          int
    rfid_uid:    str
    user_name:   str
    access_type: str
    timestamp:   datetime
    note:        Optional[str]

    class Config:
        from_attributes = True


# ─── RFID Scan Response ──────────────────────────────────────────

class RFIDAccessResponse(BaseModel):
    access_granted: bool
    user_name:      str
    uid:            str


# ─── System Status ───────────────────────────────────────────────

class YoloDetection(BaseModel):
    class_name:  str = Field(alias="class")
    confidence:  float
    bbox:        List[int]
    area:        int

    class Config:
        populate_by_name = True

class YoloStatus(BaseModel):
    active:           bool
    stream_url:       Optional[str]
    fps:              float
    detection_count:  int
    objects_detected: List[dict]
    primary_object:   Optional[dict]
    object_count:     int
    person_detected:  bool

class SystemStatusResponse(BaseModel):
    esp_online:   bool
    door_open:    bool
    alarm_active: bool
    motion:       bool
    camera_angle: int
    camera_url:   str
    yolo:         Optional[dict]
    last_seen:    Optional[str]


# ─── Command Response ────────────────────────────────────────────

class CommandQueueResponse(BaseModel):
    ok:     bool
    queued: str


# ─── Generic Responses ───────────────────────────────────────────

class OkResponse(BaseModel):
    ok:      bool
    message: Optional[str] = None

class ErrorResponse(BaseModel):
    detail: str