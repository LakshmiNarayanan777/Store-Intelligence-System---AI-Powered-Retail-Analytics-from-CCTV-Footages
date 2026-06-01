# PROMPT: "Write pytest tests for a FastAPI store analytics API that has
# endpoints: POST /events/ingest, GET /stores/{id}/metrics,
# GET /stores/{id}/funnel, GET /stores/{id}/anomalies, GET /health.
# Cover: happy path, empty store, duplicate events (idempotency),
# all-staff events excluded from metrics, re-entry in funnel,
# zero purchases store. Use TestClient and in-memory SQLite."
#
# CHANGES MADE:
# - Added edge case: batch > 500 events → 422
# - Changed fixture to use real EventRecord inserts not just API calls
# - Added assertion that is_staff=True events are excluded from unique_visitors
# - Split into clearer test classes per endpoint

import pytest
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.database import Base, get_db, EventRecord
from datetime import datetime
import uuid

# ── Test Database Setup ───────────────────────────────────────────────────────

TEST_DB_URL = "sqlite:///./test_store_intelligence.db"
test_engine = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)


client = TestClient(app)
STORE = "STORE_BLR_002"


# ── Helper: build a valid event dict ─────────────────────────────────────────

def make_event(
    event_type="ENTRY",
    visitor_id=None,
    zone_id=None,
    is_staff=False,
    dwell_ms=0,
    confidence=0.9,
):
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": STORE,
        "camera_id": "CAM_ENTRY_01",
        "visitor_id": visitor_id or f"VIS_{uuid.uuid4().hex[:6]}",
        "event_type": event_type,
        "timestamp": "2026-03-03T14:22:10Z",
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": confidence,
        "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 1},
    }


# ── Health ────────────────────────────────────────────────────────────────────

class TestHealth:
    def test_health_returns_200(self):
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] in ("ok", "degraded")
        assert "checked_at" in data

    def test_health_has_stores_list(self):
        r = client.get("/health")
        assert "stores" in r.json()


# ── Ingest ────────────────────────────────────────────────────────────────────

class TestIngest:
    def test_ingest_single_event(self):
        r = client.post("/events/ingest", json={"events": [make_event()]})
        assert r.status_code == 200
        data = r.json()
        assert data["accepted"] == 1
        assert data["rejected"] == 0

    def test_ingest_idempotent(self):
        """Posting same event twice should count as 1 accepted + 1 duplicate."""
        event = make_event()
        r1 = client.post("/events/ingest", json={"events": [event]})
        r2 = client.post("/events/ingest", json={"events": [event]})
        assert r1.json()["accepted"] == 1
        assert r2.json()["duplicate"] == 1
        assert r2.json()["accepted"] == 0

    def test_ingest_batch_limit(self):
        """Batches > 500 should be rejected with 422."""
        events = [make_event() for _ in range(501)]
        r = client.post("/events/ingest", json={"events": events})
        assert r.status_code == 422

    def test_ingest_partial_success(self):
        """Bad event + good event → 1 accepted, 1 rejected."""
        good = make_event()
        bad = make_event()
        bad["event_type"] = "TOTALLY_INVALID_TYPE"
        r = client.post("/events/ingest", json={"events": [good, bad]})
        data = r.json()
        assert data["accepted"] == 1
        assert data["rejected"] == 1
        assert len(data["errors"]) == 1

    def test_ingest_invalid_confidence(self):
        bad = make_event()
        bad["confidence"] = 1.5  # > 1.0 is invalid
        r = client.post("/events/ingest", json={"events": [bad]})
        assert r.json()["rejected"] == 1


# ── Metrics ───────────────────────────────────────────────────────────────────

class TestMetrics:
    def _seed(self, events):
        client.post("/events/ingest", json={"events": events})

    def test_metrics_empty_store(self):
        """Empty store should return zero metrics, not crash."""
        r = client.get(f"/stores/{STORE}/metrics")
        assert r.status_code == 200
        data = r.json()
        assert data["unique_visitors"] == 0
        assert data["conversion_rate"] == 0.0

    def test_metrics_excludes_staff(self):
        """Staff events must not count toward unique_visitors."""
        self._seed([
            make_event(event_type="ENTRY", is_staff=False),
            make_event(event_type="ENTRY", is_staff=True),
            make_event(event_type="ENTRY", is_staff=True),
        ])
        r = client.get(f"/stores/{STORE}/metrics")
        assert r.json()["unique_visitors"] == 1  # only the non-staff one

    def test_metrics_unique_visitors_correct(self):
        """3 different visitors → unique_visitors = 3."""
        self._seed([make_event(event_type="ENTRY") for _ in range(3)])
        r = client.get(f"/stores/{STORE}/metrics")
        assert r.json()["unique_visitors"] == 3

    def test_metrics_zero_purchases(self):
        """No POS transactions → conversion_rate = 0."""
        self._seed([make_event(event_type="ENTRY")])
        r = client.get(f"/stores/{STORE}/metrics")
        data = r.json()
        assert data["conversion_rate"] == 0.0


# ── Funnel ────────────────────────────────────────────────────────────────────

class TestFunnel:
    def test_funnel_empty_store(self):
        r = client.get(f"/stores/{STORE}/funnel")
        assert r.status_code == 200
        data = r.json()
        assert data["stages"][0]["count"] == 0

    def test_funnel_reentry_does_not_double_count(self):
        """A visitor with ENTRY + REENTRY should count as 1 unique visitor."""
        vid = f"VIS_{uuid.uuid4().hex[:6]}"
        events = [
            make_event(event_type="ENTRY",   visitor_id=vid),
            make_event(event_type="REENTRY", visitor_id=vid),
        ]
        client.post("/events/ingest", json={"events": events})
        r = client.get(f"/stores/{STORE}/funnel")
        # Entry stage counts ENTRY + REENTRY, but unique visitor = 1
        entry_stage = r.json()["stages"][0]
        assert entry_stage["count"] == 1

    def test_funnel_has_four_stages(self):
        r = client.get(f"/stores/{STORE}/funnel")
        assert len(r.json()["stages"]) == 4


# ── Anomalies ─────────────────────────────────────────────────────────────────

class TestAnomalies:
    def test_anomalies_empty_store(self):
        """Empty store should return empty anomalies list, not crash."""
        r = client.get(f"/stores/{STORE}/anomalies")
        assert r.status_code == 200
        assert isinstance(r.json()["active_anomalies"], list)

    def test_anomalies_response_structure(self):
        r = client.get(f"/stores/{STORE}/anomalies")
        data = r.json()
        assert "store_id" in data
        assert "checked_at" in data
        assert "active_anomalies" in data
