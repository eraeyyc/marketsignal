#!/usr/bin/env python3
"""
MarketSignal — NOTAM Monitor page

Shows Middle East NOTAMs collected via Laminar Data API.
Highlights airspace restriction Q-codes as anomaly signals.
"""

import streamlit as st
import plotly.graph_objects as go
import pandas as pd
import sqlite3
from datetime import datetime, timedelta, timezone

st.set_page_config(
    page_title="MarketSignal — NOTAM Monitor",
    layout="wide",
    initial_sidebar_state="expanded",
)

DB_PATH = "notam_events.db"


def qcode_severity(qcode):
    """Return severity string for a Q-code, or None if not a restriction type."""
    if not qcode:
        return None
    q = qcode.upper()
    if q.startswith(("QRD", "QRAL")):
        return "HIGH"
    if q.startswith(("QRT", "QRP", "QR")):
        return "MEDIUM"
    return None


# ── Data loading ───────────────────────────────────────────────────────────────
# Each function opens its own connection so @st.cache_data works correctly.

def _conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


@st.cache_data(ttl=120, show_spinner=False)
def load_meta():
    with _conn() as conn:
        total      = conn.execute("SELECT COUNT(*) FROM notams").fetchone()[0]
        total_anom = conn.execute("SELECT COUNT(*) FROM notam_anomalies").fetchone()[0]
        first_at   = conn.execute("SELECT MIN(first_detected_at) FROM notams").fetchone()[0]
        last_at    = conn.execute("SELECT MAX(last_seen_at)      FROM notams").fetchone()[0]
    return total, total_anom, first_at, last_at


@st.cache_data(ttl=120, show_spinner=False)
def load_all_qcodes():
    with _conn() as conn:
        rows = conn.execute("""
            SELECT DISTINCT qcode FROM notams
            WHERE qcode IS NOT NULL
            ORDER BY qcode
        """).fetchall()
    return [r[0] for r in rows]


@st.cache_data(ttl=120, show_spinner=False)
def load_currently_active():
    """NOTAMs that are still active AND have coordinates (for map)."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        df = pd.read_sql_query("""
            SELECT notam_id, location, country_code, qcode, restriction_type,
                   effective_start, effective_end, effective_end_interp,
                   lat, lon, radius_nm,
                   SUBSTR(raw_text, 1, 120) AS text_excerpt,
                   first_detected_at
            FROM notams
            WHERE (effective_end IS NULL
                   OR effective_end > ?
                   OR effective_end_interp = 'PERM')
              AND lat IS NOT NULL
              AND lon IS NOT NULL
        """, conn, params=(now,))
    return df


@st.cache_data(ttl=120, show_spinner=False)
def load_notams_window(hours):
    """All NOTAMs first detected in the last N hours (for the feed table)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _conn() as conn:
        df = pd.read_sql_query("""
            SELECT notam_id, location, country_code, fir, qcode, restriction_type,
                   effective_start, effective_end, effective_end_interp,
                   lat, lon, radius_nm,
                   SUBSTR(raw_text, 1, 120) AS text_excerpt,
                   first_detected_at
            FROM notams
            WHERE first_detected_at >= ?
            ORDER BY first_detected_at DESC
        """, conn, params=(cutoff,))
    return df


@st.cache_data(ttl=120, show_spinner=False)
def load_anomalies():
    """Anomalies from the last 7 days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    with _conn() as conn:
        return pd.read_sql_query("""
            SELECT detected_at, notam_id, location, country_code,
                   qcode, restriction_type, severity,
                   lat, lon, radius_nm,
                   SUBSTR(raw_text, 1, 120) AS text_excerpt
            FROM notam_anomalies
            WHERE detected_at >= ?
            ORDER BY detected_at DESC
            LIMIT 100
        """, conn, params=(cutoff,))


@st.cache_data(ttl=120, show_spinner=False)
def count_new(hours):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM notams WHERE first_detected_at >= ?", (cutoff,)
        ).fetchone()[0]


@st.cache_data(ttl=120, show_spinner=False)
def count_active():
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        return conn.execute("""
            SELECT COUNT(*) FROM notams
            WHERE effective_end IS NULL
               OR effective_end > ?
               OR effective_end_interp = 'PERM'
        """, (now,)).fetchone()[0]


# ── Map ────────────────────────────────────────────────────────────────────────

def chart_notam_map(df):
    """Scatter geo map of active NOTAMs with known coordinates."""
    if df.empty:
        return None

    def marker_color(qcode):
        sev = qcode_severity(qcode)
        if sev == "HIGH":
            return "rgba(239, 68, 68, 0.85)"
        if sev == "MEDIUM":
            return "rgba(249, 115, 22, 0.85)"
        return "rgba(148, 163, 184, 0.55)"

    def marker_size(radius_nm):
        if not radius_nm or radius_nm == 0:
            return 10
        return max(8, min(35, int(radius_nm * 0.4)))

    colors = [marker_color(q) for q in df["qcode"]]
    sizes  = [marker_size(r)  for r in df["radius_nm"].fillna(0)]

    hover_texts = []
    for row in df.itertuples():
        end_str = str(row.effective_end)[:16] if row.effective_end else (row.effective_end_interp or "—")
        text = (
            f"<b>{row.notam_id}</b><br>"
            f"Location: {row.location or '—'}  |  {row.country_code or ''}<br>"
            f"Type: {row.restriction_type or row.qcode or '—'}<br>"
            f"From: {str(row.effective_start or '')[:16]}<br>"
            f"To: {end_str}<br>"
            f"<i>{str(row.text_excerpt or '')}</i>"
        )
        hover_texts.append(text)

    fig = go.Figure(go.Scattergeo(
        lat=df["lat"],
        lon=df["lon"],
        mode="markers",
        marker=dict(
            size=sizes,
            color=colors,
            line=dict(width=1, color="rgba(255,255,255,0.25)"),
        ),
        hovertext=hover_texts,
        hoverinfo="text",
    ))

    fig.update_layout(
        template="plotly_dark",
        height=500,
        margin=dict(l=0, r=0, t=10, b=0),
        geo=dict(
            projection_type="mercator",
            center=dict(lat=27, lon=45),
            lataxis_range=[5, 50],
            lonaxis_range=[20, 70],
            showland=True,
            landcolor="rgba(45, 48, 58, 1)",
            showocean=True,
            oceancolor="rgba(25, 28, 40, 1)",
            showcountries=True,
            countrycolor="rgba(100, 105, 125, 0.6)",
            showcoastlines=True,
            coastlinecolor="rgba(100, 105, 125, 0.5)",
            bgcolor="rgba(18, 20, 30, 1)",
        ),
    )
    return fig


# ── Main ───────────────────────────────────────────────────────────────────────

st.markdown("## NOTAM Monitor")
st.caption("Middle East airspace notices via Laminar Data API — lat 10–45 N, lon 25–65 E")

# Check DB exists and has data
try:
    total_notams, total_anoms, first_at, last_at = load_meta()
except Exception:
    st.error(
        "No NOTAM data yet. Run `python3 notam_collector.py` to start collecting."
    )
    st.stop()

if total_notams == 0:
    st.info("Collector has run but found no NOTAMs. Check back after the next poll.")
    st.stop()

# ── Sidebar ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## MarketSignal")
    st.caption("NOTAM Monitor")
    st.divider()

    hours = st.selectbox(
        "Feed window",
        [24, 48, 72],
        format_func=lambda h: f"Last {h}h",
    )

    all_qcodes = load_all_qcodes()
    selected_qcodes = st.multiselect(
        "Q-code filter",
        options=all_qcodes,
        default=all_qcodes,
        help="Filter the NOTAM feed by Q-code. Leave all checked to see everything.",
    )

    st.divider()
    st.caption(f"Total NOTAMs stored: {total_notams:,}")
    st.caption(f"Since: {first_at[:16] if first_at else '—'}")
    st.caption(f"Last seen: {last_at[:16] if last_at else '—'}")

    if st.button("Refresh data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

# ── Metrics row ────────────────────────────────────────────────────────────────
n_active = count_active()
n_24h    = count_new(24)
n_7d     = count_new(168)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Active restrictions", n_active, help="NOTAMs with no expiry or future expiry")
c2.metric("New in last 24h",     n_24h)
c3.metric("New in last 7 days",  n_7d)
c4.metric("Total anomalies logged", total_anoms)

# ── Map ────────────────────────────────────────────────────────────────────────
st.divider()
st.subheader("Active airspace restrictions")
st.caption(
    "Active NOTAMs with known coordinates.  "
    "Red = HIGH severity (danger/all-traffic)  |  Orange = MEDIUM (restricted/prohibited)  |  "
    "Grey = other type  |  Size ∝ radius"
)

map_df = load_currently_active()
if map_df.empty:
    st.info("No currently active NOTAMs with coordinate data available.")
else:
    fig = chart_notam_map(map_df)
    if fig:
        st.plotly_chart(fig, use_container_width=True)
    st.caption(
        f"{len(map_df)} active NOTAMs plotted. NOTAMs without coordinates are in the feed table below."
    )

# ── NOTAM feed ────────────────────────────────────────────────────────────────
st.divider()
st.subheader(f"NOTAM feed — last {hours}h")
st.caption("All NOTAMs first detected in the selected window. Use the Q-code filter in the sidebar to narrow results.")

feed_df = load_notams_window(hours)

if selected_qcodes and len(selected_qcodes) < len(all_qcodes):
    feed_df = feed_df[feed_df["qcode"].isin(selected_qcodes)]

if feed_df.empty:
    st.info(f"No NOTAMs detected in the last {hours}h matching the selected Q-codes.")
else:
    display_df = feed_df[[
        "first_detected_at", "notam_id", "location", "country_code",
        "qcode", "restriction_type",
        "effective_start", "effective_end",
        "lat", "lon", "radius_nm",
        "text_excerpt",
    ]].copy()

    st.dataframe(
        display_df,
        column_config={
            "first_detected_at": st.column_config.TextColumn("First detected"),
            "notam_id":          st.column_config.TextColumn("NOTAM ID"),
            "location":          st.column_config.TextColumn("Location"),
            "country_code":      st.column_config.TextColumn("Country"),
            "qcode":             st.column_config.TextColumn("Q-code"),
            "restriction_type":  st.column_config.TextColumn("Type"),
            "effective_start":   st.column_config.TextColumn("Effective from"),
            "effective_end":     st.column_config.TextColumn("Effective to"),
            "lat":               st.column_config.NumberColumn("Lat", format="%.2f"),
            "lon":               st.column_config.NumberColumn("Lon", format="%.2f"),
            "radius_nm":         st.column_config.NumberColumn("Radius NM", format="%.0f"),
            "text_excerpt":      st.column_config.TextColumn("Text (excerpt)"),
        },
        hide_index=True,
        use_container_width=True,
    )
    st.caption(f"{len(feed_df)} NOTAMs shown.")

# ── Anomaly log ────────────────────────────────────────────────────────────────
st.divider()
st.subheader("Anomaly log")
st.caption(
    "New airspace restriction NOTAMs (QRTCA, QRPCA, QRDCA, QRALLT) are auto-flagged. "
    "Last 7 days shown."
)

anom_df = load_anomalies()
if anom_df.empty:
    st.info("No anomalies logged yet. They appear here when new restriction NOTAMs are first detected.")
else:
    def severity_style(val):
        if val == "HIGH":
            return "color: #ef4444; font-weight: bold"
        if val == "MEDIUM":
            return "color: #f97316"
        return "color: #eab308"

    st.dataframe(
        anom_df,
        column_config={
            "detected_at":      st.column_config.TextColumn("Detected at"),
            "notam_id":         st.column_config.TextColumn("NOTAM ID"),
            "location":         st.column_config.TextColumn("Location"),
            "country_code":     st.column_config.TextColumn("Country"),
            "qcode":            st.column_config.TextColumn("Q-code"),
            "restriction_type": st.column_config.TextColumn("Type"),
            "severity":         st.column_config.TextColumn("Severity"),
            "lat":              st.column_config.NumberColumn("Lat",    format="%.2f"),
            "lon":              st.column_config.NumberColumn("Lon",    format="%.2f"),
            "radius_nm":        st.column_config.NumberColumn("Radius", format="%.0f"),
            "text_excerpt":     st.column_config.TextColumn("Text (excerpt)"),
        },
        hide_index=True,
        use_container_width=True,
    )
    st.caption(f"{len(anom_df)} anomalies in the last 7 days.")
