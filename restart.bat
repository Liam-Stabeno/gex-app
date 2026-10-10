@echo off
rem Restart the dashboard the way the 6:14 scheduled task runs it (console hidden,
rem output in logs\). To stop it without restarting:  schtasks /End /TN GEXDashboard
schtasks /End /TN GEXDashboard >nul 2>&1
ping -n 4 127.0.0.1 >nul
schtasks /Run /TN GEXDashboard
echo Dashboard restarting - it opens in Chrome in a few seconds.
ping -n 6 127.0.0.1 >nul
