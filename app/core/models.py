"""Domain models for the bulk-processing pipeline.

These are plain dataclasses with no framework or I/O dependencies, which keeps
the core logic trivially unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Per-row processing status values used in the response `hospitals[]` array.
STATUS_CREATED_AND_ACTIVATED = "created_and_activated"
STATUS_CREATED = "created"
STATUS_FAILED = "failed"


@dataclass
class HospitalInput:
    """A single validated CSV row to be sent to the upstream API."""

    row: int  # 1-based data row number (excludes the header)
    name: str
    address: str
    phone: Optional[str] = None

    def to_payload(self, batch_id: str) -> Dict[str, Any]:
        """Build the JSON body for ``POST /hospitals/``.

        ``creation_batch_id`` is what makes the upstream create the record as
        inactive so the whole batch can be activated atomically afterward.
        """
        payload: Dict[str, Any] = {
            "name": self.name,
            "address": self.address,
            "creation_batch_id": batch_id,
        }
        if self.phone:
            payload["phone"] = self.phone
        return payload


@dataclass
class RowResult:
    """Outcome of attempting to create one hospital."""

    row: int
    name: str
    hospital_id: Optional[int] = None
    status: str = STATUS_FAILED
    error: Optional[str] = None

    @property
    def created(self) -> bool:
        return self.hospital_id is not None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "row": self.row,
            "hospital_id": self.hospital_id,
            "name": self.name,
            "status": self.status,
        }
        if self.error:
            out["error"] = self.error
        return out


@dataclass
class RowError:
    """A CSV row that failed input validation (never sent upstream)."""

    row: int
    errors: List[str]
    data: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {"row": self.row, "errors": self.errors, "data": self.data}


@dataclass
class BulkResult:
    """Comprehensive result of a bulk-create operation.

    Serializes to exactly the response contract specified in the assignment.
    """

    batch_id: str
    total_hospitals: int
    processed_hospitals: int
    failed_hospitals: int
    processing_time_seconds: float
    batch_activated: bool
    hospitals: List[RowResult] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "total_hospitals": self.total_hospitals,
            "processed_hospitals": self.processed_hospitals,
            "failed_hospitals": self.failed_hospitals,
            "processing_time_seconds": self.processing_time_seconds,
            "batch_activated": self.batch_activated,
            "hospitals": [h.to_dict() for h in self.hospitals],
        }
