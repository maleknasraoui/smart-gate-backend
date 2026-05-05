"""
Admin Control Routes
Remote door, alarm, camera control
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from datetime import datetime

from database import get_db, PendingCommand, AccessLog, SystemState
from routes.auth import require_admin, get_current_user
from routes.hardware import broadcast

router = APIRouter(prefix="/api/control", tags=["control"])

def queue_command(db: Session, command: str, value: str):
    """Add a command to the queue for ESP32 to pick up"""
    # Remove any existing unsent command of the same type
    db.query(PendingCommand).filter(
        PendingCommand.command == command,
        PendingCommand.sent == False
    ).delete()
    
    cmd = PendingCommand(command=command, value=value)
    db.add(cmd)
    db.commit()
    print(f"[CTRL] Queued command: {command}={value}")

@router.post("/door")
async def control_door(
    data: dict,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Open or close the door remotely"""
    action = data.get("action", "").lower()  # "open" or "close"
    
    if action not in ["open", "close"]:
        raise HTTPException(status_code=400, detail="Action must be 'open' or 'close'")
    
    queue_command(db, "door", action)
    
    # Log manual override
    log = AccessLog(
        rfid_uid    = "MANUAL",
        user_name   = current_user.username,
        access_type = "granted" if action == "open" else "closed",
        note        = f"Manual override by {current_user.username}"
    )
    db.add(log)
    db.commit()
    
    await broadcast("door_control", {"action": action, "by": current_user.username})
    
    return {"ok": True, "queued": f"door={action}"}

@router.post("/alarm")
async def control_alarm(
    data: dict,
    db: Session = Depends(get_db),
    _admin = Depends(require_admin)
):
    """Enable or disable the alarm"""
    action = data.get("action", "").lower()  # "on" or "off"
    
    if action not in ["on", "off"]:
        raise HTTPException(status_code=400, detail="Action must be 'on' or 'off'")
    
    queue_command(db, "alarm", action)
    
    await broadcast("alarm_control", {"action": action})
    return {"ok": True, "queued": f"alarm={action}"}

@router.post("/camera")
async def control_camera(
    data: dict,
    db: Session = Depends(get_db),
    _admin = Depends(require_admin)
):
    """Move camera to a specific angle (0-180)"""
    angle = data.get("angle", 90)
    
    try:
        angle = int(angle)
        angle = max(0, min(180, angle))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid angle")
    
    queue_command(db, "camera_angle", str(angle))
    
    await broadcast("camera_move", {"angle": angle})
    return {"ok": True, "queued": f"camera_angle={angle}"}