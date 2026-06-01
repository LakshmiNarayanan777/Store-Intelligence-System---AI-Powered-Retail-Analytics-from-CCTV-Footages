"""
metrics.py — Real-time store analytics.

TEACHING:
  All metrics are computed fresh from the DB each request.
  No caching = always real-time (the requirement).
  For 40 stores at scale you'd add Redis caching — but that's CHOICES.md material.

  Key metric: Conversion Rate
    = visitors who completed a purchase ÷ total unique visitors
    "completed a purchase" = was in BILLING zone within 5 min before a POS transaction
"""

import logging
from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy.orm import Session
from sqlalchemy import func, distinct

from .database import EventRecord, POSTransaction
from .models import MetricsResponse, ZoneDwell, FunnelResponse, FunnelStage, HeatmapResponse, HeatmapZone

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
POS_CORRELATION_WINDOW_MIN = 5   # visitor in billing N minutes before txn = converted
DEAD_ZONE_MINUTES = 30           # no visits in 30 min = dead zone anomaly
MIN_SESSIONS_FOR_HIGH_CONFIDENCE = 20


def get_today_window(db: Session, store_id: str):
    """Get the min/max timestamp for today's events for a store."""
    result = db.query(
        func.min(EventRecord.timestamp),
        func.max(EventRecord.timestamp)
    ).filter(
        EventRecord.store_id == store_id,
        EventRecord.is_staff == False
    ).first()
    return result[0], result[1]


def get_unique_visitors(db: Session, store_id: str) -> int:
    """Count distinct visitor_ids from ENTRY events (excluding staff)."""
    return db.query(
        func.count(distinct(EventRecord.visitor_id))
    ).filter(
        EventRecord.store_id == store_id,
        EventRecord.event_type.in_(["ENTRY", "REENTRY"]),
        EventRecord.is_staff == False,
    ).scalar() or 0


def get_converted_visitors(db: Session, store_id: str) -> int:
    """
    A visitor is "converted" if they were in the BILLING zone
    within POS_CORRELATION_WINDOW_MIN before a POS transaction.

    TEACHING: We can't match by customer_id (no PII in POS data).
    So we use time + zone: if visitor was in BILLING in the 5 minutes
    before a transaction, they're the buyer.
    This is the correlation approach specified in the problem statement.
    """
    # Get all POS transactions for this store
    transactions = db.query(POSTransaction).filter(
        POSTransaction.store_id == store_id
    ).all()

    if not transactions:
        return 0

    converted = set()

    for txn in transactions:
        window_start = txn.timestamp - timedelta(minutes=POS_CORRELATION_WINDOW_MIN)
        window_end = txn.timestamp

        # Find visitors who were in BILLING zone during this window
        billing_visitors = db.query(
            distinct(EventRecord.visitor_id)
        ).filter(
            EventRecord.store_id == store_id,
            EventRecord.zone_id == "BILLING",
            EventRecord.timestamp >= window_start,
            EventRecord.timestamp <= window_end,
            EventRecord.is_staff == False,
        ).all()

        for (vid,) in billing_visitors:
            converted.add(vid)

    return len(converted)


def get_avg_dwell_per_zone(db: Session, store_id: str) -> List[ZoneDwell]:
    """Average dwell time per zone from ZONE_DWELL events."""
    rows = db.query(
        EventRecord.zone_id,
        func.avg(EventRecord.dwell_ms).label("avg_dwell"),
        func.count(EventRecord.event_id).label("visit_count"),
    ).filter(
        EventRecord.store_id == store_id,
        EventRecord.event_type == "ZONE_DWELL",
        EventRecord.zone_id.isnot(None),
        EventRecord.is_staff == False,
    ).group_by(EventRecord.zone_id).all()

    return [
        ZoneDwell(
            zone_id=row.zone_id,
            avg_dwell_seconds=round((row.avg_dwell or 0) / 1000, 1),
            visit_count=row.visit_count,
        )
        for row in rows
    ]


def get_queue_depth(db: Session, store_id: str) -> int:
    """Current queue depth = people currently in BILLING zone."""
    # Count people who entered billing but haven't exited
    entries = db.query(
        func.count(distinct(EventRecord.visitor_id))
    ).filter(
        EventRecord.store_id == store_id,
        EventRecord.event_type.in_(["BILLING_QUEUE_JOIN", "ZONE_ENTER"]),
        EventRecord.zone_id == "BILLING",
        EventRecord.is_staff == False,
    ).scalar() or 0

    exits = db.query(
        func.count(distinct(EventRecord.visitor_id))
    ).filter(
        EventRecord.store_id == store_id,
        EventRecord.event_type == "ZONE_EXIT",
        EventRecord.zone_id == "BILLING",
        EventRecord.is_staff == False,
    ).scalar() or 0

    return max(0, entries - exits)


def get_abandonment_rate(db: Session, store_id: str) -> float:
    """
    Abandonment rate = billing queue abandonments / total billing queue joins
    """
    joins = db.query(func.count()).filter(
        EventRecord.store_id == store_id,
        EventRecord.event_type == "BILLING_QUEUE_JOIN",
        EventRecord.is_staff == False,
    ).scalar() or 0

    if joins == 0:
        return 0.0

    abandons = db.query(func.count()).filter(
        EventRecord.store_id == store_id,
        EventRecord.event_type == "BILLING_QUEUE_ABANDON",
        EventRecord.is_staff == False,
    ).scalar() or 0

    return round(abandons / joins, 3)


def compute_metrics(store_id: str, db: Session) -> MetricsResponse:
    unique = get_unique_visitors(db, store_id)
    converted = get_converted_visitors(db, store_id)
    conversion_rate = round(converted / unique, 3) if unique > 0 else 0.0
    dwell = get_avg_dwell_per_zone(db, store_id)
    queue = get_queue_depth(db, store_id)
    abandon = get_abandonment_rate(db, store_id)
    window_start, window_end = get_today_window(db, store_id)

    return MetricsResponse(
        store_id=store_id,
        unique_visitors=unique,
        conversion_rate=conversion_rate,
        avg_dwell_per_zone=dwell,
        queue_depth=queue,
        abandonment_rate=abandon,
        window_start=window_start.isoformat() if window_start else None,
        window_end=window_end.isoformat() if window_end else None,
    )


def compute_funnel(store_id: str, db: Session) -> FunnelResponse:
    """
    Conversion funnel: Entry → Zone Visit → Billing Queue → Purchase
    Session is the unit — re-entries don't double-count.

    TEACHING:
      We count unique visitor_ids at each stage.
      Drop-off % = (prev_stage - this_stage) / prev_stage * 100
    """
    # Stage 1: All unique entrants
    entries = get_unique_visitors(db, store_id)

    # Stage 2: Visitors who entered at least one product zone
    zone_visitors = db.query(
        func.count(distinct(EventRecord.visitor_id))
    ).filter(
        EventRecord.store_id == store_id,
        EventRecord.event_type == "ZONE_ENTER",
        EventRecord.zone_id.notin_(["ENTRY", "EXIT", "BILLING"]),
        EventRecord.is_staff == False,
    ).scalar() or 0

    # Stage 3: Visitors who reached billing
    billing_visitors = db.query(
        func.count(distinct(EventRecord.visitor_id))
    ).filter(
        EventRecord.store_id == store_id,
        EventRecord.zone_id == "BILLING",
        EventRecord.is_staff == False,
    ).scalar() or 0

    # Stage 4: Converted (purchased)
    purchases = get_converted_visitors(db, store_id)

    def drop(prev, curr):
        if prev == 0:
            return 0.0
        return round((prev - curr) / prev * 100, 1)

    stages = [
        FunnelStage(stage="Entry",         count=entries,         drop_off_pct=0.0),
        FunnelStage(stage="Zone Visit",    count=zone_visitors,   drop_off_pct=drop(entries, zone_visitors)),
        FunnelStage(stage="Billing Queue", count=billing_visitors, drop_off_pct=drop(zone_visitors, billing_visitors)),
        FunnelStage(stage="Purchase",      count=purchases,       drop_off_pct=drop(billing_visitors, purchases)),
    ]

    confidence = "HIGH" if entries >= MIN_SESSIONS_FOR_HIGH_CONFIDENCE else "LOW"

    return FunnelResponse(store_id=store_id, stages=stages, data_confidence=confidence)


def compute_heatmap(store_id: str, db: Session) -> HeatmapResponse:
    """
    Zone heatmap: visit frequency + avg dwell, normalised 0–100.
    """
    rows = db.query(
        EventRecord.zone_id,
        EventRecord.sku_zone,
        func.count(distinct(EventRecord.visitor_id)).label("visits"),
        func.avg(EventRecord.dwell_ms).label("avg_dwell"),
    ).filter(
        EventRecord.store_id == store_id,
        EventRecord.event_type.in_(["ZONE_ENTER", "ZONE_DWELL"]),
        EventRecord.zone_id.isnot(None),
        EventRecord.is_staff == False,
    ).group_by(EventRecord.zone_id, EventRecord.sku_zone).all()

    if not rows:
        return HeatmapResponse(store_id=store_id, zones=[], data_confidence="LOW")

    max_visits = max(r.visits for r in rows) or 1

    zones = [
        HeatmapZone(
            zone_id=row.zone_id,
            visit_frequency=row.visits,
            avg_dwell_seconds=round((row.avg_dwell or 0) / 1000, 1),
            normalised_score=round((row.visits / max_visits) * 100, 1),
            sku_zone=row.sku_zone,
        )
        for row in rows
    ]

    total_sessions = db.query(func.count(distinct(EventRecord.visitor_id))).filter(
        EventRecord.store_id == store_id
    ).scalar() or 0
    confidence = "HIGH" if total_sessions >= MIN_SESSIONS_FOR_HIGH_CONFIDENCE else "LOW"

    return HeatmapResponse(store_id=store_id, zones=zones, data_confidence=confidence)
