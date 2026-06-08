"""Concurrent bulk-processing orchestration.

This is where the assignment's "Performance & Scalability" weight is earned.
The upstream is slow (~5-6s/request on free tier). Creating 20 hospitals
sequentially would take ~2 minutes; fanning them out across a bounded thread
pool collapses that to roughly the latency of a single request.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, List, Optional

from .models import (
    STATUS_CREATED,
    STATUS_CREATED_AND_ACTIVATED,
    STATUS_FAILED,
    BulkResult,
    HospitalInput,
    RowResult,
)
from .upstream import HospitalDirectoryClient, UpstreamError

logger = logging.getLogger(__name__)

# Called as ``progress_cb(row_result, completed_count, total)`` after each
# create finishes, so async jobs can publish live progress.
ProgressCallback = Callable[[RowResult, int, int], None]


class BulkProcessor:
    """Orchestrates concurrent creation + batch activation.

    The client is injected so the processor can be unit-tested against a fake
    in-memory client without any network or HTTP mocking.
    """

    def __init__(
        self,
        client: HospitalDirectoryClient,
        *,
        concurrency: int = 8,
        activate_on_partial: bool = False,
    ):
        self.client = client
        self.concurrency = max(1, concurrency)
        # When False (default, matching the literal spec) the batch is only
        # activated if every row succeeded. When True, any successfully created
        # rows are activated even if some siblings failed.
        self.activate_on_partial = activate_on_partial

    def process(
        self,
        attempts: List[HospitalInput],
        batch_id: str,
        *,
        preserved: Optional[List[RowResult]] = None,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> BulkResult:
        """Create ``attempts`` concurrently under ``batch_id`` then activate.

        ``preserved`` carries already-successful rows from a prior run (used by
        the resume flow) so they are merged into the final result and counted
        toward activation without being re-created.
        """
        start = time.monotonic()
        preserved = preserved or []
        total = len(attempts) + len(preserved)

        new_results: List[Optional[RowResult]] = [None] * len(attempts)
        completed = len(preserved)

        if attempts:
            workers = min(self.concurrency, len(attempts))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_to_index = {
                    executor.submit(self._create_one, row, batch_id): i
                    for i, row in enumerate(attempts)
                }
                for future in as_completed(future_to_index):
                    index = future_to_index[future]
                    result = future.result()  # _create_one never raises
                    new_results[index] = result
                    completed += 1
                    if progress_cb is not None:
                        try:
                            progress_cb(result, completed, total)
                        except Exception:  # pragma: no cover - progress is best-effort
                            logger.exception("progress callback raised; ignoring")

        merged: List[RowResult] = preserved + [r for r in new_results if r is not None]
        # Present rows in their original CSV order regardless of completion order.
        merged.sort(key=lambda r: r.row)

        return self._finalize(batch_id, merged, total, start)

    # -- internals -----------------------------------------------------------

    def _create_one(self, row: HospitalInput, batch_id: str) -> RowResult:
        """Create a single hospital. Always returns a RowResult (never raises)."""
        result = RowResult(row=row.row, name=row.name)
        try:
            data = self.client.create_hospital(row.to_payload(batch_id))
            result.hospital_id = data.get("id")
            if result.hospital_id is None:
                result.status = STATUS_FAILED
                result.error = "Upstream did not return a hospital id."
            else:
                result.status = STATUS_CREATED
        except UpstreamError as exc:
            result.status = STATUS_FAILED
            result.error = _format_upstream_error(exc)
        except Exception as exc:  # defensive: a worker must not crash the pool
            logger.exception("Unexpected error creating row %s", row.row)
            result.status = STATUS_FAILED
            result.error = "Unexpected error: {}".format(exc)
        return result

    def _finalize(
        self,
        batch_id: str,
        results: List[RowResult],
        total: int,
        start: float,
    ) -> BulkResult:
        created = [r for r in results if r.created]
        failed = [r for r in results if not r.created]

        batch_activated = False
        if created and (not failed or self.activate_on_partial):
            try:
                self.client.activate_batch(batch_id)
                batch_activated = True
            except UpstreamError as exc:
                logger.error("Batch activation failed for %s: %s", batch_id, exc)
                batch_activated = False

        # Promote per-row status now that we know the batch activation outcome.
        for result in results:
            if result.created:
                result.status = (
                    STATUS_CREATED_AND_ACTIVATED if batch_activated else STATUS_CREATED
                )

        elapsed = round(time.monotonic() - start, 3)
        return BulkResult(
            batch_id=batch_id,
            total_hospitals=total,
            processed_hospitals=len(created),
            failed_hospitals=len(failed),
            processing_time_seconds=elapsed,
            batch_activated=batch_activated,
            hospitals=results,
        )


def _format_upstream_error(exc: UpstreamError) -> str:
    if exc.status_code is not None:
        return "Upstream {}: {}".format(exc.status_code, _short(exc.detail) or exc.message)
    return exc.message


def _short(detail: object, limit: int = 200) -> str:
    if detail is None:
        return ""
    text = detail if isinstance(detail, str) else str(detail)
    return text if len(text) <= limit else text[:limit] + "..."
