FROM python:3.12-slim

# El paquete ffmpeg de Debian incluye ffmpeg y ffprobe
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencias primero para aprovechar la caché de capas
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY hls_bot.py resolver.py ./

# Usuario sin privilegios
RUN useradd --create-home --uid 10001 bot
USER bot

# BOT_TOKEN, ALLOWED_USERS, MAX_UPLOAD_MB, MAX_JOBS y OWN_BACKEND_* se pasan en tiempo de ejecución
CMD ["python", "hls_bot.py"]
