@echo off
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist ".env" (
  echo Fichier .env absent : lancez d'abord install.bat
  pause
  exit /b 1
)
python -m qwen_ui
echo.
echo Programme termine ^(code %ERRORLEVEL%^).
pause
