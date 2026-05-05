"""
Access Log Routes
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from typing import Optional

from database import get_db, AccessLog
from routes.auth import get_current_user, require_admin

router = APIRouter(prefix="/api/logs", tags=["logs"])

@router.get("/")
def get_logs(
    limit:       int = Query(50, le=500),
    access_type: Optional[str] = None,
    user_name:   Optional[str] = None,
    db:          Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Get access logs.
    Admin: all logs
    User: only their own logs (by linked RFID)
    """
    query = db.query(AccessLog)
    
    # Non-admins can only see their own logs
    if current_user.role != "admin":
        if current_user.linked_rfid:
            query = query.filter(AccessLog.rfid_uid == current_user.linked_rfid)
        else:
            return []  # No linked RFID = no logs
    else:
        # Admin filters
        if access_type: query = query.filter(AccessLog.access_type == access_type)
        if user_name:   query = query.filter(AccessLog.user_name.contains(user_name))
    
    logs = query.order_by(AccessLog.timestamp.desc()).limit(limit).all()
    
    return [
        {
            "id":          l.id,
            "rfid_uid":    l.rfid_uid,
            "user_name":   l.user_name,
            "access_type": l.access_type,
            "timestamp":   l.timestamp.isoformat(),
            "note":        l.note
        }
        for l in logs
    ]

@router.delete("/")
def clear_logs(
    db:     Session = Depends(get_db),
    _admin = Depends(require_admin)
):
    """Admin: delete all logs"""
    count = db.query(AccessLog).count()
    db.query(AccessLog).delete()
    db.commit()
    return {"ok": True, "deleted": count}