@echo off
rem Builds desktop\dist\GEXDash.exe (one file) from desktop\gexdash.py.
rem The first run makes desktop\.venv from requirements.txt (pywebview + PyInstaller).
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    python -m venv .venv || exit /b 1
    .venv\Scripts\python.exe -m pip install -r requirements.txt || exit /b 1
)
if not exist gexdash.ico .venv\Scripts\python.exe make_icon.py || exit /b 1
.venv\Scripts\pyinstaller.exe --noconfirm --clean --onefile --windowed --name GEXDash ^
    --icon gexdash.ico gexdash.py || exit /b 1
echo.
echo Built %~dp0dist\GEXDash.exe
