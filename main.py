"""
main.py — FastAPI application entrypoint.

All endpoints wired here. Run with:
  uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

TEACHING — Why FastAPI?
  - Automatic request validation via Pydantic (type errors = 422 response)
  - Auto-generated API docs at /docs
  - Async support for high concurrency
  - Dependency injection (get_db) handles DB sessions cleanly
"""

import csv
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from .database import create_tables, get_db, POSTransaction, SessionLocal
from .models import IngestRequest, IngestResponse, MetricsResponse, FunnelResponse, HeatmapResponse, AnomalyResponse, HealthResponse
from .ingestion import ingest_events
from .metrics import compute_metrics, compute_funnel, compute_heatmap
from .anomalies import run_anomaly_detection
from .health import get_health

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}',
)
logger = logging.getLogger("store_intelligence")


# ── Startup / Shutdown ────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Runs on startup: create DB tables, load POS transactions.
    Runs on shutdown: cleanup.
    """
    logger.info("Starting Store Intelligence API")
    create_tables()
    load_pos_data()
    logger.info("Startup complete")
    yield
    logger.info("Shutting down")


def load_pos_data():
    """Load pos_transactions.csv into the DB at startup."""
    pos_path = os.environ.get("POS_DATA_PATH", "./data/pos_transactions.csv")
    if not Path(pos_path).exists():
        logger.warning(f"POS data not found at {pos_path} — skipping")
        return

    db = SessionLocal()
    try:
        count = 0
        with open(pos_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                ts_str = row["timestamp"].replace("Z", "+00:00")
                ts = datetime.fromisoformat(ts_str).replace(tzinfo=None)
                existing = db.query(POSTransaction).filter_by(
                    transaction_id=row["transaction_id"]
                ).first()
                if not existing:
                    db.add(POSTransaction(
                        transaction_id=row["transaction_id"],
                        store_id=row["store_id"],
                        timestamp=ts,
                        basket_value=float(row["basket_value_inr"]),
                    ))
                    count += 1
        db.commit()
        logger.info(f"Loaded {count} POS transactions")
    except Exception as e:
        logger.error(f"Failed to load POS data: {e}")
    finally:
        db.close()


# ── App Instance ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="Store Intelligence API",
    description="Purplle Tech Challenge 2026 — Retail analytics from CCTV",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Structured Logging Middleware ─────────────────────────────────────────────

@app.middleware("http")
async def log_requests(request: Request, call_next):
    """
    Every request gets: trace_id, endpoint, latency_ms, status_code.
    This is the structured logging requirement from Part C.
    """
    trace_id = str(uuid.uuid4())[:8]
    start = time.time()

    # Try to get store_id from path
    store_id = request.path_params.get("store_id", "-")

    try:
        response = await call_next(request)
    except Exception as e:
        logger.error(json.dumps({
            "trace_id": trace_id,
            "store_id": store_id,
            "endpoint": str(request.url.path),
            "latency_ms": round((time.time() - start) * 1000, 1),
            "status_code": 500,
            "error": str(e),
        }))
        raise

    latency = round((time.time() - start) * 1000, 1)
    logger.info(json.dumps({
        "trace_id": trace_id,
        "store_id": store_id,
        "endpoint": str(request.url.path),
        "method": request.method,
        "latency_ms": latency,
        "status_code": response.status_code,
    }))

    response.headers["X-Trace-Id"] = trace_id
    return response


# ── Graceful DB Error Handler ─────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """No raw stack traces in production responses."""
    logger.exception(f"Unhandled error: {exc}")
    return JSONResponse(
        status_code=503,
        content={
            "error": "service_unavailable",
            "message": "An internal error occurred. Please try again.",
            "trace_id": str(uuid.uuid4())[:8],
        }
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/events/ingest", response_model=IngestResponse)
def ingest(payload: IngestRequest, db: Session = Depends(get_db)):
    """
    Accept batches of up to 500 events.
    Idempotent by event_id — safe to call twice with same payload.
    Returns partial success on malformed events.
    """
    if len(payload.events) > 500:
        raise HTTPException(
            status_code=422,
            detail=f"Batch size {len(payload.events)} exceeds limit of 500"
        )

    logger.info(f"[Ingest] Received batch of {len(payload.events)} events")
    result = ingest_events(payload.events, db)

    logger.info(json.dumps({
        "endpoint": "/events/ingest",
        "event_count": len(payload.events),
        "accepted": result.accepted,
        "duplicate": result.duplicate,
        "rejected": result.rejected,
    }))

    return result


@app.get("/stores/{store_id}/metrics", response_model=MetricsResponse)
def metrics(store_id: str, db: Session = Depends(get_db)):
    """
    Today's store metrics: unique visitors, conversion rate,
    avg dwell per zone, queue depth, abandonment rate.
    Always real-time — not cached.
    """
    try:
        return compute_metrics(store_id, db)
    except Exception as e:
        logger.error(f"[Metrics] {store_id}: {e}")
        raise HTTPException(status_code=503, detail="metrics_unavailable")


@app.get("/stores/{store_id}/funnel", response_model=FunnelResponse)
def funnel(store_id: str, db: Session = Depends(get_db)):
    """
    Conversion funnel: Entry → Zone Visit → Billing Queue → Purchase.
    Session is the unit. Re-entries do not double-count.
    """
    try:
        return compute_funnel(store_id, db)
    except Exception as e:
        logger.error(f"[Funnel] {store_id}: {e}")
        raise HTTPException(status_code=503, detail="funnel_unavailable")


@app.get("/stores/{store_id}/heatmap", response_model=HeatmapResponse)
def heatmap(store_id: str, db: Session = Depends(get_db)):
    """
    Zone visit frequency + avg dwell, normalised 0–100.
    """
    try:
        return compute_heatmap(store_id, db)
    except Exception as e:
        logger.error(f"[Heatmap] {store_id}: {e}")
        raise HTTPException(status_code=503, detail="heatmap_unavailable")


@app.get("/stores/{store_id}/anomalies", response_model=AnomalyResponse)
def anomalies(store_id: str, db: Session = Depends(get_db)):
    """
    Active anomalies: queue spike, conversion drop, dead zone, stale feed.
    Each has severity (INFO/WARN/CRITICAL) and suggested_action.
    """
    try:
        return run_anomaly_detection(store_id, db)
    except Exception as e:
        logger.error(f"[Anomalies] {store_id}: {e}")
        raise HTTPException(status_code=503, detail="anomaly_detection_unavailable")


@app.get("/health", response_model=HealthResponse)
def health(db: Session = Depends(get_db)):
    """
    Service health: DB status, last event per store, STALE_FEED warnings.
    On-call engineers check this first.
    """
    return get_health(db)


@app.get("/")
def root(request: Request):
    """Return service metadata with absolute URLs for quick access."""
    base = str(request.base_url).rstrip("/")
    return {
        "service": "Store Intelligence API",
        "version": "1.0.0",
        "base_url": base,
        "docs": f"{base}/docs",
        "health": f"{base}/health",
    }
