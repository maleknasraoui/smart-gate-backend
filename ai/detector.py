"""
YOLO Object Detector — Fixed for PyTorch 2.6+ compatibility
Runs on PC, processes frames from ESP-CAM stream
"""
import cv2
import threading
import time
from datetime import datetime


class GateDetector:
    """
    Continuously reads the ESP-CAM stream and runs YOLO detection.
    
    Key fix: YOLO model is loaded lazily (only when first needed),
    not at import time. This prevents startup crashes.
    """

    TARGET_CLASSES = ["person", "car", "truck", "motorcycle", "bicycle"]

    def __init__(self):
        # Model is NOT loaded here — loaded lazily in start()
        self.model             = None
        self.model_loaded      = False
        self.model_error       = None          # Store any load error

        self.stream_url        = None
        self.running           = False
        self.thread            = None
        self.latest_detections = []
        self.primary_object    = None
        self.last_frame        = None          # Latest annotated JPEG bytes
        self.detection_count   = 0
        self.fps               = 0.0

        print("[YOLO] Detector instance created (model not loaded yet)")

    # ─── Model Loading ──────────────────────────────────────────────

    def _load_model(self) -> bool:
        """
        Load YOLO model. Returns True on success, False on failure.
        Called once in the background thread, not at import time.
        """
        if self.model_loaded:
            return True

        try:
            print("[YOLO] Loading YOLOv8n model...")
            from ultralytics import YOLO
            self.model        = YOLO("yolov8n.pt")
            self.model_loaded = True
            self.model_error  = None
            print("[YOLO] ✓ Model loaded successfully!")
            return True

        except Exception as e:
            self.model_error  = str(e)
            self.model_loaded = False
            print(f"[YOLO] ✗ Model load failed: {e}")
            print("[YOLO] Running in TEST MODE (simulated detections)")
            return False

    # ─── Public Control Methods ─────────────────────────────────────

    def set_stream_url(self, url: str):
        """Called when ESP-CAM registers its stream URL"""
        self.stream_url = url
        print(f"[YOLO] Stream URL set: {url}")

        # Restart detection with new URL
        if self.running:
            self.stop()
            time.sleep(1)
        self.start()

    def start(self):
        """Start background detection thread"""
        if self.running:
            return
        self.running = True
        self.thread  = threading.Thread(
            target=self._detection_loop,
            daemon=True,
            name="YOLODetector"
        )
        self.thread.start()
        print("[YOLO] Detection thread started")

    def stop(self):
        """Stop the detection thread"""
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)
        print("[YOLO] Detection stopped")

    # ─── Detection Loop ─────────────────────────────────────────────

    def _detection_loop(self):
        """
        Main background loop.
        1. Try to load model
        2. Try to open camera stream
        3. Run detection on frames
        4. Fall back to test mode if anything fails
        """

        # Step 1: Load model (safe, catches errors)
        model_ok = self._load_model()

        # Step 2: Check if we have a stream
        if not self.stream_url:
            print("[YOLO] No stream URL yet — running in TEST MODE")
            self._test_mode_loop()
            return

        # Step 3: Open video stream
        print(f"[YOLO] Opening stream: {self.stream_url}")
        cap = cv2.VideoCapture(self.stream_url)

        if not cap.isOpened():
            print(f"[YOLO] Cannot open stream — running in TEST MODE")
            self._test_mode_loop()
            return

        print("[YOLO] Stream opened — starting detection")

        fps_timer   = time.time()
        frame_count = 0

        while self.running:
            ret, frame = cap.read()

            if not ret:
                print("[YOLO] Stream lost, retrying in 3s...")
                cap.release()
                time.sleep(3)
                cap = cv2.VideoCapture(self.stream_url)
                continue

            frame_count += 1

            # Run YOLO every 3rd frame to reduce CPU load
            if frame_count % 3 == 0:
                if model_ok and self.model:
                    detections = self._process_frame(frame)
                else:
                    detections = []

                self.latest_detections = detections
                self._select_primary(detections)
                self.detection_count  += len(detections)

                # Calculate FPS
                elapsed = time.time() - fps_timer
                if elapsed >= 1.0:
                    self.fps      = round(frame_count / elapsed, 1)
                    fps_timer     = time.time()
                    frame_count   = 0

                # Save annotated frame
                self._save_annotated(frame, detections)

            # Small sleep to prevent 100% CPU usage
            time.sleep(0.01)

        cap.release()
        print("[YOLO] Detection loop ended")

    def _test_mode_loop(self):
        """
        Simulate detections when no camera / model is available.
        Used during development and testing.
        """
        import random
        print("[YOLO] TEST MODE active — simulating detections every 5s")

        while self.running:
            time.sleep(5)

            if not self.running:
                break

            # 60% chance of detecting a person
            if random.random() > 0.4:
                self.latest_detections = [{
                    "class":      "person",
                    "confidence": round(random.uniform(0.70, 0.99), 2),
                    "bbox":       [80, 40, 320, 460],
                    "area":       240 * 420
                }]
                print("[YOLO][TEST] Simulated: person detected")
            else:
                self.latest_detections = []
                print("[YOLO][TEST] Simulated: no detection")

            self._select_primary(self.latest_detections)

    # ─── Frame Processing ────────────────────────────────────────────

    def _process_frame(self, frame) -> list:
        """Run YOLO on one frame. Returns list of detections."""
        try:
            results = self.model(
                frame,
                verbose=False,   # Suppress per-frame console output
                conf=0.5,        # Minimum confidence threshold
                iou=0.45         # IOU threshold for NMS
            )
        except Exception as e:
            print(f"[YOLO] Inference error: {e}")
            return []

        detections = []

        for result in results:
            if result.boxes is None:
                continue

            for box in result.boxes:
                class_id   = int(box.cls[0])
                class_name = self.model.names[class_id]

                if class_name not in self.TARGET_CLASSES:
                    continue

                conf        = float(box.conf[0])
                x1, y1, x2, y2 = [int(v) for v in box.xyxy[0]]
                area        = (x2 - x1) * (y2 - y1)

                detections.append({
                    "class":      class_name,
                    "confidence": round(conf, 2),
                    "bbox":       [x1, y1, x2, y2],
                    "area":       area
                })

        return detections

    def _select_primary(self, detections: list):
        """
        Pick ONE primary object to track:
        - Persons take priority over vehicles
        - Among same class, pick largest (= closest to camera)
        """
        if not detections:
            self.primary_object = None
            return

        persons  = [d for d in detections if d["class"] == "person"]
        vehicles = [d for d in detections if d["class"] != "person"]

        if persons:
            self.primary_object = max(persons, key=lambda d: d["area"])
        elif vehicles:
            self.primary_object = max(vehicles, key=lambda d: d["area"])
        else:
            self.primary_object = None

    def _save_annotated(self, frame, detections: list):
        """Draw bounding boxes on frame and store as JPEG bytes."""
        annotated = frame.copy()

        for d in detections:
            x1, y1, x2, y2 = d["bbox"]

            # Green for persons, orange for vehicles
            color = (0, 255, 0) if d["class"] == "person" else (0, 165, 255)
            label = f"{d['class']} {d['confidence']:.0%}"

            cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
            cv2.putText(
                annotated, label,
                (x1, max(y1 - 10, 10)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6, color, 2
            )

        # Highlight primary object with blue border
        if self.primary_object:
            x1, y1, x2, y2 = self.primary_object["bbox"]
            cv2.rectangle(annotated, (x1, y1), (x2, y2), (255, 80, 0), 3)
            cv2.putText(
                annotated, "PRIMARY",
                (x1, y2 + 20),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5, (255, 80, 0), 2
            )

        # Timestamp overlay
        ts = datetime.now().strftime("%H:%M:%S")
        cv2.putText(
            annotated, ts,
            (10, annotated.shape[0] - 10),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5, (200, 200, 200), 1
        )

        # Encode to JPEG
        try:
            _, buffer     = cv2.imencode(
                ".jpg", annotated,
                [cv2.IMWRITE_JPEG_QUALITY, 75]
            )
            self.last_frame = buffer.tobytes()
        except Exception as e:
            print(f"[YOLO] Frame encode error: {e}")

    # ─── Status ─────────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Return current state — called by API endpoints"""
        return {
            "active":           self.running,
            "model_loaded":     self.model_loaded,
            "model_error":      self.model_error,
            "stream_url":       self.stream_url,
            "fps":              self.fps,
            "detection_count":  self.detection_count,
            "objects_detected": self.latest_detections,
            "primary_object":   self.primary_object,
            "object_count":     len(self.latest_detections),
            "person_detected":  any(
                d["class"] == "person"
                for d in self.latest_detections
            )
        }


# ─── Global Singleton ────────────────────────────────────────────────
# Created here but model is NOT loaded until start() is called
detector = GateDetector()