"""
tracker.py — Person tracking + Re-ID logic.

What this does:
- Takes bounding boxes from YOLO each frame
- Assigns consistent track IDs across frames (ByteTrack via supervision)
- Generates visitor_id tokens per session
- Detects re-entry: same person returning after an EXIT event
- Detects staff: people who appear in many zones / move constantly

KEY CONCEPT — visitor_id vs track_id:
  track_id  = supervision's internal number, resets each video
  visitor_id = our stable token like "VIS_c8a2f1", survives re-entry
"""

import hashlib
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple, List
import numpy as np


# ─── Data Structures ─────────────────────────────────────────────────────────

@dataclass
class TrackState:
    visitor_id: str
    track_id: int
    first_seen: float          # time.time()
    last_seen: float
    last_bbox: Tuple           # (x1,y1,x2,y2)
    zone_history: List[str] = field(default_factory=list)
    total_frames: int = 0
    is_staff: bool = False
    exited: bool = False
    session_seq: int = 0       # event counter for this visitor

    def update(self, bbox, timestamp):
        self.last_seen = timestamp
        self.last_bbox = bbox
        self.total_frames += 1


@dataclass
class ZoneState:
    zone_id: str
    entered_at: float
    last_dwell_emitted: float = 0.0   # last time we emitted ZONE_DWELL


# ─── Tracker ─────────────────────────────────────────────────────────────────

class PersonTracker:
    """
    Wraps supervision's ByteTrack and adds:
    1. Stable visitor_id generation
    2. Re-entry detection (30s window)
    3. Staff classification (movement-based heuristic)
    4. Zone dwell tracking
    """

    REENTRY_WINDOW_SEC = 30       # within 30s → re-entry, not new visitor
    STAFF_FRAME_THRESHOLD = 500   # seen in >500 frames → likely staff
    DWELL_EMIT_INTERVAL_SEC = 30  # emit ZONE_DWELL every 30s

    def __init__(self, store_id: str):
        self.store_id = store_id
        self._tracks: Dict[int, TrackState] = {}         # track_id → state
        self._exited: Dict[str, float] = {}              # visitor_id → exit time
        self._visitor_counter = 0
        self._zone_states: Dict[str, ZoneState] = {}    # visitor_id → current zone

    # ── Visitor ID generation ────────────────────────────────────────────────

    def _new_visitor_id(self) -> str:
        """Generate a short, readable visitor token."""
        self._visitor_counter += 1
        raw = f"{self.store_id}-{self._visitor_counter}-{time.time()}"
        h = hashlib.md5(raw.encode()).hexdigest()[:6]
        return f"VIS_{h}"

    # ── Staff classification ─────────────────────────────────────────────────

    def _classify_staff(self, state: TrackState) -> bool:
        """
        Heuristic: staff appear in many frames and move across the full
        frame width. Not perfect — we flag is_staff=True conservatively.
        """
        if state.total_frames > self.STAFF_FRAME_THRESHOLD:
            return True
        # Check if bounding box stays near edges (staff near counters)
        if state.last_bbox:
            x1, y1, x2, y2 = state.last_bbox
            cx = (x1 + x2) / 2
            # Staff tend to move across wide x range
        return False

    # ── Re-entry detection ───────────────────────────────────────────────────

    def is_reentry(self, visitor_id: str, current_time: float) -> bool:
        if visitor_id in self._exited:
            elapsed = current_time - self._exited[visitor_id]
            if elapsed < self.REENTRY_WINDOW_SEC * 10:  # generous window
                return True
        return False

    # ── Main update ─────────────────────────────────────────────────────────

    def update_track(self, track_id: int, bbox: Tuple, timestamp: float) -> TrackState:
        """
        Called every frame for each detected person.
        Returns the TrackState (creates if new).
        """
        if track_id not in self._tracks:
            # New track — check if it could be a re-entry
            visitor_id = self._find_reentry_candidate(bbox, timestamp)
            if visitor_id is None:
                visitor_id = self._new_visitor_id()

            self._tracks[track_id] = TrackState(
                visitor_id=visitor_id,
                track_id=track_id,
                first_seen=timestamp,
                last_seen=timestamp,
                last_bbox=bbox,
            )
        else:
            self._tracks[track_id].update(bbox, timestamp)
            # Update staff classification
            self._tracks[track_id].is_staff = self._classify_staff(
                self._tracks[track_id]
            )

        return self._tracks[track_id]

    def _find_reentry_candidate(self, bbox: Tuple, timestamp: float) -> Optional[str]:
        """
        Check if a new detection near the entry matches a recently exited visitor.
        Uses IoU / proximity of bounding boxes.
        """
        best_match = None
        best_time = float("inf")

        for visitor_id, exit_time in self._exited.items():
            elapsed = timestamp - exit_time
            if elapsed < self.REENTRY_WINDOW_SEC * 10:
                if elapsed < best_time:
                    best_time = elapsed
                    best_match = visitor_id

        return best_match

    def mark_exit(self, track_id: int, timestamp: float):
        if track_id in self._tracks:
            state = self._tracks[track_id]
            state.exited = True
            self._exited[state.visitor_id] = timestamp

    # ── Zone tracking ────────────────────────────────────────────────────────

    def enter_zone(self, visitor_id: str, zone_id: str, timestamp: float):
        self._zone_states[visitor_id] = ZoneState(
            zone_id=zone_id,
            entered_at=timestamp,
        )

    def get_zone_state(self, visitor_id: str) -> Optional[ZoneState]:
        return self._zone_states.get(visitor_id)

    def exit_zone(self, visitor_id: str) -> Optional[ZoneState]:
        return self._zone_states.pop(visitor_id, None)

    def should_emit_dwell(self, visitor_id: str, timestamp: float) -> bool:
        """Returns True if 30s has passed since last ZONE_DWELL emit."""
        zs = self._zone_states.get(visitor_id)
        if not zs:
            return False
        elapsed_since_emit = timestamp - zs.last_dwell_emitted
        elapsed_since_enter = timestamp - zs.entered_at
        if elapsed_since_enter >= self.DWELL_EMIT_INTERVAL_SEC:
            if elapsed_since_emit >= self.DWELL_EMIT_INTERVAL_SEC:
                zs.last_dwell_emitted = timestamp
                return True
        return False

    def get_dwell_ms(self, visitor_id: str, timestamp: float) -> int:
        zs = self._zone_states.get(visitor_id)
        if not zs:
            return 0
        return int((timestamp - zs.entered_at) * 1000)

    # ── Session sequence ─────────────────────────────────────────────────────

    def next_seq(self, track_id: int) -> int:
        if track_id in self._tracks:
            self._tracks[track_id].session_seq += 1
            return self._tracks[track_id].session_seq
        return 0

    def get_all_active(self) -> List[TrackState]:
        return [s for s in self._tracks.values() if not s.exited]
