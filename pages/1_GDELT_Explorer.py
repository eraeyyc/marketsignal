#!/usr/bin/env python3
"""
MarketSignal — GDELT Explorer
Run with: streamlit run dashboard.py
"""

import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
from google.cloud import bigquery
from datetime import date

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="MarketSignal — GDELT Explorer",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Constants ──────────────────────────────────────────────────────────────────
CREDENTIALS_FILE = "gdelt_credentials.json"

CAMEO_LABELS = {
    "03": "Intent to cooperate",
    "04": "Consult",
    "05": "Diplomatic cooperation",
    "06": "Material cooperation",
    "08": "Yield / de-escalate",
    "09": "Investigate",
    "10": "Demand",
    "11": "Disapprove",
    "12": "Reject",
    "13": "Threaten",
    "14": "Protest",
    "15": "Exhibit force posture",
    "16": "Reduce relations",
    "17": "Coerce",
    "18": "Assault",
    "19": "Fight",
    "20": "Unconventional mass violence",
}

COUNTRIES = {
    "ISR": "Israel",
    "PSE": "Palestine / Gaza",
    "IRN": "Iran",
    "LBN": "Lebanon",
    "SYR": "Syria",
    "YEM": "Yemen",
    "SAU": "Saudi Arabia",
    "JOR": "Jordan",
    "EGY": "Egypt",
    "QAT": "Qatar",
    "ARE": "UAE",
    "TUR": "Turkey",
    "USA": "United States",
    "RUS": "Russia",
    "CHN": "China",
}

ALL_ACTORS     = list(COUNTRIES.keys())
CONFLICT_CODES = ("15", "16", "17", "18", "19", "20")
COOP_CODES     = ("03", "04", "05", "06", "08")
EXCLUDED_CODES = ("01", "02", "07")  # statements, appeals, humanitarian aid


# ── BigQuery ───────────────────────────────────────────────────────────────────
@st.cache_resource
def get_client():
    return bigquery.Client.from_service_account_json(CREDENTIALS_FILE)


def build_where(start_int, end_int, countries, codes, min_articles):
    actor_list = "', '".join(ALL_ACTORS)
    clause = f"""
        WHERE SQLDATE BETWEEN {start_int} AND {end_int}
          AND NumArticles >= {min_articles}
          AND EventRootCode NOT IN {EXCLUDED_CODES}
          AND (
              Actor1CountryCode IN ('{actor_list}')
              OR Actor2CountryCode IN ('{actor_list}')
          )
    """
    if countries:
        c = "', '".join(countries)
        clause += f"  AND (Actor1CountryCode IN ('{c}') OR Actor2CountryCode IN ('{c}'))\n"
    if codes:
        c = "', '".join(codes)
        clause += f"  AND EventRootCode IN ('{c}')\n"
    return clause


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_daily(_client, start_int, end_int, countries, codes, min_articles):
    where = build_where(start_int, end_int, countries, codes, min_articles)
    q = f"""
        SELECT
            CAST(SQLDATE AS STRING)                                                          AS event_date,
            COUNT(*)                                                                         AS total,
            ROUND(AVG(GoldsteinScale), 2)                                                   AS goldstein,
            SUM(CASE WHEN EventRootCode IN {CONFLICT_CODES} THEN 1 ELSE 0 END)              AS conflict,
            SUM(CASE WHEN EventRootCode IN {COOP_CODES}     THEN 1 ELSE 0 END)              AS coop
        FROM `gdelt-bq.gdeltv2.events`
        {where}
        GROUP BY event_date
        ORDER BY event_date
    """
    return _client.query(q).to_dataframe()


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_breakdown(_client, start_int, end_int, countries, codes, min_articles):
    where = build_where(start_int, end_int, countries, codes, min_articles)
    q = f"""
        SELECT EventRootCode AS code, COUNT(*) AS n
        FROM `gdelt-bq.gdeltv2.events`
        {where}
        GROUP BY code
        ORDER BY n DESC
    """
    df = _client.query(q).to_dataframe()
    df["label"] = df["code"].map(CAMEO_LABELS).fillna("Unknown")
    return df


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_top_events(_client, start_int, end_int, countries, codes, min_articles):
    where = build_where(start_int, end_int, countries, codes, min_articles)
    q = f"""
        SELECT
            CAST(SQLDATE AS STRING)  AS date,
            Actor1CountryCode        AS actor1,
            Actor2CountryCode        AS actor2,
            EventRootCode            AS code,
            ROUND(GoldsteinScale, 1) AS goldstein,
            NumArticles              AS articles,
            ActionGeo_FullName       AS location,
            SOURCEURL                AS url
        FROM `gdelt-bq.gdeltv2.events`
        {where}
        ORDER BY NumArticles DESC
        LIMIT 200
    """
    df = _client.query(q).to_dataframe()
    df["event_type"] = df["code"].map(CAMEO_LABELS).fillna(df["code"])
    return df


# ── Charts ─────────────────────────────────────────────────────────────────────
def bar_color(g):
    if g < -1:
        return "rgba(239, 68, 68, 0.75)"   # red — conflict
    if g > 0.5:
        return "rgba(34, 197, 94, 0.75)"   # green — cooperation
    return "rgba(148, 163, 184, 0.6)"      # grey — neutral


def chart_timeline(df):
    colors = df["goldstein"].apply(bar_color).tolist()

    fig = make_subplots(specs=[[{"secondary_y": True}]])

    fig.add_trace(
        go.Bar(
            x=df["event_date"], y=df["total"],
            name="Events / day",
            marker_color=colors,
            hovertemplate="%{x}<br>Events: %{y:,}<extra></extra>",
        ),
        secondary_y=False,
    )
    fig.add_trace(
        go.Scatter(
            x=df["event_date"], y=df["goldstein"],
            name="Goldstein",
            line=dict(color="white", width=2),
            hovertemplate="%{x}<br>Goldstein: %{y:.2f}<extra></extra>",
        ),
        secondary_y=True,
    )
    fig.add_hline(y=0, line_dash="dot", line_color="rgba(255,255,255,0.25)", secondary_y=True)

    fig.update_layout(
        template="plotly_dark",
        height=360,
        margin=dict(l=0, r=0, t=10, b=0),
        legend=dict(orientation="h", y=1.08),
        hovermode="x unified",
        bargap=0.1,
    )
    fig.update_yaxes(title_text="Events / day", secondary_y=False, gridcolor="rgba(255,255,255,0.05)")
    fig.update_yaxes(title_text="Goldstein scale", secondary_y=True, range=[-10, 10], gridcolor="rgba(0,0,0,0)")

    return fig


def chart_breakdown(df):
    colors = [
        "rgba(239, 68, 68, 0.8)"  if c in CONFLICT_CODES else
        "rgba(34, 197, 94, 0.8)"  if c in COOP_CODES     else
        "rgba(148, 163, 184, 0.7)"
        for c in df["code"]
    ]
    fig = go.Figure(go.Bar(
        x=df["n"],
        y=df["label"],
        orientation="h",
        marker_color=colors,
        hovertemplate="%{y}: %{x:,}<extra></extra>",
    ))
    fig.update_layout(
        template="plotly_dark",
        height=360,
        margin=dict(l=0, r=0, t=10, b=0),
        yaxis=dict(categoryorder="total ascending"),
        xaxis=dict(gridcolor="rgba(255,255,255,0.05)"),
    )
    return fig


# ── Sidebar ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## MarketSignal")
    st.caption("GDELT Middle East Explorer")
    st.divider()

    col_a, col_b = st.columns(2)
    with col_a:
        start_date = st.date_input("From", value=date(2023, 10, 1), min_value=date(2023, 1, 1))
    with col_b:
        end_date = st.date_input("To", value=date.today())

    selected_countries = st.multiselect(
        "Countries (actor filter)",
        options=ALL_ACTORS,
        format_func=lambda c: f"{c} — {COUNTRIES[c]}",
        placeholder="All countries",
    )

    selected_codes = st.multiselect(
        "Event types",
        options=list(CAMEO_LABELS.keys()),
        format_func=lambda c: f"{c} — {CAMEO_LABELS[c]}",
        placeholder="All types",
    )

    min_articles = st.slider("Min. article coverage", min_value=1, max_value=50, value=10)

    run_btn = st.button("Run Query", type="primary", use_container_width=True)

    st.divider()
    st.caption("Source: GDELT v2 via BigQuery\nResults cached 1 hour per filter set.")


# ── Main ───────────────────────────────────────────────────────────────────────
st.markdown("## GDELT Middle East Explorer")

if run_btn:
    if end_date <= start_date:
        st.error("End date must be after start date.")
        st.stop()

    client    = get_client()
    start_int = int(start_date.strftime("%Y%m%d"))
    end_int   = int(end_date.strftime("%Y%m%d"))
    params    = (start_int, end_int, tuple(selected_countries), tuple(selected_codes), min_articles)

    with st.spinner("Querying BigQuery — usually 15–30 seconds..."):
        st.session_state["daily_df"]     = fetch_daily(client, *params)
        st.session_state["breakdown_df"] = fetch_breakdown(client, *params)
        st.session_state["events_df"]    = fetch_top_events(client, *params)
        st.session_state["query_label"]  = (
            f"{start_date.strftime('%d %b %Y')} → {end_date.strftime('%d %b %Y')}"
            + (f"  |  {', '.join(selected_countries)}" if selected_countries else "")
            + (f"  |  codes {', '.join(selected_codes)}" if selected_codes else "")
        )

if "daily_df" not in st.session_state:
    st.info("Set your filters in the sidebar and click **Run Query**.")
    st.stop()

daily_df     = st.session_state["daily_df"]
breakdown_df = st.session_state["breakdown_df"]
events_df    = st.session_state["events_df"]

if daily_df.empty:
    st.warning("No events found for this filter combination. Try widening the date range or removing filters.")
    st.stop()

# ── Metrics ────────────────────────────────────────────────────────────────────
st.caption(st.session_state.get("query_label", ""))

total        = int(daily_df["total"].sum())
avg_g        = round(float(daily_df["goldstein"].mean()), 2)
conflict_pct = round(daily_df["conflict"].sum() / total * 100, 1) if total else 0
coop_pct     = round(daily_df["coop"].sum()     / total * 100, 1) if total else 0

m1, m2, m3, m4 = st.columns(4)
m1.metric("Total events",   f"{total:,}")
m2.metric("Avg Goldstein",  avg_g)
m3.metric("Conflict %",     f"{conflict_pct}%")
m4.metric("Cooperation %",  f"{coop_pct}%")

st.divider()

# ── Charts ─────────────────────────────────────────────────────────────────────
left, right = st.columns([2, 1])
with left:
    st.subheader("Event volume + Goldstein trend")
    st.caption("Bar colour: red = conflict-skewed day, green = cooperation-skewed, grey = neutral")
    st.plotly_chart(chart_timeline(daily_df), use_container_width=True)
with right:
    st.subheader("Event type breakdown")
    st.plotly_chart(chart_breakdown(breakdown_df), use_container_width=True)

st.divider()

# ── Events table ───────────────────────────────────────────────────────────────
st.subheader(f"Top events by media coverage")
st.caption("Top 200 events for selected period, ranked by number of articles. Click source to open.")

st.dataframe(
    events_df[["date", "actor1", "actor2", "event_type", "goldstein", "articles", "location", "url"]],
    column_config={
        "date":       st.column_config.TextColumn("Date"),
        "actor1":     st.column_config.TextColumn("Actor 1"),
        "actor2":     st.column_config.TextColumn("Actor 2"),
        "event_type": st.column_config.TextColumn("Event type"),
        "goldstein":  st.column_config.NumberColumn("Goldstein", format="%.1f"),
        "articles":   st.column_config.NumberColumn("Articles"),
        "location":   st.column_config.TextColumn("Location"),
        "url":        st.column_config.LinkColumn("Source", display_text="open"),
    },
    use_container_width=True,
    hide_index=True,
)
