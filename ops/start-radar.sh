#!/usr/bin/env bash
# radar-clone prod launcher (Linux/macOS)
# Edit LEAK_SCAN_CMD if your scanner lives elsewhere; {target} is substituted per scan.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export LEAK_SCAN_CMD="node \"$HOME/Desktop/crv-leak-scan/dist/cli.js\" {target} --no-history --json"
export LEAK_SCAN_TIMEOUT=120
export RADAR_LOG="$ROOT/data/radar.log"

if [ ! -f "$ROOT/data/radar.db" ]; then
  echo "First run: pulling live data..."
  python "$ROOT/refresh.py" --live --db "$ROOT/data/radar.db"
fi
exec python "$ROOT/app.py" --live --port 5000
