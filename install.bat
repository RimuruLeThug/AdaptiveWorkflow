@echo off
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo === Installation de AdaptiveWorkflow ===
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [ERREUR] Python est introuvable. Installez Python 3.11 ou plus recent : https://www.python.org/downloads/
  echo Cochez "Add python.exe to PATH" pendant l'installation, puis relancez ce script.
  goto fin
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
  echo [ERREUR] Python 3.11 ou plus recent est requis. Version trouvee :
  python --version
  goto fin
)
echo [OK] Python :
python --version

echo.
echo Installation de la dependance recommandee (tzdata)...
python -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 echo [Attention] pip a echoue : le projet fonctionne sans, avec un calcul d'heure integre.

echo.
if exist ".env" (
  echo [OK] Le fichier .env existe deja : il n'est pas modifie.
) else (
  copy ".env.example" ".env" >nul
  echo [OK] Fichier .env cree. Ouvrez-le et renseignez GEMINI_API_KEY=votre_cle
  echo      Cle gratuite : https://aistudio.google.com ^(Cles API^)
)

echo.
where ollama >nul 2>nul
if errorlevel 1 (
  echo [A FAIRE] Ollama est introuvable. Installez-le depuis https://ollama.com puis relancez ce script.
  goto fin
)
echo [OK] Ollama detecte.
set /p PULL="Telecharger les deux modeles Qwen (environ 7 Go) ? (o/n) : "
if /i "%PULL%"=="o" (
  ollama pull qwen3.5:4b-q8_0
  ollama pull qwen2.5:3b-instruct-q4_K_M
) else (
  echo Telechargement ignore. Commandes manuelles :
  echo   ollama pull qwen3.5:4b-q8_0
  echo   ollama pull qwen2.5:3b-instruct-q4_K_M
)

echo.
echo === Termine. Renseignez GEMINI_API_KEY dans .env, puis lancez start.bat ===
:fin
echo.
pause
endlocal
