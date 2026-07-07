"""
Austin Energy outage history logger.

Polls the KUBRA-backed public outage map and appends each snapshot to a local
SQLite database. Run it on a schedule (cron, systemd timer, GitHub Actions,
or `--loop`) and the database becomes a longitudinal outage-frequency dataset
— the raw input for turf prioritization (see analyze.py).

Usage:
    python -m outage_scraper.scraper once                 # single snapshot
    python -m outage_scraper.scraper loop --minutes 15    # poll forever
    python -m outage_scraper.scraper demo                 # 90 days synthetic
    python -m outage_scraper.scraper status               # db summary

Notes:
    - The map itself refreshes roughly every 10 minutes; polling more often
      than that only re-reads the same data.
    - `demo` seeds the database with synthetic-but-realistic history so the
      full pipeline (scrape -> store -> analyze) can be exercised offline.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "outages.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_ts   TEXT PRIMARY KEY,     -- ISO8601 UTC
    total_outages INTEGER,
    total_cust    INTEGER,
    source        TEXT                  -- 'live' | 'demo'
);
CREATE TABLE IF NOT EXISTS outages (
    snapshot_ts   TEXT NOT NULL REFERENCES snapshots(snapshot_ts),
    incident_id   TEXT NOT NULL,
    latitude      REAL,
    longitude     REAL,
    cause         TEXT,
    num_out       INTEGER,
    cust_affected INTEGER,
    crew_status   TEXT,
    start_time    TEXT,
    etr           TEXT,
    is_cluster    INTEGER,
    PRIMARY KEY (snapshot_ts, incident_id)
);
CREATE INDEX IF NOT EXISTS idx_outages_incident ON outages(incident_id);
CREATE INDEX IF NOT EXISTS idx_outages_latlng   ON outages(latitude, longitude);
"""


def get_db(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def store_snapshot(conn, ts: str, outages: list[dict], source: str) -> None:
    total_cust = sum(o.get("cust_affected") or 0 for o in outages)
    conn.execute(
        "INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?)",
        (ts, len(outages), total_cust, source),
    )
    conn.executemany(
        """INSERT OR REPLACE INTO outages VALUES
           (?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                ts,
                o["incident_id"],
                o.get("latitude"),
                o.get("longitude"),
                o.get("cause"),
                o.get("num_out"),
                o.get("cust_affected"),
                o.get("crew_status"),
                o.get("start_time"),
                o.get("etr"),
                int(bool(o.get("is_cluster"))),
            )
            for o in outages
        ],
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Live polling
# ---------------------------------------------------------------------------

def poll_once(conn) -> int:
    from outage_scraper.kubra import KubraClient

    client = KubraClient()
    outages = client.fetch_outages()
    ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    store_snapshot(conn, ts, outages, source="live")
    print(f"[{ts}] stored {len(outages)} active outage(s)")
    return len(outages)


def poll_loop(conn, minutes: int) -> None:
    print(f"Polling every {minutes} min. Ctrl-C to stop.")
    while True:
        try:
            poll_once(conn)
        except Exception as exc:  # keep the loop alive across transient errors
            print(f"WARN poll failed: {exc}", file=sys.stderr)
        time.sleep(minutes * 60)


# ---------------------------------------------------------------------------
# Demo mode — synthetic history for offline pipeline testing
# ---------------------------------------------------------------------------

def seed_demo(conn, days: int = 90) -> None:
    """Generate realistic synthetic outage history across Austin.

    Deliberately biases three 'chronic' zones so analyze.py has real signal
    to surface. All demo rows are tagged source='demo'.
    """
    import random

    random.seed(42)
    causes = [
        "Equipment Failure", "Vegetation", "Weather", "Vehicle Accident",
        "Animal Contact", "Planned Maintenance", "Unknown",
    ]
    # (lat, lng, weight) — chronic zones get higher incident weight
    zones = [
        (30.187, -97.799, 5),   # chronic: SW Austin feeder
        (30.325, -97.703, 4),   # chronic: NE / Windsor Park area
        (30.402, -97.680, 4),   # chronic: N Austin / Tech Ridge area
        (30.267, -97.743, 1),   # downtown
        (30.230, -97.820, 1),   # SW
        (30.350, -97.760, 1),   # NW hills
        (30.290, -97.690, 1),   # E Austin
        (30.440, -97.770, 1),   # far north
    ]
    weighted = [z for z in zones for _ in range(z[2])]

    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    incident_n = 0
    for d in range(days, 0, -1):
        day = now - dt.timedelta(days=d)
        # storm days (~1 in 12) produce clusters of incidents
        n_incidents = random.choice([0, 0, 0, 1, 1, 2]) + (
            random.randint(4, 10) if random.random() < 0.08 else 0
        )
        for _ in range(n_incidents):
            incident_n += 1
            lat0, lng0, _ = random.choice(weighted)
            lat = lat0 + random.uniform(-0.012, 0.012)
            lng = lng0 + random.uniform(-0.012, 0.012)
            start = day + dt.timedelta(minutes=random.randint(0, 1439))
            duration = random.randint(20, 360)  # minutes
            cust = random.choice([random.randint(1, 40), random.randint(40, 900)])
            incident_id = f"DEMO-{incident_n:05d}"
            # one snapshot row per 15-min interval the outage was active
            t = start
            while t < start + dt.timedelta(minutes=duration):
                ts = t.isoformat(timespec="seconds")
                conn.execute(
                    "INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?)",
                    (ts, 0, 0, "demo"),
                )
                conn.execute(
                    """INSERT OR REPLACE INTO outages VALUES
                       (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        ts, incident_id, lat, lng, random.choice(causes),
                        1, cust, "Assigned",
                        start.isoformat(timespec="seconds"), None, 0,
                    ),
                )
                t += dt.timedelta(minutes=15)
    conn.commit()
    print(f"Seeded {incident_n} synthetic incidents over {days} days (source='demo').")


def status(conn) -> None:
    snaps, = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()
    incidents, = conn.execute(
        "SELECT COUNT(DISTINCT incident_id) FROM outages"
    ).fetchone()
    first = conn.execute("SELECT MIN(snapshot_ts) FROM snapshots").fetchone()[0]
    last = conn.execute("SELECT MAX(snapshot_ts) FROM snapshots").fetchone()[0]
    print(f"snapshots: {snaps}\nunique incidents: {incidents}")
    print(f"window: {first}  ->  {last}")


def main() -> None:
    p = argparse.ArgumentParser(description="Austin Energy outage history logger")
    p.add_argument("command", choices=["once", "loop", "demo", "status"])
    p.add_argument("--minutes", type=int, default=15, help="loop interval")
    p.add_argument("--days", type=int, default=90, help="demo history depth")
    p.add_argument("--db", type=Path, default=DB_PATH)
    args = p.parse_args()

    conn = get_db(args.db)
    if args.command == "once":
        poll_once(conn)
    elif args.command == "loop":
        poll_loop(conn, args.minutes)
    elif args.command == "demo":
        seed_demo(conn, args.days)
    elif args.command == "status":
        status(conn)


if __name__ == "__main__":
    main()
