@echo off
chcp 65001 > nul
title Push Sonora-Telegram-Bot to GitHub
cls
echo ===================================================
echo   PUSHING SONORA-BOT TO GITHUB
echo ===================================================
echo.
cd /d "%~dp0"
git add .
git commit -m "update bot"
git push -u origin main
echo.
pause
