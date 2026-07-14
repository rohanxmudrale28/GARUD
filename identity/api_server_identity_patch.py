"""
GARUD api_server.py — Identity Engine Patch
--------------------------------------------
Add these sections to your existing api_server.py to enable identity matching.
Lines marked ADD go in the specified locations.
"""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. ADD near the top — after your existing imports
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
from identity.identity_engine import IdentityEngine

# Shared identity engine — one instance, all cameras share it
identity_engine = IdentityEngine()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. ADD to identity_matches in shared state section
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
identity_matches = []   # recent identity matches, newest first


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. REPLACE the frame processing block inside CameraProcessor.run()
#    Add identity matching after step 4 (anomaly detection):
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IDENTITY_PATCH_RUN = """
            # ── 4b. Identity matching (runs every 10th frame to save CPU) ──
            if identity_engine.ready and frame_count % 10 == 0:
                id_matches = identity_engine.match_frame(frame)
                if id_matches:
                    annotated = identity_engine.annotate_frame(annotated, id_matches)
                    for m in id_matches:
                        # Escalate severity if watchlist hit
                        if m.get('is_watchlist'):
                            severity   = 'red'
                            last_event = f\"Watchlist Hit: {m['name']}\"
                        elif m.get('risk_level') == 'high' and severity == 'white':
                            severity   = 'amber'
                            last_event = f\"High-Risk Person: {m['name']}\"

                        # Add to global identity match log
                        with state_lock:
                            identity_matches.insert(0, {
                                'cam_id':     self.cam_id,
                                'city':       self.cam_meta.get('city',''),
                                'person_id':  m['person_id'],
                                'name':       m['name'],
                                'risk_level': m['risk_level'],
                                'is_watchlist': m.get('is_watchlist', False),
                                'confidence': m['confidence'],
                                'time':       datetime.datetime.now().strftime('%H:%M:%S'),
                            })
                            if len(identity_matches) > 200:
                                identity_matches.pop()
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. ADD these new API routes to api_server.py
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Copy-paste these routes into api_server.py alongside your existing routes:

NEW_ROUTES = """
@app.route("/api/identity/matches")
def get_identity_matches():
    \"\"\"Recent identity matches across all cameras.\"\"\"
    limit = int(request.args.get("limit", 50))
    with state_lock:
        return jsonify(identity_matches[:limit])

@app.route("/api/identity/person/<person_id>")
def get_identity_person(person_id):
    \"\"\"Full record for a matched person from local DB.\"\"\"
    person = identity_engine.get_person(person_id)
    if not person:
        return jsonify({"error": "Not found"}), 404
    # Don't expose raw offences JSON — parse it
    try:
        person["offences"] = json.loads(person.get("offences","[]"))
    except Exception:
        person["offences"] = []
    return jsonify(person)

@app.route("/api/identity/watchlist")
def get_watchlist():
    \"\"\"All persons on the watchlist in local DB.\"\"\"
    import sqlite3
    from identity.db_sync import DB_PATH
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        \"SELECT id,name,risk_level,city,state,offences FROM persons WHERE is_watchlist=1\"
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        try: d["offences"] = json.loads(d.get("offences","[]"))
        except: d["offences"] = []
        result.append(d)
    return jsonify(result)

@app.route("/api/identity/stats")
def get_identity_stats():
    \"\"\"DB statistics.\"\"\"
    import sqlite3
    from identity.db_sync import DB_PATH
    try:
        conn = sqlite3.connect(DB_PATH)
        total     = conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
        watchlist = conn.execute("SELECT COUNT(*) FROM persons WHERE is_watchlist=1").fetchone()[0]
        high_risk = conn.execute("SELECT COUNT(*) FROM persons WHERE risk_level='high'").fetchone()[0]
        sightings = conn.execute("SELECT COUNT(*) FROM sightings").fetchone()[0] if \
                    conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sightings'").fetchone() else 0
        conn.close()
        return jsonify({
            "total_persons": total,
            "watchlist":     watchlist,
            "high_risk":     high_risk,
            "total_sightings": sightings,
            "engine_ready":  identity_engine.ready,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/identity/rebuild-embeddings", methods=["POST"])
def rebuild_embeddings():
    \"\"\"Rebuild the face embedding index (run after db_sync).\"\"\"
    def _rebuild():
        identity_engine.build_embeddings()
    threading.Thread(target=_rebuild, daemon=True).start()
    return jsonify({"status": "rebuilding in background"})

@app.route("/api/identity/search")
def search_identity():
    \"\"\"Search local DB by name.\"\"\"
    import sqlite3
    from identity.db_sync import DB_PATH
    q = request.args.get("q","").strip()
    if not q:
        return jsonify([])
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        \"SELECT id,name,dob,city,state,risk_level,is_watchlist FROM persons WHERE name LIKE ? LIMIT 20\",
        (f\"%{q}%\",)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])
"""
