@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist logs mkdir logs
python -m towerbot_pc.cli run --max-runtime 480 >> "logs\task.log" 2>&1
exit /b %ERRORLEVEL%
