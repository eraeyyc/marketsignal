#!/usr/bin/env python3
"""
MarketSignal — ADS-B Airspace Monitor (OpenSky Network)

Polls live aircraft state vectors over Middle East bounding boxes every 10 minutes.
Stores counts and aircraft details to SQLite for anomaly detection.

Usage:
    python3 adsb_collector.py            # run once (one poll of all regions)
    python3 adsb_collector.py --loop     # continuous polling every 10 minutes
    python3 adsb_collector.py --status   # print recent snapshot summary
    python3 adsb_collector.py --anomaly  # check for anomalies vs 7-day baseline
"""

import sqlite3
import requests
import json
import time
import argparse
from datetime import datetime, timedelta, timezone

# ── Config ─────────────────────────────────────────────────────────────────────

DB_PATH       = "adsb_events.db"
OPENSKY_URL   = "https://opensky-network.org/api/states/all"
TOKEN_URL     = (
    "https://auth.opensky-network.org/auth/realms/opensky-network"
    "/protocol/openid-connect/token"
)
POLL_INTERVAL        = 600   # 10 minutes in seconds
ANOMALY_DROP         = 0.40  # flag if count drops >40% below baseline
TOKEN_REFRESH_MARGIN = 30    # seconds before expiry to proactively refresh

# OpenSky OAuth2 credentials — loaded from credentials.json
# Format: {"clientId": "...", "clientSecret": "..."}
CREDENTIALS_FILE = "credentials.json"

def _load_credentials():
    try:
        with open(CREDENTIALS_FILE) as f:
            c = json.load(f)
        return c.get("clientId", ""), c.get("clientSecret", "")
    except FileNotFoundError:
        return "", ""
    except Exception as e:
        print(f"Warning: could not load {CREDENTIALS_FILE}: {e}")
        return "", ""

CLIENT_ID, CLIENT_SECRET = _load_credentials()


# ── OAuth2 Token Manager ────────────────────────────────────────────────────────

class TokenManager:
    def __init__(self):
        self.token      = None
        self.expires_at = None

    def get_token(self):
        """Return a valid access token, refreshing automatically if needed."""
        if self.token and self.expires_at and datetime.now() < self.expires_at:
            return self.token
        return self._refresh()

    def _refresh(self):
        """Fetch a new access token from the OpenSky authentication server."""
        r = requests.post(
            TOKEN_URL,
            data={
                "grant_type":    "client_credentials",
                "client_id":     CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            },
            timeout=15,
        )
        r.raise_for_status()
        data            = r.json()
        self.token      = data["access_token"]
        expires_in      = data.get("expires_in", 1800)
        self.expires_at = datetime.now() + timedelta(seconds=expires_in - TOKEN_REFRESH_MARGIN)
        print("  [auth] OAuth2 token refreshed.")
        return self.token

    def headers(self):
        """Return request headers with a valid Bearer token."""
        return {"Authorization": f"Bearer {self.get_token()}"}


# Single shared instance — reused across all requests in the script.
_tokens = TokenManager() if CLIENT_ID else None

# ── Bounding boxes ─────────────────────────────────────────────────────────────
# Each entry: (region_id, label, lat_min, lon_min, lat_max, lon_max)

REGIONS = [
    ("ISR",  "Israel / Palestine",     28.5,  33.5,  33.5,  36.0),
    ("LBN",  "Lebanon / Syria",        33.0,  35.0,  37.5,  42.0),
    ("IRN",  "Iran",                   25.0,  44.0,  40.0,  64.0),
    ("GULF", "Persian Gulf / Qatar",   22.5,  49.5,  27.5,  57.0),
    ("YEM",  "Yemen / Red Sea",        11.0,  41.0,  20.0,  50.0),
    ("EGY",  "Egypt / Sinai",          22.0,  24.5,  31.5,  37.5),
    ("JOR",  "Jordan",                 29.0,  34.5,  33.5,  39.5),
    ("TUR",  "Turkey",                 35.5,  25.5,  42.5,  44.5),
    ("SAU",  "Saudi Arabia",           16.0,  34.5,  32.5,  56.0),
]

# Airline callsign prefixes to watch — these are major carriers
# that signal airspace safety confidence by their presence/absence
WATCHED_AIRLINES = {
    "ELY": "El Al",
    "BAW": "British Airways",
    "AFR": "Air France",
    "DLH": "Lufthansa",
    "UAE": "Emirates",
    "QTR": "Qatar Airways",
    "THY": "Turkish Airlines",
    "RYR": "Ryanair",
    "DAL": "Delta",
    "UAL": "United",
    "ETD": "Etihad",
    "SVA": "Saudia",
    "MEA": "Middle East Airlines",
}


# ── Database ───────────────────────────────────────────────────────────────────

def init_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            polled_at       TEXT NOT NULL,          -- ISO UTC timestamp
            polled_unix     INTEGER NOT NULL,        -- Unix timestamp
            hour_utc        INTEGER NOT NULL,        -- 0-23 (for baseline grouping)
            dow             INTEGER NOT NULL,        -- 0=Mon 6=Sun
            region          TEXT NOT NULL,
            region_label    TEXT,
            aircraft_count  INTEGER NOT NULL,
            on_ground       INTEGER NOT NULL,
            airborne        INTEGER NOT NULL,
            aircraft_json   TEXT                    -- JSON list of [icao24, callsign, country, alt, heading]
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS anomalies (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at     TEXT NOT NULL,
            region          TEXT NOT NULL,
            region_label    TEXT,
            current_count   INTEGER,
            baseline_avg    REAL,
            drop_pct        REAL,
            severity        TEXT                    -- LOW / MEDIUM / HIGH
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_region  ON snapshots(region)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_time    ON snapshots(polled_unix)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_hour    ON snapshots(hour_utc, dow)")
    conn.commit()
    return conn


# ── OpenSky fetch ──────────────────────────────────────────────────────────────

def fetch_region(region_id, label, lamin, lomin, lamax, lomax):
    """Fetch live aircraft states for one bounding box. Returns list of state vectors."""
    params = {
        "lamin": lamin, "lomin": lomin,
        "lamax": lamax, "lomax": lomax,
    }
    headers = _tokens.headers() if _tokens else {}
    try:
        r = requests.get(OPENSKY_URL, params=params, headers=headers, timeout=20)
        if r.status_code == 429:
            print(f"    [{region_id}] Rate limited — skipping")
            return None
        r.raise_for_status()
        data = r.json()
        return data.get("states") or []
    except requests.RequestException as e:
        print(f"    [{region_id}] Error: {e}")
        return None


def parse_states(states):
    """
    OpenSky state vector fields (by index):
    0  icao24       1  callsign     2  origin_country
    3  time_position 4 last_contact 5  longitude
    6  latitude     7  baro_altitude 8 on_ground
    9  velocity     10 true_track   11 vertical_rate
    12 sensors      13 geo_altitude 14 squawk
    15 spi          16 position_source
    """
    aircraft = []
    on_ground = 0
    airborne  = 0
    for s in states:
        if s[8]:  # on_ground flag
            on_ground += 1
        else:
            airborne += 1
        aircraft.append({
            "icao24":   s[0],
            "callsign": (s[1] or "").strip(),
            "country":  s[2],
            "alt_m":    s[7],
            "heading":  s[10],
            "on_ground": s[8],
        })
    return aircraft, on_ground, airborne


# ── Storage ────────────────────────────────────────────────────────────────────

def save_snapshot(conn, region_id, label, states):
    now     = datetime.now(timezone.utc)
    unix_ts = int(now.timestamp())
    aircraft, on_ground, airborne = parse_states(states)

    # Compact aircraft list for storage
    compact = [[a["icao24"], a["callsign"], a["country"], a["alt_m"], a["heading"]]
               for a in aircraft]

    conn.execute("""
        INSERT INTO snapshots
            (polled_at, polled_unix, hour_utc, dow, region, region_label,
             aircraft_count, on_ground, airborne, aircraft_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        now.isoformat(), unix_ts,
        now.hour, now.weekday(),
        region_id, label,
        len(states), on_ground, airborne,
        json.dumps(compact),
    ))
    conn.commit()
    return aircraft, on_ground, airborne


# ── Anomaly detection ──────────────────────────────────────────────────────────

def check_anomalies(conn):
    """
    Compare each region's latest snapshot against the 7-day rolling baseline
    for the same hour-of-day and day-of-week.
    """
    print(f"\nAnomaly check — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%MZ')}")
    print("=" * 65)

    # Get the most recent snapshot per region
    latest = conn.execute("""
        SELECT region, region_label, aircraft_count, hour_utc, dow, polled_at
        FROM snapshots
        WHERE polled_unix >= (SELECT MAX(polled_unix) - 1200 FROM snapshots)
        ORDER BY polled_unix DESC
    """).fetchall()

    if not latest:
        print("No recent snapshots found. Run a poll first.")
        return

    flags = []
    for region, label, current, hour, dow, polled_at in latest:
        # Baseline: same hour ±1, same dow, last 7 days
        baseline = conn.execute("""
            SELECT AVG(aircraft_count), COUNT(*)
            FROM snapshots
            WHERE region = ?
              AND hour_utc BETWEEN ? AND ?
              AND dow = ?
              AND polled_unix < (SELECT MAX(polled_unix) - 86400 FROM snapshots)
              AND polled_unix > (SELECT MAX(polled_unix) - 7*86400 FROM snapshots)
        """, (region, max(0, hour-1), min(23, hour+1), dow)).fetchone()

        avg_baseline, n_samples = baseline
        if not avg_baseline or n_samples < 3:
            print(f"  {label:<30} current={current:>3}   baseline=insufficient data ({n_samples} samples)")
            continue

        drop_pct = (avg_baseline - current) / avg_baseline if avg_baseline > 0 else 0

        if drop_pct >= 0.6:
            severity = "HIGH"
        elif drop_pct >= ANOMALY_DROP:
            severity = "MEDIUM"
        elif drop_pct >= 0.20:
            severity = "LOW"
        else:
            severity = None

        marker = f"  *** {severity} ***" if severity else ""
        print(f"  {label:<30} current={current:>3}  baseline={avg_baseline:.1f}  drop={drop_pct*100:.0f}%{marker}")

        if severity:
            flags.append((region, label, current, avg_baseline, drop_pct, severity))
            conn.execute("""
                INSERT INTO anomalies
                    (detected_at, region, region_label, current_count, baseline_avg, drop_pct, severity)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now(timezone.utc).isoformat(),
                region, label, current, avg_baseline, round(drop_pct, 3), severity
            ))

    conn.commit()

    if flags:
        print(f"\n  {len(flags)} anomaly/anomalies flagged and saved.")
    else:
        print("\n  No anomalies detected.")


# ── Status summary ─────────────────────────────────────────────────────────────

def print_status(conn):
    total = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    if not total:
        print("No data yet. Run a poll first.")
        return

    date_range = conn.execute(
        "SELECT MIN(polled_at), MAX(polled_at) FROM snapshots"
    ).fetchone()
    print(f"\nADS-B Database Summary")
    print("=" * 65)
    print(f"Total snapshots:  {total:,}")
    print(f"Date range:       {date_range[0][:16]} → {date_range[1][:16]}")

    print("\nMost recent counts by region:")
    rows = conn.execute("""
        SELECT s.region_label, s.aircraft_count, s.airborne, s.on_ground, s.polled_at
        FROM snapshots s
        INNER JOIN (
            SELECT region, MAX(polled_unix) AS mx FROM snapshots GROUP BY region
        ) latest ON s.region = latest.region AND s.polled_unix = latest.mx
        ORDER BY s.aircraft_count DESC
    """).fetchall()
    print(f"  {'Region':<30} {'Total':>6} {'Airborne':>9} {'Ground':>7}  Polled at")
    print("  " + "-" * 62)
    for label, total_ac, airborne, on_ground, polled_at in rows:
        print(f"  {label:<30} {total_ac:>6} {airborne:>9} {on_ground:>7}  {polled_at[:16]}")

    # Watched airline sightings in last snapshot
    print("\nWatched airline sightings (latest snapshot):")
    last_unix = conn.execute("SELECT MAX(polled_unix) FROM snapshots").fetchone()[0]
    rows = conn.execute("""
        SELECT aircraft_json FROM snapshots
        WHERE polled_unix >= ? - 1200
    """, (last_unix,)).fetchall()

    seen_airlines = {}
    for (json_str,) in rows:
        for ac in json.loads(json_str):
            callsign = ac[1]
            if callsign and len(callsign) >= 3:
                prefix = callsign[:3]
                if prefix in WATCHED_AIRLINES:
                    airline = WATCHED_AIRLINES[prefix]
                    seen_airlines[airline] = seen_airlines.get(airline, 0) + 1

    if seen_airlines:
        for airline, count in sorted(seen_airlines.items(), key=lambda x: -x[1]):
            print(f"  {airline:<25} {count} aircraft")
    else:
        print("  None of the watched airlines currently visible.")

    # Recent anomalies
    recent_anomalies = conn.execute("""
        SELECT detected_at, region_label, current_count, baseline_avg, drop_pct, severity
        FROM anomalies
        ORDER BY detected_at DESC
        LIMIT 5
    """).fetchall()
    if recent_anomalies:
        print("\nRecent anomalies:")
        for det, label, curr, base, drop, sev in recent_anomalies:
            print(f"  [{sev}] {det[:16]}  {label:<28}  count={curr}  baseline={base:.1f}  drop={drop*100:.0f}%")


# ── Poll ───────────────────────────────────────────────────────────────────────

def poll_all(conn, verbose=True):
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    if verbose:
        print(f"\nPoll — {now_str}")
        print("-" * 65)

    for region_id, label, lamin, lomin, lamax, lomax in REGIONS:
        states = fetch_region(region_id, label, lamin, lomin, lamax, lomax)
        if states is None:
            continue  # error already printed

        aircraft, on_ground, airborne = save_snapshot(conn, region_id, label, states)

        if verbose:
            print(f"  {label:<30} total={len(states):>3}  airborne={airborne:>3}  ground={on_ground:>2}")

            # Show watched airlines spotted
            spotted = set()
            for a in aircraft:
                cs = a["callsign"]
                if cs and len(cs) >= 3 and cs[:3] in WATCHED_AIRLINES:
                    spotted.add(WATCHED_AIRLINES[cs[:3]])
            if spotted:
                print(f"    Airlines: {', '.join(sorted(spotted))}")

        # Small delay between region requests to avoid rate limits
        time.sleep(2)

    if verbose:
        print(f"\nPoll complete.")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop",    action="store_true", help="Poll continuously every 10 minutes")
    parser.add_argument("--status",  action="store_true", help="Show database summary")
    parser.add_argument("--anomaly", action="store_true", help="Check for anomalies vs baseline")
    args = parser.parse_args()

    conn = init_db(DB_PATH)

    if args.status:
        print_status(conn)
        conn.close()
        return

    if args.anomaly:
        check_anomalies(conn)
        conn.close()
        return

    if args.loop:
        print(f"Starting continuous polling every {POLL_INTERVAL // 60} minutes. Ctrl+C to stop.")
        while True:
            try:
                poll_all(conn)
                check_anomalies(conn)
                print(f"  Sleeping {POLL_INTERVAL // 60}m...")
                time.sleep(POLL_INTERVAL)
            except KeyboardInterrupt:
                print("\nStopped.")
                break
    else:
        poll_all(conn)

    conn.close()


if __name__ == "__main__":
    main()
