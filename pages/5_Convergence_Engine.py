#!/usr/bin/env python3
"""
MarketSignal — Convergence Engine dashboard page
Shows the aggregated escalation/de-escalation score across all signal layers.
"""

import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
import sqlite3
import json
import sys
import os
from datetime import datetime, timezone, timedelta

st.set_page_config(
    page_title="MarketSignal — Convergence Engine",
    layout="wide",
    initial_sidebar_state="collapsed",
)

ENGINE_DB = "convergence_engine.db"

# Add project root to path so we can import convergence_engine
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Data loading ───────────────────────────────────────────────────────────────

def _conn():
    return sqlite3.connect(ENGINE_DB, check_same_thread=False)


@st.cache_data(ttl=120, show_spinner=False)
def load_latest_score():
    try:
        with _conn() as conn:
            row = conn.execute("""
                SELECT computed_at, escalation_raw, deescalation_raw,
                       escalation_prob, deescalation_prob,
                       active_signal_count, coherence_events,
                       divergence_flag, dominant_signals
                FROM scores
                ORDER BY computed_at DESC
                LIMIT 1
            """).fetchone()
        return row
    except Exception:
        return None


@st.cache_data(ttl=120, show_spinner=False)
def load_score_history(hours=72):
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with _conn() as conn:
            df = pd.read_sql_query("""
                SELECT computed_at, escalation_raw, deescalation_raw,
                       escalation_prob, deescalation_prob, active_signal_count
                FROM scores
                WHERE computed_at >= ?
                ORDER BY computed_at ASC
            """, conn, params=(cutoff,))
        if not df.empty:
            df["computed_at"] = pd.to_datetime(df["computed_at"])
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=600, show_spinner="Computing live signal breakdown...")
def load_live_signals():
    """Run compute() to get full active signal list. Cached 10 min."""
    try:
        import convergence_engine as ce
        _, _, _, _, signals = ce.compute(verbose=False)
        return signals
    except Exception as e:
        return None


@st.cache_data(ttl=120, show_spinner=False)
def load_db_meta():
    try:
        with _conn() as conn:
            total    = conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
            earliest = conn.execute("SELECT MIN(computed_at) FROM scores").fetchone()[0]
        return total, earliest
    except Exception:
        return 0, None


# ── Charts ─────────────────────────────────────────────────────────────────────

def chart_score_history(df):
    fig = make_subplots(specs=[[{"secondary_y": True}]])

    fig.add_trace(go.Scatter(
        x=df["computed_at"], y=(df["escalation_prob"] * 100).round(1),
        name="Escalation %",
        mode="lines",
        line=dict(color="#ef4444", width=2),
        hovertemplate="%{x|%H:%M}<br>Escalation: %{y:.1f}%<extra></extra>",
    ), secondary_y=False)

    fig.add_trace(go.Scatter(
        x=df["computed_at"], y=(df["deescalation_prob"] * 100).round(1),
        name="De-escalation %",
        mode="lines",
        line=dict(color="#22c55e", width=2),
        hovertemplate="%{x|%H:%M}<br>De-escalation: %{y:.1f}%<extra></extra>",
    ), secondary_y=False)

    fig.add_trace(go.Bar(
        x=df["computed_at"], y=df["active_signal_count"],
        name="Active signals",
        marker_color="rgba(148,163,184,0.2)",
        hovertemplate="%{x|%H:%M}<br>Signals: %{y}<extra></extra>",
    ), secondary_y=True)

    fig.add_hline(y=50, line_dash="dot", line_color="rgba(255,255,255,0.2)", secondary_y=False)

    fig.update_layout(
        template="plotly_dark",
        height=320,
        margin=dict(l=0, r=0, t=10, b=0),
        legend=dict(orientation="h", y=1.12),
        hovermode="x unified",
        yaxis=dict(title="Probability %", range=[0, 100],
                   gridcolor="rgba(255,255,255,0.05)"),
        yaxis2=dict(title="Signal count", gridcolor="rgba(0,0,0,0)"),
    )
    return fig


def chart_raw_scores(df):
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["computed_at"], y=df["escalation_raw"].round(2),
        name="Escalation raw",
        mode="lines",
        line=dict(color="#ef4444", width=2),
        fill="tozeroy",
        fillcolor="rgba(239,68,68,0.1)",
        hovertemplate="%{x|%H:%M}<br>Raw score: %{y:.2f}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=df["computed_at"], y=df["deescalation_raw"].round(2),
        name="De-escalation raw",
        mode="lines",
        line=dict(color="#22c55e", width=2),
        fill="tozeroy",
        fillcolor="rgba(34,197,94,0.1)",
        hovertemplate="%{x|%H:%M}<br>Raw score: %{y:.2f}<extra></extra>",
    ))
    fig.update_layout(
        template="plotly_dark",
        height=220,
        margin=dict(l=0, r=0, t=10, b=0),
        legend=dict(orientation="h", y=1.12),
        hovermode="x unified",
        yaxis=dict(title="Raw score", gridcolor="rgba(255,255,255,0.05)"),
        xaxis=dict(gridcolor="rgba(255,255,255,0.05)"),
    )
    return fig


SIGNAL_TYPE_LABELS = {
    "traffic_drop":       "ADS-B Traffic Drop",
    "vip_sighting":       "VIP Aircraft",
    "vip_dark":           "VIP Going Dark",
    "type_surge":         "Strategic Type Surge",
    "bizjet_cluster":     "Bizjet Cluster",
    "notam_restriction":  "NOTAM Restriction",
    "route_suspension":   "Route Suspension",
    "ais_anomaly":        "Maritime Density Anomaly",
    "ais_watchlist":      "Maritime Watchlist Vessel",
    "gdelt_escalation":   "GDELT Escalation",
    "gdelt_deescalation": "GDELT De-escalation",
}

SIGNAL_CLASS_COLORS = {
    "state": "#f97316",   # orange — persistent, growing
    "event": "#6366f1",   # indigo — decaying
}

TRACK_COLORS = {
    "escalation":   "#ef4444",
    "deescalation": "#22c55e",
}


# ── Page ───────────────────────────────────────────────────────────────────────

st.markdown("## Convergence Engine")
st.caption("Aggregated escalation / de-escalation score across all signal layers")

# Check DB
if not os.path.exists(ENGINE_DB):
    st.error("No convergence engine data yet. Run `python3 convergence_engine.py --loop` to start.")
    st.stop()

total_scores, earliest = load_db_meta()
latest = load_latest_score()

if not latest:
    st.info("Engine is running but no scores yet. Check back in a moment.")
    st.stop()

computed_at, esc_raw, deesc_raw, esc_prob, deesc_prob, sig_count, \
    coherence_json, divergence_flag, dominant_json = latest

computed_str = computed_at[:16] if computed_at else "—"
now_str      = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

st.caption(f"Last computed: {computed_str} UTC  |  Page loaded: {now_str}  |  {total_scores:,} scores in DB")

if st.button("Refresh", use_container_width=False):
    st.cache_data.clear()
    st.rerun()

# ── Divergence banner ──────────────────────────────────────────────────────────
if divergence_flag:
    st.warning(f"DIVERGENCE DETECTED: {divergence_flag}", icon="⚠️")

st.divider()

# ── Big probability display ────────────────────────────────────────────────────
st.subheader("Current Score")
st.caption("⚠ UNCALIBRATED — probabilities are placeholder until GDELT back-test is complete")

p1, p2, p3, p4 = st.columns(4)

esc_pct   = round(esc_prob * 100, 1)
deesc_pct = round(deesc_prob * 100, 1)

p1.metric("Escalation probability",    f"{esc_pct}%",
          help="Sigmoid-normalised. Meaningless until β is calibrated via back-test.")
p2.metric("De-escalation probability", f"{deesc_pct}%",
          help="Sigmoid-normalised. Meaningless until β is calibrated via back-test.")
p3.metric("Escalation raw score",      f"{esc_raw:.2f}",
          help="Sum of all decayed escalation signal weights.")
p4.metric("Active signals",            sig_count,
          help="Number of signals with score > 0.01 in the 30-day window.")

# Visual probability bars
col_esc, col_deesc = st.columns(2)
with col_esc:
    bar_color = "#ef4444" if esc_pct > 60 else "#f97316" if esc_pct > 40 else "#94a3b8"
    st.markdown(
        f'<div style="background:rgba(255,255,255,0.05);border-radius:6px;overflow:hidden;height:28px">'
        f'<div style="width:{esc_pct}%;background:{bar_color};height:100%;'
        f'display:flex;align-items:center;padding-left:10px;font-weight:600;font-size:13px">'
        f'Escalation {esc_pct}%</div></div>',
        unsafe_allow_html=True,
    )
with col_deesc:
    st.markdown(
        f'<div style="background:rgba(255,255,255,0.05);border-radius:6px;overflow:hidden;height:28px">'
        f'<div style="width:{deesc_pct}%;background:#22c55e;height:100%;'
        f'display:flex;align-items:center;padding-left:10px;font-weight:600;font-size:13px">'
        f'De-escalation {deesc_pct}%</div></div>',
        unsafe_allow_html=True,
    )

st.divider()

# ── Score history ──────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## MarketSignal")
    st.caption("Convergence Engine")
    st.divider()
    history_hours = st.selectbox("History window", [12, 24, 48, 72, 168], index=2,
                                 format_func=lambda h: f"Last {h}h" if h < 168 else "Last 7 days")
    st.divider()
    st.caption(f"Scores computed: {total_scores:,}")
    st.caption(f"Since: {earliest[:16] if earliest else '—'}")

history_df = load_score_history(history_hours)

st.subheader(f"Score history — last {history_hours}h")
if history_df.empty:
    st.info("Not enough history yet.")
else:
    st.plotly_chart(chart_score_history(history_df), use_container_width=True)

    with st.expander("Raw scores (pre-sigmoid)"):
        st.plotly_chart(chart_raw_scores(history_df), use_container_width=True)
        st.caption(
            "Raw score = sum of all decayed signal weights. "
            "The sigmoid converts this to a probability using β=100 (placeholder midpoint)."
        )

st.divider()

# ── Active signals breakdown ───────────────────────────────────────────────────
st.subheader("Active signal breakdown")
st.caption("Live computation — shows all signals currently contributing to the score")

signals = load_live_signals()

if signals is None:
    # Fallback: show top 5 from stored dominant_signals JSON
    st.warning("Could not run live computation. Showing top 5 stored signals from last run.")
    try:
        top5 = json.loads(dominant_json or "[]")
        if top5:
            df_top5 = pd.DataFrame(top5)
            st.dataframe(df_top5, hide_index=True, use_container_width=True)
    except Exception:
        st.info("No signal data available.")
elif not signals:
    st.info("No active signals in the last 30 days.")
else:
    esc_signals   = sorted([s for s in signals if s["track"] == "escalation"],
                           key=lambda s: -s["score"])
    deesc_signals = sorted([s for s in signals if s["track"] == "deescalation"],
                           key=lambda s: -s["score"])

    def signals_to_df(sigs):
        return pd.DataFrame([{
            "Type":         SIGNAL_TYPE_LABELS.get(s["type"], s["type"]),
            "Region":       s.get("region_label", s["region"]),
            "Class":        s["signal_class"],
            "S₀":           round(s["s0"], 1),
            "Score":        round(s["score"], 2),
            "First seen":   s.get("first_detected_at", "")[:16],
            "Last confirmed":s.get("last_confirmed_at", "")[:16],
            "Detail":       s.get("detail", ""),
        } for s in sigs])

    if esc_signals:
        st.markdown("**Escalation signals**")
        df_esc = signals_to_df(esc_signals)
        st.dataframe(
            df_esc,
            column_config={
                "Score": st.column_config.ProgressColumn(
                    "Score", min_value=0,
                    max_value=max(r["Score"] for r in df_esc.to_dict("records")) or 1,
                    format="%.2f",
                ),
                "Class": st.column_config.TextColumn("State/Event"),
            },
            hide_index=True,
            use_container_width=True,
        )

    if deesc_signals:
        st.markdown("**De-escalation signals**")
        df_deesc = signals_to_df(deesc_signals)
        st.dataframe(
            df_deesc,
            column_config={
                "Score": st.column_config.ProgressColumn(
                    "Score", min_value=0,
                    max_value=max(r["Score"] for r in df_deesc.to_dict("records")) or 1,
                    format="%.2f",
                ),
                "Class": st.column_config.TextColumn("State/Event"),
            },
            hide_index=True,
            use_container_width=True,
        )

st.divider()

# ── Coherence events ───────────────────────────────────────────────────────────
st.subheader("Coherence multiplier")
try:
    coherence = json.loads(coherence_json or "[]")
except Exception:
    coherence = []

if not coherence:
    st.info(
        "Coherence multiplier (1.5×) not currently active. "
        "Fires when 2+ signal categories both score >2.0 in the same region simultaneously."
    )
else:
    st.success(f"1.5× coherence multiplier active in {len(coherence)} region(s)")
    for ce in coherence:
        st.markdown(
            f'<div style="border-left:3px solid #22c55e;padding:8px 12px;'
            f'background:rgba(34,197,94,0.05);border-radius:3px;margin-bottom:8px">'
            f'<strong>{ce["region"]}</strong> — '
            f'{" + ".join(ce["categories"])}  '
            f'<span style="color:#22c55e">+{ce["bonus"]:.1f} pts bonus</span>'
            f'</div>',
            unsafe_allow_html=True,
        )

st.divider()

# ── Calibration note ───────────────────────────────────────────────────────────
with st.expander("How the score is calculated"):
    st.markdown("""
**Every signal fires with an initial weight S₀** based on how historically significant that signal
type is. A Doomsday plane airborne (S₀=22) outweighs a diplomatic bizjet landing (S₀=5).

**One-off events** decay exponentially from the moment they were last confirmed:
> Score = S₀ × e^(−λt)

where λ is the per-day decay rate and t is days since last confirmed.
Fast-decaying signals (NOTAM: λ=0.35, going dark: λ=0.60) become irrelevant in days.
Slow signals (strategic lift: λ=0.03) stay relevant for weeks.

**Persistent states** (ISR aircraft continuously airborne, active NOTAMs, ADS-B blackouts)
use sigmoid growth while active — the longer the condition holds, the higher the score,
up to a saturation ceiling of 2× S₀:
> Score = S₀ × 2 / (1 + e^(−0.05 × (hours − 24)))

Once the condition clears, it switches to exponential decay from the peak it reached.

**Coherence bonus (1.5×):** if 2+ signal categories both score >2.0 in the same geographic
region simultaneously, the regional subtotal gets a 50% bonus. One signal could be coincidence.
Two independent signal types in the same place at the same time is harder to explain away.

**Sigmoid normalisation:** the raw sum converts to 0–100% via:
> P = 1 / (1 + e^(−0.08 × (score − 100)))

⚠ The midpoint β=100 is a rough calibration based on expected live signal stack size.
It will be refined once 6+ months of real multi-layer data can be compared against known
historical events. The GDELT back-test validates signal direction (correct 6/7 events)
but cannot set β because GDELT alone contributes only ~1–5 pts of the total score.
""")
