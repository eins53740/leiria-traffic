"""SQLite store: saved routes, the prediction cache, collected observations, day contexts.

No raw API responses are stored — only the numbers the app uses.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .models import DayContext, Point, RouteEstimate

SCHEMA = """
CREATE TABLE IF NOT EXISTS routes (
    route_id    INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    origin      TEXT NOT NULL,          -- "lat,lon" (5 dp)
    destination TEXT NOT NULL,
    origin_label TEXT, destination_label TEXT,
    distance_m  INTEGER,
    collect     INTEGER NOT NULL DEFAULT 0   -- 1 = the collector samples it
);
CREATE TABLE IF NOT EXISTS predictions (       -- cache of provider answers, one per 10-min slot
    provider    TEXT NOT NULL,
    origin      TEXT NOT NULL,
    destination TEXT NOT NULL,
    departure   TEXT NOT NULL,          -- local ISO, snapped to the slot
    duration_s  INTEGER NOT NULL,
    arrival     TEXT NOT NULL,
    distance_m  INTEGER,
    typical_s   INTEGER, free_flow_s INTEGER, live_s INTEGER,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (provider, origin, destination, departure)
);
CREATE TABLE IF NOT EXISTS observations (      -- what the collector measured (live traffic)
    route_id    INTEGER NOT NULL REFERENCES routes(route_id),
    observed_at TEXT NOT NULL,          -- local ISO
    slot        TEXT NOT NULL,          -- HH:MM
    duration_s  INTEGER NOT NULL,
    free_flow_s INTEGER,
    distance_m  INTEGER,
    provider    TEXT NOT NULL,
    precipitation_mm REAL, temperature_c REAL,
    PRIMARY KEY (route_id, observed_at)
);
CREATE TABLE IF NOT EXISTS contexts (
    day         TEXT PRIMARY KEY,
    weekday     TEXT NOT NULL, month INTEGER NOT NULL, season TEXT NOT NULL,
    weekend     INTEGER NOT NULL, school_term INTEGER NOT NULL, school_break TEXT,
    public_holiday TEXT, events TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS obs_slot ON observations(route_id, slot);
"""


class Store:
    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # --- prediction cache -------------------------------------------------
    def cached(self, provider: str, o: Point, d: Point, departure: datetime, ttl: timedelta) -> RouteEstimate | None:
        row = self.conn.execute(
            "SELECT * FROM predictions WHERE provider=? AND origin=? AND destination=? AND departure=?",
            (provider, o.key(), d.key(), departure.isoformat(timespec="minutes"))).fetchone()
        if row is None or datetime.fromisoformat(row["fetched_at"]) < datetime.now() - ttl:
            return None
        return RouteEstimate(provider=provider, departure=departure, duration_s=row["duration_s"],
                             distance_m=row["distance_m"], typical_s=row["typical_s"],
                             free_flow_s=row["free_flow_s"], live_s=row["live_s"])

    def put(self, o: Point, d: Point, slot_departure: datetime, e: RouteEstimate) -> None:
        if not e.cacheable:
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO predictions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (e.provider, o.key(), d.key(), slot_departure.isoformat(timespec="minutes"), e.duration_s,
             e.arrival.isoformat(timespec="minutes"), e.distance_m, e.typical_s, e.free_flow_s, e.live_s,
             datetime.now().isoformat(timespec="seconds")))
        self.conn.commit()

    def purge(self, older_than: timedelta) -> int:
        cur = self.conn.execute("DELETE FROM predictions WHERE fetched_at < ?",
                                ((datetime.now() - older_than).isoformat(timespec="seconds"),))
        self.conn.commit()
        return cur.rowcount

    # --- routes -------------------------------------------------------------
    def add_route(self, name: str, o: Point, d: Point, o_label: str = "", d_label: str = "",
                  collect: bool = False) -> int:
        self.conn.execute(
            "INSERT INTO routes(name, origin, destination, origin_label, destination_label, collect) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET origin=excluded.origin, "
            "destination=excluded.destination, origin_label=excluded.origin_label, "
            "destination_label=excluded.destination_label, collect=excluded.collect",
            (name, o.key(), d.key(), o_label, d_label, int(collect)))
        self.conn.commit()
        return self.conn.execute("SELECT route_id FROM routes WHERE name=?", (name,)).fetchone()[0]

    def routes(self, collecting_only: bool = False) -> list[sqlite3.Row]:
        q = "SELECT * FROM routes" + (" WHERE collect=1" if collecting_only else "") + " ORDER BY route_id"
        return self.conn.execute(q).fetchall()

    def route_id_for(self, o: Point, d: Point) -> int | None:
        row = self.conn.execute("SELECT route_id FROM routes WHERE origin=? AND destination=?",
                                (o.key(), d.key())).fetchone()
        return row[0] if row else None

    # --- observations -------------------------------------------------------
    def add_observation(self, route_id: int, observed_at: datetime, slot: str, e: RouteEstimate,
                        precipitation_mm: float | None, temperature_c: float | None) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?,?,?,?)",
            (route_id, observed_at.isoformat(timespec="seconds"), slot, e.live_s or e.duration_s,
             e.free_flow_s, e.distance_m, e.provider, precipitation_mm, temperature_c))
        self.conn.commit()

    def observed_minutes(self, route_id: int, slot: str, *, weekday: str, school_term: bool | None,
                         raining: bool | None) -> list[float]:
        """Durations (min) for this route + slot on matching days. school_term/raining None = any."""
        q = ("SELECT o.duration_s FROM observations o JOIN contexts c ON c.day = substr(o.observed_at,1,10) "
             "WHERE o.route_id=? AND o.slot=? AND c.weekday=? AND c.public_holiday IS NULL")
        args: list = [route_id, slot, weekday]
        if school_term is not None:
            q += " AND c.school_term=?"
            args.append(int(school_term))
        if raining is not None:
            q += " AND o.precipitation_mm IS NOT NULL AND (o.precipitation_mm >= 0.2) = ?"
            args.append(int(raining))
        return [r[0] / 60 for r in self.conn.execute(q, args)]

    # --- contexts -------------------------------------------------------------
    def save_context(self, c: DayContext) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO contexts VALUES (?,?,?,?,?,?,?,?,?)",
            (c.day.isoformat(), c.weekday, c.month, c.season, int(c.weekend), int(c.school_term),
             c.school_break, c.public_holiday, json.dumps(list(c.events))))
        self.conn.commit()
