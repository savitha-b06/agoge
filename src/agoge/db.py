"""SQLite storage. One file, no server, trivially backed up, trivially inspected.

Schema philosophy: COROS owns the objective numbers, you own the subjective
ones, and every row records which is which.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY,
    coros_id        TEXT UNIQUE,
    day             TEXT NOT NULL,
    sport           TEXT NOT NULL,
    start_time      TEXT,
    duration_min    REAL,
    distance_mi     REAL,
    avg_hr          INTEGER,
    max_hr          INTEGER,
    avg_cadence     INTEGER,
    elevation_ft    REAL,
    z2_pct          REAL,
    zone_breakdown  TEXT,
    indoor          INTEGER DEFAULT 0,
    source          TEXT DEFAULT 'coros',
    raw             TEXT,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_sessions_day ON sessions(day);

CREATE TABLE IF NOT EXISTS daily (
    day             TEXT PRIMARY KEY,
    sleep_hours     REAL,
    sleep_score     INTEGER,
    hrv             INTEGER,
    resting_hr      INTEGER,
    steps           INTEGER,
    training_load   REAL,
    stress          INTEGER,
    vo2max          REAL,
    weight_lb       REAL,
    prehab_done     INTEGER,
    protein_hit     INTEGER,
    kcal_in         REAL,
    kcal_out        REAL,
    protein_g       REAL,
    carbs_g         REAL,
    fat_g           REAL,
    fiber_g         REAL,
    iron_mg         REAL,
    calcium_mg      REAL,
    sodium_mg       REAL,
    potassium_mg    REAL,
    vitamin_d_iu    REAL,
    caffeine_mg     REAL,
    water_g         REAL,
    energy_avail    REAL,
    readiness       INTEGER,
    readiness_flag  TEXT,
    note            TEXT,
    raw             TEXT,
    updated_at      TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS symptoms (
    id              INTEGER PRIMARY KEY,
    day             TEXT NOT NULL,
    injury_key      TEXT NOT NULL,
    severity        INTEGER,
    swelling        INTEGER,
    pain_type       TEXT,
    resolved_by     TEXT,
    overnight       INTEGER,
    session_id      INTEGER,
    note            TEXT,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_symptoms_day ON symptoms(day, injury_key);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY,
    day             TEXT NOT NULL,
    kind            TEXT NOT NULL,
    detail          TEXT,
    severity        TEXT,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS notes (
    id              INTEGER PRIMARY KEY,
    day             TEXT NOT NULL,
    kind            TEXT NOT NULL,
    body            TEXT NOT NULL,
    model           TEXT,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_notes_day ON notes(day, kind);

CREATE TABLE IF NOT EXISTS plan (
    id              INTEGER PRIMARY KEY,
    day             TEXT NOT NULL,
    sport           TEXT,
    title           TEXT,
    detail          TEXT,
    planned_min     REAL,
    status          TEXT DEFAULT 'planned',
    week_start      TEXT,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_plan_day ON plan(day);
"""


class DB:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """CREATE TABLE IF NOT EXISTS will not add columns to a database that
        already exists, so add anything missing by hand. Cheap and idempotent."""
        for table in ("daily", "sessions", "symptoms"):
            existing = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            wanted = _columns_in_schema(table)
            for col, decl in wanted.items():
                if col not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---------- writes ----------

    def upsert_session(self, s: dict[str, Any]) -> int:
        cols = [
            "coros_id", "day", "sport", "start_time", "duration_min", "distance_mi",
            "avg_hr", "max_hr", "avg_cadence", "elevation_ft", "z2_pct",
            "zone_breakdown", "indoor", "source", "raw",
        ]
        row = {c: s.get(c) for c in cols}
        if isinstance(row.get("zone_breakdown"), dict):
            row["zone_breakdown"] = json.dumps(row["zone_breakdown"])
        if isinstance(row.get("raw"), (dict, list)):
            row["raw"] = json.dumps(row["raw"])
        placeholders = ", ".join(f":{c}" for c in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "coros_id")
        with self.tx() as c:
            cur = c.execute(
                f"INSERT INTO sessions ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT(coros_id) DO UPDATE SET {updates}",
                row,
            )
            if cur.lastrowid:
                return cur.lastrowid
        got = self.conn.execute(
            "SELECT id FROM sessions WHERE coros_id=?", (s.get("coros_id"),)
        ).fetchone()
        return got["id"] if got else -1

    def upsert_daily(self, day: str, **fields: Any) -> None:
        fields = {k: v for k, v in fields.items() if v is not None}
        if isinstance(fields.get("raw"), (dict, list)):
            fields["raw"] = json.dumps(fields["raw"])
        if not fields:
            self.conn.execute("INSERT OR IGNORE INTO daily(day) VALUES (?)", (day,))
            self.conn.commit()
            return
        keys = list(fields)
        sets = ", ".join(f"{k}=excluded.{k}" for k in keys)
        with self.tx() as c:
            c.execute(
                f"INSERT INTO daily (day, {', '.join(keys)}) "
                f"VALUES (?, {', '.join('?' * len(keys))}) "
                f"ON CONFLICT(day) DO UPDATE SET {sets}, updated_at=CURRENT_TIMESTAMP",
                [day, *[fields[k] for k in keys]],
            )

    def add_symptom(self, **kw: Any) -> None:
        keys = list(kw)
        with self.tx() as c:
            c.execute(
                f"INSERT INTO symptoms ({', '.join(keys)}) "
                f"VALUES ({', '.join('?' * len(keys))})",
                [kw[k] for k in keys],
            )

    def add_event(self, day: str, kind: str, detail: str = "", severity: str = "info") -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO events (day, kind, detail, severity) VALUES (?,?,?,?)",
                (day, kind, detail, severity),
            )

    def add_note(self, day: str, kind: str, body: str, model: str = "") -> None:
        with self.tx() as c:
            c.execute(
                "DELETE FROM notes WHERE day=? AND kind=?", (day, kind)
            )
            c.execute(
                "INSERT INTO notes (day, kind, body, model) VALUES (?,?,?,?)",
                (day, kind, body, model),
            )

    # ---------- reads ----------

    def sessions_between(self, start: str, end: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM sessions WHERE day BETWEEN ? AND ? ORDER BY day, start_time",
            (start, end),
        ).fetchall()

    def daily_between(self, start: str, end: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM daily WHERE day BETWEEN ? AND ? ORDER BY day", (start, end)
        ).fetchall()

    def day(self, day: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM daily WHERE day=?", (day,)).fetchone()

    def open_symptoms(self, injury_key: str, since: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM symptoms WHERE injury_key=? AND day>=? ORDER BY day DESC",
            (injury_key, since),
        ).fetchall()

    def recent_symptoms(self, since: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM symptoms WHERE day>=? ORDER BY day DESC", (since,)
        ).fetchall()

    def recent_events(self, since: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM events WHERE day>=? ORDER BY day DESC", (since,)
        ).fetchall()

    def note(self, day: str, kind: str) -> str | None:
        row = self.conn.execute(
            "SELECT body FROM notes WHERE day=? AND kind=?", (day, kind)
        ).fetchone()
        return row["body"] if row else None

    def notes_between(self, start: str, end: str, kind: str = "daily") -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM notes WHERE day BETWEEN ? AND ? AND kind=? ORDER BY day",
            (start, end, kind),
        ).fetchall()

    def baseline(self, column: str, end: str, days: int = 28) -> float | None:
        row = self.conn.execute(
            f"SELECT AVG({column}) AS v FROM (SELECT {column} FROM daily "
            f"WHERE day < ? AND {column} IS NOT NULL ORDER BY day DESC LIMIT ?)",
            (end, days),
        ).fetchone()
        return row["v"] if row and row["v"] is not None else None

    def prehab_streak(self, today: str) -> int:
        rows = self.conn.execute(
            "SELECT day, prehab_done FROM daily WHERE day <= ? ORDER BY day DESC LIMIT 400",
            (today,),
        ).fetchall()
        streak = 0
        for r in rows:
            if r["prehab_done"]:
                streak += 1
            else:
                break
        return streak

    def weekly_minutes(self, week_start: str, week_end: str) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(duration_min),0) AS m FROM sessions WHERE day BETWEEN ? AND ?",
            (week_start, week_end),
        ).fetchone()
        return float(row["m"])


def _columns_in_schema(table: str) -> dict[str, str]:
    """Pull column names and types straight out of the SCHEMA string so the
    migration can never drift from the definition above it."""
    import re
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", SCHEMA, re.S)
    if not m:
        return {}
    cols = {}
    for line in m.group(1).splitlines():
        line = line.strip().rstrip(",")
        if not line or line.startswith(("FOREIGN", "PRIMARY", "UNIQUE", "CHECK")):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[0].isidentifier():
            decl = " ".join(parts[1:])
            if "PRIMARY KEY" in decl.upper():
                continue
            cols[parts[0]] = decl
    return cols


def today_str() -> str:
    return date.today().isoformat()


def now_str() -> str:
    return datetime.now().isoformat(timespec="seconds")
