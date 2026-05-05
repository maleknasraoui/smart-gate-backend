"""
Database setup using SQLAlchemy + SQLite
FIXED: Sample user UID stored without colons to match ESP32 format
"""
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import bcrypt

DATABASE_URL = "sqlite:///./gate.db"
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ═══════════════════════════════════════════════════════════════════════
# MODELS
# ═══════════════════════════════════════════════════════════════════════

class User(Base):
    __tablename__ = "users"

    id         = Column(Integer, primary_key=True, index=True)
    name       = Column(String(100), nullable=False)
    # FIX: Always stored WITHOUT colons e.g. "AABBCCDD" not "AA:BB:CC:DD"
    rfid_uid   = Column(String(50), unique=True, nullable=False)
    role       = Column(String(20), default="user")
    active     = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class WebUser(Base):
    __tablename__ = "web_users"

    id          = Column(Integer, primary_key=True, index=True)
    username    = Column(String(50), unique=True, nullable=False)
    password    = Column(String(200), nullable=False)
    role        = Column(String(20), default="user")
    linked_rfid = Column(String(50), nullable=True)


class AccessLog(Base):
    __tablename__ = "access_logs"

    id          = Column(Integer, primary_key=True, index=True)
    rfid_uid    = Column(String(50))
    user_name   = Column(String(100), default="Unknown")
    access_type = Column(String(20))
    timestamp   = Column(DateTime, default=datetime.utcnow)
    note        = Column(Text, default="")


class SystemState(Base):
    __tablename__ = "system_state"

    id           = Column(Integer, primary_key=True, default=1)
    door_open    = Column(Boolean, default=False)
    alarm_active = Column(Boolean, default=False)
    motion       = Column(Boolean, default=False)
    camera_angle = Column(Integer, default=90)
    camera_url   = Column(String(200), default="")
    last_seen    = Column(DateTime, nullable=True)


class PendingCommand(Base):
    __tablename__ = "pending_commands"

    id         = Column(Integer, primary_key=True)
    command    = Column(String(50))
    value      = Column(String(50))
    created_at = Column(DateTime, default=datetime.utcnow)
    sent       = Column(Boolean, default=False)


# ═══════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════

def normalize_uid(uid: str) -> str:
    """
    Always store and compare UIDs WITHOUT colons.
    'AA:BB:CC:DD' → 'AABBCCDD'
    'aa bb cc dd' → 'AABBCCDD'
    """
    return uid.upper().replace(":", "").replace(" ", "").strip()


# ═══════════════════════════════════════════════════════════════════════
# INIT
# ═══════════════════════════════════════════════════════════════════════

def init_db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    try:
        # System state row
        state = db.query(SystemState).filter(SystemState.id == 1).first()
        if not state:
            db.add(SystemState(id=1))

        # Default admin + user web accounts
        admin = db.query(WebUser).filter(
            WebUser.username == "admin"
        ).first()
        if not admin:
            db.add(WebUser(
                username = "admin",
                password = bcrypt.hashpw(
                    b"admin123", bcrypt.gensalt()
                ).decode(),
                role     = "admin"
            ))
            db.add(WebUser(
                username = "user",
                password = bcrypt.hashpw(
                    b"user123", bcrypt.gensalt()
                ).decode(),
                role     = "user"
            ))

        # FIX: Sample RFID user stored WITHOUT colons
        # Old code stored "AA:BB:CC:DD" — that never matched ESP32 scans
        sample_uid = "AABBCCDD"   # ← no colons
        sample = db.query(User).filter(
            User.rfid_uid == sample_uid
        ).first()

        # Also clean up old colon-format entry if it exists
        old_sample = db.query(User).filter(
            User.rfid_uid == "AA:BB:CC:DD"
        ).first()
        if old_sample:
            old_sample.rfid_uid = sample_uid
            print("[DB] Migrated sample user UID: AA:BB:CC:DD → AABBCCDD")

        if not sample and not old_sample:
            db.add(User(
                name     = "Test User",
                rfid_uid = sample_uid,
                role     = "user"
            ))

        db.commit()
        print("[DB] ✓ Database initialized")
        print("[DB] Admin login : admin / admin123")
        print("[DB] User login  : user  / user123")
        print("[DB] Test RFID   : AABBCCDD  (no colons)")

    except Exception as e:
        print(f"[DB] Init error: {e}")
        db.rollback()
    finally:
        db.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()