"""
Authentication Routes
Login, logout, token management
Uses bcrypt directly (no passlib dependency)
"""
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from pydantic import BaseModel
from jose import JWTError, jwt
from datetime import datetime, timedelta
from typing import Optional
import bcrypt

from database import get_db, WebUser

router = APIRouter(prefix="/api/auth", tags=["auth"])

# ─── Config ─────────────────────────────────────────────────────────
SECRET_KEY          = "smartgate-secret-key-change-in-production"
ALGORITHM           = "HS256"
TOKEN_EXPIRE_HOURS  = 24

security = HTTPBearer()

# ─── Password Helpers ────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    """Hash a password using bcrypt. Returns a string."""
    password_bytes = plain.encode("utf-8")
    salt           = bcrypt.gensalt(rounds=12)
    hashed         = bcrypt.hashpw(password_bytes, salt)
    return hashed.decode("utf-8")   # Store as string in DB


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plain password against a bcrypt hash."""
    try:
        return bcrypt.checkpw(
            plain.encode("utf-8"),
            hashed.encode("utf-8")
        )
    except Exception:
        return False


# ─── JWT Helpers ─────────────────────────────────────────────────────

def create_token(data: dict) -> str:
    """Create a signed JWT token."""
    to_encode        = data.copy()
    expire           = datetime.utcnow() + timedelta(hours=TOKEN_EXPIRE_HOURS)
    to_encode["exp"] = expire
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    """Decode and validate a JWT token."""
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(
            status_code = status.HTTP_401_UNAUTHORIZED,
            detail      = "Invalid or expired token"
        )


# ─── Dependencies ────────────────────────────────────────────────────

def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
) -> WebUser:
    """FastAPI dependency — validates JWT and returns current WebUser."""
    payload  = decode_token(credentials.credentials)
    username = payload.get("sub")

    if not username:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    user = db.query(WebUser).filter(WebUser.username == username).first()

    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    return user


def require_admin(
    current_user: WebUser = Depends(get_current_user)
) -> WebUser:
    """FastAPI dependency — ensures the current user is admin."""
    if current_user.role != "admin":
        raise HTTPException(
            status_code = 403,
            detail      = "Admin access required"
        )
    return current_user


# ─── Request / Response Models ───────────────────────────────────────

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


# ─── Routes ──────────────────────────────────────────────────────────

@router.post("/login", response_model=TokenResponse)
def login(request: LoginRequest, db: Session = Depends(get_db)):
    """Authenticate and return a JWT token."""
    user = db.query(WebUser).filter(
        WebUser.username == request.username
    ).first()

    if not user or not verify_password(request.password, user.password):
        raise HTTPException(
            status_code = status.HTTP_401_UNAUTHORIZED,
            detail      = "Invalid username or password"
        )

    token = create_token({"sub": user.username, "role": user.role})

    return TokenResponse(
        token    = token,
        role     = user.role,
        username = user.username
    )


@router.get("/me")
def get_me(current_user: WebUser = Depends(get_current_user)):
    """Return current logged-in user info."""
    return {
        "username":    current_user.username,
        "role":        current_user.role,
        "linked_rfid": current_user.linked_rfid
    }


@router.post("/change-password")
def change_password(
    data:         ChangePasswordRequest,
    current_user: WebUser  = Depends(get_current_user),
    db:           Session  = Depends(get_db)
):
    """Change the current user's password."""
    if not verify_password(data.old_password, current_user.password):
        raise HTTPException(
            status_code = 400,
            detail      = "Current password is incorrect"
        )

    if len(data.new_password) < 4:
        raise HTTPException(
            status_code = 400,
            detail      = "New password must be at least 4 characters"
        )

    current_user.password = hash_password(data.new_password)
    db.commit()

    return {"ok": True, "message": "Password changed successfully"}