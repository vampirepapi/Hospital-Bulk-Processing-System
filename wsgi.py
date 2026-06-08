"""WSGI entrypoint for production servers (gunicorn, etc.).

Usage:
    gunicorn --workers 1 --threads 8 --timeout 120 wsgi:app

IMPORTANT: run with a SINGLE worker process. Job state (for async/progress/
resume) is held in process memory, so multiple workers would not share jobs.
Request-level concurrency comes from --threads and from the per-job thread
pool, not from multiple workers.
"""
from app import create_app

app = create_app()
