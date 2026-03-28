#!/usr/bin/env python3
"""
MarketSignal — Polymarket Collector v2 (Stage 4)

Polls the Polymarket Gamma API for active Middle East markets, uses Claude
to classify whether each market is predictable by the signal system, and
stores prices to polymarket_markets.db.

v2 changes:
  - Claude-based classification replaces keyword geopolitical gate + track
    classifier. The model understands which outcomes the signal system can
    actually predict (military activity, airspace changes, diplomatic
    movements) vs. things it can't (elections, UN votes, sanctions rhetoric).
  - Keyword geo-filter retained as cheap pre-filter to avoid sending hundreds
    of non-ME markets to Claude.
  - Keyword classification used as automatic fallback when Claude API is
    unavailable or ANTHROPIC_API_KEY is not set.
  - Classification is cached in the markets table (classified_by column).
    Markets already classified by Claude are not re-classified on subsequent
    polls unless --reclassify is passed.

Usage:
    python3 polymarket_collector.py                # poll once
    python3 polymarket_collector.py --loop         # continuous, every 15 minutes
    python3 polymarket_collector.py --status       # print all tracked markets
    python3 polymarket_collector.py --reclassify   # re-run Claude classification on all DB entries
"""

import sqlite3
import requests
import re
import json
import time
import argparse
import os
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────

DB_PATH          = "polymarket_markets.db"
API_BASE         = "https://gamma-api.polymarket.com"
POLL_INTERVAL    = 900    # 15 minutes
PAGE_SIZE        = 100    # markets per API page
REQUEST_TIMEOUT  = 10     # seconds
CLAUDE_TIMEOUT   = 30     # seconds for classification API call
CLAUDE_MODEL     = "claude-sonnet-4-20250514"

# Set this in .env or environment — without it, falls back to keyword classification
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ── Geography pre-filter (cheap, keyword-based) ──────────────────────────────
# Only checks geography — does NOT determine relevance or track.
# Purpose: avoid sending hundreds of non-ME markets to Claude.

ME_KEYWORDS_PHRASE = [
    "israel", "gaza", "lebanon", "syria",
    "houthi", "yemen", "red sea", "west bank", "hamas", "hezbollah",
    "palestinian", "irgc", "rafah",
    "middle east", "tel aviv", "beirut", "tehran",
]

ME_KEYWORDS_WORD = ["iran"]

_ME_WORD_RE = re.compile(
    r"\b(" + "|".join(re.escape(kw) for kw in ME_KEYWORDS_WORD) + r")",
    re.IGNORECASE,
)


def is_me_geography(question: str) -> bool:
    """Cheap check: does the question mention ME geography?
    This is a PRE-FILTER only — it does not determine relevance."""
    q = question.lower()
    return any(kw in q for kw in ME_KEYWORDS_PHRASE) or bool(_ME_WORD_RE.search(question))


# ── Keyword fallback classification (used when Claude unavailable) ───────────

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

DEESC_KEYWORDS = [
    "ceasefire", "peace deal", "peace agreement", "truce",
    "hostage deal", "hostages released", "hostages freed",
    "prisoner exchange", "prisoner swap",
    "withdrawal", "withdraws", "pulls out",
    "diplomatic", "normalization", "normalisation", "normaliz", "normalise",
    "two-state", "de-escalat", "deescalat",
    "nuclear deal", "disarm", "end of military", "end hostilities",
    "halt operations",
]

_CONFLICT_NOUNS = r"(conflict|war|fighting|hostilities|crisis|violence|offensive)"
_ENDING_VERBS   = r"(ends?|over|halt|cease|stop|resolv|settl)"
_DEESC_PATTERNS = [
    re.compile(rf"{_CONFLICT_NOUNS}\s+{_ENDING_VERBS}", re.IGNORECASE),
    re.compile(rf"{_ENDING_VERBS}\s+.{{0,20}}{_CONFLICT_NOUNS}", re.IGNORECASE),
    re.compile(r"\bend\s+by\b",             re.IGNORECASE),
    re.compile(r"\bends\s+by\b",            re.IGNORECASE),
    re.compile(r"\bover\s+by\b",            re.IGNORECASE),
    re.compile(r"\bresolved?\s+by\b",       re.IGNORECASE),
    re.compile(r"\bpeace\b",                re.IGNORECASE),
    re.compile(r"\bnegotiat",               re.IGNORECASE),
    re.compile(r"\breleas",                 re.IGNORECASE),
    re.compile(r"\bfreed?\b",               re.IGNORECASE),
    re.compile(r"\bend\s+of\s+(military|combat|offensive|air)", re.IGNORECASE),
    re.compile(r"announces?\s+end\s+of",    re.IGNORECASE),
    re.compile(r"\bnuclear\s+(deal|agree)",  re.IGNORECASE),
    re.compile(r"\bdisarm",                 re.IGNORECASE),
    re.compile(r"\bnormaliz",               re.IGNORECASE),
    re.compile(r"\bagrees?\s+to\s+(end|stop|halt|limit|cap)", re.IGNORECASE),
]


def classify_track_keywords(question: str) -> str:
    """Keyword-based track classification (fallback)."""
    q = question.lower()
    if any(kw in q for kw in DEESC_KEYWORDS):
        return "deescalation"
    if any(p.search(question) for p in _DEESC_PATTERNS):
        return "deescalation"
    return "escalation"


def is_relevant_keywords(question: str) -> bool:
    """Keyword-based relevance check (fallback). Requires geo + geopolitical match."""
    q = question.lower()
    return any(kw in q for kw in GEOPOLITICAL_KEYWORDS)


# ── Claude classification ────────────────────────────────────────────────────

CLASSIFICATION_SYSTEM_PROMPT = """You classify prediction markets for a geopolitical signal detection system that monitors the Middle East.

The system's signal sources are:
- ADS-B airspace monitoring: airline route suspensions, commercial traffic drops, no-fly zones
- Military aircraft tracking: ISR platforms (E-11A BACN, RC-135), tankers (KC-135, KC-46), strategic lift (C-17, IL-76), command aircraft (E-4B)
- Diplomatic aircraft tracking: VIP government/royal flights, bizjet clusters at airports (indicator of back-channel negotiations)
- NOTAM airspace restrictions: new restricted zones, airspace closures
- Maritime AIS tracking: tanker/cargo density changes, military vessel movements, GPS spoofing campaigns
- GDELT event database: Goldstein cooperation/conflict scale trends across all ME events

For each market, decide:

1. RELEVANT: Can these specific signal types predict this market's outcome?
   YES examples: military strikes, airstrikes, ground invasions, naval blockades, ceasefire agreements (diplomatic flights + GDELT predict these), escalation/de-escalation of active conflicts, missile/drone attacks, military withdrawals, hostage deals (diplomatic bizjet activity predicts these), territorial control changes, airspace closures
   NO examples: elections, political appointments, economic sanctions policy, UN votes, ICC/ICJ rulings, public opinion, refugee statistics, specific casualty counts, statements/rhetoric by officials, social media trends, economic indicators, oil prices (unless the question is specifically about a physical blockade)

2. TRACK: If relevant, is the market asking about escalation or de-escalation?
   escalation: military action, strikes, invasion, conflict expansion, attacks, ground operations
   deescalation: ceasefire, peace deal, withdrawal, hostage release, diplomatic agreement, truce, conflict ending

Respond ONLY with a JSON array — no markdown fences, no explanation. Each element:
{"id": <index>, "relevant": true/false, "track": "escalation"|"deescalation"|null, "reason": "<brief reason, 10 words max>"}"""


CLAUDE_BATCH_SIZE = 50   # markets per API call — keeps prompt small and avoids timeouts


def _classify_batch(batch, offset):
    """
    Classify a single batch of up to CLAUDE_BATCH_SIZE markets.
    Returns a dict mapping absolute list index → classification, or None on error.
    """
    lines = [f"{i}. {m.get('question', '')}" for i, m in enumerate(batch)]
    prompt = "Classify these prediction markets:\n\n" + "\n".join(lines)

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 4096,
                "system": CLASSIFICATION_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=CLAUDE_TIMEOUT,
        )

        if resp.status_code != 200:
            print(f"  [Claude] API error {resp.status_code}: {resp.text[:200]}")
            return None

        data = resp.json()
        text = "".join(
            block.get("text", "") for block in data.get("content", [])
        )

        # Strip markdown fences if the model wraps the JSON
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        classifications = json.loads(text)

        result = {}
        for item in classifications:
            idx = item.get("id")
            if idx is not None and 0 <= idx < len(batch):
                result[offset + idx] = {
                    "relevant": bool(item.get("relevant", False)),
                    "track":    item.get("track"),
                    "reason":   item.get("reason", ""),
                }
        return result

    except json.JSONDecodeError as e:
        print(f"  [Claude] JSON parse error: {e}")
        return None
    except requests.RequestException as e:
        print(f"  [Claude] Request error: {e}")
        return None
    except Exception as e:
        print(f"  [Claude] Unexpected error: {e}")
        return None


def classify_markets_with_claude(markets):
    """
    Classify a list of market questions using Claude, in batches.

    Args:
        markets: list of dicts, each with at least a 'question' key

    Returns:
        dict mapping list index → {"relevant": bool, "track": str|None, "reason": str}
        Returns None if every batch fails (caller falls back to keywords).
    """
    if not ANTHROPIC_API_KEY:
        return None

    if not markets:
        return {}

    result = {}
    any_success = False
    for offset in range(0, len(markets), CLAUDE_BATCH_SIZE):
        batch = markets[offset : offset + CLAUDE_BATCH_SIZE]
        batch_num = offset // CLAUDE_BATCH_SIZE + 1
        total_batches = (len(markets) + CLAUDE_BATCH_SIZE - 1) // CLAUDE_BATCH_SIZE
        print(f"  [Claude] Batch {batch_num}/{total_batches} ({len(batch)} markets)...")
        batch_result = _classify_batch(batch, offset)
        if batch_result is not None:
            result.update(batch_result)
            any_success = True
        else:
            # On batch failure, fall back to keywords for this batch only
            print(f"  [Claude] Batch {batch_num} failed — keyword fallback for these markets")

    return result if any_success else None


# ── Helpers ────────────────────────────────────────────────────────────────────

def parse_prices(outcome_prices):
    """Parse outcomePrices field. Returns (yes_price, no_price) or (None, None)."""
    if not outcome_prices:
        return None, None
    if isinstance(outcome_prices, str):
        try:
            outcome_prices = json.loads(outcome_prices)
        except (ValueError, TypeError):
            return None, None
    if len(outcome_prices) != 2:
        return None, None
    try:
        return float(outcome_prices[0]), float(outcome_prices[1])
    except (TypeError, ValueError):
        return None, None


def minutes_since(iso_str: str) -> float:
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
            condition_id    TEXT PRIMARY KEY,
            question        TEXT NOT NULL,
            slug            TEXT,
            yes_price       REAL,
            no_price        REAL,
            volume          REAL,
            liquidity       REAL,
            end_date        TEXT,
            signal_track    TEXT,
            classified_by   TEXT DEFAULT 'keyword',
            classify_reason TEXT,
            active          INTEGER DEFAULT 1,
            first_seen      TEXT NOT NULL,
            last_updated    TEXT NOT NULL
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

    # Migrate existing DBs
    for col_def in ["classified_by TEXT DEFAULT 'keyword'", "classify_reason TEXT"]:
        try:
            conn.execute(f"ALTER TABLE markets ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass

    conn.commit()
    return conn


# ── API fetching ───────────────────────────────────────────────────────────────

def fetch_all_active_markets() -> list:
    """Paginate through all active Polymarket markets."""
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

        active_on_page = sum(1 for m in page if m.get("active") is True and not m.get("closed"))
        if active_on_page < PAGE_SIZE * 0.1:
            break

        if len(page) < PAGE_SIZE:
            break

        offset += PAGE_SIZE
        time.sleep(0.2)

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

    active_markets = [m for m in all_markets
                      if m.get("active") is True and not m.get("closed")]

    # Stage 1: Cheap geography pre-filter
    geo_candidates = [m for m in active_markets
                      if is_me_geography(m.get("question", ""))]
    print(f"  Active markets:        {len(active_markets):,}")
    print(f"  ME geography matches:  {len(geo_candidates)}")

    if not geo_candidates:
        print("  No ME markets found — check keyword list.")
        return

    # Stage 2: Classify with Claude (or fall back to keywords)
    # Only classify markets we haven't seen before or that need reclassification
    existing_cids = {
        row[0]: row[1] for row in
        conn.execute("SELECT condition_id, classified_by FROM markets WHERE active=1").fetchall()
    }

    needs_classification = []
    already_classified   = []
    for m in geo_candidates:
        cid = m.get("conditionId") or m.get("id")
        if cid and existing_cids.get(cid) == "claude":
            already_classified.append(m)  # already Claude-classified, just update price
        else:
            needs_classification.append(m)

    # Classify new/unclassified markets
    claude_results = None
    if needs_classification:
        print(f"  Classifying {len(needs_classification)} market(s) with Claude...")
        claude_results = classify_markets_with_claude(needs_classification)

        if claude_results is not None:
            relevant_count = sum(1 for v in claude_results.values() if v["relevant"])
            print(f"  Claude: {relevant_count} relevant, {len(claude_results) - relevant_count} filtered out")
        else:
            print("  Claude unavailable — falling back to keyword classification")

    # Process all markets
    seen_ids      = set()
    new_count     = 0
    updated_count = 0
    filtered_out  = 0

    for m in geo_candidates:
        condition_id = m.get("conditionId") or m.get("id")
        if not condition_id:
            continue

        question     = m.get("question", "")
        slug         = m.get("slug", "")
        end_date     = m.get("endDate", "")
        volume       = m.get("volume") or m.get("volumeNum") or 0.0
        liquidity    = m.get("liquidity") or 0.0

        yes_price, no_price = parse_prices(m.get("outcomePrices"))
        if yes_price is None:
            continue

        # Determine classification
        if m in needs_classification and claude_results is not None:
            # Use Claude classification
            idx = needs_classification.index(m)
            clf = claude_results.get(idx)
            if clf and clf["relevant"]:
                signal_track    = clf["track"] or "escalation"
                classified_by   = "claude"
                classify_reason = clf.get("reason", "")
            elif clf and not clf["relevant"]:
                # Claude says not relevant — skip this market
                filtered_out += 1
                continue
            else:
                # Claude didn't return a result for this index — fall back
                if not is_relevant_keywords(question):
                    filtered_out += 1
                    continue
                signal_track    = classify_track_keywords(question)
                classified_by   = "keyword"
                classify_reason = "Claude result missing, keyword fallback"
        elif existing_cids.get(condition_id) == "claude":
            # Already classified by Claude — keep existing track, just update price
            existing_track = conn.execute(
                "SELECT signal_track FROM markets WHERE condition_id=?",
                (condition_id,)
            ).fetchone()
            signal_track    = existing_track[0] if existing_track else "escalation"
            classified_by   = "claude"
            classify_reason = None  # don't overwrite existing reason
        else:
            # No Claude available — full keyword fallback
            if not is_relevant_keywords(question):
                filtered_out += 1
                continue
            signal_track    = classify_track_keywords(question)
            classified_by   = "keyword"
            classify_reason = "keyword fallback"

        seen_ids.add(condition_id)

        # Upsert
        existing = conn.execute(
            "SELECT first_seen FROM markets WHERE condition_id = ?",
            (condition_id,)
        ).fetchone()

        if existing:
            if classify_reason is not None:
                conn.execute("""
                    UPDATE markets
                    SET question=?, slug=?, yes_price=?, no_price=?,
                        volume=?, liquidity=?, end_date=?, signal_track=?,
                        classified_by=?, classify_reason=?,
                        active=1, last_updated=?
                    WHERE condition_id=?
                """, (question, slug, yes_price, no_price,
                      volume, liquidity, end_date, signal_track,
                      classified_by, classify_reason,
                      now_str, condition_id))
            else:
                # Price update only — don't overwrite Claude classification
                conn.execute("""
                    UPDATE markets
                    SET yes_price=?, no_price=?, volume=?, liquidity=?,
                        active=1, last_updated=?
                    WHERE condition_id=?
                """, (yes_price, no_price, volume, liquidity,
                      now_str, condition_id))
            updated_count += 1
        else:
            conn.execute("""
                INSERT INTO markets
                    (condition_id, question, slug, yes_price, no_price,
                     volume, liquidity, end_date, signal_track,
                     classified_by, classify_reason,
                     active, first_seen, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """, (condition_id, question, slug, yes_price, no_price,
                  volume, liquidity, end_date, signal_track,
                  classified_by, classify_reason,
                  now_str, now_str))
            new_count += 1

        # Record price history
        conn.execute("""
            INSERT INTO price_history (condition_id, polled_at, yes_price, no_price, volume)
            VALUES (?, ?, ?, ?, ?)
        """, (condition_id, now_str, yes_price, no_price, volume))

    # Deactivate stale/expired markets
    previously_active = conn.execute(
        "SELECT condition_id, end_date FROM markets WHERE active=1"
    ).fetchall()
    deactivated = 0
    for (cid, end_date) in previously_active:
        expired = end_date and end_date < now_str
        if cid not in seen_ids or expired:
            conn.execute(
                "UPDATE markets SET active=0, last_updated=? WHERE condition_id=?",
                (now_str, cid)
            )
            deactivated += 1

    conn.commit()

    print(f"  New markets:       {new_count}")
    print(f"  Updated:           {updated_count}")
    print(f"  Filtered (irrelevant): {filtered_out}")
    print(f"  Deactivated:       {deactivated}")
    print()

    # Print current state
    rows = conn.execute("""
        SELECT question, signal_track, yes_price, volume, end_date,
               classified_by, classify_reason
        FROM markets
        WHERE active=1
        ORDER BY volume DESC
    """).fetchall()

    if rows:
        print(f"  {'Track':<5} {'Cls':<4} {'Yes%':>5}  {'Volume':>8}  {'Expires':<12}  Question")
        print(f"  {'-'*90}")
        for q, track, yp, vol, ed, cls_by, cls_reason in rows:
            t     = "Esc" if track == "escalation" else "De "
            cls   = "AI" if cls_by == "claude" else "KW"
            yp_s  = f"{yp*100:.0f}%"
            vol_s = f"${vol/1e6:.1f}M" if vol >= 1e6 else f"${vol/1e3:.0f}K"
            exp   = ed[:10] if ed else "—"
            q_short = q[:50] + "…" if len(q) > 50 else q
            print(f"  {t:<5} {cls:<4} {yp_s:>5}  {vol_s:>8}  {exp:<12}  {q_short}")


# ── Status ─────────────────────────────────────────────────────────────────────

def print_status(conn: sqlite3.Connection):
    total    = conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
    active   = conn.execute("SELECT COUNT(*) FROM markets WHERE active=1").fetchone()[0]
    hist     = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    last     = conn.execute("SELECT MAX(last_updated) FROM markets").fetchone()[0]
    by_claude = conn.execute("SELECT COUNT(*) FROM markets WHERE classified_by='claude'").fetchone()[0]
    by_kw    = conn.execute("SELECT COUNT(*) FROM markets WHERE classified_by='keyword'").fetchone()[0]

    if not total:
        print("No markets yet. Run without --status first.")
        return

    print(f"\nPolymarket Database Summary")
    print("=" * 65)
    print(f"  Total markets tracked:    {total}")
    print(f"  Currently active:         {active}")
    print(f"  Classified by Claude:     {by_claude}")
    print(f"  Classified by keywords:   {by_kw}")
    print(f"  Price history rows:       {hist:,}")
    print(f"  Last poll:                {last[:16] if last else '—'} UTC")

    print(f"\nActive ME markets (sorted by volume):")
    rows = conn.execute("""
        SELECT question, signal_track, yes_price, no_price,
               volume, liquidity, end_date, classified_by, classify_reason
        FROM markets
        WHERE active=1
        ORDER BY volume DESC
    """).fetchall()

    for q, track, yp, np_, vol, liq, ed, cls_by, cls_reason in rows:
        t     = "ESC" if track == "escalation" else "DE "
        cls   = "Claude" if cls_by == "claude" else "Keyword"
        yp_s  = f"Yes={yp*100:.1f}%  No={np_*100:.1f}%"
        vol_s = f"${vol/1e6:.2f}M" if vol >= 1e6 else f"${vol/1e3:.0f}K"
        exp   = ed[:10] if ed else "—"
        print(f"\n  [{t}] {q}")
        print(f"       {yp_s}  |  Vol: {vol_s}  |  Expires: {exp}  |  {cls}", end="")
        if cls_reason:
            print(f": {cls_reason}")
        else:
            print()


# ── Reclassify ────────────────────────────────────────────────────────────────

def reclassify(conn: sqlite3.Connection):
    """Re-classify ALL markets using Claude (or keywords as fallback).
    This forces reclassification even for markets already classified by Claude."""

    rows = conn.execute("SELECT condition_id, question, active FROM markets").fetchall()
    if not rows:
        print("No markets in database.")
        return

    # Pre-filter to ME geography
    me_rows = [(cid, q, active) for cid, q, active in rows if is_me_geography(q)]
    non_me  = len(rows) - len(me_rows)
    print(f"  Total markets: {len(rows)}, ME geography: {len(me_rows)}, non-ME: {non_me}")

    # Deactivate non-ME
    for cid, q, active in rows:
        if not is_me_geography(q) and active:
            conn.execute("UPDATE markets SET active=0 WHERE condition_id=?", (cid,))

    if not me_rows:
        conn.commit()
        print("No ME markets to classify.")
        return

    # Attempt Claude classification
    market_dicts = [{"question": q, "conditionId": cid} for cid, q, _ in me_rows]
    print(f"  Sending {len(market_dicts)} markets to Claude for classification...")
    claude_results = classify_markets_with_claude(market_dicts)

    reclassified = 0
    deactivated  = 0

    for i, (cid, question, was_active) in enumerate(me_rows):
        if claude_results is not None and i in claude_results:
            clf = claude_results[i]
            if clf["relevant"]:
                track  = clf["track"] or "escalation"
                reason = clf.get("reason", "")
                conn.execute(
                    """UPDATE markets SET signal_track=?, classified_by='claude',
                       classify_reason=?, active=1 WHERE condition_id=?""",
                    (track, reason, cid)
                )
                reclassified += 1
            else:
                conn.execute(
                    """UPDATE markets SET active=0, classified_by='claude',
                       classify_reason=? WHERE condition_id=?""",
                    (clf.get("reason", "not predictable by signal system"), cid)
                )
                deactivated += 1
        else:
            # Keyword fallback
            if is_relevant_keywords(question):
                track = classify_track_keywords(question)
                conn.execute(
                    """UPDATE markets SET signal_track=?, classified_by='keyword',
                       classify_reason='keyword fallback', active=1
                       WHERE condition_id=?""",
                    (track, cid)
                )
                reclassified += 1
            else:
                conn.execute(
                    "UPDATE markets SET active=0 WHERE condition_id=?", (cid,)
                )
                deactivated += 1

    conn.commit()
    source = "Claude" if claude_results is not None else "keywords"
    print(f"  Classified via {source}: {reclassified} relevant, {deactivated} filtered out")
    print()
    print_status(conn)


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="MarketSignal Polymarket collector v2")
    parser.add_argument("--loop",        action="store_true", help="Poll continuously every 15 minutes")
    parser.add_argument("--status",      action="store_true", help="Print tracked markets and exit")
    parser.add_argument("--reclassify",  action="store_true", help="Re-run Claude classification on all DB entries and exit")
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
