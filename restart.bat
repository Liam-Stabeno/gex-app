@echo off
rem Restart the dashboard the way the 6:14 scheduled task runs it (console hidden,
rem output in logs\). Stops any running copy first, including one from start.bat.
rem To stop it without restarting, run this with:  restart.bat stop
schtasks /End /TN GEXDashboard >nul 2>&1
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*src\dashboard.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
if /i "%~1"=="stop" (
    echo Dashboard stopped.
    ping -n 3 127.0.0.1 >nul
    exit /b 0
)
ping -n 4 127.0.0.1 >nul
schtasks /Run /TN GEXDashboard
echo Dashboard restarting - it opens in Chrome in a few seconds.
ping -n 6 127.0.0.1 >nul
