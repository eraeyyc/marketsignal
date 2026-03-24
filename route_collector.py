#!/usr/bin/env python3
"""
MarketSignal — Route Suspension Collector (Cirium Flex API)

Monitors scheduled vs. actual operated flights for 13 watched airlines across
Middle East airport pairs. Flags route_suspension signals when a carrier's
operated flights drop >60% below schedule for 3+ consecutive days.

Feeds into convergence engine as route_suspension signals (λ=0.12/day, Event type).

Usage:
    python3 route_collector.py              # poll all routes, check suspensions
    python3 route_collector.py --loop       # continuous polling (once per day)
    python3 route_collector.py --status     # print active suspensions
    python3 route_collector.py --refresh    # force refresh schedule cache

Historical back-test note:
    flightstatus/historical/rest/v2/json/route/{dep}/{arr} gives actual ME route
    data going back years. Flag for GDELT calibration pass — validates route
    suspension lead times against GDELT escalation events.
"""

import sqlite3
import requests
import json
import os
import time
import argparse
from datetime import datetime, timezone, timedelta
from itertools import permutations
from dotenv import load_dotenv

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────

DB_PATH            = "route_events.db"
API_BASE           = "https://api.flightstats.com/flex"
APP_ID             = os.environ.get("CIRIUM_FLEX_APP_ID", "")
APP_KEY            = os.environ.get("CIRIUM_FLEX_APP_KEY", "")
POLL_INTERVAL      = 86400   # once per day
SCHEDULE_TTL_DAYS  = 7       # refresh schedule cache weekly
BASELINE_DAYS      = 14      # days of history used to compute schedule baseline
DROP_THRESHOLD     = 0.60    # >60% drop triggers flag
MIN_CONSEC_DAYS    = 3       # must persist 3+ consecutive days
LOOKBACK_DAYS      = 7       # rolling window for suspension check
REQUEST_PAUSE      = 1.0     # seconds between API calls (rate limiting)

# Middle East airports to monitor — all route pairs both directions
ME_AIRPORTS = [
    "TLV",  # Tel Aviv
    "AMM",  # Amman
    "BGW",  # Baghdad
    "KWI",  # Kuwait
    "BAH",  # Bahrain
    "DOH",  # Doha
    "DXB",  # Dubai
    "AUH",  # Abu Dhabi
    "MCT",  # Muscat
    "IKA",  # Tehran Imam Khomeini
    "THR",  # Tehran Mehrabad
    "BEY",  # Beirut
    "CAI",  # Cairo
    "IST",  # Istanbul
]

# Watched airlines — IATA codes (used in Cirium Flex API)
WATCHED_AIRLINES = {
    "LY": "El Al",
    "BA": "British Airways",
    "AF": "Air France",
    "LH": "Lufthansa",
    "EK": "Emirates",
    "QR": "Qatar Airways",
    "TK": "Turkish Airlines",
    "FR": "Ryanair",
    "DL": "Delta",
    "UA": "United",
    "EY": "Etihad",
    "SV": "Saudia",
    "ME": "Middle East Airlines",
}

# All directed route pairs (both directions)
ROUTE_PAIRS = list(permutations(ME_AIRPORTS, 2))


def _auth_params():
    return {"appId": APP_ID, "appKey": APP_KEY}


# ── Database ───────────────────────────────────────────────────────────────────

def _add_column(conn, table, column, col_type):
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def init_db(db_path):
    conn = sqlite3.connect(db_path)

    # Schedule baseline cache — refreshed weekly
    conn.execute("""
        CREATE TABLE IF NOT EXISTS route_schedules (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            dep              TEXT NOT NULL,
            arr              TEXT NOT NULL,
            airline          TEXT NOT NULL,   -- IATA code
            airline_name     TEXT,
            flights_per_day  REAL NOT NULL,   -- average from current schedule
            cached_at        TEXT NOT NULL,
            UNIQUE(dep, arr, airline)
        )
    """)

    # Daily actual flight counts
    conn.execute("""
        CREATE TABLE IF NOT EXISTS route_daily (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            polled_at        TEXT NOT NULL,
            dep              TEXT NOT NULL,
            arr              TEXT NOT NULL,
            airline          TEXT NOT NULL,
            flight_date      TEXT NOT NULL,   -- YYYY-MM-DD
            scheduled_count  REAL,            -- from schedule cache (flights_per_day)
            operated_count   INTEGER NOT NULL,
            cancelled_count  INTEGER NOT NULL,
            UNIQUE(dep, arr, airline, flight_date)
        )
    """)

    # Route suspension signals
    conn.execute("""
        CREATE TABLE IF NOT EXISTS route_suspensions (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            dep               TEXT NOT NULL,
            arr               TEXT NOT NULL,
            airline           TEXT NOT NULL,
            airline_name      TEXT,
            first_detected_at TEXT NOT NULL,
            last_confirmed_at TEXT,           -- updated each poll while suspended
            resolved_at       TEXT,           -- set when route resumes
            consecutive_days  INTEGER,
            drop_pct          REAL,
            severity          TEXT            -- MEDIUM (60-80%) / HIGH (>80%)
        )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_rd_route   ON route_daily(dep, arr, airline, flight_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rs_route   ON route_suspensions(dep, arr, airline)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rs_active  ON route_suspensions(resolved_at)")
    conn.commit()
    return conn


# ── API calls ──────────────────────────────────────────────────────────────────

def fetch_schedule(dep, arr):
    """
    Build a baseline schedule by averaging BASELINE_DAYS of historical flight status data.
    Returns dict: {airline_iata: flights_per_day}.

    The Cirium Flex /schedules/ endpoint is not available on this plan.
    Instead we query flightstatus for the past BASELINE_DAYS days and average
    the operated flight counts per carrier as the baseline.
    """
    today = datetime.now(timezone.utc).date()
    carrier_daily = {}  # {carrier: [count_day1, count_day2, ...]}
    days_fetched = 0

    for offset in range(1, BASELINE_DAYS + 1):
        d = today - timedelta(days=offset)
        url = (
            f"{API_BASE}/flightstatus/rest/v2/json/route/status"
            f"/{dep}/{arr}/dep/{d.year}/{d.month}/{d.day}"
        )
        try:
            r = requests.get(url, params=_auth_params(), timeout=15)
            if r.status_code == 404:
                time.sleep(REQUEST_PAUSE)
                continue
            if r.status_code == 403:
                print("  [API] 403 Forbidden — check APP_ID / APP_KEY")
                return {}
            r.raise_for_status()
            statuses = r.json().get("flightStatuses", [])
            days_fetched += 1

            # Count non-cancelled watched-carrier flights for this day
            day_counts = {}
            for f in statuses:
                carrier = f.get("carrierFsCode", "")
                if carrier not in WATCHED_AIRLINES:
                    continue
                if f.get("status", "") != "C":
                    day_counts[carrier] = day_counts.get(carrier, 0) + 1

            for carrier, count in day_counts.items():
                carrier_daily.setdefault(carrier, []).append(count)

        except requests.RequestException as e:
            print(f"    [{dep}-{arr}] Baseline fetch error (day -{offset}): {e}")

        time.sleep(REQUEST_PAUSE)

    if days_fetched == 0:
        return {}

    # Average across fetched days; carriers with no flights on some days get 0 for those days
    result = {}
    for carrier, counts in carrier_daily.items():
        avg = sum(counts) / days_fetched
        if avg > 0:
            result[carrier] = round(avg, 2)

    return result


def fetch_route_status(dep, arr, date):
    """
    GET actual flight statuses for a specific date (arrivals).
    Returns dict: {airline_iata: (operated_count, cancelled_count)}.
    Uses flightstatus/rest/v2/json/route/status/{dep}/{arr}/arr/{Y}/{M}/{D}.
    """
    url = (
        f"{API_BASE}/flightstatus/rest/v2/json/route/status"
        f"/{dep}/{arr}/arr/{date.year}/{date.month}/{date.day}"
    )
    try:
        r = requests.get(url, params=_auth_params(), timeout=15)
        if r.status_code == 404:
            return {}
        r.raise_for_status()
        data = r.json()
        statuses = data.get("flightStatuses", [])

        result = {}
        for f in statuses:
            carrier = f.get("carrierFsCode", "")
            if carrier not in WATCHED_AIRLINES:
                continue
            status = f.get("status", "")
            cancelled = status == "C"
            if carrier not in result:
                result[carrier] = [0, 0]  # [operated, cancelled]
            if cancelled:
                result[carrier][1] += 1
            else:
                result[carrier][0] += 1

        return {k: tuple(v) for k, v in result.items()}

    except requests.RequestException as e:
        print(f"    [{dep}-{arr}] Status fetch error: {e}")
        return {}


# ── Schedule cache ─────────────────────────────────────────────────────────────

def refresh_schedules(conn, force=False):
    """Refresh schedule cache for all route pairs. Skips if cache is fresh."""
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=SCHEDULE_TTL_DAYS)).isoformat()

    if not force:
        oldest = conn.execute(
            "SELECT MIN(cached_at) FROM route_schedules"
        ).fetchone()[0]
        if oldest and oldest > cutoff:
            return  # cache is fresh

    print(f"  Refreshing schedule cache ({len(ROUTE_PAIRS)} route pairs)...")
    refreshed = 0
    for dep, arr in ROUTE_PAIRS:
        sched = fetch_schedule(dep, arr)
        now_str = now.isoformat()
        for airline, fpd in sched.items():
            conn.execute("""
                INSERT INTO route_schedules (dep, arr, airline, airline_name, flights_per_day, cached_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(dep, arr, airline) DO UPDATE SET
                    flights_per_day = excluded.flights_per_day,
                    cached_at       = excluded.cached_at
            """, (dep, arr, airline, WATCHED_AIRLINES[airline], fpd, now_str))
        if sched:
            refreshed += 1
        time.sleep(REQUEST_PAUSE)

    conn.commit()
    print(f"  Schedule cache updated — {refreshed} active routes found")


# ── Daily polling ──────────────────────────────────────────────────────────────

def poll_yesterday(conn):
    """Fetch yesterday's actual flight counts for all routes with scheduled service."""
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    now_str   = datetime.now(timezone.utc).isoformat()

    # Get routes with scheduled service
    routes = conn.execute("""
        SELECT DISTINCT dep, arr, airline, airline_name, flights_per_day
        FROM route_schedules
        WHERE flights_per_day > 0
    """).fetchall()

    if not routes:
        print("  No scheduled routes in cache. Run --refresh first.")
        return 0

    print(f"  Polling {len(routes)} route/airline pairs for {yesterday}...")
    new_rows = 0
    for dep, arr, airline, airline_name, fpd in routes:
        statuses = fetch_route_status(dep, arr, yesterday)
        operated, cancelled = statuses.get(airline, (0, 0))

        try:
            conn.execute("""
                INSERT OR IGNORE INTO route_daily
                    (polled_at, dep, arr, airline, flight_date,
                     scheduled_count, operated_count, cancelled_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (now_str, dep, arr, airline,
                  yesterday.isoformat(), fpd, operated, cancelled))
            new_rows += 1
        except Exception:
            pass

        time.sleep(REQUEST_PAUSE)

    conn.commit()
    print(f"  Stored {new_rows} daily records")
    return new_rows


# ── Suspension detection ───────────────────────────────────────────────────────

def check_suspensions(conn):
    """
    For each route/airline pair, check rolling LOOKBACK_DAYS window.
    Flag when operated/scheduled < (1 - DROP_THRESHOLD) for MIN_CONSEC_DAYS+ days.
    """
    now_str   = datetime.now(timezone.utc).isoformat()
    cutoff    = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).date().isoformat()

    routes = conn.execute("""
        SELECT DISTINCT dep, arr, airline FROM route_daily
        WHERE flight_date >= ?
    """, (cutoff,)).fetchall()

    new_flags = 0
    resolved  = 0

    for dep, arr, airline in routes:
        rows = conn.execute("""
            SELECT flight_date, scheduled_count, operated_count
            FROM route_daily
            WHERE dep = ? AND arr = ? AND airline = ?
              AND flight_date >= ?
            ORDER BY flight_date DESC
        """, (dep, arr, airline, cutoff)).fetchall()

        if not rows:
            continue

        # Count consecutive days of significant drop from the most recent date backwards
        consecutive = 0
        total_drop  = 0.0
        for flight_date, sched, operated in rows:
            if not sched or sched == 0:
                break  # no scheduled service, stop counting
            ratio = operated / sched
            drop  = 1.0 - ratio
            if drop >= DROP_THRESHOLD:
                consecutive += 1
                total_drop += drop
            else:
                break  # streak broken

        avg_drop = total_drop / consecutive if consecutive > 0 else 0.0

        airline_name = WATCHED_AIRLINES.get(airline, airline)

        # Check if there's already an active (unresolved) suspension for this route
        existing = conn.execute("""
            SELECT id FROM route_suspensions
            WHERE dep = ? AND arr = ? AND airline = ? AND resolved_at IS NULL
        """, (dep, arr, airline)).fetchone()

        if consecutive >= MIN_CONSEC_DAYS:
            severity = "HIGH" if avg_drop >= 0.80 else "MEDIUM"
            if existing:
                # Update last_confirmed_at on existing suspension
                conn.execute("""
                    UPDATE route_suspensions
                    SET last_confirmed_at = ?, consecutive_days = ?, drop_pct = ?
                    WHERE id = ?
                """, (now_str, consecutive, round(avg_drop, 3), existing[0]))
            else:
                # New suspension
                conn.execute("""
                    INSERT INTO route_suspensions
                        (dep, arr, airline, airline_name, first_detected_at,
                         last_confirmed_at, consecutive_days, drop_pct, severity)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (dep, arr, airline, airline_name, now_str, now_str,
                      consecutive, round(avg_drop, 3), severity))
                new_flags += 1
                print(f"  *** ROUTE SUSPENSION [{severity}] "
                      f"{airline_name} {dep}-{arr} — "
                      f"{consecutive} days, {avg_drop*100:.0f}% drop ***")

        elif existing:
            # Streak broken — resolve the suspension
            conn.execute("""
                UPDATE route_suspensions SET resolved_at = ?
                WHERE id = ?
            """, (now_str, existing[0]))
            resolved += 1
            print(f"  [RESOLVED] {airline_name} {dep}-{arr} route resumed")

    conn.commit()
    return new_flags, resolved


# ── Status ─────────────────────────────────────────────────────────────────────

def print_status(conn):
    total = conn.execute("SELECT COUNT(*) FROM route_suspensions").fetchone()[0]
    active = conn.execute(
        "SELECT COUNT(*) FROM route_suspensions WHERE resolved_at IS NULL"
    ).fetchone()[0]

    print(f"\nRoute Suspension Summary")
    print("=" * 65)
    print(f"Total logged:   {total}")
    print(f"Currently active: {active}")

    rows = conn.execute("""
        SELECT airline_name, dep, arr, first_detected_at,
               last_confirmed_at, consecutive_days, drop_pct, severity
        FROM route_suspensions
        WHERE resolved_at IS NULL
        ORDER BY drop_pct DESC
    """).fetchall()

    if rows:
        print(f"\nActive suspensions:")
        print(f"  {'Airline':<25} {'Route':<8} {'Days':>5} {'Drop':>6}  Since")
        print("  " + "-" * 60)
        for name, dep, arr, first, lca, days, drop, sev in rows:
            print(f"  [{sev}] {name:<22} {dep}-{arr}  {days:>4}d  {drop*100:>5.0f}%  {first[:10]}")
    else:
        print("\nNo active route suspensions.")

    scheduled = conn.execute("SELECT COUNT(*) FROM route_schedules").fetchone()[0]
    daily     = conn.execute("SELECT COUNT(*) FROM route_daily").fetchone()[0]
    print(f"\nSchedule cache:   {scheduled} route/airline pairs")
    print(f"Daily records:    {daily}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop",    action="store_true", help="Poll continuously (once per day)")
    parser.add_argument("--status",  action="store_true", help="Print active suspensions")
    parser.add_argument("--refresh", action="store_true", help="Force refresh schedule cache")
    args = parser.parse_args()

    if not APP_ID or not APP_KEY:
        print("ERROR: CIRIUM_FLEX_APP_ID and CIRIUM_FLEX_APP_KEY not set in .env")
        return

    conn = init_db(DB_PATH)

    if args.status:
        print_status(conn)
        conn.close()
        return

    if args.refresh:
        refresh_schedules(conn, force=True)
        conn.close()
        return

    def run_once():
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
        print(f"\nRoute Collector — {now_str}")
        print("-" * 65)
        refresh_schedules(conn)
        poll_yesterday(conn)
        new_flags, resolved = check_suspensions(conn)
        print(f"\n  {new_flags} new suspension(s) flagged | {resolved} resolved")

    if args.loop:
        print(f"Route collector running (daily). Ctrl+C to stop.")
        while True:
            try:
                run_once()
                print(f"  Sleeping 24h...")
                time.sleep(POLL_INTERVAL)
            except KeyboardInterrupt:
                print("\nStopped.")
                break
    else:
        run_once()

    conn.close()


if __name__ == "__main__":
    main()
