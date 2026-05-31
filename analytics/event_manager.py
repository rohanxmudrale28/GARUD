from config import FRAME_CONFIRMATION, CROWD_THRESHOLD

class EventManager:
    def __init__(self):
        self.detection_memory = {}
    
    def verify_detections(self, track_ids):
        verified = []

        for tid in track_ids:
            self.detection_memory[tid] = self.detection_memory.get(tid, 0) + 1

            if self.detection_memory[tid] >= FRAME_CONFIRMATION:
                verified.append(tid)

        return verified

    def evaluate_events(self, crowd_count):
        events = []

        if crowd_count >= CROWD_THRESHOLD:
            events.append("High Crowd Density")

        return events