"""
MarketSignal — Shared UI utilities
Inject CSS and apply consistent Plotly layouts across all pages.
"""

import streamlit as st
import plotly.graph_objects as go


# ── Brand palette ───────────────────────────────────────────────────────────────

COLORS = {
    "red":    "#ef4444",
    "orange": "#f97316",
    "yellow": "#eab308",
    "green":  "#22c55e",
    "purple": "#a855f7",
    "cyan":   "#06b6d4",
    "indigo": "#6366f1",
    "grey":   "#94a3b8",
}

SEVERITY_COLORS = {
    "HIGH":   COLORS["red"],
    "MEDIUM": COLORS["orange"],
    "LOW":    COLORS["yellow"],
    "INFO":   COLORS["grey"],
}

LAYER_COLORS = {
    "ADS-B":     COLORS["indigo"],
    "Strategic": COLORS["purple"],
    "NOTAM":     COLORS["orange"],
    "VIP":       COLORS["cyan"],
    "VIP Dark":  COLORS["red"],
    "Maritime":  COLORS["cyan"],
}

# Neutral border / bg that work on both dark and light backgrounds
_BORDER   = "rgba(128,128,128,0.15)"
_BG_CARD  = "rgba(128,128,128,0.05)"
_BG_HOVER = "rgba(128,128,128,0.10)"
_TEXT_MID = "#64748b"   # readable on both white and dark backgrounds


# ── CSS ─────────────────────────────────────────────────────────────────────────

_CSS = """
<style>
/* === FONT === */
body, p, div, span, td, th, label, input, select, textarea, button {
    font-family: 'Inter', system-ui, -apple-system, sans-serif !important;
}

/* === HIDE STREAMLIT CHROME === */
footer { visibility: hidden; }
.stDeployButton { display: none; }

/* === TOP ACCENT BAR === */
[data-testid="stAppViewContainer"]::before {
    content: "";
    display: block;
    height: 3px;
    background: linear-gradient(90deg, #6366f1, #a855f7, #06b6d4);
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    z-index: 9999;
}

/* === METRIC CARDS === */
[data-testid="metric-container"] {
    background: var(--secondary-background-color, rgba(128,128,128,0.05));
    border: 1px solid rgba(128,128,128,0.15);
    border-radius: 8px;
    padding: 12px 16px;
    transition: border-color 0.2s ease, background 0.2s ease;
}
[data-testid="metric-container"]:hover {
    border-color: rgba(128,128,128,0.30);
    background: rgba(128,128,128,0.10);
}
[data-testid="metric-container"] label {
    font-size: 11px !important;
    font-weight: 600 !important;
    letter-spacing: 0.06em !important;
    text-transform: uppercase !important;
    color: #64748b !important;
}
[data-testid="metric-container"] [data-testid="stMetricValue"] {
    font-size: 22px !important;
    font-weight: 700 !important;
    letter-spacing: -0.02em !important;
}

/* === SUBHEADERS === */
h3 {
    font-size: 15px !important;
    font-weight: 600 !important;
    letter-spacing: -0.01em !important;
    color: #e2e8f0 !important;
    border-bottom: 1px solid rgba(255,255,255,0.07);
    padding-bottom: 8px;
    margin-bottom: 16px !important;
}

/* === DATAFRAME === */
[data-testid="stDataFrame"] table {
    font-size: 13px !important;
}
[data-testid="stDataFrame"] td,
[data-testid="stDataFrame"] th {
    padding: 4px 10px !important;
    line-height: 1.4 !important;
}
[data-testid="stDataFrame"] th {
    font-size: 12px !important;
    font-weight: 600 !important;
    letter-spacing: 0.02em !important;
    color: #94a3b8 !important;
    background: var(--secondary-background-color, rgba(128,128,128,0.05)) !important;
}

/* === SIDEBAR === */
[data-testid="stSidebar"] {
    border-right: 1px solid rgba(128,128,128,0.12);
}
[data-testid="stSidebar"] hr {
    border-color: rgba(128,128,128,0.12) !important;
    margin: 10px 0 !important;
}

/* === DIVIDERS === */
hr[data-testid="stDivider"] {
    border-color: rgba(128,128,128,0.15) !important;
    margin: 18px 0 !important;
}

/* === SCROLLBAR === */
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: rgba(128,128,128,0.08); }
::-webkit-scrollbar-thumb { background: rgba(128,128,128,0.25); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: rgba(128,128,128,0.40); }

/* === EXPANDERS === */
[data-testid="stExpander"] {
    border: 1px solid rgba(128,128,128,0.15) !important;
    border-radius: 6px !important;
}
[data-testid="stExpander"] summary {
    font-size: 13px !important;
    font-weight: 600 !important;
    letter-spacing: 0.01em !important;
    color: #94a3b8 !important;
}

/* === BUTTONS === */
[data-testid="stButton"] button {
    font-size: 11px !important;
    font-weight: 600 !important;
    letter-spacing: 0.04em !important;
}
</style>
"""


def inject_css():
    """Call once per page immediately after st.set_page_config()."""
    st.markdown(_CSS, unsafe_allow_html=True)


# ── Plotly helpers ──────────────────────────────────────────────────────────────

_PLOTLY_BASE = dict(
    template="plotly_dark",
    font=dict(family="Inter, system-ui, sans-serif", size=12, color="#64748b"),
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    margin=dict(l=0, r=0, t=10, b=0),
    legend=dict(
        orientation="h",
        y=1.12,
        font=dict(size=11, color="#64748b"),
        bgcolor="rgba(0,0,0,0)",
    ),
    hovermode="x unified",
    hoverlabel=dict(
        bgcolor="rgba(20,23,34,0.97)",
        bordercolor="rgba(128,128,128,0.2)",
        font_size=12,
        font_family="Inter, system-ui, sans-serif",
    ),
    modebar_remove=[
        "zoom", "pan", "select", "lasso2d", "zoomIn2d", "zoomOut2d",
        "autoScale2d", "resetScale2d", "toImage",
    ],
)

_AXIS_STYLE = dict(
    gridcolor="rgba(128,128,128,0.12)",
    linecolor="rgba(128,128,128,0.15)",
    tickfont=dict(size=11, color="#64748b"),
    title_font=dict(size=11, color="#64748b"),
    zeroline=False,
)


def plotly_layout(height=300, **overrides):
    """
    Returns a dict of Plotly layout kwargs for fig.update_layout().

    Usage:
        fig.update_layout(**plotly_layout(height=360))
    """
    layout = dict(**_PLOTLY_BASE, height=height)
    layout.update(overrides)
    return layout


def axis_style(**overrides):
    """Returns axis styling dict for use in fig.update_layout(yaxis=axis_style())."""
    style = dict(**_AXIS_STYLE)
    style.update(overrides)
    return style


# ── Page header ─────────────────────────────────────────────────────────────────

def page_header(title: str, subtitle: str = "", timestamp: str = ""):
    """Branded header with MARKETSIGNAL wordmark, page title, and optional timestamp."""
    ts_html = (
        f'<span style="color:{_TEXT_MID};font-size:11px;letter-spacing:0.04em;'
        f'font-weight:500;white-space:nowrap">{timestamp}</span>'
        if timestamp else ""
    )
    sub_html = (
        f'<div style="font-size:13px;color:{_TEXT_MID};margin-top:4px;font-weight:400">'
        f'{subtitle}</div>'
        if subtitle else ""
    )
    st.markdown(
        f'<div style="display:flex;justify-content:space-between;align-items:flex-start;'
        f'border-bottom:1px solid {_BORDER};padding-bottom:16px;margin-bottom:22px;'
        f'padding-top:8px">'
        f'<div>'
        f'<div style="font-size:10px;font-weight:800;letter-spacing:0.14em;'
        f'text-transform:uppercase;color:#6366f1;margin-bottom:5px">MARKETSIGNAL</div>'
        f'<div style="font-size:24px;font-weight:700;letter-spacing:-0.025em;'
        f'line-height:1">{title}</div>'
        f'{sub_html}'
        f'</div>'
        f'<div style="padding-top:6px">{ts_html}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )


# ── Anomaly feed card ────────────────────────────────────────────────────────────

def anomaly_card(layer: str, timestamp: str, severity: str, location: str, detail: str):
    """Styled anomaly feed card with left border, layer badge, and severity tag."""
    sev_color   = SEVERITY_COLORS.get(severity, COLORS["grey"])
    layer_color = LAYER_COLORS.get(layer, COLORS["grey"])

    st.markdown(
        f'<div style="border-left:3px solid {sev_color};padding:8px 14px;'
        f'margin-bottom:6px;background:{_BG_CARD};border-radius:0 5px 5px 0">'
        f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">'
        f'<span style="background:{layer_color}20;color:{layer_color};font-size:9px;'
        f'font-weight:800;letter-spacing:0.08em;text-transform:uppercase;'
        f'padding:2px 7px;border-radius:3px;border:1px solid {layer_color}40">{layer}</span>'
        f'<span style="color:{_TEXT_MID};font-size:11px">{timestamp}</span>'
        f'<span style="color:{sev_color};font-size:9px;font-weight:800;'
        f'letter-spacing:0.07em;text-transform:uppercase;margin-left:auto">{severity}</span>'
        f'</div>'
        f'<div style="font-size:13px;font-weight:500;line-height:1.3">'
        f'{location}</div>'
        f'<div style="font-size:12px;color:{_TEXT_MID};margin-top:2px;line-height:1.3">'
        f'{detail}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )


# ── Collector status strip ───────────────────────────────────────────────────────

def status_strip(collectors: list):
    """
    Horizontal row of glowing status dots for collector health.

    Args:
        collectors: list of (label, status) tuples.
                    status: "live" | "stale" | "offline"
    """
    STATUS_META = {
        "live":    ("#22c55e", "LIVE"),
        "stale":   ("#eab308", "STALE"),
        "offline": ("#94a3b8", "OFFLINE"),
    }
    dots = ""
    for label, status in collectors:
        color, badge = STATUS_META.get(status, ("#94a3b8", "UNKNOWN"))
        glow = f"0 0 6px {color}99" if status == "live" else "none"
        dots += (
            f'<div style="display:flex;align-items:center;gap:7px;'
            f'padding:5px 12px;background:{_BG_CARD};'
            f'border:1px solid {_BORDER};border-radius:20px">'
            f'<span style="width:6px;height:6px;border-radius:50%;background:{color};'
            f'box-shadow:{glow};display:inline-block;flex-shrink:0"></span>'
            f'<span style="font-size:11px;font-weight:600;letter-spacing:0.05em;'
            f'text-transform:uppercase;color:{_TEXT_MID}">{label}</span>'
            f'<span style="font-size:9px;font-weight:800;color:{color};'
            f'letter-spacing:0.07em">{badge}</span>'
            f'</div>'
        )
    st.markdown(
        f'<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px">'
        f'{dots}</div>',
        unsafe_allow_html=True,
    )
