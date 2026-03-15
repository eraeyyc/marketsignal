#!/usr/bin/env python3
"""
MarketSignal — Route Monitor page

Shows commercial airline route suspensions across Middle East airport pairs.
Sustained suspensions (>60% drop, 3+ consecutive days) are flagged as
route_suspension signals feeding the convergence engine.
"""

import streamlit as st
import plotly.graph_objects as go
import pandas as pd
import sqlite3
from datetime import datetime, timedelta, timezone

st.set_page_config(
    page_title="MarketSignal — Route Monitor",
    layout="wide",
    initial_sidebar_state="expanded",
)

DB_PATH = "route_events.db"

WATCHED_AIRLINES = {
    "LY": "El Al",
    "BA": "British Airways",
    "AF": "Air France",
    "LH": "Lufthansa",
    "EK": "Emirates",
    "QR": "Qatar Airways",
    "TK": "Turkish Airlines",
    "FR": "Ryanair",
    "DL": "Delta",
    "UA": "United",
    "EY": "Etihad",
    "SV": "Saudia",
    "ME": "Middle East Airlines",
}


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def _db_ready():
    try:
        with _conn() as c:
            c.execute("SELECT 1 FROM route_suspensions LIMIT 1")
        return True
    except Exception:
        return False


# ── Data loaders ───────────────────────────────────────────────────────────────

@st.cache_data(ttl=300, show_spinner=False)
def load_meta():
    with _conn() as c:
        total_susp  = c.execute("SELECT COUNT(*) FROM route_suspensions").fetchone()[0]
        active_susp = c.execute("SELECT COUNT(*) FROM route_suspensions WHERE resolved_at IS NULL").fetchone()[0]
        total_sched = c.execute("SELECT COUNT(*) FROM route_schedules").fetchone()[0]
        total_daily = c.execute("SELECT COUNT(*) FROM route_daily").fetchone()[0]
        last_poll   = c.execute("SELECT MAX(polled_at) FROM route_daily").fetchone()[0]
    return total_susp, active_susp, total_sched, total_daily, last_poll


@st.cache_data(ttl=300, show_spinner=False)
def load_active_suspensions():
    with _conn() as c:
        return pd.read_sql_query("""
            SELECT airline, airline_name, dep, arr,
                   first_detected_at, last_confirmed_at,
                   consecutive_days, drop_pct, severity
            FROM route_suspensions
            WHERE resolved_at IS NULL
            ORDER BY drop_pct DESC
        """, c)


@st.cache_data(ttl=300, show_spinner=False)
def load_suspension_history(days):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _conn() as c:
        return pd.read_sql_query("""
            SELECT airline, airline_name, dep, arr,
                   first_detected_at, resolved_at,
                   consecutive_days, drop_pct, severity
            FROM route_suspensions
            WHERE first_detected_at >= ?
            ORDER BY first_detected_at DESC
        """, c, params=(cutoff,))


@st.cache_data(ttl=300, show_spinner=False)
def load_daily_for_route(dep, arr, airline, days=14):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    with _conn() as c:
        return pd.read_sql_query("""
            SELECT flight_date, scheduled_count, operated_count, cancelled_count
            FROM route_daily
            WHERE dep = ? AND arr = ? AND airline = ?
              AND flight_date >= ?
            ORDER BY flight_date ASC
        """, c, params=(dep, arr, airline, cutoff))


@st.cache_data(ttl=300, show_spinner=False)
def load_all_monitored_routes():
    with _conn() as c:
        return pd.read_sql_query("""
            SELECT dep, arr, airline, airline_name, flights_per_day, cached_at
            FROM route_schedules
            WHERE flights_per_day > 0
            ORDER BY flights_per_day DESC
        """, c)


@st.cache_data(ttl=300, show_spinner=False)
def load_operated_heatmap(days=14):
    """Returns a pivot: flight_date × route, value = operated/scheduled ratio."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    with _conn() as c:
        df = pd.read_sql_query("""
            SELECT flight_date,
                   airline || ' ' || dep || '-' || arr AS route,
                   CASE WHEN scheduled_count > 0
                        THEN CAST(operated_count AS REAL) / scheduled_count
                        ELSE NULL END AS ratio
            FROM route_daily
            WHERE flight_date >= ?
        """, c, params=(cutoff,))
    if df.empty:
        return df
    return df.pivot_table(index="route", columns="flight_date", values="ratio")


# ── Charts ─────────────────────────────────────────────────────────────────────

def chart_route_timeseries(df, dep, arr, airline):
    """Operated vs scheduled bar chart for a single route."""
    if df.empty:
        return None

    scheduled = df["scheduled_count"].fillna(0)
    operated  = df["operated_count"].fillna(0)
    cancelled = df["cancelled_count"].fillna(0)
    dates     = df["flight_date"]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=dates, y=scheduled,
        name="Scheduled",
        marker_color="rgba(100, 116, 139, 0.5)",
    ))
    fig.add_trace(go.Bar(
        x=dates, y=operated,
        name="Operated",
        marker_color="rgba(34, 197, 94, 0.8)",
    ))
    fig.add_trace(go.Bar(
        x=dates, y=cancelled,
        name="Cancelled",
        marker_color="rgba(239, 68, 68, 0.8)",
    ))
    fig.update_layout(
        template="plotly_dark",
        barmode="overlay",
        height=300,
        margin=dict(l=0, r=0, t=10, b=0),
        legend=dict(orientation="h", y=1.05),
        xaxis_title=None,
        yaxis_title="Flights",
    )
    return fig


def chart_suspension_timeline(df):
    """Gantt-style timeline of suspension events."""
    if df.empty:
        return None

    now_str = datetime.now(timezone.utc).isoformat()
    fig = go.Figure()

    for _, row in df.iterrows():
        label    = f"{row['airline_name']} {row['dep']}-{row['arr']}"
        start    = row["first_detected_at"][:10]
        end      = row["resolved_at"][:10] if row["resolved_at"] else now_str[:10]
        color    = "rgba(239, 68, 68, 0.8)" if row["severity"] == "HIGH" else "rgba(249, 115, 22, 0.8)"
        status   = "active" if not row["resolved_at"] else "resolved"
        hover    = (
            f"<b>{label}</b><br>"
            f"{start} → {end}<br>"
            f"Drop: {row['drop_pct']*100:.0f}%  |  {row['consecutive_days']}d  |  {row['severity']}<br>"
            f"Status: {status}"
        )
        fig.add_trace(go.Scatter(
            x=[start, end],
            y=[label, label],
            mode="lines",
            line=dict(color=color, width=12),
            hovertext=hover,
            hoverinfo="text",
            showlegend=False,
        ))

    fig.update_layout(
        template="plotly_dark",
        height=max(200, len(df) * 40 + 60),
        margin=dict(l=0, r=0, t=10, b=0),
        xaxis_title=None,
        yaxis=dict(autorange="reversed"),
    )
    return fig


# ── Page ───────────────────────────────────────────────────────────────────────

st.markdown("## Route Monitor")
st.caption("Commercial airline route suspensions across Middle East airport pairs — Cirium Flex API")

if not _db_ready():
    st.error("No route data yet. Run `python3 route_collector.py --refresh` then `--loop` to start collecting.")
    st.stop()

try:
    total_susp, active_susp, total_sched, total_daily, last_poll = load_meta()
except Exception:
    st.error("Route database exists but could not be read.")
    st.stop()

# ── Sidebar ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## MarketSignal")
    st.caption("Route Monitor")
    st.divider()

    history_days = st.selectbox("History window", [7, 14, 30], index=1,
                                format_func=lambda d: f"Last {d} days")
    st.divider()
    st.caption(f"Monitored routes: {total_sched:,}")
    st.caption(f"Daily records: {total_daily:,}")
    st.caption(f"Last poll: {last_poll[:16] if last_poll else '—'}")

    if st.button("Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

# ── Metrics ────────────────────────────────────────────────────────────────────
c1, c2, c3, c4 = st.columns(4)
c1.metric("Active suspensions",  active_susp,
          help="Routes with >60% drop for 3+ consecutive days, not yet resolved")
c2.metric("Total logged",        total_susp)
c3.metric("Routes monitored",    total_sched,
          help="Route/airline pairs with scheduled service in Cirium cache")
c4.metric("Days of data",        total_daily // max(total_sched, 1) if total_sched else 0,
          help="Average days of daily records per route")

# ── Active suspensions ─────────────────────────────────────────────────────────
st.divider()
st.subheader("Active suspensions")
st.caption("Routes where operated flights have dropped >60% below schedule for 3+ consecutive days.")

active_df = load_active_suspensions()
if active_df.empty:
    st.info("No active route suspensions detected. This is the normal state — suspensions appear here when airlines go quiet.")
else:
    display = active_df.copy()
    display["drop_pct"] = (display["drop_pct"] * 100).round(1).astype(str) + "%"
    display["route"]    = display["dep"] + " → " + display["arr"]
    st.dataframe(
        display[["severity", "airline_name", "route", "consecutive_days",
                 "drop_pct", "first_detected_at", "last_confirmed_at"]],
        column_config={
            "severity":          st.column_config.TextColumn("Severity"),
            "airline_name":      st.column_config.TextColumn("Airline"),
            "route":             st.column_config.TextColumn("Route"),
            "consecutive_days":  st.column_config.NumberColumn("Days", format="%d"),
            "drop_pct":          st.column_config.TextColumn("Drop"),
            "first_detected_at": st.column_config.TextColumn("First detected"),
            "last_confirmed_at": st.column_config.TextColumn("Last confirmed"),
        },
        hide_index=True,
        use_container_width=True,
    )

    # Drill-down chart for selected suspension
    st.divider()
    st.subheader("Route drill-down")
    options = [
        f"{row['airline_name']} — {row['dep']} → {row['arr']}"
        for _, row in active_df.iterrows()
    ]
    selected = st.selectbox("Select a suspended route", options)
    if selected:
        idx = options.index(selected)
        row = active_df.iloc[idx]
        daily_df = load_daily_for_route(row["dep"], row["arr"], row["airline"])
        fig = chart_route_timeseries(daily_df, row["dep"], row["arr"], row["airline"])
        if fig:
            st.plotly_chart(fig, use_container_width=True)
            st.caption(
                f"{row['airline_name']} {row['dep']}–{row['arr']} | "
                f"Scheduled baseline: {row.get('flights_per_day', '—')}/day | "
                f"Current drop: {float(str(row['drop_pct']).replace('%','')):.0f}% over {row['consecutive_days']} days"
                if "%" not in str(row['drop_pct'])
                else f"{row['airline_name']} {row['dep']}–{row['arr']} | "
                     f"Current drop: {row['drop_pct']} over {row['consecutive_days']} days"
            )

# ── Suspension timeline ────────────────────────────────────────────────────────
st.divider()
st.subheader(f"Suspension history — last {history_days} days")

hist_df = load_suspension_history(history_days)
if hist_df.empty:
    st.info(f"No suspensions logged in the last {history_days} days.")
else:
    fig = chart_suspension_timeline(hist_df)
    if fig:
        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            f"Red = HIGH severity (>80% drop) | Orange = MEDIUM (60–80% drop) | "
            f"Bar extends to today if still active."
        )

    with st.expander(f"Full history table ({len(hist_df)} records)"):
        display = hist_df.copy()
        display["drop_pct"] = (display["drop_pct"] * 100).round(1).astype(str) + "%"
        display["route"]    = display["dep"] + " → " + display["arr"]
        display["status"]   = display["resolved_at"].apply(
            lambda x: "Resolved" if x else "⚠ Active"
        )
        st.dataframe(
            display[["severity", "status", "airline_name", "route", "consecutive_days",
                      "drop_pct", "first_detected_at", "resolved_at"]],
            column_config={
                "severity":          st.column_config.TextColumn("Severity"),
                "status":            st.column_config.TextColumn("Status"),
                "airline_name":      st.column_config.TextColumn("Airline"),
                "route":             st.column_config.TextColumn("Route"),
                "consecutive_days":  st.column_config.NumberColumn("Days", format="%d"),
                "drop_pct":          st.column_config.TextColumn("Drop"),
                "first_detected_at": st.column_config.TextColumn("First detected"),
                "resolved_at":       st.column_config.TextColumn("Resolved at"),
            },
            hide_index=True,
            use_container_width=True,
        )

# ── Monitored routes ───────────────────────────────────────────────────────────
st.divider()
st.subheader("Schedule cache")
st.caption("Routes with active scheduled service detected by Cirium Flex API. Refreshed weekly.")

with st.expander(f"All monitored routes ({total_sched})"):
    sched_df = load_all_monitored_routes()
    if sched_df.empty:
        st.info("No schedule data. Run `python3 route_collector.py --refresh`.")
    else:
        sched_df["route"] = sched_df["dep"] + " → " + sched_df["arr"]
        sched_df["airline_label"] = sched_df["airline"] + " — " + sched_df["airline_name"]
        st.dataframe(
            sched_df[["airline_label", "route", "flights_per_day", "cached_at"]],
            column_config={
                "airline_label":   st.column_config.TextColumn("Airline"),
                "route":           st.column_config.TextColumn("Route"),
                "flights_per_day": st.column_config.NumberColumn("Avg flights/day", format="%.2f"),
                "cached_at":       st.column_config.TextColumn("Cache updated"),
            },
            hide_index=True,
            use_container_width=True,
        )
