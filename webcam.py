"""
PC Webcam Server
Streams your PC webcam as MJPEG — replaces ESP-CAM for testing
Run this alongside main.py
"""
import cv2
import threading
import time
from datetime import datetime


class PCWebcam:
    def __init__(self):
        self.cap        = None
        self.running    = False
        self.last_frame = None   # Raw JPEG bytes
        self.lock       = threading.Lock()

    def start(self, camera_index: int = 0) -> bool:
        self.cap = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW)  # CAP_DSHOW = Windows

        if not self.cap.isOpened():
            # Try without CAP_DSHOW (Linux/Mac)
            self.cap = cv2.VideoCapture(camera_index)

        if not self.cap.isOpened():
            print(f"[WEBCAM] Cannot open camera {camera_index}")
            return False

        # Set resolution
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        self.cap.set(cv2.CAP_PROP_FPS,          30)

        self.running = True
        threading.Thread(target=self._capture_loop, daemon=True).start()
        print(f"[WEBCAM] ✓ Camera {camera_index} started (640x480)")
        return True

    def stop(self):
        self.running = False
        if self.cap:
            self.cap.release()

    def _capture_loop(self):
        while self.running:
            ret, frame = self.cap.read()
            if not ret:
                time.sleep(0.1)
                continue

            # Add timestamp overlay
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cv2.putText(
                frame, "SmartGate CAM | " + ts,
                (10, frame.shape[0] - 10),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5, (0, 255, 0), 1
            )

            # Encode as JPEG
            _, buffer = cv2.imencode(
                ".jpg", frame,
                [cv2.IMWRITE_JPEG_QUALITY, 80]
            )
            with self.lock:
                self.last_frame = buffer.tobytes()

            time.sleep(1/30)  # ~30 FPS cap

    def get_frame(self):
        with self.lock:
            return self.last_frame

    def generate_mjpeg(self):
        """Generator for MJPEG streaming response"""
        while self.running:
            frame = self.get_frame()
            if frame is None:
                time.sleep(0.1)
                continue

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n"
                + frame +
                b"\r\n"
            )
            time.sleep(1/15)  # Stream at ~15 FPS to save bandwidth


# Global instance
webcam = PCWebcam()