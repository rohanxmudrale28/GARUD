#!/usr/bin/env python3
"""
GARUD — CCTV Footage Tester
━━━━━━━━━━━━━━━━━━━━━━━━━━━
Run GARUD's full detection pipeline on any video file.

Usage:
    python3 test_footage.py                        # opens file picker
    python3 test_footage.py path/to/video.mp4      # direct path
    python3 test_footage.py --url                  # download sample CCTV footage

Controls while playing:
    SPACE  — pause / resume
    S      — step one frame
    Q/ESC  — quit
    +/-    — speed up / slow down
    S      — save current frame as screenshot
    H      — toggle HUD
"""

import cv2
import numpy as np
import sys
import os
import math
import time
import argparse
import threading
import urllib.request
from pathlib import Path
from datetime import datetime
from collections import defaultdict, deque

# ── Try YOLO ──────────────────────────────────────────────────────────────────
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

BASE_DIR    = Path(__file__).parent
SHOTS_DIR   = BASE_DIR / "screenshots"
SHOTS_DIR.mkdir(exist_ok=True)

# ── Sample CCTV footage URLs (public domain / CC licensed) ────────────────────
SAMPLE_VIDEOS = {
    "1": {
        "name": "Highway Traffic CCTV",
        "url":  "https://www.pexels.com/download/video/2103099/",
        "file": "sample_highway.mp4",
    },
    "2": {
        "name": "Street Intersection CCTV",
        "url":  "https://www.pexels.com/download/video/2491284/",
        "file": "sample_street.mp4",
    },
    "3": {
        "name": "Parking Lot CCTV",
        "url":  "https://www.pexels.com/download/video/3195394/",
        "file": "sample_parking.mp4",
    },
}

VEHICLE_CLASSES = {"car","truck","bus","motorcycle","bicycle"}
PERSON_CLASS    = "person"
WEAPON_CLASSES  = {"knife","scissors","baseball bat","bottle"}

KP_L_SHOULDER=5; KP_R_SHOULDER=6
KP_L_WRIST=9;    KP_R_WRIST=10
KP_L_HIP=11;     KP_R_HIP=12

# ── Colours ───────────────────────────────────────────────────────────────────
C_GREEN   = (0,255,178)
C_RED     = (0,50,255)
C_ORANGE  = (0,140,255)
C_YELLOW  = (0,200,255)
C_BLUE    = (255,150,0)
C_WHITE   = (230,235,240)
C_DIM     = (100,120,155)
C_DARK    = (10,14,26)

def put_label(img, text, pos, color=C_GREEN, scale=0.5, thickness=1):
    x,y=pos
    (tw,th),_=cv2.getTextSize(text,cv2.FONT_HERSHEY_SIMPLEX,scale,thickness)
    cv2.rectangle(img,(x-3,y-th-5),(x+tw+3,y+3),C_DARK,-1)
    cv2.putText(img,text,(x,y),cv2.FONT_HERSHEY_SIMPLEX,scale,color,thickness,cv2.LINE_AA)


# ── Tracker ───────────────────────────────────────────────────────────────────
class Tracker:
    def __init__(self):
        self.nid=0; self.objs={}; self.lost={}
        self.paths=defaultdict(lambda: deque(maxlen=60))

    def _iou(self,a,b):
        ix1,iy1=max(a[0],b[0]),max(a[1],b[1])
        ix2,iy2=min(a[2],b[2]),min(a[3],b[3])
        iw,ih=max(0,ix2-ix1),max(0,iy2-iy1); inter=iw*ih
        ua=(a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-inter
        return inter/ua if ua>0 else 0

    def update(self,dets):
        used=set(); new_o={}
        for oid,(cx,cy,box) in list(self.objs.items()):
            best,bd=0,-1
            for i,d in enumerate(dets):
                if i in used: continue
                iou=self._iou(box,d[:4])
                if iou>best: best,bd=iou,i
            if best>0.15 and bd>=0:
                x1,y1,x2,y2=dets[bd][:4]; ncx,ncy=(x1+x2)//2,(y1+y2)//2
                new_o[oid]=(ncx,ncy,(x1,y1,x2,y2)); self.paths[oid].append((ncx,ncy))
                self.lost[oid]=0; used.add(bd)
            else:
                self.lost[oid]=self.lost.get(oid,0)+1
                if self.lost[oid]<=20: new_o[oid]=(cx,cy,box)
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


# ── Accident Detector ─────────────────────────────────────────────────────────
class AccidentDetector:
    def __init__(self):
        self.vehicle_history  = defaultdict(lambda: deque(maxlen=30))
        self.speed_history    = defaultdict(lambda: deque(maxlen=15))
        self.stationary_since = {}

    def update(self, tracked):
        events      = []
        vehicle_ids = {oid:d for oid,d in tracked.items() if d[4] in VEHICLE_CLASSES}
        person_ids  = {oid:d for oid,d in tracked.items() if d[4]==PERSON_CLASS}

        for oid,det in vehicle_ids.items():
            x1,y1,x2,y2=det[:4]; cx,cy=(x1+x2)//2,(y1+y2)//2
            self.vehicle_history[oid].append((cx,cy))
            hist=list(self.vehicle_history[oid])
            if len(hist)>=3:
                dx=hist[-1][0]-hist[-3][0]; dy=hist[-1][1]-hist[-3][1]
                self.speed_history[oid].append(math.sqrt(dx*dx+dy*dy))
            speeds=list(self.speed_history[oid])
            if len(speeds)>=10:
                recent=sum(speeds[-3:])/3; earlier=sum(speeds[-10:-5])/5
                if earlier>12 and recent<2.5:
                    events.append(("SUDDEN STOP",oid,(x1,y1,x2,y2),"HIGH"))
            if len(speeds)>=5 and sum(speeds[-5:])/5<1.5:
                self.stationary_since.setdefault(oid,time.time())
                if time.time()-self.stationary_since.get(oid,time.time())>6:
                    events.append(("STATIONARY VEHICLE",oid,(x1,y1,x2,y2),"MED"))
            else:
                self.stationary_since.pop(oid,None)

        vlist=list(vehicle_ids.items())
        for i in range(len(vlist)):
            for j in range(i+1,len(vlist)):
                oa,da=vlist[i]; ob,db=vlist[j]
                if self._iou(da[:4],db[:4])>0.15:
                    box=(min(da[0],db[0]),min(da[1],db[1]),
                         max(da[2],db[2]),max(da[3],db[3]))
                    events.append(("VEHICLE COLLISION",oa,box,"CRITICAL"))

        for pid,pdet in person_ids.items():
            px1,py1,px2,py2=pdet[:4]; pcx,pcy=(px1+px2)//2,(py1+py2)//2
            for vid,vdet in vehicle_ids.items():
                vx1,vy1,vx2,vy2=vdet[:4]
                if (vx1-30<pcx<vx2+30) and (vy1-30<pcy<vy2+30):
                    vs=list(self.speed_history.get(vid,[]))
                    if vs and sum(vs[-3:])/max(len(vs[-3:]),1)<2:
                        events.append(("PERSON DOWN / HIT",pid,(px1,py1,px2,py2),"CRITICAL"))
        return events

    @staticmethod
    def _iou(a,b):
        ix1,iy1=max(a[0],b[0]),max(a[1],b[1])
        ix2,iy2=min(a[2],b[2]),min(a[3],b[3])
        iw,ih=max(0,ix2-ix1),max(0,iy2-iy1); inter=iw*ih
        ua=(a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-inter
        return inter/ua if ua>0 else 0


# ── Main tester ───────────────────────────────────────────────────────────────
class CCTVTester:
    def __init__(self, video_path):
        self.path   = video_path
        self.cap    = cv2.VideoCapture(video_path)
        if not self.cap.isOpened():
            print(f"[ERROR] Cannot open: {video_path}")
            sys.exit(1)

        self.total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.fps_video    = self.cap.get(cv2.CAP_PROP_FPS) or 25
        self.w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        print(f"[GARUD] Video: {Path(video_path).name}")
        print(f"[GARUD] Size : {self.w}x{self.h}  |  FPS: {self.fps_video:.1f}  |  Frames: {self.total_frames}")
        print(f"[GARUD] Device: {DEVICE}")

        self.tracker      = Tracker()
        self.accident_det = AccidentDetector()
        self.heatmap_acc  = np.zeros((self.h, self.w), dtype=np.float32)
        self.alert_log    = []
        self.frame_times  = deque(maxlen=30)
        self.model        = None
        self.pose_model   = None
        self._load_models()

        self.paused    = False
        self.step      = False
        self.show_hud  = True
        self.show_heat = False
        self.speed_mul = 1.0
        self.frame_num = 0

    def _load_models(self):
        if not YOLO_OK:
            print("[GARUD] YOLO not installed — showing raw video only")
            print("[GARUD] Run: pip install ultralytics")
            return
        try:
            print("[GARUD] Loading YOLOv8n detection model...")
            self.model = YOLO("yolov8n.pt"); self.model.to(DEVICE)
            print("[GARUD] Detection model ready ✓")
        except Exception as e:
            print(f"[WARN] {e}")
        try:
            print("[GARUD] Loading YOLOv8n-pose model...")
            self.pose_model = YOLO("yolov8n-pose.pt"); self.pose_model.to(DEVICE)
            print("[GARUD] Pose model ready ✓")
        except Exception as e:
            print(f"[WARN] {e}")

    def run(self):
        cv2.namedWindow("GARUD — CCTV Tester", cv2.WINDOW_NORMAL)
        cv2.resizeWindow("GARUD — CCTV Tester", min(self.w, 1280), min(self.h, 720))
        print("\n[GARUD] Controls: SPACE=pause  Q=quit  H=hud  E=heatmap  +/-=speed  S=screenshot\n")

        while True:
            if not self.paused or self.step:
                t0 = time.time()
                ret, frame = self.cap.read()
                if not ret:
                    print("\n[GARUD] Video ended.")
                    break
                self.frame_num += 1
                self.step = False

                overlay = self._process(frame)

                elapsed = time.time()-t0
                self.frame_times.append(elapsed)
                fps_proc = 1/(sum(self.frame_times)/len(self.frame_times)+1e-9)

                if self.show_hud:
                    self._draw_hud(overlay, fps_proc)

                cv2.imshow("GARUD — CCTV Tester", overlay)

                # Playback speed control
                wait = max(1, int((1000/self.fps_video)/self.speed_mul))
            else:
                wait = 50

            key = cv2.waitKey(wait) & 0xFF
            if key in (ord('q'), 27):
                break
            elif key == ord(' '):
                self.paused = not self.paused
                print(f"[GARUD] {'Paused' if self.paused else 'Resumed'}")
            elif key == ord('s'):
                self.step = True
            elif key == ord('h'):
                self.show_hud = not self.show_hud
            elif key == ord('e'):
                self.show_heat = not self.show_heat
                print(f"[GARUD] Heatmap {'ON' if self.show_heat else 'OFF'}")
            elif key == ord('+') or key == ord('='):
                self.speed_mul = min(self.speed_mul*2, 8)
                print(f"[GARUD] Speed: {self.speed_mul}x")
            elif key == ord('-'):
                self.speed_mul = max(self.speed_mul/2, 0.25)
                print(f"[GARUD] Speed: {self.speed_mul}x")
            elif key == ord('p'):
                fname = SHOTS_DIR / f"screenshot_{datetime.now().strftime('%H%M%S')}.jpg"
                cv2.imwrite(str(fname), overlay)
                print(f"[GARUD] Screenshot saved: {fname}")

        self.cap.release()
        cv2.destroyAllWindows()
        self._print_summary()

    def _process(self, frame):
        overlay = frame.copy()
        dets    = self._detect(frame)
        tracked = self.tracker.update(dets)

        # Heatmap
        for oid,det in tracked.items():
            cx,cy=(det[0]+det[2])//2,(det[1]+det[3])//2
            cv2.circle(self.heatmap_acc,(cx,cy),40,1.5,-1)
        if self.show_heat:
            hm=cv2.normalize(self.heatmap_acc,None,0,255,cv2.NORM_MINMAX)
            hm=cv2.GaussianBlur(hm.astype(np.uint8),(61,61),0)
            hm=cv2.applyColorMap(hm,cv2.COLORMAP_JET)
            overlay=cv2.addWeighted(overlay,0.65,hm,0.35,0)

        # Draw objects + trails
        persons=0
        for oid,det in tracked.items():
            x1,y1,x2,y2,label,conf=det
            color=self._color(label)
            if label==PERSON_CLASS: persons+=1
            if oid in self.tracker.paths:
                path=list(self.tracker.paths[oid])
                for i in range(1,len(path)):
                    a=i/len(path)
                    c=tuple(int(x*a) for x in color)
                    cv2.line(overlay,path[i-1],path[i],c,2)
            cv2.rectangle(overlay,(x1,y1),(x2,y2),color,2)
            put_label(overlay,f"#{oid} {label} {conf:.0%}",(x1,max(y1-8,10)),color)

            # Speed indicator
            if oid in self.tracker.paths:
                pts=list(self.tracker.paths[oid])
                if len(pts)>=5:
                    dx=pts[-1][0]-pts[-5][0]; dy=pts[-1][1]-pts[-5][1]
                    spd=math.sqrt(dx*dx+dy*dy)
                    if spd>20:
                        put_label(overlay,f"⚡{spd:.0f}px/f",(x1,y2+16),C_ORANGE,0.45)

        # Accident detection
        acc_events = self.accident_det.update(tracked)
        for evt,oid,(ex1,ey1,ex2,ey2),sev in acc_events:
            col=C_RED if sev=="CRITICAL" else C_ORANGE
            cv2.rectangle(overlay,(ex1-6,ey1-6),(ex2+6,ey2+6),col,3)
            put_label(overlay,f"⚠ {evt}",(ex1,ey1-20),col,0.65,2)
            self._log_alert(evt, sev)

        # Crowd
        if persons > 8:
            cv2.rectangle(overlay,(0,self.h-40),(self.w,self.h),(20,60,0),-1)
            cv2.putText(overlay,f"⚠  HIGH CROWD: {persons} PERSONS",
                (self.w//2-200,self.h-12),cv2.FONT_HERSHEY_SIMPLEX,
                0.65,C_YELLOW,2,cv2.LINE_AA)

        # Progress bar
        prog = self.frame_num/max(self.total_frames,1)
        bar_w = int(self.w*prog)
        cv2.rectangle(overlay,(0,self.h-4),(self.w,self.h),(30,30,30),-1)
        cv2.rectangle(overlay,(0,self.h-4),(bar_w,self.h),(0,255,178),-1)

        return overlay

    def _detect(self, frame):
        if not self.model: return []
        try:
            res=self.model(frame,verbose=False,conf=0.3)
            dets=[]
            for r in res:
                for box in r.boxes:
                    x1,y1,x2,y2=map(int,box.xyxy[0].tolist())
                    dets.append((x1,y1,x2,y2,r.names[int(box.cls[0])],float(box.conf[0])))
            return dets
        except:
            return []

    def _draw_hud(self, img, fps_proc):
        panel=np.zeros((200,270,3),dtype=np.uint8); panel[:]=(20,26,42)
        cv2.rectangle(panel,(0,0),(269,199),(0,255,178),1)
        lines=[
            ("GARUD CCTV TESTER",  (0,255,178), 0.55, 2),
            (f"File  : {Path(self.path).name[:22]}", C_WHITE,  0.4,  1),
            (f"Frame : {self.frame_num}/{self.total_frames}", C_WHITE, 0.4,1),
            (f"Time  : {self.frame_num/max(self.fps_video,1):.1f}s", C_WHITE, 0.4,1),
            (f"Proc  : {fps_proc:.1f} fps",        C_WHITE,  0.4,  1),
            (f"Speed : {self.speed_mul}x",          C_YELLOW, 0.4,  1),
            (f"Device: {DEVICE.upper()}",           (0,255,178) if MPS else C_DIM, 0.4,1),
            (f"Alerts: {len(self.alert_log)}",      (0,80,255), 0.4, 1),
            ("SPACE=pause  Q=quit",                 C_DIM,    0.38, 1),
            ("+/-=speed   H=hud  E=heat",           C_DIM,    0.38, 1),
        ]
        for i,(txt,col,sc,th) in enumerate(lines):
            cv2.putText(panel,txt,(8,20+i*18),cv2.FONT_HERSHEY_SIMPLEX,sc,col,th,cv2.LINE_AA)
        img[10:10+200,10:10+270]=panel
        if self.paused:
            cv2.putText(img,"⏸ PAUSED",
                (img.shape[1]//2-60,40),cv2.FONT_HERSHEY_SIMPLEX,
                0.9,C_YELLOW,2,cv2.LINE_AA)

    def _color(self, label):
        if label==PERSON_CLASS:      return C_GREEN
        if label in VEHICLE_CLASSES: return C_YELLOW
        if label in WEAPON_CLASSES:  return C_RED
        return (160,210,0)

    def _log_alert(self, msg, severity):
        ts=datetime.now().strftime("%H:%M:%S")
        entry={"time":ts,"msg":msg,"severity":severity}
        # Debounce — don't log same event twice within 3s
        if self.alert_log:
            last=self.alert_log[-1]
            if last["msg"]==msg and time.time()-getattr(self,"_last_log_t",0)<3:
                return
        self._last_log_t=time.time()
        self.alert_log.append(entry)
        col="\033[91m" if severity=="CRITICAL" else "\033[93m"
        print(f"{col}[ALERT] [{ts}] {msg} [{severity}]\033[0m")

    def _print_summary(self):
        print("\n" + "="*50)
        print("  GARUD — Analysis Complete")
        print("="*50)
        print(f"  Video    : {Path(self.path).name}")
        print(f"  Frames   : {self.frame_num}")
        print(f"  Duration : {self.frame_num/max(self.fps_video,1):.1f}s")
        print(f"  Alerts   : {len(self.alert_log)}")
        if self.alert_log:
            print("\n  Alert log:")
            for a in self.alert_log:
                print(f"    [{a['time']}] {a['msg']} [{a['severity']}]")
        print("="*50)


# ── File picker (macOS) ───────────────────────────────────────────────────────
def pick_file():
    try:
        import subprocess
        result = subprocess.run([
            "osascript","-e",
            'tell app "Finder" to set f to POSIX path of '
            '(choose file with prompt "Select CCTV footage video" '
            'of type {"public.movie","public.mpeg-4","public.avi"})'
        ], capture_output=True, text=True)
        path = result.stdout.strip()
        if path: return path
    except:
        pass
    return None

def show_sample_menu():
    print("\n" + "="*55)
    print("  GARUD — CCTV Footage Tester")
    print("="*55)
    print("\n  No video file provided.")
    print("\n  OPTIONS:")
    print("  1. Pick a file from Finder (opens dialog)")
    print("  2. Enter a video file path manually")
    print("  3. Use your own downloaded CCTV footage")
    print("\n  RECOMMENDED FREE CCTV FOOTAGE SOURCES:")
    print("  • https://www.pexels.com/search/videos/traffic/")
    print("  • https://www.videvo.net/search/#loaded/searchQuery=cctv")
    print("  • https://pixabay.com/videos/search/traffic/")
    print("  • Search YouTube: 'CCTV footage compilation' → download with yt-dlp")
    print("\n  yt-dlp install & use:")
    print("    pip install yt-dlp")
    print("    yt-dlp 'YOUTUBE_URL' -o footage.mp4")
    print("="*55)
    choice = input("\n  Enter choice (1/2): ").strip()
    if choice == "1":
        return pick_file()
    elif choice == "2":
        return input("  Enter full path to video file: ").strip().strip("'\"")
    return None


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GARUD CCTV Footage Tester")
    parser.add_argument("video", nargs="?", help="Path to video file")
    args = parser.parse_args()

    video_path = args.video

    if not video_path:
        video_path = show_sample_menu()

    if not video_path:
        print("[ERROR] No video selected. Exiting.")
        sys.exit(1)

    if not os.path.exists(video_path):
        print(f"[ERROR] File not found: {video_path}")
        sys.exit(1)

    tester = CCTVTester(video_path)
    tester.run()
