"""Service container wiring the core components together.

A single :class:`Services` instance is attached to the Flask app and shared
across requests. It owns the long-lived objects: the upstream client (with its
connection pool), the processor, the job store, and the background executor
that runs asynchronous jobs.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

from .config import Config
from .core.jobs import Job, JobStore
from .core.processor import BulkProcessor
from .core.upstream import HospitalDirectoryClient

logger = logging.getLogger(__name__)


def utcnow_iso() -> str:
    """Timezone-aware UTC timestamp (ISO-8601, trailing 'Z')."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Services:
    def __init__(self, config: Config, client: Optional[HospitalDirectoryClient] = None):
        self.config = config
        self.client = client or HospitalDirectoryClient(
            config.upstream_base_url,
            connect_timeout=config.connect_timeout,
            read_timeout=config.read_timeout,
            max_retries=config.max_retries,
            backoff_factor=config.backoff_factor,
            pool_size=config.pool_size,
        )
        self.processor = BulkProcessor(
            self.client,
            concurrency=config.concurrency,
            activate_on_partial=config.activate_on_partial,
        )
        self.job_store = JobStore(max_jobs=config.max_jobs)
        self._executor = ThreadPoolExecutor(
            max_workers=config.job_workers, thread_name_prefix="bulk-job"
        )

    # -- asynchronous job handling ------------------------------------------

    def submit_job(self, job: Job) -> None:
        """Register a job and schedule it to run in the background."""
        self.job_store.add(job)
        self._executor.submit(self._run_job, job)

    def _run_job(self, job: Job) -> None:
        job.mark_started(utcnow_iso())
        try:
            result = self.processor.process(
                job.rows, job.batch_id, progress_cb=job.on_progress
            )
            job.mark_finished(result, utcnow_iso())
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Job %s crashed", job.job_id)
            job.mark_error("Job failed: {}".format(exc), utcnow_iso())

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)
        try:
            self.client.close()
        except Exception:  # pragma: no cover
            pass
