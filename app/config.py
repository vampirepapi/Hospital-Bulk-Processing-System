"""Application configuration, sourced from environment variables.

All tunables live here so the service can be reconfigured for different
upstream targets, concurrency levels, and timeouts without code changes.
"""
from __future__ import annotations

import os


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _get_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _get_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Config:
    """Runtime configuration container.

    Construct from the process environment via :meth:`from_env`, or build
    directly with overrides in tests.
    """

    def __init__(
        self,
        *,
        upstream_base_url: str = "https://hospital-directory.onrender.com",
        max_hospitals: int = 20,
        concurrency: int = 8,
        connect_timeout: float = 10.0,
        read_timeout: float = 60.0,
        max_retries: int = 3,
        backoff_factor: float = 1.0,
        activate_on_partial: bool = False,
        job_workers: int = 4,
        max_jobs: int = 1000,
        max_content_length: int = 2 * 1024 * 1024,  # 2 MB upload cap
        log_level: str = "INFO",
    ) -> None:
        self.upstream_base_url = upstream_base_url.rstrip("/")
        self.max_hospitals = max_hospitals
        self.concurrency = max(1, concurrency)
        self.job_workers = max(1, job_workers)
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        # Size the shared connection pool for the worst-case concurrent fan-out
        # across all in-flight async jobs (job_workers x concurrency), not just a
        # single request, so we don't churn keep-alive connections under load.
        self.pool_size = max(self.concurrency * self.job_workers, 10)
        self.activate_on_partial = activate_on_partial
        self.max_jobs = max_jobs
        self.max_content_length = max_content_length
        self.log_level = log_level

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            upstream_base_url=os.environ.get(
                "UPSTREAM_BASE_URL", "https://hospital-directory.onrender.com"
            ),
            max_hospitals=_get_int("MAX_HOSPITALS", 20),
            concurrency=_get_int("BULK_CONCURRENCY", 8),
            connect_timeout=_get_float("UPSTREAM_CONNECT_TIMEOUT", 10.0),
            read_timeout=_get_float("UPSTREAM_READ_TIMEOUT", 60.0),
            max_retries=_get_int("UPSTREAM_MAX_RETRIES", 3),
            backoff_factor=_get_float("UPSTREAM_BACKOFF_FACTOR", 1.0),
            activate_on_partial=_get_bool("ACTIVATE_ON_PARTIAL", False),
            job_workers=_get_int("JOB_WORKERS", 4),
            max_jobs=_get_int("MAX_JOBS", 1000),
            max_content_length=_get_int("MAX_CONTENT_LENGTH", 2 * 1024 * 1024),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )

    def to_public_dict(self) -> dict:
        """Non-sensitive config view, surfaced on the health endpoint."""
        return {
            "upstream_base_url": self.upstream_base_url,
            "max_hospitals": self.max_hospitals,
            "concurrency": self.concurrency,
            "activate_on_partial": self.activate_on_partial,
            "upstream_connect_timeout": self.connect_timeout,
            "upstream_read_timeout": self.read_timeout,
            "upstream_max_retries": self.max_retries,
        }
