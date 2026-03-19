#!/usr/bin/env python3
"""
MarketSignal — Polymarket Collector (Stage 4)

Polls the Polymarket Gamma API for active Middle East markets, classifies each
as escalation or de-escalation track, and stores prices to polymarket_markets.db.

No API key required — the Gamma API is fully public for read-only access.

Usage:
    python3 polymarket_collector.py           # poll once
    python3 polymarket_collector.py --loop    # continuous, every 15 minutes
    python3 polymarket_collector.py --status  # print all tracked markets
"""

import sqlite3
import requests
import re
import time
import argparse
import os
from datetime import datetime, timezone, timedelta

# ── Config ─────────────────────────────────────────────────────────────────────

DB_PATH       = "polymarket_markets.db"
API_BASE      = "https://gamma-api.polymarket.com"
POLL_INTERVAL = 900    # 15 minutes
PAGE_SIZE     = 100    # markets per API page
REQUEST_TIMEOUT = 10   # seconds

# ── ME keyword filter ──────────────────────────────────────────────────────────
# A market passes the ME filter if it matches an ME geography keyword AND
# a geopolitical keyword. The geopolitical gate removes non-conflict markets
# (sports, weather, economics) that happen to mention a ME city or country.

# Geography keywords — at least one must match (place names and factions ONLY).
# Do NOT put geopolitical terms here (ceasefire, nuclear deal, idf) — they're not
# geography and would cause non-ME markets (Russia/Ukraine ceasefire, etc.) to pass.
ME_KEYWORDS_PHRASE = [
    "israel", "gaza", "lebanon", "syria",
    "houthi", "yemen", "red sea", "west bank", "hamas", "hezbollah",
    "palestinian", "irgc", "rafah",
    "middle east", "tel aviv", "beirut", "tehran",
]

# Word-boundary keywords — must appear as whole words to avoid false positives
# e.g. "iran" should NOT match "Miran", "Mirandés", "Miranda", "Arirang"
ME_KEYWORDS_WORD = [
    "iran",
]

_ME_WORD_RE = re.compile(
    r"\b(" + "|".join(re.escape(kw) for kw in ME_KEYWORDS_WORD) + r")",
    re.IGNORECASE,
)

# Geopolitical gate — at least one must also match for the market to be tracked.
# This filters out sports, weather, economics and other non-conflict markets
# that happen to mention a ME geography word.
GEOPOLITICAL_KEYWORDS = [
    "war", "attack", "strike", "bomb", "missile", "drone", "invasion",
    "conflict", "crisis", "military", "troops", "soldier", "army", "forces",
    "airstrike", "ceasefire", "peace", "truce", "hostage", "prisoner",
    "nuclear", "sanctions", "embargo", "blockade", "occupation",
    "assassination", "killed", "casualties", "dead", "death toll",
    "deal", "agreement", "negotiat", "diplomacy", "diplomatic",
    "idf", "irgc", "hamas", "hezbollah", "houthi",
    "escalat", "deescalat", "hostilities", "offensive", "withdraw",
    "refugee", "siege", "ground operation", "ground invasion",
    "rocket", "explosion", "shelling", "artillery",
    "shoot down", "intercept", "retaliat",
    "jcpoa", "enrichment", "warhead", "ballistic",
    "two-state", "annexat", "settlement", "normaliz", "normalise",
    "captured", "released", "freed", "exchange",
    "naval", "blockade", "strait", "hormuz",
    "regime", "leadership change", "disarm", "ceasefire",
]

# ── De-escalation track classification ────────────────────────────────────────
# A market is de-escalation if its question matches any keyword OR phrase pattern.
# Everything else that passes the ME + geopolitical filter is escalation.

DEESC_KEYWORDS = [
    "ceasefire", "peace deal", "peace agreement", "truce",
    "hostage deal", "hostages released", "hostages freed",
    "prisoner exchange", "prisoner swap",
    "withdrawal", "withdraws", "pulls out",
    "diplomatic", "normalization", "normalisation", "normaliz", "normalise",
    "two-state", "de-escalat", "deescalat",
    "nuclear deal",     # US-Iran nuclear deal = diplomatic agreement
    "disarm",           # Hezbollah disarm, weapons handover
    "end of military",  # end of military operations
    "end hostilities",  # end hostilities
    "halt operations",
]

# Phrase patterns for de-escalation — catches "conflict ends", "war ends", etc.
# These require two parts: a conflict noun + an ending verb
_CONFLICT_NOUNS = r"(conflict|war|fighting|hostilities|crisis|violence|offensive)"
_ENDING_VERBS   = r"(ends?|over|halt|cease|stop|resolv|settl)"
_DEESC_PATTERNS = [
    re.compile(rf"{_CONFLICT_NOUNS}\s+{_ENDING_VERBS}", re.IGNORECASE),
    re.compile(rf"{_ENDING_VERBS}\s+.{{0,20}}{_CONFLICT_NOUNS}", re.IGNORECASE),
    re.compile(r"\bend\s+by\b",             re.IGNORECASE),   # "end by [date]"
    re.compile(r"\bends\s+by\b",            re.IGNORECASE),   # "ends by [date]"
    re.compile(r"\bover\s+by\b",            re.IGNORECASE),   # "over by [date]"
    re.compile(r"\bresolved?\s+by\b",       re.IGNORECASE),   # "resolved by [date]"
    re.compile(r"\bpeace\b",                re.IGNORECASE),   # standalone "peace"
    re.compile(r"\bnegotiat",               re.IGNORECASE),   # negotiations, negotiated
    re.compile(r"\breleas",                 re.IGNORECASE),   # released, release of hostages
    re.compile(r"\bfreed?\b",               re.IGNORECASE),   # freed, free
    re.compile(r"\bend\s+of\s+(military|combat|offensive|air)", re.IGNORECASE),  # end of military/air operations
    re.compile(r"announces?\s+end\s+of",    re.IGNORECASE),   # announces end of [ops]
    re.compile(r"\bnuclear\s+(deal|agree)", re.IGNORECASE),   # nuclear deal / agreement
    re.compile(r"\bdisarm",                 re.IGNORECASE),   # disarm, disarmament
    re.compile(r"\bnormaliz",               re.IGNORECASE),   # normalize, normalization
    re.compile(r"\bagrees?\s+to\s+(end|stop|halt|limit|cap)", re.IGNORECASE),  # agrees to end/stop/halt
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def is_me_market(question: str) -> bool:
    """Must match an ME geography keyword AND at least one geopolitical keyword."""
    q = question.lower()
    geo_match = any(kw in q for kw in ME_KEYWORDS_PHRASE) or bool(_ME_WORD_RE.search(question))
    if not geo_match:
        return False
    return any(kw in q for kw in GEOPOLITICAL_KEYWORDS)


def classify_track(question: str) -> str:
    """Return 'deescalation' or 'escalation' based on question content."""
    q = question.lower()
    if any(kw in q for kw in DEESC_KEYWORDS):
        return "deescalation"
    if any(p.search(question) for p in _DEESC_PATTERNS):
        return "deescalation"
    return "escalation"


def parse_prices(outcome_prices):
    """
    Parse outcomePrices field. Returns (yes_price, no_price) as floats or (None, None).
    Skips markets with != 2 outcomes or missing/null prices.

    The Gamma API returns outcomePrices as a JSON-encoded string, not a list:
      '["0.525", "0.475"]'  not  ["0.525", "0.475"]
    """
    if not outcome_prices:
        return None, None
    # Deserialise the JSON string if needed
    if isinstance(outcome_prices, str):
        try:
            import json as _json
            outcome_prices = _json.loads(outcome_prices)
        except (ValueError, TypeError):
            return None, None
    if len(outcome_prices) != 2:
        return None, None
    try:
        yes = float(outcome_prices[0])
        no  = float(outcome_prices[1])
        return yes, no
    except (TypeError, ValueError):
        return None, None


def minutes_since(iso_str: str) -> float:
    """Minutes elapsed since an ISO timestamp."""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60
    except Exception:
        return 0.0


# ── Database ───────────────────────────────────────────────────────────────────

def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS markets (
            condition_id  TEXT PRIMARY KEY,
            question      TEXT NOT NULL,
            slug          TEXT,
            yes_price     REAL,
            no_price      REAL,
            volume        REAL,
            liquidity     REAL,
            end_date      TEXT,
            signal_track  TEXT,
            active        INTEGER DEFAULT 1,
            first_seen    TEXT NOT NULL,
            last_updated  TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id  TEXT NOT NULL,
            polled_at     TEXT NOT NULL,
            yes_price     REAL,
            no_price      REAL,
            volume        REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ph_condition ON price_history(condition_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ph_time ON price_history(polled_at)")
    conn.commit()
    return conn


# ── API fetching ───────────────────────────────────────────────────────────────

def fetch_all_active_markets() -> list:
    """
    Paginate through all active Polymarket markets.
    Returns a flat list of market dicts.
    """
    markets = []
    offset  = 0

    while True:
        url = f"{API_BASE}/markets"
        params = {
            "active": "true",
            "closed": "false",
            "limit":  PAGE_SIZE,
            "offset": offset,
        }
        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            page = resp.json()
        except requests.RequestException as e:
            print(f"  [API] Error at offset {offset}: {e}")
            break

        if not page:
            break

        markets.extend(page)

        # Stop early if this page is mostly inactive/closed — Polymarket returns markets
        # newest-first, so once we hit pages with mostly resolved old markets we can stop
        active_on_page = sum(1 for m in page if m.get("active") is True and not m.get("closed"))
        if active_on_page < PAGE_SIZE * 0.1:  # fewer than 10% active on this page
            break

        if len(page) < PAGE_SIZE:
            break   # last page

        offset += PAGE_SIZE
        time.sleep(0.2)  # be gentle

    return markets


# ── Poll ───────────────────────────────────────────────────────────────────────

def poll(conn: sqlite3.Connection):
    now_str  = datetime.now(timezone.utc).isoformat()
    now_disp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")

    print(f"\nPolymarket Poll — {now_disp}")
    print("-" * 65)
    print("  Fetching active markets...")

    all_markets = fetch_all_active_markets()
    print(f"  Total markets fetched: {len(all_markets):,}")

    # Filter: currently active + not closed + ME keyword match
    # Note: the API ?active=true param does NOT filter server-side — must filter client-side
    active_markets = [m for m in all_markets
                      if m.get("active") is True and not m.get("closed")]
    me_markets     = [m for m in active_markets if is_me_market(m.get("question", ""))]
    print(f"  Active markets:        {len(active_markets):,}")
    print(f"  ME markets matched:    {len(me_markets)}")

    if not me_markets:
        print("  No ME markets found — check keyword list.")
        return

    # Track which condition_ids we saw this poll (for deactivating stale entries)
    seen_ids = set()

    new_count     = 0
    updated_count = 0

    for m in me_markets:
        condition_id = m.get("conditionId") or m.get("id")
        if not condition_id:
            continue

        question     = m.get("question", "")
        slug         = m.get("slug", "")
        end_date     = m.get("endDate", "")
        volume       = m.get("volume") or m.get("volumeNum") or 0.0
        liquidity    = m.get("liquidity") or 0.0
        signal_track = classify_track(question)

        yes_price, no_price = parse_prices(m.get("outcomePrices"))
        if yes_price is None:
            continue   # no prices yet — skip

        seen_ids.add(condition_id)

        # Upsert into markets table
        existing = conn.execute(
            "SELECT first_seen FROM markets WHERE condition_id = ?",
            (condition_id,)
        ).fetchone()

        if existing:
            conn.execute("""
                UPDATE markets
                SET question=?, slug=?, yes_price=?, no_price=?,
                    volume=?, liquidity=?, end_date=?, signal_track=?,
                    active=1, last_updated=?
                WHERE condition_id=?
            """, (question, slug, yes_price, no_price,
                  volume, liquidity, end_date, signal_track,
                  now_str, condition_id))
            updated_count += 1
        else:
            conn.execute("""
                INSERT INTO markets
                    (condition_id, question, slug, yes_price, no_price,
                     volume, liquidity, end_date, signal_track,
                     active, first_seen, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """, (condition_id, question, slug, yes_price, no_price,
                  volume, liquidity, end_date, signal_track,
                  now_str, now_str))
            new_count += 1

        # Always record price history
        conn.execute("""
            INSERT INTO price_history (condition_id, polled_at, yes_price, no_price, volume)
            VALUES (?, ?, ?, ?, ?)
        """, (condition_id, now_str, yes_price, no_price, volume))

    # Mark markets that were previously active but absent this poll as inactive
    previously_active = conn.execute(
        "SELECT condition_id FROM markets WHERE active=1"
    ).fetchall()
    deactivated = 0
    for (cid,) in previously_active:
        if cid not in seen_ids:
            conn.execute(
                "UPDATE markets SET active=0, last_updated=? WHERE condition_id=?",
                (now_str, cid)
            )
            deactivated += 1

    conn.commit()

    print(f"  New markets:       {new_count}")
    print(f"  Updated:           {updated_count}")
    print(f"  Deactivated:       {deactivated}")
    print()

    # Print current state
    rows = conn.execute("""
        SELECT question, signal_track, yes_price, volume, end_date
        FROM markets
        WHERE active=1
        ORDER BY volume DESC
    """).fetchall()

    if rows:
        print(f"  {'Track':<5}  {'Yes%':>5}  {'Volume':>8}  {'Expires':<12}  Question")
        print(f"  {'-'*80}")
        for q, track, yp, vol, ed in rows:
            t    = "Esc" if track == "escalation" else "De "
            yp_s = f"{yp*100:.0f}%"
            vol_s = f"${vol/1e6:.1f}M" if vol >= 1e6 else f"${vol/1e3:.0f}K"
            exp  = ed[:10] if ed else "—"
            q_short = q[:55] + "…" if len(q) > 55 else q
            print(f"  {t:<5}  {yp_s:>5}  {vol_s:>8}  {exp:<12}  {q_short}")


# ── Status ─────────────────────────────────────────────────────────────────────

def print_status(conn: sqlite3.Connection):
    total  = conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
    active = conn.execute("SELECT COUNT(*) FROM markets WHERE active=1").fetchone()[0]
    hist   = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    last   = conn.execute("SELECT MAX(last_updated) FROM markets").fetchone()[0]

    if not total:
        print("No markets yet. Run without --status first.")
        return

    print(f"\nPolymarket Database Summary")
    print("=" * 65)
    print(f"Total markets tracked:  {total}")
    print(f"Currently active:       {active}")
    print(f"Price history rows:     {hist:,}")
    print(f"Last poll:              {last[:16] if last else '—'} UTC")

    print(f"\nActive ME markets (sorted by volume):")
    rows = conn.execute("""
        SELECT question, signal_track, yes_price, no_price,
               volume, liquidity, end_date
        FROM markets
        WHERE active=1
        ORDER BY volume DESC
    """).fetchall()

    for q, track, yp, np_, vol, liq, ed in rows:
        t    = "ESC" if track == "escalation" else "DE "
        yp_s = f"Yes={yp*100:.1f}%  No={np_*100:.1f}%"
        vol_s = f"${vol/1e6:.2f}M" if vol >= 1e6 else f"${vol/1e3:.0f}K"
        exp  = ed[:10] if ed else "—"
        print(f"\n  [{t}] {q}")
        print(f"       {yp_s}  |  Vol: {vol_s}  |  Expires: {exp}")


# ── Entry point ────────────────────────────────────────────────────────────────

def reclassify(conn: sqlite3.Connection):
    """Re-run classify_track on all markets in the DB and update signal_track."""
    rows = conn.execute("SELECT condition_id, question FROM markets").fetchall()
    updated = 0
    for cid, question in rows:
        track = classify_track(question)
        conn.execute(
            "UPDATE markets SET signal_track=? WHERE condition_id=?", (track, cid)
        )
        updated += 1
    conn.commit()
    print(f"Reclassified {updated} markets.")
    print_status(conn)


def main():
    parser = argparse.ArgumentParser(description="MarketSignal Polymarket collector")
    parser.add_argument("--loop",        action="store_true", help="Poll continuously every 15 minutes")
    parser.add_argument("--status",      action="store_true", help="Print tracked markets and exit")
    parser.add_argument("--reclassify",  action="store_true", help="Re-run track classification on all DB entries and exit")
    args = parser.parse_args()

    conn = init_db(DB_PATH)

    if args.status:
        print_status(conn)
        conn.close()
        return

    if args.reclassify:
        reclassify(conn)
        conn.close()
        return

    if args.loop:
        print(f"Polymarket collector running every {POLL_INTERVAL // 60} minutes. Ctrl+C to stop.")
        while True:
            try:
                poll(conn)
                print(f"  Sleeping {POLL_INTERVAL // 60}m...")
                time.sleep(POLL_INTERVAL)
            except KeyboardInterrupt:
                print("\nStopped.")
                break
    else:
        poll(conn)

    conn.close()


if __name__ == "__main__":
    main()
