import os
import sqlite3
from pathlib import Path


def db_path() -> Path:
    return Path(os.environ.get("BLASTWATCH_DB", Path(__file__).resolve().parents[2] / "blastwatch.db"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS subscribers(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contact TEXT NOT NULL UNIQUE,
  channel TEXT NOT NULL DEFAULT 'outbox',
  block_id TEXT NOT NULL,
  variety TEXT NOT NULL,
  method TEXT NOT NULL CHECK(method IN ('transplanted','direct_seeded')),
  establish_date TEXT NOT NULL,
  language TEXT NOT NULL DEFAULT 'ta',
  consent_at TEXT NOT NULL,
  consent_text_version TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS weather_hourly(
  block_id TEXT, ts TEXT, temp REAL, rh REAL, precip REAL, wind REAL,
  source TEXT, fetched_at TEXT,
  PRIMARY KEY(block_id, ts)
);
CREATE TABLE IF NOT EXISTS block_alerts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  block_id TEXT, issued_on TEXT, level TEXT, window_start TEXT, window_end TEXT,
  favorable_days INTEGER, hours_json TEXT, params_hash TEXT,
  UNIQUE(block_id, issued_on)
);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subscriber_id INTEGER, kind TEXT, body TEXT, sent_at TEXT, status TEXT,
  block_alert_id INTEGER
);
CREATE TABLE IF NOT EXISTS followups(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subscriber_id INTEGER, due_on TEXT,
  origin TEXT CHECK(origin IN ('alert','no_alert_sample')),
  block_alert_id INTEGER, asked_at TEXT, answered_at TEXT,
  answer TEXT CHECK(answer IN ('yes','no','unsure') OR answer IS NULL),
  officer_status TEXT NOT NULL DEFAULT 'unverified'
    CHECK(officer_status IN ('unverified','confirmed','rejected')),
  officer_note TEXT
);
"""


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(db_path())
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con
