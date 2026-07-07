# Austin D2D Turf Intelligence

Not affiliated with Austin Energy, KUBRA, or Base Power. Uses only public,
unauthenticated map endpoints at a deliberately gentle polling cadence.

Two prototypes built around a single thesis: **signals-based selling applied
to the physical world.** Territory prioritization and dispatch timing for a
door-to-door channel should be driven by observable signals — outage history,
parcel data — and gated by a compliance registry that answers go/no-go before
a rep ever knocks.

Built as a working proof-of-concept for door-to-door sales operations.
Everything here runs locally with two lightweight dependencies (`requests`,
`PyYAML`).

## Quickstart

```bash
git clone <this-repo-url> austin-d2d-turf-intelligence
cd austin-d2d-turf-intelligence
pip install -r requirements.txt
python -m outage_scraper.scraper demo                          # seed 90 days of synthetic history
python -m outage_scraper.analyze --top 10 --csv data/turf_priority.csv
python compliance/compliance.py check "Round Rock" --when "2026-07-11 18:45"
```

## 1. Outage history scraper (`outage_scraper/`)

Austin Energy's public outage map is a KUBRA Storm Center deployment that
refreshes roughly every 10 minutes. The scraper polls its public JSON
endpoints (endpoint pattern verified against Open Austin's reference
implementation) and appends every snapshot to SQLite. Over time the database
becomes something the map itself never offers: **longitudinal outage history**.

```bash
pip install -r requirements.txt

python -m outage_scraper.scraper once          # single live snapshot
python -m outage_scraper.scraper loop          # poll every 15 min
python -m outage_scraper.scraper demo          # 90 days synthetic history
python -m outage_scraper.scraper status
```

`demo` exists so the full pipeline can be exercised offline; demo rows are
tagged `source='demo'` and never mix silently with live data.

### Turf prioritization (`analyze.py`)

Aggregates history into ~1 km grid cells and scores each on frequency
(distinct incidents), severity (customer-minutes), and recency (60-day
half-life decay), composited 45/30/25 into a 0–100 turf priority score.

```bash
python -m outage_scraper.analyze --top 20 --csv data/turf_priority.csv
```

Sample output (`analyze --top 10` against the seeded demo data):

```
 score       lat        lng   inc   cust-min  top cause
  98.1    30.180    -97.800     9     366255  Vehicle Accident
  94.0    30.400    -97.680     8     431445  Animal Contact
  90.6    30.410    -97.680     8     366255  Equipment Failure
  82.0    30.320    -97.710     7     668550  Unknown
  77.0    30.330    -97.700     6     629385  Unknown
  69.8    30.400    -97.670     5     379905  Planned Maintenance
  68.8    30.410    -97.690     5     311340  Animal Contact
  66.0    30.200    -97.800     5      88620  Equipment Failure
  60.2    30.190    -97.800     4     249510  Vehicle Accident
  58.7    30.180    -97.810     4     475830  Animal Contact
```

`data/turf_priority.csv` is checked into this repo as sample output — it was
generated from the `demo` (synthetic) history above, not live outage data.

Chronic-outage neighborhoods are the highest-propensity territories for a
home battery product — the pain is lived, not hypothetical. The same feed
supports a tactical layer: when a new outage polygon appears, queue the
affected turf for follow-up in a 24–72 hour window (never mid-outage; the
brand-risk difference between "we noticed your area was affected last week"
and knocking while the lights are out is the whole game).

## 2. Compliance registry (`compliance/`)

`markets.yaml` is the single source of truth for per-market solicitation
rules: permit requirements, knocking-hour windows, sign/HOA constraints,
sources, and a verification status on every record. The CLI answers the two
questions field ops actually asks.

```bash
python compliance/compliance.py list
python compliance/compliance.py check "Round Rock" --when "2026-07-11 18:45"
python compliance/compliance.py add        # interactive market intake
python compliance/compliance.py audit      # stale/unverified records
```

Sample output (`check "Round Rock" --when "2026-07-11 18:45"`, a Saturday evening):

```
NO-GO — Round Rock, Saturday 2026-07-11 18:45

  - WARN: record status is 'researched' — confirm against current ordinance text / issuing authority before launch.
  - PERMIT: Solicitor/Peddler/Canvasser ID card (per rep) required — Round Rock Police Department, 2701 N. Mays St.. Fee $50.
  - ACTION: confirm every rep on today's turf holds a current permit/ID before knock-out.
  - BLOCK: 18:45 is outside the 10:00-18:00 window for Saturday.
  - SIGNS: posted no-soliciting/no-trespassing signs are binding (TX Penal Code 30.05) — includes subdivision-entrance signage.
```

Design choices worth noting:

- **Conservative by default.** Unverified hours are recorded as
  `conservative-policy`, not passed off as ordinance. Any unresolved
  question is a blocker in `check`, not a footnote.
- **Provenance on every record.** Each market carries its source URLs,
  `last_verified` date, and a status (`researched` → `verified`) — nothing
  is launch-ready until a human confirms it against current ordinance text.
  The four seeded markets (Austin, Round Rock, Lakeway, Dallas) were
  researched from official city pages and reporting on 2026-07-07 and are
  intentionally marked `researched`, not `verified`.
- **Statewide baseline layered under city rules.** Texas Penal Code 30.05
  makes posted no-soliciting signage binding regardless of local ordinance,
  and subdivision-entrance signs bind whole neighborhoods — the registry
  surfaces this on every check.
- **Audit catches structural traps**, e.g. Round Rock's 90-day permit
  validity being no longer than the record review interval, which means
  per-rep permit expiry must be tracked separately from market verification.

## Extending

- **Parcel enrichment:** join `turf_priority.csv` against Travis County
  Appraisal District bulk data (homestead exemption = owner-occupied, home
  value, year built, solar improvements) to score individual parcels within
  priority cells.
- **Alerting:** diff consecutive snapshots to detect new incidents and push
  affected grid cells into a dispatch queue with a 24-hour cooling-off delay.
- **Other KUBRA utilities:** the client is parameterized on
  `instance_id`/`view_id`; CPS Energy (San Antonio) and AEP Texas run the
  same platform, so the identical pipeline covers new-market expansion.

## Caveats

- The KUBRA endpoints are public but undocumented; Austin Energy can change
  deployment IDs at any time (the client resolves rotating data paths via
  `currentState`, but the instance/view IDs themselves may need
  re-extraction from the map's network traffic).
- Scraping cadence is deliberately gentle (the map updates ~10 min;
  per-request delay built in). This is a research/prototyping tool.
- Nothing in `markets.yaml` is legal advice; records are research aids
  pending verification with each issuing authority.
- Live endpoints (`scraper once`/`status`) were last verified working
  against the production KUBRA deployment on 2026-07-07.
