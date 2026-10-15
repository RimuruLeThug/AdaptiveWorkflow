@echo off
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist logs mkdir logs
echo Pilote en cours (quelques minutes, requetes Gemini espacees de 5 s)...
python pilot.py %* > logs\pilot_console.txt 2>&1
echo Termine, code %ERRORLEVEL%. Transcription dans le dossier logs.
timeout /t 15
