"""Integration tests for the HTTP API using Flask's test client."""
from __future__ import annotations

import io

from .conftest import FakeClient, wait_for_job


def _upload(client, text, path="/hospitals/bulk", **data):
    data = {**data, "file": (io.BytesIO(text.encode("utf-8")), "hospitals.csv")}
    return client.post(path, data=data, content_type="multipart/form-data")


GOOD_CSV = "name,address,phone\nGeneral Hospital,123 Main St,555-1\nSt Mary,456 Oak,555-2\n"


# -- discovery ---------------------------------------------------------------


def test_index(client):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["service"].startswith("Hospital Bulk")
    assert "POST /hospitals/bulk" in body["endpoints"]["bulk_create"]


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


def test_openapi_served(client):
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    assert resp.get_json()["info"]["title"].startswith("Hospital Bulk")


# -- sync bulk create --------------------------------------------------------


def test_bulk_sync_happy_path_matches_contract(client, app):
    resp = _upload(client, GOOD_CSV)
    assert resp.status_code == 200
    body = resp.get_json()

    # Exact response contract keys.
    assert set(body.keys()) == {
        "batch_id",
        "total_hospitals",
        "processed_hospitals",
        "failed_hospitals",
        "processing_time_seconds",
        "batch_activated",
        "hospitals",
    }
    assert body["total_hospitals"] == 2
    assert body["processed_hospitals"] == 2
    assert body["failed_hospitals"] == 0
    assert body["batch_activated"] is True
    assert isinstance(body["processing_time_seconds"], (int, float))

    first = body["hospitals"][0]
    assert set(first.keys()) == {"row", "hospital_id", "name", "status"}
    assert first["row"] == 1
    assert first["status"] == "created_and_activated"
    assert first["hospital_id"] is not None

    # The fake upstream actually got the activate call.
    assert app._fake_client.activated_batches == [body["batch_id"]]


def test_bulk_sync_partial_failure_not_activated(make_app):
    app = make_app(FakeClient(fail_names={"BadOne"}))
    client = app.test_client()
    csv = "name,address,phone\nGoodOne,1 St,555\nBadOne,2 St,556\n"
    resp = _upload(client, csv)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["processed_hospitals"] == 1
    assert body["failed_hospitals"] == 1
    assert body["batch_activated"] is False
    statuses = {h["row"]: h["status"] for h in body["hospitals"]}
    assert statuses[1] == "created"
    assert statuses[2] == "failed"


def test_bulk_rejects_missing_file(client):
    resp = client.post("/hospitals/bulk", data={}, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert resp.get_json()["error"]


def test_bulk_rejects_invalid_rows_with_details(client):
    csv = "name,address,phone\n,1 St,555\nGood,2 St,556\n"
    resp = _upload(client, csv)
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"] == "invalid_csv"
    assert body["details"][0]["row"] == 1


def test_bulk_rejects_missing_column(client):
    csv = "name,phone\nA,555\n"
    resp = _upload(client, csv)
    assert resp.status_code == 400
    assert "address" in resp.get_json()["message"]


def test_bulk_rejects_too_many_rows(client):
    rows = "\n".join("H{0},Addr {0},555".format(i) for i in range(21))
    resp = _upload(client, "name,address,phone\n" + rows + "\n")
    assert resp.status_code == 400
    assert "maximum" in resp.get_json()["message"].lower()


# -- validate ----------------------------------------------------------------


def test_validate_valid_csv(client):
    resp = _upload(client, GOOD_CSV, path="/hospitals/bulk/validate")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["valid"] is True
    assert body["valid_rows"] == 2 and body["invalid_rows"] == 0


def test_validate_reports_invalid_rows_with_200(client):
    csv = "name,address,phone\n,1 St,555\n"
    resp = _upload(client, csv, path="/hospitals/bulk/validate")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["valid"] is False
    assert body["invalid_rows"] == 1


def test_validate_does_not_call_upstream(make_app):
    app = make_app()
    client = app.test_client()
    _upload(client, GOOD_CSV, path="/hospitals/bulk/validate")
    assert app._fake_client.create_calls == 0


# -- async job + polling -----------------------------------------------------


def test_async_job_lifecycle(client):
    resp = _upload(client, GOOD_CSV, mode="async")
    assert resp.status_code == 202
    body = resp.get_json()
    assert body["status"] == "pending"
    job_id = body["job_id"]

    final = wait_for_job(client, job_id)
    assert final["status"] == "completed"
    assert final["created"] == 2
    assert final["result"]["batch_activated"] is True


def test_job_status_404_for_unknown(client):
    resp = client.get("/hospitals/bulk/does-not-exist")
    assert resp.status_code == 404


# -- resume ------------------------------------------------------------------


def test_resume_recovers_failed_rows(make_app):
    # 'Flaky' fails on first attempt, succeeds on the resume retry.
    app = make_app(FakeClient(fail_once_names={"Flaky"}))
    client = app.test_client()
    csv = "name,address,phone\nStable,1 St,555\nFlaky,2 St,556\n"

    start = _upload(client, csv, mode="async")
    job_id = start.get_json()["job_id"]
    first = wait_for_job(client, job_id)
    assert first["status"] == "partial"
    assert first["failed"] == 1

    resumed = client.post("/hospitals/bulk/{}/resume".format(job_id))
    assert resumed.status_code == 200
    body = resumed.get_json()
    assert body["status"] == "completed"
    assert body["failed"] == 0
    assert body["result"]["processed_hospitals"] == 2


def test_resume_404_for_unknown(client):
    resp = client.post("/hospitals/bulk/nope/resume")
    assert resp.status_code == 404


def test_resume_noop_when_nothing_failed(client):
    start = _upload(client, GOOD_CSV, mode="async")
    job_id = start.get_json()["job_id"]
    wait_for_job(client, job_id)
    resp = client.post("/hospitals/bulk/{}/resume".format(job_id))
    assert resp.status_code == 200
    assert "Nothing to resume" in resp.get_json()["message"]
