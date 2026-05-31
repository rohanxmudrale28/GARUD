import mediapipe as mp
import cv2


class GestureEngine:
    def __init__(self):
        self.mp_pose = mp.solutions.pose
        self.pose = self.mp_pose.Pose()
        self.mp_draw = mp.solutions.drawing_utils

        self.prev_wrist_y = None
        self.motion_threshold = 40  # Increase to reduce sensitivity

    def detect_gesture(self, frame):
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        result = self.pose.process(rgb_frame)

        gesture_detected = False

        if result.pose_landmarks:
            landmarks = result.pose_landmarks.landmark

            right_wrist = landmarks[self.mp_pose.PoseLandmark.RIGHT_WRIST]
            wrist_y = int(right_wrist.y * frame.shape[0])

            if self.prev_wrist_y is not None:
                if abs(wrist_y - self.prev_wrist_y) > self.motion_threshold:
                    gesture_detected = True

            self.prev_wrist_y = wrist_y

        return gesture_detected