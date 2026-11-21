# ==============================================================================
# Sonora Music — Telegram Auth, Registration & API Gateway
# Production Dockerfile for 24/7 Cloud Deployment (Koyeb, Render, VPS, Fly.io)
# ==============================================================================

FROM python:3.11-slim

# Prevent Python from writing .pyc and buffer output for live logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SONORA_AUTH_PORT=8088

WORKDIR /app

# Install curl for container HEALTHCHECK
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot application files
COPY . .

# Ensure storage directories exist
RUN mkdir -p /app/data /app/avatars /app/downloads

# Persistent volumes for users database and cached avatars
VOLUME ["/app/data", "/app/avatars"]

# Default exposed port for HTTP API
EXPOSE 8088

# Healthcheck monitoring
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://127.0.0.1:${PORT:-8088}/health || exit 1

# Start Sonora Auth Bot & API Gateway
CMD ["python", "bot.py"]
