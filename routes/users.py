"""
User Management Routes (Admin only)
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
import bcrypt

from database import get_db, User, WebUser
from routes.auth import require_admin, get_current_user, hash_password

router = APIRouter(prefix="/api/users", tags=["users"])


# ─── Request Models ───────────────────────────────────────────────────

class CreateUserRequest(BaseModel):
    name:     str
    rfid_uid: str
    role:     Optional[str] = "user"

class UpdateUserRequest(BaseModel):
    name:     Optional[str]  = None
    active:   Optional[bool] = None
    role:     Optional[str]  = None
    rfid_uid: Optional[str]  = None

class CreateWebUserRequest(BaseModel):
    username:    str
    password:    str
    role:        Optional[str] = "user"
    linked_rfid: Optional[str] = None


# ─── RFID Users ───────────────────────────────────────────────────────

@router.get("/")
def list_users(
    db:     Session = Depends(get_db),
    _admin          = Depends(require_admin)
):
    """List all RFID users"""
    users = db.query(User).all()
    return [
        {
            "id":         u.id,
            "name":       u.name,
            "rfid_uid":   u.rfid_uid,
            "role":       u.role,
            "active":     u.active,
            "created_at": u.created_at.isoformat() if u.created_at else None
        }
        for u in users
    ]


@router.post("/")
def create_user(
    request: CreateUserRequest,
    db:      Session = Depends(get_db),
    _admin           = Depends(require_admin)
):
    """Register a new RFID user"""
    uid      = request.rfid_uid.upper().strip()
    existing = db.query(User).filter(User.rfid_uid == uid).first()

    if existing:
        raise HTTPException(
            status_code = 400,
            detail      = f"RFID UID '{uid}' is already registered"
        )

    user = User(name=request.name, rfid_uid=uid, role=request.role)
    db.add(user)
    db.commit()
    db.refresh(user)

    return {
        "ok":      True,
        "message": f"User '{request.name}' added successfully",
        "user_id": user.id
    }


@router.put("/{user_id}")
def update_user(
    user_id: int,
    request: UpdateUserRequest,
    db:      Session = Depends(get_db),
    _admin           = Depends(require_admin)
):
    """Update an existing RFID user"""
    user = db.query(User).filter(User.id == user_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if request.name     is not None: user.name     = request.name
    if request.active   is not None: user.active   = request.active
    if request.role     is not None: user.role     = request.role
    if request.rfid_uid is not None: user.rfid_uid = request.rfid_uid.upper()

    db.commit()
    return {"ok": True, "message": "User updated"}


@router.delete("/{user_id}")
def delete_user(
    user_id: int,
    db:      Session = Depends(get_db),
    _admin           = Depends(require_admin)
):
    """Delete an RFID user"""
    user = db.query(User).filter(User.id == user_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    name = user.name
    db.delete(user)
    db.commit()

    return {"ok": True, "message": f"User '{name}' deleted"}


@router.get("/check-rfid/{uid}")
def check_rfid(
    uid: str,
    db:  Session = Depends(get_db),
    _:   WebUser = Depends(get_current_user)
):
    """Check whether an RFID UID is registered (any logged-in user can call this)"""
    user = db.query(User).filter(
        User.rfid_uid == uid.upper()
    ).first()

    if not user:
        return {"found": False}

    return {
        "found":  True,
        "name":   user.name,
        "active": user.active,
        "role":   user.role
    }


# ─── Web (Dashboard) Users ────────────────────────────────────────────

@router.get("/web-users")
def list_web_users(
    db:     Session = Depends(get_db),
    _admin          = Depends(require_admin)
):
    """List all dashboard users"""
    users = db.query(WebUser).all()
    return [
        {
            "id":          u.id,
            "username":    u.username,
            "role":        u.role,
            "linked_rfid": u.linked_rfid
        }
        for u in users
    ]


@router.post("/web-users")
def create_web_user(
    request: CreateWebUserRequest,
    db:      Session = Depends(get_db),
    _admin           = Depends(require_admin)
):
    """Create a new dashboard user"""
    existing = db.query(WebUser).filter(
        WebUser.username == request.username
    ).first()

    if existing:
        raise HTTPException(
            status_code = 400,
            detail      = f"Username '{request.username}' already exists"
        )

    web_user = WebUser(
        username    = request.username,
        password    = hash_password(request.password),
        role        = request.role,
        linked_rfid = request.linked_rfid
    )
    db.add(web_user)
    db.commit()

    return {"ok": True, "message": f"Web user '{request.username}' created"}


@router.delete("/web-users/{user_id}")
def delete_web_user(
    user_id: int,
    db:      Session = Depends(get_db),
    _admin           = Depends(require_admin)
):
    """Delete a dashboard user"""
    user = db.query(WebUser).filter(WebUser.id == user_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="Web user not found")

    if user.username == "admin":
        raise HTTPException(
            status_code = 400,
            detail      = "Cannot delete the main admin account"
        )

    db.delete(user)
    db.commit()

    return {"ok": True, "message": f"Web user '{user.username}' deleted"}