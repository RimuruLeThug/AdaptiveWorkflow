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
  echo [OK] Fichier .env cree. GEMINI_API_KEY n'est utile que si un role est confie a Gemini
  echo      Cle gratuite : https://aistudio.google.com ^(Cles API^)
)

echo.
where ollama >nul 2>nul
if errorlevel 1 (
  echo [A FAIRE] Ollama est introuvable. Installez-le depuis https://ollama.com puis relancez ce script.
  echo           ^(Modele local : replis et profils donnees personnelles.^)
  goto claude
)
echo [OK] Ollama detecte.
set /p PULL="Telecharger le modele Qwen local qwen3.5:9b-q4_K_M (environ 6,6 Go) ? (o/n) : "
if /i "%PULL%"=="o" (
  ollama pull qwen3.5:9b-q4_K_M
) else (
  echo Telechargement ignore. Commande manuelle :
  echo   ollama pull qwen3.5:9b-q4_K_M
)

echo.
:claude
where claude >nul 2>nul
if errorlevel 1 (
  echo [A FAIRE] Claude Code est introuvable. C'est l'agent par defaut de tous les roles ^(V3^).
  echo           Installez-le ^(https://claude.com/claude-code^), puis : claude auth login
  echo           Sans lui, tout passe au Qwen local.
) else (
  echo [OK] Claude Code detecte. Connexion ^(compte claude.ai^) : claude auth login
)

echo.
echo === Termine. Lancez start.bat ^(GEMINI_API_KEY dans .env seulement si un role utilise Gemini^) ===
:fin
echo.
pause
endlocal
