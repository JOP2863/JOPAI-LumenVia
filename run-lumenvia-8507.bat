@echo off
setlocal enabledelayedexpansion

title JOPAI-LumenVia (Port 8507)
cd /d "C:\Users\jop28\OneDrive\Documents\GitHub\JOPAI-LumenVia"

echo.
echo ==========================================
echo   JOPAI LumenVia — Calendrier liturgique
echo ==========================================
echo.
echo - Source liturgique : AELF (zone: france)
echo - Pages : C'est quoi ^| Dimanche ^| Aide-Memoire ^| Nous rejoindre
echo - Admin test ressources : ajoute ?admin=1 a l'URL
echo - Port : 8507
echo.
echo URL (apres demarrage) :
echo - http://localhost:8507
echo - http://localhost:8507/?admin=1
echo.

echo Lancement de Streamlit...
streamlit run app.py --server.port 8507

echo.
echo Streamlit arrete.
pause

