"""
emit.py — Event schema definition and emission logic.

Every detection the pipeline makes becomes a structured event here.
Think of this as the "language" the whole system speaks.
"""

import uuid
import json
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass, asdict


# ─── Event Type Catalogue ────────────────────────────────────────────────────

class EventType:
    ENTRY               = "ENTRY"
    EXIT                = "EXIT"
    ZONE_ENTER          = "ZONE_ENTER"
    ZONE_EXIT           = "ZONE_EXIT"
    ZONE_DWELL          = "ZONE_DWELL"
    BILLING_QUEUE_JOIN  = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
    REENTRY             = "REENTRY"


@dataclass
class EventMetadata:
    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: int = 0


@dataclass
class StoreEvent:
    """
    The canonical event structure every part of the system uses.
    Matches the schema in the problem statement exactly.
    """
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: str
    timestamp: str
    zone_id: Optional[str]
    dwell_ms: int
    is_staff: bool
    confidence: float
    metadata: EventMetadata

    # Auto-generated
    event_id: str = ""

    def __post_init__(self):
        if not self.event_id:
            self.event_id = str(uuid.uuid4())

    def to_dict(self) -> dict:
        d = asdict(self)
        d["metadata"] = asdict(self.metadata)
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


# ─── EventEmitter ─────────────────────────────────────────────────────────────

class EventEmitter:
    """
    Collects events from the detection pipeline and writes them
    to a .jsonl file (one JSON object per line — easy to stream into the API).
    """

    def __init__(self, output_path: str, store_id: str, camera_id: str):
        self.output_path = output_path
        self.store_id = store_id
        self.camera_id = camera_id
        self._file = open(output_path, "a")
        self._count = 0

    def emit(self, event: StoreEvent):
        self._file.write(event.to_json() + "\n")
        self._file.flush()
        self._count += 1

    def build_event(
        self,
        visitor_id: str,
        event_type: str,
        timestamp: datetime,
        zone_id: Optional[str] = None,
        dwell_ms: int = 0,
        is_staff: bool = False,
        confidence: float = 1.0,
        queue_depth: Optional[int] = None,
        sku_zone: Optional[str] = None,
        session_seq: int = 0,
    ) -> StoreEvent:
        return StoreEvent(
            store_id=self.store_id,
            camera_id=self.camera_id,
            visitor_id=visitor_id,
            event_type=event_type,
            timestamp=timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
            zone_id=zone_id,
            dwell_ms=dwell_ms,
            is_staff=is_staff,
            confidence=confidence,
            metadata=EventMetadata(
                queue_depth=queue_depth,
                sku_zone=sku_zone,
                session_seq=session_seq,
            ),
        )

    def close(self):
        self._file.close()
        print(f"[Emitter] Wrote {self._count} events to {self.output_path}")
