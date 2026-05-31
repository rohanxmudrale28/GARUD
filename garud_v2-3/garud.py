#!/usr/bin/env python3
"""
GARUD v3 — Cognitive Surveillance System
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
New in v3:
  1. Video clip saving  — auto-saves 15s clip on every threat to /recordings
  2. SMS + Email alerts — Twilio SMS, SMTP email on threat detection
  3. RTSP / IP camera   — connect any IP camera, not just webcam
  4. Facial recognition — match faces against known_faces/ folder
  5. Login system       — password-protected web dashboard
"""

import cv2
import numpy as np
import tkinter as tk
import tkinter.ttk as ttk
import tkinter.simpledialog as simpledialog
import threading
import time
import math
import queue
import os
import json
import subprocess
import platform
import smtplib
import sqlite3
import hashlib
import secrets
from datetime import datetime
from collections import defaultdict, deque
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

# ── Optional imports ──────────────────────────────────────────────────────────
try:
    from flask import Flask, Response, render_template_string, jsonify, request, session, redirect
    FLASK_OK = True
except ImportError:
    FLASK_OK = False

try:
    from ultralytics import YOLO
    YOLO_OK = True
except ImportError:
    YOLO_OK = False

try:
    import torch
    MPS    = hasattr(torch.backends,"mps") and torch.backends.mps.is_available()
    DEVICE = "mps" if MPS else "cpu"
except ImportError:
    MPS = False; DEVICE = "cpu"

try:
    from PIL import Image, ImageTk
    PIL_OK = True
except ImportError:
    PIL_OK = False

try:
    from twilio.rest import Client as TwilioClient
    TWILIO_OK = True
except ImportError:
    TWILIO_OK = False

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR       = Path(__file__).parent
RECORDINGS_DIR = BASE_DIR / "recordings"
KNOWN_DIR      = BASE_DIR / "known_faces"
LOGS_DIR       = BASE_DIR / "logs"
DB_PATH        = BASE_DIR / "garud.db"
CONFIG_PATH    = BASE_DIR / "config.json"

for d in [RECORDINGS_DIR, KNOWN_DIR, LOGS_DIR]:
    d.mkdir(exist_ok=True)

# ── Constants ─────────────────────────────────────────────────────────────────
VERSION        = "v3.0"
BG_DARK        = "#0A0E1A"
BG_MID         = "#111827"
BG_CARD        = "#1A2035"
ACCENT         = "#00FFB2"
DANGER         = "#FF3B5C"
WARNING        = "#FFB800"
TEXT_MAIN      = "#E8EAF0"
TEXT_DIM       = "#6B7A99"
BLUE           = "#4488FF"

WEAPON_CLASSES  = {"knife","scissors","baseball bat","bottle","fork","spoon"}
VEHICLE_CLASSES = {"car","truck","bus","motorcycle","bicycle"}
PERSON_CLASS    = "person"

KP_L_SHOULDER=5; KP_R_SHOULDER=6
KP_L_ELBOW=7;    KP_R_ELBOW=8
KP_L_WRIST=9;    KP_R_WRIST=10
KP_L_HIP=11;     KP_R_HIP=12
KP_L_KNEE=13;    KP_R_KNEE=14

def ts(fmt="%H:%M:%S"):   return datetime.now().strftime(fmt)
def ts_file():            return datetime.now().strftime("%Y%m%d_%H%M%S")
def hex2bgr(h):
    h=h.lstrip("#"); return (int(h[4:6],16),int(h[2:4],16),int(h[0:2],16))
def put_label(img,text,pos,color=(0,255,178),scale=0.5,thickness=1):
    x,y=pos
    (tw,th),_=cv2.getTextSize(text,cv2.FONT_HERSHEY_SIMPLEX,scale,thickness)
    cv2.rectangle(img,(x-3,y-th-5),(x+tw+3,y+3),(8,12,22),-1)
    cv2.putText(img,text,(x,y),cv2.FONT_HERSHEY_SIMPLEX,scale,color,thickness,cv2.LINE_AA)
def draw_box(img,x1,y1,x2,y2,color,label="",thickness=2):
    cv2.rectangle(img,(x1,y1),(x2,y2),color,thickness)
    if label: put_label(img,label,(x1,max(y1-8,12)),color)


# ═════════════════════════════════════════════════════════════════════════════
#  1. DATABASE  — stores alerts, clips, users
# ═════════════════════════════════════════════════════════════════════════════
class Database:
    def __init__(self):
        self.conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        self._lock = threading.Lock()
        self._init()

    def _init(self):
        with self._lock:
            c = self.conn.cursor()
            c.executescript("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    time     TEXT,
                    kind     TEXT,
                    msg      TEXT,
                    clip     TEXT,
                    camera   TEXT
                );
                CREATE TABLE IF NOT EXISTS users (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE,
                    pw_hash  TEXT,
                    role     TEXT DEFAULT 'operator'
                );
            """)
            # Create default admin user if none exists
            c.execute("SELECT COUNT(*) FROM users")
            if c.fetchone()[0] == 0:
                ph = hashlib.sha256("garud2024".encode()).hexdigest()
                c.execute("INSERT INTO users(username,pw_hash,role) VALUES(?,?,?)",
                          ("admin", ph, "admin"))
            self.conn.commit()

    def log_alert(self, kind, msg, clip="", camera="cam0"):
        with self._lock:
            self.conn.execute(
                "INSERT INTO alerts(time,kind,msg,clip,camera) VALUES(?,?,?,?,?)",
                (ts("%Y-%m-%d %H:%M:%S"), kind, msg, clip, camera)
            )
            self.conn.commit()

    def get_alerts(self, limit=100):
        with self._lock:
            c = self.conn.cursor()
            c.execute("SELECT time,kind,msg,clip,camera FROM alerts ORDER BY id DESC LIMIT ?", (limit,))
            rows = c.fetchall()
        return [{"time":r[0],"kind":r[1],"msg":r[2],"clip":r[3],"camera":r[4]} for r in rows]

    def check_user(self, username, password):
        ph = hashlib.sha256(password.encode()).hexdigest()
        with self._lock:
            c = self.conn.cursor()
            c.execute("SELECT role FROM users WHERE username=? AND pw_hash=?", (username,ph))
            row = c.fetchone()
        return row[0] if row else None


# ═════════════════════════════════════════════════════════════════════════════
#  2. CONFIG  — load/save Twilio, email, camera settings
# ═════════════════════════════════════════════════════════════════════════════
DEFAULT_CONFIG = {
    "twilio_sid":      "",
    "twilio_token":    "",
    "twilio_from":     "",
    "alert_phone":     "",
    "smtp_host":       "smtp.gmail.com",
    "smtp_port":       587,
    "smtp_user":       "",
    "smtp_pass":       "",
    "alert_email":     "",
    "camera_sources":  ["0"],
    "clip_seconds":    15,
    "crowd_threshold": 8,
    "web_port":        8080,
    "web_password":    "garud2024",
}

def load_config():
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
            # fill missing keys with defaults
            for k,v in DEFAULT_CONFIG.items():
                cfg.setdefault(k,v)
            return cfg
        except:
            pass
    save_config(DEFAULT_CONFIG)
    return dict(DEFAULT_CONFIG)

def save_config(cfg):
    with open(CONFIG_PATH,"w") as f:
        json.dump(cfg, f, indent=2)


# ═════════════════════════════════════════════════════════════════════════════
#  3. NOTIFIER  — SMS via Twilio + Email via SMTP
# ═════════════════════════════════════════════════════════════════════════════
class Notifier:
    def __init__(self, cfg):
        self.cfg = cfg
        self._last = {}   # debounce per alert type

    def notify(self, msg, kind="threat", clip_path=""):
        key = f"{kind}"
        now = time.time()
        if now - self._last.get(key, 0) < 30:   # 30s debounce
            return
        self._last[key] = now
        # Run in background so it never blocks the camera loop
        threading.Thread(target=self._send_all,
                         args=(msg, clip_path), daemon=True).start()

    def _send_all(self, msg, clip_path):
        self._send_sms(msg)
        self._send_email(msg, clip_path)

    def _send_sms(self, msg):
        cfg = self.cfg
        if not all([cfg.get("twilio_sid"), cfg.get("twilio_token"),
                    cfg.get("twilio_from"), cfg.get("alert_phone")]):
            return
        if not TWILIO_OK:
            print("[SMS] twilio not installed — pip install twilio")
            return
        try:
            client = TwilioClient(cfg["twilio_sid"], cfg["twilio_token"])
            client.messages.create(
                body=f"⚠ GARUD ALERT [{ts()}]\n{msg}",
                from_=cfg["twilio_from"],
                to=cfg["alert_phone"]
            )
            print(f"[SMS] Sent to {cfg['alert_phone']}")
        except Exception as e:
            print(f"[SMS] Failed: {e}")

    def _send_email(self, msg, clip_path=""):
        cfg = self.cfg
        if not all([cfg.get("smtp_user"), cfg.get("smtp_pass"), cfg.get("alert_email")]):
            return
        try:
            mime = MIMEMultipart()
            mime["From"]    = cfg["smtp_user"]
            mime["To"]      = cfg["alert_email"]
            mime["Subject"] = f"⚠ GARUD Alert — {ts()}"
            body = f"""
GARUD Surveillance Alert
━━━━━━━━━━━━━━━━━━━━━━━━
Time    : {ts('%Y-%m-%d %H:%M:%S')}
Alert   : {msg}
Clip    : {clip_path if clip_path else 'N/A'}
━━━━━━━━━━━━━━━━━━━━━━━━
GARUD Cognitive Surveillance System {VERSION}
            """
            mime.attach(MIMEText(body,"plain"))
            with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as s:
                s.starttls()
                s.login(cfg["smtp_user"], cfg["smtp_pass"])
                s.send_message(mime)
            print(f"[EMAIL] Sent to {cfg['alert_email']}")
        except Exception as e:
            print(f"[EMAIL] Failed: {e}")


# ═════════════════════════════════════════════════════════════════════════════
#  4. VIDEO RECORDER  — saves 15s clips on threat
# ═════════════════════════════════════════════════════════════════════════════
class ClipRecorder:
    """
    Keeps a rolling 5s pre-buffer, then records 10s post-trigger.
    Total clip = 5s before + 10s after = 15s evidence clip.
    """
    def __init__(self, cfg):
        self.cfg        = cfg
        self.pre_buf    = deque(maxlen=150)   # ~5s at 30fps
        self.recording  = False
        self.post_frames= []
        self.post_target= 300                 # 10s at 30fps
        self.writer     = None
        self.clip_path  = ""
        self._lock      = threading.Lock()

    def feed(self, frame):
        """Call every frame. Returns clip path when recording finishes."""
        with self._lock:
            self.pre_buf.append(frame.copy())
            if self.recording:
                if self.writer and len(self.post_frames) < self.post_target:
                    self.writer.write(frame)
                    self.post_frames.append(1)
                elif self.recording and len(self.post_frames) >= self.post_target:
                    return self._finish()
        return None

    def trigger(self):
        """Call when threat detected. Starts saving."""
        with self._lock:
            if self.recording:
                return   # already recording
            self.recording   = True
            self.post_frames = []
            fname    = f"threat_{ts_file()}.mp4"
            self.clip_path = str(RECORDINGS_DIR / fname)
            # Write pre-buffer first
            h,w = list(self.pre_buf)[-1].shape[:2] if self.pre_buf else (720,1280)
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            self.writer = cv2.VideoWriter(self.clip_path, fourcc, 30, (w,h))
            for f in list(self.pre_buf):
                self.writer.write(f)
            print(f"[CLIP] Recording started → {self.clip_path}")

    def _finish(self):
        if self.writer:
            self.writer.release()
            self.writer = None
        self.recording = False
        path = self.clip_path
        print(f"[CLIP] Saved → {path}")
        return path

    def is_recording(self):
        return self.recording


# ═════════════════════════════════════════════════════════════════════════════
#  5. FACE RECOGNIZER  — matches against known_faces/
# ═════════════════════════════════════════════════════════════════════════════
class FaceRecognizer:
    """
    Simple but effective face recognition:
    - Loads all images from known_faces/{name}.jpg
    - Uses OpenCV LBPH recognizer if opencv-contrib available
    - Falls back to detection-only if contrib not installed
    """
    def __init__(self):
        # Try contrib LBPH recognizer, fall back gracefully
        try:
            self.recognizer = cv2.face.LBPHFaceRecognizer_create()
            self.recog_ok   = True
        except AttributeError:
            print("[FACE] opencv-contrib not found — face detection only, no recognition")
            print("[FACE] Fix: pip uninstall opencv-python && pip install opencv-contrib-python")
            self.recognizer = None
            self.recog_ok   = False
        self.face_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        self.label_map    = {}   # id -> name
        self.trained      = False
        self._load_known()

    def _load_known(self):
        if not self.recog_ok: return
        images, labels = [], []
        label_id = 0
        for img_path in KNOWN_DIR.glob("*.jpg"):
            name = img_path.stem
            img  = cv2.imread(str(img_path), cv2.IMREAD_GRAYSCALE)
            if img is None: continue
            faces = self.face_cascade.detectMultiScale(img, 1.1, 5, minSize=(40,40))
            for (x,y,w,h) in faces:
                images.append(img[y:y+h,x:x+w])
                labels.append(label_id)
            self.label_map[label_id] = name
            label_id += 1

        if images:
            self.recognizer.train(images, np.array(labels))
            self.trained = True
            print(f"[FACE] Loaded {len(self.label_map)} known faces: {list(self.label_map.values())}")
        else:
            print(f"[FACE] No known faces found in {KNOWN_DIR}")
            print(f"[FACE] Add photos as: known_faces/PersonName.jpg")

    def reload(self):
        self.label_map = {}
        self.trained   = False
        self._load_known()

    def identify(self, frame):
        """Returns list of (x,y,w,h,name,confidence) for all faces in frame."""
        results = []
        gray    = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces   = self.face_cascade.detectMultiScale(gray, 1.1, 5, minSize=(40,40))
        for (x,y,w,h) in faces:
            name  = "Unknown"
            conf  = 0.0
            if self.trained and self.recog_ok:
                try:
                    label, dist = self.recognizer.predict(gray[y:y+h,x:x+w])
                    conf = max(0, 100 - dist) / 100
                    if conf > 0.4:
                        name = self.label_map.get(label, "Unknown")
                except:
                    pass
            results.append((x,y,w,h,name,conf))
        return results


# ═════════════════════════════════════════════════════════════════════════════
#  ALARM SYSTEM
# ═════════════════════════════════════════════════════════════════════════════
class AlarmSystem:
    def __init__(self):
        self.flash_until = 0
        self.last_beep   = 0

    def trigger(self, duration=4.0):
        self.flash_until = time.time() + duration
        threading.Thread(target=self._beep, daemon=True).start()

    def _beep(self):
        if time.time() - self.last_beep < 2: return
        self.last_beep = time.time()
        try:
            if platform.system() == "Darwin":
                for s in ["/System/Library/Sounds/Sosumi.aiff",
                           "/System/Library/Sounds/Basso.aiff"]:
                    if os.path.exists(s):
                        subprocess.Popen(["afplay",s],
                            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                        return
        except: pass
        print("\a\a", end="", flush=True)

    def should_flash(self):
        return time.time() < self.flash_until

    def clear(self):
        self.flash_until = 0


# ═════════════════════════════════════════════════════════════════════════════
#  TRACKER
# ═════════════════════════════════════════════════════════════════════════════
class Tracker:
    def __init__(self, max_lost=25):
        self.nid=0; self.objs={}; self.lost={}
        self.paths=defaultdict(lambda: deque(maxlen=50))
        self.max_lost=max_lost

    def _iou(self,a,b):
        ix1,iy1=max(a[0],b[0]),max(a[1],b[1])
        ix2,iy2=min(a[2],b[2]),min(a[3],b[3])
        iw,ih=max(0,ix2-ix1),max(0,iy2-iy1); inter=iw*ih
        ua=(a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-inter
        return inter/ua if ua>0 else 0

    def update(self, dets):
        used=set(); new_o={}
        for oid,(cx,cy,box) in list(self.objs.items()):
            best,bd=0,-1
            for i,d in enumerate(dets):
                if i in used: continue
                iou=self._iou(box,d[:4])
                if iou>best: best,bd=iou,i
            if best>0.15 and bd>=0:
                x1,y1,x2,y2=dets[bd][:4]; ncx,ncy=(x1+x2)//2,(y1+y2)//2
                new_o[oid]=(ncx,ncy,(x1,y1,x2,y2))
                self.paths[oid].append((ncx,ncy)); self.lost[oid]=0; used.add(bd)
            else:
                self.lost[oid]=self.lost.get(oid,0)+1
                if self.lost[oid]<=self.max_lost: new_o[oid]=(cx,cy,box)
        for i,d in enumerate(dets):
            if i not in used:
                x1,y1,x2,y2=d[:4]; cx,cy=(x1+x2)//2,(y1+y2)//2
                new_o[self.nid]=(cx,cy,(x1,y1,x2,y2))
                self.paths[self.nid].append((cx,cy)); self.lost[self.nid]=0; self.nid+=1
        self.objs=new_o
        assigned={}
        for oid,(cx,cy,box) in self.objs.items():
            for d in dets:
                if (d[0],d[1],d[2],d[3])==box: assigned[oid]=d; break
        return assigned


# ═════════════════════════════════════════════════════════════════════════════
#  FIGHT DETECTOR
# ═════════════════════════════════════════════════════════════════════════════
class FightDetector:
    def __init__(self):
        self.history = defaultdict(lambda: deque(maxlen=10))

    def analyse(self, oid, keypoints):
        kp=keypoints; score=0
        def get(idx):
            if float(kp[idx][2])>0.3:
                return (float(kp[idx][0]),float(kp[idx][1]),float(kp[idx][2]))
            return None
        ls=get(KP_L_SHOULDER); rs=get(KP_R_SHOULDER)
        lw=get(KP_L_WRIST);    rw=get(KP_R_WRIST)
        lh=get(KP_L_HIP);      rh=get(KP_R_HIP)
        if ls is not None and lw is not None:
            if lw[1]<ls[1]: score+=2
        if rs is not None and rw is not None:
            if rw[1]<rs[1]: score+=2
        if all(x is not None for x in [ls,rs,lw,rw]):
            mid_x=(ls[0]+rs[0])/2
            if abs(lw[0]-mid_x)>abs(ls[0]-rs[0])*0.8: score+=1
            if abs(rw[0]-mid_x)>abs(ls[0]-rs[0])*0.8: score+=1
        if all(x is not None for x in [lh,rh,ls,rs]):
            tw=abs(ls[0]-rs[0])
            if tw>0 and abs(((ls[1]+rs[1])/2)-((lh[1]+rh[1])/2))/tw<0.8: score+=1
        self.history[oid].append((lw,rw))
        if len(self.history[oid])>=3:
            plw,prw=self.history[oid][-3]
            if lw is not None and plw is not None:
                dx=lw[0]-plw[0]; dy=lw[1]-plw[1]
                if math.sqrt(dx*dx+dy*dy)>20: score+=2
            if rw is not None and prw is not None:
                dx=rw[0]-prw[0]; dy=rw[1]-prw[1]
                if math.sqrt(dx*dx+dy*dy)>20: score+=2
        return score>=5


# ═════════════════════════════════════════════════════════════════════════════
#  GARUD ENGINE v3
# ═════════════════════════════════════════════════════════════════════════════
class GarudEngine:
    def __init__(self, cfg, db):
        self.cfg        = cfg
        self.db         = db
        self.running    = False
        self.cap        = None
        self.model      = None
        self.pose_model = None
        self.tracker    = Tracker()
        self.alarm      = AlarmSystem()
        self.fight_det  = FightDetector()
        self.recorder   = ClipRecorder(cfg)
        self.notifier   = Notifier(cfg)
        self.face_rec   = FaceRecognizer()

        self.latest_jpg = None
        self.stats = {"fps":0,"objects":0,"faces":0,"crowd":0,
                      "alerts":0,"threats":0,"mode":"init","recording":False}
        self._last_alert = {}
        self.heatmap_acc = None
        self.frame_times = deque(maxlen=30)
        self._eq         = None

        # UI toggles
        self.show_tracks  = True
        self.show_faces   = True
        self.show_heatmap = False
        self.show_anomaly = True
        self.show_pose    = True
        self.face_blur    = False
        self.face_recog   = True

        self._load_models()

    def _load_models(self):
        if not YOLO_OK:
            self.stats["mode"] = "demo"; return
        try:
            self.model = YOLO("yolov8n.pt"); self.model.to(DEVICE)
            print("[GARUD] Detection model ✓")
        except Exception as e:
            print(f"[WARN] Detection: {e}")
        try:
            self.pose_model = YOLO("yolov8n-pose.pt"); self.pose_model.to(DEVICE)
            print("[GARUD] Pose model ✓")
        except Exception as e:
            print(f"[WARN] Pose: {e}")
        self.stats["mode"] = "MPS" if MPS else "CPU"

    # ── Camera source — webcam index OR rtsp:// url ────────────────────────
    def start(self, source=None, eq=None):
        self._eq = eq
        if source is None:
            sources = self.cfg.get("camera_sources", ["0"])
            source  = sources[0] if sources else "0"
        # Convert "0","1" string indices to int
        try:    src = int(source)
        except: src = source   # rtsp:// url stays as string
        print(f"[GARUD] Opening camera: {src}")
        self.cap = cv2.VideoCapture(src)
        if not self.cap.isOpened():
            msg = f"Cannot open camera: {src}"
            if eq: eq.put({"type":"error","msg":msg})
            print(f"[ERROR] {msg}")
            return
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH,  1280)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT,  720)
        self.cap.set(cv2.CAP_PROP_FPS, 30)
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self.running = False
        if self.cap: self.cap.release()

    def _loop(self):
        while self.running:
            t0 = time.time()
            ret, frame = self.cap.read()
            if not ret:
                time.sleep(0.05); continue

            h,w = frame.shape[:2]
            if self.heatmap_acc is None:
                self.heatmap_acc = np.zeros((h,w), dtype=np.float32)

            overlay = frame.copy()

            # ── Feed to recorder (pre-buffer) ─────────────────────────────
            clip_done = self.recorder.feed(frame)
            if clip_done:
                self.stats["recording"] = False

            # ── Detection ─────────────────────────────────────────────────
            dets, pose_res = self._detect(frame)
            tracked = self.tracker.update(dets)

            # ── Heatmap ───────────────────────────────────────────────────
            crowd = 0
            for oid,det in tracked.items():
                x1,y1,x2,y2=det[:4]; cx,cy=(x1+x2)//2,(y1+y2)//2
                cv2.circle(self.heatmap_acc,(cx,cy),50,1.8,-1)
                if det[4]==PERSON_CLASS: crowd+=1

            if self.show_heatmap:
                hm=cv2.normalize(self.heatmap_acc,None,0,255,cv2.NORM_MINMAX)
                hm=cv2.GaussianBlur(hm.astype(np.uint8),(61,61),0)
                hm=cv2.applyColorMap(hm,cv2.COLORMAP_JET)
                overlay=cv2.addWeighted(overlay,0.65,hm,0.35,0)

            # ── Alarm flash overlay ───────────────────────────────────────
            if self.alarm.should_flash():
                flash=np.zeros_like(overlay); flash[:]=(0,0,160)
                alpha=0.3+0.12*math.sin(time.time()*10)
                overlay=cv2.addWeighted(overlay,1-alpha,flash,alpha,0)
                cv2.putText(overlay,"⚠  THREAT DETECTED  ⚠",
                    (w//2-230,h//2),cv2.FONT_HERSHEY_SIMPLEX,
                    1.2,(0,50,255),3,cv2.LINE_AA)

            # Recording indicator
            if self.recorder.is_recording():
                cv2.circle(overlay,(w-30,30),12,(0,0,255),-1)
                put_label(overlay,"● REC",(w-80,38),(0,80,255),0.55,2)

            # ── Draw tracked objects ──────────────────────────────────────
            for oid,det in tracked.items():
                x1,y1,x2,y2,label,conf=det
                color=self._label_color(label)
                if self.show_tracks and oid in self.tracker.paths:
                    path=list(self.tracker.paths[oid])
                    for i in range(1,len(path)):
                        a=i/len(path)
                        c=tuple(int(x*a) for x in color)
                        cv2.line(overlay,path[i-1],path[i],c,2)
                draw_box(overlay,x1,y1,x2,y2,color,f"#{oid} {label} {conf:.0%}")

                # Weapon alert
                if label in WEAPON_CLASSES:
                    cv2.rectangle(overlay,(x1-4,y1-4),(x2+4,y2+4),(0,0,255),3)
                    put_label(overlay,"⚠ WEAPON",(x1,y2+18),(0,50,255),0.6,2)
                    self._alert(f"WEAPON: {label.upper()}","threat",label)

                # Speed anomaly
                if self.show_anomaly and oid in self.tracker.paths:
                    spd=self._speed(self.tracker.paths[oid])
                    if spd>22:
                        put_label(overlay,"⚡ FAST MOTION",(x1,y2+18),(0,180,255))
                        self._alert("FAST MOTION DETECTED","anomaly",f"spd{oid}")

            # ── Pose / fight ──────────────────────────────────────────────
            if pose_res and self.show_pose:
                self._process_pose(overlay, pose_res)

            # ── Face detection + recognition ──────────────────────────────
            if self.show_faces:
                face_results = self.face_rec.identify(frame)
                self.stats["faces"] = len(face_results)
                for (fx,fy,fw,fh,name,conf) in face_results:
                    if self.face_blur:
                        roi=overlay[fy:fy+fh,fx:fx+fw]
                        if roi.size>0:
                            overlay[fy:fy+fh,fx:fx+fw]=cv2.GaussianBlur(roi,(55,55),0)
                    else:
                        color=(0,200,100) if name!="Unknown" else (0,140,255)
                        cv2.rectangle(overlay,(fx,fy),(fx+fw,fy+fh),color,2)
                        lbl = f"{name} {conf:.0%}" if name!="Unknown" else "Unknown"
                        put_label(overlay,lbl,(fx,fy-8),color)
                        if name != "Unknown" and self.face_recog:
                            self._alert(f"KNOWN PERSON: {name}","face",name)
            else:
                self.stats["faces"] = 0

            # ── Crowd alert ───────────────────────────────────────────────
            crowd_thresh = self.cfg.get("crowd_threshold", 8)
            if crowd > crowd_thresh:
                self._alert(f"HIGH CROWD DENSITY: {crowd} persons","crowd","crowd")
                banner=np.zeros((38,w,3),dtype=np.uint8); banner[:]=(20,55,0)
                cv2.putText(banner,f"⚠  HIGH CROWD DENSITY — {crowd} PERSONS",
                    (w//2-240,26),cv2.FONT_HERSHEY_SIMPLEX,
                    0.65,hex2bgr(WARNING),2,cv2.LINE_AA)
                overlay[h-48:h-10]=banner

            # ── HUD ───────────────────────────────────────────────────────
            self._draw_hud(overlay,w,h,crowd)

            # ── Stats ─────────────────────────────────────────────────────
            self.stats.update({"objects":len(tracked),"crowd":crowd,
                                "recording":self.recorder.is_recording()})
            elapsed=time.time()-t0
            self.frame_times.append(elapsed)
            self.stats["fps"]=round(1/(sum(self.frame_times)/len(self.frame_times)+1e-9),1)

            # Share frame
            _,jpg=cv2.imencode(".jpg",overlay,[cv2.IMWRITE_JPEG_QUALITY,75])
            self.latest_jpg=jpg.tobytes()
            if self._eq:
                try: self._eq.put_nowait({"type":"frame","frame":overlay,"stats":dict(self.stats)})
                except queue.Full: pass

    def _detect(self, frame):
        if self.model:
            try:
                res=self.model(frame,verbose=False,conf=0.3)
                dets=[]
                for r in res:
                    for box in r.boxes:
                        x1,y1,x2,y2=map(int,box.xyxy[0].tolist())
                        dets.append((x1,y1,x2,y2,r.names[int(box.cls[0])],float(box.conf[0])))
                pose_res=None
                if self.pose_model:
                    try: pose_res=self.pose_model(frame,verbose=False,conf=0.3)
                    except: pass
                return dets,pose_res
            except Exception as e:
                print(f"[WARN] detect: {e}")
        # Demo
        t=time.time(); h,w=frame.shape[:2]
        ctrs=[(int(w*0.25+70*math.sin(t*0.6)),int(h*0.5+30*math.cos(t*0.4))),
              (int(w*0.6+50*math.cos(t*0.8)),int(h*0.45+40*math.sin(t*0.5))),
              (int(w*0.75+35*math.sin(t)),int(h*0.6+25*math.cos(t*0.7)))]
        lbls=["person","person","backpack"]; confs=[0.91,0.88,0.76]; dets=[]
        for i,(cx,cy) in enumerate(ctrs):
            bw,bh=(60,140)if lbls[i]=="person"else(50,60)
            x1,y1=max(0,cx-bw//2),max(0,cy-bh//2)
            x2,y2=min(w,cx+bw//2),min(h,cy+bh//2)
            dets.append((x1,y1,x2,y2,lbls[i],confs[i]))
        return dets,None

    def _process_pose(self, overlay, pose_res):
        try:
            for r in pose_res:
                if not hasattr(r,"keypoints") or r.keypoints is None: continue
                kps_all=r.keypoints.data.cpu().numpy()
                boxes=r.boxes
                SKEL=[(KP_L_SHOULDER,KP_R_SHOULDER),(KP_L_SHOULDER,KP_L_ELBOW),
                      (KP_L_ELBOW,KP_L_WRIST),(KP_R_SHOULDER,KP_R_ELBOW),
                      (KP_R_ELBOW,KP_R_WRIST),(KP_L_SHOULDER,KP_L_HIP),
                      (KP_R_SHOULDER,KP_R_HIP),(KP_L_HIP,KP_R_HIP),
                      (KP_L_HIP,KP_L_KNEE),(KP_R_HIP,KP_R_KNEE)]
                for pi,kps in enumerate(kps_all):
                    if kps.shape[0]<17: continue
                    for a,b in SKEL:
                        if float(kps[a][2])>0.3 and float(kps[b][2])>0.3:
                            cv2.line(overlay,(int(kps[a][0]),int(kps[a][1])),
                                     (int(kps[b][0]),int(kps[b][1])),(0,200,120),2)
                    for kp in kps:
                        if float(kp[2])>0.3:
                            cv2.circle(overlay,(int(kp[0]),int(kp[1])),4,(0,255,178),-1)
                    if self.fight_det.analyse(pi,kps):
                        if boxes is not None and pi<len(boxes.xyxy):
                            bx=boxes.xyxy[pi].cpu().numpy().astype(int)
                            x1,y1,x2,y2=bx
                            cv2.rectangle(overlay,(x1,y1),(x2,y2),(0,0,255),3)
                            put_label(overlay,"⚠ FIGHT",(x1,y1-20),(0,50,255),0.65,2)
                        self._alert("FIGHT / AGGRESSIVE BEHAVIOUR","threat","fight")
        except Exception as e:
            pass

    def _draw_hud(self, img, w, h, crowd):
        panel=np.zeros((185,295,3),dtype=np.uint8); panel[:]=(26,32,53)
        cv2.rectangle(panel,(0,0),(294,184),(0,255,178),1)
        rec_col = DANGER if self.recorder.is_recording() else TEXT_DIM
        lines=[
            (f"GARUD {VERSION}",        ACCENT,   0.65,2),
            (f"FPS    : {self.stats['fps']}",     TEXT_MAIN,0.48,1),
            (f"MODE   : {self.stats['mode']}",    ACCENT if MPS else TEXT_DIM,0.48,1),
            (f"OBJECTS: {self.stats['objects']}", TEXT_MAIN,0.48,1),
            (f"FACES  : {self.stats['faces']}",   TEXT_MAIN,0.48,1),
            (f"CROWD  : {crowd}",                 WARNING if crowd>self.cfg.get('crowd_threshold',8) else TEXT_MAIN,0.48,1),
            (f"THREATS: {self.stats['threats']}", DANGER,0.48,1),
            (f"ALERTS : {self.stats['alerts']}",  WARNING,0.48,1),
        ]
        for i,(txt,col,sc,th) in enumerate(lines):
            cv2.putText(panel,txt,(10,22+i*21),cv2.FONT_HERSHEY_SIMPLEX,sc,hex2bgr(col),th,cv2.LINE_AA)
        img[10:10+185,10:10+295]=panel
        cv2.putText(img,ts(),(w-165,28),cv2.FONT_HERSHEY_SIMPLEX,0.5,hex2bgr(TEXT_DIM),1,cv2.LINE_AA)

    def _label_color(self, label):
        if label==PERSON_CLASS:     return (0,255,178)
        if label in WEAPON_CLASSES: return (0,50,255)
        if label in VEHICLE_CLASSES:return (0,200,255)
        return (160,210,0)

    def _speed(self, path):
        pts=list(path)
        if len(pts)<5: return 0
        dx=pts[-1][0]-pts[-5][0]; dy=pts[-1][1]-pts[-5][1]
        return math.sqrt(dx*dx+dy*dy)

    def _alert(self, msg, kind, tag):
        key=f"{kind}_{tag}"
        now=time.time()
        if now-self._last_alert.get(key,0)<5: return
        self._last_alert[key]=now
        self.stats["alerts"]+=1
        if kind=="threat": self.stats["threats"]+=1

        # Trigger recording + alarm on threats
        if kind=="threat":
            self.recorder.trigger()
            self.alarm.trigger(4.0)
            self.stats["recording"]=True
            # Send notifications in background
            clip=self.recorder.clip_path
            self.notifier.notify(msg,"threat",clip)

        entry={"time":ts(),"msg":msg,"kind":kind}
        self.db.log_alert(kind, msg, self.recorder.clip_path if kind=="threat" else "")
        if self._eq:
            try: self._eq.put_nowait({"type":"alert","entry":entry})
            except queue.Full: pass


# ═════════════════════════════════════════════════════════════════════════════
#  WEB DASHBOARD  with login
# ═════════════════════════════════════════════════════════════════════════════
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GARUD — Live Dashboard</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Rajdhani:wght@400;600;700&display=swap');
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0A0E1A;color:#E8EAF0;font-family:'Rajdhani',sans-serif;min-height:100vh}
.topbar{background:#111827;border-bottom:2px solid #00FFB2;padding:10px 24px;
        display:flex;align-items:center;justify-content:space-between}
.logo{font-size:22px;font-weight:700;color:#00FFB2;letter-spacing:3px;font-family:'Share Tech Mono',monospace}
.badge{font-size:11px;background:#1A2035;border:1px solid #00FFB2;color:#00FFB2;
       padding:3px 10px;border-radius:2px;font-family:'Share Tech Mono',monospace}
.grid{display:grid;grid-template-columns:1fr 340px;gap:10px;padding:10px;height:calc(100vh - 54px)}
.feed-box{background:#000;border:1px solid #1A2035;border-radius:4px;overflow:hidden;
          position:relative;display:flex;align-items:center;justify-content:center}
.feed-box img{width:100%;height:100%;object-fit:contain}
.rec-badge{position:absolute;top:12px;right:12px;background:#FF3B5C;color:#fff;
           font-family:'Share Tech Mono',monospace;font-size:11px;padding:4px 10px;
           border-radius:2px;display:none}
.rec-badge.active{display:block;animation:blink 1s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.side{display:flex;flex-direction:column;gap:8px;overflow-y:auto}
.card{background:#111827;border:1px solid #1A2035;border-radius:4px;padding:12px}
.card-title{font-family:'Share Tech Mono',monospace;font-size:10px;color:#00FFB2;
            letter-spacing:2px;margin-bottom:10px;padding-bottom:5px;border-bottom:1px solid #1A2035}
.stat-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px}
.stat{background:#0A0E1A;border-radius:3px;padding:8px;text-align:center}
.stat-val{font-size:22px;font-weight:700;font-family:'Share Tech Mono',monospace;color:#00FFB2}
.stat-lbl{font-size:9px;color:#6B7A99;letter-spacing:1px;margin-top:1px}
.stat.danger .stat-val{color:#FF3B5C}
.stat.warn   .stat-val{color:#FFB800}
.alert-list{max-height:280px;overflow-y:auto;display:flex;flex-direction:column;gap:4px}
.alert-item{padding:6px 9px;border-radius:3px;font-size:11px;font-family:'Share Tech Mono',monospace;
            border-left:3px solid #333;background:#0A0E1A;word-break:break-word}
.alert-item.threat{border-color:#FF3B5C;color:#FF8099}
.alert-item.anomaly{border-color:#FFB800;color:#FFD966}
.alert-item.crowd{border-color:#00FFB2;color:#80FFDA}
.alert-item.face{border-color:#4488FF;color:#88AAFF}
.clip-list{max-height:160px;overflow-y:auto;display:flex;flex-direction:column;gap:4px;margin-top:6px}
.clip-item{padding:5px 8px;background:#0A0E1A;border-radius:3px;font-family:'Share Tech Mono',monospace;
           font-size:10px;color:#6B7A99;display:flex;justify-content:space-between;align-items:center}
.clip-item a{color:#4488FF;text-decoration:none}
.pulse{width:9px;height:9px;border-radius:50%;background:#00FFB2;display:inline-block;
       animation:pulse 1.5s infinite;margin-right:6px}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(.85)}}
.btn{padding:6px 14px;border:1px solid #1A2035;background:#1A2035;color:#E8EAF0;
     font-family:'Share Tech Mono',monospace;font-size:10px;border-radius:3px;cursor:pointer}
.btn:hover{border-color:#00FFB2;color:#00FFB2}
.btn.danger{border-color:#FF3B5C;color:#FF3B5C}
::-webkit-scrollbar{width:4px}::-webkit-scrollbar-track{background:#0A0E1A}
::-webkit-scrollbar-thumb{background:#1A2035;border-radius:2px}
</style>
</head>
<body>
<div class="topbar">
  <div class="logo">⬡ GARUD</div>
  <div style="display:flex;align-items:center;gap:12px">
    <span class="pulse" id="pulse"></span>
    <span id="live-txt" style="font-size:12px;font-family:'Share Tech Mono',monospace;color:#00FFB2">LIVE</span>
    <span class="badge">MVP v3.0</span>
    <a href="/logout" class="btn">LOGOUT</a>
  </div>
</div>
<div class="grid">
  <div class="feed-box">
    <img id="feed" src="/video_feed">
    <div class="rec-badge" id="rec-badge">● REC</div>
  </div>
  <div class="side">
    <div class="card">
      <div class="card-title">LIVE STATISTICS</div>
      <div class="stat-grid">
        <div class="stat"><div class="stat-val" id="s-fps">—</div><div class="stat-lbl">FPS</div></div>
        <div class="stat"><div class="stat-val" id="s-obj">—</div><div class="stat-lbl">OBJECTS</div></div>
        <div class="stat warn"><div class="stat-val" id="s-crowd">—</div><div class="stat-lbl">CROWD</div></div>
        <div class="stat"><div class="stat-val" id="s-faces">—</div><div class="stat-lbl">FACES</div></div>
        <div class="stat danger"><div class="stat-val" id="s-threats">—</div><div class="stat-lbl">THREATS</div></div>
        <div class="stat danger"><div class="stat-val" id="s-alerts">—</div><div class="stat-lbl">ALERTS</div></div>
      </div>
    </div>
    <div class="card">
      <div class="card-title">ALERT LOG</div>
      <div class="alert-list" id="alert-list"></div>
    </div>
    <div class="card">
      <div class="card-title">SAVED CLIPS</div>
      <div class="clip-list" id="clip-list"><div style="color:#6B7A99;font-size:11px;font-family:'Share Tech Mono',monospace">No clips yet</div></div>
    </div>
    <div class="card" style="font-family:'Share Tech Mono',monospace;font-size:10px;color:#6B7A99">
      <div class="card-title">SYSTEM</div>
      <div id="sys-mode">—</div>
      <div style="margin-top:3px">GARUD MVP v3 • MacBook Pro M4</div>
    </div>
  </div>
</div>
<script>
let alertCount=0;
function poll(){
  fetch('/stats').then(r=>r.json()).then(d=>{
    document.getElementById('s-fps').textContent    =d.fps;
    document.getElementById('s-obj').textContent    =d.objects;
    document.getElementById('s-crowd').textContent  =d.crowd;
    document.getElementById('s-faces').textContent  =d.faces;
    document.getElementById('s-threats').textContent=d.threats;
    document.getElementById('s-alerts').textContent =d.alerts;
    document.getElementById('sys-mode').textContent ='Mode: '+d.mode;
    document.getElementById('rec-badge').classList.toggle('active',d.recording);
  }).catch(()=>{});
  fetch('/alerts').then(r=>r.json()).then(data=>{
    if(data.length!==alertCount){
      alertCount=data.length;
      const list=document.getElementById('alert-list');
      list.innerHTML='';
      data.slice(0,50).forEach(a=>{
        const el=document.createElement('div');
        el.className='alert-item '+(a.kind||'');
        el.textContent='['+a.time+'] '+a.msg;
        list.appendChild(el);
      });
    }
  }).catch(()=>{});
  fetch('/clips').then(r=>r.json()).then(clips=>{
    const list=document.getElementById('clip-list');
    if(clips.length===0) return;
    list.innerHTML='';
    clips.forEach(c=>{
      const el=document.createElement('div');
      el.className='clip-item';
      el.innerHTML='<span>'+c.name+'</span><a href="/clip/'+c.name+'" download>⬇ download</a>';
      list.appendChild(el);
    });
  }).catch(()=>{});
}
setInterval(poll,900);
poll();
</script>
</body>
</html>
"""

LOGIN_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>GARUD — Login</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&display=swap');
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0A0E1A;display:flex;align-items:center;justify-content:center;
     min-height:100vh;font-family:'Share Tech Mono',monospace}
.box{background:#111827;border:1px solid #00FFB2;border-radius:6px;padding:40px;
     width:360px;text-align:center}
.logo{color:#00FFB2;font-size:28px;letter-spacing:4px;margin-bottom:6px}
.sub{color:#6B7A99;font-size:11px;margin-bottom:30px;letter-spacing:2px}
input{width:100%;padding:10px 14px;background:#0A0E1A;border:1px solid #1A2035;
      color:#E8EAF0;font-family:'Share Tech Mono',monospace;font-size:13px;
      border-radius:3px;margin-bottom:12px;outline:none}
input:focus{border-color:#00FFB2}
button{width:100%;padding:12px;background:#00FFB2;color:#0A0E1A;
       font-family:'Share Tech Mono',monospace;font-size:13px;font-weight:bold;
       border:none;border-radius:3px;cursor:pointer;letter-spacing:2px}
button:hover{background:#00DDA0}
.err{color:#FF3B5C;font-size:11px;margin-top:8px}
</style>
</head>
<body>
<div class="box">
  <div class="logo">⬡ GARUD</div>
  <div class="sub">COGNITIVE SURVEILLANCE SYSTEM</div>
  <form method="POST">
    <input name="username" placeholder="USERNAME" autocomplete="off">
    <input name="password" type="password" placeholder="PASSWORD">
    <button type="submit">LOGIN →</button>
  </form>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
</div>
</body>
</html>
"""

def start_web_server(engine, db, cfg):
    if not FLASK_OK:
        print("[WEB] Flask not installed — pip install flask"); return

    app = Flask(__name__)
    app.secret_key = secrets.token_hex(16)
    port = cfg.get("web_port", 8080)

    def auth_required(f):
        from functools import wraps
        @wraps(f)
        def decorated(*args, **kwargs):
            if not session.get("user"):
                return redirect("/login")
            return f(*args, **kwargs)
        return decorated

    @app.route("/login", methods=["GET","POST"])
    def login():
        error = ""
        if request.method == "POST":
            u = request.form.get("username","")
            p = request.form.get("password","")
            role = db.check_user(u, p)
            if role:
                session["user"] = u
                session["role"] = role
                return redirect("/")
            error = "Invalid credentials"
        return render_template_string(LOGIN_HTML, error=error)

    @app.route("/logout")
    def logout():
        session.clear(); return redirect("/login")

    @app.route("/")
    @auth_required
    def index():
        return render_template_string(DASHBOARD_HTML)

    @app.route("/video_feed")
    @auth_required
    def video_feed():
        def gen():
            while True:
                jpg=engine.latest_jpg
                if jpg:
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"+jpg+b"\r\n"
                time.sleep(0.033)
        return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")

    @app.route("/stats")
    @auth_required
    def stats():
        return jsonify(engine.stats)

    @app.route("/alerts")
    @auth_required
    def alerts():
        return jsonify(db.get_alerts(100))

    @app.route("/clips")
    @auth_required
    def clips():
        files = sorted(RECORDINGS_DIR.glob("*.mp4"), key=os.path.getmtime, reverse=True)
        return jsonify([{"name":f.name,"size":f.stat().st_size} for f in files[:20]])

    @app.route("/clip/<name>")
    @auth_required
    def serve_clip(name):
        from flask import send_from_directory
        return send_from_directory(str(RECORDINGS_DIR), name)

    print(f"[WEB] Dashboard → http://localhost:{port}  (login: admin / garud2024)")
    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port,
                               debug=False, use_reloader=False),
        daemon=True
    ).start()


# ═════════════════════════════════════════════════════════════════════════════
#  SETTINGS WINDOW
# ═════════════════════════════════════════════════════════════════════════════
class SettingsWindow:
    def __init__(self, parent, cfg, engine):
        self.cfg    = cfg
        self.engine = engine
        self.win    = tk.Toplevel(parent)
        self.win.title("GARUD Settings")
        self.win.geometry("500x620")
        self.win.configure(bg=BG_DARK)
        self.win.resizable(False, False)
        self._build()

    def _build(self):
        w = self.win
        tk.Label(w, text="⬡  GARUD Settings", font=("Courier New",16,"bold"),
                 fg=ACCENT, bg=BG_DARK).pack(pady=(16,4))
        tk.Frame(w, bg=ACCENT, height=1).pack(fill="x", padx=20)

        nb = ttk.Notebook(w)
        nb.pack(fill="both", expand=True, padx=12, pady=10)

        # Style notebook
        style = ttk.Style()
        style.configure("TNotebook",        background=BG_MID)
        style.configure("TNotebook.Tab",    background=BG_CARD, foreground=TEXT_DIM,
                        font=("Courier New",9))
        style.map("TNotebook.Tab",          background=[("selected",BG_MID)],
                  foreground=[("selected",ACCENT)])

        self._tab_alerts(nb)
        self._tab_camera(nb)
        self._tab_system(nb)

        tk.Button(w, text="SAVE & APPLY", command=self._save,
                  bg=ACCENT, fg=BG_DARK, font=("Courier New",11,"bold"),
                  relief="flat", cursor="hand2").pack(fill="x", padx=20, pady=(0,12))

    def _field(self, parent, label, key, show=""):
        f = tk.Frame(parent, bg=BG_MID)
        f.pack(fill="x", pady=3)
        tk.Label(f, text=label, width=20, anchor="w", bg=BG_MID,
                 fg=TEXT_DIM, font=("Courier New",9)).pack(side="left")
        v = tk.StringVar(value=str(self.cfg.get(key,"")))
        tk.Entry(f, textvariable=v, bg=BG_CARD, fg=TEXT_MAIN,
                 font=("Courier New",9), relief="flat",
                 insertbackground=ACCENT, show=show).pack(side="left", fill="x", expand=True)
        setattr(self, f"_v_{key}", v)

    def _tab_alerts(self, nb):
        t = tk.Frame(nb, bg=BG_MID)
        nb.add(t, text=" SMS & EMAIL ")
        tk.Label(t, text="Twilio SMS", bg=BG_MID, fg=ACCENT,
                 font=("Courier New",10,"bold")).pack(anchor="w", padx=10, pady=(10,4))
        for lbl,key in [("Account SID","twilio_sid"),("Auth Token","twilio_token"),
                         ("From Number","twilio_from"),("Alert Phone","alert_phone")]:
            self._field(t, lbl, key)
        tk.Label(t, text="Email (Gmail SMTP)", bg=BG_MID, fg=ACCENT,
                 font=("Courier New",10,"bold")).pack(anchor="w", padx=10, pady=(12,4))
        for lbl,key in [("Gmail Address","smtp_user"),("App Password","smtp_pass"),
                         ("Alert Email","alert_email")]:
            self._field(t, lbl, key, show="*" if "pass" in key or "token" in key else "")
        tk.Label(t, text="Use Gmail App Password (not your main password)\nSettings → Security → 2FA → App Passwords",
                 bg=BG_MID, fg=TEXT_DIM, font=("Courier New",8), justify="left").pack(anchor="w", padx=10, pady=4)

    def _tab_camera(self, nb):
        t = tk.Frame(nb, bg=BG_MID)
        nb.add(t, text=" CAMERAS ")
        tk.Label(t, text="Camera Source", bg=BG_MID, fg=ACCENT,
                 font=("Courier New",10,"bold")).pack(anchor="w", padx=10, pady=(10,4))
        sources = self.cfg.get("camera_sources", ["0"])
        v = tk.StringVar(value=sources[0] if sources else "0")
        self._cam_v = v
        f = tk.Frame(t, bg=BG_MID); f.pack(fill="x", padx=10, pady=3)
        tk.Label(f, text="Source (0/1 or rtsp://)", width=22, anchor="w",
                 bg=BG_MID, fg=TEXT_DIM, font=("Courier New",9)).pack(side="left")
        tk.Entry(f, textvariable=v, bg=BG_CARD, fg=TEXT_MAIN,
                 font=("Courier New",9), relief="flat",
                 insertbackground=ACCENT).pack(side="left", fill="x", expand=True)
        tk.Label(t, text="RTSP example: rtsp://admin:pass@192.168.1.64/stream",
                 bg=BG_MID, fg=TEXT_DIM, font=("Courier New",8)).pack(anchor="w", padx=10)
        tk.Label(t, text="Crowd Threshold", bg=BG_MID, fg=ACCENT,
                 font=("Courier New",10,"bold")).pack(anchor="w", padx=10, pady=(12,4))
        self._field(t, "Alert when crowd >", "crowd_threshold")

    def _tab_system(self, nb):
        t = tk.Frame(nb, bg=BG_MID)
        nb.add(t, text=" SYSTEM ")
        tk.Label(t, text="Web Dashboard", bg=BG_MID, fg=ACCENT,
                 font=("Courier New",10,"bold")).pack(anchor="w", padx=10, pady=(10,4))
        self._field(t, "Web Port",     "web_port")
        self._field(t, "Web Password", "web_password")
        tk.Label(t, text="Face Recognition DB", bg=BG_MID, fg=ACCENT,
                 font=("Courier New",10,"bold")).pack(anchor="w", padx=10, pady=(12,4))
        tk.Label(t, text=f"Add photos to:\n{KNOWN_DIR}\nFilename = person's name (e.g. John.jpg)",
                 bg=BG_MID, fg=TEXT_DIM, font=("Courier New",9), justify="left").pack(anchor="w", padx=10)
        tk.Button(t, text="Open known_faces folder",
                  command=lambda: subprocess.Popen(["open", str(KNOWN_DIR)]),
                  bg=BG_CARD, fg=ACCENT, font=("Courier New",9),
                  relief="flat", cursor="hand2").pack(anchor="w", padx=10, pady=6)
        tk.Button(t, text="Reload Face Database",
                  command=lambda: (self.engine.face_rec.reload(),
                                   tk.messagebox.showinfo("Done","Face database reloaded")),
                  bg=BG_CARD, fg=ACCENT, font=("Courier New",9),
                  relief="flat", cursor="hand2").pack(anchor="w", padx=10)
        tk.Label(t, text="Recordings", bg=BG_MID, fg=ACCENT,
                 font=("Courier New",10,"bold")).pack(anchor="w", padx=10, pady=(12,4))
        tk.Button(t, text="Open recordings folder",
                  command=lambda: subprocess.Popen(["open", str(RECORDINGS_DIR)]),
                  bg=BG_CARD, fg=ACCENT, font=("Courier New",9),
                  relief="flat", cursor="hand2").pack(anchor="w", padx=10)

    def _save(self):
        for key in DEFAULT_CONFIG:
            attr = f"_v_{key}"
            if hasattr(self, attr):
                val = getattr(self, attr).get()
                try: val = int(val)
                except: pass
                self.cfg[key] = val
        if hasattr(self, "_cam_v"):
            self.cfg["camera_sources"] = [self._cam_v.get()]
        save_config(self.cfg)
        self.engine.notifier = Notifier(self.cfg)
        tk.messagebox.showinfo("Saved", "Settings saved.\nRestart camera feed to apply camera changes.")
        self.win.destroy()


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN GUI
# ═════════════════════════════════════════════════════════════════════════════
class GarudApp:
    def __init__(self):
        self.cfg    = load_config()
        self.db     = Database()
        self.eq     = queue.Queue(maxsize=8)
        self.engine = GarudEngine(self.cfg, self.db)
        self.live   = False

        start_web_server(self.engine, self.db, self.cfg)

        self.root = tk.Tk()
        self.root.title(f"GARUD Cognitive Surveillance System {VERSION}")
        self.root.configure(bg=BG_DARK)
        self.root.geometry("1440x880")
        self.root.minsize(1100, 720)

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(16, self._poll)

    def _build_ui(self):
        self.root.grid_rowconfigure(1, weight=1)
        self.root.grid_columnconfigure(0, weight=1)

        # Top bar
        top = tk.Frame(self.root, bg=BG_CARD, height=52)
        top.grid(row=0, column=0, sticky="ew")
        top.grid_propagate(False)
        tk.Label(top, text="⬡  GARUD", font=("Courier New",20,"bold"),
                 fg=ACCENT, bg=BG_CARD).pack(side="left", padx=20, pady=10)
        tk.Label(top, text=f"Cognitive Surveillance System  •  {VERSION}",
                 font=("Courier New",11), fg=TEXT_DIM, bg=BG_CARD).pack(side="left")
        port = self.cfg.get("web_port", 8080)
        tk.Label(top, text=f"🌐  http://localhost:{port}",
                 font=("Courier New",10), fg=BLUE, bg=BG_CARD).pack(side="right", padx=20)
        self.status_lbl = tk.Label(top, text="● OFFLINE",
                 font=("Courier New",11,"bold"), fg=DANGER, bg=BG_CARD)
        self.status_lbl.pack(side="right", padx=10)

        # Stats bar
        self._build_stats_bar()

        # Content
        content = tk.Frame(self.root, bg=BG_DARK)
        content.grid(row=1, column=0, sticky="nsew", padx=8, pady=6)
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1)
        content.grid_columnconfigure(1, minsize=320, weight=0)

        # Video
        vf = tk.Frame(content, bg="black")
        vf.grid(row=0, column=0, sticky="nsew", padx=(0,6))
        self.video_lbl = tk.Label(vf, bg="black",
            text="[ CAMERA FEED INACTIVE ]\n\nClick  ▶ START FEED  to begin",
            font=("Courier New",13), fg=TEXT_DIM, width=1, height=1)
        self.video_lbl.pack(fill="both", expand=True)

        # Right panel
        right = tk.Frame(content, bg=BG_MID, width=320)
        right.grid(row=0, column=1, sticky="ns")
        right.grid_propagate(False)
        canvas = tk.Canvas(right, bg=BG_MID, highlightthickness=0, width=318)
        sb = tk.Scrollbar(right, orient="vertical", command=canvas.yview)
        self.panel = tk.Frame(canvas, bg=BG_MID)
        self.panel.bind("<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0,0), window=self.panel, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        canvas.bind_all("<MouseWheel>",
            lambda e: canvas.yview_scroll(int(-1*(e.delta/120)),"units"))
        self._build_panel(self.panel)

    def _build_stats_bar(self):
        bar = tk.Frame(self.root, bg=BG_CARD, height=44)
        bar.grid(row=2, column=0, sticky="ew")
        bar.grid_propagate(False)
        self.sv = {}
        for k,lbl,col in [
            ("fps","FPS",ACCENT),("objects","OBJECTS",ACCENT),
            ("faces","FACES",ACCENT),("crowd","CROWD",WARNING),
            ("threats","THREATS",DANGER),("alerts","ALERTS",DANGER),
        ]:
            f = tk.Frame(bar, bg=BG_CARD); f.pack(side="left", padx=18, pady=5)
            tk.Label(f, text=lbl, bg=BG_CARD, fg=TEXT_DIM,
                     font=("Courier New",8)).pack()
            v = tk.StringVar(value="—"); self.sv[k] = v
            tk.Label(f, textvariable=v, bg=BG_CARD, fg=col,
                     font=("Courier New",13,"bold")).pack()
        # Recording indicator in stats bar
        self.rec_var = tk.StringVar(value="")
        tk.Label(bar, textvariable=self.rec_var, bg=BG_CARD, fg=DANGER,
                 font=("Courier New",10,"bold")).pack(side="right", padx=20)

    def _build_panel(self, p):
        pad = dict(padx=12, pady=5)

        # Camera
        self._sec(p, "CAMERA")
        self.cam_btn = tk.Button(p, text="▶  START FEED", command=self._toggle,
            bg=ACCENT, fg=BG_DARK, font=("Courier New",11,"bold"),
            relief="flat", cursor="hand2", activebackground="#00DDA0")
        self.cam_btn.pack(fill="x", **pad)
        f = tk.Frame(p, bg=BG_MID); f.pack(fill="x", padx=12)
        tk.Label(f, text="Source:", bg=BG_MID, fg=TEXT_DIM,
                 font=("Courier New",9)).pack(side="left")
        self.cam_src = tk.StringVar(value=self.cfg.get("camera_sources",["0"])[0])
        tk.Entry(f, textvariable=self.cam_src, bg=BG_CARD, fg=TEXT_MAIN,
                 font=("Courier New",9), width=22,
                 relief="flat", insertbackground=ACCENT).pack(side="left", padx=6)

        # Features
        self._sec(p, "DETECTION")
        self._chk(p, "Motion Trails",       "show_tracks")
        self._chk(p, "Face Detection",      "show_faces")
        self._chk(p, "Face Recognition",    "face_recog")
        self._chk(p, "Pose & Skeleton",     "show_pose")
        self._chk(p, "Crowd Heatmap",       "show_heatmap", False)
        self._chk(p, "Anomaly / Speed",     "show_anomaly")
        self._chk(p, "Privacy Blur",        "face_blur",    False)

        # Alarm
        self._sec(p, "ALARM & RECORDING")
        tk.Button(p, text="🔔  TEST ALARM + RECORDING",
                  command=self._test_alarm,
                  bg="#2A1020", fg=DANGER, font=("Courier New",10,"bold"),
                  relief="flat", cursor="hand2").pack(fill="x", **pad)
        tk.Button(p, text="✕  CLEAR ALARM",
                  command=self.engine.alarm.clear,
                  bg=BG_CARD, fg=TEXT_DIM, font=("Courier New",9),
                  relief="flat", cursor="hand2").pack(fill="x", padx=12, pady=2)
        tk.Button(p, text="📁  Open Recordings Folder",
                  command=lambda: subprocess.Popen(["open", str(RECORDINGS_DIR)]),
                  bg=BG_CARD, fg=BLUE, font=("Courier New",9),
                  relief="flat", cursor="hand2").pack(fill="x", padx=12, pady=2)

        # Alerts
        self._sec(p, "LIVE ALERTS")
        af = tk.Frame(p, bg=BG_CARD, height=190)
        af.pack(fill="x", padx=12, pady=4)
        af.pack_propagate(False)
        self.alert_box = tk.Text(af, bg=BG_CARD, fg=TEXT_MAIN,
            font=("Courier New",8), relief="flat", state="disabled", wrap="word")
        asb = tk.Scrollbar(af, command=self.alert_box.yview)
        self.alert_box.configure(yscrollcommand=asb.set)
        asb.pack(side="right", fill="y")
        self.alert_box.pack(fill="both", expand=True, padx=4, pady=4)
        self.alert_box.tag_config("threat",  foreground=DANGER)
        self.alert_box.tag_config("anomaly", foreground=WARNING)
        self.alert_box.tag_config("crowd",   foreground=ACCENT)
        self.alert_box.tag_config("face",    foreground=BLUE)
        tk.Button(p, text="Clear", command=self._clear_alerts,
            bg=BG_CARD, fg=TEXT_DIM, font=("Courier New",9),
            relief="flat", cursor="hand2").pack(fill="x", padx=12, pady=2)

        # Settings
        self._sec(p, "CONFIGURE")
        tk.Button(p, text="⚙  Settings (SMS/Email/Camera)",
                  command=lambda: SettingsWindow(self.root, self.cfg, self.engine),
                  bg=BG_CARD, fg=ACCENT, font=("Courier New",9),
                  relief="flat", cursor="hand2").pack(fill="x", **pad)

        # System info
        self._sec(p, "SYSTEM")
        import sys as _sys
        for line in [
            f"Python  : {_sys.version.split()[0]}",
            f"OpenCV  : {cv2.__version__}",
            f"YOLO    : {'✓' if YOLO_OK else '✗ demo'}",
            f"MPS/M4  : {'✓' if MPS else '✗'}",
            f"Twilio  : {'✓' if TWILIO_OK else '✗ pip install twilio'}",
            f"Flask   : {'✓' if FLASK_OK else '✗'}",
            f"Rec dir : {RECORDINGS_DIR.name}/",
            f"Faces   : {len(list(KNOWN_DIR.glob('*.jpg')))} loaded",
        ]:
            tk.Label(p, text=line, bg=BG_MID, fg=TEXT_DIM,
                font=("Courier New",8), anchor="w").pack(fill="x", padx=14, pady=1)

    def _sec(self, p, t):
        tk.Label(p, text=f"  {t}", bg=BG_MID, fg=ACCENT,
                 font=("Courier New",9,"bold")).pack(fill="x", pady=(10,2))
        tk.Frame(p, bg=ACCENT, height=1).pack(fill="x", padx=12)

    def _chk(self, p, label, attr, default=True):
        v = tk.BooleanVar(value=default)
        setattr(self.engine, attr, default)
        tk.Checkbutton(p, text=label, variable=v,
            command=lambda: setattr(self.engine, attr, v.get()),
            bg=BG_MID, fg=TEXT_MAIN, selectcolor=BG_CARD,
            activebackground=BG_MID, activeforeground=ACCENT,
            font=("Courier New",9), cursor="hand2").pack(anchor="w", padx=14, pady=1)

    def _toggle(self):
        if not self.live:
            self.live = True
            self.cam_btn.configure(text="■  STOP FEED", bg=DANGER,
                activebackground="#CC2244", fg="white")
            self.status_lbl.configure(text="● LIVE", fg=ACCENT)
            src = self.cam_src.get()
            self.cfg["camera_sources"] = [src]
            self.engine.start(src, self.eq)
        else:
            self.live = False
            self.engine.stop()
            self.cam_btn.configure(text="▶  START FEED", bg=ACCENT,
                activebackground="#00DDA0", fg=BG_DARK)
            self.status_lbl.configure(text="● OFFLINE", fg=DANGER)
            self.video_lbl.configure(image="",
                text="[ CAMERA FEED INACTIVE ]\n\nClick  ▶ START FEED  to begin")

    def _test_alarm(self):
        self.engine.alarm.trigger(4.0)
        self.engine.recorder.trigger()
        self.engine.notifier.notify("TEST ALERT — alarm and recording triggered","threat","")

    def _poll(self):
        try:
            while True:
                msg = self.eq.get_nowait()
                if msg["type"] == "frame":
                    self._show(msg["frame"])
                    s = msg["stats"]
                    for k,v in s.items():
                        if k in self.sv: self.sv[k].set(str(v))
                    self.rec_var.set("● REC" if s.get("recording") else "")
                elif msg["type"] == "alert":
                    e = msg["entry"]
                    self.alert_box.configure(state="normal")
                    self.alert_box.insert("1.0",
                        f"[{e['time']}] {e['msg']}\n", e.get("kind",""))
                    self.alert_box.configure(state="disabled")
                elif msg["type"] == "error":
                    tk.messagebox.showerror("GARUD", msg["msg"])
        except queue.Empty:
            pass
        self.root.after(16, self._poll)

    def _show(self, frame):
        if not PIL_OK: return
        pw = self.video_lbl.master.winfo_width()
        ph = self.video_lbl.master.winfo_height()
        if pw < 10: pw, ph = 900, 540
        fh, fw = frame.shape[:2]
        scale  = min(pw/fw, ph/fh)
        nw, nh = int(fw*scale), int(fh*scale)
        rgb = cv2.cvtColor(cv2.resize(frame,(nw,nh)), cv2.COLOR_BGR2RGB)
        imgtk = ImageTk.PhotoImage(image=Image.fromarray(rgb))
        self.video_lbl.imgtk = imgtk
        self.video_lbl.configure(image=imgtk, text="", width=nw, height=nh)

    def _clear_alerts(self):
        self.alert_box.configure(state="normal")
        self.alert_box.delete("1.0","end")
        self.alert_box.configure(state="disabled")

    def _close(self):
        self.engine.stop()
        self.root.destroy()

    def run(self):
        port = self.cfg.get("web_port",8080)
        print("\n" + "="*55)
        print(f"  GARUD {VERSION} — Cognitive Surveillance System")
        print("="*55)
        print(f"  Desktop app      : running")
        print(f"  Web dashboard    : http://localhost:{port}")
        print(f"  Login            : admin / garud2024")
        print(f"  YOLO             : {'ready' if YOLO_OK else 'demo mode'}")
        print(f"  Apple MPS        : {'✓' if MPS else '–'}")
        print(f"  Recordings saved : {RECORDINGS_DIR}")
        print(f"  Known faces      : {KNOWN_DIR}")
        print("="*55 + "\n")
        self.root.mainloop()


if __name__ == "__main__":
    GarudApp().run()
