#!/usr/bin/env python3
"""
MarketSignal — Strategic Aircraft Monitor (Stage 2b)

VIP tail number tracking + strategic type clustering across ME watch regions.
"""

import streamlit as st
import plotly.graph_objects as go
import pandas as pd
import sqlite3
import json
from datetime import datetime, timedelta, timezone

st.set_page_config(
    page_title="MarketSignal — Strategic Monitor",
    layout="wide",
    initial_sidebar_state="expanded",
)

DB_PATH = "adsb_events.db"

REGION_LABELS = {
    "TW_PGULF": "Persian Gulf Watch",
    "TW_EMED":  "Eastern Med Watch",
    "TW_HORN":  "Horn of Africa Watch",
    "TW_CAUC":  "Caucasus Corridor",
}

CATEGORY_LABELS = {
    "strategic_lift": "Strategic Lift",
    "tanker":         "Tanker",
    "isr_command":    "ISR / Command",
    "bizjet":         "Diplomatic Bizjet",
}

CATEGORY_COLORS = {
    "strategic_lift": "#ef4444",   # red
    "tanker":         "#f97316",   # orange
    "isr_command":    "#a855f7",   # purple
    "bizjet":         "#06b6d4",   # cyan
}


# ── Data loading ───────────────────────────────────────────────────────────────

def _conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


@st.cache_data(ttl=120, show_spinner=False)
def load_meta():
    with _conn() as conn:
        vip_total   = conn.execute("SELECT COUNT(*) FROM vip_sightings").fetchone()[0]
        dark_total  = conn.execute("SELECT COUNT(*) FROM vip_dark_events").fetchone()[0]
        anom_total  = conn.execute("SELECT COUNT(*) FROM type_anomalies").fetchone()[0]
        clust_total = conn.execute("SELECT COUNT(*) FROM bizjet_clusters").fetchone()[0]
        twc_first   = conn.execute("SELECT MIN(polled_at) FROM type_watch_counts").fetchone()[0]
        twc_last    = conn.execute("SELECT MAX(polled_at) FROM type_watch_counts").fetchone()[0]
    return vip_total, dark_total, anom_total, clust_total, twc_first, twc_last


@st.cache_data(ttl=120, show_spinner=False)
def load_vip_sightings(hours):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _conn() as conn:
        return pd.read_sql_query("""
            SELECT detected_at, tail_number, operator, country,
                   aircraft_type, category, signal_value,
                   region_label, callsign, lat, lon, altitude_m, on_ground
            FROM vip_sightings
            WHERE detected_at >= ?
            ORDER BY detected_at DESC
        """, conn, params=(cutoff,))


@st.cache_data(ttl=120, show_spinner=False)
def load_vip_last_seen():
    with _conn() as conn:
        return pd.read_sql_query("""
            SELECT icao24, tail_number, operator,
                   last_seen_at, last_region, last_lat, last_lon, dark_flagged
            FROM vip_last_seen
            ORDER BY last_seen_at DESC
        """, conn)


@st.cache_data(ttl=120, show_spinner=False)
def load_dark_events(days=7):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _conn() as conn:
        return pd.read_sql_query("""
            SELECT detected_at, tail_number, operator,
                   last_seen_at, last_region, last_lat, last_lon, hours_dark
            FROM vip_dark_events
            WHERE detected_at >= ?
            ORDER BY detected_at DESC
        """, conn, params=(cutoff,))


@st.cache_data(ttl=120, show_spinner=False)
def load_type_timeseries(hours):
    cutoff_unix = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp())
    with _conn() as conn:
        df = pd.read_sql_query("""
            SELECT polled_at, region, category, count
            FROM type_watch_counts
            WHERE polled_unix >= ?
            ORDER BY polled_unix ASC
        """, conn, params=(cutoff_unix,))
    if not df.empty:
        df["polled_at"] = pd.to_datetime(df["polled_at"])
        df["region_label"]   = df["region"].map(REGION_LABELS).fillna(df["region"])
        df["category_label"] = df["category"].map(CATEGORY_LABELS).fillna(df["category"])
    return df


@st.cache_data(ttl=120, show_spinner=False)
def load_type_anomalies(days=7):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _conn() as conn:
        return pd.read_sql_query("""
            SELECT detected_at, region_label, category, current_count,
                   ROUND(baseline_mean, 1) AS baseline_mean,
                   ROUND(baseline_std,  1) AS baseline_std,
                   ROUND(sigma_above,   1) AS sigma_above,
                   severity, aircraft_seen
            FROM type_anomalies
            WHERE detected_at >= ?
            ORDER BY detected_at DESC
            LIMIT 100
        """, conn, params=(cutoff,))


@st.cache_data(ttl=120, show_spinner=False)
def load_bizjet_clusters(days=7):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _conn() as conn:
        return pd.read_sql_query("""
            SELECT detected_at, airport_name, airport_icao,
                   bizjet_count, countries
            FROM bizjet_clusters
            WHERE detected_at >= ?
            ORDER BY detected_at DESC
        """, conn, params=(cutoff,))


# ── Charts ─────────────────────────────────────────────────────────────────────

def chart_type_timeseries(df, selected_regions, selected_categories):
    if df.empty:
        return None
    fig = go.Figure()
    for region in selected_regions:
        for cat in selected_categories:
            rdf = df[(df["region_label"] == region) & (df["category"] == cat)].copy()
            if rdf.empty:
                continue
            color = CATEGORY_COLORS.get(cat, "#94a3b8")
            dash  = "solid" if region == list(selected_regions)[0] else "dash"
            fig.add_trace(go.Scatter(
                x=rdf["polled_at"],
                y=rdf["count"],
                name=f"{REGION_LABELS.get(region, region)} — {CATEGORY_LABELS.get(cat, cat)}",
                mode="lines+markers",
                line=dict(color=color, width=2, dash=dash),
                marker=dict(size=5),
                hovertemplate=f"{region} / {cat}<br>%{{x|%H:%M}}<br>Count: %{{y}}<extra></extra>",
            ))
    fig.update_layout(
        template="plotly_dark",
        height=360,
        margin=dict(l=0, r=0, t=10, b=0),
        legend=dict(orientation="h", y=1.12, font=dict(size=11)),
        hovermode="x unified",
        yaxis=dict(title="Aircraft count", gridcolor="rgba(255,255,255,0.05)", dtick=1),
        xaxis=dict(gridcolor="rgba(255,255,255,0.05)"),
    )
    return fig


# ── Main ───────────────────────────────────────────────────────────────────────

st.markdown("## Strategic Aircraft Monitor")
st.caption(
    "VIP tail number tracking (Mode A) + strategic type clustering (Mode B) — "
    "requires `adsb_collector.py --loop` running"
)

try:
    vip_total, dark_total, anom_total, clust_total, twc_first, twc_last = load_meta()
except Exception:
    st.error("No strategic monitoring data yet. Run `python3 adsb_collector.py` to start collecting.")
    st.stop()

# ── Sidebar ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## MarketSignal")
    st.caption("Strategic Monitor")
    st.divider()

    hours = st.selectbox("Data window", [6, 12, 24, 48, 72], index=2,
                         format_func=lambda h: f"Last {h}h")

    all_regions = list(REGION_LABELS.keys())
    selected_regions = st.multiselect(
        "Type-watch regions",
        options=all_regions,
        default=all_regions,
        format_func=lambda r: REGION_LABELS.get(r, r),
    )

    all_cats = list(CATEGORY_LABELS.keys())
    selected_cats = st.multiselect(
        "Aircraft categories",
        options=all_cats,
        default=all_cats,
        format_func=lambda c: CATEGORY_LABELS.get(c, c),
    )

    st.divider()
    st.caption(f"VIP sightings logged: {vip_total:,}")
    st.caption(f"Going-dark events: {dark_total:,}")
    st.caption(f"Type anomalies: {anom_total:,}")
    st.caption(f"Bizjet clusters: {clust_total:,}")
    if twc_first:
        st.caption(f"Type data since: {twc_first[:16]}")

    if st.button("Refresh data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

# ── Metrics ────────────────────────────────────────────────────────────────────
vip_24h   = load_vip_sightings(24)
dark_7d   = load_dark_events(7)
anom_7d   = load_type_anomalies(7)
clust_7d  = load_bizjet_clusters(7)

c1, c2, c3, c4 = st.columns(4)
c1.metric("VIP sightings (24h)",    len(vip_24h),
          help="Known VIP aircraft seen in any monitored region")
c2.metric("Going dark (7d)",         len(dark_7d),
          help="VIP aircraft that disappeared for >24h")
c3.metric("Type anomalies (7d)",    len(anom_7d),
          help="Strategic type counts >2σ above 30-day baseline")
c4.metric("Bizjet clusters (7d)",   len(clust_7d),
          help=f"3+ bizjets from different countries at same airport")

# ── VIP sightings ──────────────────────────────────────────────────────────────
st.divider()
st.subheader("VIP aircraft sightings")
st.caption(
    "Known VIP/diplomatic/strategic aircraft (from watchlist) detected in monitored regions. "
    "Aircraft without ICAO24 codes cannot be tracked via ADS-B."
)

vip_df = load_vip_sightings(hours)
if vip_df.empty:
    st.info(
        f"No VIP aircraft sightings in the last {hours}h. "
        "Either none of the 7 watched aircraft are in monitored airspace, "
        "or they're flying with ADS-B off."
    )
else:
    st.dataframe(
        vip_df,
        column_config={
            "detected_at":  st.column_config.TextColumn("Detected at"),
            "tail_number":  st.column_config.TextColumn("Tail"),
            "operator":     st.column_config.TextColumn("Operator"),
            "country":      st.column_config.TextColumn("Country"),
            "aircraft_type":st.column_config.TextColumn("Type"),
            "category":     st.column_config.TextColumn("Category"),
            "signal_value": st.column_config.TextColumn("Signal"),
            "region_label": st.column_config.TextColumn("Region"),
            "callsign":     st.column_config.TextColumn("Callsign"),
            "lat":          st.column_config.NumberColumn("Lat",  format="%.2f"),
            "lon":          st.column_config.NumberColumn("Lon",  format="%.2f"),
            "altitude_m":   st.column_config.NumberColumn("Alt m", format="%.0f"),
            "on_ground":    st.column_config.CheckboxColumn("Gnd"),
        },
        hide_index=True,
        use_container_width=True,
    )

# VIP last-seen status
st.markdown("**Current watchlist status**")
st.caption("All 7 aircraft with known ICAO24s — last seen timestamp and location.")
vip_ls = load_vip_last_seen()
if vip_ls.empty:
    st.info("No VIP aircraft have been seen yet since monitoring started.")
else:
    def dark_marker(row):
        return "⚠ DARK" if row["dark_flagged"] else ""
    vip_ls["status"] = vip_ls.apply(dark_marker, axis=1)
    st.dataframe(
        vip_ls[["tail_number", "operator", "last_seen_at", "last_region", "last_lat", "last_lon", "status"]],
        column_config={
            "tail_number":  st.column_config.TextColumn("Tail"),
            "operator":     st.column_config.TextColumn("Operator"),
            "last_seen_at": st.column_config.TextColumn("Last seen"),
            "last_region":  st.column_config.TextColumn("Last region"),
            "last_lat":     st.column_config.NumberColumn("Lat", format="%.2f"),
            "last_lon":     st.column_config.NumberColumn("Lon", format="%.2f"),
            "status":       st.column_config.TextColumn("Status"),
        },
        hide_index=True,
        use_container_width=True,
    )

# Going dark events
if not dark_7d.empty:
    st.divider()
    st.subheader("Going-dark events")
    st.caption("VIP aircraft that stopped transmitting for >24h. Last 7 days.")
    st.dataframe(
        dark_7d,
        column_config={
            "detected_at":  st.column_config.TextColumn("Flagged at"),
            "tail_number":  st.column_config.TextColumn("Tail"),
            "operator":     st.column_config.TextColumn("Operator"),
            "last_seen_at": st.column_config.TextColumn("Last seen"),
            "last_region":  st.column_config.TextColumn("Last region"),
            "last_lat":     st.column_config.NumberColumn("Lat", format="%.2f"),
            "last_lon":     st.column_config.NumberColumn("Lon", format="%.2f"),
            "hours_dark":   st.column_config.NumberColumn("Hours dark", format="%.0f"),
        },
        hide_index=True,
        use_container_width=True,
    )

# ── Mode B: type clustering ─────────────────────────────────────────────────────
st.divider()
st.subheader("Strategic type counts — type-watch regions")
st.caption(
    "Red = strategic lift  |  Orange = tanker  |  Purple = ISR/command  |  Cyan = bizjet  |  "
    "Anomaly threshold: 2σ above 30-day baseline (activates after ~25h of polling)"
)

df_ts = load_type_timeseries(hours)

if df_ts.empty:
    st.info(
        f"No strategic aircraft counts in the last {hours}h. "
        "Run `python3 adsb_collector.py --loop` to build up data."
    )
else:
    if not selected_regions or not selected_cats:
        st.info("Select at least one region and one category in the sidebar.")
    else:
        fig = chart_type_timeseries(df_ts, selected_regions, selected_cats)
        if fig:
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No data for the selected region/category combination.")

    # Current counts table
    if twc_last:
        st.caption(f"Latest poll: {twc_last[:16]} UTC")
    cutoff_unix = int((datetime.now(timezone.utc) - timedelta(hours=2)).timestamp())
    with sqlite3.connect(DB_PATH) as c:
        latest_counts = pd.read_sql_query("""
            SELECT region, category, SUM(count) AS count
            FROM type_watch_counts
            WHERE polled_unix >= (SELECT MAX(polled_unix) - 600 FROM type_watch_counts)
            GROUP BY region, category
            ORDER BY region, count DESC
        """, c)
    if not latest_counts.empty:
        latest_counts["region"]   = latest_counts["region"].map(REGION_LABELS).fillna(latest_counts["region"])
        latest_counts["category"] = latest_counts["category"].map(CATEGORY_LABELS).fillna(latest_counts["category"])
        latest_counts.columns     = ["Region", "Category", "Count"]
        pivot = latest_counts.pivot(index="Region", columns="Category", values="Count").fillna(0).astype(int)
        st.dataframe(pivot, use_container_width=True)

# ── Type anomalies ─────────────────────────────────────────────────────────────
st.divider()
st.subheader("Type anomaly log")
st.caption(
    "Strategic aircraft counts that exceeded 2σ above 30-day same-hour baseline. "
    "Last 7 days. Requires ≥10 baseline samples to activate."
)

if anom_7d.empty:
    st.info(
        "No type anomalies yet. Baseline activates after ~25h of continuous polling. "
        "Currently accumulating baseline data."
    )
else:
    def parse_aircraft(json_str):
        try:
            items = json.loads(json_str or "[]")
            return ", ".join(f"{i[0]}({i[1]})" for i in items[:5])
        except Exception:
            return ""
    anom_7d["aircraft"] = anom_7d["aircraft_seen"].apply(parse_aircraft)

    st.dataframe(
        anom_7d[["detected_at", "region_label", "category", "current_count",
                 "baseline_mean", "sigma_above", "severity", "aircraft"]],
        column_config={
            "detected_at":   st.column_config.TextColumn("Detected at"),
            "region_label":  st.column_config.TextColumn("Region"),
            "category":      st.column_config.TextColumn("Category"),
            "current_count": st.column_config.NumberColumn("Count"),
            "baseline_mean": st.column_config.NumberColumn("Baseline avg"),
            "sigma_above":   st.column_config.NumberColumn("Sigma", format="%.1f"),
            "severity":      st.column_config.TextColumn("Severity"),
            "aircraft":      st.column_config.TextColumn("Aircraft (icao24/type)"),
        },
        hide_index=True,
        use_container_width=True,
    )

# ── Bizjet clusters ────────────────────────────────────────────────────────────
st.divider()
st.subheader("Diplomatic bizjet clusters")
st.caption(
    f"3+ large-cabin bizjets from different countries on the ground at the same airport "
    f"within a single poll cycle. Last 7 days."
)

if clust_7d.empty:
    st.info("No bizjet clusters detected yet.")
else:
    def fmt_countries(json_str):
        try:
            return ", ".join(json.loads(json_str or "[]"))
        except Exception:
            return json_str or ""
    clust_7d["countries"] = clust_7d["countries"].apply(fmt_countries)

    st.dataframe(
        clust_7d,
        column_config={
            "detected_at":   st.column_config.TextColumn("Detected at"),
            "airport_name":  st.column_config.TextColumn("Airport"),
            "airport_icao":  st.column_config.TextColumn("ICAO"),
            "bizjet_count":  st.column_config.NumberColumn("Bizjets"),
            "countries":     st.column_config.TextColumn("Origin countries"),
        },
        hide_index=True,
        use_container_width=True,
    )
    st.caption(
        "Bizjet clusters at diplomatic hubs (Muscat, Doha, Riyadh, etc.) signal "
        "back-channel negotiations — particularly powerful for ceasefire markets."
    )
