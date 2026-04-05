FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app

RUN python -m pip install --upgrade pip && \
    pip install .

EXPOSE 8010

HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=6 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/healthz', timeout=3).read()"

CMD ["sh", "-lc", "uvicorn app.main:app --host ${LAP_VISION_F1_HOST:-0.0.0.0} --port ${LAP_VISION_F1_PORT:-8010}"]
