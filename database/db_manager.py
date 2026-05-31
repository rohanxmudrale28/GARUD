import sqlite3
import os
import datetime


class DBManager:
    def __init__(self):
        self.conn = sqlite3.connect("garud_events.db")
        self.create_table()

        if not os.path.exists("snapshots"):
            os.makedirs("snapshots")

    def create_table(self):
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT,
            severity INTEGER,
            crowd_count INTEGER,
            motion_score REAL,
            timestamp TEXT,
            snapshot_path TEXT
        )
        """)
        self.conn.commit()

    def insert_event(self, event_type, severity, crowd_count, motion_score, snapshot_path):
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        self.conn.execute("""
        INSERT INTO events (event_type, severity, crowd_count, motion_score, timestamp, snapshot_path)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (event_type, severity, crowd_count, motion_score, timestamp, snapshot_path))

        self.conn.commit()