"""
Smart Gate System — PC Backend
FIXED:
  - 30fps camera stream
  - YOLO processes every 2nd frame
  - Servo tracker tuned for 30fps
  - PIR 60s alarm window
  - RFID toggles door
"""
import cv2
import threading
import time
import random
import asyncio
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse

from database import (
    init_db, SessionLocal, normalize_uid,
    SystemState, User, AccessLog, PendingCommand, WebUser
)


# ═══════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════

def queue_command(command: str, value: str):
    db = SessionLocal()
    try:
        db.query(PendingCommand).filter(
            PendingCommand.command == command,
            PendingCommand.sent    == False
        ).delete()
        db.add(PendingCommand(command=command, value=value))
        db.commit()
        print(f"[CMD] Queued → {command} = {value}")
    finally:
        db.close()


def _is_esp_online(state) -> bool:
    if not state or not state.last_seen:
        return False
    return (datetime.utcnow() - state.last_seen).total_seconds() < 30


def _decode_token(authorization: str) -> dict:
    try:
        from jose import jwt
        token   = authorization.replace("Bearer ", "").strip()
        payload = jwt.decode(token, "secret", algorithms=["HS256"])
        return {
            "username": payload.get("sub"),
            "role":     payload.get("role", "user")
        }
    except Exception:
        return {"username": None, "role": "user"}


# ═══════════════════════════════════════════════════════════════════════
# PIR ALARM MANAGER
# ═══════════════════════════════════════════════════════════════════════

class PIRAlarmManager:
    """
    Motion detected → 60 second RFID window.
    Valid RFID in time → cancel.
    No RFID in time  → trigger alarm.
    """

    WINDOW_SECONDS = 60

    def __init__(self):
        self.motion_active   = False
        self.window_start    = None
        self.window_open     = False
        self.alarm_triggered = False
        self._task           = None
        self._loop           = None

    def set_loop(self, loop):
        self._loop = loop

    def on_motion_detected(self, detected: bool):
        self.motion_active = detected

        if detected and not self.window_open and not self.alarm_triggered:
            self.window_open  = True
            self.window_start = time.time()
            print(f"[PIR] ⚠ Motion — {self.WINDOW_SECONDS}s window started")

            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._countdown(), self._loop
                )

    def on_valid_rfid(self):
        if self.window_open:
            print("[PIR] ✓ Valid RFID — alarm window cancelled")
            self.window_open     = False
            self.window_start    = None
            self.alarm_triggered = False
            return True
        return False

    def reset_alarm(self):
        self.window_open     = False
        self.window_start    = None
        self.alarm_triggered = False
        self.motion_active   = False
        print("[PIR] State reset by admin")

    def get_status(self) -> dict:
        remaining = 0
        if self.window_open and self.window_start:
            elapsed   = time.time() - self.window_start
            remaining = max(0, int(self.WINDOW_SECONDS - elapsed))
        return {
            "motion_active":     self.motion_active,
            "window_open":       self.window_open,
            "seconds_remaining": remaining,
            "alarm_triggered":   self.alarm_triggered
        }

    async def _countdown(self):
        await asyncio.sleep(self.WINDOW_SECONDS)

        if self.window_open:
            self.alarm_triggered = True
            self.window_open     = False
            print("[PIR] ⏰ 60s elapsed — ALARM TRIGGERED")

            queue_command("alarm", "on")

            await broadcast({
                "event": "pir_alarm",
                "data": {
                    "reason": "No valid RFID within 60 seconds",
                    "alarm":  True
                },
                "timestamp": datetime.utcnow().isoformat()
            })

            db    = SessionLocal()
            state = db.query(SystemState).filter(
                SystemState.id == 1
            ).first()
            if state:
                state.alarm_active = True
                db.commit()
            db.close()


pir_manager = PIRAlarmManager()


# ═══════════════════════════════════════════════════════════════════════
# WEBCAM — 30fps
# ═══════════════════════════════════════════════════════════════════════

class PCWebcam:

    def __init__(self):
        self.cap          = None
        self.running      = False
        self.last_frame   = None
        self.lock         = threading.Lock()
        self.camera_index = 0
        self.thread       = None

    def start(self, camera_index=0):
        self.camera_index = camera_index

        cap = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap = cv2.VideoCapture(camera_index)

        if not cap.isOpened():
            print(f"[WEBCAM] Cannot open camera {camera_index} → test mode")
            self.running = True
            self.thread  = threading.Thread(
                target=self._test_mode_loop, daemon=True
            )
            self.thread.start()
            return False

        self.cap     = cap
        self.running = True

        # FIX: Set 30fps
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        self.cap.set(cv2.CAP_PROP_FPS,          30)

        actual_fps = self.cap.get(cv2.CAP_PROP_FPS)
        w          = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h          = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        print(f"[WEBCAM] Camera {camera_index} → {w}x{h} @ {actual_fps}fps")

        self.thread = threading.Thread(
            target=self._capture_loop, daemon=True
        )
        self.thread.start()
        return True

    def stop(self):
        print(f"[WEBCAM] Stopping camera {self.camera_index}...")
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)
        if self.cap:
            self.cap.release()
            self.cap = None
        self.last_frame = None
        print("[WEBCAM] Stopped")

    def switch(self, camera_index: int) -> bool:
        self.stop()
        time.sleep(0.5)
        return self.start(camera_index)

    def _capture_loop(self):
        # FIX: 30fps capture loop
        frame_interval = 1.0 / 30
        while self.running:
            if not self.cap or not self.cap.isOpened():
                break

            t0  = time.time()
            ret, frame = self.cap.read()

            if not ret:
                time.sleep(0.05)
                continue

            ts = datetime.now().strftime("%H:%M:%S")
            cv2.putText(
                frame,
                f"SmartGate CAM {self.camera_index} | {ts}",
                (10, frame.shape[0] - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1
            )

            _, buf = cv2.imencode(
                ".jpg", frame,
                [cv2.IMWRITE_JPEG_QUALITY, 80]
            )
            with self.lock:
                self.last_frame = buf.tobytes()

            # Sleep only the remaining time to hit 30fps
            elapsed = time.time() - t0
            sleep_t = frame_interval - elapsed
            if sleep_t > 0:
                time.sleep(sleep_t)

    def _test_mode_loop(self):
        import numpy as np
        print("[WEBCAM] Test mode — placeholder frames")
        while self.running:
            frame = np.zeros((480, 640, 3), dtype=np.uint8)
            ts    = datetime.now().strftime("%H:%M:%S")
            cv2.putText(
                frame, "NO CAMERA", (200, 220),
                cv2.FONT_HERSHEY_SIMPLEX, 1.2, (80, 80, 80), 2
            )
            cv2.putText(
                frame, ts, (260, 270),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (60, 60, 60), 1
            )
            _, buf = cv2.imencode(".jpg", frame)
            with self.lock:
                self.last_frame = buf.tobytes()
            time.sleep(1 / 30)

    def get_frame(self):
        with self.lock:
            return self.last_frame


# ═══════════════════════════════════════════════════════════════════════
# SERVO TRACKER — tuned for 30fps
# ═══════════════════════════════════════════════════════════════════════

class ServoTracker:
    """
    Tuned for 30fps:
    - Dead zone: 60px
    - Sensitivity: 0.035 deg/px
    - Cooldown: 0.5s
    - Max step: 12 degrees
    - Stops moving after 3 consecutive centered frames
    """

    FRAME_CENTER    = 320
    SERVO_MIN       = 10
    SERVO_MAX       = 170
    DEAD_ZONE_PX    = 60
    SENSITIVITY     = 0.035
    MOVE_COOLDOWN   = 0.5
    MAX_STEP_DEG    = 12

    def __init__(self):
        self.current_angle   = 90
        self.enabled         = True
        self._last_move      = 0
        self._centered_count = 0

    def update(self, primary_object: dict) -> bool:
        if not self.enabled or not primary_object:
            return False

        bbox = primary_object.get("bbox")
        if not bbox or len(bbox) < 4:
            return False

        now = time.time()
        if now - self._last_move < self.MOVE_COOLDOWN:
            return False

        x1, _, x2, _ = bbox
        obj_center_x  = (x1 + x2) / 2
        offset        = obj_center_x - self.FRAME_CENTER

        # Dead zone
        if abs(offset) < self.DEAD_ZONE_PX:
            self._centered_count += 1
            # Stop after 3 centered frames in a row
            if self._centered_count >= 3:
                return False
        else:
            self._centered_count = 0

        # Cap the step
        raw_adj    = -offset * self.SENSITIVITY
        adjustment = max(
            -self.MAX_STEP_DEG,
            min(self.MAX_STEP_DEG, raw_adj)
        )

        new_angle = int(
            max(self.SERVO_MIN,
                min(self.SERVO_MAX,
                    self.current_angle + adjustment))
        )

        if new_angle == self.current_angle:
            return False

        self.current_angle = new_angle
        self._last_move    = now

        queue_command("camera_angle", str(new_angle))
        print(
            f"[TRACKER] offset={offset:+.0f}px "
            f"adj={adjustment:+.1f}° → {new_angle}°"
        )
        return True


# ═══════════════════════════════════════════════════════════════════════
# YOLO DETECTOR — 30fps optimized
# ═══════════════════════════════════════════════════════════════════════

class GateDetector:

    TARGET_CLASSES = [
        "person", "car", "truck", "motorcycle", "bicycle"
    ]

    def __init__(self):
        self.model             = None
        self.model_loaded      = False
        self.model_error       = None
        self.stream_url        = None
        self.running           = False
        self.thread            = None
        self.latest_detections = []
        self.primary_object    = None
        self.last_frame        = None
        self.detection_count   = 0
        self.fps               = 0.0
        self.tracker           = ServoTracker()

    def _load_model(self) -> bool:
        try:
            print("[YOLO] Loading YOLOv8n...")
            from ultralytics import YOLO
            self.model        = YOLO("yolov8n.pt")
            self.model_loaded = True
            self.model_error  = None
            print("[YOLO] ✓ Model loaded")
            return True
        except Exception as e:
            self.model_error  = str(e)
            self.model_loaded = False
            print(f"[YOLO] ✗ {e} → test mode")
            return False

    def set_stream_url(self, url: str):
        self.stream_url = url
        print(f"[YOLO] Stream: {url}")

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread  = threading.Thread(
            target=self._detection_loop,
            daemon=True,
            name="YOLOThread"
        )
        self.thread.start()
        print("[YOLO] Detection thread started")

    def stop(self):
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)
        print("[YOLO] Stopped")

    def _detection_loop(self):
        model_ok = self._load_model()

        if not self.stream_url:
            self._test_mode()
            return

        time.sleep(2)

        cap = cv2.VideoCapture(self.stream_url)
        if not cap.isOpened():
            print("[YOLO] Cannot open stream → test mode")
            self._test_mode()
            return

        # FIX: Set stream to 30fps
        cap.set(cv2.CAP_PROP_FPS, 30)

        print("[YOLO] Stream open — 30fps detection running")

        fps_timer   = time.time()
        frame_count = 0

        while self.running:
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.5)
                cap = cv2.VideoCapture(self.stream_url)
                cap.set(cv2.CAP_PROP_FPS, 30)
                continue

            frame_count += 1

            # FIX: Process every 2nd frame (was 3rd)
            # At 30fps this gives ~15 detection updates/sec
            if frame_count % 2 == 0:
                detections = (
                    self._process_frame(frame) if model_ok else []
                )

                self.latest_detections = detections
                self._select_primary(detections)
                self.detection_count  += len(detections)

                if self.primary_object:
                    self.tracker.update(self.primary_object)

                elapsed = time.time() - fps_timer
                if elapsed >= 1.0:
                    self.fps      = round(frame_count / elapsed, 1)
                    fps_timer     = time.time()
                    frame_count   = 0

                self._save_annotated(frame, detections)

            # FIX: Tighter sleep for 30fps
            time.sleep(1 / 60)

        cap.release()

    def _test_mode(self):
        print("[YOLO] TEST MODE — simulated detections")
        while self.running:
            time.sleep(3)
            if not self.running:
                break
            if random.random() > 0.4:
                x1 = random.randint(30, 420)
                x2 = x1 + random.randint(100, 200)
                self.latest_detections = [{
                    "class":      "person",
                    "confidence": round(random.uniform(0.72, 0.97), 2),
                    "bbox":       [x1, 40, x2, 460],
                    "area":       (x2 - x1) * 420
                }]
            else:
                self.latest_detections = []

            self._select_primary(self.latest_detections)

            if self.primary_object:
                self.tracker.update(self.primary_object)

    def _process_frame(self, frame) -> list:
        try:
            results = self.model(
                frame, verbose=False, conf=0.5, iou=0.45
            )
        except Exception as e:
            print(f"[YOLO] Inference error: {e}")
            return []

        detections = []
        for result in results:
            if result.boxes is None:
                continue
            for box in result.boxes:
                cid             = int(box.cls[0])
                name            = self.model.names[cid]
                if name not in self.TARGET_CLASSES:
                    continue
                conf            = float(box.conf[0])
                x1, y1, x2, y2 = [int(v) for v in box.xyxy[0]]
                detections.append({
                    "class":      name,
                    "confidence": round(conf, 2),
                    "bbox":       [x1, y1, x2, y2],
                    "area":       (x2 - x1) * (y2 - y1)
                })
        return detections

    def _select_primary(self, detections: list):
        if not detections:
            self.primary_object = None
            return
        persons = [d for d in detections if d["class"] == "person"]
        pool    = persons if persons else detections
        self.primary_object = (
            max(pool, key=lambda d: d["area"]) if pool else None
        )

    def _save_annotated(self, frame, detections: list):
        out = frame.copy()

        for d in detections:
            x1, y1, x2, y2 = d["bbox"]
            color = (
                (0, 255, 0)
                if d["class"] == "person"
                else (0, 165, 255)
            )
            cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
            cv2.putText(
                out,
                f"{d['class']} {d['confidence']:.0%}",
                (x1, max(y1 - 10, 10)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2
            )

        if self.primary_object:
            x1, y1, x2, y2 = self.primary_object["bbox"]
            cx = (x1 + x2) // 2
            cv2.rectangle(out, (x1, y1), (x2, y2), (255, 80, 0), 3)
            cv2.putText(
                out, "PRIMARY", (x1, y2 + 20),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 80, 0), 2
            )
            cv2.line(
                out, (cx, 0), (cx, out.shape[0]), (255, 80, 0), 1
            )

        # Frame center reference line
        cv2.line(
            out, (320, 0), (320, out.shape[0]),
            (200, 200, 200), 1
        )

        # HUD
        trk = self.tracker
        cv2.putText(
            out,
            f"Servo:{trk.current_angle}° "
            f"Track:{'ON' if trk.enabled else 'OFF'} "
            f"FPS:{self.fps}",
            (8, 22),
            cv2.FONT_HERSHEY_SIMPLEX, 0.48, (0, 255, 255), 1
        )
        cv2.putText(
            out,
            datetime.now().strftime("%H:%M:%S"),
            (8, out.shape[0] - 8),
            cv2.FONT_HERSHEY_SIMPLEX, 0.4, (160, 160, 160), 1
        )

        try:
            _, buf = cv2.imencode(
                ".jpg", out,
                [cv2.IMWRITE_JPEG_QUALITY, 75]
            )
            self.last_frame = buf.tobytes()
        except Exception:
            pass

    def get_status(self) -> dict:
        return {
            "active":           self.running,
            "model_loaded":     self.model_loaded,
            "model_error":      self.model_error,
            "stream_url":       self.stream_url,
            "fps":              round(self.fps, 1),
            "detection_count":  self.detection_count,
            "objects_detected": self.latest_detections,
            "primary_object":   self.primary_object,
            "object_count":     len(self.latest_detections),
            "person_detected":  any(
                d["class"] == "person"
                for d in self.latest_detections
            ),
            "tracking_enabled": self.tracker.enabled,
            "tracking_angle":   self.tracker.current_angle
        }


# ═══════════════════════════════════════════════════════════════════════
# GLOBALS
# ═══════════════════════════════════════════════════════════════════════

detector                        = GateDetector()
webcam                          = PCWebcam()
ws_connections: list[WebSocket] = []


async def broadcast(message: dict):
    dead = []
    for ws in ws_connections:
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for d in dead:
        if d in ws_connections:
            ws_connections.remove(d)


# ═══════════════════════════════════════════════════════════════════════
# LIFESPAN
# ═══════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("\n" + "=" * 55)
    print("   Smart Gate System — Backend  (30fps)")
    print("=" * 55)

    init_db()

    # Give PIR manager the event loop reference
    pir_manager.set_loop(asyncio.get_event_loop())

    CAMERA_INDEX = 0
    webcam.start(CAMERA_INDEX)

    detector.set_stream_url(
        "http://localhost:8000/api/camera/stream"
    )
    detector.start()

    async def _push_yolo():
        while True:
            await asyncio.sleep(2)
            if ws_connections:
                await broadcast({
                    "event":     "yolo_update",
                    "data":      detector.get_status(),
                    "timestamp": datetime.utcnow().isoformat()
                })

    async def _push_pir():
        """Push PIR countdown every second when window is open"""
        while True:
            await asyncio.sleep(1)
            status = pir_manager.get_status()
            if status["window_open"] and ws_connections:
                await broadcast({
                    "event":     "pir_countdown",
                    "data":      status,
                    "timestamp": datetime.utcnow().isoformat()
                })

    asyncio.create_task(_push_yolo())
    asyncio.create_task(_push_pir())

    print("[SERVER] Ready! http://localhost:8000")
    print("[SERVER] Docs:  http://localhost:8000/docs")
    print("[SERVER] Cam:   http://localhost:8000/api/camera/stream")
    print("=" * 55 + "\n")

    yield

    detector.stop()
    webcam.stop()


# ═══════════════════════════════════════════════════════════════════════
# APP
# ═══════════════════════════════════════════════════════════════════════

app = FastAPI(
    title="Smart Gate API",
    version="2.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)


# ═══════════════════════════════════════════════════════════════════════
# WEBSOCKET
# ═══════════════════════════════════════════════════════════════════════

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_connections.append(ws)
    print(f"[WS] +1 client → total {len(ws_connections)}")

    db    = SessionLocal()
    state = db.query(SystemState).filter(SystemState.id == 1).first()
    db.close()

    if state:
        await ws.send_json({
            "event": "initial_state",
            "data": {
                "door_open":    state.door_open,
                "alarm_active": state.alarm_active,
                "motion":       state.motion,
                "camera_angle": state.camera_angle,
                "camera_url":   "http://localhost:8000/api/camera/stream",
                "yolo":         detector.get_status(),
                "esp_online":   _is_esp_online(state),
                "pir_status":   pir_manager.get_status()
            },
            "timestamp": datetime.utcnow().isoformat()
        })

    try:
        while True:
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text("pong")
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        if ws in ws_connections:
            ws_connections.remove(ws)
        print(f"[WS] -1 client → total {len(ws_connections)}")


# ═══════════════════════════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════════════════════════

@app.post("/api/auth/login")
async def login(data: dict):
    from fastapi import HTTPException
    import bcrypt
    from jose import jwt

    db   = SessionLocal()
    user = db.query(WebUser).filter(
        WebUser.username == data.get("username", "").strip()
    ).first()
    db.close()

    if not user or not bcrypt.checkpw(
        data.get("password", "").encode(),
        user.password.encode()
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid username or password"
        )

    token = jwt.encode(
        {"sub": user.username, "role": user.role},
        "secret",
        algorithm="HS256"
    )
    return {
        "token":    token,
        "role":     user.role,
        "username": user.username
    }


@app.get("/api/auth/me")
async def get_me(authorization: Optional[str] = Header(None)):
    if not authorization:
        return {
            "username":    "unknown",
            "role":        "user",
            "linked_rfid": None
        }
    info = _decode_token(authorization)
    db   = SessionLocal()
    user = db.query(WebUser).filter(
        WebUser.username == info["username"]
    ).first()
    db.close()
    if not user:
        return {**info, "linked_rfid": None}
    return {
        "username":    user.username,
        "role":        user.role,
        "linked_rfid": user.linked_rfid
    }


@app.post("/api/auth/change-password")
async def change_password(
    data: dict,
    authorization: Optional[str] = Header(None)
):
    from fastapi import HTTPException
    import bcrypt

    if not authorization:
        raise HTTPException(status_code=401, detail="Not authenticated")

    info = _decode_token(authorization)
    db   = SessionLocal()
    user = db.query(WebUser).filter(
        WebUser.username == info["username"]
    ).first()

    if not user:
        db.close()
        raise HTTPException(status_code=404, detail="User not found")

    if not bcrypt.checkpw(
        data.get("old_password", "").encode(),
        user.password.encode()
    ):
        db.close()
        raise HTTPException(
            status_code=400,
            detail="Current password incorrect"
        )

    new_pw = data.get("new_password", "")
    if len(new_pw) < 4:
        db.close()
        raise HTTPException(
            status_code=400,
            detail="Password too short (min 4)"
        )

    user.password = bcrypt.hashpw(
        new_pw.encode(), bcrypt.gensalt()
    ).decode()
    db.commit()
    db.close()
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════════
# HARDWARE — ESP32 endpoints
# ═══════════════════════════════════════════════════════════════════════

@app.post("/api/motion")
async def motion_detected(data: dict):
    """
    PIR notification — starts 60s RFID window.
    Does NOT open gate directly.
    """
    detected = bool(data.get("motion", False))
    print(f"[PIR] {'⚠ DETECTED' if detected else 'clear'}")

    pir_manager.on_motion_detected(detected)

    db    = SessionLocal()
    state = db.query(SystemState).filter(SystemState.id == 1).first()
    if state:
        state.motion = detected
        db.commit()
    db.close()

    pir_status = pir_manager.get_status()

    await broadcast({
        "event": "motion",
        "data": {
            "detected":          detected,
            "window_open":       pir_status["window_open"],
            "seconds_remaining": pir_status["seconds_remaining"],
            "notification_only": True
        },
        "timestamp": datetime.utcnow().isoformat()
    })

    return {
        "received":          True,
        "window_open":       pir_status["window_open"],
        "seconds_remaining": pir_status["seconds_remaining"]
    }


@app.post("/api/rfid")
async def rfid_scanned(data: dict):
    """
    ESP32 gate RFID scan.
    Valid card → grant access, toggle door, cancel alarm window.
    """
    raw_uid    = data.get("uid", "").upper().strip()
    uid        = normalize_uid(raw_uid)
    uid_colons = ":".join(uid[i:i+2] for i in range(0, len(uid), 2))

    print(f"[RFID] raw='{raw_uid}' → normalized='{uid}'")

    db = SessionLocal()

    user = db.query(User).filter(
        User.rfid_uid == uid,
        User.active   == True
    ).first()

    if not user:
        user = db.query(User).filter(
            User.rfid_uid == uid_colons,
            User.active   == True
        ).first()
        if user:
            # Migrate to no-colon format
            user.rfid_uid = uid
            db.commit()

    granted   = user is not None
    user_name = user.name if user else "Unknown"

    # Valid RFID → cancel PIR alarm window
    alarm_cancelled = False
    if granted:
        alarm_cancelled = pir_manager.on_valid_rfid()
        if alarm_cancelled:
            queue_command("alarm", "off")
            print(f"[PIR] Alarm window cancelled by {user_name}")

    db.add(AccessLog(
        rfid_uid    = uid,
        user_name   = user_name,
        access_type = "granted" if granted else "denied",
        note        = "Gate RFID scan"
    ))
    db.commit()
    db.close()

    print(
        f"[RFID] {uid} → "
        f"{'GRANTED ✓' if granted else 'DENIED ✗'} "
        f"({user_name})"
    )

    await broadcast({
        "event": "rfid_scan",
        "data": {
            "uid":             uid,
            "user_name":       user_name,
            "access_granted":  granted,
            "alarm_cancelled": alarm_cancelled
        },
        "timestamp": datetime.utcnow().isoformat()
    })

    return {
        "access_granted": granted,
        "user_name":      user_name,
        "uid":            uid
    }


@app.post("/api/rfid/check")
async def rfid_check_only(data: dict):
    """Admin check — no gate action, no log"""
    raw_uid    = data.get("uid", "").upper().strip()
    uid        = normalize_uid(raw_uid)
    uid_colons = ":".join(uid[i:i+2] for i in range(0, len(uid), 2))

    db   = SessionLocal()
    user = db.query(User).filter(
        User.rfid_uid.in_([uid, uid_colons])
    ).first()
    db.close()

    return {
        "uid":        uid,
        "registered": user is not None,
        "user_name":  user.name   if user else None,
        "active":     user.active if user else None
    }


@app.post("/api/heartbeat")
async def heartbeat(data: dict):
    db    = SessionLocal()
    state = db.query(SystemState).filter(SystemState.id == 1).first()
    if state:
        state.door_open    = bool(data.get("door_open",    False))
        state.alarm_active = bool(data.get("alarm_active", False))
        state.motion       = bool(data.get("motion",       False))
        state.camera_angle = int(data.get("camera_angle",  90))
        state.last_seen    = datetime.utcnow()
        db.commit()
    db.close()

    await broadcast({
        "event": "status_update",
        "data": {
            "door_open":    data.get("door_open",    False),
            "alarm_active": data.get("alarm_active", False),
            "motion":       data.get("motion",       False),
            "camera_angle": data.get("camera_angle", 90),
            "esp_online":   True
        },
        "timestamp": datetime.utcnow().isoformat()
    })
    return {"ok": True}


@app.get("/api/commands")
async def get_commands():
    db   = SessionLocal()
    cmds = db.query(PendingCommand).filter(
        PendingCommand.sent == False
    ).all()

    result = {}
    for cmd in cmds:
        result[cmd.command] = cmd.value
        cmd.sent = True

    if cmds:
        db.commit()
    db.close()
    return result


# ═══════════════════════════════════════════════════════════════════════
# STATUS
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/status")
async def get_status():
    db    = SessionLocal()
    state = db.query(SystemState).filter(SystemState.id == 1).first()
    lc    = db.query(AccessLog).count()
    uc    = db.query(User).count()
    db.close()

    return {
        "esp_online":   _is_esp_online(state),
        "door_open":    state.door_open    if state else False,
        "alarm_active": state.alarm_active if state else False,
        "motion":       state.motion       if state else False,
        "camera_angle": state.camera_angle if state else 90,
        "camera_url":   "http://localhost:8000/api/camera/stream",
        "yolo":         detector.get_status(),
        "pir_status":   pir_manager.get_status(),
        "last_seen":    (
            state.last_seen.isoformat()
            if state and state.last_seen else None
        ),
        "logs_count":   lc,
        "users_count":  uc
    }


# ═══════════════════════════════════════════════════════════════════════
# CONTROL
# ═══════════════════════════════════════════════════════════════════════

@app.post("/api/control/door")
async def control_door(
    data: dict,
    authorization: Optional[str] = Header(None)
):
    from fastapi import HTTPException
    action = data.get("action", "close").lower()
    if action not in ["open", "close"]:
        raise HTTPException(
            status_code=400,
            detail="action must be 'open' or 'close'"
        )

    queue_command("door", action)

    who = (
        _decode_token(authorization)["username"]
        if authorization else "dashboard"
    )
    db = SessionLocal()
    db.add(AccessLog(
        rfid_uid    = "MANUAL",
        user_name   = who or "admin",
        access_type = "granted" if action == "open" else "closed",
        note        = f"Remote {action} by {who}"
    ))
    db.commit()
    db.close()

    await broadcast({
        "event":     "door_control",
        "data":      {"action": action},
        "timestamp": datetime.utcnow().isoformat()
    })
    return {"ok": True}


@app.post("/api/control/alarm")
async def control_alarm(data: dict):
    from fastapi import HTTPException
    action = data.get("action", "off").lower()
    if action not in ["on", "off"]:
        raise HTTPException(
            status_code=400,
            detail="action must be 'on' or 'off'"
        )

    queue_command("alarm", action)

    if action == "off":
        pir_manager.reset_alarm()

    await broadcast({
        "event":     "alarm_control",
        "data":      {"action": action},
        "timestamp": datetime.utcnow().isoformat()
    })
    return {"ok": True}


@app.post("/api/control/camera")
async def control_camera(data: dict):
    angle = max(0, min(180, int(data.get("angle", 90))))
    queue_command("camera_angle", str(angle))
    detector.tracker.current_angle = angle
    await broadcast({
        "event":     "camera_move",
        "data":      {"angle": angle},
        "timestamp": datetime.utcnow().isoformat()
    })
    return {"ok": True}


@app.post("/api/control/tracking")
async def control_tracking(data: dict):
    enabled                  = bool(data.get("enabled", True))
    detector.tracker.enabled = enabled
    print(f"[TRACKER] {'Enabled' if enabled else 'Disabled'}")
    return {"ok": True, "tracking": enabled}


# ═══════════════════════════════════════════════════════════════════════
# USERS
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/users/")
async def list_users(authorization: Optional[str] = Header(None)):
    from fastapi import HTTPException
    if not authorization:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if _decode_token(authorization)["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    db    = SessionLocal()
    users = db.query(User).all()
    db.close()

    return [
        {
            "id":         u.id,
            "name":       u.name,
            "rfid_uid":   u.rfid_uid,
            "role":       u.role,
            "active":     u.active,
            "created_at": (
                u.created_at.isoformat() if u.created_at else None
            )
        }
        for u in users
    ]


@app.post("/api/users/")
async def create_user(
    data: dict,
    authorization: Optional[str] = Header(None)
):
    from fastapi import HTTPException
    import bcrypt

    if not authorization:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if _decode_token(authorization)["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    name         = data.get("name",         "").strip()
    raw_uid      = data.get("rfid_uid",     "").strip()
    role         = data.get("role",         "user")
    web_username = data.get("web_username", "").strip()
    web_password = data.get("web_password", "").strip()

    if not name:
        raise HTTPException(status_code=400, detail="name required")
    if not raw_uid:
        raise HTTPException(status_code=400, detail="rfid_uid required")

    rfid_uid = normalize_uid(raw_uid)
    if not rfid_uid:
        raise HTTPException(status_code=400, detail="Invalid RFID UID")

    uid_colons = ":".join(
        rfid_uid[i:i+2] for i in range(0, len(rfid_uid), 2)
    )

    db       = SessionLocal()
    existing = db.query(User).filter(
        User.rfid_uid.in_([rfid_uid, uid_colons])
    ).first()
    if existing:
        db.close()
        raise HTTPException(
            status_code=400,
            detail=(
                f"RFID '{rfid_uid}' already registered "
                f"to '{existing.name}'"
            )
        )

    if web_username and not web_password:
        db.close()
        raise HTTPException(status_code=400, detail="Password required")
    if web_password and not web_username:
        db.close()
        raise HTTPException(status_code=400, detail="Username required")
    if web_username and len(web_password) < 4:
        db.close()
        raise HTTPException(
            status_code=400, detail="Password min 4 chars"
        )
    if web_username:
        taken = db.query(WebUser).filter(
            WebUser.username == web_username
        ).first()
        if taken:
            db.close()
            raise HTTPException(
                status_code=400,
                detail=f"Username '{web_username}' taken"
            )

    rfid_user = User(name=name, rfid_uid=rfid_uid, role=role)
    db.add(rfid_user)
    db.commit()
    db.refresh(rfid_user)

    web_created = False
    if web_username and web_password:
        db.add(WebUser(
            username    = web_username,
            password    = bcrypt.hashpw(
                web_password.encode(), bcrypt.gensalt()
            ).decode(),
            role        = role,
            linked_rfid = rfid_uid
        ))
        db.commit()
        web_created = True

    db.close()
    print(f"[USER] Created '{name}' rfid={rfid_uid}")
    return {
        "ok":          True,
        "message":     (
            f"User '{name}' created"
            + (" with web account" if web_created else "")
        ),
        "user_id":     rfid_user.id,
        "web_created": web_created
    }


@app.put("/api/users/{user_id}")
async def update_user(
    user_id: int,
    data: dict,
    authorization: Optional[str] = Header(None)
):
    from fastapi import HTTPException
    if not authorization:
        raise HTTPException(status_code=401, detail="Not authenticated")

    db   = SessionLocal()
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        db.close()
        raise HTTPException(status_code=404, detail="Not found")

    if "name"     in data: user.name     = data["name"]
    if "active"   in data: user.active   = bool(data["active"])
    if "role"     in data: user.role     = data["role"]
    if "rfid_uid" in data:
        user.rfid_uid = normalize_uid(data["rfid_uid"])

    db.commit()
    db.close()
    return {"ok": True}


@app.delete("/api/users/{user_id}")
async def delete_user(
    user_id: int,
    authorization: Optional[str] = Header(None)
):
    from fastapi import HTTPException
    if not authorization:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if _decode_token(authorization)["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    db   = SessionLocal()
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        db.close()
        raise HTTPException(status_code=404, detail="Not found")

    name = user.name
    uid  = user.rfid_uid
    web  = db.query(WebUser).filter(
        WebUser.linked_rfid == uid
    ).first()
    if web:
        db.delete(web)
    db.delete(user)
    db.commit()
    db.close()
    return {"ok": True, "message": f"Deleted '{name}'"}


@app.get("/api/users/check-rfid/{uid}")
async def check_rfid_get(uid: str):
    clean      = normalize_uid(uid)
    uid_colons = ":".join(
        clean[i:i+2] for i in range(0, len(clean), 2)
    )
    db   = SessionLocal()
    user = db.query(User).filter(
        User.rfid_uid.in_([clean, uid_colons])
    ).first()
    db.close()
    if not user:
        return {"found": False}
    return {
        "found":  True,
        "name":   user.name,
        "active": user.active,
        "role":   user.role
    }


# ═══════════════════════════════════════════════════════════════════════
# LOGS
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/logs/")
async def get_logs(
    limit:         int = 100,
    access_type:   str = None,
    authorization: Optional[str] = Header(None)
):
    db    = SessionLocal()
    query = db.query(AccessLog)

    if authorization:
        info = _decode_token(authorization)
        if info["role"] != "admin":
            web_user = db.query(WebUser).filter(
                WebUser.username == info["username"]
            ).first()
            if web_user and web_user.linked_rfid:
                linked        = normalize_uid(web_user.linked_rfid)
                linked_colons = ":".join(
                    linked[i:i+2] for i in range(0, len(linked), 2)
                )
                query = query.filter(
                    AccessLog.rfid_uid.in_([linked, linked_colons]),
                    AccessLog.access_type.in_(["granted", "denied"])
                )
            else:
                db.close()
                return []

    if access_type:
        query = query.filter(AccessLog.access_type == access_type)

    logs = query.order_by(
        AccessLog.timestamp.desc()
    ).limit(limit).all()
    db.close()

    return [
        {
            "id":          l.id,
            "rfid_uid":    l.rfid_uid,
            "user_name":   l.user_name,
            "access_type": l.access_type,
            "timestamp":   l.timestamp.isoformat(),
            "note":        l.note or ""
        }
        for l in logs
    ]


@app.delete("/api/logs/")
async def clear_logs(authorization: Optional[str] = Header(None)):
    from fastapi import HTTPException
    if not authorization:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if _decode_token(authorization)["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    db    = SessionLocal()
    count = db.query(AccessLog).count()
    db.query(AccessLog).delete()
    db.commit()
    db.close()
    return {"ok": True, "deleted": count}


# ═══════════════════════════════════════════════════════════════════════
# CAMERA — 30fps stream
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/camera/stream")
async def camera_stream():
    def generate():
        # FIX: 30fps stream (was 15fps)
        frame_interval = 1.0 / 30
        while True:
            t0    = time.time()
            frame = webcam.get_frame()
            if frame:
                yield (
                    b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                    + frame + b"\r\n"
                )
            elapsed = time.time() - t0
            sleep_t = frame_interval - elapsed
            if sleep_t > 0:
                time.sleep(sleep_t)

    return StreamingResponse(
        generate(),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.get("/api/camera/snapshot")
async def camera_snapshot():
    frame = webcam.get_frame()
    if frame:
        return StreamingResponse(
            iter([frame]), media_type="image/jpeg"
        )
    return JSONResponse({"error": "No frame"}, status_code=503)


@app.get("/api/camera/list")
async def list_cameras():
    found = []
    for i in range(8):
        cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap = cv2.VideoCapture(i)
        if cap.isOpened():
            ret, _ = cap.read()
            if ret:
                w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                found.append({
                    "index":      i,
                    "resolution": f"{w}x{h}",
                    "label":      f"Camera {i} ({w}x{h})",
                    "current":    i == webcam.camera_index
                })
            cap.release()
    return {"cameras": found, "current": webcam.camera_index}


@app.post("/api/camera/select")
async def select_camera(data: dict):
    index   = int(data.get("index", 0))
    success = webcam.switch(index)
    time.sleep(0.3)
    if success:
        return {
            "ok":      True,
            "message": f"Camera {index} active",
            "index":   index
        }
    return {
        "ok":      False,
        "message": f"Camera {index} failed",
        "index":   webcam.camera_index
    }


@app.post("/api/camera/register")
async def camera_register(data: dict):
    stream_url = data.get("stream_url", "")
    db    = SessionLocal()
    state = db.query(SystemState).filter(SystemState.id == 1).first()
    if state:
        state.camera_url = stream_url
        db.commit()
    db.close()
    detector.set_stream_url(stream_url)
    await broadcast({
        "event":     "camera_connected",
        "data":      {"stream_url": stream_url},
        "timestamp": datetime.utcnow().isoformat()
    })
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════════
# YOLO
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/yolo/frame")
async def yolo_frame():
    if detector.last_frame:
        return StreamingResponse(
            iter([detector.last_frame]),
            media_type="image/jpeg"
        )
    return JSONResponse({"error": "No frame"}, status_code=503)


@app.get("/api/yolo/status")
async def yolo_status():
    return detector.get_status()


# ═══════════════════════════════════════════════════════════════════════
# ROOT
# ═══════════════════════════════════════════════════════════════════════

@app.get("/")
def root():
    return {
        "name":    "Smart Gate API",
        "version": "2.0.0",
        "docs":    "/docs",
        "camera":  "/api/camera/stream"
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)