"""Unit tests for CSV parsing and input validation."""
from __future__ import annotations

import pytest

from app.core.csv_parser import CsvValidationError, parse_csv


def _b(text: str) -> bytes:
    return text.encode("utf-8")


def test_parses_valid_rows():
    raw = _b("name,address,phone\nA Hospital,1 St,555-1\nB Clinic,2 Ave,\n")
    valid, errors, total = parse_csv(raw, max_rows=20)
    assert total == 2
    assert errors == []
    assert [r.name for r in valid] == ["A Hospital", "B Clinic"]
    assert valid[0].row == 1
    assert valid[1].phone is None  # empty phone -> None


def test_phone_is_optional_column():
    raw = _b("name,address\nA Hospital,1 St\n")
    valid, errors, total = parse_csv(raw, max_rows=20)
    assert total == 1 and not errors
    assert valid[0].phone is None


def test_headers_are_normalized_case_and_whitespace():
    raw = _b(" Name , ADDRESS , Phone \nA,1,555\n")
    valid, errors, total = parse_csv(raw, max_rows=20)
    assert valid[0].name == "A" and valid[0].address == "1" and valid[0].phone == "555"


def test_strips_bom():
    raw = "﻿name,address,phone\nA,1,555\n".encode("utf-8")
    valid, errors, total = parse_csv(raw, max_rows=20)
    assert valid[0].name == "A"


def test_missing_required_column_raises():
    raw = _b("name,phone\nA,555\n")
    with pytest.raises(CsvValidationError) as ei:
        parse_csv(raw, max_rows=20)
    assert "address" in str(ei.value)


def test_empty_file_raises():
    with pytest.raises(CsvValidationError):
        parse_csv(b"", max_rows=20)


def test_header_only_raises():
    with pytest.raises(CsvValidationError):
        parse_csv(_b("name,address,phone\n"), max_rows=20)


def test_row_with_empty_name_is_an_error_not_valid():
    raw = _b("name,address,phone\n,1 St,555\nGood,2 St,556\n")
    valid, errors, total = parse_csv(raw, max_rows=20)
    assert total == 2
    assert len(valid) == 1 and valid[0].name == "Good"
    assert len(errors) == 1
    assert errors[0].row == 1
    assert any("name" in e for e in errors[0].errors)


def test_blank_lines_are_skipped():
    raw = _b("name,address,phone\nA,1,555\n\n\nB,2,556\n")
    valid, errors, total = parse_csv(raw, max_rows=20)
    assert total == 2 and len(valid) == 2


def test_exactly_max_rows_allowed():
    rows = "\n".join("H{0},Addr {0},555".format(i) for i in range(20))
    raw = _b("name,address,phone\n" + rows + "\n")
    valid, errors, total = parse_csv(raw, max_rows=20)
    assert total == 20 and len(valid) == 20


def test_exceeding_max_rows_raises():
    rows = "\n".join("H{0},Addr {0},555".format(i) for i in range(21))
    raw = _b("name,address,phone\n" + rows + "\n")
    with pytest.raises(CsvValidationError) as ei:
        parse_csv(raw, max_rows=20)
    assert "maximum" in str(ei.value).lower()


def test_extra_columns_are_ignored():
    raw = _b("name,address,phone,extra\nA,1,555,ignored\n")
    valid, errors, total = parse_csv(raw, max_rows=20)
    assert valid[0].name == "A" and not errors


def test_malformed_oversized_field_raises_validation_error():
    # An unterminated quoted field overflows csv's field-size limit and raises
    # csv.Error; parse_csv must surface it as a CsvValidationError (-> 400),
    # not let it escape as an unhandled 500.
    big = '"' + ("a" * 200000)
    raw = _b("name,address,phone\n" + big + ",1 St,555\n")
    with pytest.raises(CsvValidationError) as ei:
        parse_csv(raw, max_rows=20)
    assert "malformed" in str(ei.value).lower()
