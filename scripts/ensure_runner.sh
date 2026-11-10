#!/usr/bin/env bash
# Verify the multi-variant DRY-RUN runner is alive and fresh; restart it if not; print the report.
# Usage: bash scripts/ensure_runner.sh
cd "$(dirname "$0")/.." || exit 1
pids=$(pgrep -f "python3 run_multi.py" | tr '\n' ' ')
age=$(python3 -c "import json,time;print(int(time.time()-json.load(open('data/shared/heartbeat.json'))['at']))" 2>/dev/null || echo 999999)
if [ -n "$pids" ] && [ "$age" -lt 900 ]; then
  echo "runner OK (pid $pids, heartbeat ${age}s ago)"
else
  [ -n "$pids" ] && { echo "runner stale (heartbeat ${age}s ago) - restarting pid $pids"; kill $pids; sleep 2; }
  [ -z "$pids" ] && echo "runner not running - starting"
  nohup python3 run_multi.py >> data/run.log 2>&1 &
  sleep 20
  echo "started pid $(pgrep -f 'python3 run_multi.py' | tr '\n' ' ')"
fi
tail -n 3 data/run.log
python3 report.py
