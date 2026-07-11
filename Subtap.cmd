@echo off
title Subtap
cd /d "%~dp0"
echo.
echo   Starting Subtap...  a browser tab will open at http://127.0.0.1:8756/
echo   Load your audio + captions from the top bar. Close this window (or Ctrl+C) to stop.
echo.
python subtap.py
echo.
echo   Subtap stopped.
pause
