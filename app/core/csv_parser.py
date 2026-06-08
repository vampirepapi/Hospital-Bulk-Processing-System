"""CSV parsing and input validation.

Separates *input validation* (malformed CSV / bad rows -> 400, never sent
upstream) from *processing outcomes* (upstream failures -> counted in the
result). This keeps the contract clean: a 200 from ``/hospitals/bulk`` means
the file was well-formed, regardless of how many upstream calls succeeded.
"""
from __future__ import annotations

import csv
import io
from typing import Dict, List, Optional, Tuple

from .models import HospitalInput, RowError

REQUIRED_COLUMNS = ("name", "address")
OPTIONAL_COLUMNS = ("phone",)


class CsvValidationError(Exception):
    """Raised when the uploaded file cannot be processed at all (HTTP 400)."""

    def __init__(self, message: str, errors: Optional[List[dict]] = None, status: int = 400):
        super().__init__(message)
        self.message = message
        self.errors = errors or []
        self.status = status


def _decode(raw: bytes) -> str:
    # utf-8-sig transparently strips a BOM if present (common from Excel).
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    # latin-1 maps all 256 byte values, so it always decodes — the catch-all.
    return raw.decode("latin-1")


def parse_csv(raw: bytes, max_rows: int) -> Tuple[List[HospitalInput], List[RowError], int]:
    """Parse and validate CSV bytes.

    Returns ``(valid_rows, row_errors, total_data_rows)``.

    Raises :class:`CsvValidationError` for file-level problems (empty file,
    missing required columns, exceeding the row cap) that make the whole
    upload unprocessable.
    """
    if not raw or not raw.strip():
        raise CsvValidationError("CSV file is empty.")

    text = _decode(raw)
    reader = csv.DictReader(io.StringIO(text))
    try:
        # Force the full parse here so any csv.Error (e.g. an oversized or
        # unterminated quoted field) surfaces as a clean 400, not a 500.
        fieldnames = reader.fieldnames
        records = list(reader)
    except csv.Error as exc:
        raise CsvValidationError("CSV file is malformed: {}".format(exc))

    if not fieldnames:
        raise CsvValidationError("CSV file has no header row.")

    # Normalize headers: trim + lowercase so "Name", " phone " etc. all work.
    normalized = [(field or "").strip().lower() for field in fieldnames]
    header_map: Dict[str, str] = {}
    for norm, original in zip(normalized, fieldnames):
        # First occurrence wins for duplicate headers.
        header_map.setdefault(norm, original)

    missing = [col for col in REQUIRED_COLUMNS if col not in header_map]
    if missing:
        raise CsvValidationError(
            "CSV is missing required column(s): {}. "
            "Expected header: name,address,phone (phone is optional).".format(
                ", ".join(missing)
            )
        )

    valid_rows: List[HospitalInput] = []
    row_errors: List[RowError] = []
    data_row = 0

    def cell(record: dict, column: str) -> Optional[str]:
        original = header_map.get(column)
        value = record.get(original) if original is not None else None
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return None

    for record in records:
        # Skip fully blank lines so trailing newlines don't count as a row.
        if all((v is None or str(v).strip() == "") for v in record.values()):
            continue

        data_row += 1
        if data_row > max_rows:
            raise CsvValidationError(
                "CSV exceeds the maximum of {} hospitals per upload.".format(max_rows)
            )

        name = cell(record, "name")
        address = cell(record, "address")
        phone = cell(record, "phone")

        problems: List[str] = []
        if not name:
            problems.append("name is required and cannot be empty")
        if not address:
            problems.append("address is required and cannot be empty")

        if problems:
            row_errors.append(
                RowError(
                    row=data_row,
                    errors=problems,
                    data={"name": name, "address": address, "phone": phone},
                )
            )
        else:
            valid_rows.append(
                HospitalInput(row=data_row, name=name, address=address, phone=phone)
            )

    if data_row == 0:
        raise CsvValidationError("CSV file contains a header but no data rows.")

    return valid_rows, row_errors, data_row
