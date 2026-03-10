#!/usr/bin/env python3
"""
NOTAM API Sanity Check — ICAO Data Services

Tests both the Stored NOTAMs and Realtime NOTAMs endpoints and reports:
- How many NOTAMs are returned per country/location
- Q-code breakdown
- Criticality distribution (NORM AI score 0-4)
- Sample records so we can see data quality

Run with: python3 notam_check.py
"""

import requests
import json
from collections import Counter
from datetime import datetime

API_KEY      = "0e99641e-1c16-442e-a901-a4bafd4a319d"
STORED_URL   = "https://dataservices.icao.int/api/notams-list"
REALTIME_URL = "https://dataservices.icao.int/api/realtime-notams"

# ISO 3-letter country codes for Middle East watchlist
ME_STATES = "ISR,JOR,QAT,OMN,IRN,EGY,TUR,LBN,SYR,YEM,SAU"

# Watched airport/FIR ICAO codes (max 10 per request)
WATCHED_LOCATIONS = [
    "LLBG",  # Tel Aviv — Ben Gurion
    "OTHH",  # Doha — Hamad
    "OOMS",  # Muscat
    "LTAC",  # Ankara
    "OJAI",  # Amman — Queen Alia
    "HECA",  # Cairo
    "LLBK",  # Israel FIR
    "OJAC",  # Jordan FIR
    "OTDF",  # Qatar FIR
    "OIIX",  # Iran — Tehran FIR
]

# Q-code regex patterns to query (ICAO supports regex in Qcode param)
SIGNAL_QUERIES = [
    ("RT??", "Restricted areas (all)"),
    ("FA??", "Airport status (closure etc)"),
    ("MR??", "Runway status"),
    ("RA??", "Air defence / prohibited areas"),
]


def get(url, params):
    """Make a request and return parsed JSON or an error string."""
    try:
        params["api_key"] = API_KEY
        params["format"]  = "json"
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        return {"error": str(e)}
    except json.JSONDecodeError:
        return {"error": f"Non-JSON response: {r.text[:200]}"}


def summarise(data, label):
    """Print a summary of a NOTAM response."""
    if isinstance(data, dict) and "error" in data:
        print(f"  ERROR: {data['error']}")
        return

    if not data:
        print("  No NOTAMs returned")
        return

    print(f"  Records returned: {len(data)}")

    # Q-code breakdown
    qcodes  = [r.get("Qcode", "") for r in data if r.get("Qcode")]
    crit    = [r.get("criticality", -1) for r in data]

    if qcodes:
        top = Counter(qcodes).most_common(8)
        print("  Top Q-codes:")
        for qcode, count in top:
            # Show decoded meaning if available
            sample = next((r for r in data if r.get("Qcode") == qcode), {})
            meaning = f"{sample.get('Subject', '')} — {sample.get('Modifier', '')}".strip(" —")
            print(f"    {qcode:<8} x{count:<4}  {meaning[:55]}")

    if any(c >= 0 for c in crit):
        crit_dist = Counter(c for c in crit if c >= 0)
        print(f"  Criticality (NORM): {dict(sorted(crit_dist.items()))}")
        high = [r for r in data if r.get("criticality", -1) >= 3]
        if high:
            print(f"  High-criticality NOTAMs (score 3-4): {len(high)}")

    # Sample record
    if data:
        s = data[0]
        print(f"  Sample NOTAM:")
        print(f"    ID:       {s.get('id')}")
        print(f"    Location: {s.get('location')}  ({s.get('StateName')})")
        print(f"    Q-code:   {s.get('Qcode')}  — {s.get('Subject')} {s.get('Modifier')}")
        print(f"    Valid:    {s.get('startdate')} → {s.get('enddate')}")
        msg = (s.get("message") or "").replace("\n", " ")[:120]
        print(f"    Message:  {msg}")


def run():
    print(f"\nNOTAM Sanity Check — {datetime.utcnow().strftime('%Y-%m-%d %H:%MZ')}")
    print("=" * 65)

    # ── Test 1: Stored NOTAMs by country, filtered to signal Q-codes ──
    print("\n1. STORED NOTAMs — Middle East states, signal Q-codes")
    print("-" * 65)

    for qpattern, qlabel in SIGNAL_QUERIES:
        print(f"\n  Query: Qcode={qpattern}  ({qlabel})")
        data = get(STORED_URL, {
            "states":  ME_STATES,
            "Qcode":   qpattern,
            "ICAOonly": "false",
        })
        summarise(data, qlabel)

    # ── Test 2: Stored NOTAMs — no Q-code filter, all types, by state ──
    print("\n\n2. STORED NOTAMs — all types, by country")
    print("-" * 65)

    for state, name in [("ISR","Israel"), ("IRN","Iran"), ("QAT","Qatar"), ("EGY","Egypt")]:
        print(f"\n  {name} ({state}):")
        data = get(STORED_URL, {"states": state, "ICAOonly": "false"})
        summarise(data, name)

    # ── Test 3: Realtime NOTAMs for watched airports ──
    print("\n\n3. REALTIME NOTAMs — watched airports (up to 10 locations)")
    print("-" * 65)

    locations_str = ",".join(WATCHED_LOCATIONS)
    data = get(REALTIME_URL, {
        "locations":   locations_str,
        "criticality": "true",
    })
    print(f"\n  Locations queried: {locations_str}")
    summarise(data, "realtime")

    # ── Summary ──
    print("\n\n" + "=" * 65)
    print("Key questions to answer from the above:")
    print("  1. Did Iran (IRN) return stored NOTAMs?")
    print("  2. Are QRTCA/restricted area codes present?")
    print("  3. Are criticality scores populated (not all -1)?")
    print("  4. Did realtime endpoint return data for watched airports?")


if __name__ == "__main__":
    run()
