"""
detect.py — Main detection + tracking pipeline.

FLOW:
  video file
    → YOLO detects people each frame
    → supervision ByteTrack assigns consistent track IDs
    → PersonTracker maps track IDs to visitor_ids
    → ZoneClassifier maps bbox position to store zone
    → EventEmitter writes structured events to .jsonl

HOW TO RUN:
  python detect.py --video ../data/videos/CAM_1.mp4 \
                   --camera CAM_ENTRY_01 \
                   --store STORE_BLR_002 \
                   --output ../data/events.jsonl

TEACHING NOTES:
  - We process every Nth frame (default N=5) for speed
  - Frame timestamp = clip_start_time + (frame_number / fps)
  - Entry/exit is detected by checking if bbox crosses a threshold line
  - Zone is determined by which horizontal band the bbox centroid falls in
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

# ── Import our modules ───────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from tracker import PersonTracker
from emit import EventEmitter, EventType


# ─── Zone Classifier ─────────────────────────────────────────────────────────

class ZoneClassifier:
    """
    Maps a bounding box centroid position to a named zone.

    For entry cameras: we only care about the entry line crossing.
    For floor cameras: we divide the frame into horizontal bands per zone.
    For billing cameras: whole frame = BILLING zone.

    This is a rule-based approach — simple, fast, explainable.
    A VLM could do this better but would be 100x slower.
    """

    def __init__(self, camera_type: str, frame_width: int, frame_height: int, store_layout: dict):
        self.camera_type = camera_type
        self.W = frame_width
        self.H = frame_height
        self.layout = store_layout

        # Entry threshold line (horizontal line at 40% from top)
        self.entry_line_y = int(frame_height * 0.4)

        # Zone bands for floor camera (divide vertically)
        zones = [z for z in store_layout.get("zones", [])
                 if z.get("camera") in ("CAM_FLOOR_01", "CAM_FLOOR_02")]
        self.floor_zones = zones if zones else [
            {"zone_id": "SKINCARE", "sku_zone": "MOISTURISER"},
            {"zone_id": "HAIRCARE", "sku_zone": "SHAMPOO"},
            {"zone_id": "MAKEUP",   "sku_zone": "FOUNDATION"},
        ]

    def classify(self, bbox, prev_y: Optional[float] = None):
        """
        Returns (zone_id, sku_zone, is_entry_event, is_exit_event)
        """
        x1, y1, x2, y2 = bbox
        cx = (x1 + x2) / 2
        cy = (y1 + y2) / 2

        is_entry = False
        is_exit = False

        if self.camera_type == "entry":
            # Detect line crossing
            if prev_y is not None:
                if prev_y < self.entry_line_y <= cy:
                    is_entry = True   # moving downward = entering store
                elif prev_y > self.entry_line_y >= cy:
                    is_exit = True    # moving upward = exiting store
            zone_id = "ENTRY"
            sku_zone = None

        elif self.camera_type == "billing":
            zone_id = "BILLING"
            sku_zone = "CHECKOUT"

        else:  # floor camera
            # Divide frame into N equal bands top-to-bottom
            n = len(self.floor_zones)
            band = int((cy / self.H) * n)
            band = min(band, n - 1)
            z = self.floor_zones[band]
            zone_id = z["zone_id"]
            sku_zone = z.get("sku_zone")

        return zone_id, sku_zone, is_entry, is_exit


# ─── Staff Detector ──────────────────────────────────────────────────────────

class StaffDetector:
    """
    Classifies a person as staff based on:
    1. Uniform color heuristic (dominant color in bbox)
    2. Long presence (tracked for many frames)
    3. Movement pattern (staff move back and forth)

    TEACHING: This is a simple heuristic. A real system would use
    a separate classifier trained on staff uniforms.
    """

    # Staff often wear solid-color uniforms — look for dominant non-skin hue
    STAFF_COLOR_THRESHOLDS = {
        "blue":  ([100, 50, 50], [130, 255, 255]),   # blue uniform
        "black": ([0, 0, 0],     [180, 255, 50]),     # black uniform
        "white": ([0, 0, 200],   [180, 30, 255]),     # white uniform
    }

    def __init__(self):
        self._frame_counts: dict = {}   # track_id → frame count

    def update(self, track_id: int):
        self._frame_counts[track_id] = self._frame_counts.get(track_id, 0) + 1

    def is_staff(self, track_id: int, frame: np.ndarray, bbox) -> bool:
        count = self._frame_counts.get(track_id, 0)
        # Simple heuristic: present for very long = staff
        if count > 300:
            return True

        # Color-based check on the upper body region
        x1, y1, x2, y2 = [int(v) for v in bbox]
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(frame.shape[1], x2), min(frame.shape[0], y2)

        if x2 <= x1 or y2 <= y1:
            return False

        # Take top 40% of bbox (torso — where uniform is most visible)
        torso_y2 = y1 + int((y2 - y1) * 0.4)
        crop = frame[y1:torso_y2, x1:x2]
        if crop.size == 0:
            return False

        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        for color, (lo, hi) in self.STAFF_COLOR_THRESHOLDS.items():
            mask = cv2.inRange(hsv, np.array(lo), np.array(hi))
            ratio = mask.sum() / (mask.size * 255)
            if ratio > 0.4:   # 40%+ of torso is uniform color
                return True

        return False


# ─── Main Pipeline ────────────────────────────────────────────────────────────

def process_video(
    video_path: str,
    camera_id: str,
    camera_type: str,      # "entry" | "floor" | "billing"
    store_id: str,
    output_path: str,
    store_layout: dict,
    clip_start_time: Optional[datetime] = None,
    frame_skip: int = 3,   # process every Nth frame
    conf_threshold: float = 0.3,
):
    """
    Core processing loop.

    TEACHING — Frame skip:
      At 30fps, processing every frame = 30 detections/sec per video.
      With 5 videos that's 150 YOLO inferences/sec — too slow on CPU.
      We skip every 3rd frame → ~10 detections/sec, still accurate enough
      because people move slowly relative to FPS.
    """
    from ultralytics import YOLO
    import supervision as sv

    print(f"\n[Pipeline] Processing {video_path}")
    print(f"  Camera: {camera_id} ({camera_type})")
    print(f"  Store:  {store_id}")
    print(f"  Output: {output_path}")

    # ── Load model ──────────────────────────────────────────────────────────
    # YOLOv8n = nano model, fast on CPU, ~37ms/frame
    # We only want class 0 = "person"
    model = YOLO("yolov8n.pt")
    print(f"  Model:  YOLOv8n loaded")

    # ── Open video ──────────────────────────────────────────────────────────
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"  Video:  {W}x{H} @ {fps:.1f}fps, {total_frames} frames")

    if clip_start_time is None:
        clip_start_time = datetime(2026, 3, 3, 10, 0, 0, tzinfo=timezone.utc)

    # ── Initialize components ───────────────────────────────────────────────
    tracker_sv = sv.ByteTrack()
    person_tracker = PersonTracker(store_id)
    zone_clf = ZoneClassifier(camera_type, W, H, store_layout)
    staff_det = StaffDetector()
    emitter = EventEmitter(output_path, store_id, camera_id)

    # State for zone tracking per track_id
    prev_cy: dict = {}          # track_id → previous centroid Y
    active_zones: dict = {}     # track_id → current zone_id
    billing_queue: list = []    # visitor_ids currently in billing

    frame_num = 0
    processed = 0

    print(f"  Processing frames (skip={frame_skip})...")

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame_num += 1
        if frame_num % frame_skip != 0:
            continue

        processed += 1
        frame_time = clip_start_time + timedelta(seconds=frame_num / fps)
        ts = frame_time.timestamp()

        # ── YOLO detection ───────────────────────────────────────────────
        results = model(frame, classes=[0], conf=conf_threshold, verbose=False)[0]

        # Convert to supervision Detections
        detections = sv.Detections.from_ultralytics(results)
        if len(detections) == 0:
            continue

        # ── ByteTrack update ─────────────────────────────────────────────
        detections = tracker_sv.update_with_detections(detections)

        queue_depth = len(billing_queue)

        for i in range(len(detections)):
            bbox = detections.xyxy[i]           # [x1,y1,x2,y2]
            track_id = int(detections.tracker_id[i])
            confidence = float(detections.confidence[i])

            x1, y1, x2, y2 = bbox
            cx = (x1 + x2) / 2
            cy = (y1 + y2) / 2

            # ── Staff detection ──────────────────────────────────────────
            staff_det.update(track_id)
            is_staff = staff_det.is_staff(track_id, frame, bbox)

            # ── Update tracker ───────────────────────────────────────────
            state = person_tracker.update_track(track_id, tuple(bbox), ts)
            state.is_staff = is_staff

            visitor_id = state.visitor_id
            prev_y_val = prev_cy.get(track_id)
            prev_cy[track_id] = cy

            # ── Zone classification ──────────────────────────────────────
            zone_id, sku_zone, is_entry_cross, is_exit_cross = zone_clf.classify(
                bbox, prev_y_val
            )

            # ── Emit ENTRY event ─────────────────────────────────────────
            if is_entry_cross and track_id not in active_zones:
                # Check re-entry
                was_reentry = person_tracker.is_reentry(visitor_id, ts)
                evt_type = EventType.REENTRY if was_reentry else EventType.ENTRY

                event = emitter.build_event(
                    visitor_id=visitor_id,
                    event_type=evt_type,
                    timestamp=frame_time,
                    zone_id=None,
                    is_staff=is_staff,
                    confidence=confidence,
                    session_seq=person_tracker.next_seq(track_id),
                )
                emitter.emit(event)
                active_zones[track_id] = None

            # ── Emit EXIT event ──────────────────────────────────────────
            elif is_exit_cross and track_id in active_zones:
                event = emitter.build_event(
                    visitor_id=visitor_id,
                    event_type=EventType.EXIT,
                    timestamp=frame_time,
                    zone_id=None,
                    is_staff=is_staff,
                    confidence=confidence,
                    session_seq=person_tracker.next_seq(track_id),
                )
                emitter.emit(event)
                person_tracker.mark_exit(track_id, ts)
                active_zones.pop(track_id, None)
                person_tracker.exit_zone(visitor_id)

                # Check billing abandonment
                if visitor_id in billing_queue:
                    billing_queue.remove(visitor_id)
                    evt = emitter.build_event(
                        visitor_id=visitor_id,
                        event_type=EventType.BILLING_QUEUE_ABANDON,
                        timestamp=frame_time,
                        zone_id="BILLING",
                        is_staff=is_staff,
                        confidence=confidence,
                        session_seq=person_tracker.next_seq(track_id),
                    )
                    emitter.emit(evt)

            # ── Zone entry / exit (floor + billing cams) ────────────────
            elif camera_type in ("floor", "billing"):
                prev_zone = active_zones.get(track_id)

                if prev_zone != zone_id:
                    # Zone exit
                    if prev_zone is not None:
                        zs = person_tracker.exit_zone(visitor_id)
                        dwell = person_tracker.get_dwell_ms(visitor_id, ts) if zs else 0
                        evt = emitter.build_event(
                            visitor_id=visitor_id,
                            event_type=EventType.ZONE_EXIT,
                            timestamp=frame_time,
                            zone_id=prev_zone,
                            dwell_ms=dwell,
                            is_staff=is_staff,
                            confidence=confidence,
                            session_seq=person_tracker.next_seq(track_id),
                        )
                        emitter.emit(evt)

                    # Zone enter
                    person_tracker.enter_zone(visitor_id, zone_id, ts)
                    active_zones[track_id] = zone_id

                    enter_evt_type = EventType.ZONE_ENTER
                    kw = {}

                    # Billing queue join
                    if zone_id == "BILLING" and not is_staff:
                        if visitor_id not in billing_queue:
                            billing_queue.append(visitor_id)
                        if queue_depth > 0:
                            enter_evt_type = EventType.BILLING_QUEUE_JOIN
                            kw["queue_depth"] = queue_depth

                    evt = emitter.build_event(
                        visitor_id=visitor_id,
                        event_type=enter_evt_type,
                        timestamp=frame_time,
                        zone_id=zone_id,
                        is_staff=is_staff,
                        confidence=confidence,
                        sku_zone=sku_zone,
                        session_seq=person_tracker.next_seq(track_id),
                        **kw,
                    )
                    emitter.emit(evt)

                # Zone dwell (every 30s)
                elif person_tracker.should_emit_dwell(visitor_id, ts):
                    dwell_ms = person_tracker.get_dwell_ms(visitor_id, ts)
                    evt = emitter.build_event(
                        visitor_id=visitor_id,
                        event_type=EventType.ZONE_DWELL,
                        timestamp=frame_time,
                        zone_id=zone_id,
                        dwell_ms=dwell_ms,
                        is_staff=is_staff,
                        confidence=confidence,
                        sku_zone=sku_zone,
                        session_seq=person_tracker.next_seq(track_id),
                    )
                    emitter.emit(evt)

        if processed % 100 == 0:
            pct = (frame_num / total_frames) * 100
            print(f"  [{pct:.0f}%] frame {frame_num}/{total_frames}, "
                  f"tracked {len(detections)} people")

    cap.release()
    emitter.close()
    print(f"[Pipeline] Done: {processed} frames processed")


# ─── CLI Entry Point ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Store Intelligence Detection Pipeline")
    parser.add_argument("--video",   required=True,  help="Path to video file")
    parser.add_argument("--camera",  required=True,  help="Camera ID (e.g. CAM_ENTRY_01)")
    parser.add_argument("--type",    required=True,  choices=["entry","floor","billing"],
                        help="Camera type")
    parser.add_argument("--store",   default="STORE_BLR_002", help="Store ID")
    parser.add_argument("--output",  default="events.jsonl",  help="Output .jsonl file")
    parser.add_argument("--layout",  default="../data/store_layout.json")
    parser.add_argument("--skip",    type=int, default=3, help="Process every Nth frame")
    parser.add_argument("--conf",    type=float, default=0.3, help="Detection confidence threshold")
    args = parser.parse_args()

    with open(args.layout) as f:
        layout_data = json.load(f)

    store_layout = next(
        (s for s in layout_data["stores"] if s["store_id"] == args.store),
        layout_data["stores"][0]
    )

    process_video(
        video_path=args.video,
        camera_id=args.camera,
        camera_type=args.type,
        store_id=args.store,
        output_path=args.output,
        store_layout=store_layout,
        frame_skip=args.skip,
        conf_threshold=args.conf,
    )


if __name__ == "__main__":
    main()
