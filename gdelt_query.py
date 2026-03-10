#!/usr/bin/env python3
"""
Quick GDELT query tool.
Edit START_DATE and END_DATE to explore any time range.
"""

import sqlite3

DB_PATH = "gdelt_events.db"
START_DATE = "20260201"
END_DATE   = "20260307"

conn = sqlite3.connect(DB_PATH)

print(f"\nDate range: {START_DATE} to {END_DATE}")
print("=" * 62)

# Daily summary
print("\nDay-by-day breakdown:")
print(f"{'Date':<12} {'Events':>7} {'Goldstein':>10} {'Conflict':>9} {'Coop':>6}")
print("-" * 48)

rows = conn.execute("""
    SELECT
        event_date,
        COUNT(*) as total,
        ROUND(AVG(goldstein_scale), 2) as avg_goldstein,
        SUM(CASE WHEN event_root_code IN ('18','19','20') THEN 1 ELSE 0 END) as conflict,
        SUM(CASE WHEN event_root_code IN ('03','04','05','08') THEN 1 ELSE 0 END) as coop
    FROM events
    WHERE event_date BETWEEN ? AND ?
    GROUP BY event_date
    ORDER BY event_date
""", (START_DATE, END_DATE)).fetchall()

for r in rows:
    print(f"{r[0]:<12} {r[1]:>7} {r[2]:>10} {r[3]:>9} {r[4]:>6}")

# Top events by media weight
print("\nTop 20 highest-coverage events in period:")
print("-" * 62)

top = conn.execute("""
    SELECT
        event_date,
        actor1_country,
        actor2_country,
        event_description,
        goldstein_scale,
        num_articles,
        source_url
    FROM events
    WHERE event_date BETWEEN ? AND ?
    ORDER BY num_articles DESC
    LIMIT 20
""", (START_DATE, END_DATE)).fetchall()

for r in top:
    print(f"{r[0]}  {r[1]:<4} <-> {r[2]:<4}  {r[3]:<28}  G:{r[4]:>5}  n:{r[5]:>4}")
    print(f"         {r[6][:80]}")
    print()

conn.close()
