"""Unit tests for the concurrent BulkProcessor using a fake client."""
from __future__ import annotations

from app.core.models import HospitalInput
from app.core.processor import BulkProcessor

from .conftest import FakeClient

BATCH = "11111111-1111-1111-1111-111111111111"


def _rows(n: int):
    return [HospitalInput(row=i + 1, name="H{}".format(i + 1), address="Addr {}".format(i + 1)) for i in range(n)]


def test_all_success_creates_and_activates():
    fake = FakeClient()
    proc = BulkProcessor(fake, concurrency=4)
    result = proc.process(_rows(5), BATCH)

    assert result.total_hospitals == 5
    assert result.processed_hospitals == 5
    assert result.failed_hospitals == 0
    assert result.batch_activated is True
    assert fake.activated_batches == [BATCH]
    assert all(h.status == "created_and_activated" for h in result.hospitals)
    assert all(h.hospital_id is not None for h in result.hospitals)


def test_results_preserve_csv_row_order():
    fake = FakeClient(latency=0.0)
    proc = BulkProcessor(fake, concurrency=8)
    result = proc.process(_rows(10), BATCH)
    assert [h.row for h in result.hospitals] == list(range(1, 11))


def test_partial_failure_does_not_activate_by_default():
    fake = FakeClient(fail_names={"H3"})
    proc = BulkProcessor(fake, concurrency=4, activate_on_partial=False)
    result = proc.process(_rows(5), BATCH)

    assert result.processed_hospitals == 4
    assert result.failed_hospitals == 1
    assert result.batch_activated is False
    assert fake.activated_batches == []
    statuses = {h.row: h.status for h in result.hospitals}
    assert statuses[3] == "failed"
    assert statuses[1] == "created"  # created but NOT activated
    failed = next(h for h in result.hospitals if h.row == 3)
    assert failed.error and "500" in failed.error


def test_partial_failure_activates_when_configured():
    fake = FakeClient(fail_names={"H3"})
    proc = BulkProcessor(fake, concurrency=4, activate_on_partial=True)
    result = proc.process(_rows(5), BATCH)

    assert result.batch_activated is True
    assert fake.activated_batches == [BATCH]
    statuses = {h.row: h.status for h in result.hospitals}
    assert statuses[1] == "created_and_activated"
    assert statuses[3] == "failed"


def test_activation_failure_is_handled():
    fake = FakeClient(fail_activate=True)
    proc = BulkProcessor(fake, concurrency=4)
    result = proc.process(_rows(3), BATCH)

    assert result.processed_hospitals == 3
    assert result.batch_activated is False
    assert all(h.status == "created" for h in result.hospitals)


def test_all_failed_does_not_activate():
    fake = FakeClient(fail_names={"H1", "H2"})
    proc = BulkProcessor(fake, concurrency=4)
    result = proc.process(_rows(2), BATCH)
    assert result.processed_hospitals == 0
    assert result.failed_hospitals == 2
    assert result.batch_activated is False


def test_empty_input():
    fake = FakeClient()
    proc = BulkProcessor(fake, concurrency=4)
    result = proc.process([], BATCH)
    assert result.total_hospitals == 0
    assert result.batch_activated is False


def test_preserved_results_merge_for_resume():
    # Simulate resume: 2 already-created, re-attempt 1 failed row.
    fake = FakeClient()
    proc = BulkProcessor(fake, concurrency=4)
    first = proc.process(_rows(3), BATCH)
    preserved = [h for h in first.hospitals if h.created][:2]

    new_attempt = [HospitalInput(row=3, name="H3-retry", address="Addr 3")]
    merged = proc.process(new_attempt, BATCH, preserved=preserved)

    assert merged.total_hospitals == 3
    assert merged.processed_hospitals == 3
    assert [h.row for h in merged.hospitals] == [1, 2, 3]


def test_progress_callback_invoked_per_row():
    fake = FakeClient()
    proc = BulkProcessor(fake, concurrency=2)
    seen = []
    proc.process(_rows(4), BATCH, progress_cb=lambda r, c, t: seen.append((c, t)))
    assert len(seen) == 4
    assert seen[-1][0] == 4 and seen[-1][1] == 4


def test_creates_run_concurrently():
    # Deterministic (no wall-clock): with latency holding workers open, the
    # observed peak in-flight must reach the configured concurrency.
    fake = FakeClient(latency=0.1)
    proc = BulkProcessor(fake, concurrency=6)
    proc.process(_rows(6), BATCH)
    assert fake.peak_in_flight == 6  # all 6 ran at once, not sequentially


def test_concurrency_bounded_by_pool_size():
    # 8 rows but concurrency=3 -> never more than 3 in flight at once.
    fake = FakeClient(latency=0.05)
    proc = BulkProcessor(fake, concurrency=3)
    proc.process(_rows(8), BATCH)
    assert fake.peak_in_flight == 3


def test_upstream_200_without_id_is_treated_as_failure():
    fake = FakeClient(drop_id=True)
    proc = BulkProcessor(fake, concurrency=2)
    result = proc.process(_rows(2), BATCH)
    assert result.processed_hospitals == 0
    assert result.failed_hospitals == 2
    assert result.batch_activated is False
    assert fake.activated_batches == []
    assert all(h.status == "failed" for h in result.hospitals)
    assert all("did not return a hospital id" in h.error for h in result.hospitals)
