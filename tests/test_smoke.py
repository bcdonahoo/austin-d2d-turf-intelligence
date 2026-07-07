"""
End-to-end smoke tests, offline only (no live KUBRA calls).

Covers: demo data seeding, turf analysis, compliance go/no-go, and the
quadkey/polyline utilities against known reference values.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from outage_scraper.analyze import analyze
from outage_scraper.kubra import decode_polyline, latlng_to_quadkey
from outage_scraper.scraper import get_db, seed_demo
from compliance.compliance import check, load

REGISTRY = Path(__file__).resolve().parent.parent / "compliance" / "markets.yaml"


def test_seed_demo_stores_incidents(tmp_path):
    db_path = tmp_path / "outages.db"
    conn = get_db(db_path)
    seed_demo(conn, days=90)
    incidents, = conn.execute(
        "SELECT COUNT(DISTINCT incident_id) FROM outages"
    ).fetchone()
    assert incidents > 0


def test_analyze_returns_scored_cells(tmp_path):
    db_path = tmp_path / "outages.db"
    conn = get_db(db_path)
    seed_demo(conn, days=90)

    cells = analyze(db_path, since_days=None)
    assert len(cells) > 0
    for cell in cells:
        assert 0 <= cell["score"] <= 100


def test_compliance_round_rock_saturday_evening_is_no_go():
    doc = load(REGISTRY)
    when = dt.datetime(2026, 7, 11, 18, 45)  # Saturday, past the 18:00 cutoff
    go, findings = check(doc, "Round Rock", when)
    assert go is False
    assert findings


def test_compliance_austin_wednesday_afternoon_is_go():
    doc = load(REGISTRY)
    when = dt.datetime(2026, 7, 8, 14, 0)  # Wednesday, inside 09:00-19:00
    go, findings = check(doc, "Austin", when)
    assert go is True
    assert findings


def test_latlng_to_quadkey_known_value():
    # (0, 0) at zoom 2 sits in tile (2, 2) -> quadkey "30"
    assert latlng_to_quadkey(0, 0, 2) == "30"


def test_decode_polyline_known_value():
    # Reference example from Google's polyline algorithm documentation.
    points = decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
    assert points == [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]
