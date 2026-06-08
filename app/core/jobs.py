"""In-memory, thread-safe job tracking for asynchronous bulk operations.

Enables the progress-tracking (polling) and resume bonus features. Because
state lives in process memory, the service MUST run as a single worker
process (see README / render.yaml). Concurrency for the actual work comes from
the per-job thread pool, not from multiple worker processes.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from .models import BulkResult, HospitalInput, RowResult

# Job lifecycle states.
STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_COMPLETED = "completed"  # all rows created
STATUS_PARTIAL = "partial"  # some rows failed
STATUS_FAILED = "failed"  # nothing created / fatal error


class Job:
    """Mutable, lock-guarded record of one async bulk operation."""

    def __init__(self, job_id: str, batch_id: str, rows: List[HospitalInput], created_at: str):
        self.job_id = job_id
        self.batch_id = batch_id
        self.rows = rows
        self.total = len(rows)

        self.status = STATUS_PENDING
        self.completed = 0
        self.created_count = 0
        self.failed_count = 0

        self.result: Optional[BulkResult] = None
        self.error: Optional[str] = None

        self.created_at = created_at
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None

        self._lock = threading.Lock()

    def mark_started(self, when: str) -> None:
        with self._lock:
            self.status = STATUS_PROCESSING
            self.started_at = when

    def on_progress(self, row_result: RowResult, completed: int, total: int) -> None:
        with self._lock:
            self.completed = completed
            if row_result.created:
                self.created_count += 1
            else:
                self.failed_count += 1

    def mark_finished(self, result: BulkResult, when: str) -> None:
        with self._lock:
            self.result = result
            self.completed = result.total_hospitals
            self.created_count = result.processed_hospitals
            self.failed_count = result.failed_hospitals
            self.finished_at = when
            if result.failed_hospitals == 0:
                self.status = STATUS_COMPLETED
            elif result.processed_hospitals == 0:
                self.status = STATUS_FAILED
            else:
                self.status = STATUS_PARTIAL

    def mark_error(self, message: str, when: str) -> None:
        with self._lock:
            self.status = STATUS_FAILED
            self.error = message
            self.finished_at = when

    def failed_rows(self) -> List[HospitalInput]:
        """Inputs whose create did not succeed in the latest result."""
        with self._lock:
            if self.result is None:
                return []
            failed_row_numbers = {
                r.row for r in self.result.hospitals if not r.created
            }
        return [row for row in self.rows if row.row in failed_row_numbers]

    def successful_results(self) -> List[RowResult]:
        with self._lock:
            if self.result is None:
                return []
            return [r for r in self.result.hospitals if r.created]

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            percent = round(100.0 * self.completed / self.total, 1) if self.total else 100.0
            data: Dict[str, Any] = {
                "job_id": self.job_id,
                "batch_id": self.batch_id,
                "status": self.status,
                "total_hospitals": self.total,
                "completed": self.completed,
                "created": self.created_count,
                "failed": self.failed_count,
                "progress_percent": percent,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            }
            if self.result is not None:
                data["result"] = self.result.to_dict()
            if self.error is not None:
                data["error"] = self.error
            return data


class JobStore:
    """Bounded, thread-safe map of job_id -> Job with FIFO eviction."""

    def __init__(self, max_jobs: int = 1000):
        self._jobs: "OrderedDict[str, Job]" = OrderedDict()
        self._lock = threading.Lock()
        self.max_jobs = max_jobs

    def add(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.job_id] = job
            self._jobs.move_to_end(job.job_id)
            while len(self._jobs) > self.max_jobs:
                self._jobs.popitem(last=False)

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def __len__(self) -> int:
        with self._lock:
            return len(self._jobs)
