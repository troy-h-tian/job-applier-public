#!/usr/bin/env bash
# Run assist.sh detached from whoever started it, so it keeps going if the terminal, editor or Claude
# Code session that launched it closes (a background job of a session is killed when the session
# ends, even in the middle of a form).
#
#   scripts/assist_bg.sh start [assist.sh args]   # e.g. start --my-browser
#   scripts/assist_bg.sh stop                     # stops it; it saves its results first
#   scripts/assist_bg.sh status
set -euo pipefail
cd "$(dirname "$0")/.."
pidfile=data/.assist.pid
log=data/logs/assist-live.log
alive() { [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; }

case "${1:-status}" in
  start)
    shift
    if alive; then echo "Already running (pid $(cat "$pidfile")). Stop it first."; exit 1; fi
    mkdir -p data/logs
    python3 - "$pidfile" "$log" "$@" <<'PY'
import os, subprocess, sys
pidfile, log, args = sys.argv[1], sys.argv[2], sys.argv[3:]
p = subprocess.Popen(["bash", "scripts/assist.sh", *args], stdin=subprocess.DEVNULL,
                     stdout=open(log, "a"), stderr=subprocess.STDOUT, start_new_session=True)
open(pidfile, "w").write(str(p.pid))
print(f"Started (pid {p.pid}); output in {log}")
PY
    ;;
  stop)
    if ! alive; then echo "Not running."; rm -f "$pidfile"; exit 0; fi
    pid=$(cat "$pidfile")
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid"   # its whole process group; assist.sh saves on exit
    for _ in $(seq 60); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    kill -0 "$pid" 2>/dev/null && { echo "Still running after 60 s."; exit 1; }
    rm -f "$pidfile"; echo "Stopped."
    ;;
  status)
    if alive; then echo "Running (pid $(cat "$pidfile"))."; tail -3 "$log" 2>/dev/null; else echo "Not running."; fi
    ;;
  *) echo "usage: $0 start|stop|status [args]"; exit 2 ;;
esac
