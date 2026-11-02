#!/usr/bin/env bash
set -e

# ====================================================================
# VTU Campus.Life Autonomous Attendance - Isolated Runner
# ====================================================================
# Strictly isolated to /home/ubuntu/vtu_attendance
# No system-wide modifications, no port binding, no process interference.
# ====================================================================

APP_DIR="/home/ubuntu/vtu_attendance"
LOG_FILE="$APP_DIR/attendance.log"

cd "$APP_DIR"

ACTION="${1:-auto}"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S %Z')
echo "==========================================================" >> "$LOG_FILE"
echo "[$TIMESTAMP] Starting VTU Attendance Runner (Action: $ACTION)" >> "$LOG_FILE"

# Determine action if 'auto'
if [ "$ACTION" = "auto" ]; then
    HOUR=$(date -u +%H)
    if [ "$HOUR" -lt "08" ]; then
        ACTION="checkin"
    else
        ACTION="checkout"
    fi
fi

# 1. Pull latest state from GitHub cleanly
git fetch origin main >> "$LOG_FILE" 2>&1 || true
git reset --hard origin/main >> "$LOG_FILE" 2>&1 || true

# 2. Execute attendance action with jitter
JITTER_FLAG="--jitter"
if [ "$2" = "--no-jitter" ]; then
    JITTER_FLAG=""
fi

echo "[$TIMESTAMP] Executing python3 campus_life_auto.py --action $ACTION $JITTER_FLAG" >> "$LOG_FILE"
python3 campus_life_auto.py --action "$ACTION" $JITTER_FLAG >> "$LOG_FILE" 2>&1

# 3. Verify against official VTU APTS server database
echo "[$TIMESTAMP] Running server verification..." >> "$LOG_FILE"
python3 campus_life_auto.py --action verify >> "$LOG_FILE" 2>&1

# 4. Commit and push updated progress back to GitHub
git add events_db.json attendance_state.json >> "$LOG_FILE" 2>&1
if ! git diff --staged --quiet; then
    echo "[$TIMESTAMP] Pushing updated event hours to GitHub..." >> "$LOG_FILE"
    git commit -m "Auto-sync attendance progress [Oracle VM: $(date '+%Y-%m-%d %H:%M')]" >> "$LOG_FILE" 2>&1
    git push origin main >> "$LOG_FILE" 2>&1 || true
else
    echo "[$TIMESTAMP] No state change to commit." >> "$LOG_FILE"
fi

echo "[$TIMESTAMP] Completed successfully." >> "$LOG_FILE"
echo "==========================================================" >> "$LOG_FILE"
