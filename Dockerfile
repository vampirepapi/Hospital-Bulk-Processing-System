# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Run as a non-root user.
RUN useradd --create-home appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"

# Single worker: the in-memory job store is not shared across processes.
# Concurrency comes from --threads and the per-job thread pool.
# Generous --timeout absorbs the slow, cold-starting upstream.
CMD ["gunicorn", "--workers", "1", "--threads", "8", "--timeout", "120", \
     "--access-logfile", "-", "--bind", "0.0.0.0:8000", "wsgi:app"]
