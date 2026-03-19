#!/usr/bin/env python3
"""
MarketSignal — Convergence Engine (Stage 3)

Reads all signal tables, applies time-decay math, and outputs one escalation
score + one de-escalation score every N minutes.

State vs Event logic:
  Events  — single occurrence (bizjet landing, VIP sighting): exponential decay
             from last_confirmed_at.
  States  — ongoing condition (ADS-B blackout, active NOTAM): sigmoid growth
             while active, exponential decay only after resolved.

Coherence multiplier (1.5×) fires only when BOTH participating signals score > 2.0
to prevent ghost signals from triggering it.

Score is sigmoid-normalised to 0–1 probability space for Polymarket comparison.

Lambda values are per-day rates from the original MarketSignal design doc.
S_0 initial weights are PLACEHOLDERS — must be calibrated via GDELT back-test.

Usage:
    python3 convergence_engine.py           # compute once, print scores
    python3 convergence_engine.py --loop    # continuous loop every 10 minutes
    python3 convergence_engine.py --status  # print last N scored records
    python3 convergence_engine.py --signals # print all active signals and scores
"""

import sqlite3
import json
import math
import os
import time
import argparse
from datetime import datetime, timezone, timedelta

# ── Config ─────────────────────────────────────────────────────────────────────

ADSB_DB       = "adsb_events.db"
NOTAM_DB      = "notam_events.db"
GDELT_DB      = "gdelt_events.db"
ROUTE_DB      = "route_events.db"
AIS_DB        = "ais_events.db"
ENGINE_DB     = "convergence_engine.db"

POLL_INTERVAL = 600          # 10 minutes
SIGNAL_WINDOW_DAYS = 30      # ignore signals older than this
COHERENCE_FLOOR = 2.0        # minimum score per signal before coherence fires

# ── Lambda values (per-day decay rates, Events only) ───────────────────────────
# ⚠ PLACEHOLDER — calibrate via GDELT back-test before trading
LAMBDAS = {
    "strategic_lift":  0.03,
    "tanker":          0.04,
    "isr_command":     0.06,
    "bizjet":          0.10,
    "route_suspension":0.12,
    "notam":           0.35,
    "going_dark":      0.60,
    # de-escalation signals
    "deesc_bizjet":    0.10,
    "deesc_notam_lift":0.35,
    "gdelt_deesc":     0.009,
    "gdelt_esc":       0.009,
    "ais_tanker":      0.04,
    "ais_military":    0.06,
    "ais_spoofing":    0.20,   # half-life ~3.5 days — significant but time-sensitive
}

# ── S_0 initial weights ────────────────────────────────────────────────────────
# ⚠ PLACEHOLDER — all values below are estimates, not back-tested.
# Run gdelt_backtest.py (not yet written) to replace these with conditional
# probabilities derived from 2.5M historical GDELT events.
S0 = {
    "traffic_drop_low":      3.0,
    "traffic_drop_medium":   6.0,
    "traffic_drop_high":    12.0,
    "vip_sighting":          5.0,   # diplomatic / strategic_lift VIP (Event)
    "going_dark":           15.0,
    "strategic_lift_medium": 8.0,
    "strategic_lift_high":  16.0,
    "tanker_medium":         7.0,
    "tanker_high":          14.0,
    "isr_medium":           10.0,   # ISR/BACN aircraft (State)
    "isr_high":             20.0,
    "command_medium":       12.0,   # E-4B / C-32B Gatekeeper (State)
    "command_high":         22.0,
    "bizjet_medium":         6.0,
    "bizjet_high":          12.0,
    "bizjet_cluster":       10.0,
    "notam_medium":          3.0,
    "notam_high":            5.0,
    "gdelt_escalation":      4.0,
    "gdelt_deescalation":    4.0,
    "ais_tanker_medium":     6.0,
    "ais_tanker_high":      12.0,
    "ais_military_high":    14.0,
    "ais_watchlist":         8.0,
    "ais_spoofing_low":      8.0,   # isolated event — could be technical
    "ais_spoofing_medium":  14.0,   # 2–4 events — likely deliberate
    "ais_spoofing_high":    20.0,   # 5+ events — active jamming/spoofing campaign
}

# Sigmoid normalisation parameters (β=midpoint, α=steepness)
# ⚠ PLACEHOLDER — β should equal historical average convergence score from back-test
SIGMOID_BETA  = 100.0  # escalation midpoint — raw score at P=50%. High because
                       # active-war conditions stack 6 signal layers simultaneously,
                       # easily reaching 150-200 pts. Correct at current conflict level.
DEESC_SIGMOID_BETA = 40.0  # de-escalation midpoint — lower because de-escalation signals
                            # are structurally fewer/gentler. Full ceasefire-negotiation
                            # scenario realistically peaks at ~60-70 pts (bizjet clusters +
                            # VIP shuttle + GDELT improvement). β=40 maps that to ~95%,
                            # and early signals (~14 pts) to ~10-15% — visible but not
                            # alarming. Calibrated Mar 2026.
SIGMOID_ALPHA = 0.08   # steepness (shared)

# Velocity (score acceleration) parameters
# A rising score gets a bonus proportional to its 24h rate of change.
# This means a score that goes 10→20→40 is treated more urgently than a static 40,
# which matches how intelligence analysts actually think about signal acceleration.
VELOCITY_WEIGHT     = 0.30   # bonus = velocity_24h * this weight (if positive)
VELOCITY_LOOKBACK_H = 24     # hours to look back for velocity comparison
VELOCITY_MAX_BONUS  = 30.0   # hard cap on velocity bonus to prevent runaway amplification

# Sigmoid growth parameters for States
STATE_L  = 2.0    # saturation ceiling multiplier on S_0
STATE_K  = 0.05   # growth rate per hour
STATE_X0 = 24.0   # inflection point (hours) — routine becomes significant


# ── Math ───────────────────────────────────────────────────────────────────────

def hours_elapsed(timestamp_str):
    """Return hours between a UTC ISO timestamp and now."""
    if not timestamp_str:
        return 0.0
    try:
        ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    except Exception:
        return 0.0


def days_elapsed(timestamp_str):
    return hours_elapsed(timestamp_str) / 24.0


def event_score(s0, signal_type, last_confirmed_at):
    """Exponential decay from last_confirmed_at."""
    lam = LAMBDAS.get(signal_type, 0.10)
    dt  = days_elapsed(last_confirmed_at)
    return s0 * math.exp(-lam * dt)


def state_score(s0, first_detected_at, signal_type, resolved_at=None):
    """
    Sigmoid growth while active; exponential decay after resolved.

    signal_type selects the decay lambda used after resolution — each state type
    should decay at its own characteristic rate once the condition clears:
      going_dark      λ=0.60  — reappearance closes the threat window quickly
      notam           λ=0.35  — lifted restrictions fade in days
      route_suspension λ=0.12 — airlines take time to resume routes
      isr_command     λ=0.06  — intelligence requirement doesn't vanish on landing
      strategic_lift  λ=0.03  — repositioned assets stay relevant for weeks

    Returns current weight.
    """
    duration_h = hours_elapsed(first_detected_at)
    lam        = LAMBDAS.get(signal_type, LAMBDAS["notam"])

    if resolved_at:
        # State cleared — grow to peak as if still active, then decay from resolution
        peak = s0 * STATE_L / (1 + math.exp(-STATE_K * (duration_h - STATE_X0)))
        dt   = days_elapsed(resolved_at)
        return peak * math.exp(-lam * dt)
    else:
        # Still active — sigmoid growth toward 2×S0 ceiling
        return s0 * STATE_L / (1 + math.exp(-STATE_K * (duration_h - STATE_X0)))


def track_probability(raw_score, track="escalation", velocity_bonus=0.0):
    """
    Independent sigmoid normalisation for a single track.

    Each track uses its own beta reflecting structural differences in signal volume:
      - Escalation  β=100: active war stacks 6 layers → 150-200 pts
      - De-escalation β=40: ceasefire scenario peaks ~60-70 pts

    Returns a 0–1 probability for this track alone. No coupling to the other track.
    "No signals" → (esc≈0%, deesc≈4%) which is clearly distinguishable from
    "conflicting signals" → (esc≈50%, deesc≈50%).
    """
    beta     = DEESC_SIGMOID_BETA if track == "deescalation" else SIGMOID_BETA
    adjusted = raw_score + velocity_bonus
    prob     = 1.0 / (1.0 + math.exp(-SIGMOID_ALPHA * (adjusted - beta)))
    return round(prob, 4)


def compute_tension(esc_prob, deesc_prob):
    """
    Tension metric: geometric mean of both probabilities.

    Returns 0–1:
      ~0.0 = at least one track is quiet (one-sided or no signals)
      ~1.0 = both tracks near 100% (maximum contradictory signals)

    High tension (>0.15) is the best indicator of mispriced Polymarket markets —
    when signals point both directions, crowds default to ~50/50 but the system
    can see which track has stronger physical corroboration.
    """
    return round(math.sqrt(max(0.0, esc_prob) * max(0.0, deesc_prob)), 4)


# ── Signal readers ─────────────────────────────────────────────────────────────

def _cutoff():
    return (datetime.now(timezone.utc) - timedelta(days=SIGNAL_WINDOW_DAYS)).isoformat()


def read_traffic_anomalies(adsb_conn):
    """Traffic drop anomalies — treated as States."""
    rows = adsb_conn.execute("""
        SELECT region, region_label, severity, detected_at,
               last_confirmed_at, resolved_at
        FROM anomalies
        WHERE detected_at > ?
        ORDER BY detected_at DESC
    """, (_cutoff(),)).fetchall()

    # One active signal per region (most recent)
    seen = {}
    signals = []
    for region, label, severity, detected_at, lca, resolved_at in rows:
        if region in seen:
            continue
        seen[region] = True
        s0 = S0.get(f"traffic_drop_{(severity or 'low').lower()}", S0["traffic_drop_medium"])
        score = state_score(s0, detected_at, "route_suspension", resolved_at)
        if score < 0.01:
            continue
        signals.append({
            "type": "traffic_drop",
            "signal_class": "state",
            "category": "route_suspension",
            "track": "escalation",
            "region": region,
            "region_label": label or region,
            "s0": s0,
            "score": score,
            "first_detected_at": detected_at,
            "last_confirmed_at": lca or detected_at,
            "resolved_at": resolved_at,
            "severity": severity,
        })
    return signals


def read_vip_sightings(adsb_conn):
    """
    VIP sightings — logic depends on aircraft category:

    State (sigmoid growth while active, decay after dark):
      isr     — E-11A BACN, RC-12: persistent airborne infrastructure
      command — E-4B Doomsday, C-32B Gatekeeper: elevated readiness posture

    Event (exponential decay from last sighting):
      diplomatic     — VIP state visits, royal transports
      strategic_lift — individual IL-76/C-17 sightings (type clustering handles the surge)
      everything else
    """
    STATE_CATEGORIES    = {"isr", "command"}
    ACTIVE_THRESHOLD_M  = 30   # minutes without a sighting → treat as resolved

    rows = adsb_conn.execute("""
        SELECT icao24, tail_number, operator, category, signal_value,
               region, region_label,
               MIN(detected_at) AS first_seen,
               MAX(detected_at) AS last_seen
        FROM vip_sightings
        WHERE detected_at > ?
        GROUP BY icao24, region
    """, (_cutoff(),)).fetchall()

    now = datetime.now(timezone.utc)
    seen = {}
    signals = []

    for icao, tail, operator, category, sig_val, region, label, first_seen, last_seen in rows:
        key = (icao, region)
        if key in seen:
            continue
        seen[key] = True

        if category in STATE_CATEGORIES:
            # ── State logic ────────────────────────────────────────────────
            try:
                last_dt = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
            except Exception:
                last_dt = now

            minutes_dark = (now - last_dt).total_seconds() / 60
            resolved_at  = last_seen if minutes_dark > ACTIVE_THRESHOLD_M else None

            sig   = (sig_val or "medium").lower()
            cat   = category  # "isr" or "command"
            s0    = S0.get(f"{cat}_{sig}", S0.get(f"{cat}_medium", S0["isr_medium"]))
            score = state_score(s0, first_seen, "isr_command", resolved_at)
            if score < 0.01:
                continue

            status = "active" if not resolved_at else f"dark {minutes_dark:.0f}m"
            signals.append({
                "type":             "vip_sighting",
                "signal_class":     "state",
                "category":         "isr_command",
                "track":            "escalation",
                "region":           region,
                "region_label":     label or region,
                "s0":               s0,
                "score":            score,
                "first_detected_at":first_seen,
                "last_confirmed_at":last_seen,
                "resolved_at":      resolved_at,
                "detail":           f"{tail} ({operator}) [{status}]",
            })

        else:
            # ── Event logic (diplomatic / strategic_lift / unknown) ────────
            s0    = S0["vip_sighting"]
            score = event_score(s0, "bizjet", last_seen)
            if score < 0.01:
                continue
            track = "deescalation" if category == "diplomatic" else "escalation"
            signals.append({
                "type":             "vip_sighting",
                "signal_class":     "event",
                "category":         "bizjet",
                "track":            track,
                "region":           region,
                "region_label":     label or region,
                "s0":               s0,
                "score":            score,
                "first_detected_at":first_seen,
                "last_confirmed_at":last_seen,
                "resolved_at":      None,
                "detail":           f"{tail} ({operator})",
            })

    return signals


def read_vip_dark(adsb_conn):
    """VIP going-dark events — treated as States (high urgency)."""
    rows = adsb_conn.execute("""
        SELECT icao24, tail_number, operator, detected_at,
               last_confirmed_at, resolved_at
        FROM vip_dark_events
        WHERE detected_at > ?
        ORDER BY detected_at DESC
    """, (_cutoff(),)).fetchall()

    seen = {}
    signals = []
    for icao, tail, operator, detected_at, lca, resolved_at in rows:
        if icao in seen:
            continue
        seen[icao] = True
        s0    = S0["going_dark"]
        score = state_score(s0, detected_at, "going_dark", resolved_at)
        if score < 0.01:
            continue
        signals.append({
            "type": "vip_dark",
            "signal_class": "state",
            "category": "going_dark",
            "track": "escalation",
            "region": "GLOBAL",
            "region_label": "Global",
            "s0": s0,
            "score": score,
            "first_detected_at": detected_at,
            "last_confirmed_at": lca or detected_at,
            "resolved_at": resolved_at,
            "detail": f"{tail} ({operator})",
        })
    return signals


def read_type_anomalies(adsb_conn):
    """Strategic type surges — Events (each detection is discrete)."""
    rows = adsb_conn.execute("""
        SELECT region, region_label, category, severity, detected_at,
               last_confirmed_at, resolved_at, sigma_above
        FROM type_anomalies
        WHERE detected_at > ?
        ORDER BY detected_at DESC
    """, (_cutoff(),)).fetchall()

    seen = {}
    signals = []
    for region, label, category, severity, detected_at, lca, resolved_at, sigma in rows:
        key = (region, category)
        if key in seen:
            continue
        seen[key] = True

        sev_key = (severity or "MEDIUM").upper()
        s0_key  = f"{category}_{sev_key.lower()}"
        s0      = S0.get(s0_key, S0.get(f"{category}_medium", 8.0))
        score   = event_score(s0, category, lca or detected_at)
        if score < 0.01:
            continue
        signals.append({
            "type": "type_surge",
            "signal_class": "event",
            "category": category,
            "track": "escalation",
            "region": region,
            "region_label": label or region,
            "s0": s0,
            "score": score,
            "first_detected_at": detected_at,
            "last_confirmed_at": lca or detected_at,
            "resolved_at": resolved_at,
            "severity": severity,
            "sigma": sigma,
        })
    return signals


def read_bizjet_clusters(adsb_conn):
    """Bizjet diplomatic clusters — Events, de-escalation track."""
    rows = adsb_conn.execute("""
        SELECT airport_name, airport_icao, bizjet_count, countries,
               detected_at, last_confirmed_at, resolved_at
        FROM bizjet_clusters
        WHERE detected_at > ?
        ORDER BY detected_at DESC
    """, (_cutoff(),)).fetchall()

    seen = {}
    signals = []
    for ap_name, ap_icao, count, countries, detected_at, lca, resolved_at in rows:
        if ap_icao in seen:
            continue
        seen[ap_icao] = True
        s0    = S0["bizjet_cluster"]
        score = event_score(s0, "deesc_bizjet", lca or detected_at)
        if score < 0.01:
            continue
        signals.append({
            "type": "bizjet_cluster",
            "signal_class": "event",
            "category": "deesc_bizjet",
            "track": "deescalation",
            "region": ap_icao or "UNKNOWN",
            "region_label": ap_name or ap_icao,
            "s0": s0,
            "score": score,
            "first_detected_at": detected_at,
            "last_confirmed_at": lca or detected_at,
            "resolved_at": resolved_at,
            "detail": f"{count} bizjets | {countries}",
        })
    return signals


def read_notam_anomalies(notam_conn):
    """Active airspace restrictions — States.

    One signal per FIR (location), taking the worst severity active in that FIR.
    Only counts NOTAMs from Middle East FIRs — the Cirium bounding box also returns
    FIRs from Romania, Russia, Greece, India etc. whose boundaries overlap the ME box.
    """
    ME_FIRS = {
        "OIIX",  # Iran Tehran
        "OIFM",  # Iran Esfahan
        "OEJD",  # Saudi Arabia Jeddah
        "OERK",  # Saudi Arabia Riyadh
        "OOMM",  # Oman Muscat
        "OMAE",  # UAE Emirates
        "HECC",  # Egypt Cairo
        "HECA",  # Egypt Cairo ACC
        "OTDF",  # Qatar Doha
        "LCCC",  # Cyprus Nicosia
        "OLBB",  # Lebanon Beirut
        "ORBB",  # Iraq Baghdad
        "LLLL",  # Israel Tel Aviv
        "OBBB",  # Bahrain
        "OKAC",  # Kuwait
        "OYSC",  # Yemen Sana'a
        "OJAI",  # Jordan Amman
        "OSTT",  # Syria Damascus
        "HESH",  # Egypt Sharm el-Sheikh
    }

    try:
        rows = notam_conn.execute("""
            SELECT notam_id, location, severity, detected_at,
                   last_confirmed_at, resolved_at
            FROM notam_anomalies
            WHERE detected_at > ?
            ORDER BY detected_at DESC
        """, (_cutoff(),)).fetchall()
    except sqlite3.OperationalError:
        return []  # table not yet created (NOTAM collector not yet activated)

    # Deduplicate by FIR (location), keep worst severity
    sev_rank = {"HIGH": 2, "MEDIUM": 1}
    fir_best = {}  # location → (severity, detected_at, lca, resolved_at, notam_id)
    for notam_id, location, severity, detected_at, lca, resolved_at in rows:
        fir = (location or "").upper()
        if fir not in ME_FIRS:
            continue
        cur = fir_best.get(fir)
        rank = sev_rank.get((severity or "MEDIUM").upper(), 1)
        if cur is None or rank > sev_rank.get(cur[0], 1):
            fir_best[fir] = (severity, detected_at, lca, resolved_at, notam_id)

    signals = []
    for fir, (severity, detected_at, lca, resolved_at, notam_id) in fir_best.items():
        sev_key = (severity or "MEDIUM").upper()
        s0      = S0.get(f"notam_{sev_key.lower()}", S0["notam_medium"])
        score   = state_score(s0, detected_at, "notam", resolved_at)
        if score < 0.01:
            continue
        signals.append({
            "type": "notam_restriction",
            "signal_class": "state",
            "category": "notam",
            "track": "escalation",
            "region": fir,
            "region_label": fir,
            "s0": s0,
            "score": score,
            "first_detected_at": detected_at,
            "last_confirmed_at": lca or detected_at,
            "resolved_at": resolved_at,
            "severity": severity,
            "detail": notam_id,
        })
    return signals


def read_route_suspensions(route_conn):
    """Route suspension signals — Events, escalation track (λ=0.12/day)."""
    try:
        rows = route_conn.execute("""
            SELECT dep, arr, airline_name, consecutive_days, drop_pct, severity,
                   first_detected_at, last_confirmed_at, resolved_at
            FROM route_suspensions
            WHERE first_detected_at > ?
            ORDER BY first_detected_at DESC
        """, (_cutoff(),)).fetchall()
    except sqlite3.OperationalError:
        return []

    seen = {}
    signals = []
    for dep, arr, airline_name, days, drop_pct, severity, first, lca, resolved_at in rows:
        key = (dep, arr, airline_name)
        if key in seen:
            continue
        seen[key] = True
        sev_key = (severity or "MEDIUM").lower()
        s0      = S0.get(f"traffic_drop_{sev_key}", S0["traffic_drop_medium"])
        score   = event_score(s0, "route_suspension", lca or first)
        if score < 0.01:
            continue
        signals.append({
            "type": "route_suspension",
            "signal_class": "event",
            "category": "route_suspension",
            "track": "escalation",
            "region": f"{dep}-{arr}",
            "region_label": f"{dep}-{arr}",
            "s0": s0,
            "score": score,
            "first_detected_at": first,
            "last_confirmed_at": lca or first,
            "resolved_at": resolved_at,
            "severity": severity,
            "detail": f"{airline_name} | {days}d | {drop_pct*100:.0f}% drop",
        })
    return signals


def read_ais_anomalies(ais_conn):
    """
    AIS maritime anomalies — Events.

    Two signal types:
      - tanker/cargo density drop  → escalation (vessels rerouting / avoiding)
      - military vessel surge      → escalation
    One signal per (region, category), most recent unresolved anomaly wins.
    Also emits a signal for each watchlist vessel sighting in the last 24h.
    """
    signals = []

    # ── Density anomalies ────────────────────────────────────────────────────
    try:
        rows = ais_conn.execute("""
            SELECT region, region_label, category, anomaly_type, severity,
                   detected_at, last_confirmed_at, resolved_at,
                   baseline_count, observed_count, drop_pct
            FROM vessel_anomalies
            WHERE detected_at > ?
            ORDER BY detected_at DESC
        """, (_cutoff(),)).fetchall()
    except sqlite3.OperationalError:
        rows = []

    seen = {}
    for region, region_label, category, anomaly_type, severity, \
            detected_at, lca, resolved_at, baseline, observed, drop_pct in rows:
        key = (region, category)
        if key in seen:
            continue
        seen[key] = True

        sev = (severity or "MEDIUM").upper()
        if category == "military":
            s0  = S0.get("ais_military_high", 14.0)
            lam = "ais_military"
        elif sev == "HIGH":
            s0  = S0.get("ais_tanker_high", 12.0)
            lam = "ais_tanker"
        else:
            s0  = S0.get("ais_tanker_medium", 6.0)
            lam = "ais_tanker"

        score = event_score(s0, lam, lca or detected_at)
        if score < 0.01:
            continue

        signals.append({
            "type":             "ais_anomaly",
            "signal_class":     "event",
            "category":         "maritime",
            "track":            "escalation",
            "region":           region,
            "region_label":     region_label or region,
            "s0":               s0,
            "score":            score,
            "first_detected_at": detected_at,
            "last_confirmed_at": lca or detected_at,
            "resolved_at":      resolved_at,
            "severity":         severity,
            "detail":           f"{category} | {anomaly_type} | {severity}",
        })

    # ── Watchlist sightings (last 24h) ───────────────────────────────────────
    try:
        cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        rows_w = ais_conn.execute("""
            SELECT mmsi, vessel_name, country, operator, region, region_label, detected_at
            FROM vessel_sightings
            WHERE detected_at > ?
              AND signal_value = 'WATCHLIST'
            ORDER BY detected_at DESC
        """, (cutoff_24h,)).fetchall()
    except sqlite3.OperationalError:
        rows_w = []

    seen_mmsi = {}
    for mmsi, name, country, operator, region, region_label, detected_at in rows_w:
        if mmsi in seen_mmsi:
            continue
        seen_mmsi[mmsi] = True
        s0    = S0.get("ais_watchlist", 8.0)
        score = event_score(s0, "ais_military", detected_at)
        if score < 0.01:
            continue
        signals.append({
            "type":             "ais_watchlist",
            "signal_class":     "event",
            "category":         "maritime",
            "track":            "escalation",
            "region":           region or "ME",
            "region_label":     region_label or region or "ME",
            "s0":               s0,
            "score":            score,
            "first_detected_at": detected_at,
            "last_confirmed_at": detected_at,
            "resolved_at":      None,
            "severity":         "HIGH",
            "detail":           f"{name or mmsi} | {operator or country}",
        })

    return signals


def read_spoofing_events(ais_conn):
    """
    GPS spoofing events — Events, escalation track.

    Groups by maritime region within the signal window. Tiers S_0 by cluster size:
      1 event   → LOW    (could be technical glitch)
      2–4 events → MEDIUM (likely deliberate)
      5+ events  → HIGH   (active jamming/spoofing campaign)

    Note: spoofing_events has lat/lon but no region field, so we do an inline
    region lookup mirroring ais_collector.py REGIONS.
    """
    # Inline region lookup (mirrors ais_collector.py REGIONS, Hormuz first so it
    # matches before the broader Persian Gulf box that contains it)
    AIS_REGIONS = [
        {"id": "hormuz",       "label": "Strait of Hormuz", "lat": (25.5, 27.0), "lon": (56.0, 58.0)},
        {"id": "persian_gulf", "label": "Persian Gulf",     "lat": (22.0, 30.0), "lon": (48.0, 60.0)},
        {"id": "red_sea",      "label": "Red Sea",          "lat": (12.0, 30.0), "lon": (32.0, 44.0)},
        {"id": "gulf_of_aden", "label": "Gulf of Aden",     "lat": (10.0, 14.0), "lon": (42.0, 52.0)},
        {"id": "arabian_sea",  "label": "Arabian Sea",      "lat": (10.0, 25.0), "lon": (55.0, 70.0)},
    ]

    def _spoof_region(lat, lon):
        if lat is None or lon is None:
            return "ME_MARITIME", "ME Maritime"
        for r in AIS_REGIONS:
            if r["lat"][0] <= lat <= r["lat"][1] and r["lon"][0] <= lon <= r["lon"][1]:
                return r["id"], r["label"]
        return "ME_MARITIME", "ME Maritime"

    try:
        rows = ais_conn.execute("""
            SELECT mmsi, vessel_name, lat, lon, anomaly_type, detail, detected_at
            FROM spoofing_events
            WHERE detected_at > ?
            ORDER BY detected_at DESC
        """, (_cutoff(),)).fetchall()
    except sqlite3.OperationalError:
        return []

    if not rows:
        return []

    from collections import defaultdict
    by_region = defaultdict(list)
    for mmsi, name, lat, lon, atype, detail, detected_at in rows:
        rid, rlabel = _spoof_region(lat, lon)
        by_region[(rid, rlabel)].append(detected_at)

    signals = []
    for (rid, rlabel), timestamps in by_region.items():
        count       = len(timestamps)
        most_recent = max(timestamps)
        first       = min(timestamps)

        if count >= 5:
            sev_key  = "ais_spoofing_high"
            severity = "HIGH"
        elif count >= 2:
            sev_key  = "ais_spoofing_medium"
            severity = "MEDIUM"
        else:
            sev_key  = "ais_spoofing_low"
            severity = "LOW"

        s0    = S0.get(sev_key, S0["ais_spoofing_medium"])
        score = event_score(s0, "ais_spoofing", most_recent)
        if score < 0.01:
            continue

        signals.append({
            "type":              "ais_spoofing",
            "signal_class":      "event",
            "category":          "maritime",
            "track":             "escalation",
            "region":            rid,
            "region_label":      rlabel,
            "s0":                s0,
            "score":             score,
            "first_detected_at": first,
            "last_confirmed_at": most_recent,
            "resolved_at":       None,
            "severity":          severity,
            "detail":            f"{count} spoofing event(s) in window",
        })

    return signals


def read_gdelt_signals(gdelt_conn):
    """
    GDELT Goldstein average signal.

    Computes the average Goldstein scale across all ME events in the last 30 days
    and compares it against the prior 60-day baseline. A drop in average (more
    negative) = escalation; a rise (more positive) = de-escalation.

    This is a State signal — it reflects current conditions without decay.
    Replaces the old count-based approach which permanently saturated.

    Note: S0["gdelt_escalation"] is now score-per-unit-of-Goldstein-delta,
    not a fixed initial weight.
    """
    today      = datetime.now(timezone.utc)
    win_start  = (today - timedelta(days=30)).strftime("%Y%m%d")
    win_end    = today.strftime("%Y%m%d")
    # Baseline sits at days 91–270 (≈3–9 months ago).
    # Starting at day 91 — not day 31 — means a sustained 3-month escalation cannot
    # contaminate the baseline and cause the delta to silently shrink toward zero.
    # The GDELT DB covers Jan 2023 onward, so this window is always populated.
    base_start = (today - timedelta(days=270)).strftime("%Y%m%d")
    base_end   = (today - timedelta(days=91)).strftime("%Y%m%d")

    DELTA_FLOOR = 0.30   # minimum delta to fire (below this = normal variation)

    try:
        row_win = gdelt_conn.execute("""
            SELECT AVG(goldstein_scale), COUNT(*)
            FROM events
            WHERE event_date BETWEEN ? AND ?
        """, (win_start, win_end)).fetchone()

        row_base = gdelt_conn.execute("""
            SELECT AVG(goldstein_scale), COUNT(*)
            FROM events
            WHERE event_date BETWEEN ? AND ?
        """, (base_start, base_end)).fetchone()

        avg_win,  count_win  = row_win
        avg_base, count_base = row_base

        if not avg_win or not avg_base or count_win < 50 or count_base < 50:
            return []

        delta   = avg_win - avg_base   # negative = more hostile, positive = more cooperative
        now_str = today.isoformat()
        signals = []

        if delta < -DELTA_FLOOR:
            score = S0["gdelt_escalation"] * (-delta - DELTA_FLOOR)
            signals.append({
                "type":             "gdelt_escalation",
                "signal_class":     "state",
                "category":         "gdelt_esc",
                "track":            "escalation",
                "region":           "ME",
                "region_label":     "GDELT Middle East",
                "s0":               S0["gdelt_escalation"],
                "score":            score,
                "first_detected_at": win_start,
                "last_confirmed_at": now_str,
                "resolved_at":      None,
                "detail": (f"avg_30d={avg_win:.3f}  baseline(91-270d)={avg_base:.3f}  "
                           f"Δ={delta:+.3f}  n={count_win:,}"),
            })

        if delta > DELTA_FLOOR:
            score = S0["gdelt_deescalation"] * (delta - DELTA_FLOOR)
            signals.append({
                "type":             "gdelt_deescalation",
                "signal_class":     "state",
                "category":         "gdelt_deesc",
                "track":            "deescalation",
                "region":           "ME",
                "region_label":     "GDELT Middle East",
                "s0":               S0["gdelt_deescalation"],
                "score":            score,
                "first_detected_at": win_start,
                "last_confirmed_at": now_str,
                "resolved_at":      None,
                "detail": (f"avg_30d={avg_win:.3f}  baseline(91-270d)={avg_base:.3f}  "
                           f"Δ={delta:+.3f}  n={count_win:,}"),
            })

        return signals

    except Exception as e:
        print(f"  [GDELT] Query error: {e}")
        return []


# ── Region → coherence zone mapping ────────────────────────────────────────────
# Maps the heterogeneous region strings used by each signal layer onto a common
# set of macro-zones so the coherence check can fire across layers.
#
# "ME" is a wildcard zone — signals in this zone (GDELT, going-dark) can cohere
# with any zone that already has a qualifying physical signal, but they cannot
# trigger coherence on their own (prevents GDELT alone from earning the bonus).

_ZONE_MAP = {
    # ADS-B traffic region labels
    "Israel / Palestine":   "LEVANT",
    "Lebanon / Syria":      "LEVANT",
    "Jordan":               "LEVANT",
    "Egypt / Sinai":        "EGYPT",
    "Iran":                 "IRAN",
    "Yemen / Red Sea":      "YEMEN_RED_SEA",
    "Persian Gulf / Qatar": "GULF",
    "Saudi Arabia":         "SAUDI",
    "Turkey":               "TURKEY",
    # AIS region IDs
    "persian_gulf":         "GULF",
    "hormuz":               "GULF",
    "red_sea":              "YEMEN_RED_SEA",
    "gulf_of_aden":         "YEMEN_RED_SEA",
    "arabian_sea":          "GULF",
    "ME_MARITIME":          "ME",
    # NOTAM FIR codes
    "LLLL": "LEVANT",        # Israel
    "OLBB": "LEVANT",        # Lebanon
    "OSTT": "LEVANT",        # Syria
    "OJAI": "LEVANT",        # Jordan
    "LCCC": "LEVANT",        # Cyprus
    "OIIX": "IRAN",          # Iran Tehran
    "OIFM": "IRAN",          # Iran Esfahan
    "OMAE": "GULF",          # UAE
    "OTDF": "GULF",          # Qatar
    "OBBB": "GULF",          # Bahrain
    "OKAC": "GULF",          # Kuwait
    "OOMM": "GULF",          # Oman
    "ORBB": "IRAQ",          # Iraq
    "OEJD": "SAUDI",         # Saudi Jeddah
    "OERK": "SAUDI",         # Saudi Riyadh
    "HECC": "EGYPT",         # Egypt Cairo
    "HECA": "EGYPT",         # Egypt Cairo ACC
    "HESH": "EGYPT",         # Egypt Sharm el-Sheikh
    "OYSC": "YEMEN_RED_SEA", # Yemen
    # Broad / global
    "ME":     "ME",
    "GLOBAL": "ME",
}

# Two-letter ICAO prefix → zone (catches airport codes in bizjet_cluster signals
# and any FIR codes not listed above)
_ICAO_PREFIX_ZONES = {
    "LL": "LEVANT",       # Israel
    "OL": "LEVANT",       # Lebanon
    "OS": "LEVANT",       # Syria
    "OJ": "LEVANT",       # Jordan
    "LC": "LEVANT",       # Cyprus
    "OI": "IRAN",
    "OM": "GULF",         # UAE
    "OT": "GULF",         # Qatar
    "OB": "GULF",         # Bahrain
    "OK": "GULF",         # Kuwait
    "OO": "GULF",         # Oman
    "OE": "SAUDI",
    "OR": "IRAQ",
    "HE": "EGYPT",
    "OY": "YEMEN_RED_SEA",
    "LT": "TURKEY",
}


def _coherence_zone(region):
    """Map a raw signal region string to a macro coherence zone."""
    zone = _ZONE_MAP.get(region)
    if zone:
        return zone
    # Try 2-char ICAO prefix (catches 4-char airport ICAO codes not in _ZONE_MAP)
    if len(region) >= 2:
        zone = _ICAO_PREFIX_ZONES.get(region[:2].upper())
        if zone:
            return zone
    return "ME"   # fallback: treat as broad ME signal


# ── Scoring ────────────────────────────────────────────────────────────────────

def calculate_scores(signals):
    """
    Sum decayed signal scores by track.
    Apply coherence multiplier (1.5×) per coherence zone where:
      - 2+ signals from different categories both score > COHERENCE_FLOOR
    Signals are normalised to macro zones via _coherence_zone() so that e.g. an
    ADS-B traffic drop in "Persian Gulf / Qatar" and an AIS tanker surge in
    "persian_gulf" both map to zone "GULF" and can cohere.

    "ME" wildcard signals (GDELT, going-dark) can participate in any zone that
    already has a qualifying physical signal, but cannot trigger coherence alone —
    this prevents GDELT from earning a bonus when there's no physical corroboration.

    Returns (escalation_raw, deescalation_raw, coherence_events).
    """
    esc_total   = 0.0
    deesc_total = 0.0

    esc_signals   = [s for s in signals if s["track"] == "escalation"]
    deesc_signals = [s for s in signals if s["track"] == "deescalation"]

    # Base sums
    for s in esc_signals:
        esc_total += s["score"]
    for s in deesc_signals:
        deesc_total += s["score"]

    # Coherence multiplier — group escalation signals by coherence zone
    coherence_events = []
    from collections import defaultdict

    # Split ME-wildcard signals from zone-specific signals
    me_qualifying   = [s for s in esc_signals
                       if _coherence_zone(s["region"]) == "ME" and s["score"] > COHERENCE_FLOOR]
    zonal_signals   = [s for s in esc_signals if _coherence_zone(s["region"]) != "ME"]

    by_zone = defaultdict(list)
    for s in zonal_signals:
        by_zone[_coherence_zone(s["region"])].append(s)

    # Inject ME wildcards into every zone that has at least one qualifying zonal signal.
    # They contribute their category to the coherence check but NOT to the bonus score
    # (they are already counted in esc_total).
    for zone, zone_sigs in by_zone.items():
        has_qualifying_zonal = any(s["score"] > COHERENCE_FLOOR for s in zone_sigs)
        if has_qualifying_zonal and me_qualifying:
            by_zone[zone] = zone_sigs + me_qualifying

    bonus = 0.0
    for zone, zone_signals in by_zone.items():
        qualifying  = [s for s in zone_signals if s["score"] > COHERENCE_FLOOR]
        categories  = {s["category"] for s in qualifying}
        if len(categories) >= 2:
            # Bonus is applied only to zone-specific signal scores to avoid
            # double-counting ME signals that are already in esc_total
            zonal_qualifying = [s for s in qualifying if _coherence_zone(s["region"]) != "ME"]
            zone_score  = sum(s["score"] for s in zonal_qualifying)
            zone_bonus  = zone_score * 0.5   # 1.5× = original + 0.5×
            bonus += zone_bonus
            coherence_events.append({
                "region":             zone,
                "categories":         sorted(categories),
                "qualifying_signals": len(qualifying),
                "bonus":              round(zone_bonus, 2),
            })

    esc_total += bonus

    # Divergence: if GDELT shows de-escalation but ADS-B shows escalation
    # flag as narrative incoherence (informational only, no score impact here)
    gdelt_esc   = sum(s["score"] for s in esc_signals   if s["type"] == "gdelt_escalation")
    gdelt_deesc = sum(s["score"] for s in deesc_signals if s["type"] == "gdelt_deescalation")
    adsb_esc    = sum(s["score"] for s in esc_signals   if s["type"] in ("traffic_drop", "type_surge"))

    divergence = None
    if gdelt_deesc > gdelt_esc and adsb_esc > 5.0:
        divergence = "NARRATIVE_INCOHERENCE: GDELT de-escalating, ADS-B escalating — potential alpha signal"
    elif gdelt_esc > gdelt_deesc and deesc_total > esc_total:
        divergence = "NARRATIVE_INCOHERENCE: GDELT escalating, physical signals de-escalating"

    return esc_total, deesc_total, coherence_events, divergence


# ── Output DB ──────────────────────────────────────────────────────────────────

def init_engine_db():
    conn = sqlite3.connect(ENGINE_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scores (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            computed_at          TEXT NOT NULL,
            escalation_raw       REAL,
            deescalation_raw     REAL,
            escalation_prob      REAL,
            deescalation_prob    REAL,
            tension              REAL,
            active_signal_count  INTEGER,
            coherence_events     TEXT,
            divergence_flag      TEXT,
            dominant_signals     TEXT,
            velocity_24h         REAL,
            velocity_bonus       REAL,
            deesc_velocity_24h   REAL,
            deesc_velocity_bonus REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scores_time ON scores(computed_at)")
    # Additive migrations — safe to retry
    for col_def in [
        "velocity_24h REAL", "velocity_bonus REAL",
        "tension REAL", "deesc_velocity_24h REAL", "deesc_velocity_bonus REAL",
    ]:
        try:
            conn.execute(f"ALTER TABLE scores ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    return conn


def compute_velocity(engine_conn, current_raw, track="escalation"):
    """
    Compare current raw score against ~24h ago for the given track.

    Returns (velocity_24h, velocity_bonus).
    A ±2h window handles polling gaps. Returns (0.0, 0.0) if insufficient history.
    """
    col = "escalation_raw" if track == "escalation" else "deescalation_raw"
    lo  = (datetime.now(timezone.utc) - timedelta(hours=VELOCITY_LOOKBACK_H + 2)).isoformat()
    hi  = (datetime.now(timezone.utc) - timedelta(hours=VELOCITY_LOOKBACK_H - 2)).isoformat()

    row = engine_conn.execute(f"""
        SELECT {col} FROM scores
        WHERE computed_at BETWEEN ? AND ?
        ORDER BY computed_at DESC
        LIMIT 1
    """, (lo, hi)).fetchone()

    if row is None:
        return 0.0, 0.0

    velocity_24h   = current_raw - row[0]
    velocity_bonus = min(max(0.0, velocity_24h) * VELOCITY_WEIGHT, VELOCITY_MAX_BONUS)
    return round(velocity_24h, 3), round(velocity_bonus, 3)


def save_score(engine_conn, esc_raw, deesc_raw, signals, coherence_events, divergence,
               esc_velocity_24h=0.0, esc_velocity_bonus=0.0,
               deesc_velocity_24h=0.0, deesc_velocity_bonus=0.0):
    now        = datetime.now(timezone.utc).isoformat()
    esc_prob   = track_probability(esc_raw,   "escalation",   esc_velocity_bonus)
    deesc_prob = track_probability(deesc_raw, "deescalation", deesc_velocity_bonus)
    tension    = compute_tension(esc_prob, deesc_prob)

    top5 = sorted(signals, key=lambda s: s["score"], reverse=True)[:5]
    top5_json = json.dumps([{
        "type": s["type"], "region": s["region"],
        "score": round(s["score"], 2), "track": s["track"],
    } for s in top5])

    engine_conn.execute("""
        INSERT INTO scores
            (computed_at, escalation_raw, deescalation_raw,
             escalation_prob, deescalation_prob, tension,
             active_signal_count, coherence_events, divergence_flag, dominant_signals,
             velocity_24h, velocity_bonus, deesc_velocity_24h, deesc_velocity_bonus)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        now,
        round(esc_raw, 3), round(deesc_raw, 3),
        esc_prob, deesc_prob, tension,
        len(signals),
        json.dumps(coherence_events), divergence, top5_json,
        esc_velocity_24h, esc_velocity_bonus,
        deesc_velocity_24h, deesc_velocity_bonus,
    ))
    engine_conn.commit()


# ── Main compute ───────────────────────────────────────────────────────────────

def compute(verbose=True):
    # Open all source DBs (read-only where possible)
    adsb_conn   = sqlite3.connect(f"file:{ADSB_DB}?mode=ro",   uri=True) if os.path.exists(ADSB_DB)   else None
    notam_conn  = sqlite3.connect(f"file:{NOTAM_DB}?mode=ro",  uri=True) if os.path.exists(NOTAM_DB)  else None
    gdelt_conn  = sqlite3.connect(f"file:{GDELT_DB}?mode=ro",  uri=True) if os.path.exists(GDELT_DB)  else None
    route_conn  = sqlite3.connect(f"file:{ROUTE_DB}?mode=ro",  uri=True) if os.path.exists(ROUTE_DB)  else None
    ais_conn    = sqlite3.connect(f"file:{AIS_DB}?mode=ro",    uri=True) if os.path.exists(AIS_DB)    else None
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

    # Bilateral velocity — each track gets its own urgency bonus
    esc_vel_24h,   esc_vel_bonus   = compute_velocity(engine_conn, esc_raw,   "escalation")
    deesc_vel_24h, deesc_vel_bonus = compute_velocity(engine_conn, deesc_raw, "deescalation")

    # Independent track probabilities (no longer sum to 1)
    esc_prob   = track_probability(esc_raw,   "escalation",   esc_vel_bonus)
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


def print_status():
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
    for computed_at, er, dr, ep, dp, tension, sigs, flag in rows:
        flag_str = " DIVERGE" if flag else ""
        t = tension or 0.0
        t_marker = " ⚠" if t > 0.15 else ""
        print(f"  {computed_at[:16]:<20} {er:>8.2f} {dr:>6.2f} {ep*100:>5.1f}% {dp*100:>6.1f}% {t:>7.3f}{t_marker}  {sigs:>4}{flag_str}")
    conn.close()


def print_signals():
    """Print all active signals and their current decayed scores."""
    result  = compute(verbose=False)
    signals = result[5]  # index 5 in new 10-value return signature
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


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop",    action="store_true", help="Run continuously every 10 minutes")
    parser.add_argument("--status",  action="store_true", help="Print last 20 scored records")
    parser.add_argument("--signals", action="store_true", help="Print all active signals and scores")
    args = parser.parse_args()

    if args.status:
        print_status()
        return

    if args.signals:
        print_signals()
        return

    if args.loop:
        print(f"Convergence engine running every {POLL_INTERVAL // 60} minutes. Ctrl+C to stop.")
        while True:
            try:
                compute()
                print(f"  Sleeping {POLL_INTERVAL // 60}m...")
                time.sleep(POLL_INTERVAL)
            except KeyboardInterrupt:
                print("\nStopped.")
                break
    else:
        compute()


if __name__ == "__main__":
    main()
