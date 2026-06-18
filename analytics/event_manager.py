from config import FRAME_CONFIRMATION, CROWD_THRESHOLD

# YOLO class IDs (COCO dataset — what your ObjectEngine uses)
COCO_PERSON   = 0
COCO_KNIFE    = 43
COCO_SCISSORS = 76
# Note: handguns/rifles are not in COCO. We detect them via motion + crowd heuristics.
# If you add a custom weapon model later, add its class IDs here.

WEAPON_CLASS_IDS = {43, 76}   # knife, scissors — detectable in COCO

# How many consecutive anomaly frames = a confirmed anomaly event
ANOMALY_CONFIRM = 5

class EventManager:
    def __init__(self):
        self.detection_memory  = {}   # track_id → frame count
        self.anomaly_streak    = 0    # consecutive anomaly frames
        self.high_motion_count = 0    # consecutive high-motion frames

    # ── Called every frame with current track IDs ──────────────────────────────
    def verify_detections(self, track_ids):
        """
        Confirms detections that have persisted for FRAME_CONFIRMATION frames.
        Same logic as original — unchanged.
        """
        verified = []
        for tid in track_ids:
            self.detection_memory[tid] = self.detection_memory.get(tid, 0) + 1
            if self.detection_memory[tid] >= FRAME_CONFIRMATION:
                verified.append(tid)
        # Prune IDs no longer in frame
        active = set(track_ids)
        self.detection_memory = {k: v for k, v in self.detection_memory.items() if k in active}
        return verified

    # ── Main evaluation — called every frame ───────────────────────────────────
    def evaluate_events(self, crowd_count, motion_score=0,
                        anomaly_detected=False, gesture_detected=False,
                        detected_classes=None):
        """
        Returns a list of (event_name, severity) tuples.

        Parameters
        ----------
        crowd_count      : int   — number of people detected this frame
        motion_score     : float — raw motion area from AnomalyEngine
        anomaly_detected : bool  — multi-frame confirmed anomaly from AnomalyEngine
        gesture_detected : bool  — wrist raise from GestureEngine
        detected_classes : set   — YOLO class IDs seen this frame (for weapon detection)
        """
        events = []
        detected_classes = detected_classes or set()

        # ── RED: critical ──────────────────────────────────────────────────────

        # Weapon in frame (knife / scissors detected by YOLO)
        if WEAPON_CLASS_IDS & detected_classes:
            events.append(("Weapon Detected", "red"))

        # Extreme overcrowding (3× threshold) + high motion = stampede risk
        if crowd_count >= CROWD_THRESHOLD * 3 and anomaly_detected:
            events.append(("Stampede Risk", "red"))

        # Very high motion with confirmed anomaly = potential riot / violence
        elif anomaly_detected and motion_score > 300_000:
            events.append(("Extreme Violence Detected", "red"))

        # ── AMBER: medium ──────────────────────────────────────────────────────

        # Crowd above threshold
        if crowd_count >= CROWD_THRESHOLD * 2 and not any(s == "red" for _, s in events):
            events.append(("High Crowd Density", "amber"))
        elif crowd_count >= CROWD_THRESHOLD:
            events.append(("High Crowd Density", "amber"))

        # Confirmed motion anomaly without extreme violence
        if anomaly_detected and not any(e[0] in ("Stampede Risk", "Extreme Violence Detected") for e in events):
            events.append(("Motion Anomaly Detected", "amber"))

        # ── YELLOW: low ───────────────────────────────────────────────────────

        # Gesture (raised hand / distress signal)
        if gesture_detected:
            events.append(("Distress Gesture Detected", "yellow"))

        # Moderate motion (not confirmed anomaly, but above baseline)
        if motion_score > 80_000 and not anomaly_detected:
            events.append(("Unusual Motion", "yellow"))

        # ── No events = normal ─────────────────────────────────────────────────
        if not events:
            events.append(("Normal", "white"))

        return events