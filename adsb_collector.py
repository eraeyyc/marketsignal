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
import os
import time
import argparse
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

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

# OpenSky OAuth2 credentials — from .env (falls back to credentials.json)
def _load_credentials():
    client_id     = os.environ.get("OPENSKY_CLIENT_ID", "")
    client_secret = os.environ.get("OPENSKY_CLIENT_SECRET", "")
    if client_id and client_secret:
        return client_id, client_secret
    # Legacy fallback: credentials.json
    try:
        with open("credentials.json") as f:
            c = json.load(f)
        return c.get("clientId", ""), c.get("clientSecret", "")
    except FileNotFoundError:
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

VIP_CSV          = "VIP Aircraft.csv"
AIRCRAFT_DB_CSV  = "aircraft-database-complete.csv"
GOING_DARK_HOURS = 24   # flag VIP aircraft as "going dark" after this many hours unseen

# ── Mode B: strategic aircraft type categories ──────────────────────────────────
STRATEGIC_TYPES = {
    "strategic_lift": {"C17", "C5M", "IL76", "Y20"},
    "tanker":         {"KC135", "KC46", "A332"},
    "isr_command":    {"RC35", "E3CF", "E3TF", "RQ4", "E8"},
    "bizjet":         {"F2TH", "F900", "F7X", "F8X", "GL5T", "GLEX",
                       "GLF4", "GLF5", "GLF6", "G280", "GL7T", "C56X"},
}
# Flat set for fast membership checks
ALL_STRATEGIC = {t for types in STRATEGIC_TYPES.values() for t in types}

# Reverse map: typecode → category
TYPECODE_CATEGORY = {t: cat for cat, types in STRATEGIC_TYPES.items() for t in types}

# Mode B watch regions — broader zones for strategic type clustering
# (region_id, label, lat_min, lon_min, lat_max, lon_max)
TYPE_WATCH_REGIONS = [
    ("TW_PGULF", "Persian Gulf Watch",   22, 48, 30, 60),
    ("TW_EMED",  "Eastern Med Watch",    30, 28, 38, 38),
    ("TW_HORN",  "Horn of Africa Watch",  8, 40, 16, 52),
    ("TW_CAUC",  "Caucasus Corridor",    38, 40, 44, 52),
]

# Major airports for bizjet clustering (name, icao, lat, lon)
MAJOR_AIRPORTS = [
    ("Dubai",        "OMDB",  25.2528,  55.3644),
    ("Abu Dhabi",    "OMAA",  24.4328,  54.6511),
    ("Doha",         "OTHH",  25.2731,  51.6083),
    ("Riyadh",       "OERK",  24.9597,  46.6988),
    ("Jeddah",       "OEJN",  21.6796,  39.1565),
    ("Kuwait",       "OKBK",  29.2267,  47.9689),
    ("Muscat",       "OOMS",  23.5933,  58.2844),
    ("Tehran",       "OIIE",  35.4161,  51.1522),
    ("Beirut",       "OLBA",  33.8209,  35.4884),
    ("Amman",        "OJAM",  31.9726,  35.9919),
    ("Cairo",        "HECA",  30.1219,  31.4056),
    ("Tel Aviv",     "LLBG",  32.0055,  34.8854),
    ("Istanbul",     "LTFM",  41.2753,  28.7519),
    ("Ankara",       "LTAC",  39.9455,  32.6888),
    ("Erbil",        "ORER",  36.2376,  43.9632),
    ("Baghdad",      "ORBI",  33.2625,  44.2346),
    ("Bahrain",      "OBBI",  26.2708,  50.6336),
    ("Sharjah",      "OMSJ",  25.3275,  55.5172),
]
AIRPORT_CLUSTER_RADIUS_KM = 50   # aircraft within this radius counted as "at" that airport
BIZJET_CLUSTER_MIN        = 3    # 3+ bizjets from different countries = flag
BIZJET_CLUSTER_WINDOW_H   = 12   # look-back window for bizjet clustering


def _haversine_km(lat1, lon1, lat2, lon2):
    """Approximate great-circle distance in km."""
    import math
    R    = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a    = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))


def nearest_airport(lat, lon):
    """Return (name, icao) of nearest major airport within AIRPORT_CLUSTER_RADIUS_KM, or None."""
    if lat is None or lon is None:
        return None
    best_dist, best_airport = float("inf"), None
    for name, icao, alat, alon in MAJOR_AIRPORTS:
        d = _haversine_km(lat, lon, alat, alon)
        if d < best_dist:
            best_dist, best_airport = d, (name, icao)
    if best_dist <= AIRPORT_CLUSTER_RADIUS_KM:
        return best_airport
    return None


# ── Startup data loading ────────────────────────────────────────────────────────

def _load_vip_watchlist():
    """Load VIP Aircraft.csv → dict icao24 → {tail, operator, country, type, category, signal_value}.
    Skips rows with empty icao24."""
    import csv
    result = {}
    try:
        with open(VIP_CSV, newline="") as f:
            for row in csv.DictReader(f):
                icao = (row.get("icao24") or "").strip().lower()
                if icao:
                    result[icao] = {
                        "tail":         row.get("tail_number", ""),
                        "operator":     row.get("operator", ""),
                        "country":      row.get("country", ""),
                        "aircraft_type":row.get("aircraft_type", ""),
                        "category":     row.get("category", ""),
                        "signal_value": row.get("signal_value", ""),
                    }
        print(f"  [VIP] Loaded {len(result)} VIP aircraft with known ICAO24s")
    except FileNotFoundError:
        print(f"  [VIP] {VIP_CSV} not found — VIP tracking disabled")
    return result


def _load_typecode_db():
    """Load aircraft-database-complete.csv → dict icao24 → typecode.
    Only loads strategic types to keep memory small."""
    import csv
    import sys
    csv.field_size_limit(min(sys.maxsize, 10_000_000))
    result = {}
    try:
        with open(AIRCRAFT_DB_CSV, newline="") as f:
            reader = csv.DictReader(f)
            # Field names may be single-quoted (e.g. "'icao24'") — build a normalised key map
            raw_fields = reader.fieldnames or []
            norm       = {k.strip("'"): k for k in raw_fields}
            tc_key     = norm.get("typecode",  "typecode")
            icao_key   = norm.get("icao24",    "icao24")
            for row in reader:
                tc = (row.get(tc_key) or "").strip().strip("'").upper()
                if tc in ALL_STRATEGIC:
                    icao = (row.get(icao_key) or "").strip().strip("'").lower()
                    if icao:
                        result[icao] = tc
        print(f"  [TypeDB] Loaded {len(result)} strategic aircraft from type database")
    except FileNotFoundError:
        print(f"  [TypeDB] {AIRCRAFT_DB_CSV} not found — Mode B type lookup disabled")
    return result


# Load at module level — used throughout
VIP_WATCH   = _load_vip_watchlist()
TYPECODE_DB = _load_typecode_db()

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

    # ── Mode A: VIP aircraft tracking ──────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vip_sightings (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at    TEXT NOT NULL,
            icao24         TEXT NOT NULL,
            tail_number    TEXT,
            operator       TEXT,
            country        TEXT,
            aircraft_type  TEXT,
            category       TEXT,
            signal_value   TEXT,
            region         TEXT,
            region_label   TEXT,
            callsign       TEXT,
            lat            REAL,
            lon            REAL,
            altitude_m     REAL,
            on_ground      INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vip_last_seen (
            icao24         TEXT PRIMARY KEY,
            tail_number    TEXT,
            operator       TEXT,
            last_seen_at   TEXT NOT NULL,
            last_region    TEXT,
            last_lat       REAL,
            last_lon       REAL,
            dark_flagged   INTEGER DEFAULT 0   -- 1 once a going-dark alert has been filed
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vip_dark_events (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at    TEXT NOT NULL,
            icao24         TEXT NOT NULL,
            tail_number    TEXT,
            operator       TEXT,
            last_seen_at   TEXT,
            last_region    TEXT,
            last_lat       REAL,
            last_lon       REAL,
            hours_dark     REAL
        )
    """)

    # ── Mode B: strategic type clustering ──────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS type_watch_counts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            polled_at       TEXT NOT NULL,
            polled_unix     INTEGER NOT NULL,
            region          TEXT NOT NULL,
            category        TEXT NOT NULL,    -- strategic_lift / tanker / isr_command / bizjet
            count           INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS type_anomalies (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at     TEXT NOT NULL,
            region          TEXT NOT NULL,
            region_label    TEXT,
            category        TEXT NOT NULL,
            current_count   INTEGER,
            baseline_mean   REAL,
            baseline_std    REAL,
            sigma_above     REAL,
            severity        TEXT,             -- MEDIUM (2-3σ) / HIGH (>3σ)
            aircraft_seen   TEXT              -- JSON list of [icao24, typecode] spotted
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bizjet_clusters (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at     TEXT NOT NULL,
            airport_name    TEXT,
            airport_icao    TEXT,
            bizjet_count    INTEGER,
            countries       TEXT,             -- JSON list of origin countries
            aircraft_json   TEXT              -- JSON list of [icao24, typecode, country, lat, lon]
        )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_vip_icao     ON vip_sightings(icao24)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vip_time     ON vip_sightings(detected_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_twc_region   ON type_watch_counts(region, polled_unix)")
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
            "icao24":    s[0],
            "callsign":  (s[1] or "").strip(),
            "country":   s[2],
            "lon":       s[5],
            "lat":       s[6],
            "alt_m":     s[7],
            "on_ground": bool(s[8]),
            "heading":   s[10],
        })
    return aircraft, on_ground, airborne


# ── Storage ────────────────────────────────────────────────────────────────────

def save_snapshot(conn, region_id, label, states):
    now     = datetime.now(timezone.utc)
    unix_ts = int(now.timestamp())
    aircraft, on_ground, airborne = parse_states(states)

    # Compact aircraft list for storage — [icao24, callsign, country, alt_m, heading, lat, lon, on_ground]
    # Indices 5-7 are new; older rows will have only 5 fields (handled gracefully on read)
    compact = [[a["icao24"], a["callsign"], a["country"], a["alt_m"], a["heading"],
                a["lat"], a["lon"], int(a["on_ground"])]
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


# ── Mode A: VIP tracking ───────────────────────────────────────────────────────

def process_vip_sightings(conn, region_id, region_label, aircraft):
    """Check aircraft list for VIP icao24 matches. Log sightings, update last_seen."""
    now = datetime.now(timezone.utc).isoformat()
    hits = []
    for a in aircraft:
        icao = (a["icao24"] or "").lower()
        if icao not in VIP_WATCH:
            continue
        vip = VIP_WATCH[icao]
        conn.execute("""
            INSERT INTO vip_sightings
                (detected_at, icao24, tail_number, operator, country,
                 aircraft_type, category, signal_value,
                 region, region_label, callsign,
                 lat, lon, altitude_m, on_ground)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            now, icao, vip["tail"], vip["operator"], vip["country"],
            vip["aircraft_type"], vip["category"], vip["signal_value"],
            region_id, region_label, a["callsign"],
            a["lat"], a["lon"], a["alt_m"], int(a["on_ground"]),
        ))
        conn.execute("""
            INSERT INTO vip_last_seen
                (icao24, tail_number, operator, last_seen_at, last_region, last_lat, last_lon, dark_flagged)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(icao24) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                last_region  = excluded.last_region,
                last_lat     = excluded.last_lat,
                last_lon     = excluded.last_lon,
                dark_flagged = 0
        """, (icao, vip["tail"], vip["operator"], now, region_id, a["lat"], a["lon"]))
        hits.append(vip)
    conn.commit()
    return hits


def check_going_dark(conn):
    """Flag VIP aircraft not seen for GOING_DARK_HOURS hours."""
    now      = datetime.now(timezone.utc)
    cutoff   = (now - timedelta(hours=GOING_DARK_HOURS)).isoformat()
    now_str  = now.isoformat()
    rows = conn.execute("""
        SELECT icao24, tail_number, operator, last_seen_at, last_region, last_lat, last_lon
        FROM vip_last_seen
        WHERE last_seen_at < ? AND dark_flagged = 0
    """, (cutoff,)).fetchall()

    for icao, tail, operator, last_seen, last_region, last_lat, last_lon in rows:
        try:
            last_dt    = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
            hours_dark = (now - last_dt).total_seconds() / 3600
        except Exception:
            hours_dark = 0
        conn.execute("""
            INSERT INTO vip_dark_events
                (detected_at, icao24, tail_number, operator,
                 last_seen_at, last_region, last_lat, last_lon, hours_dark)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (now_str, icao, tail, operator, last_seen, last_region, last_lat, last_lon, round(hours_dark, 1)))
        conn.execute("UPDATE vip_last_seen SET dark_flagged = 1 WHERE icao24 = ?", (icao,))
        print(f"  [VIP DARK] {tail or icao} ({operator}) — last seen {last_seen[:16]} in {last_region} ({hours_dark:.0f}h ago)")

    conn.commit()
    if not rows:
        pass  # silent when no going-dark events


# ── Mode B: strategic type clustering ──────────────────────────────────────────

def process_type_watch(conn, region_id, region_label, aircraft):
    """Count strategic aircraft types in a Type Watch region and save counts."""
    now     = datetime.now(timezone.utc)
    unix_ts = int(now.timestamp())
    now_str = now.isoformat()

    # Count by category; also collect spotted strategic aircraft for anomaly reporting
    cat_counts   = {cat: 0 for cat in STRATEGIC_TYPES}
    cat_aircraft = {cat: [] for cat in STRATEGIC_TYPES}

    for a in aircraft:
        icao = (a["icao24"] or "").lower()
        tc   = TYPECODE_DB.get(icao)
        if not tc:
            continue
        cat = TYPECODE_CATEGORY.get(tc)
        if cat:
            cat_counts[cat]   += 1
            cat_aircraft[cat].append([icao, tc])

    # Always record all categories (including zeros) so baseline reflects true average
    for cat, count in cat_counts.items():
        conn.execute("""
            INSERT INTO type_watch_counts (polled_at, polled_unix, region, category, count)
            VALUES (?, ?, ?, ?, ?)
        """, (now_str, unix_ts, region_id, cat, count))

    conn.commit()

    # Anomaly check: compare to 30-day baseline (mean + std), flag at 2σ+
    flagged = []
    for cat, count in cat_counts.items():
        if count == 0:
            continue
        stats = conn.execute("""
            SELECT AVG(count), COUNT(*),
                   AVG(count * count) - AVG(count) * AVG(count)  -- variance
            FROM type_watch_counts
            WHERE region = ? AND category = ?
              AND polled_unix < ? - 86400
              AND polled_unix > ? - 30*86400
        """, (region_id, cat, unix_ts, unix_ts)).fetchone()

        mean, n, variance = stats
        if not mean or n < 3:
            continue
        std = variance ** 0.5 if variance and variance > 0 else 0
        if std == 0:
            continue
        sigma = (count - mean) / std
        if sigma < 2.0:
            continue
        severity = "HIGH" if sigma >= 3.0 else "MEDIUM"
        conn.execute("""
            INSERT INTO type_anomalies
                (detected_at, region, region_label, category,
                 current_count, baseline_mean, baseline_std, sigma_above,
                 severity, aircraft_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            now_str, region_id, region_label, cat,
            count, round(mean, 2), round(std, 2), round(sigma, 2),
            severity, json.dumps(cat_aircraft[cat]),
        ))
        flagged.append((cat, count, sigma, severity))
        print(f"  [TYPE {severity}] {region_label} — {cat}: {count} aircraft ({sigma:.1f}σ above baseline)")

    conn.commit()
    return cat_counts


def check_bizjet_clusters(conn, all_region_aircraft):
    """
    Look for 3+ bizjets from different countries on the ground near the same
    major airport within the last BIZJET_CLUSTER_WINDOW_H hours.
    all_region_aircraft: list of aircraft dicts from all type-watch region polls this cycle.
    """
    now     = datetime.now(timezone.utc).isoformat()
    # Filter to on-ground bizjets with coordinates
    bizjets = []
    for a in all_region_aircraft:
        if not a["on_ground"]:
            continue
        icao = (a["icao24"] or "").lower()
        tc   = TYPECODE_DB.get(icao)
        if tc and TYPECODE_CATEGORY.get(tc) == "bizjet":
            ap = nearest_airport(a["lat"], a["lon"])
            if ap:
                bizjets.append({**a, "icao24": icao, "typecode": tc, "airport": ap})

    # Group by airport
    from collections import defaultdict
    by_airport = defaultdict(list)
    for bj in bizjets:
        by_airport[bj["airport"]].append(bj)

    for (ap_name, ap_icao), jets in by_airport.items():
        countries = {j["country"] for j in jets if j["country"]}
        if len(jets) >= BIZJET_CLUSTER_MIN and len(countries) >= 2:
            aircraft_data = [[j["icao24"], j["typecode"], j["country"], j["lat"], j["lon"]] for j in jets]
            conn.execute("""
                INSERT INTO bizjet_clusters
                    (detected_at, airport_name, airport_icao, bizjet_count, countries, aircraft_json)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (now, ap_name, ap_icao, len(jets), json.dumps(sorted(countries)), json.dumps(aircraft_data)))
            conn.commit()
            print(f"  [BIZJET CLUSTER] {ap_name} ({ap_icao}) — {len(jets)} bizjets from: {', '.join(sorted(countries))}")


# ── Poll ───────────────────────────────────────────────────────────────────────

def poll_all(conn, verbose=True):
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    if verbose:
        print(f"\nPoll — {now_str}")
        print("-" * 65)

    # ── Standard traffic regions (existing behaviour) ───────────────────────────
    for region_id, label, lamin, lomin, lamax, lomax in REGIONS:
        states = fetch_region(region_id, label, lamin, lomin, lamax, lomax)
        if states is None:
            continue

        aircraft, on_ground, airborne = save_snapshot(conn, region_id, label, states)

        if verbose:
            print(f"  {label:<30} total={len(states):>3}  airborne={airborne:>3}  ground={on_ground:>2}")
            spotted = set()
            for a in aircraft:
                cs = a["callsign"]
                if cs and len(cs) >= 3 and cs[:3] in WATCHED_AIRLINES:
                    spotted.add(WATCHED_AIRLINES[cs[:3]])
            if spotted:
                print(f"    Airlines: {', '.join(sorted(spotted))}")

        # Mode A: VIP sightings across all standard regions
        if VIP_WATCH:
            hits = process_vip_sightings(conn, region_id, label, aircraft)
            for vip in hits:
                print(f"  *** VIP SIGHTING: {vip['tail']} ({vip['operator']}) in {label} ***")

        time.sleep(2)

    # ── Mode B: type-watch regions ──────────────────────────────────────────────
    if TYPECODE_DB:
        if verbose:
            print(f"\n  [Mode B] Type-watch regions:")
        all_type_aircraft = []
        for region_id, label, lamin, lomin, lamax, lomax in TYPE_WATCH_REGIONS:
            states = fetch_region(region_id, label, lamin, lomin, lamax, lomax)
            if states is None:
                time.sleep(2)
                continue
            aircraft, on_ground, airborne = parse_states(states)
            all_type_aircraft.extend(aircraft)
            cat_counts = process_type_watch(conn, region_id, label, aircraft)
            if verbose:
                strategic_total = sum(cat_counts.values())
                print(f"  {label:<30} total={len(states):>3}  strategic={strategic_total:>2}  "
                      f"lift={cat_counts['strategic_lift']}  "
                      f"tanker={cat_counts['tanker']}  "
                      f"isr={cat_counts['isr_command']}  "
                      f"bizjet={cat_counts['bizjet']}")
            time.sleep(2)

        # Bizjet clustering across all type-watch aircraft this cycle
        check_bizjet_clusters(conn, all_type_aircraft)

    # Mode A: going-dark check (once per poll cycle, after all regions)
    if VIP_WATCH:
        check_going_dark(conn)

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
