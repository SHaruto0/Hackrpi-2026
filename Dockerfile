FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        libglib2.0-0 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-vps.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Optional visual positioning. Default image stays light.
# docker compose build --build-arg VPS=1
ARG VPS=0
RUN if [ "$VPS" = "1" ]; then pip install --no-cache-dir -r requirements-vps.txt; fi

COPY backend ./backend
COPY viewer ./viewer
COPY data/buildings ./data/buildings

ENV PYTHONUNBUFFERED=1 \
    FIRESIGHT_BUILDING=data/buildings/dcc.json

EXPOSE 8000
CMD ["uvicorn", "backend.server:app", "--host", "0.0.0.0", "--port", "8000"]
