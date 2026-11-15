@echo off
rem ==== CRV recon - double-click launcher ====
rem Zero-config: scanner auto-detected, first run pulls live data, then serves.
title CRV recon
cd /d "%~dp0"
set LEAK_SCAN_TIMEOUT=120
set RADAR_LOG=%~dp0data\radar.log

if not exist "data\radar.db" (
  echo First run: pulling live programs ^(takes ~2 min^)...
  python refresh.py --live
)
echo Serving on http://127.0.0.1:5000 — close this window to stop.
start "" http://127.0.0.1:5000/
python app.py --live --port 5000
