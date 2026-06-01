"""
models.py — Pydantic schemas for the API.

TEACHING:
  Pydantic validates incoming JSON automatically.
  If a field is missing or wrong type, FastAPI returns 422 instantly.
  This is how we get "schema compliance" for free.
"""

from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


# ─── Inbound Event Schema ────────────────────────────────────────────────────

class EventMetadata(BaseModel):
    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: int = 0


class StoreEvent(BaseModel):
    event_id: str
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: str
    timestamp: str
    zone_id: Optional[str] = None
    dwell_ms: int = 0
    is_staff: bool = False
    confidence: float
    metadata: EventMetadata = Field(default_factory=EventMetadata)


class IngestRequest(BaseModel):
    events: List[StoreEvent]


# ─── Response Schemas ─────────────────────────────────────────────────────────

class IngestResponse(BaseModel):
    accepted: int
    rejected: int
    duplicate: int
    errors: List[dict] = []


class ZoneDwell(BaseModel):
    zone_id: str
    avg_dwell_seconds: float
    visit_count: int


class MetricsResponse(BaseModel):
    store_id: str
    unique_visitors: int
    conversion_rate: float
    avg_dwell_per_zone: List[ZoneDwell]
    queue_depth: int
    abandonment_rate: float
    window_start: Optional[str] = None
    window_end: Optional[str] = None


class FunnelStage(BaseModel):
    stage: str
    count: int
    drop_off_pct: float


class FunnelResponse(BaseModel):
    store_id: str
    stages: List[FunnelStage]
    data_confidence: str = "HIGH"


class HeatmapZone(BaseModel):
    zone_id: str
    visit_frequency: int
    avg_dwell_seconds: float
    normalised_score: float    # 0–100
    sku_zone: Optional[str] = None


class HeatmapResponse(BaseModel):
    store_id: str
    zones: List[HeatmapZone]
    data_confidence: str = "HIGH"


class Anomaly(BaseModel):
    anomaly_id: str
    anomaly_type: str
    severity: str              # INFO | WARN | CRITICAL
    description: str
    suggested_action: str
    detected_at: str
    store_id: str
    zone_id: Optional[str] = None
    value: Optional[float] = None
    threshold: Optional[float] = None


class AnomalyResponse(BaseModel):
    store_id: str
    active_anomalies: List[Anomaly]
    checked_at: str


class StoreHealth(BaseModel):
    store_id: str
    status: str
    last_event_timestamp: Optional[str]
    lag_seconds: Optional[float]
    feed_status: str           # OK | STALE_FEED


class HealthResponse(BaseModel):
    service: str = "store-intelligence-api"
    status: str = "ok"
    stores: List[StoreHealth]
    checked_at: str
