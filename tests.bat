@echo off
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist logs mkdir logs
python -m unittest discover -s tests -t . > logs\tests.txt 2>&1
echo Termine, code %ERRORLEVEL%. Resultat dans logs\tests.txt
timeout /t 10
