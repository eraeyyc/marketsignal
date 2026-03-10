#!/usr/bin/env python3
"""
GDELT Stage 1: Middle East Event Collector

Queries GDELT v2 via BigQuery for conflict/cooperation events Jan 2023-present.
Stores results in a local SQLite database (gdelt_events.db).

Usage:
    python gdelt_collector.py          # full pull
    python gdelt_collector.py --test   # pull 500 rows to verify setup works
"""

import sqlite3
import argparse
from datetime import datetime
from google.cloud import bigquery
import pandas as pd

# ── Config ─────────────────────────────────────────────────────────────────────

CREDENTIALS_FILE = "gdelt_credentials.json"
DB_PATH = "gdelt_events.db"
START_DATE = 20230101

# CAMEO 3-letter actor country codes to watch
ACTOR_COUNTRIES = [
    "ISR", "PSE", "IRN", "LBN", "SYR", "YEM",
    "SAU", "JOR", "EGY", "QAT", "ARE", "TUR",
    "USA", "RUS", "CHN",
]

# FIPS codes for where events physically happened (Middle East geography)
ACTION_GEO_COUNTRIES = [
    "IS",  # Israel
    "GZ",  # Gaza
    "WE",  # West Bank
    "IR",  # Iran
    "LE",  # Lebanon
    "SY",  # Syria
    "YM",  # Yemen
    "SA",  # Saudi Arabia
    "JO",  # Jordan
    "EG",  # Egypt
    "QA",  # Qatar
    "AE",  # UAE
    "TU",  # Turkey
]

# Human-readable labels for CAMEO root codes (01-20)
CAMEO_ROOT_LABELS = {
    "01": "Public statement",
    "02": "Appeal",
    "03": "Intent to cooperate",
    "04": "Consult",
    "05": "Diplomatic cooperation",
    "06": "Material cooperation",
    "07": "Provide aid",
    "08": "Yield",
    "09": "Investigate",
    "10": "Demand",
    "11": "Disapprove",
    "12": "Reject",
    "13": "Threaten",
    "14": "Protest",
    "15": "Exhibit force posture",
    "16": "Reduce relations",
    "17": "Coerce",
    "18": "Assault",
    "19": "Fight",
    "20": "Unconventional mass violence",
}

# ── Database setup ─────────────────────────────────────────────────────────────

def init_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            event_date         TEXT,       -- YYYYMMDD
            actor1_name        TEXT,
            actor1_country     TEXT,       -- CAMEO 3-letter code
            actor1_type        TEXT,
            actor2_name        TEXT,
            actor2_country     TEXT,
            actor2_type        TEXT,
            event_code         TEXT,       -- full CAMEO event code
            event_base_code    TEXT,
            event_root_code    TEXT,       -- 2-digit root (01-20)
            event_description  TEXT,       -- human-readable root label
            goldstein_scale    REAL,       -- -10 (hostile) to +10 (cooperative)
            num_mentions       INTEGER,
            num_sources        INTEGER,
            num_articles       INTEGER,
            action_geo_country TEXT,
            action_geo_name    TEXT,
            action_geo_lat     REAL,
            action_geo_long    REAL,
            source_url         TEXT,
            label              TEXT,       -- for human annotation: escalation / de-escalation / neutral
            notes              TEXT,
            ingested_at        TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_event_date    ON events(event_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_actor1        ON events(actor1_country)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_actor2        ON events(actor2_country)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_root_code     ON events(event_root_code)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_goldstein     ON events(goldstein_scale)")
    conn.commit()
    return conn


# ── BigQuery query ─────────────────────────────────────────────────────────────

def build_query(test_mode=False):
    actor_list  = "', '".join(ACTOR_COUNTRIES)
    geo_list    = "', '".join(ACTION_GEO_COUNTRIES)

    # Core ME actors — at least one of these must be involved
    core_me_actors = "', '".join([
        "ISR", "PSE", "IRN", "LBN", "SYR", "YEM", "SAU", "JOR", "EGY", "QAT", "ARE", "TUR"
    ])

    # Exclude only: 01 (public statements), 02 (appeals), 07 (humanitarian aid)
    # These are either too noisy or not conflict/ceasefire signals.
    # Everything else is kept — notably 08 (Yield) which contains 0871: Declare ceasefire.
    signal_root_codes = "', '".join([
        "03", "04", "05", "06",
        "08", "09", "10", "11", "12",
        "13", "14", "15", "16", "17",
        "18", "19", "20",
    ])

    limit_clause = "LIMIT 500" if test_mode else ""

    return f"""
        SELECT
            CAST(SQLDATE AS STRING)   AS event_date,
            Actor1Name                AS actor1_name,
            Actor1CountryCode         AS actor1_country,
            Actor1Type1Code           AS actor1_type,
            Actor2Name                AS actor2_name,
            Actor2CountryCode         AS actor2_country,
            Actor2Type1Code           AS actor2_type,
            EventCode                 AS event_code,
            EventBaseCode             AS event_base_code,
            EventRootCode             AS event_root_code,
            GoldsteinScale            AS goldstein_scale,
            NumMentions               AS num_mentions,
            NumSources                AS num_sources,
            NumArticles               AS num_articles,
            ActionGeo_CountryCode     AS action_geo_country,
            ActionGeo_FullName        AS action_geo_name,
            ActionGeo_Lat             AS action_geo_lat,
            ActionGeo_Long            AS action_geo_long,
            SOURCEURL                 AS source_url
        FROM `gdelt-bq.gdeltv2.events`
        WHERE SQLDATE >= {START_DATE}
          AND NumArticles >= 10
          AND EventRootCode IN ('{signal_root_codes}')
          AND (
              Actor1CountryCode IN ('{core_me_actors}')
              OR Actor2CountryCode IN ('{core_me_actors}')
          )
          AND (
              ActionGeo_CountryCode IN ('{geo_list}')
              OR (
                  Actor1CountryCode IN ('{actor_list}')
                  AND Actor2CountryCode IN ('{actor_list}')
              )
          )
        ORDER BY SQLDATE DESC
        {limit_clause}
    """


def run_query(test_mode=False):
    print(f"Connecting to BigQuery using {CREDENTIALS_FILE}...")
    client = bigquery.Client.from_service_account_json(CREDENTIALS_FILE)

    query = build_query(test_mode)

    if test_mode:
        print("TEST MODE: pulling 500 rows to verify setup.")
    else:
        print(f"Pulling all Middle East events from {START_DATE} to present.")
        print("This may take 1-3 minutes...")

    df = client.query(query).to_dataframe()
    print(f"Retrieved {len(df):,} rows from BigQuery.")
    return df


# ── Storage ────────────────────────────────────────────────────────────────────

def save_to_db(df, conn):
    # Add human-readable event description from CAMEO root code
    df["event_description"] = df["event_root_code"].map(CAMEO_ROOT_LABELS).fillna("Unknown")

    # Reorder columns to match schema (drop id, label, notes, ingested_at — SQLite handles those)
    cols = [
        "event_date", "actor1_name", "actor1_country", "actor1_type",
        "actor2_name", "actor2_country", "actor2_type",
        "event_code", "event_base_code", "event_root_code", "event_description",
        "goldstein_scale", "num_mentions", "num_sources", "num_articles",
        "action_geo_country", "action_geo_name", "action_geo_lat", "action_geo_long",
        "source_url",
    ]
    df = df[cols]
    df.to_sql("events", conn, if_exists="append", index=False)
    conn.commit()
    print(f"Saved {len(df):,} rows to {DB_PATH}.")


# ── Summary ────────────────────────────────────────────────────────────────────

def print_summary(conn):
    print("\n── Dataset Summary ───────────────────────────────────────────")

    total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    print(f"Total events:      {total:,}")

    date_range = conn.execute(
        "SELECT MIN(event_date), MAX(event_date) FROM events"
    ).fetchone()
    print(f"Date range:        {date_range[0]} → {date_range[1]}")

    print("\nTop event types (by count):")
    rows = conn.execute("""
        SELECT event_root_code, event_description, COUNT(*) as n
        FROM events
        GROUP BY event_root_code, event_description
        ORDER BY n DESC
        LIMIT 10
    """).fetchall()
    for code, label, count in rows:
        print(f"  {code} {label:<25} {count:>8,}")

    print("\nEvent volume by month (most recent 12):")
    rows = conn.execute("""
        SELECT SUBSTR(event_date, 1, 6) as month, COUNT(*) as n
        FROM events
        GROUP BY month
        ORDER BY month DESC
        LIMIT 12
    """).fetchall()
    for month, count in rows:
        print(f"  {month}   {count:>8,}")

    print("──────────────────────────────────────────────────────────────\n")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--test", action="store_true",
        help="Pull only 500 rows to verify credentials and setup"
    )
    args = parser.parse_args()

    conn = init_db(DB_PATH)

    # Warn if database already has data
    existing = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    if existing > 0:
        print(f"Warning: database already contains {existing:,} rows.")
        answer = input("Append more rows? (y/n): ").strip().lower()
        if answer != "y":
            print("Aborted.")
            conn.close()
            return

    df = run_query(test_mode=args.test)
    save_to_db(df, conn)
    print_summary(conn)
    conn.close()


if __name__ == "__main__":
    main()
