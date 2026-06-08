"""Tests for the in-memory Job model and JobStore."""
from __future__ import annotations

from app.core.jobs import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PARTIAL,
    Job,
    JobStore,
)
from app.core.models import BulkResult, HospitalInput, RowResult


def _job(n=3):
    rows = [HospitalInput(row=i + 1, name="H{}".format(i + 1), address="A") for i in range(n)]
    return Job("job1", "batch1", rows, created_at="now")


def test_store_get_and_missing():
    store = JobStore()
    job = _job()
    store.add(job)
    assert store.get("job1") is job
    assert store.get("nope") is None


def test_store_evicts_oldest_beyond_capacity():
    store = JobStore(max_jobs=2)
    for i in range(3):
        rows = [HospitalInput(row=1, name="H", address="A")]
        store.add(Job("job{}".format(i), "b", rows, created_at="now"))
    assert store.get("job0") is None  # evicted
    assert store.get("job1") is not None
    assert store.get("job2") is not None
    assert len(store) == 2


def test_snapshot_status_completed():
    job = _job(2)
    result = BulkResult("batch1", 2, 2, 0, 1.0, True, [
        RowResult(1, "H1", hospital_id=1, status="created_and_activated"),
        RowResult(2, "H2", hospital_id=2, status="created_and_activated"),
    ])
    job.mark_finished(result, "later")
    snap = job.snapshot()
    assert snap["status"] == STATUS_COMPLETED
    assert snap["created"] == 2 and snap["failed"] == 0
    assert snap["progress_percent"] == 100.0
    assert snap["result"]["batch_id"] == "batch1"


def test_snapshot_status_partial_and_failed_rows():
    job = _job(2)
    result = BulkResult("batch1", 2, 1, 1, 1.0, False, [
        RowResult(1, "H1", hospital_id=1, status="created"),
        RowResult(2, "H2", hospital_id=None, status="failed", error="boom"),
    ])
    job.mark_finished(result, "later")
    assert job.snapshot()["status"] == STATUS_PARTIAL
    failed = job.failed_rows()
    assert [r.row for r in failed] == [2]
    assert [r.row for r in job.successful_results()] == [1]


def test_snapshot_status_failed_when_none_created():
    job = _job(1)
    result = BulkResult("batch1", 1, 0, 1, 1.0, False, [
        RowResult(1, "H1", hospital_id=None, status="failed"),
    ])
    job.mark_finished(result, "later")
    assert job.snapshot()["status"] == STATUS_FAILED


def test_progress_updates_counts():
    job = _job(2)
    job.on_progress(RowResult(1, "H1", hospital_id=1), 1, 2)
    job.on_progress(RowResult(2, "H2", hospital_id=None), 2, 2)
    snap = job.snapshot()
    assert snap["completed"] == 2
    assert snap["created"] == 1 and snap["failed"] == 1
