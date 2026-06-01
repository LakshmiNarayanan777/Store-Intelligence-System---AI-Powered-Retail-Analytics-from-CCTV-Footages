"""
health.py — Service health check.

TEACHING:
  The /health endpoint is what on-call engineers check first.
  It must be accurate — lying about health is worse than being down.
  We check: DB connectivity, last event per store, feed staleness.
"""

import logging
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import func, distinct

from .database import EventRecord
from .models import HealthResponse, StoreHealth

logger = logging.getLogger(__name__)

STALE_FEED_MINUTES = 10


def get_health(db: Session) -> HealthResponse:
    try:
        # Get all known stores
        stores = db.query(distinct(EventRecord.store_id)).all()
        store_ids = [s[0] for s in stores] if stores else ["STORE_BLR_002"]

        store_healths = []
        now = datetime.utcnow()

        for store_id in store_ids:
            last_ts = db.query(func.max(EventRecord.timestamp)).filter(
                EventRecord.store_id == store_id
            ).scalar()

            if last_ts is None:
                store_healths.append(StoreHealth(
                    store_id=store_id,
                    status="no_data",
                    last_event_timestamp=None,
                    lag_seconds=None,
                    feed_status="STALE_FEED",
                ))
                continue

            lag = (now - last_ts).total_seconds()
            feed_status = "STALE_FEED" if lag > STALE_FEED_MINUTES * 60 else "OK"

            store_healths.append(StoreHealth(
                store_id=store_id,
                status="ok",
                last_event_timestamp=last_ts.isoformat(),
                lag_seconds=round(lag, 1),
                feed_status=feed_status,
            ))

        return HealthResponse(
            status="ok",
            stores=store_healths,
            checked_at=now.isoformat(),
        )

    except Exception as e:
        logger.error(f"[Health] DB check failed: {e}")
        return HealthResponse(
            status="degraded",
            stores=[],
            checked_at=datetime.utcnow().isoformat(),
        )
