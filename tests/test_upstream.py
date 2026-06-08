"""Tests for the upstream HTTP client (single-threaded, via `responses`)."""
from __future__ import annotations

import json

import pytest
import requests
import responses

from app.core.upstream import HospitalDirectoryClient, UpstreamError

BASE = "http://upstream.local"


def _client() -> HospitalDirectoryClient:
    return HospitalDirectoryClient(BASE, max_retries=0, connect_timeout=1, read_timeout=2)


@responses.activate
def test_create_hospital_success_sends_payload():
    responses.add(
        responses.POST,
        BASE + "/hospitals/",
        json={"id": 7, "name": "X", "active": False},
        status=200,
    )
    out = _client().create_hospital(
        {"name": "X", "address": "1 St", "creation_batch_id": "b1"}
    )
    assert out["id"] == 7
    sent = json.loads(responses.calls[0].request.body)
    assert sent["name"] == "X"
    assert sent["creation_batch_id"] == "b1"


@responses.activate
def test_create_hospital_422_raises_with_detail():
    responses.add(
        responses.POST,
        BASE + "/hospitals/",
        json={"detail": [{"msg": "too short"}]},
        status=422,
    )
    with pytest.raises(UpstreamError) as ei:
        _client().create_hospital({"name": ""})
    assert ei.value.status_code == 422
    assert ei.value.detail == {"detail": [{"msg": "too short"}]}


@responses.activate
def test_activate_batch():
    responses.add(
        responses.PATCH,
        BASE + "/hospitals/batch/abc/activate",
        json={"activated_count": 3, "message": "ok"},
        status=200,
    )
    out = _client().activate_batch("abc")
    assert out["activated_count"] == 3


@responses.activate
def test_get_batch_returns_list():
    responses.add(
        responses.GET,
        BASE + "/hospitals/batch/abc",
        json=[{"id": 1}, {"id": 2}],
        status=200,
    )
    assert _client().get_batch("abc") == [{"id": 1}, {"id": 2}]


@responses.activate
def test_delete_batch_204_returns_empty():
    responses.add(responses.DELETE, BASE + "/hospitals/batch/abc", status=204)
    assert _client().delete_batch("abc") == {}


@responses.activate
def test_network_error_is_wrapped():
    responses.add(
        responses.POST,
        BASE + "/hospitals/",
        body=requests.exceptions.ConnectionError("boom"),
    )
    with pytest.raises(UpstreamError):
        _client().create_hospital({"name": "X", "address": "1"})


@responses.activate
def test_health_false_on_error():
    responses.add(responses.GET, BASE + "/", status=503)
    assert _client().health() is False


@responses.activate
def test_health_true_on_200():
    responses.add(responses.GET, BASE + "/", json={"status": "ok"}, status=200)
    assert _client().health() is True
