@echo off
cd /d "%~dp0"
rem Окно выбора папок Windows: контейнер сам открыть его не может
start "" pythonw gui.py --picker
docker compose up -d --build
if errorlevel 1 pause
