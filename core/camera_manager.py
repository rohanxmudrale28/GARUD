import cv2

class CameraManager:
    def __init__(self, source=0, width=1280, height=720):
        self.cap = cv2.VideoCapture(source)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

        if not self.cap.isOpened():
            raise Exception("Camera could not be opened")

    def get_frame(self):
        ret, frame = self.cap.read()
        if not ret:
            raise Exception("Failed to grab frame")
        return frame

    def release(self):
        self.cap.release()