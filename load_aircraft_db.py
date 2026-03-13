#!/usr/bin/env python3
"""
One-time migration: load aircraft-database-complete.csv into SQLite.

Creates the aircraft_lookup table in adsb_events.db with schema:
    icao24 TEXT PRIMARY KEY
    typecode TEXT
    registration TEXT
    operator TEXT

Run once before starting adsb_collector.py on a new machine.

Usage:
    python3 load_aircraft_db.py
    python3 load_aircraft_db.py --csv path/to/aircraft-database-complete.csv
    python3 load_aircraft_db.py --db path/to/adsb_events.db
"""

import sqlite3
import csv
import sys
import argparse
import time

CSV_PATH = "aircraft-database-complete.csv"
DB_PATH  = "adsb_events.db"
BATCH    = 10_000  # rows per INSERT batch


def create_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS aircraft_lookup (
            icao24        TEXT PRIMARY KEY,
            typecode      TEXT,
            registration  TEXT,
            operator      TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_al_icao ON aircraft_lookup(icao24)")
    conn.commit()


def load(csv_path, db_path):
    print(f"Source: {csv_path}")
    print(f"Target: {db_path} → aircraft_lookup")

    conn = sqlite3.connect(db_path)
    create_table(conn)

    # Check if already populated
    existing = conn.execute("SELECT COUNT(*) FROM aircraft_lookup").fetchone()[0]
    if existing:
        print(f"Table already has {existing:,} rows. Use --force to reload.")
        conn.close()
        return

    csv.field_size_limit(min(sys.maxsize, 10_000_000))
    start = time.time()
    inserted = 0
    batch = []

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        # Field names may be single-quoted (e.g. "'icao24'") — normalise
        raw_fields = reader.fieldnames or []
        norm = {k.strip("'"): k for k in raw_fields}
        icao_key = norm.get("icao24",       "icao24")
        tc_key   = norm.get("typecode",     "typecode")
        reg_key  = norm.get("registration", "registration")
        op_key   = norm.get("operator",     "operator")

        for row in reader:
            icao24 = (row.get(icao_key) or "").strip().strip("'").lower()
            if not icao24:
                continue
            typecode     = (row.get(tc_key)  or "").strip().strip("'").upper() or None
            registration = (row.get(reg_key) or "").strip().strip("'")         or None
            operator     = (row.get(op_key)  or "").strip().strip("'")         or None

            batch.append((icao24, typecode, registration, operator))

            if len(batch) >= BATCH:
                conn.executemany(
                    "INSERT OR REPLACE INTO aircraft_lookup VALUES (?, ?, ?, ?)", batch
                )
                conn.commit()
                inserted += len(batch)
                batch = []
                print(f"  {inserted:,} rows inserted...", end="\r")

    if batch:
        conn.executemany(
            "INSERT OR REPLACE INTO aircraft_lookup VALUES (?, ?, ?, ?)", batch
        )
        conn.commit()
        inserted += len(batch)

    elapsed = time.time() - start
    print(f"  Done. {inserted:,} rows in {elapsed:.1f}s")
    conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv",   default=CSV_PATH, help="Path to aircraft-database-complete.csv")
    parser.add_argument("--db",    default=DB_PATH,  help="Path to adsb_events.db")
    parser.add_argument("--force", action="store_true", help="Drop and reload even if table is populated")
    args = parser.parse_args()

    if args.force:
        conn = sqlite3.connect(args.db)
        conn.execute("DROP TABLE IF EXISTS aircraft_lookup")
        conn.commit()
        conn.close()
        print("Dropped existing aircraft_lookup table.")

    load(args.csv, args.db)

    # Verify
    conn = sqlite3.connect(args.db)
    total = conn.execute("SELECT COUNT(*) FROM aircraft_lookup").fetchone()[0]
    with_type = conn.execute("SELECT COUNT(*) FROM aircraft_lookup WHERE typecode IS NOT NULL").fetchone()[0]
    print(f"\nVerification:")
    print(f"  Total rows:        {total:,}")
    print(f"  Rows with typecode:{with_type:,}")
    sample = conn.execute(
        "SELECT icao24, typecode, registration, operator FROM aircraft_lookup WHERE typecode IS NOT NULL LIMIT 5"
    ).fetchall()
    print(f"  Sample rows:")
    for row in sample:
        print(f"    {row}")
    conn.close()


if __name__ == "__main__":
    main()
