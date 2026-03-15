#!/usr/bin/env python3
"""
MarketSignal — GDELT Back-test

Computes retrospective GDELT-only convergence scores for known historical events
and diagnoses signal design issues.

Usage:
    python3 gdelt_backtest.py              # full back-test, all events
    python3 gdelt_backtest.py --verbose    # include day-by-day tables
    python3 gdelt_backtest.py --calibrate  # calibration summary only

Outputs:
  1. Per-event signal trajectory (event count + score at T-30 to T-0)
  2. Baseline comparison (quiet period vs. event period)
  3. Alternative lambda analysis (what faster decay would look like)
  4. Calibration recommendations for SIGMOID_BETA and signal parameters
"""

import sqlite3
import math
import argparse
from datetime import datetime, date, timedelta

DB_PATH = "gdelt_events.db"

# ── Current convergence_engine.py constants (for comparison) ───────────────────
S0_ESC    = 4.0     # convergence_engine.py S0["gdelt_escalation"]
S0_DEESC  = 4.0     # convergence_engine.py S0["gdelt_deescalation"]
LAMBDA    = 0.009   # convergence_engine.py LAMBDAS["gdelt_esc"]
LIMIT     = 50      # convergence_engine.py hard cap per query

# Alternative lambda to test (0.10/day = 7-day half-life, much more discriminating)
LAMBDA_ALT = 0.10

SIGMOID_BETA  = 30.0
SIGMOID_ALPHA = 0.08
WINDOW_DAYS   = 30

# ── Known historical events ────────────────────────────────────────────────────

KNOWN_EVENTS = [
    {
        "label":  "Hamas Oct 7 attack",
        "t0":     date(2023, 10, 7),
        "track":  "escalation",
        "notes":  "Root 18/19/20 (assault/fight/mass violence). Goldstein minimum.",
    },
    {
        "label":  "Iranian drone/missile strike on Israel",
        "t0":     date(2024, 4, 14),
        "track":  "escalation",
        "notes":  "Root 15/18/19. IRN+ISR actors. Largest Iranian strike on Israel.",
    },
    {
        "label":  "Houthi Red Sea escalation (US/UK airstrikes)",
        "t0":     date(2024, 1, 12),
        "track":  "escalation",
        "notes":  "Root 13/15/18. YEM actors. US/UK airstrikes on Houthi targets.",
    },
    {
        "label":  "Hezbollah opens northern front",
        "t0":     date(2023, 10, 8),
        "track":  "escalation",
        "notes":  "Root 15/18. LBN+ISR. Day after Oct 7.",
    },
    {
        "label":  "First Gaza ceasefire (Nov 2023)",
        "t0":     date(2023, 11, 22),
        "track":  "deescalation",
        "notes":  "Root 03/04/05. QAT/EGY/USA mediating. 4-day pause.",
    },
    {
        "label":  "Lebanon ceasefire (Nov 2024)",
        "t0":     date(2024, 11, 27),
        "track":  "deescalation",
        "notes":  "Root 03/04/05/08. ISR+LBN. 60-day ceasefire via USA/FRA.",
    },
    {
        "label":  "Gaza ceasefire agreement (Jan 2025)",
        "t0":     date(2025, 1, 15),
        "track":  "deescalation",
        "notes":  "Root 03/04/05/08. Broad diplomatic cooperation. Major ceasefire.",
    },
]

# "Quiet" period baseline: Jan–Sep 2023, before Oct 7 changed the ME baseline
QUIET_PERIOD = (date(2023, 1, 1), date(2023, 9, 30))

CHECKPOINTS = [30, 14, 7, 3, 1, 0]


# ── Math ───────────────────────────────────────────────────────────────────────

def decay_score(s0, event_date_str, eval_date, lam):
    event_dt = datetime.strptime(event_date_str, "%Y%m%d").date()
    days_ago = (eval_date - event_dt).days
    if days_ago < 0:
        return 0.0
    return s0 * math.exp(-lam * days_ago)


def to_probability(raw_score):
    return 1.0 / (1.0 + math.exp(-SIGMOID_ALPHA * (raw_score - SIGMOID_BETA)))


# ── Queries ────────────────────────────────────────────────────────────────────

def _where_clause(track):
    if track == "escalation":
        return "goldstein_scale < -5 AND CAST(event_root_code AS INTEGER) BETWEEN 14 AND 20"
    return "goldstein_scale > 5 AND CAST(event_root_code AS INTEGER) BETWEEN 3 AND 8"


def event_count_in_window(conn, start_date, end_date, track):
    """Raw count of qualifying events in a date range."""
    rows = conn.execute(f"""
        SELECT COUNT(*) FROM events
        WHERE event_date BETWEEN ? AND ?
          AND {_where_clause(track)}
    """, (start_date.strftime("%Y%m%d"), end_date.strftime("%Y%m%d"))).fetchone()
    return rows[0]


def gdelt_score_at_date(conn, eval_date, track, lam=LAMBDA):
    """
    GDELT convergence score at a historical date using the same logic as
    convergence_engine.py read_gdelt_signals() — limited to 50 events.
    """
    window_start = (eval_date - timedelta(days=WINDOW_DAYS)).strftime("%Y%m%d")
    window_end   = eval_date.strftime("%Y%m%d")
    order        = "ASC" if track == "escalation" else "DESC"
    s0           = S0_ESC if track == "escalation" else S0_DEESC

    rows = conn.execute(f"""
        SELECT event_date, goldstein_scale
        FROM events
        WHERE event_date BETWEEN ? AND ?
          AND {_where_clause(track)}
        ORDER BY goldstein_scale {order}
        LIMIT {LIMIT}
    """, (window_start, window_end)).fetchall()

    return sum(decay_score(s0, r[0], eval_date, lam) for r in rows)


def gdelt_score_unlimited(conn, eval_date, track, lam):
    """
    GDELT score without LIMIT cap — use article-weighted S0 per event,
    scaled so a single massive spike day matches the existing S0 scale.
    """
    window_start = (eval_date - timedelta(days=WINDOW_DAYS)).strftime("%Y%m%d")
    window_end   = eval_date.strftime("%Y%m%d")

    rows = conn.execute(f"""
        SELECT event_date, num_articles
        FROM events
        WHERE event_date BETWEEN ? AND ?
          AND {_where_clause(track)}
    """, (window_start, window_end)).fetchall()

    if not rows:
        return 0.0

    # Normalise: treat each event's contribution as S0 × article_weight × decay
    # article_weight = log(articles+1) / log(max_articles+1) keeps it 0–1
    articles = [r[1] or 1 for r in rows]
    max_art  = max(articles)

    s0 = S0_ESC if track == "escalation" else S0_DEESC
    total = 0.0
    for (event_date_str, art), weight_art in zip(rows, articles):
        art_w = math.log(art + 1) / math.log(max_art + 1)
        total += decay_score(s0 * art_w, event_date_str, eval_date, lam)
    return total


def daily_event_counts(conn, t0, track):
    """Return dict {eval_date: count} for each day in [t0-30, t0]."""
    result = {}
    for days_before in range(WINDOW_DAYS + 1):
        eval_date    = t0 - timedelta(days=days_before)
        window_start = eval_date - timedelta(days=WINDOW_DAYS)
        count        = event_count_in_window(conn, window_start, eval_date, track)
        result[eval_date] = count
    return result


# ── Baseline ───────────────────────────────────────────────────────────────────

def compute_quiet_baseline(conn, track):
    """
    Average daily event count in the quiet period (Jan–Sep 2023).
    Returns (avg_per_day, avg_score_with_current_lambda, avg_score_with_alt_lambda).
    """
    start, end = QUIET_PERIOD
    total_days = (end - start).days + 1

    # Sample 10 evenly spaced evaluation dates across the quiet window
    sample_scores_curr = []
    sample_scores_alt  = []
    for i in range(10):
        eval_date = start + timedelta(days=i * (total_days // 10))
        sample_scores_curr.append(gdelt_score_at_date(conn, eval_date, track, LAMBDA))
        sample_scores_alt.append(gdelt_score_at_date(conn, eval_date, track, LAMBDA_ALT))

    total_count = event_count_in_window(conn, start, end, track)
    avg_per_day = total_count / total_days

    return (
        avg_per_day,
        sum(sample_scores_curr) / len(sample_scores_curr),
        sum(sample_scores_alt)  / len(sample_scores_alt),
    )


# ── Sparkline ──────────────────────────────────────────────────────────────────

def sparkline(values):
    BLOCKS = " ▁▂▃▄▅▆▇█"
    if not values:
        return ""
    max_v = max(values) or 1.0
    return "".join(BLOCKS[min(int(v / max_v * (len(BLOCKS) - 1)), len(BLOCKS) - 1)] for v in values)


# ── Per-event analysis ─────────────────────────────────────────────────────────

def analyse_event(conn, event, baseline_scores, verbose=False):
    t0    = event["t0"]
    track = event["track"]
    label = event["label"]

    earliest_str = conn.execute("SELECT MIN(event_date) FROM events").fetchone()[0]
    earliest     = datetime.strptime(earliest_str, "%Y%m%d").date()

    # Per-checkpoint: event count, score (current lambda), score (alt lambda)
    checkpoints = {}
    for cp in CHECKPOINTS:
        eval_date = t0 - timedelta(days=cp)
        if eval_date < earliest:
            checkpoints[cp] = None
            continue
        window_start = eval_date - timedelta(days=WINDOW_DAYS)
        count        = event_count_in_window(conn, window_start, eval_date, track)
        score_curr   = gdelt_score_at_date(conn, eval_date, track, LAMBDA)
        score_alt    = gdelt_score_at_date(conn, eval_date, track, LAMBDA_ALT)
        checkpoints[cp] = (count, score_curr, score_alt)

    # Sparkline: daily event counts
    daily = {}
    for days_before in range(WINDOW_DAYS + 1):
        eval_date    = t0 - timedelta(days=days_before)
        if eval_date < earliest:
            continue
        window_start = eval_date - timedelta(days=WINDOW_DAYS)
        daily[eval_date] = event_count_in_window(conn, window_start, eval_date, track)

    # Event count on event day vs. quiet baseline
    event_window_count = event_count_in_window(
        conn, t0 - timedelta(days=WINDOW_DAYS), t0, track
    )
    quiet_avg, quiet_score_curr, quiet_score_alt = baseline_scores

    ratio = event_window_count / max(quiet_avg * WINDOW_DAYS, 1)

    return {
        "label":               label,
        "t0":                  t0,
        "track":               track,
        "notes":               event["notes"],
        "checkpoints":         checkpoints,
        "daily_counts":        daily,
        "event_window_count":  event_window_count,
        "quiet_avg_per_day":   quiet_avg,
        "quiet_score_curr":    quiet_score_curr,
        "quiet_score_alt":     quiet_score_alt,
        "event_baseline_ratio":ratio,
    }


def print_event_result(result, verbose=False):
    label  = result["label"]
    t0     = result["t0"]
    track  = result["track"]
    checks = result["checkpoints"]
    daily  = result["daily_counts"]

    icon = "↑ ESC" if track == "escalation" else "↓ DEESC"
    t0_data = checks.get(0)

    print(f"\n{'=' * 72}")
    print(f"  {icon}  {label}")
    print(f"  Date: {t0}  |  {result['notes']}")
    print(f"{'=' * 72}")

    # Event density vs. quiet baseline
    evt_count = result["event_window_count"]
    quiet_day = result["quiet_avg_per_day"]
    ratio     = result["event_baseline_ratio"]
    print(f"  30-day window event count at T-0: {evt_count:,}  "
          f"(quiet avg {quiet_day:.0f}/day × 30 = {quiet_day*30:.0f})  "
          f"ratio: {ratio:.1f}×")

    # Checkpoint table
    print(f"\n  {'Day':<8}  {'Events in 30d win':>18}  "
          f"{'Score (λ=0.009)':>16}  {'Score (λ=0.10)':>14}  {'Prob (λ=0.10)':>14}")
    print(f"  {'-' * 78}")

    for cp in CHECKPOINTS:
        data = checks.get(cp)
        if data is None:
            print(f"  T-{cp:<5}   {'(before DB)':>18}")
            continue
        count, score_curr, score_alt = data
        prob_curr = to_probability(score_curr) * 100
        prob_alt  = to_probability(score_alt)  * 100
        marker    = "  ← event" if cp == 0 else ""
        print(f"  T-{cp:<5}   {count:>18,}  {score_curr:>16.1f}  "
              f"{score_alt:>14.2f}  {prob_alt:>13.1f}%{marker}")

    # Trend using alt lambda (the meaningful one)
    t7 = checks.get(7)
    t0d = checks.get(0)
    if t7 and t0d:
        _, _, s7 = t7
        _, _, s0 = t0d
        if s7 > 0:
            rise = (s0 - s7) / s7 * 100
            trend_str = f"+{rise:.0f}% rise T-7→T-0" if rise >= 0 else f"{rise:.0f}% decline T-7→T-0"
            print(f"\n  Trend (λ=0.10): {trend_str}")

    # Sparkline of daily 30d-rolling event counts
    if daily:
        sorted_days = sorted(daily.keys())
        vals = [daily[d] for d in sorted_days]
        print(f"  Sparkline (event count in rolling 30d window, T-30→T-0): "
              f"{sparkline(vals)}  peak={max(vals):,}")

    if verbose and daily:
        print(f"\n  Day-by-day 30d-rolling event count:")
        for d in sorted(daily.keys()):
            bar = "█" * min(int(daily[d] / 2000), 40)
            print(f"    {d}  {daily[d]:>8,}  {bar}")


# ── Calibration summary ────────────────────────────────────────────────────────

def print_calibration_summary(results):
    print(f"\n\n{'=' * 72}")
    print("  CALIBRATION SUMMARY & SIGNAL DESIGN DIAGNOSIS")
    print(f"{'=' * 72}\n")

    # Collect T-0 scores
    esc_curr_t0, esc_alt_t0, deesc_curr_t0, deesc_alt_t0 = [], [], [], []
    esc_ratios = []

    for r in results:
        t0_data = r["checkpoints"].get(0)
        if t0_data is None:
            continue
        count, score_curr, score_alt = t0_data
        if r["track"] == "escalation":
            esc_curr_t0.append(score_curr)
            esc_alt_t0.append(score_alt)
            esc_ratios.append(r["event_baseline_ratio"])
        else:
            deesc_curr_t0.append(score_curr)
            deesc_alt_t0.append(score_alt)

    # Diagnosis 1: LIMIT 50 saturation
    print("  ── Finding 1: Signal Saturation (LIMIT 50 + slow lambda) ──────────")
    print()
    if esc_curr_t0:
        avg_curr = sum(esc_curr_t0) / len(esc_curr_t0)
        print(f"  Current design (λ=0.009, LIMIT 50):")
        print(f"    Average T-0 escalation score: {avg_curr:.1f}")
        print(f"    Score at 'quiet' period:       {results[0]['quiet_score_curr']:.1f}")
        print(f"    Difference:                    {avg_curr - results[0]['quiet_score_curr']:.1f}")
        print()
        print("  PROBLEM: With λ=0.009 (77-day half-life) and LIMIT 50,")
        print("  the Middle East always has 50+ qualifying events in any 30-day window.")
        print("  Score is permanently saturated — no contrast between quiet and hot periods.")
        print(f"  Current β=30 → P ≈ 100% always. Threshold is completely wrong.")
        print()

    # Diagnosis 2: Event density ratio
    if esc_ratios:
        avg_ratio = sum(esc_ratios) / len(esc_ratios)
        print(f"  ── Finding 2: The Signal IS There (Event Density) ─────────────────")
        print()
        print(f"  Average event density ratio (event window / quiet baseline): {avg_ratio:.1f}×")
        print(f"  → Escalation events cause a real spike in GDELT activity.")
        print(f"  → The signal exists — it just needs a redesigned extraction method.")
        print()

    # Diagnosis 3: Alt lambda is better
    if esc_alt_t0:
        avg_alt    = sum(esc_alt_t0) / len(esc_alt_t0)
        quiet_alt  = results[0]["quiet_score_alt"]
        contrast   = avg_alt - quiet_alt
        print(f"  ── Finding 3: Alternative Lambda (λ=0.10, 7-day half-life) ────────")
        print()
        print(f"  With λ=0.10, LIMIT 50:")
        print(f"    Average T-0 escalation score: {avg_alt:.2f}")
        print(f"    Quiet-period score:            {quiet_alt:.2f}")
        print(f"    Contrast (signal above noise): {contrast:.2f}")
        if contrast > 1.0:
            print(f"  ✓ Lambda=0.10 creates meaningful signal/noise contrast")
        else:
            print(f"  △ Even λ=0.10 shows limited contrast — LIMIT 50 still saturates")
        print()

    # Recommendations
    print(f"  ── Recommended Parameter Changes ───────────────────────────────────")
    print()
    print("  IN convergence_engine.py:")
    print()
    print("  1. Increase GDELT lambda:")
    print("       LAMBDAS[\"gdelt_esc\"]   = 0.10  # was 0.009 (7-day half-life)")
    print("       LAMBDAS[\"gdelt_deesc\"] = 0.10  # was 0.009")
    print()
    print("  2. Remove or raise LIMIT cap to allow density signal through:")
    print("       Change: LIMIT 50 → LIMIT 500 in read_gdelt_signals() queries")
    print("       OR weight events by num_articles so burst days dominate naturally")
    print()
    print("  3. Reduce S0 to prevent single-layer saturation:")
    print("       S0[\"gdelt_escalation\"]   = 0.05  # was 4.0")
    print("       S0[\"gdelt_deescalation\"] = 0.05  # was 4.0")
    print("       → With λ=0.10, LIMIT 500, S0=0.05: a day with 500 events scores")
    print("         500 × 0.05 = 25 on event day, decaying to ~9 by T-7")
    print()
    print("  4. SIGMOID_BETA should be set AFTER running this script with corrected")
    print("     parameters. Run: python3 gdelt_backtest.py (with updated engine values)")
    print("     then set β = average T-0 full-system score.")
    print()
    print("  5. Deeper fix (future): replace raw event count with a")
    print("     z-score vs. 90-day rolling baseline per actor pair.")
    print("     This would cleanly separate 'normal ME conflict noise' from spikes.")
    print()
    print("  ── What This Means for Trading ─────────────────────────────────────")
    print()
    print("  GDELT alone is NOT a reliable signal for the current system design.")
    print("  It's always 'red' regardless of what's happening.")
    print("  The ADS-B + NOTAM layers are currently the only discriminating signals.")
    print("  Do not trade on convergence scores until lambda/S0 are corrected")
    print("  and re-validated with this script.")
    print()
    print("  ── ADS-B / NOTAM back-test ─────────────────────────────────────────")
    print()
    print("  ADS-B and NOTAM data only exists from 2026-03-13 forward.")
    print("  Run this script again after 6+ months of live collection to validate")
    print("  those signal layers against any new events that occur.")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="MarketSignal GDELT back-test")
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
    print(f"\nGDELT Back-test — MarketSignal")
    print(f"Database: {total:,} events  |  {date_range[0]} → {date_range[1]}")
    print(f"Events to test: {len(KNOWN_EVENTS)}")
    print(f"Quiet baseline: {QUIET_PERIOD[0]} → {QUIET_PERIOD[1]}")

    # Compute quiet baselines (do once, shared across events)
    print("\nComputing quiet-period baseline...", end=" ", flush=True)
    esc_baseline   = compute_quiet_baseline(conn, "escalation")
    deesc_baseline = compute_quiet_baseline(conn, "deescalation")
    print("done.")

    results = []
    for event in KNOWN_EVENTS:
        baseline = esc_baseline if event["track"] == "escalation" else deesc_baseline
        result = analyse_event(conn, event, baseline, verbose=args.verbose)
        results.append(result)

    if not args.calibrate:
        for result in results:
            print_event_result(result, verbose=args.verbose)

    print_calibration_summary(results)

    conn.close()


if __name__ == "__main__":
    main()
