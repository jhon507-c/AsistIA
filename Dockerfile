FROM python:3.10-slim

# System dependencies required by OpenCV and InsightFace
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1-mesa-glx \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY server.py .
COPY kiosk.html .

# Pre-download InsightFace model (cached in image layer)
RUN python3 -c "\
import insightface; \
app = insightface.app.FaceAnalysis(name='buffalo_sc', root='/app/models', providers=['CPUExecutionProvider']); \
app.prepare(ctx_id=0, det_size=(320,320)); \
print('Model ready')"

# Create static dir and copy frontend
RUN mkdir -p /app/static
RUN cp kiosk.html /app/static/kiosk.html

# Data directory (mount as volume for persistence)
RUN mkdir -p /app/data

EXPOSE 8000

# Single worker — SQLite does not support concurrent writes
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
