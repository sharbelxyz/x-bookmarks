#!/bin/bash
# Scheduler: runs sync_bookmarks.py at random intervals (1-10 hours), max 2x/day
# Designed to run as a persistent launchd agent

SYNC_SCRIPT="/Users/mshrmnsr/claude1/x-bookmarks/scripts/service.py"
STATE_FILE="/Users/mshrmnsr/claude1/x-bookmarks/data/sync_scheduler_state.json"
LOG="/Users/mshrmnsr/claude1/x-bookmarks/data/sync_stdout.log"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

log() {
    echo "[scheduler $(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG"
}

get_today() {
    date '+%Y-%m-%d'
}

get_runs_today() {
    if [ ! -f "$STATE_FILE" ]; then
        echo 0
        return
    fi
    TODAY=$(get_today)
    python3 -c "
import json
try:
    with open('$STATE_FILE') as f:
        state = json.load(f)
    print(state.get('runs', {}).get('$TODAY', 0))
except: print(0)
"
}

record_run() {
    TODAY=$(get_today)
    python3 -c "
import json, os
state = {}
if os.path.exists('$STATE_FILE'):
    try:
        with open('$STATE_FILE') as f:
            state = json.load(f)
    except: pass
runs = state.get('runs', {})
runs['$TODAY'] = runs.get('$TODAY', 0) + 1
# Keep only last 7 days
from datetime import datetime, timedelta
cutoff = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
runs = {k: v for k, v in runs.items() if k >= cutoff}
state['runs'] = runs
with open('$STATE_FILE', 'w') as f:
    json.dump(state, f, indent=2)
"
}

random_sleep() {
    # Random between 3600 (1hr) and 36000 (10hr) seconds
    SLEEP_SECS=$(python3 -c "import random; print(random.randint(3600, 36000))")
    SLEEP_HRS=$(python3 -c "print(round($SLEEP_SECS / 3600, 1))")
    log "Sleeping ${SLEEP_HRS}h (${SLEEP_SECS}s) until next sync"
    sleep "$SLEEP_SECS"
}

log "Scheduler started"

while true; do
    RUNS=$(get_runs_today)

    if [ "$RUNS" -ge 2 ]; then
        # Already ran 2x today, sleep until tomorrow
        SECS_LEFT=$(python3 -c "
from datetime import datetime, timedelta
now = datetime.now()
tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0)
print(int((tomorrow - now).total_seconds()) + 60)
")
        log "Already ran ${RUNS}x today, sleeping until tomorrow (${SECS_LEFT}s)"
        sleep "$SECS_LEFT"
        continue
    fi

    log "Running sync (run #$((RUNS + 1)) today)..."
    python3 "$SYNC_SCRIPT" sync >> "$LOG" 2>&1
    EXIT_CODE=$?

    if [ $EXIT_CODE -eq 0 ]; then
        record_run
        log "Sync completed successfully"
    else
        log "Sync failed with exit code $EXIT_CODE"
    fi

    # Random wait before next run
    random_sleep
done
