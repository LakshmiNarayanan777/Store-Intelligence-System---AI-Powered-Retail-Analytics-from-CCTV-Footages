"""
anomalies.py — Detect operational anomalies in real time.

Anomaly types (from problem statement):
  BILLING_QUEUE_SPIKE  — queue depth above threshold
  CONVERSION_DROP      — conversion rate below 7-day average
  DEAD_ZONE            — no visits to a zone in 30+ minutes
  STALE_FEED           — no events from a camera in 10+ minutes

TEACHING:
  Each anomaly has a severity: INFO / WARN / CRITICAL
  and a suggested_action string for the ops team.
  We write them to the DB so they persist across requests.
"""

import uuid
import logging
from datetime import datetime, timedelta
from typing import List

from sqlalchemy.orm import Session
from sqlalchemy import func, distinct

from .database import EventRecord, AnomalyRecord
from .models import Anomaly, AnomalyResponse

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────
QUEUE_SPIKE_THRESHOLD   = 5     # >5 people in billing = spike
CONVERSION_DROP_PCT     = 0.20  # 20% below 7-day avg = anomaly
DEAD_ZONE_MINUTES       = 30
STALE_FEED_MINUTES      = 10


def detect_queue_spike(store_id: str, db: Session) -> List[AnomalyRecord]:
    """Check if current billing queue depth is above threshold."""
    anomalies = []

    # Count people in billing who haven't exited
    joins = db.query(func.count(distinct(EventRecord.visitor_id))).filter(
        EventRecord.store_id == store_id,
        EventRecord.event_type.in_(["BILLING_QUEUE_JOIN", "ZONE_ENTER"]),
        EventRecord.zone_id == "BILLING",
        EventRecord.is_staff == False,
    ).scalar() or 0

    exits = db.query(func.count(distinct(EventRecord.visitor_id))).filter(
        EventRecord.store_id == store_id,
        EventRecord.event_type == "ZONE_EXIT",
        EventRecord.zone_id == "BILLING",
        EventRecord.is_staff == False,
    ).scalar() or 0

    queue_depth = max(0, joins - exits)

    if queue_depth >= QUEUE_SPIKE_THRESHOLD:
        severity = "CRITICAL" if queue_depth >= QUEUE_SPIKE_THRESHOLD * 2 else "WARN"
        anomalies.append(AnomalyRecord(
            anomaly_id=str(uuid.uuid4()),
            store_id=store_id,
            anomaly_type="BILLING_QUEUE_SPIKE",
            severity=severity,
            description=f"Billing queue depth is {queue_depth} (threshold: {QUEUE_SPIKE_THRESHOLD})",
            suggested_action="Open additional billing counter or call supervisor to billing area",
            zone_id="BILLING",
            value=float(queue_depth),
            threshold=float(QUEUE_SPIKE_THRESHOLD),
        ))

    return anomalies


def detect_conversion_drop(store_id: str, db: Session) -> List[AnomalyRecord]:
    """
    Compare today's conversion rate against the 7-day average.
    If today's rate is 20%+ below average → anomaly.
    """
    anomalies = []

    # Today's stats
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0)

    today_visitors = db.query(func.count(distinct(EventRecord.visitor_id))).filter(
        EventRecord.store_id == store_id,
        EventRecord.event_type.in_(["ENTRY", "REENTRY"]),
        EventRecord.timestamp >= today_start,
        EventRecord.is_staff == False,
    ).scalar() or 0

    if today_visitors < 5:
        return []   # Not enough data

    # 7-day average visitors per day
    seven_days_ago = now - timedelta(days=7)
    hist_visitors = db.query(func.count(distinct(EventRecord.visitor_id))).filter(
        EventRecord.store_id == store_id,
        EventRecord.event_type.in_(["ENTRY", "REENTRY"]),
        EventRecord.timestamp >= seven_days_ago,
        EventRecord.timestamp < today_start,
        EventRecord.is_staff == False,
    ).scalar() or 0

    avg_daily = hist_visitors / 7 if hist_visitors else today_visitors

    # Conversion rate today (simplified: billing visits / total)
    today_billing = db.query(func.count(distinct(EventRecord.visitor_id))).filter(
        EventRecord.store_id == store_id,
        EventRecord.zone_id == "BILLING",
        EventRecord.timestamp >= today_start,
        EventRecord.is_staff == False,
    ).scalar() or 0

    today_rate = today_billing / today_visitors if today_visitors > 0 else 0

    # Historical rate
    hist_billing = db.query(func.count(distinct(EventRecord.visitor_id))).filter(
        EventRecord.store_id == store_id,
        EventRecord.zone_id == "BILLING",
        EventRecord.timestamp >= seven_days_ago,
        EventRecord.timestamp < today_start,
        EventRecord.is_staff == False,
    ).scalar() or 0

    hist_rate = hist_billing / hist_visitors if hist_visitors > 0 else today_rate

    if hist_rate > 0 and (hist_rate - today_rate) / hist_rate > CONVERSION_DROP_PCT:
        drop_pct = round((hist_rate - today_rate) / hist_rate * 100, 1)
        anomalies.append(AnomalyRecord(
            anomaly_id=str(uuid.uuid4()),
            store_id=store_id,
            anomaly_type="CONVERSION_DROP",
            severity="WARN",
            description=f"Conversion rate {today_rate:.1%} is {drop_pct}% below 7-day avg {hist_rate:.1%}",
            suggested_action="Review today's funnel; check for pricing issues or stock gaps in high-traffic zones",
            value=today_rate,
            threshold=hist_rate,
        ))

    return anomalies


def detect_dead_zones(store_id: str, db: Session) -> List[AnomalyRecord]:
    """
    Find zones with no visits in the last DEAD_ZONE_MINUTES.
    A dead zone could mean camera failure or genuine empty period.
    """
    anomalies = []
    cutoff = datetime.utcnow() - timedelta(minutes=DEAD_ZONE_MINUTES)

    # Get all zones that have ever had events
    all_zones = db.query(distinct(EventRecord.zone_id)).filter(
        EventRecord.store_id == store_id,
        EventRecord.zone_id.isnot(None),
        EventRecord.zone_id.notin_(["ENTRY", "EXIT"]),
    ).all()

    for (zone_id,) in all_zones:
        recent = db.query(func.count()).filter(
            EventRecord.store_id == store_id,
            EventRecord.zone_id == zone_id,
            EventRecord.timestamp >= cutoff,
            EventRecord.is_staff == False,
        ).scalar() or 0

        if recent == 0:
            anomalies.append(AnomalyRecord(
                anomaly_id=str(uuid.uuid4()),
                store_id=store_id,
                anomaly_type="DEAD_ZONE",
                severity="INFO",
                description=f"Zone {zone_id} has had no customer visits in {DEAD_ZONE_MINUTES}+ minutes",
                suggested_action=f"Check camera covering {zone_id}; verify zone is accessible and stocked",
                zone_id=zone_id,
            ))

    return anomalies


def detect_stale_feed(store_id: str, db: Session) -> List[AnomalyRecord]:
    """Check if any camera hasn't sent events in 10+ minutes."""
    anomalies = []
    cutoff = datetime.utcnow() - timedelta(minutes=STALE_FEED_MINUTES)

    cameras = db.query(distinct(EventRecord.camera_id)).filter(
        EventRecord.store_id == store_id,
    ).all()

    for (cam_id,) in cameras:
        last_event = db.query(func.max(EventRecord.timestamp)).filter(
            EventRecord.store_id == store_id,
            EventRecord.camera_id == cam_id,
        ).scalar()

        if last_event and last_event < cutoff:
            lag = (datetime.utcnow() - last_event).seconds
            anomalies.append(AnomalyRecord(
                anomaly_id=str(uuid.uuid4()),
                store_id=store_id,
                anomaly_type="STALE_FEED",
                severity="CRITICAL",
                description=f"Camera {cam_id} last event was {lag}s ago (threshold: {STALE_FEED_MINUTES*60}s)",
                suggested_action=f"Check network connection and power for camera {cam_id}; alert on-call engineer",
            ))

    return anomalies


def run_anomaly_detection(store_id: str, db: Session) -> AnomalyResponse:
    """Run all anomaly detectors and return active anomalies."""

    # Clear old unresolved anomalies for this store (re-detect fresh)
    db.query(AnomalyRecord).filter(
        AnomalyRecord.store_id == store_id,
        AnomalyRecord.resolved == False,
    ).delete()
    db.commit()

    all_anomalies = []
    all_anomalies.extend(detect_queue_spike(store_id, db))
    all_anomalies.extend(detect_conversion_drop(store_id, db))
    all_anomalies.extend(detect_dead_zones(store_id, db))
    all_anomalies.extend(detect_stale_feed(store_id, db))

    # Persist to DB
    for a in all_anomalies:
        db.add(a)
    db.commit()

    logger.info(f"[Anomaly] {store_id}: {len(all_anomalies)} active anomalies")

    return AnomalyResponse(
        store_id=store_id,
        active_anomalies=[
            Anomaly(
                anomaly_id=a.anomaly_id,
                anomaly_type=a.anomaly_type,
                severity=a.severity,
                description=a.description,
                suggested_action=a.suggested_action,
                detected_at=a.detected_at.isoformat() if a.detected_at else datetime.utcnow().isoformat(),
                store_id=a.store_id,
                zone_id=a.zone_id,
                value=a.value,
                threshold=a.threshold,
            )
            for a in all_anomalies
        ],
        checked_at=datetime.utcnow().isoformat(),
    )
