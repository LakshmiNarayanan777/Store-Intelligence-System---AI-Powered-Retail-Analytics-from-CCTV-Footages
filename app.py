"""
dashboard/app.py — Live Store Intelligence Dashboard

Shows real-time metrics from the API, auto-refreshing every 5 seconds.
This satisfies Part E (bonus points) of the challenge.

TEACHING:
  Streamlit reruns the whole script on each refresh cycle.
  st.empty() lets us update widgets in-place without flicker.
  We call the API (not the DB directly) — the dashboard is a pure API consumer.
"""

import os
import time
import requests
import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd
from datetime import datetime

API_URL = os.environ.get("API_URL", "http://localhost:8000")
STORE_ID = "STORE_BLR_002"
REFRESH_SECONDS = 5


# ── API helpers ───────────────────────────────────────────────────────────────

def fetch(path: str):
    try:
        r = requests.get(f"{API_URL}{path}", timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return None


# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Store Intelligence — Purplle",
    page_icon="🛍️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
    .metric-card {
        background: #1e1e2e;
        border-radius: 12px;
        padding: 20px;
        text-align: center;
    }
    .big-number { font-size: 2.5rem; font-weight: 700; color: #cba6f7; }
    .metric-label { font-size: 0.9rem; color: #a6adc8; margin-top: 4px; }
    .anomaly-critical { border-left: 4px solid #f38ba8; padding: 8px 12px; background: #1e1e2e; border-radius: 4px; margin: 4px 0; }
    .anomaly-warn { border-left: 4px solid #fab387; padding: 8px 12px; background: #1e1e2e; border-radius: 4px; margin: 4px 0; }
    .anomaly-info { border-left: 4px solid #89b4fa; padding: 8px 12px; background: #1e1e2e; border-radius: 4px; margin: 4px 0; }
</style>
""", unsafe_allow_html=True)


# ── Header ────────────────────────────────────────────────────────────────────

col_title, col_status = st.columns([3, 1])
with col_title:
    st.title("🛍️ Store Intelligence Dashboard")
    st.caption(f"Store: {STORE_ID} · Auto-refreshing every {REFRESH_SECONDS}s")

with col_status:
    health = fetch("/health")
    if health and health.get("status") == "ok":
        st.success("🟢 API Online")
    else:
        st.error("🔴 API Offline")

st.divider()


# ── Fetch all data ────────────────────────────────────────────────────────────

metrics  = fetch(f"/stores/{STORE_ID}/metrics")
funnel   = fetch(f"/stores/{STORE_ID}/funnel")
heatmap  = fetch(f"/stores/{STORE_ID}/heatmap")
anomalies = fetch(f"/stores/{STORE_ID}/anomalies")


# ── Key Metrics Row ───────────────────────────────────────────────────────────

st.subheader("📊 Live Metrics")
m1, m2, m3, m4, m5 = st.columns(5)

if metrics:
    with m1:
        st.metric("👥 Unique Visitors",  metrics.get("unique_visitors", 0))
    with m2:
        rate = metrics.get("conversion_rate", 0)
        st.metric("💳 Conversion Rate", f"{rate:.1%}")
    with m3:
        st.metric("🧾 Queue Depth", metrics.get("queue_depth", 0))
    with m4:
        abandon = metrics.get("abandonment_rate", 0)
        st.metric("🚪 Abandonment Rate", f"{abandon:.1%}")
    with m5:
        zones = metrics.get("avg_dwell_per_zone", [])
        if zones:
            top_zone = max(zones, key=lambda z: z["avg_dwell_seconds"])
            st.metric("⏱️ Top Dwell Zone", top_zone["zone_id"],
                      f"{top_zone['avg_dwell_seconds']:.0f}s avg")
        else:
            st.metric("⏱️ Top Dwell Zone", "—")
else:
    st.warning("⚠️ Could not fetch metrics from API. Is the API running?")

st.divider()


# ── Funnel + Heatmap side by side ─────────────────────────────────────────────

col_funnel, col_heat = st.columns(2)

with col_funnel:
    st.subheader("🔽 Conversion Funnel")
    if funnel and funnel.get("stages"):
        stages = funnel["stages"]
        df_funnel = pd.DataFrame(stages)

        fig = go.Figure(go.Funnel(
            y=df_funnel["stage"],
            x=df_funnel["count"],
            textinfo="value+percent initial",
            marker=dict(color=["#cba6f7", "#89b4fa", "#94e2d5", "#a6e3a1"]),
        ))
        fig.update_layout(
            margin=dict(t=20, b=20),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color="white"),
        )
        st.plotly_chart(fig, use_container_width=True)

        # Drop-off table
        for stage in stages[1:]:
            drop = stage.get("drop_off_pct", 0)
            color = "🔴" if drop > 50 else "🟡" if drop > 25 else "🟢"
            st.caption(f"{color} {stage['stage']}: {drop:.1f}% drop-off")
    else:
        st.info("No funnel data yet — run the detection pipeline first.")

with col_heat:
    st.subheader("🗺️ Zone Heatmap")
    if heatmap and heatmap.get("zones"):
        zones = heatmap["zones"]
        df_heat = pd.DataFrame(zones)

        fig2 = px.bar(
            df_heat.sort_values("normalised_score", ascending=True),
            x="normalised_score",
            y="zone_id",
            orientation="h",
            color="normalised_score",
            color_continuous_scale="Purples",
            labels={"normalised_score": "Normalised Score (0–100)", "zone_id": "Zone"},
            text=df_heat.sort_values("normalised_score", ascending=True)["avg_dwell_seconds"].apply(
                lambda s: f"{s:.0f}s dwell"
            ),
        )
        fig2.update_layout(
            margin=dict(t=20, b=20),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color="white"),
            coloraxis_showscale=False,
        )
        fig2.update_traces(textposition="outside")
        st.plotly_chart(fig2, use_container_width=True)

        confidence = heatmap.get("data_confidence", "LOW")
        if confidence == "LOW":
            st.caption("⚠️ Low confidence — fewer than 20 sessions in window")
    else:
        st.info("No heatmap data yet — run the detection pipeline first.")

st.divider()


# ── Dwell time per zone ───────────────────────────────────────────────────────

if metrics and metrics.get("avg_dwell_per_zone"):
    st.subheader("⏱️ Average Dwell Time by Zone")
    dwell_data = metrics["avg_dwell_per_zone"]
    df_dwell = pd.DataFrame(dwell_data)

    fig3 = px.bar(
        df_dwell,
        x="zone_id",
        y="avg_dwell_seconds",
        color="avg_dwell_seconds",
        color_continuous_scale="Teal",
        labels={"avg_dwell_seconds": "Avg Dwell (seconds)", "zone_id": "Zone"},
        text="avg_dwell_seconds",
    )
    fig3.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="white"),
        coloraxis_showscale=False,
    )
    fig3.update_traces(texttemplate="%{text:.0f}s", textposition="outside")
    st.plotly_chart(fig3, use_container_width=True)
    st.divider()


# ── Anomalies ─────────────────────────────────────────────────────────────────

st.subheader("🚨 Active Anomalies")
if anomalies and anomalies.get("active_anomalies"):
    for a in anomalies["active_anomalies"]:
        sev = a.get("severity", "INFO")
        css_class = f"anomaly-{sev.lower()}"
        icon = "🔴" if sev == "CRITICAL" else "🟡" if sev == "WARN" else "🔵"
        st.markdown(f"""
        <div class="{css_class}">
            <strong>{icon} [{sev}] {a['anomaly_type']}</strong><br>
            {a['description']}<br>
            <em>➡️ {a['suggested_action']}</em>
        </div>
        """, unsafe_allow_html=True)
else:
    st.success("✅ No active anomalies")


# ── Footer + Auto-refresh ─────────────────────────────────────────────────────

st.divider()
st.caption(f"Last updated: {datetime.now().strftime('%H:%M:%S')} · "
           f"API: {API_URL} · "
           f"Confidence: {heatmap.get('data_confidence','—') if heatmap else '—'}")

# Auto-refresh via Streamlit's rerun
time.sleep(REFRESH_SECONDS)
st.rerun()
