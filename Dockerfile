FROM python:3.10-slim

# Install ffmpeg + Node.js 22 (required by yt-dlp as JS runtime)
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates ffmpeg && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /tmp/downloads /tmp/yt_dlp_cache

EXPOSE 8000

CMD ["gunicorn", "--worker-class", "eventlet", "-w", "1", "--bind", "0.0.0.0:8000", "--timeout", "120", "app:app"]
