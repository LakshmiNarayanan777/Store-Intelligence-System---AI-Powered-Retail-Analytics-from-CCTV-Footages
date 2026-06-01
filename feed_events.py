"""
scripts/feed_events.py — Feed events.jsonl into the API.

Two modes:
  Normal:   reads all events, sends in batches of N (fast)
  Realtime: replays events at their original timestamps (for dashboard demo)

Usage:
  python feed_events.py --events ../data/events.jsonl --api http://localhost:8000
  python feed_events.py --events ../data/events.jsonl --api http://localhost:8000 --realtime
"""

import argparse
import json
import sys
import time
from datetime import datetime
from typing import List

import requests


def parse_ts(ts_str: str) -> datetime:
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))


def send_batch(events: List[dict], api_url: str) -> dict:
    try:
        r = requests.post(
            f"{api_url}/events/ingest",
            json={"events": events},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()
    except requests.exceptions.ConnectionError:
        print(f"  [ERROR] Cannot connect to API at {api_url}")
        print(f"  Make sure the API is running: docker compose up")
        sys.exit(1)
    except Exception as e:
        print(f"  [ERROR] {e}")
        return {"accepted": 0, "rejected": len(events), "duplicate": 0}


def feed_normal(events_path: str, api_url: str, batch_size: int):
    """Send all events as fast as possible in batches."""
    print(f"[Feed] Loading events from {events_path}")
    with open(events_path) as f:
        all_events = [json.loads(line) for line in f if line.strip()]

    print(f"[Feed] {len(all_events)} events to send in batches of {batch_size}")

    total_accepted = total_dup = total_rejected = 0

    for i in range(0, len(all_events), batch_size):
        batch = all_events[i:i + batch_size]
        result = send_batch(batch, api_url)
        total_accepted += result.get("accepted", 0)
        total_dup      += result.get("duplicate", 0)
        total_rejected += result.get("rejected", 0)

        pct = min(100, int((i + len(batch)) / len(all_events) * 100))
        print(f"  [{pct:3d}%] Batch {i//batch_size + 1}: "
              f"accepted={result.get('accepted',0)} "
              f"dup={result.get('duplicate',0)} "
              f"rejected={result.get('rejected',0)}")

    print(f"\n[Feed] Complete!")
    print(f"  Total accepted : {total_accepted}")
    print(f"  Total duplicate: {total_dup}")
    print(f"  Total rejected : {total_rejected}")


def feed_realtime(events_path: str, api_url: str, batch_size: int):
    """
    Replay events at simulated real-time pace.
    Groups events into 1-second windows and sends each window with a sleep.
    This makes the dashboard update live as you watch.
    """
    print(f"[Feed] Real-time replay mode")
    print(f"[Feed] Loading events from {events_path}")

    with open(events_path) as f:
        all_events = [json.loads(line) for line in f if line.strip()]

    if not all_events:
        print("[Feed] No events found!")
        return

    # Sort by timestamp
    all_events.sort(key=lambda e: e["timestamp"])

    first_ts = parse_ts(all_events[0]["timestamp"])
    last_ts  = parse_ts(all_events[-1]["timestamp"])
    duration = (last_ts - first_ts).total_seconds()

    print(f"[Feed] {len(all_events)} events spanning {duration:.0f}s")
    print(f"[Feed] Replaying at 10x speed (10 event-seconds per real second)")
    print(f"[Feed] Estimated replay time: {duration/10:.0f}s")
    print(f"[Feed] Starting...")

    SPEED = 10.0  # 10x faster than real time

    batch = []
    prev_bucket = None

    for event in all_events:
        ts = parse_ts(event["timestamp"])
        elapsed = (ts - first_ts).total_seconds()
        bucket = int(elapsed)  # 1-second bucket

        if prev_bucket is not None and bucket != prev_bucket:
            # Send accumulated batch
            if batch:
                result = send_batch(batch, api_url)
                print(f"  t={prev_bucket:5.0f}s | sent {len(batch):3d} events | "
                      f"accepted={result.get('accepted',0)}")
                batch = []

            # Sleep proportional to time gap (divided by speed)
            gap = (bucket - prev_bucket) / SPEED
            if gap > 0:
                time.sleep(min(gap, 2.0))  # cap at 2s max sleep

        batch.append(event)
        prev_bucket = bucket

    # Send remaining
    if batch:
        result = send_batch(batch, api_url)
        print(f"  Final batch: {len(batch)} events | accepted={result.get('accepted',0)}")

    print(f"\n[Feed] Real-time replay complete!")


def main():
    parser = argparse.ArgumentParser(description="Feed events into Store Intelligence API")
    parser.add_argument("--events",     required=True, help="Path to events.jsonl")
    parser.add_argument("--api",        default="http://localhost:8000", help="API base URL")
    parser.add_argument("--batch-size", type=int, default=100, help="Events per batch")
    parser.add_argument("--realtime",   action="store_true", help="Simulated real-time replay")
    args = parser.parse_args()

    # Quick health check
    try:
        r = requests.get(f"{args.api}/health", timeout=5)
        print(f"[Feed] API health: {r.json().get('status', 'unknown')}")
    except Exception:
        print(f"[Feed] WARNING: API not responding at {args.api}")
        print(f"[Feed] Start with: docker compose up")
        sys.exit(1)

    if args.realtime:
        feed_realtime(args.events, args.api, args.batch_size)
    else:
        feed_normal(args.events, args.api, args.batch_size)


if __name__ == "__main__":
    main()
