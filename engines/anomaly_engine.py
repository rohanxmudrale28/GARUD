import cv2
import numpy as np


class AnomalyEngine:
    def __init__(self,
                 motion_threshold=80000,
                 min_contour_area=5000,
                 frame_confirm=5):

        self.previous_gray = None
        self.motion_threshold = motion_threshold
        self.min_contour_area = min_contour_area
        self.frame_confirm = frame_confirm
        self.anomaly_counter = 0

    def detect_anomaly(self, frame):

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (21, 21), 0)

        if self.previous_gray is None:
            self.previous_gray = gray
            return False, 0

        frame_delta = cv2.absdiff(self.previous_gray, gray)
        thresh = cv2.threshold(frame_delta, 25, 255, cv2.THRESH_BINARY)[1]

        thresh = cv2.dilate(thresh, None, iterations=2)

        contours, _ = cv2.findContours(
            thresh.copy(),
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE
        )

        motion_score = 0
        significant_motion = False

        for contour in contours:
            area = cv2.contourArea(contour)
            if area > self.min_contour_area:
                motion_score += area
                significant_motion = True

        self.previous_gray = gray

        # Multi-frame confirmation logic
        if significant_motion and motion_score > self.motion_threshold:
            self.anomaly_counter += 1
        else:
            self.anomaly_counter = max(0, self.anomaly_counter - 1)

        anomaly_detected = self.anomaly_counter >= self.frame_confirm

        return anomaly_detected, motion_score