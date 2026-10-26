@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
where pythonw >nul 2>nul
if errorlevel 1 (
  python -m towerbot_pc.gui %*
) else (
  start "" pythonw -m towerbot_pc.gui %*
)
