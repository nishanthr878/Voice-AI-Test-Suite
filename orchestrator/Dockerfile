FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    ffmpeg \
    libsndfile1 \
    libopus-dev \
    libvpx-dev \
    pkg-config \
    build-essential \
    wget \
    espeak \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /app/models /app/logs /app/test_cases

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p /app/models \
    && wget -q -O /app/models/en_US-lessac-medium.onnx \
    https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx \
    && wget -q -O /app/models/en_US-lessac-medium.onnx.json \
    https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json

COPY . .

EXPOSE 5060/udp
EXPOSE 5060/tcp
EXPOSE 8000

ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]