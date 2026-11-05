@echo off
setlocal
title Overwatch 1.74 - Local Practice
cd /d "%~dp0"
set PYTHONUTF8=1
rem Optional local diagnostics preserve explicitly supplied environment values.
if not defined OW174_MOVELOG set OW174_MOVELOG=0
if not defined OW174_BODY_TRACE set OW174_BODY_TRACE=0
chcp 65001 >nul
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
  if errorlevel 1 goto failed
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -c "import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(('127.0.0.1',3724))==0 else 1)"
if not errorlevel 1 (
  echo A local lobby is already running on port 3724. Use the running game.
  echo Dashboard: http://127.0.0.1:3725
  echo Close the existing server before starting another one.
  pause
  exit /b 0
)
echo Local practice server. Keep this window open while playing.
echo Dashboard: http://127.0.0.1:3725
echo Soldier: 76 combat and training bots are experimental. See docs\STATE.md.
".venv\Scripts\python.exe" -u -B -m ow174 --mode retail --bnet-port 1118 --test-players 1 %*
if errorlevel 1 goto failed
exit /b 0
:failed
echo.
echo Startup failed. See logs\ow174.log for details.
pause
exit /b 1
