@echo off
chcp 65001 > nul
title Sonora Telegram Auth Bot
color 0A

echo ============================================================
echo           SONORA MUSIC - TELEGRAM AUTH BOT AND GATEWAY
echo ============================================================
echo.

set "SONORA_BOT_TOKEN=9127762870:AAHJXJL9EADvRAeTh8zdblhBbbjLxvBtPgU"
set "SONORA_AUTH_PORT=8088"
set "SONORA_BOT_USERNAME=sonoramus1c_bot"

echo [*] Токен бота: @sonoramus1c_bot
echo [*] Запуск шлюза SonoraAuthBot на порту 8088...
echo.

cd /d "%~dp0"
python -u "bot.py"

echo.
echo [*] Сервис завершил работу.
pause
