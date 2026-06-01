# DESIGN.md — Store Intelligence System

## Overview

This system ingests raw CCTV footage from a retail store and produces a live analytics API. The pipeline runs offline (batch or simulated real-time), emits structured events into a SQLite database via a REST API, and exposes queryable endpoints for metrics, funnels, heatmaps, and anomaly detection.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        PIPELINE LAYER                           │
│                                                                 │
│  CAM_1.mp4 ──►  detect.py                                       │
│  CAM_2.mp4 ──►  (YOLOv8n + ByteTrack)                          │
│  CAM_3.mp4 ──►  tracker.py (Re-ID + zones)                     │
│  CAM_4.mp4 ──►  emit.py (event schema)                         │
│  CAM_5.mp4 ──►                │                                 │
│                               ▼                                 │
│                         events.jsonl                            │
└───────────────────────────────┬─────────────────────────────────┘
                                │  POST /events/ingest
┌───────────────────────────────▼─────────────────────────────────┐
│                         API LAYER (FastAPI)                     │
│                                                                 │
│  ingestion.py  ──►  SQLite DB (SQLAlchemy ORM)                  │
│  metrics.py    ──►  GET /stores/{id}/metrics                    │
│  funnel.py     ──►  GET /stores/{id}/funnel                     │
│  anomalies.py  ──►  GET /stores/{id}/anomalies                  │
│  health.py     ──►  GET /health                                 │
└───────────────────────────────┬─────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────┐
│                      DASHBOARD LAYER (Streamlit)                │
│                                                                 │
│  Live metrics · Funnel chart · Zone heatmap · Anomaly feed      │
│  Auto-refreshes every 5 seconds via st.rerun()                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Component Breakdown

### 1. Detection Pipeline (`pipeline/`)

**`detect.py`** — Main loop. Opens video with OpenCV, runs YOLOv8n inference every 3rd frame (frame skipping for CPU performance), passes detections to supervision's ByteTrack, then routes each tracked bounding box through the zone classifier and event emitter.

**`tracker.py`** — PersonTracker wraps ByteTrack's integer track IDs and maps them to stable `visitor_id` tokens (e.g. `VIS_c8a2f1`). Handles:
- Re-entry detection: if a track_id reappears near the entry line within 30s of an EXIT, it's flagged as REENTRY not a new ENTRY
- Staff classification: persons tracked for >300 frames are flagged `is_staff=True`
- Zone dwell: tracks when each visitor entered each zone, emits ZONE_DWELL every 30s

**`emit.py`** — EventEmitter writes one JSON object per line to a `.jsonl` file. Each event matches the schema exactly: `event_id`, `store_id`, `camera_id`, `visitor_id`, `event_type`, `timestamp`, `zone_id`, `dwell_ms`, `is_staff`, `confidence`, `metadata`.

**`ZoneClassifier`** — Rule-based. For entry cameras, detects line crossing (centroid Y crosses threshold at 40% from top). For floor cameras, divides frame into horizontal bands mapped to zone names from `store_layout.json`. For billing cameras, everything is the BILLING zone. This is simple and fast — no ML required for zone classification at this stage.

**`StaffDetector`** — Checks two signals: frame count (>300 frames = likely staff) and uniform color (HSV color range matching for blue/black/white uniforms in the torso region of the bounding box).

---

### 2. Intelligence API (`app/`)

**`main.py`** — FastAPI app with lifespan hooks (creates DB tables on startup, loads POS CSV). Middleware adds `trace_id`, `latency_ms`, `store_id`, `status_code` to every request log. Global exception handler prevents raw stack traces in responses (returns structured 503).

**`ingestion.py`** — Validates each event in a batch independently. Uses SQLite's primary key constraint on `event_id` for idempotency — duplicate inserts raise `IntegrityError` which we catch and count as `duplicate`. Commits once per batch for efficiency.

**`metrics.py`** — All metrics computed fresh from DB per request (no cache). Conversion rate uses POS correlation: a visitor is "converted" if they were in the BILLING zone in the 5-minute window before a POS transaction timestamp, since POS data has no customer_id.

**`anomalies.py`** — Four detectors run on each request: queue spike (current billing depth > threshold), conversion drop (today's rate vs 7-day average), dead zone (no visits in 30min), stale feed (no events from a camera in 10min). Each anomaly has `severity` (INFO/WARN/CRITICAL) and `suggested_action`.

**`health.py`** — Checks DB connectivity and per-store event lag. Returns `STALE_FEED` if last event is >10 minutes ago.

---

### 3. Storage

SQLite via SQLAlchemy ORM. Three tables:
- `events` — every ingested event, deduplicated by `event_id` (primary key)
- `pos_transactions` — loaded from CSV at startup
- `anomalies` — persisted anomaly records

SQLite was chosen over PostgreSQL for simplicity — no separate server needed, runs inside the container, file-based persistence via Docker volume. At 40 stores with real-time event streams, this would be the first thing to migrate to PostgreSQL.

---

### 4. Dashboard (`dashboard/`)

Streamlit app. Polls the API every 5 seconds (`st.rerun()`). Shows:
- Key metrics row (visitors, conversion rate, queue depth, abandonment rate)
- Plotly funnel chart (Entry → Zone Visit → Billing → Purchase)
- Zone heatmap (normalised visit frequency as horizontal bar chart)
- Dwell time per zone (bar chart)
- Active anomalies with severity colour-coding

---

## AI-Assisted Decisions

### 1. Frame skipping strategy

I initially processed every frame, which was too slow on CPU (~3fps throughput for 1080p). I asked Claude: *"What's the right frame skip interval for pedestrian tracking in retail CCTV at 30fps?"* Claude suggested skipping every 5th frame, reasoning that a person walking at 1m/s moves ~3cm between frames at typical retail camera distances — well within ByteTrack's motion model tolerance. I tested with skip=3 and skip=5 on the first 60 seconds of CAM_1.mp4 and found skip=3 gave better track continuity near the entry threshold without significant speed loss. I overrode the AI suggestion and went with skip=3.

### 2. POS correlation approach

The problem statement says correlation is done by "time window + store" since there's no customer_id. I asked Claude to reason through edge cases: *"What breaks if two customers are both in the billing zone when a transaction fires?"* Claude pointed out that both would be counted as converted, inflating conversion rate. It suggested using queue depth and session sequence to pick the most likely buyer. I implemented the simpler time-window approach (matching the problem statement spec) but documented this limitation in CHOICES.md. The AI suggestion was noted but deprioritised — the spec defines the correlation method.

### 3. Anomaly severity thresholds

I asked Claude: *"What are reasonable queue spike thresholds for a retail store with 40 customers/hour?"* It suggested WARN at 5 people, CRITICAL at 10, based on typical retail service time of ~3 minutes per transaction. I used WARN=5, CRITICAL=10 (double the warn threshold). These are configurable via environment variables in production.

---

## Known Limitations

1. **Cross-camera deduplication** is not fully implemented — a person moving from entry cam to floor cam could be counted twice. A full Re-ID model (OSNet/torchreid) would solve this.
2. **Staff detection** is heuristic-based — in clips with non-uniform staff, this will misclassify. A dedicated staff classifier trained on labelled data would be more reliable.
3. **SQLite at scale** — at 40 live stores, SQLite will bottleneck on concurrent writes. PostgreSQL + connection pooling is the production path.
4. **Re-entry window** is time-based, not appearance-based — a different customer entering immediately after an exit could be misidentified as a re-entry.
