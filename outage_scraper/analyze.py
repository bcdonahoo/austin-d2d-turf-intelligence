"""
Turf prioritization from outage history.

Aggregates the logged outage snapshots into ~1km grid cells and scores each
cell on three dimensions:

    frequency  — distinct incidents observed in the cell
    severity   — total customer-minutes of outage (customers x observed duration)
    recency    — exponentially-decayed weight favoring recent incidents

The composite score ranks neighborhoods where outage pain is chronic, severe,
and fresh — i.e., where a battery-backup pitch lands on lived experience
rather than a hypothetical. Output is a CSV ready for turf mapping.

Usage:
    python -m outage_scraper.analyze --top 20
    python -m outage_scraper.analyze --csv data/turf_priority.csv
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import math
from pathlib import Path

from outage_scraper.scraper import DB_PATH, get_db

GRID = 0.01          # degrees; ~1.1 km N-S, ~0.96 km E-W at Austin's latitude
SNAPSHOT_MIN = 15    # each snapshot row represents ~15 minutes of outage
HALF_LIFE_DAYS = 60  # recency decay half-life


def cell_of(lat: float, lng: float) -> tuple[float, float]:
    return (round(lat / GRID) * GRID, round(lng / GRID) * GRID)


def analyze(db: Path, since_days: int | None) -> list[dict]:
    conn = get_db(db)
    where, params = "", []
    if since_days:
        cutoff = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=since_days)
        ).isoformat(timespec="seconds")
        where, params = "WHERE snapshot_ts >= ?", [cutoff]

    rows = conn.execute(
        f"""SELECT incident_id, latitude, longitude, cause,
                   MAX(cust_affected) AS cust,
                   COUNT(*)           AS intervals,
                   MAX(snapshot_ts)   AS last_seen
            FROM outages {where}
            GROUP BY incident_id""",
        params,
    ).fetchall()

    now = dt.datetime.now(dt.timezone.utc)
    cells: dict[tuple, dict] = {}
    for inc_id, lat, lng, cause, cust, intervals, last_seen in rows:
        if lat is None or lng is None:
            continue
        key = cell_of(lat, lng)
        c = cells.setdefault(
            key,
            {
                "lat": key[0], "lng": key[1], "incidents": 0,
                "customer_minutes": 0.0, "recency_weight": 0.0,
                "causes": {}, "last_incident": "",
            },
        )
        c["incidents"] += 1
        c["customer_minutes"] += (cust or 1) * intervals * SNAPSHOT_MIN
        last = dt.datetime.fromisoformat(last_seen)
        if last.tzinfo is None:
            last = last.replace(tzinfo=dt.timezone.utc)
        age_days = max((now - last).days, 0)
        c["recency_weight"] += 0.5 ** (age_days / HALF_LIFE_DAYS)
        c["causes"][cause or "Unknown"] = c["causes"].get(cause or "Unknown", 0) + 1
        c["last_incident"] = max(c["last_incident"], last_seen)

    if not cells:
        return []

    # normalize each dimension 0-1, then composite
    max_inc = max(c["incidents"] for c in cells.values())
    max_cm = max(c["customer_minutes"] for c in cells.values())
    max_rw = max(c["recency_weight"] for c in cells.values())
    scored = []
    for c in cells.values():
        f = c["incidents"] / max_inc
        s = math.log1p(c["customer_minutes"]) / math.log1p(max_cm) if max_cm else 0
        r = c["recency_weight"] / max_rw if max_rw else 0
        c["score"] = round(100 * (0.45 * f + 0.30 * s + 0.25 * r), 1)
        c["top_cause"] = max(c["causes"], key=c["causes"].get)
        c["customer_minutes"] = round(c["customer_minutes"])
        del c["causes"], c["recency_weight"]
        scored.append(c)
    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored


def main() -> None:
    p = argparse.ArgumentParser(description="Outage-history turf prioritization")
    p.add_argument("--db", type=Path, default=DB_PATH)
    p.add_argument("--since-days", type=int, default=None)
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--csv", type=Path, default=None)
    args = p.parse_args()

    scored = analyze(args.db, args.since_days)
    if not scored:
        print("No outage history in the database yet. Run the scraper first.")
        return

    cols = ["score", "lat", "lng", "incidents", "customer_minutes",
            "top_cause", "last_incident"]
    print(f"{'score':>6}  {'lat':>8}  {'lng':>9}  {'inc':>4}  "
          f"{'cust-min':>9}  top cause")
    for c in scored[: args.top]:
        print(f"{c['score']:>6}  {c['lat']:>8.3f}  {c['lng']:>9.3f}  "
              f"{c['incidents']:>4}  {c['customer_minutes']:>9}  {c['top_cause']}")

    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for c in scored:
                w.writerow({k: c[k] for k in cols})
        print(f"\nWrote {len(scored)} grid cells to {args.csv}")


if __name__ == "__main__":
    main()
