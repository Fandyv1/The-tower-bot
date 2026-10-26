@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1

rem No args: launch GUI. With args: CLI.
if "%~1"=="" (
  where pythonw >nul 2>nul
  if errorlevel 1 (
    start "" python -m towerbot_pc.gui
  ) else (
    start "" pythonw -m towerbot_pc.gui
  )
  exit /b 0
)

python -m towerbot_pc.cli %*
