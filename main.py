import cv2
import time
import datetime

from core.camera_manager import CameraManager
from engines.object_engine import ObjectEngine
from engines.crowd_engine import CrowdEngine
from engines.anomaly_engine import AnomalyEngine
from engines.gesture_engine import GestureEngine
from analytics.event_manager import EventManager
from alerts.alert_manager import AlertManager
from database.db_manager import DBManager


def main():

    # ===============================
    # Initialize All Engines
    # ===============================
    camera = CameraManager()
    detector = ObjectEngine()
    crowd_engine = CrowdEngine()
    anomaly_engine = AnomalyEngine()
    gesture_engine = GestureEngine()
    event_manager = EventManager()
    alert_manager = AlertManager()
    db_manager = DBManager()

    prev_time = 0

    while True:
        frame = camera.get_frame()

        # ===============================
        # 1️⃣ Object Detection
        # ===============================
        results = detector.detect(frame)

        # ===============================
        # 2️⃣ Crowd Counting
        # ===============================
        crowd_count, track_ids = crowd_engine.count_people(results)
        verified_ids = event_manager.verify_detections(track_ids)

        # ===============================
        # 3️⃣ Motion Anomaly Detection
        # ===============================
        anomaly_detected, motion_score = anomaly_engine.detect_anomaly(frame)

        # ===============================
        # 4️⃣ Gesture Detection
        # ===============================
        gesture_detected = gesture_engine.detect_gesture(frame)

        # ===============================
        # 5️⃣ Evaluate Events
        # ===============================
        events = event_manager.evaluate_events(
            crowd_count,
            motion_score,
            anomaly_detected,
            gesture_detected
        )

        # ===============================
        # 6️⃣ Log Events + Save Snapshots
        # ===============================
        for event_name, severity in events:

            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_event_name = event_name.replace(" ", "_")
            snapshot_path = f"snapshots/{safe_event_name}_{timestamp}.jpg"

            # Save snapshot
            cv2.imwrite(snapshot_path, frame)

            # Insert into DB
            db_manager.insert_event(
                event_name,
                severity,
                crowd_count,
                motion_score,
                snapshot_path
            )

            print(f"[LOGGED] {event_name} | Severity: {severity}")

            # Trigger console alert
            alert_manager.trigger([event_name])

        # ===============================
        # 7️⃣ Draw Detection Output
        # ===============================
        annotated_frame = results[0].plot()

        # FPS Calculation
        curr_time = time.time()
        fps = 1 / (curr_time - prev_time) if prev_time != 0 else 0
        prev_time = curr_time

        # ===============================
        # Display Info Overlay
        # ===============================
        cv2.putText(
            annotated_frame,
            f"FPS: {int(fps)}",
            (20, 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            1,
            (0, 255, 0),
            2
        )

        cv2.putText(
            annotated_frame,
            f"Crowd Count: {crowd_count}",
            (20, 80),
            cv2.FONT_HERSHEY_SIMPLEX,
            1,
            (0, 0, 255),
            2
        )

        cv2.putText(
            annotated_frame,
            f"Motion Score: {int(motion_score)}",
            (20, 120),
            cv2.FONT_HERSHEY_SIMPLEX,
            1,
            (255, 0, 0),
            2
        )

        if anomaly_detected:
            cv2.putText(
                annotated_frame,
                "ANOMALY DETECTED",
                (20, 170),
                cv2.FONT_HERSHEY_SIMPLEX,
                1.2,
                (0, 0, 255),
                3
            )

        if gesture_detected:
            cv2.putText(
                annotated_frame,
                "GESTURE ALERT",
                (20, 220),
                cv2.FONT_HERSHEY_SIMPLEX,
                1.2,
                (0, 165, 255),
                3
            )

        # ===============================
        # Show Window
        # ===============================
        cv2.imshow("GARUD v4 - Cognitive Surveillance System", annotated_frame)

        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    camera.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()