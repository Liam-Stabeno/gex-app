@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
rem Manual run: keep the console window visible (the scheduled task hides it)
set GEX_SHOW_CONSOLE=1
python src\dashboard.py
pause
