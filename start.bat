@echo off
title Python App Launcher

:: Install dependencies only if marker file doesn't exist
if not exist .deps_installed (
    echo Installing requirements...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo Failed to install requirements.
        pause
        exit /b 1
    )
    echo.>.deps_installed
)

echo Starting application...
python main.py

pause