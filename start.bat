@echo off
rem Start the dashboard in the background, through the GEXDashboard scheduled task:
rem no console window to close by accident (closing one stopped the app mid-session
rem on 2026-10-10 and 2026-10-13). Output: logs\  (live view: watch_log.bat)
rem Restart / stop: restart.bat  /  restart.bat stop
cd /d "%~dp0"
powershell -NoProfile -Command "try { $null = Invoke-WebRequest -UseBasicParsing http://127.0.0.1:5000/api/history_days -TimeoutSec 3; exit 0 } catch { exit 1 }"
if not errorlevel 1 (
    echo GEX Dashboard is already running - opening it.
    start "" http://127.0.0.1:5000
    ping -n 3 127.0.0.1 >nul
    exit /b 0
)
schtasks /Run /TN GEXDashboard >nul 2>&1
if errorlevel 1 (
    echo No GEXDashboard scheduled task - running it in this window instead.
    echo Closing this window stops the dashboard.
    call .venv\Scripts\activate.bat
    set GEX_SHOW_CONSOLE=1
    python src\dashboard.py
    pause
    exit /b
)
echo GEX Dashboard is starting in the background - it opens in Chrome in a few seconds.
echo Live output: watch_log.bat    Restart / stop: restart.bat / restart.bat stop
ping -n 6 127.0.0.1 >nul
