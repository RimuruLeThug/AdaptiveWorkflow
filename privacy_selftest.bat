@echo off
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist logs mkdir logs
python -m privacy selftest > logs\privacy_selftest.txt 2>&1
echo Termine, code %ERRORLEVEL%.
timeout /t 10
