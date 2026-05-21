FROM python:3.10-slim

# Runtime libs para OpenCV e InsightFace (sin python3-dev — python:3.10-slim ya trae los headers correctos)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove gcc g++ 2>/dev/null || true

# Copy application files
COPY server.py .

# Static files (frontend + assets)
RUN mkdir -p /app/static
COPY static/kiosk.html  /app/static/kiosk.html
COPY static/sw.js       /app/static/sw.js
COPY static/manifest.json /app/static/manifest.json
COPY static/flash.mp3   /app/static/flash.mp3
COPY assets/            /app/static/

# Pre-download InsightFace model (cached in image layer)
RUN python3 -c "\
import insightface; \
app = insightface.app.FaceAnalysis(name='buffalo_sc', root='/app/models', providers=['CPUExecutionProvider']); \
app.prepare(ctx_id=0, det_size=(320,320)); \
print('Model ready')"

# Data directory (mount as volume for persistence)
RUN mkdir -p /app/data

EXPOSE 8000

# Single worker — SQLite does not support concurrent writes
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
