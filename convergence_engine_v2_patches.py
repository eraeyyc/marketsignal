"""
MarketSignal — Convergence Engine v2 Patches
=============================================

Drop-in replacement functions for convergence_engine.py.

WHAT CHANGED AND WHY:
─────────────────────
1. REMOVED competing_probabilities()
   The net = esc_norm - deesc_norm formula collapsed two dimensions into one.
   "No signals" and "strong conflicting signals" both produced ~50% — but
   those are completely different situations for finding Polymarket edge.

2. ADDED track_probability()
   Each track now gets an independent sigmoid probability using its own beta.
   Escalation markets compare against esc_prob; de-escalation markets compare
   against deesc_prob. They don't couple or sum to 1.

3. ADDED compute_tension()
   New metric: geometric mean of both probabilities. High tension = both tracks
   elevated simultaneously = maximum ambiguity = markets most likely mispriced.
   This is the "fork in the road" signal the old formula threw away.

4. UPDATED compute_velocity() → works for both tracks
   De-escalation signals now get an urgency bonus when rising. A sudden flurry
   of diplomatic bizjets gets the same velocity treatment as a military surge.

5. ADDED compute_edge()
   Per-market edge calculation that routes to the correct track and adjusts
   for market-specific factors.

6. UPDATED save_score(), init_engine_db(), compute(), print_status()
   New columns: tension, deesc_velocity_24h, deesc_velocity_bonus.
   compute() now calculates velocity for both tracks independently.

INTEGRATION:
────────────
Replace the corresponding functions in convergence_engine.py.
The init_engine_db() migration is additive (ALTER TABLE) so your existing
convergence_engine.db will upgrade automatically on first run.

After patching, your dashboard's Convergence Engine page will need to read
the new `tension` column and display it. The `escalation_prob` and
`deescalation_prob` columns now store independent values (they no longer
sum to 1), so any UI showing them as a stacked bar or pie chart should
be updated to show them as two separate gauges.
"""

import math
import json
import sqlite3
from datetime import datetime, timezone, timedelta


# ── These constants are unchanged, included here for reference ────────────────

SIGMOID_BETA       = 100.0
DEESC_SIGMOID_BETA = 40.0
SIGMOID_ALPHA      = 0.08

VELOCITY_WEIGHT     = 0.30
VELOCITY_LOOKBACK_H = 24
VELOCITY_MAX_BONUS  = 30.0


# ══════════════════════════════════════════════════════════════════════════════
# REPLACEMENT 1: Delete competing_probabilities(), add these two functions
# ══════════════════════════════════════════════════════════════════════════════

def track_probability(raw_score, track="escalation", velocity_bonus=0.0):
    """
    Independent sigmoid normalisation for a single track.

    Each track uses its own beta (midpoint) reflecting the structural
    differences in signal volume:
      - Escalation  β=100: active war stacks 6 layers → 150-200 pts
      - De-escalation β=40: ceasefire scenario peaks ~60-70 pts

    Returns a 0–1 probability for this track alone. No coupling to the
    other track — they are separate assessments of separate questions.

    This replaces competing_probabilities(). The key difference:
      OLD: esc + deesc always sum to 1 → "no signals" ≈ "conflicting signals" ≈ 50%
      NEW: each track is independent → "no signals" = (esc≈0%, deesc≈4%) which is
           clearly distinguishable from "conflicting signals" = (esc≈50%, deesc≈50%)
    """
    if track == "deescalation":
        beta = DEESC_SIGMOID_BETA
    else:
        beta = SIGMOID_BETA
    adjusted = raw_score + velocity_bonus
    prob = 1.0 / (1.0 + math.exp(-SIGMOID_ALPHA * (adjusted - beta)))
    return round(prob, 4)


def compute_tension(esc_prob, deesc_prob):
    """
    Tension metric: how elevated BOTH tracks are simultaneously.

    Returns 0–1:
      ~0.0 = at least one track is quiet (one-sided or no signals)
      ~1.0 = both tracks near 100% (maximum contradictory signals)

    Uses geometric mean: sqrt(esc_prob × deesc_prob).
    Unlike arithmetic mean, geometric mean requires BOTH inputs to be
    elevated to produce a high value. If either track is near zero,
    tension collapses regardless of the other.

    WHY THIS MATTERS FOR POLYMARKET:
    High tension is the single best indicator of mispriced markets.
    When physical signals point both directions simultaneously, crowds
    default to ~50/50 pricing — but your system can see the specific
    signals driving each track and identify which direction has stronger
    physical corroboration (coherence multiplier, signal diversity, etc).

    Use tension > 0.15 as a flag to surface the divergence detail and
    examine which track has coherence support vs which is GDELT-only.
    """
    return round(math.sqrt(max(0.0, esc_prob) * max(0.0, deesc_prob)), 4)


# ══════════════════════════════════════════════════════════════════════════════
# REPLACEMENT 2: compute_velocity() — now accepts a `track` parameter
# ══════════════════════════════════════════════════════════════════════════════

def compute_velocity(engine_conn, current_raw, track="escalation"):
    """
    Compare the current raw score against the score from ~24h ago.
    Works for both escalation and de-escalation tracks.

    Returns (velocity_24h, velocity_bonus):
      velocity_24h  — signed rate of change (positive = rising)
      velocity_bonus — extra score added before sigmoid (only for rising scores)

    A ±2h search window around the 24h lookback handles gaps in polling.
    Returns (0.0, 0.0) when insufficient history exists.
    """
    col = "escalation_raw" if track == "escalation" else "deescalation_raw"

    lo = (datetime.now(timezone.utc) - timedelta(hours=VELOCITY_LOOKBACK_H + 2)).isoformat()
    hi = (datetime.now(timezone.utc) - timedelta(hours=VELOCITY_LOOKBACK_H - 2)).isoformat()

    row = engine_conn.execute(f"""
        SELECT {col} FROM scores
        WHERE computed_at BETWEEN ? AND ?
        ORDER BY computed_at DESC
        LIMIT 1
    """, (lo, hi)).fetchone()

    if row is None:
        return 0.0, 0.0

    velocity_24h   = current_raw - row[0]
    velocity_bonus  = min(max(0.0, velocity_24h) * VELOCITY_WEIGHT, VELOCITY_MAX_BONUS)
    return round(velocity_24h, 3), round(velocity_bonus, 3)


# ══════════════════════════════════════════════════════════════════════════════
# NEW: compute_edge() — per-market edge calculation
# ══════════════════════════════════════════════════════════════════════════════

def compute_edge(market_track, market_yes_price, esc_prob, deesc_prob, tension=0.0):
    """
    Compute edge for a single Polymarket market.

    Routes to the correct track probability based on market classification:
      - Escalation market  → compare model's esc_prob against market Yes price
      - De-escalation market → compare model's deesc_prob against market Yes price

    Returns dict with:
      edge        — signed difference (positive = market underpricing, buy Yes)
      confidence  — how much to trust this edge (reduced under high tension)
      direction   — "YES" or "NO" (which side to bet)

    IMPORTANT CAVEAT:
    The model produces a general "escalation likelihood" score, but each
    Polymarket question asks about a SPECIFIC event (e.g., "Israeli strike
    on Iran by April"). The model probability is a prior that should be
    adjusted per-market based on:
      - Time horizon (short-dated markets need stronger signals)
      - Specificity (broad "will there be escalation" ≈ direct comparison;
        narrow "will X specific event happen" needs a discount)

    For now this returns the raw edge. Phase 2 should add per-market
    calibration based on market end_date and question specificity.
    """
    if market_track == "escalation":
        model_prob = esc_prob
    elif market_track == "deescalation":
        model_prob = deesc_prob
    else:
        return None

    raw_edge = model_prob - market_yes_price

    # Confidence adjustment: high tension means the model is seeing strong
    # signals in BOTH directions. Edge estimates are less reliable when
    # the situation is genuinely ambiguous, so we discount.
    # tension=0 → full confidence; tension=0.5 → 75% confidence
    confidence = 1.0 - (tension * 0.5)

    adjusted_edge = raw_edge * confidence

    return {
        "edge":           round(adjusted_edge, 4),
        "raw_edge":       round(raw_edge, 4),
        "model_prob":     round(model_prob, 4),
        "confidence":     round(confidence, 4),
        "tension":        round(tension, 4),
        "direction":      "YES" if adjusted_edge > 0 else "NO",
    }


# ══════════════════════════════════════════════════════════════════════════════
# REPLACEMENT 3: init_engine_db() — adds tension + deesc velocity columns
# ══════════════════════════════════════════════════════════════════════════════

ENGINE_DB = "convergence_engine.db"

def init_engine_db():
    conn = sqlite3.connect(ENGINE_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scores (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            computed_at         TEXT NOT NULL,
            escalation_raw      REAL,
            deescalation_raw    REAL,
            escalation_prob     REAL,
            deescalation_prob   REAL,
            tension             REAL,
            active_signal_count INTEGER,
            coherence_events    TEXT,
            divergence_flag     TEXT,
            dominant_signals    TEXT,
            velocity_24h        REAL,
            velocity_bonus      REAL,
            deesc_velocity_24h  REAL,
            deesc_velocity_bonus REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scores_time ON scores(computed_at)")

    # Migrate existing DBs — additive ALTER TABLEs are safe to retry
    for col_def in [
        "velocity_24h REAL",
        "velocity_bonus REAL",
        "tension REAL",
        "deesc_velocity_24h REAL",
        "deesc_velocity_bonus REAL",
    ]:
        try:
            conn.execute(f"ALTER TABLE scores ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass  # column already exists

    conn.commit()
    return conn


# ══════════════════════════════════════════════════════════════════════════════
# REPLACEMENT 4: save_score() — stores independent probs + tension
# ══════════════════════════════════════════════════════════════════════════════

def save_score(engine_conn, esc_raw, deesc_raw, signals, coherence_events, divergence,
               esc_velocity_24h=0.0, esc_velocity_bonus=0.0,
               deesc_velocity_24h=0.0, deesc_velocity_bonus=0.0):
    now = datetime.now(timezone.utc).isoformat()

    # Independent track probabilities (no longer sum to 1)
    esc_prob   = track_probability(esc_raw, "escalation", esc_velocity_bonus)
    deesc_prob = track_probability(deesc_raw, "deescalation", deesc_velocity_bonus)
    tension    = compute_tension(esc_prob, deesc_prob)

    top5 = sorted(signals, key=lambda s: s["score"], reverse=True)[:5]
    top5_json = json.dumps([{
        "type": s["type"],
        "region": s["region"],
        "score": round(s["score"], 2),
        "track": s["track"],
    } for s in top5])

    engine_conn.execute("""
        INSERT INTO scores
            (computed_at, escalation_raw, deescalation_raw,
             escalation_prob, deescalation_prob, tension,
             active_signal_count,
             coherence_events, divergence_flag, dominant_signals,
             velocity_24h, velocity_bonus,
             deesc_velocity_24h, deesc_velocity_bonus)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        now,
        round(esc_raw, 3),
        round(deesc_raw, 3),
        esc_prob,
        deesc_prob,
        tension,
        len(signals),
        json.dumps(coherence_events),
        divergence,
        top5_json,
        esc_velocity_24h,
        esc_velocity_bonus,
        deesc_velocity_24h,
        deesc_velocity_bonus,
    ))
    engine_conn.commit()


# ══════════════════════════════════════════════════════════════════════════════
# REPLACEMENT 5: compute() — bilateral velocity, independent probabilities
# ══════════════════════════════════════════════════════════════════════════════

# NOTE: This function references read_*() and calculate_scores() which are
# unchanged from v1. Only the scoring/output section is different.

def compute(verbose=True):
    import os

    ADSB_DB  = "adsb_events.db"
    NOTAM_DB = "notam_events.db"
    GDELT_DB = "gdelt_events.db"
    ROUTE_DB = "route_events.db"
    AIS_DB   = "ais_events.db"

    # Open all source DBs (read-only where possible)
    adsb_conn  = sqlite3.connect(f"file:{ADSB_DB}?mode=ro",  uri=True) if os.path.exists(ADSB_DB)  else None
    notam_conn = sqlite3.connect(f"file:{NOTAM_DB}?mode=ro", uri=True) if os.path.exists(NOTAM_DB) else None
    gdelt_conn = sqlite3.connect(f"file:{GDELT_DB}?mode=ro", uri=True) if os.path.exists(GDELT_DB) else None
    route_conn = sqlite3.connect(f"file:{ROUTE_DB}?mode=ro", uri=True) if os.path.exists(ROUTE_DB) else None
    ais_conn   = sqlite3.connect(f"file:{AIS_DB}?mode=ro",   uri=True) if os.path.exists(AIS_DB)   else None
    engine_conn = init_engine_db()

    signals = []

    if adsb_conn:
        signals += read_traffic_anomalies(adsb_conn)
        signals += read_vip_sightings(adsb_conn)
        signals += read_vip_dark(adsb_conn)
        signals += read_type_anomalies(adsb_conn)
        signals += read_bizjet_clusters(adsb_conn)
        adsb_conn.close()

    if notam_conn:
        signals += read_notam_anomalies(notam_conn)
        notam_conn.close()

    if route_conn:
        signals += read_route_suspensions(route_conn)
        route_conn.close()

    if ais_conn:
        signals += read_ais_anomalies(ais_conn)
        signals += read_spoofing_events(ais_conn)
        ais_conn.close()

    if gdelt_conn:
        signals += read_gdelt_signals(gdelt_conn)
        gdelt_conn.close()

    esc_raw, deesc_raw, coherence_events, divergence = calculate_scores(signals)

    # ── Bilateral velocity ────────────────────────────────────────────────
    esc_vel_24h,   esc_vel_bonus   = compute_velocity(engine_conn, esc_raw,   "escalation")
    deesc_vel_24h, deesc_vel_bonus = compute_velocity(engine_conn, deesc_raw, "deescalation")

    # ── Independent track probabilities ───────────────────────────────────
    esc_prob   = track_probability(esc_raw, "escalation", esc_vel_bonus)
    deesc_prob = track_probability(deesc_raw, "deescalation", deesc_vel_bonus)
    tension    = compute_tension(esc_prob, deesc_prob)

    save_score(engine_conn, esc_raw, deesc_raw, signals, coherence_events, divergence,
               esc_vel_24h, esc_vel_bonus, deesc_vel_24h, deesc_vel_bonus)
    engine_conn.close()

    if verbose:
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
        print(f"\nConvergence Engine — {now_str}")
        print("=" * 65)
        print(f"  Active signals:      {len(signals)}")
        print(f"  Escalation raw:      {esc_raw:.2f}")
        print(f"  De-escalation raw:   {deesc_raw:.2f}")

        if esc_vel_24h != 0.0:
            d = "↑" if esc_vel_24h > 0 else "↓"
            print(f"  Esc velocity (24h):  {esc_vel_24h:+.2f}  {d}  bonus={esc_vel_bonus:.2f}")
        if deesc_vel_24h != 0.0:
            d = "↑" if deesc_vel_24h > 0 else "↓"
            print(f"  Deesc velocity (24h):{deesc_vel_24h:+.2f}  {d}  bonus={deesc_vel_bonus:.2f}")

        print(f"  Escalation prob:     {esc_prob*100:.1f}%")
        print(f"  De-escalation prob:  {deesc_prob*100:.1f}%")
        print(f"  Tension:             {tension:.3f}", end="")
        if tension > 0.15:
            print("  ⚠ HIGH TENSION — conflicting signals, check divergence")
        else:
            print()

        if coherence_events:
            print(f"\n  Coherence multiplier active in {len(coherence_events)} region(s):")
            for ce in coherence_events:
                print(f"    {ce['region']}: {', '.join(ce['categories'])}  +{ce['bonus']:.1f} pts")
        if divergence:
            print(f"\n  *** {divergence} ***")

        top5 = sorted(signals, key=lambda s: s["score"], reverse=True)[:5]
        if top5:
            print(f"\n  Top signals:")
            for s in top5:
                print(f"    [{s['track'][:3].upper()}] {s['type']:<22} {s['region']:<12} score={s['score']:.2f}")

    return esc_raw, deesc_raw, esc_prob, deesc_prob, tension, signals, \
           esc_vel_24h, esc_vel_bonus, deesc_vel_24h, deesc_vel_bonus


# ══════════════════════════════════════════════════════════════════════════════
# REPLACEMENT 6: print_status() — shows tension column
# ══════════════════════════════════════════════════════════════════════════════

def print_status():
    import os
    if not os.path.exists(ENGINE_DB):
        print("No engine DB yet. Run without --status first.")
        return
    conn = sqlite3.connect(ENGINE_DB)
    rows = conn.execute("""
        SELECT computed_at, escalation_raw, deescalation_raw,
               escalation_prob, deescalation_prob,
               tension, active_signal_count, divergence_flag
        FROM scores
        ORDER BY computed_at DESC
        LIMIT 20
    """).fetchall()
    print(f"\nLast {len(rows)} convergence scores:")
    print(f"  {'Time':<20} {'Esc Raw':>8} {'Deesc':>6} {'Esc%':>6} {'Deesc%':>7} {'Tension':>8}  Sigs  Flag")
    print("  " + "-" * 78)
    for row in rows:
        computed_at, er, dr, ep, dp, tension, sigs, flag = row
        flag_str = " DIVERGE" if flag else ""
        t = tension or 0.0
        t_marker = " ⚠" if t > 0.15 else ""
        print(f"  {computed_at[:16]:<20} {er:>8.2f} {dr:>6.2f} {ep*100:>5.1f}% {dp*100:>6.1f}% {t:>7.3f}{t_marker}  {sigs:>4}{flag_str}")
    conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# REPLACEMENT 7: print_signals() — updated return signature
# ══════════════════════════════════════════════════════════════════════════════

def print_signals():
    """Print all active signals and their current decayed scores."""
    result = compute(verbose=False)
    # New return signature has 10 values; we only need signals (index 5)
    signals = result[5]
    if not signals:
        print("No active signals in the last 30 days.")
        return
    esc   = sorted([s for s in signals if s["track"] == "escalation"],   key=lambda s: -s["score"])
    deesc = sorted([s for s in signals if s["track"] == "deescalation"], key=lambda s: -s["score"])

    print(f"\nEscalation signals ({len(esc)}):")
    print(f"  {'Type':<22} {'Region':<14} {'Category':<18} {'S0':>5} {'Score':>7}  Class")
    print("  " + "-" * 72)
    for s in esc:
        print(f"  {s['type']:<22} {s['region']:<14} {s['category']:<18} {s['s0']:>5.1f} {s['score']:>7.2f}  {s['signal_class']}")

    print(f"\nDe-escalation signals ({len(deesc)}):")
    for s in deesc:
        print(f"  {s['type']:<22} {s['region']:<14} {s['category']:<18} {s['s0']:>5.1f} {s['score']:>7.2f}  {s['signal_class']}")
