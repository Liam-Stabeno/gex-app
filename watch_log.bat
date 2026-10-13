@echo off
rem Live view of the dashboard's output. Closing this window does NOT stop the app.
cd /d "%~dp0"
title GEX Dashboard log (closing this window does not stop the app)
powershell -NoProfile -Command "$f = Get-ChildItem logs\app_*.log | Sort-Object LastWriteTime | Select-Object -Last 1; Write-Host ('Watching ' + $f.Name + ' - close this window any time, the dashboard keeps running.') -ForegroundColor Cyan; Get-Content $f.FullName -Encoding UTF8 -Tail 40 -Wait | Where-Object { $_ -notmatch ' DEBUG ' }"
