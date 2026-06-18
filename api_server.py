"""
GARUD HQ API Server
-------------------
  GET  /api/cameras        — all cameras + live state
  GET  /api/camera/<id>    — single camera
  GET  /api/stream/<id>    — MJPEG live feed
  GET  /api/events         — recent event log
  GET  /api/alarm          — current alarm state (for dashboard polling)
  POST /api/register       — register a new camera at runtime

Run: python api_server.py
"""

import cv2
import time
import datetime
import threading
import sys
import os

from flask import Flask, Response, jsonify, request
from flask_cors import CORS

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engines.object_engine   import ObjectEngine
from engines.crowd_engine    import CrowdEngine
from engines.anomaly_engine  import AnomalyEngine
from engines.gesture_engine  import GestureEngine
from analytics.event_manager import EventManager
from alerts.alert_manager    import AlertManager
from database.db_manager     import DBManager
import config

app  = Flask(__name__)
CORS(app)

# ── Severity helpers ──────────────────────────────────────────────────────────
SEV_RANK = {"white": 0, "yellow": 1, "amber": 2, "red": 3}

def top_severity(events):
    """Return the highest severity from a list of (name, sev) tuples."""
    best_sev  = "white"
    best_name = "Normal"
    for name, sev in events:
        if SEV_RANK.get(sev, 0) > SEV_RANK.get(best_sev, 0):
            best_sev  = sev
            best_name = name
    return best_name, best_sev

# ── Camera registry — edit source for each camera ────────────────────────────
CAMERA_REGISTRY = {
    "C001": {
        "id":     "C001",
        "name":   "Test Camera",
        "city":   "Mumbai",
        "lat":    19.0760,
        "lng":    72.8777,
        "source": 0,           # 0 = MacBook webcam
        # "source": "garud_v2-3/recordings/threat_20260306_182607.mp4",
    },
    # Uncomment and edit to add more cameras:
    # "C002": {
    #     "id":     "C002",
    #     "name":   "Main Gate",
    #     "city":   "New Delhi",
    #     "lat":    28.6139,
    #     "lng":    77.2090,
    #     "source": "rtsp://admin:pass@192.168.1.100:554/stream1",
    # },
}

# ── Shared state ──────────────────────────────────────────────────────────────
camera_states = {}
event_log     = []
alarm_state   = {"active": False, "reason": "", "cam_id": "", "since": ""}
state_lock    = threading.Lock()

# ── Per-camera processing thread ──────────────────────────────────────────────
class CameraProcessor(threading.Thread):
    def __init__(self, cam_id, cam_meta):
        super().__init__(daemon=True)
        self.cam_id     = cam_id
        self.cam_meta   = cam_meta
        self.latest_frame = None
        self.frame_lock = threading.Lock()

    def run(self):
        source = self.cam_meta.get("source", 0)

        try:
            cap            = cv2.VideoCapture(source)
            detector       = ObjectEngine(model_path=config.MODEL_PATH)
            crowd_engine   = CrowdEngine()
            anomaly_engine = AnomalyEngine()
            gesture_engine = GestureEngine()
            event_manager  = EventManager()
            alert_manager  = AlertManager()
            db_manager     = DBManager()
        except Exception as e:
            print(f"[{self.cam_id}] Engine init failed: {e}")
            _set_state(self.cam_id, self.cam_meta, offline=True)
            return

        print(f"[{self.cam_id}] Pipeline running — source: {source}")
        prev_time = 0

        while True:
            ret, frame = cap.read()
            if not ret:
                time.sleep(2)
                cap = cv2.VideoCapture(source)
                continue

            # ── 1. Object detection ───────────────────────────────────────
            results = detector.detect(frame)

            # ── 2. Extract detected YOLO class IDs for weapon detection ───
            detected_classes = set()
            if results and results[0].boxes:
                for box in results[0].boxes:
                    detected_classes.add(int(box.cls))

            # ── 3. Crowd count ────────────────────────────────────────────
            crowd_count, track_ids = crowd_engine.count_people(results)
            event_manager.verify_detections(track_ids)

            # ── 4. Anomaly detection ──────────────────────────────────────
            anomaly_detected, motion_score = anomaly_engine.detect_anomaly(frame)

            # ── 5. Gesture detection ──────────────────────────────────────
            gesture_detected = gesture_engine.detect_gesture(frame)

            # ── 6. Evaluate events ────────────────────────────────────────
            events = event_manager.evaluate_events(
                crowd_count      = crowd_count,
                motion_score     = motion_score,
                anomaly_detected = anomaly_detected,
                gesture_detected = gesture_detected,
                detected_classes = detected_classes,
            )

            last_event, severity = top_severity(events)

            # ── 7. Alarm: trigger if severity = red ───────────────────────
            with state_lock:
                if severity == "red":
                    alarm_state["active"] = True
                    alarm_state["reason"] = last_event
                    alarm_state["cam_id"] = self.cam_id
                    alarm_state["since"]  = datetime.datetime.now().isoformat()
                elif all(SEV_RANK.get(camera_states.get(cid, {}).get("severity","white"), 0) < 3
                         for cid in camera_states if cid != self.cam_id):
                    # Only clear alarm if no other camera is also red
                    alarm_state["active"] = False

            # ── 8. Log non-normal events ──────────────────────────────────
            for event_name, sev in events:
                if sev != "white":
                    ts        = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                    safe_name = event_name.replace(" ", "_")
                    snapshot  = f"snapshots/{self.cam_id}_{safe_name}_{ts}.jpg"
                    cv2.imwrite(snapshot, frame)
                    db_manager.insert_event(event_name, sev, crowd_count, motion_score, snapshot)
                    alert_manager.trigger([event_name])
                    _add_event(self.cam_id, self.cam_meta, event_name, sev, crowd_count, motion_score)

            # ── 9. FPS ────────────────────────────────────────────────────
            curr_time = time.time()
            fps       = round(1 / (curr_time - prev_time)) if prev_time else 0
            prev_time = curr_time

            # ── 10. Annotate frame ────────────────────────────────────────
            annotated = results[0].plot()

            # Severity banner at top
            sev_colours = {"white":(180,180,180), "yellow":(0,210,255),
                           "amber":(0,140,255),   "red":(0,0,255)}
            banner_col = sev_colours.get(severity, (180,180,180))
            cv2.rectangle(annotated, (0,0), (annotated.shape[1], 36), (10,15,25), -1)
            cv2.putText(annotated, f"{self.cam_id}  |  {last_event}  |  {severity.upper()}",
                        (10, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.75, banner_col, 2)

            # Stats
            cv2.putText(annotated, f"FPS: {fps}",
                        (20, 65),  cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0,255,0), 2)
            cv2.putText(annotated, f"Crowd: {crowd_count}",
                        (20, 95),  cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0,100,255), 2)
            cv2.putText(annotated, f"Motion: {int(motion_score)}",
                        (20, 125), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255,100,0), 2)
            if anomaly_detected:
                cv2.putText(annotated, "⚠ ANOMALY",
                            (20, 165), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0,0,255), 3)
            if gesture_detected:
                cv2.putText(annotated, "⚠ GESTURE",
                            (20, 205), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0,165,255), 3)

            with self.frame_lock:
                self.latest_frame = annotated.copy()

            _set_state(self.cam_id, self.cam_meta,
                       severity=severity, crowd=crowd_count,
                       motion=int(motion_score), event=last_event, fps=fps)


def _set_state(cam_id, meta, severity="white", crowd=0,
               motion=0, event="Normal", fps=0, offline=False):
    with state_lock:
        camera_states[cam_id] = {
            "id":       cam_id,
            "name":     meta.get("name", cam_id),
            "city":     meta.get("city", ""),
            "lat":      meta.get("lat", 0),
            "lng":      meta.get("lng", 0),
            "severity": "offline" if offline else severity,
            "crowd":    crowd,
            "motion":   motion,
            "event":    event,
            "fps":      fps,
            "updated":  datetime.datetime.now().isoformat(),
        }

def _add_event(cam_id, meta, event_name, severity, crowd, motion):
    with state_lock:
        event_log.insert(0, {
            "cam_id":    cam_id,
            "city":      meta.get("city", ""),
            "event":     event_name,
            "severity":  severity,
            "crowd":     crowd,
            "motion":    motion,
            "time":      datetime.datetime.now().strftime("%H:%M:%S"),
            "timestamp": datetime.datetime.now().isoformat(),
        })
        if len(event_log) > 100:
            event_log.pop()

# ── API routes ────────────────────────────────────────────────────────────────
@app.route("/api/cameras")
def get_cameras():
    with state_lock:
        return jsonify(list(camera_states.values()))

@app.route("/api/camera/<cam_id>")
def get_camera(cam_id):
    with state_lock:
        cam = camera_states.get(cam_id)
    return jsonify(cam) if cam else (jsonify({"error": "Not found"}), 404)

@app.route("/api/events")
def get_events():
    limit = int(request.args.get("limit", 50))
    with state_lock:
        return jsonify(event_log[:limit])

@app.route("/api/alarm")
def get_alarm():
    with state_lock:
        return jsonify(dict(alarm_state))

@app.route("/api/alarm/dismiss", methods=["POST"])
def dismiss_alarm():
    with state_lock:
        alarm_state["active"] = False
    return jsonify({"status": "dismissed"})

@app.route("/api/stream/<cam_id>")
def stream(cam_id):
    processor = processors.get(cam_id)
    if not processor:
        return "Camera not found", 404
    def generate():
        while True:
            with processor.frame_lock:
                frame = processor.latest_frame
            if frame is None:
                time.sleep(0.05)
                continue
            _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                   + buf.tobytes() + b"\r\n")
            time.sleep(1/25)
    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")

@app.route("/api/register", methods=["POST"])
def register_camera():
    data = request.json
    required = ["id","name","city","lat","lng","source"]
    if not all(k in data for k in required):
        return jsonify({"error": f"Missing fields: {required}"}), 400
    cam_id = data["id"]
    CAMERA_REGISTRY[cam_id] = data
    p = CameraProcessor(cam_id, data)
    processors[cam_id] = p
    p.start()
    return jsonify({"status": "ok", "cam_id": cam_id}), 201

@app.route("/api/health")
def health():
    return jsonify({"status": "online", "cameras": len(processors),
                    "time": datetime.datetime.now().isoformat()})

# ── Start ─────────────────────────────────────────────────────────────────────
processors = {}

def start_processors():
    for cam_id, meta in CAMERA_REGISTRY.items():
        p = CameraProcessor(cam_id, meta)
        processors[cam_id] = p
        p.start()
        print(f"[GARUD] Started processor for {cam_id} — {meta['city']}")

if __name__ == "__main__":
    os.makedirs("snapshots", exist_ok=True)
    start_processors()
    app.run(host="0.0.0.0", port=5001, threaded=True)