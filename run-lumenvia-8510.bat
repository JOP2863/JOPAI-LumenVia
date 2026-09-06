@echo off
setlocal enabledelayedexpansion

title JOPAI-LumenVia (Port 8510)
cd /d "%~dp0"

echo.
echo ==========================================
echo   JOPAI LumenVia — Calendrier liturgique
echo ==========================================
echo.
echo - Source liturgique : AELF (zone: france)
echo - Pages : C'est quoi ^| Dimanche ^| Aide-Memoire ^| Nous rejoindre
echo - Admin test ressources : ajoute ?admin=1 a l'URL
echo - Port : 8510 (registre NEXUS / galaxie_manifest)
echo.
echo URL (apres demarrage) :
echo - http://localhost:8510
echo - http://localhost:8510/?admin=1
echo.

echo Lancement de Streamlit...
streamlit run app.py --server.port 8510

echo.
echo Streamlit arrete.
pause
