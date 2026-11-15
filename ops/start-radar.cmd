@echo off
rem radar-clone prod launcher (Windows)
rem Edit LEAK_SCAN_CMD if your scanner lives elsewhere; {target} is substituted per scan.
setlocal
set ROOT=%~dp0..
set LEAK_SCAN_CMD=node "%USERPROFILE%\Desktop\crv-leak-scan\dist\cli.js" {target} --no-history --json
set LEAK_SCAN_TIMEOUT=120
set RADAR_LOG=%ROOT%\data\radar.log

if not exist "%ROOT%\data\radar.db" (
  echo First run: pulling live data...
  python "%ROOT%\refresh.py" --live --db "%ROOT%\data\radar.db"
)
python "%ROOT%\app.py" --live --port 5000
