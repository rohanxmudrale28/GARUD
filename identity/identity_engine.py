"""
GARUD Identity Engine
---------------------
Matches persons detected by the camera pipeline against the local identity DB.
100% offline — no API calls at runtime.

Uses InsightFace for face detection + ArcFace embeddings (same approach as
modern law enforcement ReID systems).

Install:
  pip install insightface onnxruntime-silicon  # Apple Silicon
  pip install insightface onnxruntime          # Intel / Linux
"""

import os
import cv2
import json
import sqlite3
import numpy as np
import datetime
import threading

BASE_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH    = os.path.join(BASE_DIR, "database", "identity.db")
PHOTO_DIR  = os.path.join(BASE_DIR, "database", "identity_photos")
EMBED_PATH = os.path.join(BASE_DIR, "database", "face_embeddings.npy")
META_PATH  = os.path.join(BASE_DIR, "database", "face_embeddings_meta.json")

# Cosine similarity threshold for a positive match
# 0.5 = strict (fewer false positives), 0.4 = relaxed (catches more, more FP)
MATCH_THRESHOLD = 0.50


class IdentityEngine:
    """
    Detects faces in a frame, extracts ArcFace embeddings,
    and matches against the local identity database.
    """

    def __init__(self):
        self._lock       = threading.Lock()
        self._embeddings = None   # np.array shape (N, 512)
        self._meta       = []     # list of {person_id, name, risk_level, is_watchlist}
        self._model      = None
        self._ready      = False

        # Load in background so it doesn't block camera startup
        t = threading.Thread(target=self._init_model, daemon=True)
        t.start()

    def _init_model(self):
        try:
            import insightface
            from insightface.app import FaceAnalysis
            self._model = FaceAnalysis(
                name="buffalo_sc",          # lightweight model, downloads ~300 MB once
                providers=["CoreMLExecutionProvider",   # Apple Silicon
                           "CUDAExecutionProvider",     # NVIDIA GPU
                           "CPUExecutionProvider"],     # fallback
            )
            self._model.prepare(ctx_id=0, det_size=(640, 640))
            self._load_embeddings()
            self._ready = True
            print("[IdentityEngine] Ready.")
        except ImportError:
            print("[IdentityEngine] insightface not installed — identity matching disabled.")
            print("  Install: pip install insightface onnxruntime")
        except Exception as e:
            print(f"[IdentityEngine] Init failed: {e}")

    def _load_embeddings(self):
        """Load pre-computed embeddings from disk into memory."""
        if os.path.exists(EMBED_PATH) and os.path.exists(META_PATH):
            with self._lock:
                self._embeddings = np.load(EMBED_PATH)
                with open(META_PATH) as f:
                    self._meta = json.load(f)
            print(f"[IdentityEngine] Loaded {len(self._meta)} face embeddings from disk.")
        else:
            print("[IdentityEngine] No embeddings found — run build_embeddings() first.")

    def build_embeddings(self):
        """
        Build face embedding index from all photos in the identity DB.
        Run this once after db_sync.py populates the database.
        Also run after every sync to pick up new records.
        """
        if not self._ready:
            print("[IdentityEngine] Model not ready yet.")
            return

        conn  = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows  = conn.execute(
            "SELECT id, name, risk_level, is_watchlist, photo_path FROM persons"
        ).fetchall()
        conn.close()

        embeddings, meta = [], []
        skipped = 0

        for row in rows:
            photo_path = row["photo_path"]
            if not photo_path or not os.path.exists(photo_path):
                skipped += 1
                continue

            img = cv2.imread(photo_path)
            if img is None:
                skipped += 1
                continue

            faces = self._model.get(img)
            if not faces:
                skipped += 1
                continue

            # Use the largest face in the photo (should be one person per ID photo)
            face = max(faces, key=lambda f: (f.bbox[2]-f.bbox[0]) * (f.bbox[3]-f.bbox[1]))
            embeddings.append(face.normed_embedding)
            meta.append({
                "person_id":   row["id"],
                "name":        row["name"],
                "risk_level":  row["risk_level"],
                "is_watchlist":row["is_watchlist"],
            })

        if not embeddings:
            print(f"[IdentityEngine] No embeddings built (skipped {skipped} — no photos).")
            print("  Tip: photos are fetched from the API during db_sync.py --full")
            return

        emb_array = np.array(embeddings, dtype=np.float32)
        with self._lock:
            self._embeddings = emb_array
            self._meta       = meta

        np.save(EMBED_PATH, emb_array)
        with open(META_PATH, "w") as f:
            json.dump(meta, f, indent=2)

        print(f"[IdentityEngine] Built {len(embeddings)} embeddings ({skipped} skipped).")

    def match_frame(self, frame):
        """
        Detect all faces in a frame and match each against the local DB.

        Returns: list of match dicts, one per detected face that exceeded threshold.
        Each dict: {
            person_id, name, risk_level, is_watchlist,
            confidence, bbox (x1,y1,x2,y2), frame_time
        }
        """
        if not self._ready or self._embeddings is None or len(self._embeddings) == 0:
            return []

        faces = self._model.get(frame)
        if not faces:
            return []

        matches = []
        with self._lock:
            emb_matrix = self._embeddings   # (N, 512)
            meta       = self._meta

        for face in faces:
            query = face.normed_embedding.reshape(1, -1)  # (1, 512)

            # Cosine similarity: dot product of unit vectors
            sims = np.dot(emb_matrix, query.T).flatten()
            best_idx = int(np.argmax(sims))
            best_sim = float(sims[best_idx])

            if best_sim >= MATCH_THRESHOLD:
                m = dict(meta[best_idx])
                m["confidence"]  = round(best_sim, 3)
                m["bbox"]        = [int(x) for x in face.bbox]
                m["frame_time"]  = datetime.datetime.now().isoformat()
                matches.append(m)

                # Log match to DB
                self._log_match(m)

        return matches

    def _log_match(self, match):
        """Record every match in the sightings table for audit trail."""
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sightings (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    person_id   TEXT,
                    cam_id      TEXT,
                    confidence  REAL,
                    seen_at     TEXT
                )
            """)
            conn.execute(
                "INSERT INTO sightings (person_id,confidence,seen_at) VALUES (?,?,?)",
                (match["person_id"], match["confidence"], match["frame_time"])
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    def get_person(self, person_id):
        """Fetch full person record from local DB."""
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM persons WHERE id=?", (person_id,)).fetchone()
        conn.close()
        return dict(row) if row else None

    def annotate_frame(self, frame, matches):
        """
        Draw bounding boxes and identity labels on a frame.
        Colour-coded by risk level.
        """
        risk_col = {
            "high":   (0,   0,   255),   # red
            "medium": (0,  140,  255),   # amber
            "low":    (0,  210,  255),   # yellow
            "none":   (0,  200,    0),   # green
        }
        for m in matches:
            x1,y1,x2,y2 = m["bbox"]
            col  = risk_col.get(m.get("risk_level","none"), (0,200,0))
            name = m.get("name","Unknown")
            conf = m.get("confidence",0)
            wl   = " ⚠ WATCHLIST" if m.get("is_watchlist") else ""

            cv2.rectangle(frame, (x1,y1), (x2,y2), col, 2)

            label = f"{name} ({conf:.2f}){wl}"
            (w,h),_ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
            cv2.rectangle(frame, (x1, y1-h-10), (x1+w+4, y1), col, -1)
            cv2.putText(frame, label, (x1+2, y1-4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0,0,0), 1)
        return frame

    @property
    def ready(self):
        return self._ready

    @property
    def db_size(self):
        try:
            conn = sqlite3.connect(DB_PATH)
            n = conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
            conn.close()
            return n
        except Exception:
            return 0
