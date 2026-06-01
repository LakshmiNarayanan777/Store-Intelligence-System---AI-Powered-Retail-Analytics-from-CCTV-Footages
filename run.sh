#!/bin/bash
# run.sh — Process all CCTV clips and emit events into events.jsonl
# Usage: bash run.sh [path/to/videos/]
#
# This script runs the detection pipeline on all 5 cameras
# and appends all events to a single events.jsonl file.

set -e

VIDEOS_DIR="${1:-../data/videos}"
OUTPUT="${2:-../data/events.jsonl}"
LAYOUT="../data/store_layout.json"
STORE="STORE_BLR_002"

echo "========================================="
echo " Purplle Store Intelligence Pipeline"
echo "========================================="
echo "Videos dir : $VIDEOS_DIR"
echo "Output     : $OUTPUT"
echo ""

# Clear previous output
> "$OUTPUT"

# Map camera files to their types
declare -A CAM_TYPES=(
    ["CAM_1.mp4"]="entry:CAM_ENTRY_01"
    ["CAM_2.mp4"]="floor:CAM_FLOOR_01"
    ["CAM_3.mp4"]="billing:CAM_BILLING_01"
    ["CAM_4.mp4"]="floor:CAM_FLOOR_02"
    ["CAM_5.mp4"]="entry:CAM_ENTRY_02"
)

for filename in "${!CAM_TYPES[@]}"; do
    VIDEO_PATH="$VIDEOS_DIR/$filename"
    if [ ! -f "$VIDEO_PATH" ]; then
        echo "[SKIP] $filename not found at $VIDEO_PATH"
        continue
    fi

    IFS=':' read -r CAM_TYPE CAM_ID <<< "${CAM_TYPES[$filename]}"

    echo "[RUN] $filename → $CAM_ID ($CAM_TYPE)"
    python detect.py \
        --video "$VIDEO_PATH" \
        --camera "$CAM_ID" \
        --type "$CAM_TYPE" \
        --store "$STORE" \
        --output "$OUTPUT" \
        --layout "$LAYOUT" \
        --skip 3 \
        --conf 0.3

    echo "[DONE] $filename"
    echo ""
done

echo "========================================="
echo " All clips processed!"
echo " Events written to: $OUTPUT"
echo " Line count: $(wc -l < "$OUTPUT")"
echo "========================================="
