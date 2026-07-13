@echo off
cd /d "%~dp0"
REM Desktop app: if pywebview is installed, hand off to pythonw (no console) and close this window,
REM so all you see is the app window -- no ugly console lingering behind it.
python -c "import webview" 1>nul 2>nul
if not errorlevel 1 (
  start "" pythonw subtap.py
  exit /b
)
REM Browser mode (no pywebview): keep the console -- it shows the URL and Ctrl+C stops the server.
title Subtap
echo.
echo   Opening in your browser at http://127.0.0.1:8756/
echo   [tip] For a native app window with NO console, install pywebview once:  pip install pywebview
echo         (then use Subtap.vbs, or just run this again -- it detaches automatically.)
echo   Close this window ^(or press Ctrl+C here^) to stop.
echo.
python subtap.py
echo.
echo   Subtap stopped.
pause
