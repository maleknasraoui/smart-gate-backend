"""
Hardware API Routes
ESP32 communicates with these endpoints
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from datetime import datetime
from typing import Optional
import asyncio

from database import get_db, AccessLog, SystemState, User, PendingCommand
from ai.detector import detector

router = APIRouter(prefix="/api", tags=["hardware"])

# WebSocket manager (imported from main)
# We'll use a simple pub/sub pattern
_ws_callbacks = []

def register_ws_callback(cb):
    _ws_callbacks.append(cb)

async def broadcast(event: str, data: dict):
    """Send event to all connected WebSocket clients"""
    message = {"event": event, "data": data, "timestamp": datetime.utcnow().isoformat()}
    dead = []
    for cb in _ws_callbacks:
        try:
            await cb(message)
        except Exception:
            dead.append(cb)
    for d in dead:
        _ws_callbacks.remove(d)

# ─── Request Models ─────────────────────────────────

class MotionPayload(BaseModel):
    motion: bool
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

# ─── Motion Endpoint ────────────────────────────────

@router.post("/motion")
async def motion_detected(payload: MotionPayload, db: Session = Depends(get_db)):
    """
    Called by ESP32 when PIR detects motion.
    Triggers YOLO if not already running.
    """
    print(f"[API] Motion: {payload.motion}")
    
    # Update system state
    state = db.query(SystemState).filter(SystemState.id == 1).first()
    if state:
        state.motion    = payload.motion
        state.last_seen = datetime.utcnow()
        db.commit()
    
    # Broadcast to dashboard via WebSocket
    await broadcast("motion", {
        "detected": payload.motion,
        "yolo_active": detector.running
    })
    
    return {
        "received":    True,
        "yolo_active": detector.running
    }

# ─── RFID Endpoint ──────────────────────────────────

@router.post("/rfid")
async def rfid_scanned(payload: RFIDPayload, db: Session = Depends(get_db)):
    """
    Called by ESP32 when an RFID card is scanned.
    Returns whether access is granted.
    ESP32 then opens door or triggers alarm.
    """
    uid = payload.uid.upper().strip()
    print(f"[API] RFID scan: {uid}")
    
    # Check if user exists and is active
    user = db.query(User).filter(
        User.rfid_uid == uid,
        User.active == True
    ).first()
    
    granted   = user is not None
    user_name = user.name if user else "Unknown"
    
    # Log the attempt
    log_entry = AccessLog(
        rfid_uid    = uid,
        user_name   = user_name,
        access_type = "granted" if granted else "denied",
        note        = "RFID scan"
    )
    db.add(log_entry)
    db.commit()
    
    print(f"[API] Access {'GRANTED' if granted else 'DENIED'} for {uid} ({user_name})")
    
    # Broadcast to dashboard
    await broadcast("rfid_scan", {
        "uid":            uid,
        "user_name":      user_name,
        "access_granted": granted
    })
    
    return {
        "access_granted": granted,
        "user_name":      user_name,
        "uid":            uid
    }

# ─── Heartbeat Endpoint ─────────────────────────────

@router.post("/heartbeat")
async def heartbeat(payload: HeartbeatPayload, db: Session = Depends(get_db)):
    """
    ESP32 sends this every 10 seconds with current state.
    Updates database and broadcasts to dashboard.
    """
    state = db.query(SystemState).filter(SystemState.id == 1).first()
    if state:
        state.door_open    = payload.door_open
        state.alarm_active = payload.alarm_active
        state.motion       = payload.motion
        state.camera_angle = payload.camera_angle
        state.last_seen    = datetime.utcnow()
        db.commit()
    
    # Broadcast current state to all dashboard clients
    await broadcast("status_update", {
        "door_open":    payload.door_open,
        "alarm_active": payload.alarm_active,
        "motion":       payload.motion,
        "camera_angle": payload.camera_angle,
        "wifi_rssi":    payload.wifi_rssi
    })
    
    return {"ok": True}

# ─── Commands Endpoint ──────────────────────────────

@router.get("/commands")
async def get_commands(db: Session = Depends(get_db)):
    """
    ESP32 polls this every 500ms to get pending commands.
    Returns commands and marks them as sent.
    """
    # Get all unsent commands
    commands = db.query(PendingCommand).filter(
        PendingCommand.sent == False
    ).all()
    
    result = {}
    for cmd in commands:
        result[cmd.command] = cmd.value
        cmd.sent = True
    
    if commands:
        db.commit()
        print(f"[API] Sending commands to ESP: {result}")
    
    return result

# ─── Camera Register ────────────────────────────────

@router.post("/camera/register")
async def camera_register(payload: CameraRegisterPayload, db: Session = Depends(get_db)):
    """
    Called by ESP-CAM when it boots.
    Saves stream URL and starts YOLO.
    """
    print(f"[API] Camera registered: {payload.stream_url}")
    
    state = db.query(SystemState).filter(SystemState.id == 1).first()
    if state:
        state.camera_url = payload.stream_url
        db.commit()
    
    # Start YOLO with new stream URL
    detector.set_stream_url(payload.stream_url)
    
    await broadcast("camera_connected", {
        "stream_url":   payload.stream_url,
        "snapshot_url": payload.snapshot_url
    })
    
    return {"ok": True, "message": "Camera registered, YOLO starting"}

# ─── Hardware Status ────────────────────────────────

@router.get("/status")
async def get_status(db: Session = Depends(get_db)):
    """Full system status for dashboard"""
    state = db.query(SystemState).filter(SystemState.id == 1).first()
    
    # Check if ESP32 is alive (seen in last 30 seconds)
    esp_online = False
    if state and state.last_seen:
        diff = (datetime.utcnow() - state.last_seen).total_seconds()
        esp_online = diff < 30
    
    return {
        "esp_online":     esp_online,
        "door_open":      state.door_open    if state else False,
        "alarm_active":   state.alarm_active if state else False,
        "motion":         state.motion       if state else False,
        "camera_angle":   state.camera_angle if state else 90,
        "camera_url":     state.camera_url   if state else "",
        "yolo":           detector.get_status(),
        "last_seen":      state.last_seen.isoformat() if state and state.last_seen else None
    }