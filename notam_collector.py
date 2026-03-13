#!/usr/bin/env python3
"""
MarketSignal — NOTAM Monitor (Laminar Data API)

Polls Middle East NOTAMs from Laminar Data every 30 minutes.
Stores all NOTAMs to SQLite; auto-flags airspace restriction Q-codes as anomalies.

Usage:
    python3 notam_collector.py            # run once
    python3 notam_collector.py --loop     # continuous polling every 30 minutes
    python3 notam_collector.py --status   # print database summary
"""

import sqlite3
import requests
import json
import os
import time
import argparse
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────

DB_PATH       = "notam_events.db"
API_BASE      = "https://api.sky.cirium.com"
POLL_INTERVAL = 1800  # 30 minutes in seconds

# Cirium Sky API — Authorization header takes the Identifier directly as the token
API_TOKEN = os.environ.get("CIRIUM_SKY_APP_ID", "")

# Middle East bounding box: lat 10–45, lon 25–65
# API expects a GeoJSON Feature wrapping a Polygon geometry.
# Coordinates are [longitude, latitude] order.
ME_GEOMETRY = {
    "type": "Feature",
    "geometry": {
        "type": "Polygon",
        "coordinates": [[[25, 10], [65, 10], [65, 45], [25, 45], [25, 10]]],
    },
}

# Q-code prefixes that trigger anomaly flagging (airspace restriction/closure)
RESTRICTION_PREFIXES = ("QRT", "QRP", "QRD", "QRAL")


def qcode_label(qcode):
    """Return (human_label, severity) for a Q-code. Severity is None for non-restriction types."""
    if not qcode:
        return "Unknown", None
    q = qcode.upper()
    if q.startswith("QRT"):
        return "Temporary Restricted Area", "MEDIUM"
    if q.startswith("QRP"):
        return "Prohibited Area", "MEDIUM"
    if q.startswith("QRD"):
        return "Danger Area", "HIGH"
    if q.startswith("QRAL"):
        return "All Traffic Restricted", "HIGH"
    if q.startswith("QR"):
        return "Airspace Restriction", "MEDIUM"
    return qcode, None


def is_restriction(qcode):
    """Return True if this Q-code should trigger an anomaly entry."""
    if not qcode:
        return False
    q = qcode.upper()
    return any(q.startswith(p) for p in RESTRICTION_PREFIXES)


# ── Database ───────────────────────────────────────────────────────────────────

def _add_column(conn, table, column, col_type):
    """Add a column to an existing table if it doesn't already exist."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def init_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notams (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            notam_id             TEXT UNIQUE NOT NULL,   -- "{number}/{year}", e.g. "A1234/26"
            location             TEXT,                   -- ICAO 4-letter aerodrome/FIR code
            country_code         TEXT,                   -- ISO 2-letter
            fir                  TEXT,                   -- affected FIR
            qcode                TEXT,                   -- Q-code, e.g. QRTCA
            restriction_type     TEXT,                   -- human-readable label
            effective_start      TEXT,                   -- ISO 8601
            effective_end        TEXT,                   -- ISO 8601 or null
            effective_end_interp TEXT,                   -- "EST" or "PERM"
            lat                  REAL,                   -- center latitude (may be null)
            lon                  REAL,                   -- center longitude (may be null)
            radius_nm            REAL,                   -- radius in nautical miles
            min_fl               INTEGER,                -- minimum flight level
            max_fl               INTEGER,                -- maximum flight level
            raw_text             TEXT,                   -- full NOTAM text
            geometry_json        TEXT,                   -- GeoJSON geometry string (may be null)
            first_detected_at    TEXT NOT NULL,          -- CRITICAL: set on INSERT, never overwritten
            last_seen_at         TEXT NOT NULL           -- updated every poll cycle
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notam_anomalies (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at       TEXT NOT NULL,
            notam_id          TEXT NOT NULL,
            location          TEXT,
            country_code      TEXT,
            qcode             TEXT,
            restriction_type  TEXT,
            lat               REAL,
            lon               REAL,
            radius_nm         REAL,
            effective_start   TEXT,
            effective_end     TEXT,
            raw_text          TEXT,
            anomaly_type      TEXT DEFAULT 'new_restriction',
            severity          TEXT,
            last_confirmed_at TEXT,             -- updated each poll while NOTAM still active
            resolved_at       TEXT              -- set when NOTAM no longer returned by API
        )
    """)
    # ── Migrate existing tables ──────────────────────────────────────────────────
    _add_column(conn, "notam_anomalies", "last_confirmed_at", "TEXT")
    _add_column(conn, "notam_anomalies", "resolved_at",       "TEXT")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_notam_qcode    ON notams(qcode)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notam_location ON notams(location)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notam_start    ON notams(effective_start)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notam_detected ON notams(first_detected_at)")
    conn.commit()
    return conn


# ── API fetch ──────────────────────────────────────────────────────────────────

def fetch_notams():
    """POST to Cirium Sky API with ME bounding box. Returns list of GeoJSON feature dicts."""
    if not API_TOKEN:
        print("  [API] No credentials — set CIRIUM_SKY_APP_ID in .env")
        return []

    url = f"{API_BASE}/v1/notams/"
    try:
        r = requests.post(
            url,
            data=json.dumps(ME_GEOMETRY),
            headers={
                "Content-Type":  "application/geo+json",
                "Accept":        "application/geo+json",
                "Authorization": f"Bearer {API_TOKEN}",
            },
            timeout=30,
        )

        if r.status_code == 401:
            print("  [API] 401 Unauthorized — check CIRIUM_SKY_APP_ID in .env")
            return []

        r.raise_for_status()
        data     = r.json()
        features = data.get("features", [])
        print(f"  [API] {len(features)} NOTAMs in response")
        return features

    except requests.RequestException as e:
        print(f"  [API] Request error: {e}")
        return []


# ── Storage ────────────────────────────────────────────────────────────────────

def save_notam(conn, feature):
    """
    Insert or update a NOTAM. first_detected_at is set only on INSERT — never changed.
    Returns True if this is a brand-new NOTAM (just inserted for the first time).
    """
    p = feature.get("properties") or {}

    number = p.get("number", "")
    year   = p.get("year",   "")
    if not number:
        return False
    notam_id = f"{number}/{year}" if year else str(number)

    location   = p.get("location") or p.get("affectedAerodrome")
    country    = p.get("countryCode")
    fir        = p.get("affectedFIR")
    qcode      = p.get("qcode")
    label, _   = qcode_label(qcode)
    eff_start  = p.get("effectiveStart")
    eff_end    = p.get("effectiveEnd")
    eff_interp = p.get("effectiveEndInterpretation")
    lat        = p.get("lat")
    lon        = p.get("lon")
    radius     = p.get("radius")
    min_fl     = p.get("minimumFL")
    max_fl     = p.get("maximumFL")
    raw_text   = p.get("text")
    geom       = feature.get("geometry")
    geom_json  = json.dumps(geom) if geom else None
    now        = datetime.now(timezone.utc).isoformat()

    cursor = conn.execute("""
        INSERT OR IGNORE INTO notams
            (notam_id, location, country_code, fir, qcode, restriction_type,
             effective_start, effective_end, effective_end_interp,
             lat, lon, radius_nm, min_fl, max_fl,
             raw_text, geometry_json,
             first_detected_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        notam_id, location, country, fir, qcode, label,
        eff_start, eff_end, eff_interp,
        lat, lon, radius, min_fl, max_fl,
        raw_text, geom_json,
        now, now,
    ))

    is_new = cursor.rowcount > 0

    if not is_new:
        conn.execute(
            "UPDATE notams SET last_seen_at = ? WHERE notam_id = ?",
            (now, notam_id),
        )

    return is_new


def flag_anomaly(conn, feature, severity):
    """Insert a new restriction NOTAM into notam_anomalies."""
    p = feature.get("properties") or {}

    number   = p.get("number", "")
    year     = p.get("year",   "")
    notam_id = f"{number}/{year}" if year else str(number)
    qcode    = p.get("qcode")
    label, _ = qcode_label(qcode)
    now      = datetime.now(timezone.utc).isoformat()

    conn.execute("""
        INSERT INTO notam_anomalies
            (detected_at, notam_id, location, country_code,
             qcode, restriction_type,
             lat, lon, radius_nm,
             effective_start, effective_end,
             raw_text, anomaly_type, severity)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new_restriction', ?)
    """, (
        now,
        notam_id,
        p.get("location") or p.get("affectedAerodrome"),
        p.get("countryCode"),
        qcode,
        label,
        p.get("lat"),
        p.get("lon"),
        p.get("radius"),
        p.get("effectiveStart"),
        p.get("effectiveEnd"),
        p.get("text"),
        severity,
    ))


# ── Poll ───────────────────────────────────────────────────────────────────────

def poll(conn):
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    print(f"\nNOTAM Poll — {now_str}")
    print("-" * 65)

    features = fetch_notams()
    if not features:
        print("  No NOTAMs returned — nothing to store.")
        return

    new_count   = 0
    seen_count  = 0
    new_restrictions = []

    for feat in features:
        is_new = save_notam(conn, feat)
        if is_new:
            new_count += 1
            qcode = (feat.get("properties") or {}).get("qcode", "")
            if is_restriction(qcode):
                _, severity = qcode_label(qcode)
                if severity:
                    new_restrictions.append((feat, severity))
        else:
            seen_count += 1

    for feat, severity in new_restrictions:
        flag_anomaly(conn, feat, severity)

    conn.commit()

    print(f"  Results: {new_count} new  |  {seen_count} already known")

    if new_restrictions:
        print(f"\n  *** {len(new_restrictions)} NEW AIRSPACE RESTRICTION(S) ***")
        for feat, severity in new_restrictions:
            p   = feat.get("properties") or {}
            loc = p.get("location") or p.get("affectedAerodrome") or "?"
            lbl, _ = qcode_label(p.get("qcode", ""))
            num  = p.get("number", "?")
            yr   = p.get("year", "")
            nid  = f"{num}/{yr}" if yr else str(num)
            start = str(p.get("effectiveStart", ""))[:10]
            print(f"  [{severity}] {nid:<15} {loc:<6}  {lbl}  {start}")
    else:
        print("  No new airspace restrictions detected.")

    print(f"\n  Poll complete — {new_count + seen_count} NOTAMs processed.")


# ── Status ─────────────────────────────────────────────────────────────────────

def print_status(conn):
    total = conn.execute("SELECT COUNT(*) FROM notams").fetchone()[0]
    if not total:
        print("No NOTAM data yet. Run without --status first to collect data.")
        return

    dr = conn.execute(
        "SELECT MIN(first_detected_at), MAX(last_seen_at) FROM notams"
    ).fetchone()

    print(f"\nNOTAM Database Summary")
    print("=" * 65)
    print(f"Total NOTAMs stored:  {total:,}")
    print(f"First detected:       {dr[0][:16] if dr[0] else '—'}")
    print(f"Last seen:            {dr[1][:16] if dr[1] else '—'}")

    # Q-code breakdown
    print("\nTop Q-codes:")
    rows = conn.execute("""
        SELECT qcode, COUNT(*) AS cnt
        FROM notams
        GROUP BY qcode
        ORDER BY cnt DESC
        LIMIT 15
    """).fetchall()
    for qcode, cnt in rows:
        label, sev = qcode_label(qcode)
        sev_str = f"  [{sev}]" if sev else ""
        print(f"  {(qcode or 'None'):<10} {cnt:>5}  {label}{sev_str}")

    # Recent anomalies
    anoms = conn.execute("""
        SELECT detected_at, notam_id, location, restriction_type, severity
        FROM notam_anomalies
        ORDER BY detected_at DESC
        LIMIT 5
    """).fetchall()
    if anoms:
        print("\nRecent anomalies:")
        for det, nid, loc, rtype, sev in anoms:
            print(f"  [{sev}] {det[:16]}  {nid:<15}  {(loc or '?'):>6}  {rtype}")
    else:
        print("\nNo anomalies logged yet.")

    total_anoms = conn.execute("SELECT COUNT(*) FROM notam_anomalies").fetchone()[0]
    print(f"\nTotal anomalies logged: {total_anoms}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="MarketSignal NOTAM collector (Laminar Data)")
    parser.add_argument("--loop",   action="store_true", help="Poll continuously every 30 minutes")
    parser.add_argument("--status", action="store_true", help="Print database summary and exit")
    args = parser.parse_args()

    conn = init_db(DB_PATH)

    if args.status:
        print_status(conn)
        conn.close()
        return

    if args.loop:
        print(f"Starting continuous NOTAM polling every {POLL_INTERVAL // 60} minutes. Ctrl+C to stop.")
        while True:
            try:
                poll(conn)
                print(f"  Sleeping {POLL_INTERVAL // 60}m ...")
                time.sleep(POLL_INTERVAL)
            except KeyboardInterrupt:
                print("\nStopped.")
                break
    else:
        poll(conn)

    conn.close()


if __name__ == "__main__":
    main()
