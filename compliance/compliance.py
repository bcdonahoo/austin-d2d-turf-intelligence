"""
D2D compliance registry CLI.

The registry (markets.yaml) is the single source of truth for per-market
solicitation rules: permits, knocking hours, sign/HOA constraints, sources,
and verification status. This CLI handles intake and the two questions field
ops actually asks:

    "Can we knock in <market> right now / at <time>?"
    "Which market records are stale and need re-verification?"

Usage:
    python compliance/compliance.py list
    python compliance/compliance.py show "Round Rock"
    python compliance/compliance.py check "Round Rock" --when "2026-07-11 18:45"
    python compliance/compliance.py check Dallas --when "2026-07-12 11:00"
    python compliance/compliance.py audit
    python compliance/compliance.py add            # interactive intake
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import yaml

REGISTRY = Path(__file__).resolve().parent / "markets.yaml"


def load(path: Path = REGISTRY) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def save(doc: dict, path: Path = REGISTRY) -> None:
    with open(path, "w") as f:
        yaml.safe_dump(doc, f, sort_keys=False, allow_unicode=True)


def find(doc: dict, market: str) -> dict | None:
    for m in doc.get("markets", []):
        if m["market"].lower() == market.lower():
            return m
    return None


# ---------------------------------------------------------------------------
# check: can we knock at <market> at <datetime>?
# ---------------------------------------------------------------------------

def window_for(m: dict, when: dt.datetime) -> dict | str | None:
    hours = m.get("hours", {})
    dow = when.weekday()  # Mon=0 ... Sun=6
    if dow == 6 and "sunday" in hours:
        return hours["sunday"]
    if dow == 5 and "saturday" in hours:
        return hours["saturday"]
    if dow >= 5 and "weekend" in hours:
        return hours["weekend"]
    return hours.get("weekday")


def check(doc: dict, market: str, when: dt.datetime) -> tuple[bool, list[str]]:
    """Returns (go/no-go, list of findings). Conservative by design:
    any unresolved question is a blocker, not a footnote."""
    m = find(doc, market)
    if m is None:
        return False, [f"BLOCK: no registry record for '{market}'. "
                       f"Intake the market before any rep knocks a door."]

    findings: list[str] = []
    go = True

    # 1. record freshness
    interval = doc.get("defaults", {}).get("review_interval_days", 90)
    last = dt.date.fromisoformat(str(m.get("last_verified", "1970-01-01")))
    age = (dt.date.today() - last).days
    if m.get("status") != "verified":
        findings.append(
            f"WARN: record status is '{m.get('status')}' — confirm against "
            f"current ordinance text / issuing authority before launch."
        )
    if age > interval:
        go = False
        findings.append(f"BLOCK: record is {age} days old (review interval "
                        f"{interval}d). Re-verify before fielding reps.")

    # 2. permit
    if m.get("permit_required"):
        findings.append(
            f"PERMIT: {m.get('permit_type', 'permit')} required — "
            f"{m.get('permit_authority', 'authority not recorded')}."
            + (f" Fee ${m['permit_fee_usd']}." if m.get("permit_fee_usd") else "")
        )
        findings.append("ACTION: confirm every rep on today's turf holds a "
                        "current permit/ID before knock-out.")
    else:
        findings.append("PERMIT: none identified city-wide — see permit_notes.")

    # 3. hours
    w = window_for(m, when)
    if w == "prohibited" or w is None and m.get("hours"):
        go = False
        findings.append(f"BLOCK: solicitation prohibited on "
                        f"{when.strftime('%A')}s in {m['market']}.")
    elif isinstance(w, dict):
        start = dt.time.fromisoformat(w["start"])
        end_raw = w["end"]
        if end_raw == "sunset":
            findings.append("HOURS: end of window is local sunset — "
                            "check today's sunset time; treating 20:30 as "
                            "a conservative summer proxy.")
            end = dt.time(20, 30)
        else:
            end = dt.time.fromisoformat(end_raw)
        if not (start <= when.time() <= end):
            go = False
            findings.append(
                f"BLOCK: {when.strftime('%H:%M')} is outside the "
                f"{w['start']}-{end_raw} window for {when.strftime('%A')}."
            )
        else:
            findings.append(
                f"HOURS: {when.strftime('%H:%M %A')} is inside the "
                f"{w['start']}-{end_raw} window "
                f"(basis: {m.get('hours_basis', 'unrecorded')})."
            )
    if m.get("holidays_prohibited"):
        findings.append("WARN: solicitation prohibited on listed holidays — "
                        "check today's date against the market's holiday list.")

    # 4. standing constraints
    if doc.get("defaults", {}).get("sign_compliance_required", True):
        findings.append("SIGNS: posted no-soliciting/no-trespassing signs are "
                        "binding (TX Penal Code 30.05) — includes "
                        "subdivision-entrance signage.")
    if m.get("hoa_notes"):
        findings.append(f"HOA: {m['hoa_notes'].strip()}")
    if m.get("dnk_list") not in (None, "none_published"):
        findings.append(f"DNK: consult do-not-knock list: {m['dnk_list']}")

    return go, findings


# ---------------------------------------------------------------------------
# intake / list / audit
# ---------------------------------------------------------------------------

INTAKE_FIELDS = [
    ("market", "Market (city) name", None),
    ("county", "County", None),
    ("permit_required", "Permit required? (y/n)", "bool"),
    ("permit_authority", "Issuing authority (blank if none)", "opt"),
    ("permit_type", "Permit type (blank if none)", "opt"),
    ("permit_fee_usd", "Permit fee USD (blank if none)", "num"),
    ("permit_validity_days", "Permit validity in days (blank if unknown)", "num"),
]


def intake(doc: dict) -> None:
    print("New market intake — leave a field blank to skip.\n")
    rec: dict = {}
    for key, prompt, kind in INTAKE_FIELDS:
        val = input(f"{prompt}: ").strip()
        if not val and kind in ("opt", "num"):
            continue
        if kind == "bool":
            rec[key] = val.lower() in ("y", "yes", "true", "1")
        elif kind == "num":
            rec[key] = float(val) if "." in val else int(val)
        else:
            rec[key] = val
    ws = input("Weekday window HH:MM-HH:MM (e.g. 09:00-19:00): ").strip()
    we = input("Weekend window HH:MM-HH:MM (blank = same): ").strip() or ws
    rec["hours"] = {
        "weekday": dict(zip(("start", "end"), ws.split("-"))),
        "weekend": dict(zip(("start", "end"), we.split("-"))),
    }
    rec["hours_basis"] = input(
        "Hours basis (ordinance-cited / ordinance-reported / "
        "conservative-policy): ").strip() or "conservative-policy"
    src = input("Source URL(s), comma-separated: ").strip()
    rec["sources"] = [s.strip() for s in src.split(",") if s.strip()]
    rec["dnk_list"] = input("DNK list URL (blank if none published): "
                            ).strip() or "none_published"
    rec["status"] = "researched"
    rec["last_verified"] = dt.date.today().isoformat()

    if find(doc, rec["market"]):
        print(f"'{rec['market']}' already exists — not overwriting. "
              f"Edit markets.yaml directly to update.")
        return
    doc.setdefault("markets", []).append(rec)
    save(doc)
    print(f"\nAdded '{rec['market']}' with status=researched. "
          f"Verify against the current ordinance before launch.")


def list_markets(doc: dict) -> None:
    print(f"{'market':<14} {'permit':<7} {'status':<11} {'verified':<12} weekday window")
    for m in doc.get("markets", []):
        w = m.get("hours", {}).get("weekday", {})
        print(f"{m['market']:<14} {'yes' if m.get('permit_required') else 'no':<7} "
              f"{m.get('status', '?'):<11} {str(m.get('last_verified')):<12} "
              f"{w.get('start', '?')}-{w.get('end', '?')}")


def audit(doc: dict) -> int:
    interval = doc.get("defaults", {}).get("review_interval_days", 90)
    today = dt.date.today()
    issues = 0
    for m in doc.get("markets", []):
        problems = []
        age = (today - dt.date.fromisoformat(str(m.get("last_verified",
                                                       "1970-01-01")))).days
        if age > interval:
            problems.append(f"stale ({age}d old, interval {interval}d)")
        if m.get("status") != "verified":
            problems.append(f"status={m.get('status')}")
        if not m.get("sources"):
            problems.append("no sources recorded")
        if m.get("permit_required") and m.get("permit_validity_days") and \
                m["permit_validity_days"] <= interval:
            problems.append(
                f"permit validity ({m['permit_validity_days']}d) shorter than "
                f"review interval — track per-rep permit expiry separately")
        if problems:
            issues += 1
            print(f"{m['market']}: " + "; ".join(problems))
    if not issues:
        print("All market records clean.")
    return issues


def main() -> None:
    p = argparse.ArgumentParser(description="D2D compliance registry")
    p.add_argument("command", choices=["list", "show", "check", "add", "audit"])
    p.add_argument("market", nargs="?", help="market name for show/check")
    p.add_argument("--when", default=None,
                   help='local datetime "YYYY-MM-DD HH:MM" (default: now)')
    p.add_argument("--registry", type=Path, default=REGISTRY)
    args = p.parse_args()

    doc = load(args.registry)
    if args.command == "list":
        list_markets(doc)
    elif args.command == "show":
        m = find(doc, args.market or "")
        print(yaml.safe_dump(m, sort_keys=False) if m else "Not found.")
    elif args.command == "check":
        if not args.market:
            sys.exit("check requires a market name")
        when = (dt.datetime.fromisoformat(args.when)
                if args.when else dt.datetime.now())
        go, findings = check(doc, args.market, when)
        print(f"\n{'GO' if go else 'NO-GO'} — {args.market}, "
              f"{when.strftime('%A %Y-%m-%d %H:%M')}\n")
        for f in findings:
            print(f"  - {f}")
    elif args.command == "add":
        intake(doc)
    elif args.command == "audit":
        audit(doc)


if __name__ == "__main__":
    main()
