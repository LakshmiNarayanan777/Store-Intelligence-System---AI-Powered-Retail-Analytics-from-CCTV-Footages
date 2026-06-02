# Store Intelligence System
### Purplle Tech Challenge 2026 — Round 2

An end-to-end retail analytics pipeline that processes raw CCTV footage and delivers live store metrics via a production-grade REST API.

---

## Quick Start (5 commands)

```bash
# 1. Clone the repo
git clone https://github.com/lakshminarayanan777/store-intelligence.git
cd store-intelligence

# 2. Place your video files in data/videos/
mkdir -p data/videos
cp /path/to/CAM_*.mp4 data/videos/

# 3. Run the detection pipeline (generates events.jsonl)
cd pipeline && bash run.sh ../data/videos/ ../data/events.jsonl && cd ..

# 4. Start the API + dashboard
docker compose up --build

# 5. Feed events into the API
python scripts/feed_events.py --events data/events.jsonl --api http://localhost:8000
```

**API docs:** http://localhost:8000/docs  
**Dashboard:** http://localhost:8501  
**Health check:** http://localhost:8000/health

---

## Architecture

```
CCTV Videos → Detection Pipeline → events.jsonl → API → Dashboard
                (YOLOv8n +                     (FastAPI +  (Streamlit)
                 ByteTrack)                     SQLite)
```

Full architecture details: [docs/DESIGN.md](docs/DESIGN.md)  
Key decisions: [docs/CHOICES.md](docs/CHOICES.md)

---

## Project Structure

```
store-intelligence/
├── pipeline/
│   ├── detect.py          # YOLOv8 + ByteTrack detection loop
│   ├── tracker.py         # Re-ID, zone tracking, staff detection
│   ├── emit.py            # Event schema + .jsonl emission
│   └── run.sh             # One command: all clips → events.jsonl
├── app/
│   ├── main.py            # FastAPI entrypoint + middleware
│   ├── models.py          # Pydantic request/response schemas
│   ├── database.py        # SQLAlchemy ORM + SQLite setup
│   ├── ingestion.py       # Ingest, validate, deduplicate
│   ├── metrics.py         # Real-time metric computation
│   ├── anomalies.py       # Anomaly detection (4 detectors)
│   └── health.py          # Health check endpoint
├── dashboard/
│   └── app.py             # Streamlit live dashboard
├── tests/
│   └── test_api.py        # pytest suite (>70% coverage)
├── docs/
│   ├── DESIGN.md          # Architecture + AI-assisted decisions
│   └── CHOICES.md         # 3 key decisions with full reasoning
├── data/
│   ├── store_layout.json  # Zone definitions
│   └── events.jsonl       # Pipeline output (git-ignored)
├── scripts/
│   └── feed_events.py     # Feeds events.jsonl into the API
├── docker-compose.yml
├── Dockerfile
├── Dockerfile.dashboard
└── requirements.txt
```

---

## Running the Detection Pipeline

### Prerequisites
```bash
pip install ultralytics supervision opencv-python-headless
```

### Single camera
```bash
cd pipeline
python detect.py \
  --video ../data/videos/CAM_1.mp4 \
  --camera CAM_ENTRY_01 \
  --type entry \
  --store STORE_BLR_002 \
  --output ../data/events.jsonl
```

### All cameras (recommended)
```bash
cd pipeline
bash run.sh ../data/videos/ ../data/events.jsonl
```

Camera type mapping:
| File | Camera ID | Type |
|------|-----------|------|
| CAM_1.mp4 | CAM_ENTRY_01 | entry |
| CAM_2.mp4 | CAM_FLOOR_01 | floor |
| CAM_3.mp4 | CAM_BILLING_01 | billing |
| CAM_4.mp4 | CAM_FLOOR_02 | floor |
| CAM_5.mp4 | CAM_ENTRY_02 | entry |

---

## API Reference

### POST /events/ingest
Ingest up to 500 events. Idempotent by `event_id`.

```bash
curl -X POST http://localhost:8000/events/ingest \
  -H "Content-Type: application/json" \
  -d '{"events": [...]}'
```

Response:
```json
{"accepted": 47, "rejected": 0, "duplicate": 3, "errors": []}
```

### GET /stores/{store_id}/metrics
```bash
curl http://localhost:8000/stores/STORE_BLR_002/metrics
```

### GET /stores/{store_id}/funnel
```bash
curl http://localhost:8000/stores/STORE_BLR_002/funnel
```

### GET /stores/{store_id}/heatmap
```bash
curl http://localhost:8000/stores/STORE_BLR_002/heatmap
```

### GET /stores/{store_id}/anomalies
```bash
curl http://localhost:8000/stores/STORE_BLR_002/anomalies
```

### GET /health
```bash
curl http://localhost:8000/health
```

---

## Feeding Events into the API

After running the detection pipeline, feed `events.jsonl` into the API:

```bash
python scripts/feed_events.py \
  --events data/events.jsonl \
  --api http://localhost:8000 \
  --batch-size 100
```

For **simulated real-time** (Part E dashboard demo):
```bash
python scripts/feed_events.py \
  --events data/events.jsonl \
  --api http://localhost:8000 \
  --realtime   # replays events at original timestamps
```

---

## Running Tests

```bash
pip install pytest httpx
pytest tests/ -v --tb=short
```

Expected output:
```
tests/test_api.py::TestHealth::test_health_returns_200 PASSED
tests/test_api.py::TestIngest::test_ingest_single_event PASSED
tests/test_api.py::TestIngest::test_ingest_idempotent PASSED
...
```

---

## Deployment

This repo can be deployed using Docker-friendly hosts.

- `render.yaml` is included for Render.com with two services:
  - `store-intelligence-api`
  - `store-intelligence-dashboard`
- `dashboard/app.py` can also be hosted on Streamlit Community Cloud.

See [DEPLOY.md](DEPLOY.md) for deployment steps and environment setup.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_PATH` | `./store_intelligence.db` | SQLite database path |
| `POS_DATA_PATH` | `./data/pos_transactions.csv` | POS transactions CSV |
| `API_URL` | `http://localhost:8000` | API URL for dashboard |

---

## AI Usage

AI tools (Claude, GitHub Copilot) were used throughout this project.  
See [docs/DESIGN.md](docs/DESIGN.md) → *AI-Assisted Decisions* for where AI shaped the architecture.  
See [docs/CHOICES.md](docs/CHOICES.md) for decisions where I agreed/overrode AI suggestions.  
Prompt blocks are included at the top of each test file.
