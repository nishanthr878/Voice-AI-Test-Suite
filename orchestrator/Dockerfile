FROM python:3.11-slim

WORKDIR /app

# System dependencies
# libsndfile1 — audio file reading for sounddevice
# ffmpeg — audio format conversion (WAV processing)
# wget — downloading Piper TTS binary inside container
RUN apt-get update && apt-get install -y \
    libsndfile1 \
    ffmpeg \
    wget \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies first (Docker layer caching)
# If requirements.txt doesn't change, this layer is cached
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create directories the app expects
RUN mkdir -p /app/logs /app/test_cases

EXPOSE 8000

CMD ["python", "main.py"]