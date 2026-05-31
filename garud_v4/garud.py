#!/usr/bin/env python3
"""
GARUD v4 — Cognitive Surveillance System
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
New in v4:
  1. Loitering detection   — flags persons idle > threshold seconds
  2. Push notifications    — Firebase / ntfy.sh (no app needed)
  3. Daily PDF reports     — auto-generated every 24h
  4. Continuous recording  — 24/7 segmented video archive
  5. Multi-camera support  — up to 4 simultaneous feeds
"""

import cv2, numpy as np, tkinter as tk, tkinter.ttk as ttk
import threading, time, math, queue, os, json, subprocess
import platform, smtplib, sqlite3, hashlib, secrets, shutil
from datetime import datetime, timedelta
from collections import defaultdict, deque
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import urllib.request, urllib.parse
import http.client

# ── Optional imports ──────────────────────────────────────────────────────────
try:
    from flask import (Flask, Response, render_template_string,
                       jsonify, request, session, redirect, send_from_directory)
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
    MPS=False; DEVICE="cpu"

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

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                     Table, TableStyle, HRFlowable)
    from reportlab.lib.units import cm
    PDF_OK = True
except ImportError:
    PDF_OK = False

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR        = Path(__file__).parent
RECORDINGS_DIR  = BASE_DIR / "recordings"
CONTINUOUS_DIR  = BASE_DIR / "continuous"
KNOWN_DIR       = BASE_DIR / "known_faces"
LOGS_DIR        = BASE_DIR / "logs"
REPORTS_DIR     = BASE_DIR / "reports"
DB_PATH         = BASE_DIR / "garud.db"
CONFIG_PATH     = BASE_DIR / "config.json"
for d in [RECORDINGS_DIR,CONTINUOUS_DIR,KNOWN_DIR,LOGS_DIR,REPORTS_DIR]:
    d.mkdir(exist_ok=True)

# ── Constants ─────────────────────────────────────────────────────────────────
VERSION        = "v4.0"
BG_DARK        = "#0A0E1A"
BG_MID         = "#111827"
BG_CARD        = "#1A2035"
ACCENT         = "#00FFB2"
DANGER         = "#FF3B5C"
WARNING        = "#FFB800"
TEXT_MAIN      = "#E8EAF0"
TEXT_DIM       = "#6B7A99"
BLUE           = "#4488FF"
ORANGE         = "#FF8C00"

WEAPON_CLASSES  = {"knife","scissors","baseball bat","bottle","fork","spoon"}
VEHICLE_CLASSES = {"car","truck","bus","motorcycle","bicycle"}
PERSON_CLASS    = "person"

KP_L_SHOULDER=5; KP_R_SHOULDER=6
KP_L_WRIST=9;    KP_R_WRIST=10
KP_L_HIP=11;     KP_R_HIP=12
KP_L_KNEE=13;    KP_R_KNEE=14

MAX_CAMERAS = 4

def ts(fmt="%H:%M:%S"):    return datetime.now().strftime(fmt)
def ts_file():             return datetime.now().strftime("%Y%m%d_%H%M%S")
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
#  DATABASE
# ═════════════════════════════════════════════════════════════════════════════
class Database:
    def __init__(self):
        self.conn  = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        self._lock = threading.Lock()
        self._init()

    def _init(self):
        with self._lock:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    time     TEXT,
                    kind     TEXT,
                    msg      TEXT,
                    clip     TEXT,
                    camera   TEXT
                );
                CREATE TABLE IF NOT EXISTS loiter_events (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    time       TEXT,
                    duration_s REAL,
                    camera     TEXT
                );
                CREATE TABLE IF NOT EXISTS users (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE,
                    pw_hash  TEXT,
                    role     TEXT DEFAULT 'operator'
                );
            """)
            c = self.conn.cursor()
            c.execute("SELECT COUNT(*) FROM users")
            if c.fetchone()[0] == 0:
                ph = hashlib.sha256("garud2024".encode()).hexdigest()
                self.conn.execute(
                    "INSERT INTO users(username,pw_hash,role) VALUES(?,?,?)",
                    ("admin",ph,"admin"))
            self.conn.commit()

    def log_alert(self, kind, msg, clip="", camera="cam0"):
        with self._lock:
            self.conn.execute(
                "INSERT INTO alerts(time,kind,msg,clip,camera) VALUES(?,?,?,?,?)",
                (ts("%Y-%m-%d %H:%M:%S"),kind,msg,clip,camera))
            self.conn.commit()

    def log_loiter(self, duration_s, camera="cam0"):
        with self._lock:
            self.conn.execute(
                "INSERT INTO loiter_events(time,duration_s,camera) VALUES(?,?,?)",
                (ts("%Y-%m-%d %H:%M:%S"),duration_s,camera))
            self.conn.commit()

    def get_alerts(self, limit=200, since_hours=None):
        with self._lock:
            c = self.conn.cursor()
            if since_hours:
                cutoff = (datetime.now()-timedelta(hours=since_hours)).strftime("%Y-%m-%d %H:%M:%S")
                c.execute("SELECT time,kind,msg,clip,camera FROM alerts WHERE time>? ORDER BY id DESC LIMIT ?",(cutoff,limit))
            else:
                c.execute("SELECT time,kind,msg,clip,camera FROM alerts ORDER BY id DESC LIMIT ?",(limit,))
            rows = c.fetchall()
        return [{"time":r[0],"kind":r[1],"msg":r[2],"clip":r[3],"camera":r[4]} for r in rows]

    def get_stats_today(self):
        with self._lock:
            c = self.conn.cursor()
            today = datetime.now().strftime("%Y-%m-%d")
            c.execute("SELECT kind,COUNT(*) FROM alerts WHERE time LIKE ? GROUP BY kind",(f"{today}%",))
            rows = c.fetchall()
            c.execute("SELECT COUNT(*),AVG(duration_s) FROM loiter_events WHERE time LIKE ?",(f"{today}%",))
            loiter = c.fetchone()
        stats = {r[0]:r[1] for r in rows}
        stats["loiter_count"]   = loiter[0] or 0
        stats["loiter_avg_sec"] = round(loiter[1] or 0, 1)
        return stats

    def check_user(self, username, password):
        ph = hashlib.sha256(password.encode()).hexdigest()
        with self._lock:
            c = self.conn.cursor()
            c.execute("SELECT role FROM users WHERE username=? AND pw_hash=?",(username,ph))
            row = c.fetchone()
        return row[0] if row else None


# ═════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ═════════════════════════════════════════════════════════════════════════════
DEFAULT_CONFIG = {
    "twilio_sid":"", "twilio_token":"", "twilio_from":"", "alert_phone":"",
    "smtp_host":"smtp.gmail.com", "smtp_port":587,
    "smtp_user":"", "smtp_pass":"", "alert_email":"",
    "ntfy_topic":"",                    # ntfy.sh push topic (free, no app needed)
    "camera_sources":["0","","",""],    # up to 4 sources
    "clip_seconds":15,
    "crowd_threshold":8,
    "loiter_threshold":20,              # seconds before loitering alert
    "continuous_segment_min":10,        # minutes per continuous recording segment
    "continuous_enabled": False,
    "web_port":8080,
    "web_password":"garud2024",
    "report_hour":6,                    # hour to auto-generate daily report (6am)
}

def load_config():
    if CONFIG_PATH.exists():
        try:
            cfg=json.load(open(CONFIG_PATH))
            for k,v in DEFAULT_CONFIG.items(): cfg.setdefault(k,v)
            return cfg
        except: pass
    save_config(DEFAULT_CONFIG); return dict(DEFAULT_CONFIG)

def save_config(cfg):
    json.dump(cfg, open(CONFIG_PATH,"w"), indent=2)


# ═════════════════════════════════════════════════════════════════════════════
#  1. LOITERING DETECTOR
# ═════════════════════════════════════════════════════════════════════════════
class LoiteringDetector:
    """
    Tracks how long each person stays nearly stationary in the frame.
    Raises loitering alert after threshold seconds.
    Uses a zone map — person must stay within a radius to count as loitering.
    """
    def __init__(self, threshold_sec=20):
        self.threshold   = threshold_sec
        self.first_seen  = {}    # oid -> timestamp first seen stationary
        self.position    = {}    # oid -> (cx,cy) last position
        self.alerted     = set() # oids already alerted this session

    def update(self, tracked):
        """Returns list of (oid, bbox, duration_sec) for loiterers."""
        loiterers = []
        now = time.time()
        active_oids = set()

        for oid, det in tracked.items():
            if det[4] != PERSON_CLASS: continue
            x1,y1,x2,y2 = det[:4]
            cx,cy = (x1+x2)//2,(y1+y2)//2
            active_oids.add(oid)

            if oid in self.position:
                px,py = self.position[oid]
                dist  = math.sqrt((cx-px)**2+(cy-py)**2)
                # Person barely moved (< 30px) — still loitering
                if dist < 30:
                    if oid not in self.first_seen:
                        self.first_seen[oid] = now
                    duration = now - self.first_seen[oid]
                    if duration >= self.threshold:
                        loiterers.append((oid,(x1,y1,x2,y2),duration))
                else:
                    # Person moved — reset timer
                    self.first_seen.pop(oid, None)
                    self.alerted.discard(oid)
            self.position[oid] = (cx,cy)

        # Clean up gone persons
        for oid in list(self.first_seen.keys()):
            if oid not in active_oids:
                self.first_seen.pop(oid,None)
                self.position.pop(oid,None)
                self.alerted.discard(oid)

        return loiterers

    def should_alert(self, oid):
        """Returns True only once per loitering episode."""
        if oid not in self.alerted:
            self.alerted.add(oid)
            return True
        return False

    def set_threshold(self, sec):
        self.threshold = sec


# ═════════════════════════════════════════════════════════════════════════════
#  2. PUSH NOTIFICATIONS  (ntfy.sh — free, no app install needed)
# ═════════════════════════════════════════════════════════════════════════════
class PushNotifier:
    """
    Uses ntfy.sh — completely free push notifications.
    User subscribes to their topic at https://ntfy.sh/YOUR_TOPIC
    OR installs the ntfy app (iOS/Android) and subscribes.
    No account needed.
    """
    def __init__(self, cfg):
        self.cfg   = cfg
        self._last = {}

    def push(self, title, msg, priority="high", tags="warning"):
        topic = self.cfg.get("ntfy_topic","").strip()
        if not topic: return
        key = f"{title}"
        now = time.time()
        if now - self._last.get(key,0) < 30: return
        self._last[key] = now
        threading.Thread(target=self._send,
                         args=(topic,title,msg,priority,tags), daemon=True).start()

    def _send(self, topic, title, msg, priority, tags):
        try:
            data = msg.encode("utf-8")
            req  = urllib.request.Request(
                f"https://ntfy.sh/{topic}",
                data=data,
                headers={
                    "Title":    title,
                    "Priority": priority,
                    "Tags":     tags,
                    "Content-Type": "text/plain",
                }
            )
            urllib.request.urlopen(req, timeout=5)
            print(f"[PUSH] Sent: {title}")
        except Exception as e:
            print(f"[PUSH] Failed: {e}")


# ═════════════════════════════════════════════════════════════════════════════
#  3. PDF REPORT GENERATOR
# ═════════════════════════════════════════════════════════════════════════════
class ReportGenerator:
    def __init__(self, db, cfg):
        self.db  = db
        self.cfg = cfg
        self._last_report_date = None
        self._running = True
        threading.Thread(target=self._scheduler, daemon=True).start()

    def _scheduler(self):
        """Checks every minute if it's time to generate the daily report."""
        while self._running:
            now = datetime.now()
            target_hour = self.cfg.get("report_hour", 6)
            if (now.hour == target_hour and now.minute == 0
                    and self._last_report_date != now.date()):
                self.generate(now.date() - timedelta(days=1))
                self._last_report_date = now.date()
            time.sleep(60)

    def generate(self, report_date=None):
        """Generate PDF report. Returns path to saved PDF."""
        if report_date is None:
            report_date = datetime.now().date()

        fname  = REPORTS_DIR / f"GARUD_Report_{report_date}.pdf"

        if not PDF_OK:
            # Fallback: plain text report
            return self._generate_txt(report_date, fname.with_suffix(".txt"))

        try:
            return self._generate_pdf(report_date, fname)
        except Exception as e:
            print(f"[REPORT] PDF failed ({e}), generating text report")
            return self._generate_txt(report_date, fname.with_suffix(".txt"))

    def _generate_pdf(self, report_date, fname):
        alerts  = self.db.get_alerts(limit=500, since_hours=24)
        stats   = self.db.get_stats_today()
        clips   = list(RECORDINGS_DIR.glob("*.mp4"))

        doc = SimpleDocTemplate(str(fname), pagesize=A4,
                                 topMargin=2*cm, bottomMargin=2*cm,
                                 leftMargin=2*cm, rightMargin=2*cm)
        styles = getSampleStyleSheet()

        # Custom styles
        title_style = ParagraphStyle("title", parent=styles["Title"],
                                      fontSize=22, textColor=colors.HexColor("#0A0E1A"),
                                      spaceAfter=6)
        h2_style    = ParagraphStyle("h2", parent=styles["Heading2"],
                                      fontSize=13, textColor=colors.HexColor("#00916E"),
                                      spaceBefore=14, spaceAfter=6)
        body_style  = ParagraphStyle("body", parent=styles["Normal"],
                                      fontSize=9, leading=14)
        dim_style   = ParagraphStyle("dim", parent=styles["Normal"],
                                      fontSize=8, textColor=colors.grey)

        story = []

        # Header
        story.append(Paragraph("⬡  GARUD Surveillance System", title_style))
        story.append(Paragraph(f"Daily Security Report — {report_date}", styles["Heading3"]))
        story.append(Paragraph(f"Generated: {ts('%Y-%m-%d %H:%M:%S')}  |  System: {VERSION}",dim_style))
        story.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor("#00FFB2")))
        story.append(Spacer(1,0.4*cm))

        # Summary stats table
        story.append(Paragraph("SUMMARY", h2_style))
        total   = sum(v for k,v in stats.items() if k not in ("loiter_count","loiter_avg_sec"))
        summary_data = [
            ["Metric","Count","Status"],
            ["Total Alerts",      str(total),                "—"],
            ["Threat Events",     str(stats.get("threat",0)),
             "⚠ HIGH" if stats.get("threat",0)>0 else "✓ CLEAR"],
            ["Anomaly Events",    str(stats.get("anomaly",0)), "—"],
            ["Crowd Alerts",      str(stats.get("crowd",0)),  "—"],
            ["Loitering Events",  str(stats.get("loiter_count",0)),"—"],
            ["Accident Alerts",   str(stats.get("accident",0)),"—"],
            ["Video Clips Saved", str(len(clips)),            "—"],
        ]
        t = Table(summary_data, colWidths=[8*cm,4*cm,5*cm])
        t.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#0A0E1A")),
            ("TEXTCOLOR",  (0,0),(-1,0),colors.white),
            ("FONTNAME",   (0,0),(-1,0),"Helvetica-Bold"),
            ("FONTSIZE",   (0,0),(-1,0),10),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#F5F5F5")]),
            ("FONTSIZE",   (0,1),(-1,-1),9),
            ("GRID",       (0,0),(-1,-1),0.5,colors.lightgrey),
            ("VALIGN",     (0,0),(-1,-1),"MIDDLE"),
            ("TOPPADDING", (0,0),(-1,-1),5),
            ("BOTTOMPADDING",(0,0),(-1,-1),5),
        ]))
        story.append(t)
        story.append(Spacer(1,0.5*cm))

        # Alert log table
        story.append(Paragraph("FULL ALERT LOG", h2_style))
        if alerts:
            alert_data = [["Time","Type","Description","Camera"]]
            for a in alerts[:80]:
                kind_color = {"threat":"⚠","accident":"🚨","crowd":"👥",
                              "loiter":"🕐","anomaly":"⚡"}.get(a["kind"],"•")
                alert_data.append([
                    a["time"], f"{kind_color} {a['kind'].upper()}",
                    a["msg"][:60]+"…" if len(a["msg"])>60 else a["msg"],
                    a["camera"]
                ])
            at = Table(alert_data, colWidths=[3.5*cm,3*cm,9*cm,2*cm])
            at.setStyle(TableStyle([
                ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#111827")),
                ("TEXTCOLOR",  (0,0),(-1,0),colors.white),
                ("FONTNAME",   (0,0),(-1,0),"Helvetica-Bold"),
                ("FONTSIZE",   (0,0),(-1,-1),7.5),
                ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#F9F9F9")]),
                ("GRID",       (0,0),(-1,-1),0.3,colors.lightgrey),
                ("VALIGN",     (0,0),(-1,-1),"MIDDLE"),
                ("TOPPADDING", (0,0),(-1,-1),3),
                ("BOTTOMPADDING",(0,0),(-1,-1),3),
            ]))
            story.append(at)
        else:
            story.append(Paragraph("No alerts recorded in this period.", body_style))

        # Footer
        story.append(Spacer(1,1*cm))
        story.append(HRFlowable(width="100%",thickness=1,color=colors.lightgrey))
        story.append(Paragraph(
            f"GARUD Cognitive Surveillance System {VERSION}  |  Confidential",
            dim_style))

        doc.build(story)
        print(f"[REPORT] PDF saved → {fname}")
        return str(fname)

    def _generate_txt(self, report_date, fname):
        alerts = self.db.get_alerts(limit=500, since_hours=24)
        stats  = self.db.get_stats_today()
        lines  = [
            f"GARUD DAILY REPORT — {report_date}",
            f"Generated: {ts('%Y-%m-%d %H:%M:%S')}",
            "="*50,
            f"Total Alerts   : {sum(v for k,v in stats.items() if k not in ('loiter_count','loiter_avg_sec'))}",
            f"Threats        : {stats.get('threat',0)}",
            f"Anomalies      : {stats.get('anomaly',0)}",
            f"Crowd Alerts   : {stats.get('crowd',0)}",
            f"Loitering      : {stats.get('loiter_count',0)}",
            f"Accidents      : {stats.get('accident',0)}",
            "="*50,
            "ALERT LOG:",
        ]
        for a in alerts:
            lines.append(f"[{a['time']}] [{a['kind'].upper()}] {a['msg']} (cam:{a['camera']})")
        open(fname,"w").write("\n".join(lines))
        print(f"[REPORT] Text report saved → {fname}")
        return str(fname)

    def stop(self):
        self._running = False


# ═════════════════════════════════════════════════════════════════════════════
#  4. CONTINUOUS RECORDER
# ═════════════════════════════════════════════════════════════════════════════
class ContinuousRecorder:
    """
    Records camera feed 24/7 in segments (default 10 min each).
    Saves to continuous/ folder. Old segments auto-deleted after 48h.
    """
    def __init__(self, cfg, cam_id="cam0"):
        self.cfg        = cfg
        self.cam_id     = cam_id
        self.writer     = None
        self.seg_start  = None
        self.seg_path   = None
        self.enabled    = cfg.get("continuous_enabled", False)
        self.seg_min    = cfg.get("continuous_segment_min", 10)
        self._lock      = threading.Lock()

    def feed(self, frame):
        if not self.enabled: return
        with self._lock:
            now = time.time()
            # Start new segment if needed
            if (self.writer is None or
                    (self.seg_start and now - self.seg_start > self.seg_min * 60)):
                self._new_segment(frame)

            if self.writer:
                try: self.writer.write(frame)
                except: pass

        # Clean old segments (older than 48h)
        self._cleanup()

    def _new_segment(self, frame):
        if self.writer:
            self.writer.release()
        h,w = frame.shape[:2]
        fname = CONTINUOUS_DIR / f"{self.cam_id}_{ts_file()}.mp4"
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        self.writer   = cv2.VideoWriter(str(fname), fourcc, 20, (w,h))
        self.seg_start = time.time()
        self.seg_path  = str(fname)
        print(f"[CONT] New segment → {fname.name}")

    def _cleanup(self):
        cutoff = time.time() - 48*3600
        for f in CONTINUOUS_DIR.glob("*.mp4"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except: pass

    def stop(self):
        with self._lock:
            if self.writer:
                self.writer.release()
                self.writer = None

    def set_enabled(self, val):
        self.enabled = val
        if not val: self.stop()


# ═════════════════════════════════════════════════════════════════════════════
#  ALARM, TRACKER, FIGHT, ACCIDENT (same as v3, carried forward)
# ═════════════════════════════════════════════════════════════════════════════
class AlarmSystem:
    def __init__(self):
        self.flash_until=0; self.last_beep=0
    def trigger(self,duration=4.0):
        self.flash_until=time.time()+duration
        threading.Thread(target=self._beep,daemon=True).start()
    def _beep(self):
        if time.time()-self.last_beep<2: return
        self.last_beep=time.time()
        try:
            if platform.system()=="Darwin":
                for s in ["/System/Library/Sounds/Sosumi.aiff","/System/Library/Sounds/Basso.aiff"]:
                    if os.path.exists(s):
                        subprocess.Popen(["afplay",s],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); return
        except: pass
        print("\a\a",end="",flush=True)
    def should_flash(self): return time.time()<self.flash_until
    def clear(self): self.flash_until=0

class Tracker:
    def __init__(self,max_lost=25):
        self.nid=0;self.objs={};self.lost={}
        self.paths=defaultdict(lambda:deque(maxlen=50));self.max_lost=max_lost
    def _iou(self,a,b):
        ix1,iy1=max(a[0],b[0]),max(a[1],b[1]);ix2,iy2=min(a[2],b[2]),min(a[3],b[3])
        iw,ih=max(0,ix2-ix1),max(0,iy2-iy1);inter=iw*ih
        ua=(a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-inter
        return inter/ua if ua>0 else 0
    def update(self,dets):
        used=set();new_o={}
        for oid,(cx,cy,box) in list(self.objs.items()):
            best,bd=0,-1
            for i,d in enumerate(dets):
                if i in used: continue
                iou=self._iou(box,d[:4])
                if iou>best: best,bd=iou,i
            if best>0.15 and bd>=0:
                x1,y1,x2,y2=dets[bd][:4];ncx,ncy=(x1+x2)//2,(y1+y2)//2
                new_o[oid]=(ncx,ncy,(x1,y1,x2,y2));self.paths[oid].append((ncx,ncy))
                self.lost[oid]=0;used.add(bd)
            else:
                self.lost[oid]=self.lost.get(oid,0)+1
                if self.lost[oid]<=self.max_lost: new_o[oid]=(cx,cy,box)
        for i,d in enumerate(dets):
            if i not in used:
                x1,y1,x2,y2=d[:4];cx,cy=(x1+x2)//2,(y1+y2)//2
                new_o[self.nid]=(cx,cy,(x1,y1,x2,y2))
                self.paths[self.nid].append((cx,cy));self.lost[self.nid]=0;self.nid+=1
        self.objs=new_o
        assigned={}
        for oid,(cx,cy,box) in self.objs.items():
            for d in dets:
                if (d[0],d[1],d[2],d[3])==box: assigned[oid]=d; break
        return assigned

class FightDetector:
    def __init__(self): self.history=defaultdict(lambda:deque(maxlen=10))
    def analyse(self,oid,keypoints):
        kp=keypoints;score=0
        def get(idx):
            if float(kp[idx][2])>0.3: return (float(kp[idx][0]),float(kp[idx][1]),float(kp[idx][2]))
            return None
        ls=get(KP_L_SHOULDER);rs=get(KP_R_SHOULDER);lw=get(KP_L_WRIST);rw=get(KP_R_WRIST)
        lh=get(KP_L_HIP);rh=get(KP_R_HIP)
        if ls is not None and lw is not None:
            if lw[1]<ls[1]: score+=2
        if rs is not None and rw is not None:
            if rw[1]<rs[1]: score+=2
        if all(x is not None for x in [ls,rs,lw,rw]):
            mid_x=(ls[0]+rs[0])/2
            if abs(lw[0]-mid_x)>abs(ls[0]-rs[0])*0.8: score+=1
            if abs(rw[0]-mid_x)>abs(ls[0]-rs[0])*0.8: score+=1
        self.history[oid].append((lw,rw))
        if len(self.history[oid])>=3:
            plw,prw=self.history[oid][-3]
            if lw is not None and plw is not None:
                dx=lw[0]-plw[0];dy=lw[1]-plw[1]
                if math.sqrt(dx*dx+dy*dy)>20: score+=2
            if rw is not None and prw is not None:
                dx=rw[0]-prw[0];dy=rw[1]-prw[1]
                if math.sqrt(dx*dx+dy*dy)>20: score+=2
        return score>=5

class AccidentDetector:
    def __init__(self):
        self.vehicle_history=defaultdict(lambda:deque(maxlen=30))
        self.speed_history=defaultdict(lambda:deque(maxlen=15))
        self.stationary_since={}
    def update(self,tracked):
        events=[]
        vids={oid:d for oid,d in tracked.items() if d[4] in VEHICLE_CLASSES}
        pids={oid:d for oid,d in tracked.items() if d[4]==PERSON_CLASS}
        for oid,det in vids.items():
            x1,y1,x2,y2=det[:4];cx,cy=(x1+x2)//2,(y1+y2)//2
            self.vehicle_history[oid].append((cx,cy))
            hist=list(self.vehicle_history[oid])
            if len(hist)>=3:
                dx=hist[-1][0]-hist[-3][0];dy=hist[-1][1]-hist[-3][1]
                self.speed_history[oid].append(math.sqrt(dx*dx+dy*dy))
            speeds=list(self.speed_history[oid])
            if len(speeds)>=10:
                recent=sum(speeds[-3:])/3;earlier=sum(speeds[-10:-5])/5
                if earlier>12 and recent<2.5:
                    events.append(("SUDDEN STOP",oid,(x1,y1,x2,y2),"HIGH"))
            if len(speeds)>=5 and sum(speeds[-5:])/5<1.5:
                self.stationary_since.setdefault(oid,time.time())
                if time.time()-self.stationary_since.get(oid,time.time())>8:
                    events.append(("STATIONARY VEHICLE",oid,(x1,y1,x2,y2),"MED"))
            else: self.stationary_since.pop(oid,None)
        vlist=list(vids.items())
        for i in range(len(vlist)):
            for j in range(i+1,len(vlist)):
                oa,da=vlist[i];ob,db=vlist[j]
                if self._iou(da[:4],db[:4])>0.15:
                    box=(min(da[0],db[0]),min(da[1],db[1]),max(da[2],db[2]),max(da[3],db[3]))
                    events.append(("VEHICLE COLLISION",oa,box,"CRITICAL"))
        for pid,pdet in pids.items():
            px1,py1,px2,py2=pdet[:4];pcx,pcy=(px1+px2)//2,(py1+py2)//2
            for vid,vdet in vids.items():
                vx1,vy1,vx2,vy2=vdet[:4]
                if (vx1-30<pcx<vx2+30) and (vy1-30<pcy<vy2+30):
                    vs=list(self.speed_history.get(vid,[]))
                    if vs and sum(vs[-3:])/max(len(vs[-3:]),1)<2:
                        events.append(("PERSON DOWN / HIT",pid,(px1,py1,px2,py2),"CRITICAL"))
        return events
    @staticmethod
    def _iou(a,b):
        ix1,iy1=max(a[0],b[0]),max(a[1],b[1]);ix2,iy2=min(a[2],b[2]),min(a[3],b[3])
        iw,ih=max(0,ix2-ix1),max(0,iy2-iy1);inter=iw*ih
        ua=(a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-inter
        return inter/ua if ua>0 else 0

class ClipRecorder:
    def __init__(self,cfg):
        self.cfg=cfg;self.pre_buf=deque(maxlen=150);self.recording=False
        self.post_frames=[];self.post_target=300;self.writer=None;self.clip_path=""
        self._lock=threading.Lock()
    def feed(self,frame):
        with self._lock:
            self.pre_buf.append(frame.copy())
            if self.recording:
                if self.writer and len(self.post_frames)<self.post_target:
                    self.writer.write(frame);self.post_frames.append(1)
                elif self.recording and len(self.post_frames)>=self.post_target:
                    return self._finish()
        return None
    def trigger(self):
        with self._lock:
            if self.recording: return
            self.recording=True;self.post_frames=[]
            fname=f"threat_{ts_file()}.mp4";self.clip_path=str(RECORDINGS_DIR/fname)
            h,w=list(self.pre_buf)[-1].shape[:2] if self.pre_buf else (720,1280)
            self.writer=cv2.VideoWriter(self.clip_path,cv2.VideoWriter_fourcc(*"mp4v"),30,(w,h))
            for f in list(self.pre_buf): self.writer.write(f)
    def _finish(self):
        if self.writer: self.writer.release();self.writer=None
        self.recording=False;return self.clip_path
    def is_recording(self): return self.recording

class FaceRecognizer:
    def __init__(self):
        try: self.recognizer=cv2.face.LBPHFaceRecognizer_create();self.recog_ok=True
        except: self.recognizer=None;self.recog_ok=False
        self.face_cascade=cv2.CascadeClassifier(cv2.data.haarcascades+"haarcascade_frontalface_default.xml")
        self.label_map={};self.trained=False;self._load_known()
    def _load_known(self):
        if not self.recog_ok: return
        images,labels,lid=[],[],0
        for p in KNOWN_DIR.glob("*.jpg"):
            img=cv2.imread(str(p),cv2.IMREAD_GRAYSCALE)
            if img is None: continue
            faces=self.face_cascade.detectMultiScale(img,1.1,5,minSize=(40,40))
            for (x,y,w,h) in faces: images.append(img[y:y+h,x:x+w]);labels.append(lid)
            self.label_map[lid]=p.stem;lid+=1
        if images: self.recognizer.train(images,np.array(labels));self.trained=True
    def reload(self): self.label_map={};self.trained=False;self._load_known()
    def identify(self,frame):
        results=[];gray=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)
        faces=self.face_cascade.detectMultiScale(gray,1.1,5,minSize=(40,40))
        for (x,y,w,h) in faces:
            name="Unknown";conf=0.0
            if self.trained and self.recog_ok:
                try:
                    label,dist=self.recognizer.predict(gray[y:y+h,x:x+w])
                    conf=max(0,100-dist)/100
                    if conf>0.4: name=self.label_map.get(label,"Unknown")
                except: pass
            results.append((x,y,w,h,name,conf))
        return results

class Notifier:
    def __init__(self,cfg):
        self.cfg=cfg;self._last={}
    def notify(self,msg,kind="threat",clip=""):
        key=kind;now=time.time()
        if now-self._last.get(key,0)<30: return
        self._last[key]=now
        threading.Thread(target=self._send_all,args=(msg,clip),daemon=True).start()
    def _send_all(self,msg,clip):
        self._send_sms(msg);self._send_email(msg,clip)
    def _send_sms(self,msg):
        cfg=self.cfg
        if not all([cfg.get("twilio_sid"),cfg.get("twilio_token"),cfg.get("twilio_from"),cfg.get("alert_phone")]): return
        if not TWILIO_OK: return
        try:
            TwilioClient(cfg["twilio_sid"],cfg["twilio_token"]).messages.create(
                body=f"⚠ GARUD [{ts()}]\n{msg}",from_=cfg["twilio_from"],to=cfg["alert_phone"])
        except Exception as e: print(f"[SMS] {e}")
    def _send_email(self,msg,clip=""):
        cfg=self.cfg
        if not all([cfg.get("smtp_user"),cfg.get("smtp_pass"),cfg.get("alert_email")]): return
        try:
            mime=MIMEMultipart();mime["From"]=cfg["smtp_user"];mime["To"]=cfg["alert_email"]
            mime["Subject"]=f"⚠ GARUD Alert — {ts()}"
            mime.attach(MIMEText(f"GARUD Alert\nTime: {ts('%Y-%m-%d %H:%M:%S')}\n{msg}\nClip: {clip or 'N/A'}","plain"))
            with smtplib.SMTP(cfg["smtp_host"],cfg["smtp_port"]) as s:
                s.starttls();s.login(cfg["smtp_user"],cfg["smtp_pass"]);s.send_message(mime)
        except Exception as e: print(f"[EMAIL] {e}")


# ═════════════════════════════════════════════════════════════════════════════
#  5. CAMERA WORKER  — one per camera, runs in own thread
# ═════════════════════════════════════════════════════════════════════════════
class CameraWorker:
    """One instance per camera. Runs detection independently."""
    def __init__(self, cam_id, source, model, pose_model, cfg, db, alarm,
                 push_notifier, notifier, shared_alert_cb):
        self.cam_id       = cam_id
        self.source       = source
        self.model        = model
        self.pose_model   = pose_model
        self.cfg          = cfg
        self.db           = db
        self.alarm        = alarm
        self.push         = push_notifier
        self.notifier     = notifier
        self.alert_cb     = shared_alert_cb  # callback to main engine

        self.tracker      = Tracker()
        self.fight_det    = FightDetector()
        self.accident_det = AccidentDetector()
        self.loiter_det   = LoiteringDetector(cfg.get("loiter_threshold",20))
        self.clip_rec     = ClipRecorder(cfg)
        self.cont_rec     = ContinuousRecorder(cfg, cam_id)
        self.face_rec     = FaceRecognizer()

        self.running      = False
        self.cap          = None
        self.latest_jpg   = None
        self.heatmap_acc  = None
        self.frame_times  = deque(maxlen=30)
        self._last_alert  = {}
        self.stats        = {"fps":0,"objects":0,"faces":0,"crowd":0,
                              "threats":0,"alerts":0,"loiterers":0,"recording":False}

        # toggles (shared by UI)
        self.show_tracks=True; self.show_faces=True; self.show_pose=True
        self.show_heatmap=False; self.show_anomaly=True; self.show_accident=True
        self.show_loiter=True; self.face_blur=False; self.face_recog=True

    def start(self):
        try: src=int(self.source)
        except: src=self.source
        self.cap=cv2.VideoCapture(src)
        if not self.cap.isOpened():
            print(f"[CAM{self.cam_id}] Cannot open: {self.source}"); return False
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH,1280)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT,720)
        self.cap.set(cv2.CAP_PROP_FPS,30)
        self.running=True
        threading.Thread(target=self._loop,daemon=True,name=f"cam_{self.cam_id}").start()
        print(f"[CAM{self.cam_id}] Started → {self.source}")
        return True

    def stop(self):
        self.running=False
        self.clip_rec._finish()
        self.cont_rec.stop()
        if self.cap: self.cap.release()

    def _loop(self):
        while self.running:
            t0=time.time()
            ret,frame=self.cap.read()
            if not ret: time.sleep(0.05); continue

            h,w=frame.shape[:2]
            if self.heatmap_acc is None:
                self.heatmap_acc=np.zeros((h,w),dtype=np.float32)

            # Continuous recording
            self.cont_rec.feed(frame)

            overlay=frame.copy()
            clip_done=self.clip_rec.feed(frame)
            if clip_done: self.stats["recording"]=False

            dets,pose_res=self._detect(frame)
            tracked=self.tracker.update(dets)

            # Heatmap
            crowd=0
            for oid,det in tracked.items():
                cx,cy=(det[0]+det[2])//2,(det[1]+det[3])//2
                cv2.circle(self.heatmap_acc,(cx,cy),50,1.8,-1)
                if det[4]==PERSON_CLASS: crowd+=1
            if self.show_heatmap:
                hm=cv2.normalize(self.heatmap_acc,None,0,255,cv2.NORM_MINMAX)
                hm=cv2.GaussianBlur(hm.astype(np.uint8),(61,61),0)
                hm=cv2.applyColorMap(hm,cv2.COLORMAP_JET)
                overlay=cv2.addWeighted(overlay,0.65,hm,0.35,0)

            # Alarm flash
            if self.alarm.should_flash():
                flash=np.zeros_like(overlay);flash[:]=(0,0,160)
                alpha=0.3+0.12*math.sin(time.time()*10)
                overlay=cv2.addWeighted(overlay,1-alpha,flash,alpha,0)
                cv2.putText(overlay,"⚠  THREAT DETECTED  ⚠",
                    (w//2-230,h//2),cv2.FONT_HERSHEY_SIMPLEX,1.2,(0,50,255),3,cv2.LINE_AA)

            if self.clip_rec.is_recording():
                cv2.circle(overlay,(w-30,30),12,(0,0,255),-1)
                put_label(overlay,"● REC",(w-80,38),(0,80,255),0.55,2)

            # Draw objects
            for oid,det in tracked.items():
                x1,y1,x2,y2,label,conf=det
                color=self._label_color(label)
                if self.show_tracks and oid in self.tracker.paths:
                    path=list(self.tracker.paths[oid])
                    for i in range(1,len(path)):
                        a=i/len(path);c=tuple(int(x*a) for x in color)
                        cv2.line(overlay,path[i-1],path[i],c,2)
                draw_box(overlay,x1,y1,x2,y2,color,f"#{oid} {label} {conf:.0%}")
                if label in WEAPON_CLASSES:
                    cv2.rectangle(overlay,(x1-4,y1-4),(x2+4,y2+4),(0,0,255),3)
                    put_label(overlay,"⚠ WEAPON",(x1,y2+18),(0,50,255),0.6,2)
                    self._alert(f"WEAPON: {label.upper()}","threat",label)
                if self.show_anomaly and oid in self.tracker.paths:
                    pts=list(self.tracker.paths[oid])
                    if len(pts)>=5:
                        dx=pts[-1][0]-pts[-5][0];dy=pts[-1][1]-pts[-5][1]
                        if math.sqrt(dx*dx+dy*dy)>22:
                            put_label(overlay,"⚡ FAST",(x1,y2+18),(0,180,255))
                            self._alert("FAST MOTION","anomaly",f"spd{oid}")

            # ── LOITERING ─────────────────────────────────────────────────
            if self.show_loiter:
                loiterers=self.loiter_det.update(tracked)
                self.stats["loiterers"]=len(loiterers)
                for oid,(lx1,ly1,lx2,ly2),duration in loiterers:
                    # Pulsing amber box
                    pulse=int(128+127*math.sin(time.time()*3))
                    lcolor=(0,pulse,255)
                    cv2.rectangle(overlay,(lx1-4,ly1-4),(lx2+4,ly2+4),lcolor,3)
                    put_label(overlay,f"⏱ LOITERING {duration:.0f}s",
                              (lx1,ly1-22),lcolor,0.65,2)
                    if self.loiter_det.should_alert(oid):
                        self._alert(f"LOITERING DETECTED — {duration:.0f}s in zone",
                                    "loiter",f"loi{oid}")
                        self.db.log_loiter(duration,self.cam_id)
            else:
                self.stats["loiterers"]=0

            # ── POSE / FIGHT ───────────────────────────────────────────────
            if pose_res and self.show_pose:
                self._process_pose(overlay,pose_res)

            # ── ACCIDENT ──────────────────────────────────────────────────
            if self.show_accident:
                for evt,oid,(ex1,ey1,ex2,ey2),sev in self.accident_det.update(tracked):
                    col=(0,0,255) if sev=="CRITICAL" else (0,100,255)
                    cv2.rectangle(overlay,(ex1-6,ey1-6),(ex2+6,ey2+6),col,3)
                    put_label(overlay,f"⚠ {evt}",(ex1,ey1-22),col,0.65,2)
                    self._alert(f"ACCIDENT — {evt} [{sev}]","accident",f"acc{oid}")
                    if sev=="CRITICAL": self.alarm.trigger(5.0)

            # ── FACES ─────────────────────────────────────────────────────
            if self.show_faces:
                face_results=self.face_rec.identify(frame)
                self.stats["faces"]=len(face_results)
                for (fx,fy,fw,fh,name,conf) in face_results:
                    if self.face_blur:
                        roi=overlay[fy:fy+fh,fx:fx+fw]
                        if roi.size>0:
                            overlay[fy:fy+fh,fx:fx+fw]=cv2.GaussianBlur(roi,(55,55),0)
                    else:
                        fc=(0,200,100) if name!="Unknown" else (0,140,255)
                        cv2.rectangle(overlay,(fx,fy),(fx+fw,fy+fh),fc,2)
                        put_label(overlay,f"{name} {conf:.0%}" if name!="Unknown" else "Unknown",(fx,fy-8),fc)
                        if name!="Unknown" and self.face_recog:
                            self._alert(f"KNOWN PERSON: {name}","face",name)
            else:
                self.stats["faces"]=0

            # ── CROWD ─────────────────────────────────────────────────────
            crowd_thresh=self.cfg.get("crowd_threshold",8)
            if crowd>crowd_thresh:
                self._alert(f"HIGH CROWD: {crowd} persons","crowd","crowd")
                banner=np.zeros((38,w,3),dtype=np.uint8);banner[:]=(20,55,0)
                cv2.putText(banner,f"⚠  HIGH CROWD — {crowd} PERSONS",
                    (w//2-200,26),cv2.FONT_HERSHEY_SIMPLEX,0.65,hex2bgr(WARNING),2,cv2.LINE_AA)
                overlay[h-48:h-10]=banner

            # ── HUD ───────────────────────────────────────────────────────
            self._draw_hud(overlay,w,h,crowd)

            # ── Stats ─────────────────────────────────────────────────────
            self.stats.update({"objects":len(tracked),"crowd":crowd,
                                "recording":self.clip_rec.is_recording()})
            elapsed=time.time()-t0
            self.frame_times.append(elapsed)
            self.stats["fps"]=round(1/(sum(self.frame_times)/len(self.frame_times)+1e-9),1)

            _,jpg=cv2.imencode(".jpg",overlay,[cv2.IMWRITE_JPEG_QUALITY,72])
            self.latest_jpg=jpg.tobytes()

    def _detect(self,frame):
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
            except: pass
        # Demo
        t=time.time();h,w=frame.shape[:2]
        ctrs=[(int(w*0.25+70*math.sin(t*0.6)),int(h*0.5+30*math.cos(t*0.4))),
              (int(w*0.6+50*math.cos(t*0.8)),int(h*0.45+40*math.sin(t*0.5)))]
        dets=[]
        for i,(cx,cy) in enumerate(ctrs):
            x1,y1=max(0,cx-30),max(0,cy-70);x2,y2=min(w,cx+30),min(h,cy+70)
            dets.append((x1,y1,x2,y2,"person",0.91))
        return dets,None

    def _process_pose(self,overlay,pose_res):
        try:
            SKEL=[(KP_L_SHOULDER,KP_R_SHOULDER),(5,7),(7,9),(6,8),(8,10),(5,11),(6,12),(11,12),(11,13),(12,14)]
            for r in pose_res:
                if not hasattr(r,"keypoints") or r.keypoints is None: continue
                kps_all=r.keypoints.data.cpu().numpy()
                for pi,kps in enumerate(kps_all):
                    if kps.shape[0]<17: continue
                    for a,b in SKEL:
                        if float(kps[a][2])>0.3 and float(kps[b][2])>0.3:
                            cv2.line(overlay,(int(kps[a][0]),int(kps[a][1])),(int(kps[b][0]),int(kps[b][1])),(0,200,120),2)
                    for kp in kps:
                        if float(kp[2])>0.3: cv2.circle(overlay,(int(kp[0]),int(kp[1])),4,(0,255,178),-1)
                    if self.fight_det.analyse(pi,kps):
                        self._alert("FIGHT / AGGRESSIVE BEHAVIOUR","threat","fight")
        except: pass

    def _draw_hud(self,img,w,h,crowd):
        panel=np.zeros((195,300,3),dtype=np.uint8);panel[:]=(26,32,53)
        cv2.rectangle(panel,(0,0),(299,194),(0,255,178),1)
        ct=self.cfg.get("crowd_threshold",8)
        lines=[
            (f"GARUD {VERSION} | CAM {self.cam_id}",ACCENT,0.55,2),
            (f"FPS    : {self.stats['fps']}",TEXT_MAIN,0.45,1),
            (f"MODE   : {DEVICE.upper()}",ACCENT if MPS else TEXT_DIM,0.45,1),
            (f"OBJECTS: {self.stats['objects']}",TEXT_MAIN,0.45,1),
            (f"FACES  : {self.stats['faces']}",TEXT_MAIN,0.45,1),
            (f"CROWD  : {crowd}",WARNING if crowd>ct else TEXT_MAIN,0.45,1),
            (f"LOITER : {self.stats['loiterers']}",hex2bgr(ORANGE) if self.stats['loiterers']>0 else hex2bgr(TEXT_DIM),0.45,1),
            (f"THREATS: {self.stats['threats']}",hex2bgr(DANGER),0.45,1),
            (f"ALERTS : {self.stats['alerts']}",hex2bgr(WARNING),0.45,1),
        ]
        for i,(txt,col,sc,th) in enumerate(lines):
            c=col if isinstance(col,tuple) else hex2bgr(col)
            cv2.putText(panel,txt,(8,20+i*20),cv2.FONT_HERSHEY_SIMPLEX,sc,c,th,cv2.LINE_AA)
        img[8:8+195,8:8+300]=panel
        cv2.putText(img,ts(),(w-165,26),cv2.FONT_HERSHEY_SIMPLEX,0.48,hex2bgr(TEXT_DIM),1,cv2.LINE_AA)

    def _label_color(self,label):
        if label==PERSON_CLASS: return (0,255,178)
        if label in WEAPON_CLASSES: return (0,50,255)
        if label in VEHICLE_CLASSES: return (0,200,255)
        return (160,210,0)

    def _alert(self,msg,kind,tag):
        key=f"{kind}_{tag}";now=time.time()
        if now-self._last_alert.get(key,0)<5: return
        self._last_alert[key]=now
        self.stats["alerts"]+=1
        if kind=="threat": self.stats["threats"]+=1
        if kind in ("threat","accident"):
            self.clip_rec.trigger();self.alarm.trigger(4.0)
            self.stats["recording"]=True
            self.notifier.notify(msg,kind,self.clip_rec.clip_path)
            self.push.push(f"⚠ GARUD {kind.upper()}",
                           f"[{self.cam_id}] {msg}","urgent","warning,rotating_light")
        elif kind=="loiter":
            self.push.push("⏱ GARUD LOITERING",f"[{self.cam_id}] {msg}","default","eyes")
        entry={"time":ts(),"msg":msg,"kind":kind,"camera":self.cam_id}
        self.db.log_alert(kind,msg,self.clip_rec.clip_path if kind in("threat","accident") else "",self.cam_id)
        self.alert_cb(entry)


# ═════════════════════════════════════════════════════════════════════════════
#  GARUD ENGINE v4  — orchestrates multiple cameras
# ═════════════════════════════════════════════════════════════════════════════
class GarudEngine:
    def __init__(self, cfg, db):
        self.cfg      = cfg
        self.db       = db
        self.alarm    = AlarmSystem()
        self.push     = PushNotifier(cfg)
        self.notifier = Notifier(cfg)
        self.workers  = {}   # cam_id -> CameraWorker
        self._eq      = None
        self._last_alert_entries = deque(maxlen=200)

        self.model      = None
        self.pose_model = None
        self._load_models()

        # Combined stats
        self.stats = {"fps":0,"objects":0,"faces":0,"crowd":0,
                      "threats":0,"alerts":0,"loiterers":0,
                      "mode": "MPS" if MPS else "CPU",
                      "cameras":0,"recording":False}

    def _load_models(self):
        if not YOLO_OK: return
        try:
            self.model=YOLO("yolov8n.pt"); self.model.to(DEVICE)
            print("[GARUD] Detection model ✓")
        except Exception as e: print(f"[WARN] {e}")
        try:
            self.pose_model=YOLO("yolov8n-pose.pt"); self.pose_model.to(DEVICE)
            print("[GARUD] Pose model ✓")
        except Exception as e: print(f"[WARN] {e}")

    def start_camera(self, cam_id, source, eq=None):
        self._eq = eq
        if cam_id in self.workers:
            self.workers[cam_id].stop()
        w = CameraWorker(
            cam_id=cam_id, source=source,
            model=self.model, pose_model=self.pose_model,
            cfg=self.cfg, db=self.db,
            alarm=self.alarm, push_notifier=self.push,
            notifier=self.notifier,
            shared_alert_cb=self._on_alert
        )
        if w.start():
            self.workers[cam_id]=w
            self.stats["cameras"]=len(self.workers)
            return True
        return False

    def stop_camera(self, cam_id):
        if cam_id in self.workers:
            self.workers[cam_id].stop()
            del self.workers[cam_id]
            self.stats["cameras"]=len(self.workers)

    def stop_all(self):
        for w in self.workers.values(): w.stop()
        self.workers.clear()

    def get_frame(self, cam_id):
        w = self.workers.get(cam_id)
        return w.latest_jpg if w else None

    def aggregate_stats(self):
        if not self.workers: return self.stats
        s = {"fps":0,"objects":0,"faces":0,"crowd":0,
             "threats":0,"alerts":0,"loiterers":0,
             "mode":self.stats["mode"],"cameras":len(self.workers),"recording":False}
        for w in self.workers.values():
            s["fps"]       = max(s["fps"],w.stats["fps"])
            s["objects"]  += w.stats["objects"]
            s["faces"]    += w.stats["faces"]
            s["crowd"]    += w.stats["crowd"]
            s["threats"]  += w.stats["threats"]
            s["alerts"]   += w.stats["alerts"]
            s["loiterers"]+= w.stats["loiterers"]
            if w.stats["recording"]: s["recording"]=True
        self.stats.update(s)
        return s

    def _on_alert(self, entry):
        self._last_alert_entries.appendleft(entry)
        if self._eq:
            try: self._eq.put_nowait({"type":"alert","entry":entry})
            except queue.Full: pass

    def set_flag(self, attr, val):
        for w in self.workers.values():
            if hasattr(w,attr): setattr(w,attr,val)

    def reload_faces(self):
        for w in self.workers.values():
            w.face_rec.reload()

    def update_loiter_threshold(self, val):
        for w in self.workers.values():
            w.loiter_det.set_threshold(val)

    def set_continuous(self, val):
        for w in self.workers.values():
            w.cont_rec.set_enabled(val)


# ═════════════════════════════════════════════════════════════════════════════
#  WEB DASHBOARD  (multi-camera)
# ═════════════════════════════════════════════════════════════════════════════
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GARUD v4 — Dashboard</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Rajdhani:wght@400;600;700&display=swap');
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0A0E1A;color:#E8EAF0;font-family:'Rajdhani',sans-serif;min-height:100vh}
.topbar{background:#111827;border-bottom:2px solid #00FFB2;padding:8px 20px;
        display:flex;align-items:center;justify-content:space-between}
.logo{font-size:20px;font-weight:700;color:#00FFB2;letter-spacing:3px;font-family:'Share Tech Mono',monospace}
.badge{font-size:10px;background:#1A2035;border:1px solid #00FFB2;color:#00FFB2;
       padding:2px 8px;border-radius:2px;font-family:'Share Tech Mono',monospace}
.btn{padding:5px 12px;border:1px solid #1A2035;background:#1A2035;color:#E8EAF0;
     font-family:'Share Tech Mono',monospace;font-size:10px;border-radius:3px;
     cursor:pointer;text-decoration:none}
.btn:hover{border-color:#00FFB2;color:#00FFB2}
.main{display:grid;grid-template-columns:1fr 320px;gap:8px;padding:8px;height:calc(100vh - 50px)}
.feeds{display:grid;grid-template-columns:1fr 1fr;grid-template-rows:1fr 1fr;gap:6px}
.feed-cell{background:#000;border:1px solid #1A2035;border-radius:3px;overflow:hidden;
           position:relative;display:flex;align-items:center;justify-content:center;min-height:180px}
.feed-cell img{width:100%;height:100%;object-fit:contain}
.feed-label{position:absolute;top:6px;left:6px;font-family:'Share Tech Mono',monospace;
            font-size:10px;background:rgba(10,14,26,.8);color:#00FFB2;padding:2px 8px;border-radius:2px}
.feed-offline{color:#6B7A99;font-family:'Share Tech Mono',monospace;font-size:11px}
.side{display:flex;flex-direction:column;gap:8px;overflow-y:auto}
.card{background:#111827;border:1px solid #1A2035;border-radius:4px;padding:10px}
.ct{font-family:'Share Tech Mono',monospace;font-size:9px;color:#00FFB2;letter-spacing:2px;
    margin-bottom:8px;padding-bottom:4px;border-bottom:1px solid #1A2035}
.sg{display:grid;grid-template-columns:1fr 1fr 1fr;gap:5px}
.st{background:#0A0E1A;border-radius:3px;padding:6px;text-align:center}
.sv{font-size:20px;font-weight:700;font-family:'Share Tech Mono',monospace;color:#00FFB2}
.sl{font-size:8px;color:#6B7A99;letter-spacing:1px;margin-top:1px}
.st.d .sv{color:#FF3B5C}.st.w .sv{color:#FFB800}.st.o .sv{color:#FF8C00}
.al{max-height:220px;overflow-y:auto;display:flex;flex-direction:column;gap:3px}
.ai{padding:5px 8px;border-radius:2px;font-size:10px;font-family:'Share Tech Mono',monospace;
    border-left:3px solid #333;background:#0A0E1A;word-break:break-word}
.ai.threat{border-color:#FF3B5C;color:#FF8099}
.ai.anomaly{border-color:#FFB800;color:#FFD966}
.ai.crowd{border-color:#00FFB2;color:#80FFDA}
.ai.face{border-color:#4488FF;color:#88AAFF}
.ai.loiter{border-color:#FF8C00;color:#FFAA55}
.ai.accident{border-color:#FF3B5C;color:#FF9944}
.cl{max-height:120px;overflow-y:auto;display:flex;flex-direction:column;gap:3px;margin-top:4px}
.ci{padding:4px 7px;background:#0A0E1A;border-radius:2px;font-family:'Share Tech Mono',monospace;
    font-size:9px;color:#6B7A99;display:flex;justify-content:space-between}
.ci a{color:#4488FF;text-decoration:none}
.pulse{width:8px;height:8px;border-radius:50%;background:#00FFB2;display:inline-block;
       animation:pulse 1.5s infinite;margin-right:5px}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
::-webkit-scrollbar{width:3px}::-webkit-scrollbar-thumb{background:#1A2035}
</style>
</head>
<body>
<div class="topbar">
  <div class="logo">⬡ GARUD</div>
  <div style="display:flex;align-items:center;gap:10px">
    <span class="pulse"></span>
    <span style="font-size:11px;font-family:'Share Tech Mono',monospace;color:#00FFB2">LIVE</span>
    <span class="badge">v4.0 — MULTI-CAM</span>
    <a href="/report" class="btn">📄 REPORT</a>
    <a href="/logout" class="btn">LOGOUT</a>
  </div>
</div>
<div class="main">
  <div class="feeds">
    <div class="feed-cell" id="cell0">
      <span class="feed-label">CAM 0</span>
      <img id="feed0" src="/video_feed/0" onerror="this.style.display='none'">
      <span class="feed-offline" id="off0">OFFLINE</span>
    </div>
    <div class="feed-cell" id="cell1">
      <span class="feed-label">CAM 1</span>
      <img id="feed1" src="/video_feed/1" onerror="this.style.display='none'">
      <span class="feed-offline" id="off1">OFFLINE</span>
    </div>
    <div class="feed-cell" id="cell2">
      <span class="feed-label">CAM 2</span>
      <img id="feed2" src="/video_feed/2" onerror="this.style.display='none'">
      <span class="feed-offline" id="off2">OFFLINE</span>
    </div>
    <div class="feed-cell" id="cell3">
      <span class="feed-label">CAM 3</span>
      <img id="feed3" src="/video_feed/3" onerror="this.style.display='none'">
      <span class="feed-offline" id="off3">OFFLINE</span>
    </div>
  </div>
  <div class="side">
    <div class="card">
      <div class="ct">LIVE STATS</div>
      <div class="sg">
        <div class="st"><div class="sv" id="s-fps">—</div><div class="sl">FPS</div></div>
        <div class="st"><div class="sv" id="s-cam">—</div><div class="sl">CAMERAS</div></div>
        <div class="st"><div class="sv" id="s-obj">—</div><div class="sl">OBJECTS</div></div>
        <div class="st w"><div class="sv" id="s-crd">—</div><div class="sl">CROWD</div></div>
        <div class="st o"><div class="sv" id="s-loi">—</div><div class="sl">LOITERING</div></div>
        <div class="st"><div class="sv" id="s-fac">—</div><div class="sl">FACES</div></div>
        <div class="st d"><div class="sv" id="s-thr">—</div><div class="sl">THREATS</div></div>
        <div class="st d"><div class="sv" id="s-ale">—</div><div class="sl">ALERTS</div></div>
        <div class="st"><div class="sv" id="s-mod">—</div><div class="sl">MODE</div></div>
      </div>
    </div>
    <div class="card">
      <div class="ct">ALERT LOG</div>
      <div class="al" id="alert-list"></div>
    </div>
    <div class="card">
      <div class="ct">SAVED CLIPS</div>
      <div class="cl" id="clip-list"><div style="color:#6B7A99;font-size:10px;font-family:'Share Tech Mono',monospace">No clips yet</div></div>
    </div>
  </div>
</div>
<script>
let ac=0;
function poll(){
  fetch('/stats').then(r=>r.json()).then(d=>{
    document.getElementById('s-fps').textContent=d.fps;
    document.getElementById('s-cam').textContent=d.cameras;
    document.getElementById('s-obj').textContent=d.objects;
    document.getElementById('s-crd').textContent=d.crowd;
    document.getElementById('s-loi').textContent=d.loiterers;
    document.getElementById('s-fac').textContent=d.faces;
    document.getElementById('s-thr').textContent=d.threats;
    document.getElementById('s-ale').textContent=d.alerts;
    document.getElementById('s-mod').textContent=d.mode;
  }).catch(()=>{});
  fetch('/alerts').then(r=>r.json()).then(data=>{
    if(data.length!==ac){ac=data.length;
      const list=document.getElementById('alert-list');list.innerHTML='';
      data.slice(0,60).forEach(a=>{
        const el=document.createElement('div');
        el.className='ai '+(a.kind||'');
        el.textContent='['+a.time+']['+a.camera+'] '+a.msg;
        list.appendChild(el);
      });
    }
  }).catch(()=>{});
  fetch('/clips').then(r=>r.json()).then(clips=>{
    if(!clips.length) return;
    const list=document.getElementById('clip-list');list.innerHTML='';
    clips.forEach(c=>{
      const el=document.createElement('div');el.className='ci';
      el.innerHTML='<span>'+c.name+'</span><a href="/clip/'+c.name+'" download>⬇</a>';
      list.appendChild(el);
    });
  }).catch(()=>{});
}
setInterval(poll,900); poll();
</script>
</body>
</html>
"""

LOGIN_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>GARUD Login</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&display=swap');
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0A0E1A;display:flex;align-items:center;justify-content:center;min-height:100vh;font-family:'Share Tech Mono',monospace}
.box{background:#111827;border:1px solid #00FFB2;border-radius:6px;padding:36px;width:340px;text-align:center}
.logo{color:#00FFB2;font-size:26px;letter-spacing:4px;margin-bottom:4px}
.sub{color:#6B7A99;font-size:10px;margin-bottom:28px;letter-spacing:2px}
input{width:100%;padding:9px 12px;background:#0A0E1A;border:1px solid #1A2035;color:#E8EAF0;
      font-family:'Share Tech Mono',monospace;font-size:12px;border-radius:3px;margin-bottom:10px;outline:none}
input:focus{border-color:#00FFB2}
button{width:100%;padding:11px;background:#00FFB2;color:#0A0E1A;font-family:'Share Tech Mono',monospace;
       font-size:12px;font-weight:bold;border:none;border-radius:3px;cursor:pointer;letter-spacing:2px}
button:hover{background:#00DDA0}
.err{color:#FF3B5C;font-size:10px;margin-top:8px}
</style></head><body>
<div class="box">
  <div class="logo">⬡ GARUD</div>
  <div class="sub">COGNITIVE SURVEILLANCE v4</div>
  <form method="POST">
    <input name="username" placeholder="USERNAME" autocomplete="off">
    <input name="password" type="password" placeholder="PASSWORD">
    <button type="submit">LOGIN →</button>
  </form>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
</div></body></html>
"""

def start_web_server(engine, db, cfg, report_gen):
    if not FLASK_OK: return
    app = Flask(__name__)
    app.secret_key = secrets.token_hex(16)
    port = cfg.get("web_port", 8080)

    def auth(f):
        from functools import wraps
        @wraps(f)
        def dec(*a,**kw):
            if not session.get("user"): return redirect("/login")
            return f(*a,**kw)
        return dec

    @app.route("/login", methods=["GET","POST"])
    def login():
        err=""
        if request.method=="POST":
            u=request.form.get("username",""); p=request.form.get("password","")
            role=db.check_user(u,p)
            if role: session["user"]=u; session["role"]=role; return redirect("/")
            err="Invalid credentials"
        return render_template_string(LOGIN_HTML, error=err)

    @app.route("/logout")
    def logout(): session.clear(); return redirect("/login")

    @app.route("/")
    @auth
    def index(): return render_template_string(DASHBOARD_HTML)

    @app.route("/video_feed/<int:cam_id>")
    @auth
    def video_feed(cam_id):
        def gen():
            while True:
                jpg=engine.get_frame(str(cam_id))
                if jpg: yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"+jpg+b"\r\n"
                time.sleep(0.033)
        return Response(gen(),mimetype="multipart/x-mixed-replace; boundary=frame")

    @app.route("/stats")
    @auth
    def stats(): return jsonify(engine.aggregate_stats())

    @app.route("/alerts")
    @auth
    def alerts(): return jsonify(db.get_alerts(100))

    @app.route("/clips")
    @auth
    def clips():
        files=sorted(RECORDINGS_DIR.glob("*.mp4"),key=os.path.getmtime,reverse=True)
        return jsonify([{"name":f.name} for f in files[:20]])

    @app.route("/clip/<name>")
    @auth
    def serve_clip(name): return send_from_directory(str(RECORDINGS_DIR),name)

    @app.route("/report")
    @auth
    def report():
        path=report_gen.generate()
        if path and os.path.exists(path):
            return send_from_directory(str(Path(path).parent), Path(path).name)
        return "Report generation failed — install reportlab: pip install reportlab", 500

    print(f"[WEB] Dashboard → http://localhost:{port}  (admin / garud2024)")
    threading.Thread(
        target=lambda: app.run(host="0.0.0.0",port=port,debug=False,use_reloader=False),
        daemon=True).start()


# ═════════════════════════════════════════════════════════════════════════════
#  SETTINGS WINDOW
# ═════════════════════════════════════════════════════════════════════════════
class SettingsWindow:
    def __init__(self, parent, cfg, engine):
        self.cfg=cfg; self.engine=engine
        self.win=tk.Toplevel(parent)
        self.win.title("GARUD v4 Settings")
        self.win.geometry("520x640")
        self.win.configure(bg=BG_DARK)
        self.win.resizable(False,False)
        self._build()

    def _build(self):
        w=self.win
        tk.Label(w,text="⬡  GARUD v4 Settings",font=("Courier New",15,"bold"),
                 fg=ACCENT,bg=BG_DARK).pack(pady=(14,4))
        tk.Frame(w,bg=ACCENT,height=1).pack(fill="x",padx=18)
        nb=ttk.Notebook(w); nb.pack(fill="both",expand=True,padx=10,pady=8)
        style=ttk.Style()
        style.configure("TNotebook",background=BG_MID)
        style.configure("TNotebook.Tab",background=BG_CARD,foreground=TEXT_DIM,font=("Courier New",9))
        style.map("TNotebook.Tab",background=[("selected",BG_MID)],foreground=[("selected",ACCENT)])
        self._tab_alerts(nb); self._tab_cameras(nb)
        self._tab_detection(nb); self._tab_system(nb)
        tk.Button(w,text="SAVE & APPLY",command=self._save,
                  bg=ACCENT,fg=BG_DARK,font=("Courier New",11,"bold"),
                  relief="flat",cursor="hand2").pack(fill="x",padx=18,pady=(0,10))

    def _field(self,parent,label,key,show=""):
        f=tk.Frame(parent,bg=BG_MID); f.pack(fill="x",pady=2)
        tk.Label(f,text=label,width=22,anchor="w",bg=BG_MID,fg=TEXT_DIM,
                 font=("Courier New",9)).pack(side="left")
        v=tk.StringVar(value=str(self.cfg.get(key,"")))
        tk.Entry(f,textvariable=v,bg=BG_CARD,fg=TEXT_MAIN,font=("Courier New",9),
                 relief="flat",insertbackground=ACCENT,show=show).pack(side="left",fill="x",expand=True)
        setattr(self,f"_v_{key}",v)

    def _tab_alerts(self,nb):
        t=tk.Frame(nb,bg=BG_MID); nb.add(t,text=" ALERTS ")
        tk.Label(t,text="Push Notifications (ntfy.sh — FREE)",bg=BG_MID,fg=ACCENT,
                 font=("Courier New",10,"bold")).pack(anchor="w",padx=10,pady=(10,2))
        self._field(t,"ntfy Topic","ntfy_topic")
        tk.Label(t,text="Subscribe free at https://ntfy.sh/YOUR_TOPIC\nor install ntfy app on iOS/Android",
                 bg=BG_MID,fg=TEXT_DIM,font=("Courier New",8),justify="left").pack(anchor="w",padx=12,pady=2)
        tk.Label(t,text="Twilio SMS",bg=BG_MID,fg=ACCENT,
                 font=("Courier New",10,"bold")).pack(anchor="w",padx=10,pady=(10,2))
        for l,k in [("Account SID","twilio_sid"),("Auth Token","twilio_token"),
                    ("From Number","twilio_from"),("Alert Phone","alert_phone")]:
            self._field(t,l,k)
        tk.Label(t,text="Email (Gmail)",bg=BG_MID,fg=ACCENT,
                 font=("Courier New",10,"bold")).pack(anchor="w",padx=10,pady=(10,2))
        for l,k in [("Gmail Address","smtp_user"),("App Password","smtp_pass"),("Alert Email","alert_email")]:
            self._field(t,l,k,show="*" if "pass" in k or "token" in k else "")

    def _tab_cameras(self,nb):
        t=tk.Frame(nb,bg=BG_MID); nb.add(t,text=" CAMERAS ")
        tk.Label(t,text="Camera Sources (0-3)",bg=BG_MID,fg=ACCENT,
                 font=("Courier New",10,"bold")).pack(anchor="w",padx=10,pady=(10,4))
        sources=self.cfg.get("camera_sources",["0","","",""])
        while len(sources)<4: sources.append("")
        self._cam_vars=[]
        for i in range(4):
            f=tk.Frame(t,bg=BG_MID); f.pack(fill="x",padx=10,pady=2)
            tk.Label(f,text=f"Camera {i}:",width=12,anchor="w",bg=BG_MID,fg=TEXT_DIM,
                     font=("Courier New",9)).pack(side="left")
            v=tk.StringVar(value=sources[i])
            tk.Entry(f,textvariable=v,bg=BG_CARD,fg=TEXT_MAIN,font=("Courier New",9),
                     relief="flat",insertbackground=ACCENT).pack(side="left",fill="x",expand=True)
            self._cam_vars.append(v)
        tk.Label(t,text="Use 0,1,2 for webcams or rtsp://user:pass@IP/stream for IP cams",
                 bg=BG_MID,fg=TEXT_DIM,font=("Courier New",8)).pack(anchor="w",padx=10,pady=4)

    def _tab_detection(self,nb):
        t=tk.Frame(nb,bg=BG_MID); nb.add(t,text=" DETECTION ")
        for l,k in [("Crowd Threshold","crowd_threshold"),
                    ("Loiter Alert (seconds)","loiter_threshold"),
                    ("Continuous Segment (min)","continuous_segment_min")]:
            self._field(t,l,k)
        tk.Label(t,text="Continuous Recording",bg=BG_MID,fg=ACCENT,
                 font=("Courier New",10,"bold")).pack(anchor="w",padx=10,pady=(12,4))
        self._cont_var=tk.BooleanVar(value=self.cfg.get("continuous_enabled",False))
        tk.Checkbutton(t,text="Enable 24/7 continuous recording",variable=self._cont_var,
                       bg=BG_MID,fg=TEXT_MAIN,selectcolor=BG_CARD,
                       activebackground=BG_MID,font=("Courier New",9)).pack(anchor="w",padx=14)
        tk.Button(t,text="Open continuous folder",
                  command=lambda:subprocess.Popen(["open",str(CONTINUOUS_DIR)]),
                  bg=BG_CARD,fg=BLUE,font=("Courier New",9),
                  relief="flat",cursor="hand2").pack(anchor="w",padx=10,pady=6)

    def _tab_system(self,nb):
        t=tk.Frame(nb,bg=BG_MID); nb.add(t,text=" SYSTEM ")
        self._field(t,"Web Port","web_port")
        self._field(t,"Report Hour (0-23)","report_hour")
        tk.Label(t,text="Face Recognition",bg=BG_MID,fg=ACCENT,
                 font=("Courier New",10,"bold")).pack(anchor="w",padx=10,pady=(12,4))
        tk.Label(t,text=f"Add photos to:\n{KNOWN_DIR}\nFilename = Name.jpg",
                 bg=BG_MID,fg=TEXT_DIM,font=("Courier New",9),justify="left").pack(anchor="w",padx=10)
        tk.Button(t,text="Open known_faces folder",
                  command=lambda:subprocess.Popen(["open",str(KNOWN_DIR)]),
                  bg=BG_CARD,fg=ACCENT,font=("Courier New",9),relief="flat",cursor="hand2").pack(anchor="w",padx=10,pady=4)
        tk.Button(t,text="Reload Face Database",
                  command=lambda:(self.engine.reload_faces(),
                                   tk.messagebox.showinfo("Done","Faces reloaded")),
                  bg=BG_CARD,fg=ACCENT,font=("Courier New",9),relief="flat",cursor="hand2").pack(anchor="w",padx=10)
        tk.Button(t,text="Generate Report Now",
                  command=self._gen_report,
                  bg=BG_CARD,fg=BLUE,font=("Courier New",9),relief="flat",cursor="hand2").pack(anchor="w",padx=10,pady=6)

    def _gen_report(self):
        if not hasattr(self,'_report_gen'): return
        path=self._report_gen.generate()
        if path:
            subprocess.Popen(["open",str(REPORTS_DIR)])
            tk.messagebox.showinfo("Report","Report saved to reports/ folder")

    def _save(self):
        for key in DEFAULT_CONFIG:
            attr=f"_v_{key}"
            if hasattr(self,attr):
                val=getattr(self,attr).get()
                try: val=int(val)
                except: pass
                self.cfg[key]=val
        if hasattr(self,"_cam_vars"):
            self.cfg["camera_sources"]=[v.get() for v in self._cam_vars]
        if hasattr(self,"_cont_var"):
            self.cfg["continuous_enabled"]=self._cont_var.get()
            self.engine.set_continuous(self._cont_var.get())
        save_config(self.cfg)
        self.engine.notifier=Notifier(self.cfg)
        self.engine.push=PushNotifier(self.cfg)
        self.engine.update_loiter_threshold(self.cfg.get("loiter_threshold",20))
        tk.messagebox.showinfo("Saved","Settings saved. Restart cameras to apply camera changes.")
        self.win.destroy()


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN GUI
# ═════════════════════════════════════════════════════════════════════════════
class GarudApp:
    def __init__(self):
        self.cfg        = load_config()
        self.db         = Database()
        self.eq         = queue.Queue(maxsize=16)
        self.engine     = GarudEngine(self.cfg, self.db)
        self.report_gen = ReportGenerator(self.db, self.cfg)
        self.cam_active = {}   # cam_id -> bool

        start_web_server(self.engine, self.db, self.cfg, self.report_gen)

        self.root=tk.Tk()
        self.root.title(f"GARUD Cognitive Surveillance System {VERSION}")
        self.root.configure(bg=BG_DARK)
        self.root.geometry("1480x900")
        self.root.minsize(1200,750)

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW",self._close)
        self.root.after(16,self._poll)

    def _build_ui(self):
        self.root.grid_rowconfigure(1,weight=1)
        self.root.grid_columnconfigure(0,weight=1)

        # Top bar
        top=tk.Frame(self.root,bg=BG_CARD,height=52)
        top.grid(row=0,column=0,sticky="ew"); top.grid_propagate(False)
        tk.Label(top,text="⬡  GARUD",font=("Courier New",20,"bold"),
                 fg=ACCENT,bg=BG_CARD).pack(side="left",padx=18,pady=10)
        tk.Label(top,text=f"Cognitive Surveillance System  •  {VERSION}",
                 font=("Courier New",11),fg=TEXT_DIM,bg=BG_CARD).pack(side="left")
        port=self.cfg.get("web_port",8080)
        tk.Label(top,text=f"🌐  http://localhost:{port}",
                 font=("Courier New",10),fg=BLUE,bg=BG_CARD).pack(side="right",padx=18)
        self.status_lbl=tk.Label(top,text="● OFFLINE",
                 font=("Courier New",11,"bold"),fg=DANGER,bg=BG_CARD)
        self.status_lbl.pack(side="right",padx=8)

        self._build_stats_bar()

        # Content
        content=tk.Frame(self.root,bg=BG_DARK)
        content.grid(row=1,column=0,sticky="nsew",padx=8,pady=6)
        content.grid_rowconfigure(0,weight=1)
        content.grid_columnconfigure(0,weight=1)
        content.grid_columnconfigure(1,minsize=330,weight=0)

        # Camera grid (2x2)
        cam_frame=tk.Frame(content,bg=BG_DARK)
        cam_frame.grid(row=0,column=0,sticky="nsew",padx=(0,6))
        cam_frame.grid_rowconfigure(0,weight=1); cam_frame.grid_rowconfigure(1,weight=1)
        cam_frame.grid_columnconfigure(0,weight=1); cam_frame.grid_columnconfigure(1,weight=1)

        self.cam_labels={}
        for i in range(4):
            r,c=divmod(i,2)
            cell=tk.Frame(cam_frame,bg="black")
            cell.grid(row=r,column=c,sticky="nsew",padx=3,pady=3)
            cell.grid_rowconfigure(0,weight=1); cell.grid_columnconfigure(0,weight=1)
            lbl=tk.Label(cell,bg="black",
                text=f"[ CAM {i} — INACTIVE ]",
                font=("Courier New",10),fg=TEXT_DIM,width=1,height=1)
            lbl.grid(row=0,column=0,sticky="nsew")
            self.cam_labels[str(i)]=lbl

        # Right panel
        right=tk.Frame(content,bg=BG_MID,width=330)
        right.grid(row=0,column=1,sticky="ns"); right.grid_propagate(False)
        canvas=tk.Canvas(right,bg=BG_MID,highlightthickness=0,width=328)
        sb=tk.Scrollbar(right,orient="vertical",command=canvas.yview)
        self.panel=tk.Frame(canvas,bg=BG_MID)
        self.panel.bind("<Configure>",
            lambda e:canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0,0),window=self.panel,anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left",fill="both",expand=True)
        sb.pack(side="right",fill="y")
        canvas.bind_all("<MouseWheel>",
            lambda e:canvas.yview_scroll(int(-1*(e.delta/120)),"units"))
        self._build_panel(self.panel)

    def _build_stats_bar(self):
        bar=tk.Frame(self.root,bg=BG_CARD,height=44)
        bar.grid(row=2,column=0,sticky="ew"); bar.grid_propagate(False)
        self.sv={}
        for k,lbl,col in [
            ("fps","FPS",ACCENT),("cameras","CAMS",ACCENT),
            ("objects","OBJECTS",ACCENT),("crowd","CROWD",WARNING),
            ("loiterers","LOITERING",ORANGE),("faces","FACES",ACCENT),
            ("threats","THREATS",DANGER),("alerts","ALERTS",DANGER),
        ]:
            f=tk.Frame(bar,bg=BG_CARD); f.pack(side="left",padx=14,pady=5)
            tk.Label(f,text=lbl,bg=BG_CARD,fg=TEXT_DIM,font=("Courier New",8)).pack()
            v=tk.StringVar(value="—"); self.sv[k]=v
            tk.Label(f,textvariable=v,bg=BG_CARD,fg=col,
                     font=("Courier New",12,"bold")).pack()
        self.rec_var=tk.StringVar(value="")
        tk.Label(bar,textvariable=self.rec_var,bg=BG_CARD,fg=DANGER,
                 font=("Courier New",10,"bold")).pack(side="right",padx=16)

    def _build_panel(self,p):
        pad=dict(padx=12,pady=4)

        # Camera controls
        self._sec(p,"CAMERAS")
        sources=self.cfg.get("camera_sources",["0","","",""])
        while len(sources)<4: sources.append("")
        self.cam_src_vars={}
        self.cam_btns={}
        for i in range(4):
            f=tk.Frame(p,bg=BG_MID); f.pack(fill="x",padx=12,pady=2)
            tk.Label(f,text=f"CAM {i}:",width=6,anchor="w",bg=BG_MID,fg=TEXT_DIM,
                     font=("Courier New",9)).pack(side="left")
            v=tk.StringVar(value=sources[i]); self.cam_src_vars[str(i)]=v
            tk.Entry(f,textvariable=v,bg=BG_CARD,fg=TEXT_MAIN,
                     font=("Courier New",9),width=12,relief="flat",
                     insertbackground=ACCENT).pack(side="left",padx=4)
            btn=tk.Button(f,text="▶",width=3,
                command=lambda ci=str(i): self._toggle_cam(ci),
                bg=ACCENT,fg=BG_DARK,font=("Courier New",9,"bold"),
                relief="flat",cursor="hand2")
            btn.pack(side="left"); self.cam_btns[str(i)]=btn

        # Features
        self._sec(p,"DETECTION")
        self._chk(p,"Motion Trails",   "show_tracks")
        self._chk(p,"Face Detection",  "show_faces")
        self._chk(p,"Face Recognition","face_recog")
        self._chk(p,"Pose & Skeleton", "show_pose")
        self._chk(p,"Loitering",       "show_loiter")
        self._chk(p,"Accident",        "show_accident")
        self._chk(p,"Crowd Heatmap",   "show_heatmap",False)
        self._chk(p,"Anomaly/Speed",   "show_anomaly")
        self._chk(p,"Privacy Blur",    "face_blur",False)

        # Alarm
        self._sec(p,"ALARM")
        tk.Button(p,text="🔔  TEST ALARM",command=lambda:self.engine.alarm.trigger(3),
                  bg="#2A1020",fg=DANGER,font=("Courier New",10,"bold"),
                  relief="flat",cursor="hand2").pack(fill="x",**pad)
        tk.Button(p,text="✕  CLEAR",command=self.engine.alarm.clear,
                  bg=BG_CARD,fg=TEXT_DIM,font=("Courier New",9),
                  relief="flat",cursor="hand2").pack(fill="x",padx=12,pady=2)

        # Report
        self._sec(p,"REPORTS")
        tk.Button(p,text="📄  Generate Report Now",
                  command=self._gen_report,
                  bg=BG_CARD,fg=BLUE,font=("Courier New",9),
                  relief="flat",cursor="hand2").pack(fill="x",**pad)
        tk.Button(p,text="📁  Open Reports Folder",
                  command=lambda:subprocess.Popen(["open",str(REPORTS_DIR)]),
                  bg=BG_CARD,fg=TEXT_DIM,font=("Courier New",9),
                  relief="flat",cursor="hand2").pack(fill="x",padx=12,pady=2)
        tk.Button(p,text="📁  Open Recordings",
                  command=lambda:subprocess.Popen(["open",str(RECORDINGS_DIR)]),
                  bg=BG_CARD,fg=TEXT_DIM,font=("Courier New",9),
                  relief="flat",cursor="hand2").pack(fill="x",padx=12,pady=2)

        # Alerts
        self._sec(p,"LIVE ALERTS")
        af=tk.Frame(p,bg=BG_CARD,height=180); af.pack(fill="x",padx=12,pady=4)
        af.pack_propagate(False)
        self.alert_box=tk.Text(af,bg=BG_CARD,fg=TEXT_MAIN,
            font=("Courier New",8),relief="flat",state="disabled",wrap="word")
        asb=tk.Scrollbar(af,command=self.alert_box.yview)
        self.alert_box.configure(yscrollcommand=asb.set)
        asb.pack(side="right",fill="y")
        self.alert_box.pack(fill="both",expand=True,padx=4,pady=4)
        for tag,col in [("threat",DANGER),("anomaly",WARNING),("crowd",ACCENT),
                         ("face",BLUE),("loiter",ORANGE),("accident","#FF9944")]:
            self.alert_box.tag_config(tag,foreground=col)
        tk.Button(p,text="Clear",command=self._clear_alerts,
            bg=BG_CARD,fg=TEXT_DIM,font=("Courier New",9),
            relief="flat",cursor="hand2").pack(fill="x",padx=12,pady=2)

        # Settings
        self._sec(p,"CONFIGURE")
        sw=SettingsWindow.__new__(SettingsWindow)
        tk.Button(p,text="⚙  Settings",
                  command=lambda:SettingsWindow(self.root,self.cfg,self.engine),
                  bg=BG_CARD,fg=ACCENT,font=("Courier New",9),
                  relief="flat",cursor="hand2").pack(fill="x",**pad)

        # System info
        self._sec(p,"SYSTEM")
        import sys as _sys
        for line in [
            f"Python  : {_sys.version.split()[0]}",
            f"YOLO    : {'✓' if YOLO_OK else '✗ demo'}",
            f"MPS/M4  : {'✓' if MPS else '✗'}",
            f"PDF     : {'✓' if PDF_OK else '✗ pip install reportlab'}",
            f"Twilio  : {'✓' if TWILIO_OK else '✗ pip install twilio'}",
            f"Flask   : {'✓' if FLASK_OK else '✗'}",
        ]:
            tk.Label(p,text=line,bg=BG_MID,fg=TEXT_DIM,
                font=("Courier New",8),anchor="w").pack(fill="x",padx=14,pady=1)

    def _sec(self,p,t):
        tk.Label(p,text=f"  {t}",bg=BG_MID,fg=ACCENT,
                 font=("Courier New",9,"bold")).pack(fill="x",pady=(10,2))
        tk.Frame(p,bg=ACCENT,height=1).pack(fill="x",padx=12)

    def _chk(self,p,label,attr,default=True):
        v=tk.BooleanVar(value=default)
        tk.Checkbutton(p,text=label,variable=v,
            command=lambda:self.engine.set_flag(attr,v.get()),
            bg=BG_MID,fg=TEXT_MAIN,selectcolor=BG_CARD,
            activebackground=BG_MID,activeforeground=ACCENT,
            font=("Courier New",9),cursor="hand2").pack(anchor="w",padx=14,pady=1)

    def _toggle_cam(self,cam_id):
        if self.cam_active.get(cam_id):
            self.engine.stop_camera(cam_id)
            self.cam_active[cam_id]=False
            self.cam_btns[cam_id].configure(text="▶",bg=ACCENT,fg=BG_DARK)
            self.cam_labels[cam_id].configure(image="",
                text=f"[ CAM {cam_id} — INACTIVE ]")
            if not any(self.cam_active.values()):
                self.status_lbl.configure(text="● OFFLINE",fg=DANGER)
        else:
            src=self.cam_src_vars[cam_id].get().strip()
            if not src: src=cam_id
            ok=self.engine.start_camera(cam_id,src,self.eq)
            if ok:
                self.cam_active[cam_id]=True
                self.cam_btns[cam_id].configure(text="■",bg=DANGER,fg="white")
                self.status_lbl.configure(text="● LIVE",fg=ACCENT)

    def _gen_report(self):
        path=self.report_gen.generate()
        if path:
            subprocess.Popen(["open",str(REPORTS_DIR)])
            tk.messagebox.showinfo("Report Generated",
                f"Report saved to:\n{path}\n\nOpening reports folder…")
        else:
            tk.messagebox.showwarning("Report",
                "Install reportlab for PDF:\npip install reportlab\n\nText report generated instead.")

    def _poll(self):
        try:
            while True:
                msg=self.eq.get_nowait()
                if msg["type"]=="alert":
                    e=msg["entry"]
                    self.alert_box.configure(state="normal")
                    cam=e.get("camera","")
                    self.alert_box.insert("1.0",
                        f"[{e['time']}][{cam}] {e['msg']}\n",e.get("kind",""))
                    self.alert_box.configure(state="disabled")
                elif msg["type"]=="error":
                    tk.messagebox.showerror("GARUD",msg["msg"])
        except queue.Full: pass
        except queue.Empty: pass

        # Update video frames for all active cameras
        for cam_id,active in self.cam_active.items():
            if not active: continue
            w=self.engine.workers.get(cam_id)
            if w and w.latest_jpg and PIL_OK:
                try:
                    img_data=w.latest_jpg
                    nparr=np.frombuffer(img_data,np.uint8)
                    frame=cv2.imdecode(nparr,cv2.IMREAD_COLOR)
                    if frame is not None:
                        lbl=self.cam_labels[cam_id]
                        pw=lbl.master.winfo_width(); ph=lbl.master.winfo_height()
                        if pw<10: pw,ph=480,270
                        fh,fw=frame.shape[:2]
                        scale=min(pw/fw,ph/fh)
                        nw,nh=int(fw*scale),int(fh*scale)
                        rgb=cv2.cvtColor(cv2.resize(frame,(nw,nh)),cv2.COLOR_BGR2RGB)
                        imgtk=ImageTk.PhotoImage(image=Image.fromarray(rgb))
                        lbl.imgtk=imgtk
                        lbl.configure(image=imgtk,text="",width=nw,height=nh)
                except: pass

        # Update stats
        s=self.engine.aggregate_stats()
        for k,v in s.items():
            if k in self.sv: self.sv[k].set(str(v))
        self.rec_var.set("● REC" if s.get("recording") else "")

        self.root.after(16,self._poll)

    def _clear_alerts(self):
        self.alert_box.configure(state="normal")
        self.alert_box.delete("1.0","end")
        self.alert_box.configure(state="disabled")

    def _close(self):
        self.engine.stop_all()
        self.report_gen.stop()
        self.root.destroy()

    def run(self):
        port=self.cfg.get("web_port",8080)
        print("\n"+"="*58)
        print(f"  GARUD {VERSION} — Cognitive Surveillance System")
        print("="*58)
        print(f"  Web dashboard : http://localhost:{port}")
        print(f"  Login         : admin / garud2024")
        print(f"  YOLO          : {'ready' if YOLO_OK else 'demo mode'}")
        print(f"  MPS/M4        : {'✓ enabled' if MPS else '–'}")
        print(f"  PDF reports   : {'✓' if PDF_OK else '✗ — pip install reportlab'}")
        print(f"  Push notifs   : ntfy.sh (set topic in Settings)")
        print(f"  Recordings    : {RECORDINGS_DIR}")
        print(f"  Reports       : {REPORTS_DIR}")
        print("="*58+"\n")
        self.root.mainloop()


if __name__ == "__main__":
    GarudApp().run()
