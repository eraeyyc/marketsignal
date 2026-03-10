#!/usr/bin/env python3
"""
MarketSignal — ADS-B Airspace Monitor page
"""

import streamlit as st
import plotly.graph_objects as go
import pandas as pd
import sqlite3
import json
from datetime import datetime, timezone

st.set_page_config(
    page_title="MarketSignal — ADS-B Monitor",
    layout="wide",
    initial_sidebar_state="expanded",
)

DB_PATH = "adsb_events.db"

WATCHED_AIRLINES = {
    "ELY": "El Al",
    "BAW": "British Airways",
    "AFR": "Air France",
    "DLH": "Lufthansa",
    "UAE": "Emirates",
    "QTR": "Qatar Airways",
    "THY": "Turkish Airlines",
    "RYR": "Ryanair",
    "DAL": "Delta",
    "UAL": "United",
    "ETD": "Etihad",
    "SVA": "Saudia",
    "MEA": "Middle East Airlines",
}

REGION_ORDER = [
    "Israel / Palestine",
    "Lebanon / Syria",
    "Jordan",
    "Egypt / Sinai",
    "Iran",
    "Yemen / Red Sea",
    "Persian Gulf / Qatar",
    "Saudi Arabia",
    "Turkey",
]


# ── Data loading ───────────────────────────────────────────────────────────────
# Each function opens its own connection so @st.cache_data can hash arguments
# correctly and return distinct results for different parameter values.

def _conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


@st.cache_data(ttl=120, show_spinner=False)
def load_latest_snapshot():
    with _conn() as conn:
        rows = conn.execute("""
            SELECT s.region_label, s.aircraft_count, s.airborne, s.on_ground,
                   s.polled_at, s.aircraft_json
            FROM snapshots s
            INNER JOIN (
                SELECT region, MAX(polled_unix) AS mx FROM snapshots GROUP BY region
            ) latest ON s.region = latest.region AND s.polled_unix = latest.mx
            ORDER BY s.aircraft_count DESC
        """).fetchall()
    return rows


@st.cache_data(ttl=120, show_spinner=False)
def load_timeseries(hours=48):
    cutoff_unix = int(datetime.now(timezone.utc).timestamp()) - hours * 3600
    with _conn() as conn:
        df = pd.read_sql_query("""
            SELECT region_label, polled_at, aircraft_count, airborne
            FROM snapshots
            WHERE polled_unix >= ?
            ORDER BY polled_unix ASC
        """, conn, params=(cutoff_unix,))
    if not df.empty:
        df["polled_at"] = pd.to_datetime(df["polled_at"])
    return df


@st.cache_data(ttl=120, show_spinner=False)
def load_anomalies():
    with _conn() as conn:
        return pd.read_sql_query("""
            SELECT detected_at, region_label, current_count,
                   ROUND(baseline_avg, 1) AS baseline_avg,
                   ROUND(drop_pct * 100, 0) AS drop_pct,
                   severity
            FROM anomalies
            ORDER BY detected_at DESC
            LIMIT 50
        """, conn)


@st.cache_data(ttl=120, show_spinner=False)
def load_db_meta():
    with _conn() as conn:
        total    = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        latest   = conn.execute("SELECT MAX(polled_at) FROM snapshots").fetchone()[0]
        earliest = conn.execute("SELECT MIN(polled_at) FROM snapshots").fetchone()[0]
    return total, earliest, latest


# ── Charts ─────────────────────────────────────────────────────────────────────

def chart_current_counts(rows):
    labels  = [r[0] for r in rows]
    counts  = [r[1] for r in rows]
    colors  = []
    for label, count in zip(labels, counts):
        if label in ("Israel / Palestine", "Lebanon / Syria", "Iran"):
            colors.append("rgba(239, 68, 68, 0.75)")   # red — conflict zone
        elif label in ("Persian Gulf / Qatar", "Turkey", "Saudi Arabia"):
            colors.append("rgba(99, 102, 241, 0.75)")  # indigo — Gulf/regional
        else:
            colors.append("rgba(148, 163, 184, 0.65)") # grey — other

    fig = go.Figure(go.Bar(
        x=labels,
        y=counts,
        marker_color=colors,
        hovertemplate="%{x}<br>Aircraft: %{y}<extra></extra>",
    ))
    fig.update_layout(
        template="plotly_dark",
        height=300,
        margin=dict(l=0, r=0, t=10, b=0),
        yaxis=dict(title="Aircraft count", gridcolor="rgba(255,255,255,0.05)"),
        xaxis=dict(tickangle=-20),
    )
    return fig


def chart_timeseries(df, selected_regions, hours):
    fig = go.Figure()
    colors = [
        "#ef4444", "#f97316", "#eab308", "#22c55e",
        "#06b6d4", "#6366f1", "#a855f7", "#ec4899", "#94a3b8",
    ]
    for i, region in enumerate(selected_regions):
        rdf = df[df["region_label"] == region].copy()
        if rdf.empty:
            continue
        fig.add_trace(go.Scatter(
            x=rdf["polled_at"],
            y=rdf["aircraft_count"],
            name=region,
            mode="lines+markers",
            line=dict(color=colors[i % len(colors)], width=2),
            marker=dict(size=4),
            hovertemplate=f"{region}<br>%{{x|%H:%M}}<br>Aircraft: %{{y}}<extra></extra>",
        ))
    fig.update_layout(
        template="plotly_dark",
        height=380,
        margin=dict(l=0, r=0, t=10, b=0),
        legend=dict(orientation="h", y=1.08),
        hovermode="x unified",
        yaxis=dict(title="Aircraft count", gridcolor="rgba(255,255,255,0.05)"),
        xaxis=dict(gridcolor="rgba(255,255,255,0.05)"),
    )
    return fig


# ── Main ───────────────────────────────────────────────────────────────────────

st.markdown("## ADS-B Airspace Monitor")
st.caption("Live aircraft state vectors via OpenSky Network — Middle East bounding boxes")

# Check DB exists
try:
    total_snaps, earliest, latest_ts = load_db_meta()
except Exception:
    st.error("No ADS-B data yet. Run `python3 adsb_collector.py --loop` to start collecting.")
    st.stop()

if total_snaps == 0:
    st.info("Collector is running but no snapshots yet. Check back in a minute.")
    st.stop()

# ── Sidebar controls ───────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## MarketSignal")
    st.caption("ADS-B Monitor")
    st.divider()
    hours = st.selectbox("Trend window", [6, 12, 24, 48, 72], index=2, format_func=lambda h: f"Last {h}h")
    selected_regions = st.multiselect(
        "Regions to plot",
        options=REGION_ORDER,
        default=["Israel / Palestine", "Lebanon / Syria", "Iran", "Persian Gulf / Qatar"],
    )
    st.divider()
    st.caption(f"Snapshots collected: {total_snaps:,}")
    st.caption(f"Since: {earliest[:16] if earliest else '—'}")
    if latest_ts:
        st.caption(f"Last poll: {latest_ts[:16]}")
    if st.button("Refresh data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

# ── Current snapshot ───────────────────────────────────────────────────────────
rows = load_latest_snapshot()
if not rows:
    st.warning("No snapshots found.")
    st.stop()

polled_at = rows[0][4][:16] if rows else "—"
st.caption(f"Current snapshot — {polled_at} UTC")

st.subheader("Aircraft count by region")
st.caption("Red = conflict zone  |  Indigo = Gulf/regional  |  Grey = other")
st.plotly_chart(chart_current_counts(rows), use_container_width=True)

# ── Metrics row ────────────────────────────────────────────────────────────────
st.divider()
cols = st.columns(len(rows))
for col, (label, total_ac, airborne, on_ground, _, _json) in zip(cols, rows):
    short = label.split(" /")[0]  # "Israel / Palestine" → "Israel"
    col.metric(short, total_ac, help=f"Airborne: {airborne}  |  Ground: {on_ground}")

# ── Trend chart ────────────────────────────────────────────────────────────────
st.divider()
st.subheader(f"Traffic trend — last {hours}h")

df_ts = load_timeseries(hours)
if df_ts.empty:
    st.info("Not enough history yet for trend chart. Check back after a few polls.")
else:
    if not selected_regions:
        st.info("Select at least one region in the sidebar to plot.")
    else:
        st.plotly_chart(chart_timeseries(df_ts, selected_regions, hours), use_container_width=True)

# ── Airline presence ───────────────────────────────────────────────────────────
st.divider()
st.subheader("Watched airline presence (latest snapshot)")

airline_counts = {}
for _, _, _, _, _, aircraft_json in rows:
    for ac in json.loads(aircraft_json or "[]"):
        callsign = ac[1]
        if callsign and len(callsign) >= 3:
            prefix = callsign[:3]
            if prefix in WATCHED_AIRLINES:
                name = WATCHED_AIRLINES[prefix]
                airline_counts[name] = airline_counts.get(name, 0) + 1

if airline_counts:
    airline_df = pd.DataFrame(
        sorted(airline_counts.items(), key=lambda x: -x[1]),
        columns=["Airline", "Aircraft visible"],
    )
    st.dataframe(airline_df, hide_index=True, use_container_width=True)
    st.caption("Presence of major carriers signals airspace safety confidence. Absence is the signal.")
else:
    st.info("No watched airlines currently visible across monitored regions.")

# ── Anomaly log ────────────────────────────────────────────────────────────────
st.divider()
st.subheader("Anomaly log")
st.caption("Traffic drops >40% below 7-day same-hour baseline are flagged.")

anomaly_df = load_anomalies()
if anomaly_df.empty:
    st.info("No anomalies detected yet. Baseline builds after 3–4 days of polling.")
else:
    def severity_color(val):
        if val == "HIGH":
            return "color: #ef4444; font-weight: bold"
        if val == "MEDIUM":
            return "color: #f97316"
        return "color: #eab308"

    st.dataframe(
        anomaly_df,
        column_config={
            "detected_at":  st.column_config.TextColumn("Detected at"),
            "region_label": st.column_config.TextColumn("Region"),
            "current_count":st.column_config.NumberColumn("Count"),
            "baseline_avg": st.column_config.NumberColumn("Baseline avg"),
            "drop_pct":     st.column_config.NumberColumn("Drop %", format="%.0f%%"),
            "severity":     st.column_config.TextColumn("Severity"),
        },
        hide_index=True,
        use_container_width=True,
    )
