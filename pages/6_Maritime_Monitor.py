"""
MarketSignal — Maritime Monitor (Page 6)
AIS vessel tracking: tanker density, military presence, watchlist sightings,
GPS spoofing events.
"""

import streamlit as st
import sqlite3
import pandas as pd
import plotly.express as px
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.styles import inject_css, page_header
from datetime import datetime, timezone, timedelta

DB_PATH = "ais_events.db"

st.set_page_config(page_title="MarketSignal — Maritime Monitor", layout="wide",
                   initial_sidebar_state="collapsed")
inject_css()

# ── Data loaders ───────────────────────────────────────────────────────────────

@st.cache_data(ttl=120)
def load_meta():
    conn = sqlite3.connect(DB_PATH)
    try:
        total = conn.execute("SELECT COUNT(*) FROM vessel_snapshots").fetchone()[0]
        dr    = conn.execute(
            "SELECT MIN(snapshot_time), MAX(snapshot_time) FROM vessel_snapshots"
        ).fetchone()
        return total, dr[0], dr[1]
    except Exception:
        return 0, None, None
    finally:
        conn.close()


@st.cache_data(ttl=120)
def load_active_anomalies():
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute("""
            SELECT detected_at, region_label, category, anomaly_type,
                   severity, baseline_count, observed_count, drop_pct, detail,
                   last_confirmed_at
            FROM vessel_anomalies
            WHERE resolved_at IS NULL
            ORDER BY detected_at DESC
        """).fetchall()
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows, columns=[
            "Detected", "Region", "Category", "Type", "Severity",
            "Baseline", "Observed", "Drop %", "Detail", "Last Confirmed"
        ])
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()


@st.cache_data(ttl=120)
def load_recent_counts(hours=48):
    conn = sqlite3.connect(DB_PATH)
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = conn.execute("""
            SELECT snapshot_time, region_label, category, vessel_count
            FROM vessel_snapshots
            WHERE snapshot_time > ?
              AND category IN ('tanker', 'cargo', 'military')
            ORDER BY snapshot_time
        """, (cutoff,)).fetchall()
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows, columns=["Time", "Region", "Category", "Count"])
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()


@st.cache_data(ttl=120)
def load_watchlist_sightings(hours=72):
    conn = sqlite3.connect(DB_PATH)
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = conn.execute("""
            SELECT detected_at, mmsi, vessel_name, country, operator,
                   category, signal_value, lat, lon, sog, region_label
            FROM vessel_sightings
            WHERE detected_at > ?
            ORDER BY detected_at DESC
            LIMIT 100
        """, (cutoff,)).fetchall()
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows, columns=[
            "Detected", "MMSI", "Vessel", "Country", "Operator",
            "Category", "Signal", "Lat", "Lon", "SOG (kn)", "Region"
        ])
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()


@st.cache_data(ttl=120)
def load_spoofing(days=7):
    conn = sqlite3.connect(DB_PATH)
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = conn.execute("""
            SELECT detected_at, mmsi, vessel_name, lat, lon,
                   reported_sog, anomaly_type, detail
            FROM spoofing_events
            WHERE detected_at > ?
            ORDER BY detected_at DESC
            LIMIT 50
        """, (cutoff,)).fetchall()
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows, columns=[
            "Detected", "MMSI", "Vessel", "Lat", "Lon",
            "Reported SOG", "Type", "Detail"
        ])
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()


# ── Metric row ─────────────────────────────────────────────────────────────────

total_snaps, first_snap, last_snap = load_meta()
anomalies_df = load_active_anomalies()
spoofing_df  = load_spoofing()

page_header("Maritime Monitor", "AIS vessel tracking — Middle East via aisstream.io",
            timestamp=f"Last update: {last_snap[:16] if last_snap else '—'} UTC")

col1, col2, col3, col4 = st.columns(4)
col1.metric("Active Anomalies",   len(anomalies_df))
col2.metric("Spoofing Events (7d)", len(spoofing_df))
col3.metric("Total Snapshots",    f"{total_snaps:,}")
col4.metric("Last Update", last_snap[:16] if last_snap else "—")

st.divider()

# ── Active anomalies ───────────────────────────────────────────────────────────

st.subheader("Active Anomalies")

if anomalies_df.empty:
    if total_snaps == 0:
        st.info("No AIS data yet. Run `python3 ais_collector.py --loop` to start collecting. "
                "Baseline builds after ~7 days of snapshots.")
    else:
        st.success("No active maritime anomalies.")
else:
    def sev_color(sev):
        return "🔴" if sev == "HIGH" else "🟡"

    for _, row in anomalies_df.iterrows():
        icon = sev_color(row["Severity"])
        st.markdown(
            f"{icon} **[{row['Severity']}]** {row['Region']} — "
            f"{row['Category']} {row['Type']}  \n"
            f"*{row['Detail']}*  \n"
            f"Detected: {str(row['Detected'])[:16]}  |  "
            f"Last confirmed: {str(row['Last Confirmed'])[:16]}"
        )

st.divider()

# ── Vessel count trends ────────────────────────────────────────────────────────

st.subheader("Vessel Count Trends")

time_window = st.selectbox("Time window", ["24h", "48h", "7d"], index=1)
hours_map   = {"24h": 24, "48h": 48, "7d": 168}
counts_df   = load_recent_counts(hours_map[time_window])

if counts_df.empty:
    st.info("No count history yet — data appears after first collection window.")
else:
    cat_filter = st.multiselect(
        "Categories", ["tanker", "cargo", "military"], default=["tanker", "cargo"]
    )
    filtered = counts_df[counts_df["Category"].isin(cat_filter)]

    fig = px.line(
        filtered, x="Time", y="Count", color="Region",
        facet_col="Category", facet_col_wrap=2,
        height=400, template="plotly_dark",
    )
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=0, r=0, t=30, b=0),
    )
    st.plotly_chart(fig, use_container_width=True)

st.divider()

# ── Watchlist sightings ────────────────────────────────────────────────────────

st.subheader("Watchlist Sightings")
sightings_df = load_watchlist_sightings()

if sightings_df.empty:
    st.info("No watchlist sightings yet. Vessels may be AIS-dark or not yet in the ME region.")
else:
    st.dataframe(sightings_df, use_container_width=True, hide_index=True)

    # Map
    map_df = sightings_df.dropna(subset=["Lat", "Lon"])
    if not map_df.empty:
        fig_map = px.scatter_geo(
            map_df,
            lat="Lat", lon="Lon",
            hover_name="Vessel",
            hover_data={"MMSI": True, "Operator": True, "SOG (kn)": True, "Region": True},
            color="Category",
            projection="natural earth",
            template="plotly_dark",
        )
        fig_map.update_geos(
            center={"lat": 22, "lon": 55},
            projection_scale=4,
            showland=True, landcolor="#2d3748",
            showocean=True, oceancolor="#1a202c",
        )
        fig_map.update_layout(
            paper_bgcolor="rgba(0,0,0,0)",
            margin=dict(l=0, r=0, t=10, b=0),
        )
        st.plotly_chart(fig_map, use_container_width=True)

st.divider()

# ── GPS spoofing ───────────────────────────────────────────────────────────────

st.subheader("GPS Spoofing Events")
st.caption("Vessels reporting impossible speeds or position jumps — common in Hormuz/Red Sea")

if spoofing_df.empty:
    st.info("No spoofing events detected in the last 7 days.")
else:
    st.dataframe(spoofing_df, use_container_width=True, hide_index=True)

st.divider()

# ── Coverage note ──────────────────────────────────────────────────────────────

with st.expander("Coverage notes"):
    st.markdown("""
    **AIS coverage in the Middle East:**
    - **Persian Gulf / Hormuz**: Good — heavy traffic, many shore-based receivers
    - **Red Sea**: Good — coastal receivers on both sides
    - **Gulf of Aden**: Moderate — some gaps in open water
    - **Arabian Sea (open ocean)**: Limited — satellite AIS needed for full coverage.
      US carrier strike groups operating here may not appear.

    **Why vessels go missing:**
    - Military vessels often disable AIS in operational areas (including US Navy warships)
    - Iran/IRGCN vessels selectively disable AIS during sensitive operations
    - GPS spoofing in Hormuz can corrupt position data (600+ events/day reported)

    **Spoofing detection:** flags SOG > 50 knots or position jumps > 50nm in short intervals.
    These are almost always spoofed — real ships max out around 25-30 knots.
    """)
