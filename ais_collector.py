#!/usr/bin/env python3
"""
MarketSignal — AIS Maritime Monitor (aisstream.io)

Streams AIS position data for the Middle East maritime zone every 30 minutes.
Tracks vessel density by type and region, watches specific MMSIs, and flags
anomalies: tanker route avoidance, military concentrations, GPS spoofing.

Signal logic mirrors adsb_collector.py:
  - Count tankers/cargo per region vs 7-day same-hour baseline
  - Drop >30% → MEDIUM anomaly; >50% → HIGH (route avoidance signal)
  - Military vessel surge >2× baseline → escalation signal
  - Watched MMSIs from VIP Vessels.csv logged every sighting

Usage:
    python3 ais_collector.py            # collect one snapshot
    python3 ais_collector.py --loop     # continuous, every 30 minutes
    python3 ais_collector.py --status   # print database summary
    python3 ais_collector.py --signals  # print active anomalies

Requirements:
    pip install websockets python-dotenv
"""

import asyncio
import websockets
import json
import sqlite3
import csv
import os
import time
import math
import argparse
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────

DB_PATH            = "ais_events.db"
WATCHLIST_PATH     = "VIP Vessels.csv"
COLLECT_WINDOW_SEC = 600    # 10 minutes of streaming per snapshot
POLL_INTERVAL      = 1800   # 30 minutes between polls
BASELINE_DAYS      = 7      # days of same-hour history for baseline
SPOOF_SOG_KNOTS    = 50.0   # speed above this = almost certainly spoofed

API_KEY = os.environ.get("AISSTREAM_API_KEY", "")

# aisstream.io WebSocket endpoint
WS_URL = "wss://stream.aisstream.io/v0/stream"

# Bounding box covering entire ME maritime zone [lon_min, lat_min], [lon_max, lat_max]
ME_BBOX = [[[30.0, 8.0], [72.0, 32.0]]]

# ── Regions ────────────────────────────────────────────────────────────────────

REGIONS = [
    {"id": "persian_gulf",  "label": "Persian Gulf",       "lat": (22.0, 30.0), "lon": (48.0, 60.0)},
    {"id": "hormuz",        "label": "Strait of Hormuz",   "lat": (25.5, 27.0), "lon": (56.0, 58.0)},
    {"id": "red_sea",       "label": "Red Sea",            "lat": (12.0, 30.0), "lon": (32.0, 44.0)},
    {"id": "gulf_of_aden",  "label": "Gulf of Aden",       "lat": (10.0, 14.0), "lon": (42.0, 52.0)},
    {"id": "arabian_sea",   "label": "Arabian Sea",        "lat": (10.0, 25.0), "lon": (55.0, 70.0)},
]

# ── Vessel type → category mapping (AIS ship type codes) ──────────────────────

VESSEL_CATEGORIES = {
    "tanker":    list(range(80, 90)),   # 80-89: oil, chemical, gas tankers
    "cargo":     list(range(70, 80)),   # 70-79: dry cargo, bulk, container
    "military":  [35],                  # 35: military
    "passenger": list(range(60, 70)),   # 60-69: passenger ships
}

# Anomaly thresholds
DROP_MEDIUM_PCT = 0.30   # >30% drop from baseline → MEDIUM
DROP_HIGH_PCT   = 0.50   # >50% drop → HIGH
SURGE_MULT      = 2.0    # >2× baseline military count → flag


# ── Helpers ────────────────────────────────────────────────────────────────────

def vessel_category(type_code):
    if type_code is None:
        return "unknown"
    for cat, codes in VESSEL_CATEGORIES.items():
        if type_code in codes:
            return cat
    return "other"


def region_for(lat, lon):
    """Return list of region IDs a position falls in (can overlap)."""
    matched = []
    for r in REGIONS:
        if r["lat"][0] <= lat <= r["lat"][1] and r["lon"][0] <= lon <= r["lon"][1]:
            matched.append(r["id"])
    return matched


def region_label(region_id):
    for r in REGIONS:
        if r["id"] == region_id:
            return r["label"]
    return region_id


def haversine_nm(lat1, lon1, lat2, lon2):
    """Distance in nautical miles between two points."""
    R = 3440.065  # Earth radius in NM
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def load_watchlist():
    """Load VIP Vessels.csv. Returns dict keyed by MMSI string."""
    watchlist = {}
    if not os.path.exists(WATCHLIST_PATH):
        return watchlist
    with open(WATCHLIST_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            mmsi = row.get("mmsi", "").strip()
            if mmsi:
                watchlist[mmsi] = row
    return watchlist


# ── Database ───────────────────────────────────────────────────────────────────

def init_db(db_path):
    conn = sqlite3.connect(db_path)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS vessel_snapshots (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_time     TEXT NOT NULL,
            region            TEXT NOT NULL,
            region_label      TEXT,
            category          TEXT NOT NULL,
            vessel_count      INTEGER NOT NULL,
            unique_mmsi_count INTEGER NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS vessel_sightings (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at  TEXT NOT NULL,
            mmsi         TEXT NOT NULL,
            vessel_name  TEXT,
            country      TEXT,
            operator     TEXT,
            vessel_type  TEXT,
            category     TEXT,
            lat          REAL,
            lon          REAL,
            sog          REAL,
            heading      REAL,
            region       TEXT,
            region_label TEXT,
            nav_status   INTEGER,
            signal_value TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS vessel_anomalies (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at      TEXT NOT NULL,
            region           TEXT NOT NULL,
            region_label     TEXT,
            category         TEXT,
            anomaly_type     TEXT,
            severity         TEXT,
            baseline_count   REAL,
            observed_count   INTEGER,
            drop_pct         REAL,
            detail           TEXT,
            last_confirmed_at TEXT,
            resolved_at      TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS spoofing_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at  TEXT NOT NULL,
            mmsi         TEXT NOT NULL,
            vessel_name  TEXT,
            lat          REAL,
            lon          REAL,
            reported_sog REAL,
            anomaly_type TEXT,
            detail       TEXT
        )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_time     ON vessel_snapshots(snapshot_time)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_region   ON vessel_snapshots(region)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sight_mmsi    ON vessel_sightings(mmsi)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_anom_region   ON vessel_anomalies(region)")
    conn.commit()
    return conn


# ── AIS streaming ──────────────────────────────────────────────────────────────

async def collect_window(watchlist, duration_sec=COLLECT_WINDOW_SEC):
    """
    Connect to aisstream.io, stream AIS messages for duration_sec, return aggregated results.

    Returns:
        counts   — {region_id: {category: set(mmsi)}}
        sightings — list of watchlist hit dicts
        spoof     — list of spoofing event dicts
        total     — total messages received
    """
    if not API_KEY:
        print("  [AIS] No API key — set AISSTREAM_API_KEY in .env")
        return {}, [], [], 0

    # Per-region, per-category sets of unique MMSIs seen
    counts = {r["id"]: {cat: set() for cat in list(VESSEL_CATEGORIES.keys()) + ["other", "unknown"]}
              for r in REGIONS}

    # MMSI → vessel type (populated from ShipStaticData)
    type_cache = {}
    # MMSI → last position (for spoofing detection)
    pos_cache  = {}

    sightings = []
    spoof     = []
    total     = 0

    sub_msg = json.dumps({
        "Apikey":             API_KEY,
        "BoundingBoxes":      ME_BBOX,
        "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
    })

    deadline = asyncio.get_event_loop().time() + duration_sec

    try:
        async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=30) as ws:
            await ws.send(sub_msg)

            while asyncio.get_event_loop().time() < deadline:
                try:
                    remaining = deadline - asyncio.get_event_loop().time()
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 30))
                except asyncio.TimeoutError:
                    break

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("MessageType")
                meta     = msg.get("MetaData", {})
                mmsi     = str(meta.get("MMSI") or "")
                name     = (meta.get("ShipName") or "").strip()
                lat      = meta.get("latitude")
                lon      = meta.get("longitude")

                if not mmsi or lat is None or lon is None:
                    continue

                total += 1

                # ── ShipStaticData → cache vessel type ─────────────────────
                if msg_type == "ShipStaticData":
                    ssd = msg.get("Message", {}).get("ShipStaticData", {})
                    vtype = ssd.get("Type")
                    if vtype is not None:
                        type_cache[mmsi] = int(vtype)
                    continue

                # ── PositionReport ─────────────────────────────────────────
                if msg_type != "PositionReport":
                    continue

                pr     = msg.get("Message", {}).get("PositionReport", {})
                sog    = pr.get("Sog", 0.0) or 0.0
                cog    = pr.get("Cog")
                nav    = pr.get("NavigationalStatus")

                vtype  = type_cache.get(mmsi)
                cat    = vessel_category(vtype)
                now    = datetime.now(timezone.utc).isoformat()

                # ── Spoofing detection ─────────────────────────────────────
                if sog > SPOOF_SOG_KNOTS:
                    spoof.append({
                        "detected_at":  now,
                        "mmsi":         mmsi,
                        "vessel_name":  name,
                        "lat":          lat,
                        "lon":          lon,
                        "reported_sog": sog,
                        "anomaly_type": "impossible_speed",
                        "detail":       f"SOG {sog:.1f}kn (max realistic ~{SPOOF_SOG_KNOTS}kn)",
                    })

                # Position jump detection (using previous position cache)
                if mmsi in pos_cache:
                    prev_lat, prev_lon, prev_time = pos_cache[mmsi]
                    now_dt   = datetime.now(timezone.utc)
                    prev_dt  = datetime.fromisoformat(prev_time)
                    mins_elapsed = (now_dt - prev_dt).total_seconds() / 60
                    if mins_elapsed > 0:
                        dist_nm = haversine_nm(prev_lat, prev_lon, lat, lon)
                        implied_sog = dist_nm / (mins_elapsed / 60)
                        if dist_nm > 50 and implied_sog > SPOOF_SOG_KNOTS:
                            spoof.append({
                                "detected_at":  now,
                                "mmsi":         mmsi,
                                "vessel_name":  name,
                                "lat":          lat,
                                "lon":          lon,
                                "reported_sog": sog,
                                "anomaly_type": "position_jump",
                                "detail":       f"{dist_nm:.0f}nm jump in {mins_elapsed:.0f}min "
                                                f"(implied {implied_sog:.0f}kn)",
                            })
                pos_cache[mmsi] = (lat, lon, now)

                # ── Assign to regions ──────────────────────────────────────
                regions_hit = region_for(lat, lon)
                for rid in regions_hit:
                    counts[rid][cat].add(mmsi)

                # ── Watchlist check ────────────────────────────────────────
                if mmsi in watchlist:
                    entry = watchlist[mmsi]
                    regions_str = ", ".join(region_label(r) for r in regions_hit) or "outside ME"
                    sightings.append({
                        "detected_at":  now,
                        "mmsi":         mmsi,
                        "vessel_name":  name or entry.get("name", ""),
                        "country":      entry.get("country", ""),
                        "operator":     entry.get("operator", ""),
                        "vessel_type":  entry.get("vessel_type", ""),
                        "category":     entry.get("category", ""),
                        "signal_value": entry.get("signal_value", ""),
                        "lat":          lat,
                        "lon":          lon,
                        "sog":          sog,
                        "heading":      cog,
                        "region":       regions_hit[0] if regions_hit else "unknown",
                        "region_label": regions_str,
                        "nav_status":   nav,
                    })

    except (websockets.exceptions.WebSocketException, OSError, asyncio.TimeoutError) as e:
        print(f"  [AIS] WebSocket error: {e}")

    return counts, sightings, spoof, total


# ── Storage ────────────────────────────────────────────────────────────────────

def save_snapshot(conn, counts, now_str):
    """Store aggregated vessel counts for this snapshot."""
    for r in REGIONS:
        rid = r["id"]
        for cat, mmsi_set in counts.get(rid, {}).items():
            if not mmsi_set:
                continue
            conn.execute("""
                INSERT INTO vessel_snapshots
                    (snapshot_time, region, region_label, category,
                     vessel_count, unique_mmsi_count)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (now_str, rid, r["label"], cat, len(mmsi_set), len(mmsi_set)))


def save_sightings(conn, sightings):
    for s in sightings:
        conn.execute("""
            INSERT INTO vessel_sightings
                (detected_at, mmsi, vessel_name, country, operator,
                 vessel_type, category, lat, lon, sog, heading,
                 region, region_label, nav_status, signal_value)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            s["detected_at"], s["mmsi"], s["vessel_name"], s["country"], s["operator"],
            s["vessel_type"], s["category"], s["lat"], s["lon"], s["sog"], s["heading"],
            s["region"], s["region_label"], s["nav_status"], s["signal_value"],
        ))


def save_spoofing(conn, spoof_events):
    for e in spoof_events:
        conn.execute("""
            INSERT INTO spoofing_events
                (detected_at, mmsi, vessel_name, lat, lon,
                 reported_sog, anomaly_type, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            e["detected_at"], e["mmsi"], e["vessel_name"],
            e["lat"], e["lon"], e["reported_sog"],
            e["anomaly_type"], e["detail"],
        ))


# ── Baseline & anomaly detection ───────────────────────────────────────────────

def get_baseline(conn, region, category, eval_time):
    """
    Average vessel count for same hour-of-day over the last BASELINE_DAYS days.
    Returns None if insufficient data.
    """
    eval_dt  = datetime.fromisoformat(eval_time)
    hour     = eval_dt.hour
    cutoff   = (eval_dt - timedelta(days=BASELINE_DAYS)).isoformat()

    rows = conn.execute("""
        SELECT vessel_count
        FROM vessel_snapshots
        WHERE region   = ?
          AND category = ?
          AND snapshot_time > ?
          AND CAST(SUBSTR(snapshot_time, 12, 2) AS INTEGER) BETWEEN ? AND ?
    """, (region, category, cutoff, max(0, hour - 1), min(23, hour + 1))).fetchall()

    if len(rows) < 3:
        return None
    return sum(r[0] for r in rows) / len(rows)


def check_anomalies(conn, counts, now_str):
    """
    Compare current snapshot to baseline.
    Upserts anomalies: UPDATE last_confirmed_at if active, INSERT if new.
    Resolves anomalies for region/category combos that no longer trigger.
    """
    triggered = set()  # (region, category, anomaly_type)

    for r in REGIONS:
        rid   = r["id"]
        label = r["label"]

        for cat in ["tanker", "cargo", "military"]:
            observed = len(counts.get(rid, {}).get(cat, set()))
            baseline = get_baseline(conn, rid, cat, now_str)

            if baseline is None or baseline < 2:
                continue  # not enough history

            drop_pct = (baseline - observed) / baseline

            # ── Traffic drop (tanker/cargo route avoidance) ────────────────
            if cat in ("tanker", "cargo") and drop_pct >= DROP_MEDIUM_PCT:
                severity = "HIGH" if drop_pct >= DROP_HIGH_PCT else "MEDIUM"
                atype    = "traffic_drop"
                key      = (rid, cat, atype)
                triggered.add(key)

                existing = conn.execute("""
                    SELECT id FROM vessel_anomalies
                    WHERE region = ? AND category = ? AND anomaly_type = ?
                      AND resolved_at IS NULL
                    ORDER BY detected_at DESC LIMIT 1
                """, (rid, cat, atype)).fetchone()

                detail = (f"{cat} count {observed} vs baseline {baseline:.1f} "
                          f"({drop_pct*100:.0f}% drop)")

                if existing:
                    conn.execute("""
                        UPDATE vessel_anomalies
                        SET last_confirmed_at = ?, severity = ?, observed_count = ?,
                            drop_pct = ?, detail = ?
                        WHERE id = ?
                    """, (now_str, severity, observed, drop_pct, detail, existing[0]))
                else:
                    conn.execute("""
                        INSERT INTO vessel_anomalies
                            (detected_at, region, region_label, category, anomaly_type,
                             severity, baseline_count, observed_count, drop_pct,
                             detail, last_confirmed_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (now_str, rid, label, cat, atype, severity, baseline,
                          observed, drop_pct, detail, now_str))

            # ── Military surge ─────────────────────────────────────────────
            if cat == "military" and baseline > 0 and observed >= baseline * SURGE_MULT:
                atype = "military_surge"
                key   = (rid, cat, atype)
                triggered.add(key)

                existing = conn.execute("""
                    SELECT id FROM vessel_anomalies
                    WHERE region = ? AND category = ? AND anomaly_type = ?
                      AND resolved_at IS NULL
                    ORDER BY detected_at DESC LIMIT 1
                """, (rid, cat, atype)).fetchone()

                detail = (f"military vessels {observed} vs baseline {baseline:.1f} "
                          f"({observed/baseline:.1f}×)")
                severity = "HIGH"

                if existing:
                    conn.execute("""
                        UPDATE vessel_anomalies
                        SET last_confirmed_at = ?, observed_count = ?, detail = ?
                        WHERE id = ?
                    """, (now_str, observed, detail, existing[0]))
                else:
                    conn.execute("""
                        INSERT INTO vessel_anomalies
                            (detected_at, region, region_label, category, anomaly_type,
                             severity, baseline_count, observed_count, drop_pct,
                             detail, last_confirmed_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (now_str, rid, label, cat, atype, "HIGH", baseline,
                          observed, None, detail, now_str))

    # Resolve anomalies no longer firing
    active = conn.execute("""
        SELECT id, region, category, anomaly_type
        FROM vessel_anomalies WHERE resolved_at IS NULL
    """).fetchall()

    for row_id, region, cat, atype in active:
        if (region, cat, atype) not in triggered:
            conn.execute(
                "UPDATE vessel_anomalies SET resolved_at = ? WHERE id = ?",
                (now_str, row_id)
            )


# ── Poll ───────────────────────────────────────────────────────────────────────

def poll(conn, watchlist):
    now_str  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    print(f"\nAIS Poll — {now_str}")
    print("-" * 65)
    print(f"  Streaming for {COLLECT_WINDOW_SEC // 60} minutes...")

    counts, sightings, spoof, total = asyncio.run(
        collect_window(watchlist, COLLECT_WINDOW_SEC)
    )

    now_iso = datetime.now(timezone.utc).isoformat()

    if not any(counts.values()):
        print("  No AIS data returned — check API key or connection.")
        return

    save_snapshot(conn, counts, now_iso)
    save_sightings(conn, sightings)
    save_spoofing(conn, spoof)
    check_anomalies(conn, counts, now_iso)
    conn.commit()

    print(f"  Messages received: {total:,}")
    print()

    # Per-region summary
    print(f"  {'Region':<22}  {'Tankers':>7}  {'Cargo':>7}  {'Military':>9}  {'Other':>6}")
    print(f"  {'-' * 60}")
    for r in REGIONS:
        rid  = r["id"]
        rcounts = counts.get(rid, {})
        tan  = len(rcounts.get("tanker",   set()))
        cgo  = len(rcounts.get("cargo",    set()))
        mil  = len(rcounts.get("military", set()))
        oth  = len(rcounts.get("other",    set())) + len(rcounts.get("unknown", set()))
        if tan + cgo + mil + oth == 0:
            continue
        print(f"  {r['label']:<22}  {tan:>7}  {cgo:>7}  {mil:>9}  {oth:>6}")

    if sightings:
        print(f"\n  *** {len(sightings)} WATCHLIST SIGHTING(S) ***")
        seen = set()
        for s in sightings:
            if s["mmsi"] not in seen:
                seen.add(s["mmsi"])
                print(f"  [{s['signal_value'].upper()}] {s['mmsi']:<12}  "
                      f"{s['vessel_name']:<22}  {s['region_label']}  "
                      f"SOG {s['sog']:.1f}kn")

    if spoof:
        print(f"\n  *** {len(spoof)} GPS SPOOFING EVENT(S) ***")
        for e in spoof:
            print(f"  {e['mmsi']:<12}  {e['vessel_name']:<20}  {e['detail']}")

    # Anomaly summary
    anoms = conn.execute("""
        SELECT region_label, category, anomaly_type, severity
        FROM vessel_anomalies
        WHERE resolved_at IS NULL
        ORDER BY detected_at DESC
    """).fetchall()

    if anoms:
        print(f"\n  Active anomalies ({len(anoms)}):")
        for label, cat, atype, sev in anoms:
            print(f"  [{sev}] {label:<22}  {cat:<10}  {atype}")
    else:
        print("\n  No active anomalies.")


# ── Status ─────────────────────────────────────────────────────────────────────

def print_status(conn):
    total = conn.execute("SELECT COUNT(*) FROM vessel_snapshots").fetchone()[0]
    if not total:
        print("No AIS data yet. Run without --status first.")
        return

    dr = conn.execute(
        "SELECT MIN(snapshot_time), MAX(snapshot_time) FROM vessel_snapshots"
    ).fetchone()
    print(f"\nAIS Database Summary")
    print("=" * 65)
    print(f"Total snapshots:    {total:,}")
    print(f"First snapshot:     {dr[0][:16] if dr[0] else '—'}")
    print(f"Last snapshot:      {dr[1][:16] if dr[1] else '—'}")

    print("\nRecent counts by region (last snapshot):")
    last_snap = dr[1]
    if last_snap:
        rows = conn.execute("""
            SELECT region_label, category, vessel_count
            FROM vessel_snapshots
            WHERE snapshot_time = ?
            ORDER BY region_label, category
        """, (last_snap,)).fetchall()
        for label, cat, cnt in rows:
            print(f"  {label:<22}  {cat:<12}  {cnt:>4}")

    anoms = conn.execute("""
        SELECT detected_at, region_label, category, anomaly_type, severity
        FROM vessel_anomalies
        WHERE resolved_at IS NULL
        ORDER BY detected_at DESC
    """).fetchall()
    print(f"\nActive anomalies: {len(anoms)}")
    for det, label, cat, atype, sev in anoms:
        print(f"  [{sev}] {det[:16]}  {label:<22}  {cat}  {atype}")

    spoof_total = conn.execute("SELECT COUNT(*) FROM spoofing_events").fetchone()[0]
    print(f"\nGPS spoofing events logged: {spoof_total}")

    sight_total = conn.execute("SELECT COUNT(*) FROM vessel_sightings").fetchone()[0]
    print(f"Watchlist sightings logged: {sight_total}")


def print_signals(conn):
    anoms = conn.execute("""
        SELECT detected_at, region_label, category, anomaly_type,
               severity, baseline_count, observed_count, drop_pct, detail,
               last_confirmed_at
        FROM vessel_anomalies
        WHERE resolved_at IS NULL
        ORDER BY detected_at DESC
    """).fetchall()

    if not anoms:
        print("No active AIS anomalies.")
        return

    print(f"\nActive AIS Anomalies ({len(anoms)})")
    print("=" * 65)
    for det, label, cat, atype, sev, base, obs, drop, detail, lca in anoms:
        print(f"\n  [{sev}] {label} — {cat} {atype}")
        print(f"  Detected:  {det[:16]}")
        print(f"  Confirmed: {(lca or det)[:16]}")
        print(f"  Detail:    {detail}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="MarketSignal AIS collector")
    parser.add_argument("--loop",    action="store_true", help="Poll continuously every 30 minutes")
    parser.add_argument("--status",  action="store_true", help="Print database summary and exit")
    parser.add_argument("--signals", action="store_true", help="Print active anomalies and exit")
    args = parser.parse_args()

    conn      = init_db(DB_PATH)
    watchlist = load_watchlist()

    if watchlist:
        print(f"  Loaded {len(watchlist)} vessels from watchlist")

    if args.status:
        print_status(conn)
        conn.close()
        return

    if args.signals:
        print_signals(conn)
        conn.close()
        return

    if args.loop:
        sleep_sec = POLL_INTERVAL - COLLECT_WINDOW_SEC
        print(f"Starting continuous AIS polling (collect {COLLECT_WINDOW_SEC//60}min, "
              f"sleep {sleep_sec//60}min). Ctrl+C to stop.")
        while True:
            try:
                poll(conn, watchlist)
                print(f"  Sleeping {sleep_sec // 60}m ...")
                time.sleep(sleep_sec)
            except KeyboardInterrupt:
                print("\nStopped.")
                break
    else:
        poll(conn, watchlist)

    conn.close()


if __name__ == "__main__":
    main()
