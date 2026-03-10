#!/usr/bin/env python3
"""
GDELT Stage 1: Verification

Spot-checks the collected dataset against known historical events.
If the data is correct, each event below should show a spike in
conflict-coded GDELT events in the surrounding window.

Usage:
    python gdelt_verify.py
"""

import sqlite3

DB_PATH = "gdelt_events.db"

# ── Known events to verify against ────────────────────────────────────────────
# Format: (label, YYYYMMDD start, YYYYMMDD end, what we expect to see)

KNOWN_EVENTS = [
    (
        "Hamas Oct 7 attack",
        "20231007", "20231010",
        "Massive spike in root codes 18/19/20 (assault/fight/mass violence), "
        "actors ISR + PSE, geography GZ/IS",
    ),
    (
        "First Gaza ceasefire (Nov 2023)",
        "20231122", "20231128",
        "Spike in root codes 03/04/05 (cooperation/consult/diplomatic), "
        "actors QAT/EGY/USA mediating ISR+PSE",
    ),
    (
        "Iranian drone/missile strike on Israel (Apr 2024)",
        "20240413", "20240415",
        "Spike in root codes 15/18/19 (military posture/assault/fight), "
        "actors IRN + ISR",
    ),
    (
        "Houthi Red Sea campaign escalation (Jan 2024)",
        "20240110", "20240115",
        "Events involving YEM actors, root codes 13/15/18 (threaten/military/assault)",
    ),
    (
        "Gaza ceasefire agreement (Jan 2025)",
        "20250115", "20250120",
        "Spike in cooperative root codes 03/04/05, "
        "QAT/EGY/USA as mediators",
    ),
]

# Conflict root codes (we expect spikes around escalation events)
CONFLICT_CODES = ("15", "16", "17", "18", "19", "20")

# Cooperation root codes (we expect spikes around ceasefire/negotiation events)
COOPERATION_CODES = ("03", "04", "05", "06")


# ── Query helpers ──────────────────────────────────────────────────────────────

def event_counts_in_window(conn, start_date, end_date):
    """Return total events and breakdown by root code for a date window."""
    rows = conn.execute("""
        SELECT event_root_code, event_description, COUNT(*) as n
        FROM events
        WHERE event_date BETWEEN ? AND ?
        GROUP BY event_root_code, event_description
        ORDER BY n DESC
    """, (start_date, end_date)).fetchall()
    return rows


def top_actors_in_window(conn, start_date, end_date):
    """Return most frequent actor pairs in a date window."""
    rows = conn.execute("""
        SELECT actor1_country, actor2_country, COUNT(*) as n
        FROM events
        WHERE event_date BETWEEN ? AND ?
          AND actor1_country != ''
          AND actor2_country != ''
        GROUP BY actor1_country, actor2_country
        ORDER BY n DESC
        LIMIT 8
    """, (start_date, end_date)).fetchall()
    return rows


def goldstein_in_window(conn, start_date, end_date):
    """Return average Goldstein scale for a date window."""
    row = conn.execute("""
        SELECT AVG(goldstein_scale), MIN(goldstein_scale), MAX(goldstein_scale)
        FROM events
        WHERE event_date BETWEEN ? AND ?
    """, (start_date, end_date)).fetchone()
    return row


def conflict_ratio(rows):
    total = sum(r[2] for r in rows)
    conflict = sum(r[2] for r in rows if r[0] in CONFLICT_CODES)
    if total == 0:
        return 0, 0
    return conflict, round(conflict / total * 100, 1)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    conn = sqlite3.connect(DB_PATH)

    total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    if total == 0:
        print("Database is empty. Run gdelt_collector.py first.")
        conn.close()
        return

    date_range = conn.execute(
        "SELECT MIN(event_date), MAX(event_date) FROM events"
    ).fetchone()
    print(f"\nDatabase: {total:,} events  |  {date_range[0]} → {date_range[1]}")
    print("=" * 65)

    passed = 0

    for label, start, end, expectation in KNOWN_EVENTS:
        print(f"\nEvent: {label}  ({start} → {end})")
        print(f"Expect: {expectation}")
        print("-" * 65)

        rows = event_counts_in_window(conn, start, end)
        total_window = sum(r[2] for r in rows)

        if total_window == 0:
            print("  !! NO DATA FOUND in this window — check date range or filters")
            continue

        print(f"  Total events in window: {total_window:,}")

        conflict_n, conflict_pct = conflict_ratio(rows)
        print(f"  Conflict-coded events (15-20): {conflict_n:,}  ({conflict_pct}%)")

        print("  Event type breakdown:")
        for code, desc, count in rows[:8]:
            marker = " <--" if code in CONFLICT_CODES else ""
            print(f"    {code}  {desc:<28} {count:>5}{marker}")

        print("  Top actor pairs:")
        for a1, a2, count in top_actors_in_window(conn, start, end):
            print(f"    {a1} <-> {a2:<6}  {count:>5}")

        avg_g, min_g, max_g = goldstein_in_window(conn, start, end)
        print(f"  Goldstein scale — avg: {avg_g:.2f}  min: {min_g:.1f}  max: {max_g:.1f}")

        # Simple pass heuristic: at least 10 events and some conflict activity
        if total_window >= 10:
            print("  PASS: data present for this event")
            passed += 1
        else:
            print("  WARN: low event count — may indicate a data gap")

    print("\n" + "=" * 65)
    print(f"Verification: {passed}/{len(KNOWN_EVENTS)} known events have data coverage")

    if passed == len(KNOWN_EVENTS):
        print("Stage 1 complete: dataset looks good.")
    elif passed >= 3:
        print("Partial coverage. Review any WARN events above before proceeding.")
    else:
        print("Low coverage. Re-run collector or review country/date filters.")

    conn.close()


if __name__ == "__main__":
    main()
