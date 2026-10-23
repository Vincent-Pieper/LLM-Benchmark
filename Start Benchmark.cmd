@echo off
cd /d "%~dp0"
py -3 server.py
if errorlevel 1 (
  echo.
  echo Benchmark could not start. Python 3 is required.
  pause
)
