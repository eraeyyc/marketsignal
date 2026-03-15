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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.styles import inject_css, page_header, plotly_layout, axis_style
inject_css()

ENGINE_DB = "convergence_engine.db"
POLY_DB   = "polymarket_markets.db"

# Add project root to path so we can import convergence_engine (already done above)


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
                       divergence_flag, dominant_signals,
                       COALESCE(velocity_24h, 0.0),
                       COALESCE(velocity_bonus, 0.0)
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
                       escalation_prob, deescalation_prob, active_signal_count,
                       COALESCE(velocity_24h, 0.0) AS velocity_24h,
                       COALESCE(velocity_bonus, 0.0) AS velocity_bonus
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
        _, _, _, _, signals, _, _ = ce.compute(verbose=False)
        return signals
    except Exception as e:
        return None


@st.cache_data(ttl=300, show_spinner=False)
def load_polymarket_markets():
    """Returns DataFrame of active ME markets from polymarket_markets.db."""
    try:
        with sqlite3.connect(POLY_DB) as conn:
            df = pd.read_sql_query("""
                SELECT question, slug, yes_price, no_price,
                       volume, signal_track, end_date, last_updated
                FROM markets
                WHERE active = 1
                ORDER BY volume DESC
            """, conn)
        return df
    except Exception:
        return None


@st.cache_data(ttl=300, show_spinner=False)
def load_polymarket_last_polled():
    """Returns ISO string of most recent last_updated, or None."""
    try:
        with sqlite3.connect(POLY_DB) as conn:
            row = conn.execute("SELECT MAX(last_updated) FROM markets").fetchone()
        return row[0] if row else None
    except Exception:
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

    fig.update_layout(**plotly_layout(
        height=320,
        yaxis=dict(title="Probability %", range=[0, 100], **axis_style()),
        yaxis2=dict(title="Signal count", gridcolor="rgba(0,0,0,0)", **axis_style()),
    ))
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
    fig.update_layout(**plotly_layout(
        height=220,
        yaxis=dict(title="Raw score", **axis_style()),
        xaxis=axis_style(),
    ))
    return fig


def chart_probability_gauge(esc_pct, deesc_pct):
    """Horizontal bar chart showing escalation vs de-escalation probability."""
    esc_color = "#ef4444" if esc_pct > 60 else "#f97316" if esc_pct > 40 else "#94a3b8"
    fig = go.Figure()
    fig.add_trace(go.Bar(
        name="De-escalation",
        x=[deesc_pct], y=["De-escalation"],
        orientation="h",
        marker=dict(color="#22c55e", opacity=0.85),
        width=0.45,
        hovertemplate="De-escalation: %{x:.1f}%<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        name="Escalation",
        x=[esc_pct], y=["Escalation"],
        orientation="h",
        marker=dict(color=esc_color, opacity=0.85),
        width=0.45,
        hovertemplate="Escalation: %{x:.1f}%<extra></extra>",
    ))
    fig.add_vline(x=50, line_dash="dot", line_color="rgba(255,255,255,0.2)", line_width=1)
    fig.update_layout(
        **plotly_layout(height=130),
        xaxis=dict(range=[0, 100], ticksuffix="%", **axis_style()),
        yaxis=axis_style(),
        barmode="group",
        showlegend=False,
        annotations=[
            dict(x=min(esc_pct + 1, 99), y="Escalation",
                 text=f"<b>{esc_pct}%</b>", showarrow=False,
                 xanchor="left", font=dict(color=esc_color, size=14)),
            dict(x=min(deesc_pct + 1, 99), y="De-escalation",
                 text=f"<b>{deesc_pct}%</b>", showarrow=False,
                 xanchor="left", font=dict(color="#22c55e", size=14)),
        ],
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
    "ais_spoofing":       "GPS Spoofing / AIS Jamming",
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

page_header(
    "Convergence Engine",
    "Aggregated escalation / de-escalation score across all signal layers",
)

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
    coherence_json, divergence_flag, dominant_json, velocity_24h, velocity_bonus = latest

computed_str = computed_at[:16] if computed_at else "—"
now_str      = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

st.caption(f"Last computed: {computed_str} UTC  ·  {total_scores:,} scores in DB")

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

p1, p2, p3, p4, p5 = st.columns(5)

esc_pct   = round(esc_prob * 100, 1)
deesc_pct = round(deesc_prob * 100, 1)

velocity_arrow = "↑" if velocity_24h > 0 else ("↓" if velocity_24h < 0 else "→")
velocity_delta = f"{velocity_24h:+.1f} pts" if velocity_24h != 0 else "no history"

p1.metric("Escalation probability",    f"{esc_pct}%",
          help="Sigmoid-normalised using velocity-adjusted score. Meaningless until β is calibrated via back-test.")
p2.metric("De-escalation probability", f"{deesc_pct}%",
          help="Sigmoid-normalised. Meaningless until β is calibrated via back-test.")
p3.metric("Escalation raw score",      f"{esc_raw:.2f}",
          help="Sum of all decayed escalation signal weights (pre-velocity).")
p4.metric("Velocity (24h)",            f"{velocity_arrow} {velocity_delta}",
          help=f"Score change vs 24h ago. Velocity bonus applied to probability: +{velocity_bonus:.2f} pts. "
               f"Rising scores get up to {30:.0f} pts bonus (VELOCITY_WEIGHT=0.30).")
p5.metric("Active signals",            sig_count,
          help="Number of signals with score > 0.01 in the 30-day window.")

st.plotly_chart(chart_probability_gauge(esc_pct, deesc_pct), use_container_width=True)

st.divider()

# ── Polymarket: ME Markets ──────────────────────────────────────────────────────
st.subheader("Polymarket: ME Markets")

if not os.path.exists(POLY_DB):
    st.info("Run `python3 polymarket_collector.py --loop` to track Polymarket ME markets.")
else:
    last_polled = load_polymarket_last_polled()
    poly_df     = load_polymarket_markets()

    # Staleness check
    if last_polled:
        try:
            lp_dt   = datetime.fromisoformat(last_polled.replace("Z", "+00:00"))
            if lp_dt.tzinfo is None:
                lp_dt = lp_dt.replace(tzinfo=timezone.utc)
            age_min = (datetime.now(timezone.utc) - lp_dt).total_seconds() / 60
            age_str = f"{int(age_min)} min ago" if age_min < 90 else f"{age_min/60:.1f}h ago"
        except Exception:
            age_min, age_str = 999, "unknown"
    else:
        age_min, age_str = 999, "unknown"

    n_markets = len(poly_df) if poly_df is not None else 0
    st.caption(f"Last polled: {age_str}  ·  {n_markets} markets tracked")

    if age_min > 30:
        st.warning("Market prices may be stale — collector may be down (last update > 30 min ago)")

    if poly_df is None or poly_df.empty:
        st.info("No active ME markets in DB. Run collector to populate.")
    else:
        now_utc = datetime.now(timezone.utc)

        def _days_to_expiry(end_date_str):
            if not end_date_str:
                return None
            try:
                dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                days = (dt - now_utc).days
                return max(days, 0)
            except Exception:
                return None

        def _model_pct(track):
            if track == "deescalation":
                return round(deesc_prob * 100, 1)
            return round(esc_prob * 100, 1)

        rows = []
        for _, r in poly_df.iterrows():
            yes_pct   = round(r["yes_price"] * 100, 1) if r["yes_price"] is not None else None
            model_pct = _model_pct(r["signal_track"])
            if yes_pct is not None:
                edge = round((model_pct - yes_pct), 1)
                bet  = "Yes" if edge >= 2 else ("No" if edge <= -2 else "—")
            else:
                edge, bet = None, "—"

            days_left = _days_to_expiry(r["end_date"])
            vol = r["volume"] or 0
            vol_str = f"${vol/1e6:.1f}M" if vol >= 1e6 else f"${vol/1e3:.0f}K"

            url = f"https://polymarket.com/event/{r['slug']}" if r["slug"] else ""
            rows.append({
                "Market":  url or r["question"],
                "Track":   "De-esc" if r["signal_track"] == "deescalation" else "Esc",
                "Yes%":    yes_pct,
                "Model%":  model_pct,
                "Edge":    edge,
                "Bet":     bet,
                "Volume":  vol_str,
                "Expires": f"{days_left}d" if days_left is not None else "—",
            })

        display_df = pd.DataFrame(rows)

        def _style_edge(val):
            """Color edge cells by absolute magnitude (applied to numeric Edge column)."""
            try:
                abs_e = abs(float(val))
            except (TypeError, ValueError):
                return "color: #475569"
            if abs_e > 10:
                return "color: #22c55e; font-weight: 700"
            elif abs_e > 2:
                return "color: #86efac"
            return "color: #475569"

        show_cols = ["Market", "Track", "Yes%", "Model%", "Edge", "Bet", "Volume", "Expires"]
        styled = (
            display_df[show_cols]
            .style
            .map(_style_edge, subset=["Edge"])
            .format({"Edge": lambda v: f"{v:+.1f}%" if v is not None else "—"})
        )

        st.dataframe(
            styled,
            column_config={
                "Market": st.column_config.LinkColumn(
                    "Market",
                    display_text=r"https://polymarket\.com/event/(.+)",
                    help="Click to open on Polymarket",
                ),
                "Track":  st.column_config.TextColumn("Track",   width="small"),
                "Yes%":   st.column_config.NumberColumn("Yes%",  format="%.1f%%", width="small"),
                "Model%": st.column_config.NumberColumn("Model%",format="%.1f%%", width="small"),
                "Edge":   st.column_config.TextColumn("Edge",    width="small"),
                "Bet":    st.column_config.TextColumn("Bet",     width="small"),
                "Volume": st.column_config.TextColumn("Volume",  width="small"),
                "Expires":st.column_config.TextColumn("Expires", width="small"),
            },
            hide_index=True,
            use_container_width=True,
        )

        st.caption(
            "Edge = Model% − Yes%.  "
            "Green >10% = meaningful alpha.  "
            "Positive edge → bet Yes; negative → bet No.  "
            "Model% uses escalation prob for Esc markets, de-escalation prob for De-esc markets."
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
        "Fires when 2+ signal categories both score >2.0 in the same macro-zone "
        "(GULF / LEVANT / IRAN / YEMEN_RED_SEA / EGYPT / SAUDI / IRAQ / TURKEY) simultaneously. "
        "GDELT and going-dark signals act as wildcards that can contribute to any zone's coherence."
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

Once the condition clears, it switches to exponential decay from the peak it reached,
using the signal's own λ — so a resolved ISR orbit (λ=0.06) stays relevant for weeks,
while a lifted NOTAM (λ=0.35) fades in days.

**Score velocity:** a rising score earns a bonus before the sigmoid step:
> Velocity bonus = min(score_now − score_24h ago, 30) × 0.30

A score accelerating from 20 → 40 → 80 is treated more urgently than a static 80.
The bonus is applied only to the probability calculation — the stored raw score is
never inflated, so the history chart remains comparable across time.

**Coherence bonus (1.5×):** if 2+ signal categories both score >2.0 in the same macro-zone
(GULF, LEVANT, IRAN, YEMEN\_RED\_SEA, etc.) simultaneously, that zone's subtotal gets a 50% bonus.
All signal layers — ADS-B, AIS, NOTAM, GDELT — are normalised to the same zone vocabulary first,
so a tanker surge in "persian\_gulf" and a traffic drop in "Persian Gulf / Qatar" both map to GULF
and can cohere. GDELT and going-dark signals act as wildcards: they can join any zone's coherence
check, but cannot trigger the bonus on their own — physical corroboration is required.

**Sigmoid normalisation:** the raw sum converts to 0–100% via:
> P = 1 / (1 + e^(−0.08 × (score − 100)))

⚠ The midpoint β=100 is a rough calibration based on expected live signal stack size.
It will be refined once 6+ months of real multi-layer data can be compared against known
historical events. The GDELT back-test validates signal direction (correct 6/7 events)
but cannot set β because GDELT alone contributes only ~1–5 pts of the total score.
""")
