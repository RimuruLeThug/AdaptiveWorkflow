@echo off
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist logs mkdir logs
echo Pilote escalade en cours...
python pilot.py pilot_escalade.txt > logs\pilot_console.txt 2>&1
echo Termine, code %ERRORLEVEL%.
timeout /t 15
