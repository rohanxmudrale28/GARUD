# GARUD Identity Engine — Setup Guide

## Architecture

```
External API (Aadhaar / Police DB)
         │
         ▼  (db_sync.py — runs on schedule, NOT at runtime)
  Local SQLite DB  ←─────────────────────────────────┐
  identity.db                                         │
  identity_photos/                                    │
  face_embeddings.npy  ◄── built from photos once    │
         │                                            │
         ▼  (identity_engine.py — runs inside api_server)
  Camera frame → face detected → ArcFace embedding
         │
         ▼
  Cosine similarity vs local embeddings (zero API calls)
         │
         ▼
  Match found → annotate frame → alert → log sighting
```

## Step 1 — Install

```bash
pip install insightface
pip install onnxruntime-silicon   # Apple Silicon M-series
# OR
pip install onnxruntime           # Intel / Linux
```

## Step 2 — Place files in GARUD root

```
GARUD/
├── identity/
│   ├── __init__.py          ← create empty file
│   ├── db_sync.py
│   ├── identity_engine.py
│   └── api_server_identity_patch.py
├── database/
│   ├── identity.db          ← created automatically
│   ├── identity_photos/     ← created automatically
│   └── face_embeddings.npy  ← created after build step
```

Create the `__init__.py`:
```bash
touch identity/__init__.py
```

## Step 3 — Load mock data (for testing now)

```bash
python identity/db_sync.py --mock
python identity/db_sync.py --stats
```

You'll see 10 mock persons loaded with realistic data.

## Step 4 — Patch api_server.py

Open `api_server.py` and add these three things from `api_server_identity_patch.py`:

1. Add at top (after imports):
```python
from identity.identity_engine import IdentityEngine
identity_engine = IdentityEngine()
identity_matches = []
```

2. Add inside `CameraProcessor.run()` after anomaly detection (step 4):
```python
# Identity matching — every 10th frame
if identity_engine.ready and frame_count % 10 == 0:
    id_matches = identity_engine.match_frame(frame)
    if id_matches:
        annotated = identity_engine.annotate_frame(annotated, id_matches)
        for m in id_matches:
            if m.get('is_watchlist'):
                severity   = 'red'
                last_event = f"Watchlist Hit: {m['name']}"
            with state_lock:
                identity_matches.insert(0, {
                    'cam_id': self.cam_id, 'city': self.cam_meta.get('city',''),
                    'person_id': m['person_id'], 'name': m['name'],
                    'risk_level': m['risk_level'], 'is_watchlist': m.get('is_watchlist',False),
                    'confidence': m['confidence'],
                    'time': datetime.datetime.now().strftime('%H:%M:%S'),
                })
```

Also add `frame_count = 0` before the while loop, and `frame_count += 1` each iteration.

3. Add the new routes (copy from `api_server_identity_patch.py` NEW_ROUTES section).

## Step 5 — Restart and verify

```bash
python api_server.py

# Check the new endpoints:
curl http://localhost:5001/api/identity/stats
curl http://localhost:5001/api/identity/watchlist
curl http://localhost:5001/api/identity/search?q=Rajan
```

## Step 6 — Build embeddings (when you have real photos)

After real API sync runs and photos are downloaded:
```bash
curl -X POST http://localhost:5001/api/identity/rebuild-embeddings
```

## Step 7 — Real API integration (when licensed)

Edit `identity/db_sync.py`:

1. Update `API_CONFIG["base_url"]` to the real endpoint
2. Update `API_CONFIG["api_key"]` or set `IDENTITY_API_KEY` env var
3. Update `normalise_record()` to match real API field names
4. Run: `python identity/db_sync.py --full`

Everything else — matching, alerting, DB storage — stays exactly the same.

## Sync schedule (production)

```bash
# Add to crontab — daily delta sync at 2 AM
crontab -e
0 2 * * * cd /path/to/GARUD && .venv/bin/python identity/db_sync.py --delta >> logs/sync.log 2>&1

# Weekly full sync Sunday 3 AM
0 3 * * 0 cd /path/to/GARUD && .venv/bin/python identity/db_sync.py --full >> logs/sync.log 2>&1
```

## API Endpoints (after patching api_server.py)

| Endpoint | Description |
|---|---|
| GET /api/identity/stats | DB stats + engine status |
| GET /api/identity/matches | Recent identity matches from cameras |
| GET /api/identity/watchlist | All watchlist persons |
| GET /api/identity/person/\<id\> | Full record for one person |
| GET /api/identity/search?q=name | Search by name |
| POST /api/identity/rebuild-embeddings | Rebuild face index after sync |
