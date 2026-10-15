@echo off
rem Decharge de la VRAM les modeles Ollama du projet (sans toucher aux autres modeles).
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python -m local_llm unload
timeout /t 5
