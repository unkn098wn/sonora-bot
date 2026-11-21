#!/usr/bin/env bash
# ==============================================================================
# Sonora Telegram Bot & API Gateway — 1-Click Ubuntu / Debian Server Setup
# ==============================================================================
set -e

echo "🚀 Установка и запуск Sonora Telegram Bot..."

# 1. Update and install Docker if not present
if ! command -v docker &> /dev/null; then
    echo "📦 Установка Docker и Docker Compose..."
    curl -fsSL https://get.docker.com -o get-docker.sh
    sh get-docker.sh
    rm get-docker.sh
fi

# 2. Build and run via Docker Compose
echo "🔨 Сборка Docker контейнера Sonora Bot..."
docker compose down || true
docker compose build
docker compose up -d

echo "✅ Бот успешно запущен в фоновом режиме!"
echo "📊 Проверить статус: docker compose logs -f"
