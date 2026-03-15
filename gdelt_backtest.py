#!/usr/bin/env python3
"""
MarketSignal — GDELT Back-test (Goldstein average approach)

Instead of counting events with extreme Goldstein scores (which saturates),
this approach uses the AVERAGE Goldstein scale over a rolling 30-day window
compared against the prior 60-day baseline.

  delta = avg_goldstein_30d - avg_goldstein_baseline
  - Negative delta → Goldstein dropped → escalation signal
  - Positive delta → Goldstein rose   → de-escalation signal

Mirrors the new read_gdelt_signals() in convergence_engine.py exactly.

Usage:
    python3 gdelt_backtest.py              # full back-test, all events
    python3 gdelt_backtest.py --verbose    # include day-by-day tables
    python3 gdelt_backtest.py --calibrate  # calibration summary only
"""

import sqlite3
import math
import argparse
from datetime import datetime, date, timedelta

DB_PATH = "gdelt_events.db"

# ── Signal parameters (must match convergence_engine.py) ──────────────────────

S0_PER_UNIT   = 4.0    # score contribution per 1.0 unit of Goldstein delta
DELTA_FLOOR   = 0.3    # minimum delta to fire a signal (below this = noise)
BASELINE_DAYS = 60     # days to use as baseline (days 31–90 before eval date)
WINDOW_DAYS   = 30     # rolling signal window

# Sigmoid params (convergence_engine.py values)
SIGMOID_BETA  = 100.0
SIGMOID_ALPHA = 0.08

# ── Known historical events ────────────────────────────────────────────────────

KNOWN_EVENTS = [
    {
        "label":  "Hamas Oct 7 attack",
        "t0":     date(2023, 10, 7),
        "track":  "escalation",
        "notes":  "Goldstein should plunge — highest-intensity attack in dataset.",
    },
    {
        "label":  "Iranian drone/missile strike on Israel",
        "t0":     date(2024, 4, 14),
        "track":  "escalation",
        "notes":  "Goldstein should drop. IRN+ISR direct exchange.",
    },
    {
        "label":  "Houthi Red Sea escalation (US/UK airstrikes)",
        "t0":     date(2024, 1, 12),
        "track":  "escalation",
        "notes":  "Goldstein drop. YEM actors. Multi-country military response.",
    },
    {
        "label":  "Hezbollah opens northern front",
        "t0":     date(2023, 10, 8),
        "track":  "escalation",
        "notes":  "Goldstein drop — same window as Oct 7, compounds the signal.",
    },
    {
        "label":  "First Gaza ceasefire (Nov 2023)",
        "t0":     date(2023, 11, 22),
        "track":  "deescalation",
        "notes":  "Goldstein should rise. QAT/EGY/USA mediating.",
    },
    {
        "label":  "Lebanon ceasefire (Nov 2024)",
        "t0":     date(2024, 11, 27),
        "track":  "deescalation",
        "notes":  "Goldstein rise. ISR+LBN 60-day ceasefire.",
    },
    {
        "label":  "Gaza ceasefire agreement (Jan 2025)",
        "t0":     date(2025, 1, 15),
        "track":  "deescalation",
        "notes":  "Goldstein rise. Broad multi-party diplomatic agreement.",
    },
]

CHECKPOINTS = [30, 14, 7, 3, 1, 0]


# ── Math ───────────────────────────────────────────────────────────────────────

def to_probability(raw_score):
    return 1.0 / (1.0 + math.exp(-SIGMOID_ALPHA * (raw_score - SIGMOID_BETA)))


def gdelt_signal_at_date(conn, eval_date):
    """
    Compute Goldstein-average GDELT signal at a historical date.
    Returns dict with keys: avg_30d, avg_baseline, delta, esc_score, deesc_score.
    Mirrors convergence_engine.py read_gdelt_signals() logic exactly.
    """
    win_start  = (eval_date - timedelta(days=WINDOW_DAYS)).strftime("%Y%m%d")
    win_end    = eval_date.strftime("%Y%m%d")
    base_start = (eval_date - timedelta(days=WINDOW_DAYS + BASELINE_DAYS)).strftime("%Y%m%d")
    base_end   = (eval_date - timedelta(days=WINDOW_DAYS + 1)).strftime("%Y%m%d")

    row_win = conn.execute("""
        SELECT AVG(goldstein_scale), COUNT(*)
        FROM events
        WHERE event_date BETWEEN ? AND ?
    """, (win_start, win_end)).fetchone()

    row_base = conn.execute("""
        SELECT AVG(goldstein_scale), COUNT(*)
        FROM events
        WHERE event_date BETWEEN ? AND ?
    """, (base_start, base_end)).fetchone()

    avg_30d,     count_30d    = row_win
    avg_baseline, count_base  = row_base

    if not avg_30d or not avg_baseline or count_30d < 50 or count_base < 50:
        return None  # not enough data

    delta      = avg_30d - avg_baseline
    esc_score  = S0_PER_UNIT * max(0.0, -delta - DELTA_FLOOR)
    deesc_score = S0_PER_UNIT * max(0.0,  delta - DELTA_FLOOR)

    return {
        "avg_30d":      avg_30d,
        "avg_baseline": avg_baseline,
        "delta":        delta,
        "esc_score":    esc_score,
        "deesc_score":  deesc_score,
        "count_30d":    count_30d,
        "count_base":   count_base,
    }


# ── Sparkline ──────────────────────────────────────────────────────────────────

def sparkline(values, center=0.0):
    """ASCII sparkline. Values above center shown with up-blocks, below with down."""
    BLOCKS = " ▁▂▃▄▅▆▇█"
    if not values:
        return ""
    mn, mx = min(values), max(values)
    rng = (mx - mn) or 1.0
    return "".join(BLOCKS[min(int((v - mn) / rng * (len(BLOCKS) - 1)), len(BLOCKS) - 1)]
                   for v in values)


# ── Back-test runner ───────────────────────────────────────────────────────────

def analyse_event(conn, event):
    t0    = event["t0"]
    track = event["track"]

    # Compute signal at each checkpoint
    checkpoints = {}
    for cp in CHECKPOINTS:
        eval_date = t0 - timedelta(days=cp)
        sig = gdelt_signal_at_date(conn, eval_date)
        checkpoints[cp] = sig

    # Day-by-day for sparkline
    daily = {}
    for days_before in range(WINDOW_DAYS + 1):
        eval_date = t0 - timedelta(days=days_before)
        sig = gdelt_signal_at_date(conn, eval_date)
        if sig:
            daily[eval_date] = sig

    return {
        "label":       event["label"],
        "t0":          t0,
        "track":       track,
        "notes":       event["notes"],
        "checkpoints": checkpoints,
        "daily":       daily,
    }


def print_event_result(result, verbose=False):
    label  = result["label"]
    t0     = result["t0"]
    track  = result["track"]
    checks = result["checkpoints"]
    daily  = result["daily"]

    icon = "↑ ESC" if track == "escalation" else "↓ DEESC"
    score_key = "esc_score" if track == "escalation" else "deesc_score"

    print(f"\n{'=' * 74}")
    print(f"  {icon}  {label}")
    print(f"  Date: {t0}  |  {result['notes']}")
    print(f"{'=' * 74}")

    t0_sig = checks.get(0)
    if t0_sig:
        print(f"  At T-0: avg_30d={t0_sig['avg_30d']:.3f}  "
              f"baseline={t0_sig['avg_baseline']:.3f}  "
              f"delta={t0_sig['delta']:+.3f}  "
              f"count={t0_sig['count_30d']:,}")
    else:
        print("  T-0: insufficient data")

    print(f"\n  {'Day':<8}  {'avg 30d':>9}  {'baseline':>9}  {'delta':>8}  "
          f"{'Score':>8}  {'Prob':>8}  {'Signal?':>8}")
    print(f"  {'-' * 68}")

    for cp in CHECKPOINTS:
        sig = checks.get(cp)
        if not sig:
            print(f"  T-{cp:<5}   {'(no data)':>9}")
            continue
        score  = sig[score_key]
        prob   = to_probability(score) * 100
        fired  = "YES" if sig["delta"] < -DELTA_FLOOR or sig["delta"] > DELTA_FLOOR else "no"
        marker = "  ← event" if cp == 0 else ""
        print(f"  T-{cp:<5}   {sig['avg_30d']:>9.3f}  {sig['avg_baseline']:>9.3f}  "
              f"{sig['delta']:>+8.3f}  {score:>8.2f}  {prob:>7.1f}%  {fired:>8}{marker}")

    # Trend: did score rise into event?
    t7_sig = checks.get(7)
    t0_sig = checks.get(0)
    if t7_sig and t0_sig:
        s7 = t7_sig[score_key]
        s0 = t0_sig[score_key]
        if s7 > 0:
            rise = (s0 - s7) / s7 * 100
            print(f"\n  Trend T-7→T-0: score {s7:.2f} → {s0:.2f}  ({rise:+.0f}%)")
        elif s0 > 0:
            print(f"\n  Trend T-7→T-0: signal appeared at T-0 (was zero at T-7)")
        else:
            print(f"\n  Trend T-7→T-0: signal did not fire")

    # Goldstein average sparkline
    if daily:
        sorted_days = sorted(daily.keys())
        avgs = [daily[d]["avg_30d"] for d in sorted_days]
        deltas = [daily[d]["delta"] for d in sorted_days]
        print(f"  Goldstein avg (T-30→T-0, range {min(avgs):.2f}–{max(avgs):.2f}): "
              f"{sparkline(avgs)}")
        print(f"  Delta vs baseline (T-30→T-0, range {min(deltas):+.2f}–{max(deltas):+.2f}): "
              f"{sparkline(deltas)}")

    if verbose and daily:
        print(f"\n  Day-by-day:")
        print(f"  {'Date':<12}  {'avg_30d':>8}  {'baseline':>9}  {'delta':>8}  {'score':>8}")
        for d in sorted(daily.keys()):
            sig = daily[d]
            print(f"  {d}  {sig['avg_30d']:>8.3f}  {sig['avg_baseline']:>9.3f}  "
                  f"{sig['delta']:>+8.3f}  {sig[score_key]:>8.2f}")


# ── Calibration summary ────────────────────────────────────────────────────────

def print_calibration_summary(results):
    score_key_map = {"escalation": "esc_score", "deescalation": "deesc_score"}

    print(f"\n\n{'=' * 74}")
    print("  CALIBRATION SUMMARY")
    print(f"{'=' * 74}\n")
    print(f"  Signal design: avg_goldstein(30d) vs avg_goldstein(60d baseline)")
    print(f"  Score = S0_per_unit × max(0, |delta| − floor)  where S0={S0_PER_UNIT}, floor={DELTA_FLOOR}")
    print(f"  Current SIGMOID_BETA = {SIGMOID_BETA}")
    print()

    all_t0 = {"escalation": [], "deescalation": []}
    all_deltas = {"escalation": [], "deescalation": []}
    fired = {"escalation": 0, "deescalation": 0}
    total = {"escalation": 0, "deescalation": 0}

    for r in results:
        track = r["track"]
        sk    = score_key_map[track]
        t0_sig = r["checkpoints"].get(0)
        if not t0_sig:
            continue
        total[track] += 1
        all_t0[track].append(t0_sig[sk])
        all_deltas[track].append(t0_sig["delta"])
        if t0_sig[sk] > 0:
            fired[track] += 1

    for track in ["escalation", "deescalation"]:
        scores = all_t0[track]
        deltas = all_deltas[track]
        if not scores:
            continue
        avg_score = sum(scores) / len(scores)
        avg_delta = sum(deltas) / len(deltas)
        print(f"  {track.upper()} events ({total[track]} total, {fired[track]} fired signal at T-0):")
        print(f"    avg delta at T-0: {avg_delta:+.3f}  "
              f"(negative = more hostile, positive = more cooperative)")
        print(f"    avg score at T-0: {avg_score:.2f}")
        print(f"    P(T-0) with β={SIGMOID_BETA}: {to_probability(avg_score)*100:.1f}%")
        print()

    print("  ── Signal quality assessment ─────────────────────────────────────")
    print()

    esc_scores = all_t0["escalation"]
    deesc_scores = all_t0["deescalation"]

    if esc_scores and max(esc_scores) > 0:
        print(f"  Escalation: {fired['escalation']}/{total['escalation']} events fired signal at T-0")
        print(f"    Score range: {min(esc_scores):.2f} – {max(esc_scores):.2f}")
        print(f"    Delta range: {min(all_deltas['escalation']):+.3f} – "
              f"{max(all_deltas['escalation']):+.3f}")
    else:
        print("  Escalation: signal did not fire at T-0 for any event")

    if deesc_scores and max(deesc_scores) > 0:
        print(f"  De-escalation: {fired['deescalation']}/{total['deescalation']} events fired signal at T-0")
        print(f"    Score range: {min(deesc_scores):.2f} – {max(deesc_scores):.2f}")
        print(f"    Delta range: {min(all_deltas['deescalation']):+.3f} – "
              f"{max(all_deltas['deescalation']):+.3f}")
    else:
        print("  De-escalation: signal did not fire at T-0 for any event")

    print()
    print("  ── SIGMOID_BETA recommendation ───────────────────────────────────")
    print()

    all_scores = esc_scores + deesc_scores
    if all_scores:
        avg_all = sum(all_scores) / len(all_scores)
        # Beta at which avg score = 50% probability
        # We want P=50% when all layers combined = beta
        # GDELT is one of ~5 signal layers, typically contributing 10-25% of total score
        # So estimated full-system score ≈ GDELT score / 0.15
        estimated_full = avg_all / 0.15 if avg_all > 0 else None
        print(f"  Average GDELT-only score at T-0: {avg_all:.2f}")
        if estimated_full:
            print(f"  Estimated full-system score (GDELT = ~15% of total): ~{estimated_full:.0f}")
            print(f"  Suggested SIGMOID_BETA: {estimated_full:.0f}  "
                  f"(update convergence_engine.py)")
        else:
            print("  GDELT signal did not fire — cannot estimate SIGMOID_BETA from GDELT alone.")
            print("  Set SIGMOID_BETA based on ADS-B + NOTAM scores after 6+ months of data.")

    print()
    print("  ── Next steps ────────────────────────────────────────────────────")
    print()
    print("  1. If signal fired correctly above → apply parameter changes to")
    print("     convergence_engine.py (already done if you see this after the update)")
    print("  2. Re-run this script to confirm calibration")
    print("  3. After 6+ months of live data, add ADS-B/NOTAM validation here")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="MarketSignal GDELT back-test (Goldstein avg)")
    parser.add_argument("--verbose",   action="store_true", help="Day-by-day tables")
    parser.add_argument("--calibrate", action="store_true", help="Calibration summary only")
    args = parser.parse_args()

    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        print(f"Cannot open {DB_PATH}. Run gdelt_collector.py first.")
        return

    total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    if total == 0:
        print("Database is empty. Run gdelt_collector.py first.")
        conn.close()
        return

    date_range = conn.execute("SELECT MIN(event_date), MAX(event_date) FROM events").fetchone()
    print(f"\nGDELT Back-test — Goldstein Average Approach")
    print(f"Database: {total:,} events  |  {date_range[0]} → {date_range[1]}")
    print(f"Events to test: {len(KNOWN_EVENTS)}")
    print(f"Signal: avg_goldstein(30d) vs avg_goldstein(prior 60d)  "
          f"[S0={S0_PER_UNIT}/unit, floor=±{DELTA_FLOOR}]")

    results = []
    for event in KNOWN_EVENTS:
        result = analyse_event(conn, event)
        results.append(result)

    if not args.calibrate:
        for result in results:
            print_event_result(result, verbose=args.verbose)

    print_calibration_summary(results)
    conn.close()


if __name__ == "__main__":
    main()
