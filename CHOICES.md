# CHOICES.md — Key Engineering Decisions

## Decision 1: Detection Model — YOLOv8n

### Options Considered

| Model | Speed (CPU) | Accuracy | Complexity |
|-------|------------|----------|------------|
| YOLOv8n | ~35ms/frame | Good | Low |
| YOLOv8m | ~90ms/frame | Better | Low |
| RT-DETR | ~120ms/frame | Best | Medium |
| MediaPipe | ~15ms/frame | OK for faces | High (retail) |
| GPT-4V / Claude Vision (VLM) | ~2000ms/frame | Excellent reasoning | High |

### What AI Suggested

I asked Claude: *"For retail people counting from 1080p CCTV footage on CPU, which YOLO variant gives the best accuracy/speed tradeoff?"*

Claude recommended YOLOv8s (small) over nano, arguing the accuracy difference on partially occluded persons is significant. It also flagged that the nano model struggles when multiple people overlap in dense billing queue scenarios.

### What I Chose and Why

**YOLOv8n** (nano) — I disagreed with Claude on this one.

The problem statement says detections must "degrade gracefully, not fail silently" — meaning I should report low-confidence detections rather than miss them. YOLOv8n with a lower confidence threshold (0.3 vs the default 0.5) catches more people at the cost of some false positives. For a retail analytics use case, undercounting is worse than slight overcounting (a missed customer is invisible to the business; a false detection is visible in the confidence field).

Additionally, with 5 videos at 30fps and no GPU, YOLOv8n at frame_skip=3 gives ~10 effective FPS throughput — processable in reasonable time on CPU. YOLOv8s would be 2.5x slower.

**VLM consideration:** I evaluated using Claude Vision for zone classification (which product zone is this person standing in?). The advantage is richer scene understanding — a VLM can reason about shelving, displays, and product categories directly from pixels. The disadvantage is latency: ~2 seconds per frame vs ~35ms for YOLO. For 20-minute clips at even 1fps, that's 2400 VLM calls per camera. Not viable for this challenge's timeline. If I were building this as a cloud service with async processing and a budget, I would use a VLM for zone classification and reserve YOLO purely for detection + tracking.

---

## Decision 2: Event Schema Design

### The Core Problem

The schema needs to serve two masters:
1. The detection pipeline (which produces events)
2. The analytics API (which queries events)

A schema that's easy to emit is often hard to query, and vice versa.

### Options Considered

**Option A — Flat schema, one table**
Every event type in one table with nullable fields. Simple to emit. Hard to query efficiently (lots of NULLs, type-specific logic scattered).

**Option B — Polymorphic schema, one table per event type**
`entry_events`, `zone_events`, `billing_events` tables. Clean queries per type. Hard to ingest (routing logic) and hard to stream (multiple sinks).

**Option C — Flat schema with typed metadata (chosen)**
One table for all events. Event-specific data in a `metadata` JSON-like field (stored as columns: `queue_depth`, `sku_zone`, `session_seq`). Each event type uses only the fields relevant to it.

### What AI Suggested

Claude suggested Option B, arguing that separate tables enable better index performance and prevent the NULL-column problem. It's correct at scale.

### What I Chose and Why

**Option C** — One flat table, typed metadata columns.

Reason: the problem statement requires the API to run on `docker compose up` with no manual setup. A single-table design means one migration, one ORM model, one ingest code path. The analytics queries (metrics, funnel, heatmap) all filter by `event_type` and `zone_id` — which are indexed. The performance difference only matters at scale (millions of events), not for a 5-camera, 20-minute dataset.

The `metadata` fields (`queue_depth`, `sku_zone`, `session_seq`) are stored as actual columns rather than a JSON blob. This makes them queryable with SQLAlchemy without JSON extraction functions, which SQLite handles poorly.

I agreed with the AI that at production scale (40 stores, 24/7), Option B or a time-series database (TimescaleDB, InfluxDB) would be the right choice. That's documented in DESIGN.md.

---

## Decision 3: API Architecture — Synchronous vs Async Metrics

### The Problem

The `/stores/{id}/metrics` endpoint computes conversion rate, dwell times, and queue depth on every request — no caching. At 40 stores with frequent dashboard polling, this could be slow.

### Options Considered

**Option A — Compute on every request (chosen)**
Always real-time. Simple. Can be slow under load.

**Option B — Background worker pre-computes metrics every N seconds**
Fast responses. Not truly real-time. Requires a task queue (Celery, APScheduler).

**Option C — Event-driven: recompute on every ingest**
Perfectly real-time. Complex. Every POST /events/ingest triggers metric recomputation.

### What AI Suggested

Claude recommended Option B with a 10-second pre-computation interval, arguing that "real-time" in a retail context doesn't need sub-second freshness, and that pre-computation dramatically simplifies the API's response time profile.

### What I Chose and Why

**Option A** — Compute on every request.

The problem statement explicitly says metrics must be "real-time — not cached from yesterday." While Claude's suggestion is operationally sound, it introduces complexity (background worker, shared state, potential staleness between ingest and recomputation window) that isn't justified for this challenge scope.

For the 5-camera dataset, SQLite queries run in <50ms. The structured logging middleware captures `latency_ms` per request, so if this becomes a bottleneck it's immediately visible.

**What would make me change this:** At 40 live stores polling every 5 seconds, that's 8 metric queries/second minimum — each doing 5-6 SQL aggregations. At that point I'd introduce a Redis cache with a 10-second TTL and a background invalidation trigger on new event ingestion. I'd add this as the first performance optimisation post-launch, not as an upfront design choice that adds complexity before we know it's needed.

---

## Summary Table

| Decision | AI Suggested | I Chose | Why I Deviated |
|----------|-------------|---------|----------------|
| Detection model | YOLOv8s | YOLOv8n + lower conf | Speed on CPU; graceful degradation > raw accuracy |
| Event schema | Separate tables per type | Single flat table | Simpler ops; good enough for dataset scale |
| Metrics computation | Pre-compute with background worker | Compute on request | Spec says real-time; complexity not yet justified |
