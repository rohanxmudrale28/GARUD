"""
GARUD Identity Sync Engine
--------------------------
Fetches person records from an external API (Aadhaar or any identity API)
and stores them locally in SQLite + face embeddings in a vector store.

All runtime matching is done 100% locally — zero API calls during surveillance.

Usage:
  python db_sync.py --full       # full sync (first time)
  python db_sync.py --delta      # fetch only records updated since last sync
  python db_sync.py --person ID  # fetch one specific person by ID
  python db_sync.py --mock       # populate with mock data for testing

Schedule via cron for daily delta sync:
  0 2 * * * cd /path/to/GARUD && .venv/bin/python identity/db_sync.py --delta
"""

import sqlite3
import requests
import argparse
import hashlib
import json
import os
import sys
import datetime
import time
import urllib.request

# ── Configuration ─────────────────────────────────────────────────────────────
# When you get real Aadhaar / police API access, update these values.
# Everything else in this file stays the same.

API_CONFIG = {
    # Base URL of the identity API
    # Aadhaar AUA endpoint example (replace when licensed):
    # "base_url": "https://auth.uidai.gov.in/1.6/",
    "base_url": "http://localhost:8888/mock-identity-api",  # mock for now

    # Auth — replace with real API key / OAuth token when available
    "api_key":  os.environ.get("IDENTITY_API_KEY", "MOCK_KEY_REPLACE_ME"),

    # How many records to fetch per page
    "page_size": 100,

    # Endpoints (adjust to match real API structure)
    "endpoints": {
        "person_by_id":    "/person/{id}",
        "watchlist":       "/watchlist",
        "delta_since":     "/records/updated-since/{timestamp}",
        "full_export":     "/records/export",
        "photo_by_id":     "/person/{id}/photo",
    },

    # Request timeout in seconds
    "timeout": 30,
}

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH     = os.path.join(BASE_DIR, "database", "identity.db")
PHOTO_DIR   = os.path.join(BASE_DIR, "database", "identity_photos")
EMBED_PATH  = os.path.join(BASE_DIR, "database", "face_embeddings.json")
LOG_PATH    = os.path.join(BASE_DIR, "database", "sync_log.txt")

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
os.makedirs(PHOTO_DIR, exist_ok=True)


# ── Database setup ────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS persons (
            id              TEXT PRIMARY KEY,
            name            TEXT,
            dob             TEXT,
            gender          TEXT,
            address         TEXT,
            state           TEXT,
            city            TEXT,
            photo_path      TEXT,
            has_embedding   INTEGER DEFAULT 0,
            is_watchlist    INTEGER DEFAULT 0,
            offences        TEXT,        -- JSON array of known offences
            risk_level      TEXT DEFAULT 'none',  -- none / low / medium / high
            source          TEXT DEFAULT 'api',   -- api / manual / local
            api_updated_at  TEXT,
            synced_at       TEXT,
            notes           TEXT
        );

        CREATE TABLE IF NOT EXISTS watchlist (
            id              TEXT PRIMARY KEY,
            person_id       TEXT REFERENCES persons(id),
            added_by        TEXT,
            reason          TEXT,
            added_at        TEXT,
            expires_at      TEXT,
            active          INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS sync_history (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            sync_type       TEXT,   -- full / delta / manual
            started_at      TEXT,
            finished_at     TEXT,
            records_fetched INTEGER DEFAULT 0,
            records_added   INTEGER DEFAULT 0,
            records_updated INTEGER DEFAULT 0,
            status          TEXT,   -- success / failed / partial
            error           TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_persons_name      ON persons(name);
        CREATE INDEX IF NOT EXISTS idx_persons_watchlist ON persons(is_watchlist);
        CREATE INDEX IF NOT EXISTS idx_persons_state     ON persons(state);
    """)
    conn.commit()
    conn.close()
    log("Database initialised.")


# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


# ── API client ────────────────────────────────────────────────────────────────
class IdentityAPIClient:
    """
    Thin wrapper around the identity API.
    When you get real API access, only this class needs changes.
    The rest of the sync engine is API-agnostic.
    """
    def __init__(self):
        self.base   = API_CONFIG["base_url"].rstrip("/")
        self.key    = API_CONFIG["api_key"]
        self.timeout = API_CONFIG["timeout"]
        self.headers = {
            "Authorization": f"Bearer {self.key}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }

    def _get(self, endpoint, params=None):
        url = self.base + endpoint
        try:
            r = requests.get(url, headers=self.headers,
                             params=params, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.ConnectionError:
            log(f"  API unreachable at {url} — using mock data")
            return None
        except Exception as e:
            log(f"  API error: {e}")
            return None

    def get_person(self, person_id):
        ep = API_CONFIG["endpoints"]["person_by_id"].format(id=person_id)
        return self._get(ep)

    def get_watchlist(self):
        ep = API_CONFIG["endpoints"]["watchlist"]
        return self._get(ep)

    def get_delta(self, since_timestamp):
        ep = API_CONFIG["endpoints"]["delta_since"].format(timestamp=since_timestamp)
        return self._get(ep)

    def get_full_export(self):
        ep = API_CONFIG["endpoints"]["full_export"]
        page, all_records = 1, []
        while True:
            data = self._get(ep, params={"page": page, "size": API_CONFIG["page_size"]})
            if not data or not data.get("records"):
                break
            all_records.extend(data["records"])
            log(f"  Fetched page {page} — {len(all_records)} records total")
            if not data.get("has_more"):
                break
            page += 1
            time.sleep(0.1)  # be polite to the API
        return all_records

    def get_photo(self, person_id):
        ep = API_CONFIG["endpoints"]["photo_by_id"].format(id=person_id)
        url = self.base + ep
        try:
            r = requests.get(url, headers=self.headers, timeout=self.timeout)
            if r.ok:
                return r.content  # raw bytes
        except Exception:
            pass
        return None


# ── Record normaliser ─────────────────────────────────────────────────────────
def normalise_record(raw):
    """
    Converts API response (whatever shape it comes in) into our standard schema.
    ── ADAPT THIS when you get real API docs ──
    Common Aadhaar-style field names are handled below.
    """
    # Handle both camelCase and snake_case
    def g(*keys):
        for k in keys:
            if raw.get(k) is not None:
                return raw[k]
        return None

    offences = g("offences", "known_offences", "criminal_record", "crimes") or []
    if isinstance(offences, str):
        try:    offences = json.loads(offences)
        except: offences = [offences]

    risk = "none"
    if offences:
        risk = "high" if any(o.get("severity") == "high" for o in offences
                             if isinstance(o, dict)) else "medium"

    return {
        "id":             str(g("id", "uid", "aadhaar_id", "person_id") or ""),
        "name":           g("name", "full_name", "fullName") or "",
        "dob":            g("dob", "date_of_birth", "dateOfBirth") or "",
        "gender":         g("gender", "sex") or "",
        "address":        g("address", "addr") or "",
        "state":          g("state", "pr_state") or "",
        "city":           g("city", "district", "pr_city") or "",
        "is_watchlist":   1 if g("watchlist", "is_watchlist", "flagged") else 0,
        "offences":       json.dumps(offences),
        "risk_level":     g("risk_level", "riskLevel") or risk,
        "api_updated_at": g("updated_at", "updatedAt", "last_modified") or "",
        "synced_at":      datetime.datetime.now().isoformat(),
        "notes":          g("notes", "remarks") or "",
        "source":         "api",
        "has_embedding":  0,
        "photo_path":     "",
    }


# ── Upsert into DB ────────────────────────────────────────────────────────────
def upsert_person(conn, record):
    existing = conn.execute(
        "SELECT id, has_embedding FROM persons WHERE id=?", (record["id"],)
    ).fetchone()

    if existing:
        # Preserve existing embedding flag
        record["has_embedding"] = existing["has_embedding"]
        conn.execute("""
            UPDATE persons SET
              name=:name, dob=:dob, gender=:gender, address=:address,
              state=:state, city=:city, is_watchlist=:is_watchlist,
              offences=:offences, risk_level=:risk_level,
              api_updated_at=:api_updated_at, synced_at=:synced_at,
              notes=:notes
            WHERE id=:id
        """, record)
        return "updated"
    else:
        conn.execute("""
            INSERT INTO persons
              (id,name,dob,gender,address,state,city,photo_path,
               has_embedding,is_watchlist,offences,risk_level,
               source,api_updated_at,synced_at,notes)
            VALUES
              (:id,:name,:dob,:gender,:address,:state,:city,:photo_path,
               :has_embedding,:is_watchlist,:offences,:risk_level,
               :source,:api_updated_at,:synced_at,:notes)
        """, record)
        return "added"


def save_photo(person_id, photo_bytes):
    if not photo_bytes:
        return ""
    path = os.path.join(PHOTO_DIR, f"{person_id}.jpg")
    with open(path, "wb") as f:
        f.write(photo_bytes)
    return path


# ── Mock data for testing ─────────────────────────────────────────────────────
MOCK_RECORDS = [
    {"id":"MOCK001","name":"Rajan Verma",    "dob":"1985-03-12","gender":"M","city":"Mumbai",   "state":"Maharashtra","address":"Andheri West","watchlist":True, "offences":[{"type":"theft","severity":"medium","year":2022}]},
    {"id":"MOCK002","name":"Priya Singh",    "dob":"1992-07-22","gender":"F","city":"Delhi",    "state":"Delhi",      "address":"Connaught Place","watchlist":False,"offences":[]},
    {"id":"MOCK003","name":"Arjun Sharma",   "dob":"1978-11-05","gender":"M","city":"Bengaluru","state":"Karnataka",  "address":"MG Road","watchlist":True, "offences":[{"type":"assault","severity":"high","year":2023},{"type":"robbery","severity":"high","year":2021}]},
    {"id":"MOCK004","name":"Fatima Khan",    "dob":"1995-01-30","gender":"F","city":"Hyderabad","state":"Telangana",  "address":"Charminar","watchlist":False,"offences":[]},
    {"id":"MOCK005","name":"Suresh Nair",    "dob":"1970-08-18","gender":"M","city":"Kochi",    "state":"Kerala",     "address":"Marine Drive","watchlist":True, "offences":[{"type":"fraud","severity":"medium","year":2020}]},
    {"id":"MOCK006","name":"Anjali Desai",   "dob":"1988-05-14","gender":"F","city":"Ahmedabad","state":"Gujarat",   "address":"Sabarmati","watchlist":False,"offences":[]},
    {"id":"MOCK007","name":"Vikram Reddy",   "dob":"1982-09-27","gender":"M","city":"Chennai",  "state":"Tamil Nadu", "address":"Anna Nagar","watchlist":True, "offences":[{"type":"extortion","severity":"high","year":2024}]},
    {"id":"MOCK008","name":"Meera Patel",    "dob":"1999-12-03","gender":"F","city":"Jaipur",   "state":"Rajasthan",  "address":"Hawa Mahal Rd","watchlist":False,"offences":[]},
    {"id":"MOCK009","name":"Rahul Mishra",   "dob":"1975-06-19","gender":"M","city":"Lucknow",  "state":"UP",         "address":"Hazratganj","watchlist":True, "offences":[{"type":"drug_trafficking","severity":"high","year":2022}]},
    {"id":"MOCK010","name":"Deepa Iyer",     "dob":"1993-04-08","gender":"F","city":"Pune",     "state":"Maharashtra","address":"FC Road","watchlist":False,"offences":[]},
]

def load_mock_data():
    log("Loading mock identity records…")
    conn = get_db()
    added = updated = 0
    for raw in MOCK_RECORDS:
        record = normalise_record(raw)
        result = upsert_person(conn, record)
        if result == "added":    added   += 1
        else:                    updated += 1
    conn.commit()
    conn.close()
    log(f"Mock data loaded — {added} added, {updated} updated.")
    log_sync("mock", added, updated, "success")


# ── Sync routines ─────────────────────────────────────────────────────────────
def sync_full():
    log("Starting FULL sync…")
    client  = IdentityAPIClient()
    records = client.get_full_export()
    if records is None:
        log("Full export failed — API unreachable. Run with --mock to use test data.")
        log_sync("full", 0, 0, "failed", "API unreachable")
        return
    _process_records(records, "full")


def sync_delta():
    conn = get_db()
    last = conn.execute(
        "SELECT started_at FROM sync_history WHERE status='success' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    since = last["started_at"] if last else "2020-01-01T00:00:00"
    log(f"Starting DELTA sync since {since}…")
    client  = IdentityAPIClient()
    records = client.get_delta(since)
    if records is None:
        log("Delta sync failed — API unreachable.")
        log_sync("delta", 0, 0, "failed", "API unreachable")
        return
    _process_records(records, "delta")


def sync_person(person_id):
    log(f"Fetching single person: {person_id}…")
    client = IdentityAPIClient()
    raw    = client.get_person(person_id)
    if not raw:
        log(f"Person {person_id} not found in API.")
        return
    conn   = get_db()
    record = normalise_record(raw)
    result = upsert_person(conn, record)
    # Try to get photo
    photo  = client.get_photo(person_id)
    if photo:
        path = save_photo(person_id, photo)
        conn.execute("UPDATE persons SET photo_path=? WHERE id=?", (path, person_id))
    conn.commit()
    conn.close()
    log(f"Person {person_id} ({record['name']}) — {result}.")


def _process_records(records, sync_type):
    conn = get_db()
    client = IdentityAPIClient()
    added = updated = 0
    started = datetime.datetime.now().isoformat()
    for raw in records:
        try:
            record = normalise_record(raw)
            if not record["id"]:
                continue
            result = upsert_person(conn, record)
            # Fetch and store photo
            photo  = client.get_photo(record["id"])
            if photo:
                path = save_photo(record["id"], photo)
                conn.execute("UPDATE persons SET photo_path=? WHERE id=?",
                             (path, record["id"]))
            if result == "added":   added   += 1
            else:                   updated += 1
        except Exception as e:
            log(f"  Error processing record: {e}")
    conn.commit()
    conn.close()
    log(f"Sync complete — {added} added, {updated} updated.")
    log_sync(sync_type, added, updated, "success")


def log_sync(sync_type, added, updated, status, error=""):
    conn = get_db()
    conn.execute("""
        INSERT INTO sync_history (sync_type,started_at,finished_at,records_fetched,
                                  records_added,records_updated,status,error)
        VALUES (?,?,?,?,?,?,?,?)
    """, (sync_type,
          datetime.datetime.now().isoformat(),
          datetime.datetime.now().isoformat(),
          added+updated, added, updated, status, error))
    conn.commit()
    conn.close()


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GARUD Identity DB Sync")
    parser.add_argument("--full",   action="store_true", help="Full API sync")
    parser.add_argument("--delta",  action="store_true", help="Delta sync (changes only)")
    parser.add_argument("--person", metavar="ID",        help="Sync one person by ID")
    parser.add_argument("--mock",   action="store_true", help="Load mock data for testing")
    parser.add_argument("--stats",  action="store_true", help="Show DB statistics")
    args = parser.parse_args()

    init_db()

    if args.mock:
        load_mock_data()
    elif args.full:
        sync_full()
    elif args.delta:
        sync_delta()
    elif args.person:
        sync_person(args.person)
    elif args.stats:
        conn = get_db()
        total    = conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
        watchlist= conn.execute("SELECT COUNT(*) FROM persons WHERE is_watchlist=1").fetchone()[0]
        high_risk= conn.execute("SELECT COUNT(*) FROM persons WHERE risk_level='high'").fetchone()[0]
        last_sync= conn.execute("SELECT started_at FROM sync_history ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        print(f"\nGARUD Identity DB — {DB_PATH}")
        print(f"  Total persons : {total}")
        print(f"  Watchlist     : {watchlist}")
        print(f"  High risk     : {high_risk}")
        print(f"  Last sync     : {last_sync[0] if last_sync else 'never'}\n")
    else:
        parser.print_help()
