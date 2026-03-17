#!/usr/bin/env python3
"""
MarketSignal — Signal Overview Dashboard
Run with: streamlit run dashboard.py
"""

import streamlit as st
import plotly.graph_objects as go
import pandas as pd
import sqlite3
import os
from datetime import datetime, timezone, timedelta

from utils.styles import inject_css, page_header, anomaly_card, status_strip, plotly_layout, axis_style

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="MarketSignal — Overview",
    layout="wide",
    initial_sidebar_state="collapsed",
)
inject_css()

ADSB_DB    = "adsb_events.db"
NOTAM_DB   = "notam_events.db"
GDELT_DB   = "gdelt_events.db"
POLY_DB    = "polymarket_markets.db"
ENGINE_DB  = "convergence_engine.db"

CONFLICT_CODES = ("15", "16", "17", "18", "19", "20")
COOP_CODES     = ("03", "04", "05", "06", "08")

# ── Helpers ────────────────────────────────────────────────────────────────────

def _db(path):
    return sqlite3.connect(path, check_same_thread=False)

def _db_exists(path):
    try:
        conn = sqlite3.connect(path)
        conn.execute("SELECT 1")
        conn.close()
        return True
    except Exception:
        return False


# ── ADS-B data ─────────────────────────────────────────────────────────────────

@st.cache_data(ttl=120, show_spinner=False)
def adsb_current_counts():
    """Latest snapshot count per region."""
    try:
        with _db(ADSB_DB) as conn:
            rows = conn.execute("""
                SELECT s.region_label, s.aircraft_count
                FROM snapshots s
                INNER JOIN (
                    SELECT region, MAX(polled_unix) AS mx FROM snapshots GROUP BY region
                ) latest ON s.region = latest.region AND s.polled_unix = latest.mx
                ORDER BY s.aircraft_count DESC
            """).fetchall()
        return rows
    except Exception:
        return []

@st.cache_data(ttl=120, show_spinner=False)
def adsb_latest_ts():
    try:
        with _db(ADSB_DB) as conn:
            ts = conn.execute("SELECT MAX(polled_at) FROM snapshots").fetchone()[0]
        return ts[:16] if ts else None
    except Exception:
        return None

@st.cache_data(ttl=120, show_spinner=False)
def adsb_anomalies_recent(days=7):
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")
        with _db(ADSB_DB) as conn:
            df = pd.read_sql_query("""
                SELECT detected_at, region_label AS location,
                       'Traffic drop ' || ROUND(drop_pct*100,0) || '%' AS detail,
                       severity, 'ADS-B' AS layer
                FROM anomalies
                WHERE detected_at >= ?
                ORDER BY detected_at DESC
                LIMIT 30
            """, conn, params=(cutoff,))
        return df
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=120, show_spinner=False)
def adsb_type_anomalies_recent(days=7):
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")
        with _db(ADSB_DB) as conn:
            df = pd.read_sql_query("""
                SELECT detected_at,
                       region || ' — ' || type_category AS location,
                       'σ=' || ROUND(sigma,1) || ' (' || observed_count || ' aircraft)' AS detail,
                       severity, 'Strategic' AS layer
                FROM type_anomalies
                WHERE detected_at >= ?
                ORDER BY detected_at DESC
                LIMIT 20
            """, conn, params=(cutoff,))
        return df
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=120, show_spinner=False)
def vip_sightings_recent(hours=24):
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")
        with _db(ADSB_DB) as conn:
            df = pd.read_sql_query("""
                SELECT seen_at AS detected_at,
                       tail_number || ' (' || country || ')' AS location,
                       region || ' @ ' || COALESCE(nearest_airport,'?') AS detail,
                       'INFO' AS severity, 'VIP' AS layer
                FROM vip_sightings
                WHERE seen_at >= ?
                ORDER BY seen_at DESC
                LIMIT 20
            """, conn, params=(cutoff,))
        return df
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=120, show_spinner=False)
def vip_dark_events_recent(days=7):
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")
        with _db(ADSB_DB) as conn:
            df = pd.read_sql_query("""
                SELECT dark_since AS detected_at,
                       tail_number || ' (' || country || ')' AS location,
                       'Last seen: ' || last_region AS detail,
                       'HIGH' AS severity, 'VIP Dark' AS layer
                FROM vip_dark_events
                WHERE dark_since >= ?
                ORDER BY dark_since DESC
                LIMIT 10
            """, conn, params=(cutoff,))
        return df
    except Exception:
        return pd.DataFrame()


# ── NOTAM data ─────────────────────────────────────────────────────────────────

@st.cache_data(ttl=120, show_spinner=False)
def notam_active_count():
    try:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
        with _db(NOTAM_DB) as conn:
            n = conn.execute("""
                SELECT COUNT(*) FROM notams
                WHERE (effective_end IS NULL OR effective_end > ? OR effective_end_interp = 'PERM')
                AND qcode LIKE 'QR%'
            """, (now,)).fetchone()[0]
        return n
    except Exception:
        return None

@st.cache_data(ttl=120, show_spinner=False)
def notam_anomalies_recent(days=7):
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")
        with _db(NOTAM_DB) as conn:
            df = pd.read_sql_query("""
                SELECT detected_at,
                       location || ' (' || country_code || ')' AS location,
                       restriction_type || ' — ' || SUBSTR(raw_text,1,60) AS detail,
                       severity, 'NOTAM' AS layer
                FROM notam_anomalies
                WHERE detected_at >= ?
                ORDER BY detected_at DESC
                LIMIT 30
            """, conn, params=(cutoff,))
        return df
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=120, show_spinner=False)
def notam_active_by_country():
    try:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
        with _db(NOTAM_DB) as conn:
            df = pd.read_sql_query("""
                SELECT country_code, COUNT(*) AS active_restrictions
                FROM notams
                WHERE (effective_end IS NULL OR effective_end > ? OR effective_end_interp = 'PERM')
                AND qcode LIKE 'QR%'
                GROUP BY country_code
                ORDER BY active_restrictions DESC
                LIMIT 15
            """, conn, params=(now,))
        return df
    except Exception:
        return pd.DataFrame()


# ── GDELT local data ───────────────────────────────────────────────────────────

@st.cache_data(ttl=3600, show_spinner=False)
def gdelt_goldstein_trend(days=30):
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y%m%d")
        with _db(GDELT_DB) as conn:
            df = pd.read_sql_query("""
                SELECT event_date,
                       ROUND(AVG(goldstein_scale),2) AS goldstein,
                       COUNT(*) AS total
                FROM events
                WHERE event_date >= ?
                GROUP BY event_date
                ORDER BY event_date
            """, conn, params=(cutoff,))
        return df
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=3600, show_spinner=False)
def gdelt_summary_stats(days=30):
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y%m%d")
        with _db(GDELT_DB) as conn:
            row = conn.execute("""
                SELECT ROUND(AVG(goldstein_scale),2),
                       SUM(CASE WHEN event_root_code IN ('15','16','17','18','19','20') THEN 1 ELSE 0 END),
                       COUNT(*)
                FROM events WHERE event_date >= ?
            """, (cutoff,)).fetchone()
        if row and row[2]:
            return {"goldstein": row[0], "conflict_pct": round(row[1]/row[2]*100,1), "total": row[2]}
        return None
    except Exception:
        return None


# ── Polymarket data ────────────────────────────────────────────────────────────

@st.cache_data(ttl=300, show_spinner=False)
def load_model_probs():
    try:
        with _db(ENGINE_DB) as conn:
            row = conn.execute("""
                SELECT escalation_prob, deescalation_prob, escalation_raw, velocity_24h
                FROM scores ORDER BY computed_at DESC LIMIT 1
            """).fetchone()
        return row if row else (0.5, 0.5, 0.0, 0.0)
    except Exception:
        return (0.5, 0.5, 0.0, 0.0)

@st.cache_data(ttl=300, show_spinner=False)
def load_polymarket_opportunities(top_n=3):
    """Returns list of dicts for the top N |edge| opportunities."""
    try:
        if not os.path.exists(POLY_DB):
            return []
        model_row = load_model_probs()
        esc_prob, deesc_prob = model_row[0], model_row[1]

        with _db(POLY_DB) as conn:
            rows = conn.execute("""
                SELECT question, slug, yes_price, volume, end_date, signal_track
                FROM markets WHERE active=1 AND yes_price IS NOT NULL
                ORDER BY volume DESC
            """).fetchall()

        scored = []
        for question, slug, yes_price, volume, end_date, track in rows:
            model_p = esc_prob if track == "escalation" else deesc_prob
            edge    = model_p - yes_price
            bet     = "Yes" if edge > 0 else "No"
            scored.append({
                "question":   question,
                "slug":       slug or "",
                "yes_pct":    round(yes_price * 100, 1),
                "model_pct":  round(model_p * 100, 1),
                "edge":       round(edge * 100, 1),
                "bet":        bet,
                "volume":     volume or 0,
                "end_date":   end_date,
                "track":      track,
            })

        scored.sort(key=lambda x: abs(x["edge"]), reverse=True)
        return scored[:top_n]
    except Exception:
        return []

@st.cache_data(ttl=300, show_spinner=False)
def load_polymarket_top_edge():
    opps = load_polymarket_opportunities(1)
    if not opps:
        return None, None
    o = opps[0]
    sign = "+" if o["edge"] >= 0 else ""
    return f"{sign}{o['edge']:.1f}%", (o["question"][:35] + "…") if len(o["question"]) > 35 else o["question"]


# ── Charts ─────────────────────────────────────────────────────────────────────

def chart_adsb_bar(rows):
    labels = [r[0].split(" /")[0] for r in rows]
    counts = [r[1] for r in rows]
    CONFLICT_REGIONS = {"Israel", "Lebanon", "Iran", "Yemen"}
    colors = [
        "rgba(239,68,68,0.75)"   if l in CONFLICT_REGIONS else
        "rgba(99,102,241,0.75)"  if l in {"Persian Gulf", "Turkey", "Saudi Arabia"} else
        "rgba(148,163,184,0.65)"
        for l in labels
    ]
    fig = go.Figure(go.Bar(
        x=labels, y=counts,
        marker_color=colors,
        hovertemplate="%{x}: %{y}<extra></extra>",
    ))
    fig.update_layout(**plotly_layout(
        height=260,
        xaxis=dict(tickangle=-20, **axis_style()),
        yaxis=axis_style(),
    ))
    return fig


def chart_gdelt_sparkline(df):
    if df.empty:
        return None
    colors = []
    for g in df["goldstein"]:
        if g < -1:
            colors.append("rgba(239,68,68,0.7)")
        elif g > 0.5:
            colors.append("rgba(34,197,94,0.7)")
        else:
            colors.append("rgba(148,163,184,0.6)")

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=df["event_date"].astype(str), y=df["total"],
        marker_color=colors,
        hovertemplate="%{x}<br>Events: %{y:,}<extra></extra>",
        name="Events",
    ))
    fig.add_trace(go.Scatter(
        x=df["event_date"].astype(str), y=df["goldstein"],
        line=dict(color="white", width=1.5),
        yaxis="y2", name="Goldstein",
        hovertemplate="Goldstein: %{y:.2f}<extra></extra>",
    ))
    fig.add_hline(y=0, line_dash="dot", line_color="rgba(255,255,255,0.2)", yref="y2")
    fig.update_layout(**plotly_layout(
        height=200,
        yaxis=dict(title="Events/day", **axis_style()),
        yaxis2=dict(overlaying="y", side="right", range=[-10, 10],
                    title="Goldstein", gridcolor="rgba(0,0,0,0)", **axis_style()),
    ))
    return fig


def chart_notam_countries(df):
    fig = go.Figure(go.Bar(
        x=df["active_restrictions"],
        y=df["country_code"],
        orientation="h",
        marker_color="rgba(251,146,60,0.75)",
        hovertemplate="%{y}: %{x} restrictions<extra></extra>",
    ))
    fig.update_layout(**plotly_layout(
        height=260,
        yaxis=dict(categoryorder="total ascending", **axis_style()),
        xaxis=axis_style(),
    ))
    return fig


# ── Unified anomaly feed ────────────────────────────────────────────────────────

def build_unified_feed():
    frames = []
    for fn in [adsb_anomalies_recent, adsb_type_anomalies_recent,
               notam_anomalies_recent, vip_dark_events_recent]:
        df = fn()
        if not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True)
    combined = combined.sort_values("detected_at", ascending=False).head(40)
    return combined


# ── Helpers (used by page layout) ──────────────────────────────────────────────

def _collector_status(ts_str, stale_minutes=30):
    if ts_str is None:
        return "offline"
    try:
        last = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - last).total_seconds() / 60
        return "live" if age < stale_minutes else "stale"
    except Exception:
        return "offline"

def _db_latest_ts(db_path, table, ts_col):
    try:
        with _db(db_path) as conn:
            row = conn.execute(f"SELECT MAX({ts_col}) FROM {table}").fetchone()
        return row[0] if row else None
    except Exception:
        return None

def _days_left(end_date_str):
    if not end_date_str:
        return None
    try:
        dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max((dt - datetime.now(timezone.utc)).days, 0)
    except Exception:
        return None


# ── Page ───────────────────────────────────────────────────────────────────────

adsb_ts   = adsb_latest_ts()
now_str   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
page_header(
    "Signal Overview",
    "Geopolitical signal monitoring for Polymarket trading — Middle East",
    timestamp=now_str,
)

if st.button("Refresh", use_container_width=False):
    st.cache_data.clear()
    st.rerun()

st.divider()

# ── Collector status strip ─────────────────────────────────────────────────────
rows_adsb     = adsb_current_counts()
total_me_ac   = sum(r[1] for r in rows_adsb) if rows_adsb else None
active_notams = notam_active_count()
gdelt_stats   = gdelt_summary_stats(30)
ais_ts        = _db_latest_ts("ais_events.db",         "vessel_snapshots", "snapshot_time")
engine_ts     = _db_latest_ts("convergence_engine.db", "scores",           "computed_at")

status_strip([
    ("ADS-B",       _collector_status(adsb_ts,   stale_minutes=30)),
    ("NOTAM",       "live" if active_notams else "offline"),
    ("GDELT",       "live" if gdelt_stats   else "offline"),
    ("Maritime",    _collector_status(ais_ts,     stale_minutes=60)),
    ("Convergence", _collector_status(engine_ts,  stale_minutes=20)),
])

st.divider()

# ── TOP POLYMARKET OPPORTUNITIES ───────────────────────────────────────────────
st.subheader("Top Polymarket Opportunities")

opps = load_polymarket_opportunities(3)
model_row = load_model_probs()
esc_prob, deesc_prob, esc_raw = model_row[0], model_row[1], model_row[2]

if not os.path.exists(POLY_DB) or not opps:
    st.info("No Polymarket data yet. Run `python3 polymarket_collector.py --loop`")
else:
    _EDGE_COLORS = {
        "high":   ("#22c55e", "rgba(34,197,94,0.08)",  "rgba(34,197,94,0.3)"),
        "medium": ("#86efac", "rgba(134,239,172,0.06)","rgba(134,239,172,0.2)"),
        "low":    ("#94a3b8", "rgba(148,163,184,0.04)","rgba(148,163,184,0.15)"),
    }
    def _edge_tier(abs_edge):
        return "high" if abs_edge >= 10 else "medium" if abs_edge >= 4 else "low"

    cols = st.columns(len(opps))
    for col, opp in zip(cols, opps):
        abs_e  = abs(opp["edge"])
        tier   = _edge_tier(abs_e)
        tc, bg, border = _EDGE_COLORS[tier]
        sign   = "+" if opp["edge"] >= 0 else ""
        vol_s  = f"${opp['volume']/1e6:.1f}M" if opp["volume"] >= 1e6 else f"${opp['volume']/1e3:.0f}K"
        exp    = _days_left(opp["end_date"])
        exp_s  = f"{exp}d left" if exp is not None else "—"
        url    = f"https://polymarket.com/event/{opp['slug']}" if opp["slug"] else "#"
        track_label = "De-esc" if opp["track"] == "deescalation" else "Esc"
        q_disp = opp["question"] if len(opp["question"]) <= 70 else opp["question"][:68] + "…"

        col.markdown(
            f'<a href="{url}" target="_blank" style="text-decoration:none">'
            f'<div style="border:1px solid {border};background:{bg};border-radius:8px;'
            f'padding:16px;height:100%;cursor:pointer">'
            f'<div style="font-size:10px;font-weight:800;letter-spacing:0.1em;'
            f'text-transform:uppercase;color:{tc};margin-bottom:8px">'
            f'BET {opp["bet"].upper()} &nbsp;·&nbsp; {track_label}</div>'
            f'<div style="font-size:13px;font-weight:500;line-height:1.4;'
            f'color:#e2e8f0;margin-bottom:12px">{q_disp}</div>'
            f'<div style="display:flex;gap:16px;flex-wrap:wrap">'
            f'<span style="font-size:11px;color:#94a3b8">Market <b style="color:#e2e8f0">{opp["yes_pct"]}%</b></span>'
            f'<span style="font-size:11px;color:#94a3b8">Model <b style="color:#e2e8f0">{opp["model_pct"]}%</b></span>'
            f'<span style="font-size:13px;font-weight:700;color:{tc}">{sign}{opp["edge"]:.1f}% edge</span>'
            f'</div>'
            f'<div style="margin-top:8px;font-size:11px;color:#64748b">'
            f'{vol_s} vol &nbsp;·&nbsp; {exp_s}</div>'
            f'</div></a>',
            unsafe_allow_html=True,
        )

    st.caption(
        "Edge = Model% − Market Yes%.  Model uses convergence engine probability.  "
        "⚠ Model is uncalibrated — treat as directional signal only, not precise probability.  "
        "Click any card to open on Polymarket."
    )

st.divider()

# ── Convergence score + key metrics ───────────────────────────────────────────
cutoff_7d  = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M")
cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M")

try:
    with _db(ADSB_DB) as conn:
        adsb_anomaly_count = conn.execute("SELECT COUNT(*) FROM anomalies WHERE detected_at >= ?", (cutoff_7d,)).fetchone()[0]
        vip_sighting_count = conn.execute("SELECT COUNT(*) FROM vip_sightings WHERE seen_at >= ?", (cutoff_24h,)).fetchone()[0]
except Exception:
    adsb_anomaly_count = vip_sighting_count = None

try:
    with _db(NOTAM_DB) as conn:
        notam_anomaly_count = conn.execute("SELECT COUNT(*) FROM notam_anomalies WHERE detected_at >= ?", (cutoff_7d,)).fetchone()[0]
except Exception:
    notam_anomaly_count = None

m1, m2, m3, m4, m5, m6 = st.columns(6)
m1.metric("Escalation P",
          f"{esc_prob*100:.1f}%" if esc_prob else "—",
          help="Convergence engine escalation probability (uncalibrated)")
m2.metric("Raw Score",
          f"{esc_raw:.1f}" if esc_raw else "—",
          help="Sum of all decayed signal weights")
m3.metric("ME Aircraft",
          total_me_ac if total_me_ac is not None else "—",
          help="Total aircraft across all ME regions, latest snapshot")
m4.metric("Active NOTAM Restrictions",
          active_notams if active_notams is not None else "—",
          help="QR* codes currently in effect")
m5.metric("GDELT Goldstein (30d)",
          f"{gdelt_stats['goldstein']:+.2f}" if gdelt_stats else "—",
          help="Average Goldstein scale — negative = conflict-skewed")
m6.metric("VIP Sightings (24h)",
          vip_sighting_count if vip_sighting_count is not None else "—",
          help="Watched tail numbers spotted in last 24 hours")

st.divider()

# ── Anomaly feed + GDELT trend ────────────────────────────────────────────────
left, right = st.columns([3, 2])

with left:
    st.subheader("Unified Anomaly Feed")
    st.caption("All signal layers · last 7 days · newest first")
    feed_df = build_unified_feed()
    if feed_df.empty:
        st.info("No anomalies yet. Collectors need a few days to build baselines.")
    else:
        for _, row in feed_df.iterrows():
            anomaly_card(
                layer=str(row.get("layer", "")),
                timestamp=str(row["detected_at"])[:16],
                severity=str(row.get("severity", "INFO")),
                location=str(row.get("location", "")),
                detail=str(row.get("detail", ""))[:80],
            )

with right:
    st.subheader("GDELT Goldstein Trend (30d)")
    st.caption("Event volume + Goldstein scale · red=conflict, green=coop")
    gdelt_df = gdelt_goldstein_trend(30)
    if gdelt_df.empty:
        st.info("GDELT data not available locally.")
    else:
        fig_g = chart_gdelt_sparkline(gdelt_df)
        if fig_g:
            st.plotly_chart(fig_g, use_container_width=True)
        if gdelt_stats:
            g1, g2 = st.columns(2)
            g1.metric("Avg Goldstein", f"{gdelt_stats['goldstein']:+.2f}")
            g2.metric("Conflict %", f"{gdelt_stats['conflict_pct']}%")

st.divider()

# ── ADS-B + NOTAM charts ──────────────────────────────────────────────────────
col_l, col_r = st.columns(2)

with col_l:
    st.subheader("ADS-B — Current by Region")
    if rows_adsb:
        st.plotly_chart(chart_adsb_bar(rows_adsb), use_container_width=True)
        st.caption(f"Last poll: {adsb_ts or '—'} UTC")
    else:
        st.info("No ADS-B data. Run `python3 adsb_collector.py --loop`")

with col_r:
    st.subheader("Active NOTAM Restrictions by Country")
    notam_country_df = notam_active_by_country()
    if notam_country_df.empty:
        st.info("No active NOTAM data.")
    else:
        st.plotly_chart(chart_notam_countries(notam_country_df), use_container_width=True)
        st.caption("Restriction Q-codes (QR*) only")

st.divider()

# ── VIP sightings ─────────────────────────────────────────────────────────────
vip_df = vip_sightings_recent(24)
if not vip_df.empty:
    st.subheader("VIP Aircraft — Last 24h")
    st.dataframe(
        vip_df[["detected_at", "location", "detail"]].rename(
            columns={"detected_at": "Seen at", "location": "Aircraft", "detail": "Region / Airport"}
        ),
        hide_index=True, use_container_width=True,
    )
    st.divider()

st.caption("ADS-B Monitor · NOTAM Monitor · Strategic Monitor · Maritime Monitor · Convergence Engine · GDELT Explorer")
