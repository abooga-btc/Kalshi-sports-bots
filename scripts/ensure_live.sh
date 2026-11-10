#!/usr/bin/env bash
# Keep the LIVE runner (real orders) alive: restart it if its heartbeat is stale, unless data/live/KILL exists.
# Usage: bash scripts/ensure_live.sh
cd "$(dirname "$0")/.." || exit 1
mkdir -p data/live
pids=$(pgrep -f "python3 run_live.py --live" | tr '\n' ' ')
age=$(python3 -c "import json,time;print(int(time.time()-json.load(open('data/live/heartbeat.json'))['at']))" 2>/dev/null || echo 999999)
if [ -e data/live/KILL ]; then
  echo "KILL file present (data/live/KILL): not (re)starting the live runner. pids running: ${pids:-none}"
elif [ -n "$pids" ] && [ "$age" -lt 900 ]; then
  echo "live runner OK (pid $pids, heartbeat ${age}s ago)"
else
  [ -n "$pids" ] && { echo "live runner stale (heartbeat ${age}s ago) - restarting pid $pids"; kill $pids; sleep 3; }
  [ -z "$pids" ] && echo "live runner not running - starting"
  nohup python3 run_live.py --live >> data/live/run.log 2>&1 &
  sleep 25
  echo "started pid $(pgrep -f 'python3 run_live.py --live' | tr '\n' ' ')"
fi
tail -n 4 data/live/run.log 2>/dev/null
python3 report_live.py
