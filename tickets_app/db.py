"""SQLite: todas as observações, varreduras, consultas, combinações e alertas."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config as C

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS sweeps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL DEFAULT 'running',      -- running | ok | partial | failed | interrupted
  queries_total INTEGER DEFAULT 0,
  queries_ok INTEGER DEFAULT 0,
  queries_failed INTEGER DEFAULT 0,
  queries_blocked INTEGER DEFAULT 0,
  queries_skipped INTEGER DEFAULT 0,
  flights_found INTEGER DEFAULT 0,
  flights_valid INTEGER DEFAULT 0,
  combos_valid INTEGER DEFAULT 0,
  best_total REAL,
  best_combo TEXT,
  alerts_sent INTEGER DEFAULT 0,
  notes TEXT
);
CREATE TABLE IF NOT EXISTS queries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sweep_id INTEGER,
  source TEXT NOT NULL,
  direction TEXT NOT NULL,              -- ida | volta
  query_date TEXT NOT NULL,
  origin TEXT NOT NULL,
  dest TEXT NOT NULL,
  started_at TEXT NOT NULL,
  duration_ms INTEGER,
  status TEXT NOT NULL,                 -- ok | empty | blocked | error | skipped
  http_status INTEGER,
  n_flights INTEGER DEFAULT 0,
  error TEXT,
  url TEXT
);
CREATE INDEX IF NOT EXISTS ix_queries_sweep ON queries(sweep_id);
CREATE INDEX IF NOT EXISTS ix_queries_source ON queries(source, started_at);
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sweep_id INTEGER,
  query_id INTEGER,
  observed_at TEXT NOT NULL,
  source TEXT NOT NULL,
  seller_kind TEXT,                     -- metabusca | ota | cia | revenda_milhas | promo
  direction TEXT NOT NULL,
  query_date TEXT NOT NULL,
  origin TEXT NOT NULL,
  dest TEXT NOT NULL,
  airline_code TEXT,
  airline_name TEXT,
  flight_no TEXT,
  dep_date TEXT, dep_time TEXT,
  arr_date TEXT, arr_time TEXT,
  duration_min INTEGER,
  stops INTEGER,
  price REAL,
  currency TEXT DEFAULT 'BRL',
  fare_note TEXT,
  link TEXT,
  valid INTEGER NOT NULL,
  reject_reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_obs_key ON observations(direction, query_date, flight_no);
CREATE INDEX IF NOT EXISTS ix_obs_sweep ON observations(sweep_id);
-- Tarifa vigente: a observação mais recente de cada (fonte, voo, data).
CREATE TABLE IF NOT EXISTS current_fares (
  source TEXT NOT NULL,
  direction TEXT NOT NULL,
  query_date TEXT NOT NULL,
  origin TEXT NOT NULL,
  dest TEXT NOT NULL,
  flight_no TEXT NOT NULL,
  observation_id INTEGER NOT NULL,
  observed_at TEXT NOT NULL,
  price REAL NOT NULL,
  PRIMARY KEY (source, direction, query_date, origin, dest, flight_no)
);
CREATE TABLE IF NOT EXISTS combos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sweep_id INTEGER NOT NULL,
  computed_at TEXT NOT NULL,
  out_date TEXT NOT NULL,
  ret_date TEXT NOT NULL,
  stay_days INTEGER NOT NULL,
  window_idx INTEGER NOT NULL,
  out_obs_id INTEGER NOT NULL,
  ret_obs_id INTEGER NOT NULL,
  out_price REAL NOT NULL,
  ret_price REAL NOT NULL,
  total REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_combos_sweep ON combos(sweep_id, total);
CREATE INDEX IF NOT EXISTS ix_combos_dates ON combos(out_date, ret_date);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  combo_key TEXT NOT NULL,              -- data+voo ida | data+voo volta
  total REAL NOT NULL,
  created_at TEXT NOT NULL,
  sent_ok INTEGER NOT NULL,
  http_status INTEGER,
  error TEXT,
  is_test INTEGER DEFAULT 0,
  payload TEXT
);
CREATE INDEX IF NOT EXISTS ix_alerts_key ON alerts(combo_key);
CREATE TABLE IF NOT EXISTS promos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  url TEXT NOT NULL,
  title TEXT,
  published TEXT,
  price REAL,
  first_seen_at TEXT NOT NULL,
  UNIQUE(source, url)
);
CREATE TABLE IF NOT EXISTS source_state (
  source TEXT PRIMARY KEY,
  consecutive_blocks INTEGER DEFAULT 0,
  cooldown_until TEXT,
  last_ok_at TEXT,
  last_error_at TEXT,
  last_error TEXT,
  status_note TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    C.DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(C.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    return con


MIGRATIONS = [
    ("observations", "seller", "TEXT"),          # vendedor real (ex.: 'Decolar' via Kayak)
    ("source_state", "last_run_at", "TEXT"),     # início da última execução (fontes com intervalo próprio)
]


def init() -> None:
    with connect() as con:
        con.executescript(SCHEMA)
        for table, col, typ in MIGRATIONS:  # idempotente
            cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


@contextmanager
def tx():
    con = connect()
    try:
        yield con
        con.commit()
    finally:
        con.close()
