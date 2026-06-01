"""
ingestion.py — Ingest events, deduplicate, validate, persist.

TEACHING:
  Idempotency: if you POST the same event twice, it should only be stored once.
  We achieve this by using event_id as the PRIMARY KEY in SQLite.
  Inserting a duplicate → SQLite raises IntegrityError → we count it as duplicate, not error.

  Partial success: if a batch of 500 events has 3 malformed ones,
  we accept the 497 good ones and report the 3 failures.
  We never reject the whole batch.
"""

import uuid
import logging
from datetime import datetime
from typing import List, Tuple

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from .models import StoreEvent, IngestResponse
from .database import EventRecord

logger = logging.getLogger(__name__)

VALID_EVENT_TYPES = {
    "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT",
    "ZONE_DWELL", "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON", "REENTRY"
}


def parse_timestamp(ts_str: str) -> datetime:
    """Parse ISO-8601 UTC timestamp string to datetime."""
    ts_str = ts_str.replace("Z", "+00:00")
    return datetime.fromisoformat(ts_str).replace(tzinfo=None)


def validate_event(event: StoreEvent) -> Tuple[bool, str]:
    """
    Returns (is_valid, error_message).
    We validate the fields that matter for correctness.
    """
    if not event.event_id:
        return False, "event_id is required"
    if not event.store_id:
        return False, "store_id is required"
    if not event.visitor_id:
        return False, "visitor_id is required"
    if event.event_type not in VALID_EVENT_TYPES:
        return False, f"unknown event_type: {event.event_type}"
    if not event.timestamp:
        return False, "timestamp is required"
    try:
        parse_timestamp(event.timestamp)
    except Exception:
        return False, f"invalid timestamp format: {event.timestamp}"
    if not (0.0 <= event.confidence <= 1.0):
        return False, f"confidence must be 0-1, got {event.confidence}"
    return True, ""


def ingest_events(events: List[StoreEvent], db: Session) -> IngestResponse:
    """
    Processes a batch of events:
    1. Validate each event
    2. Try to insert (idempotent by event_id)
    3. Count accepted / rejected / duplicate
    """
    accepted = 0
    rejected = 0
    duplicate = 0
    errors = []

    for event in events:
        # ── Validate ──────────────────────────────────────────────────────
        valid, err_msg = validate_event(event)
        if not valid:
            rejected += 1
            errors.append({"event_id": event.event_id, "error": err_msg})
            logger.warning(f"[Ingest] Rejected event {event.event_id}: {err_msg}")
            continue

        # ── Parse timestamp ───────────────────────────────────────────────
        try:
            ts = parse_timestamp(event.timestamp)
        except Exception as e:
            rejected += 1
            errors.append({"event_id": event.event_id, "error": str(e)})
            continue

        # ── Build DB record ───────────────────────────────────────────────
        record = EventRecord(
            event_id    = event.event_id,
            store_id    = event.store_id,
            camera_id   = event.camera_id,
            visitor_id  = event.visitor_id,
            event_type  = event.event_type,
            timestamp   = ts,
            zone_id     = event.zone_id,
            dwell_ms    = event.dwell_ms,
            is_staff    = event.is_staff,
            confidence  = event.confidence,
            queue_depth = event.metadata.queue_depth if event.metadata else None,
            sku_zone    = event.metadata.sku_zone if event.metadata else None,
            session_seq = event.metadata.session_seq if event.metadata else 0,
        )

        # ── Insert (idempotent) ───────────────────────────────────────────
        try:
            db.add(record)
            db.flush()   # flush to catch IntegrityError before commit
            accepted += 1
        except IntegrityError:
            db.rollback()
            duplicate += 1
            logger.debug(f"[Ingest] Duplicate event_id: {event.event_id}")
        except Exception as e:
            db.rollback()
            rejected += 1
            errors.append({"event_id": event.event_id, "error": str(e)})
            logger.error(f"[Ingest] DB error for {event.event_id}: {e}")

    # ── Commit all accepted records at once ───────────────────────────────
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"[Ingest] Commit failed: {e}")
        raise

    logger.info(
        f"[Ingest] Batch complete: accepted={accepted} "
        f"duplicate={duplicate} rejected={rejected}"
    )

    return IngestResponse(
        accepted=accepted,
        rejected=rejected,
        duplicate=duplicate,
        errors=errors,
    )
