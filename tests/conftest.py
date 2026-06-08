"""Shared test fixtures and an in-memory fake of the upstream client.

The fake mirrors :class:`HospitalDirectoryClient`'s public surface so the
processor and HTTP layer can be tested deterministically without any network
or HTTP mocking — which also sidesteps thread-safety issues that arise from
mocking HTTP at the socket layer under a ThreadPoolExecutor.
"""
from __future__ import annotations

import threading
import time
from typing import Dict, Iterable, Optional

import pytest

from app import create_app
from app.config import Config
from app.core.upstream import UpstreamError


class FakeClient:
    """Deterministic, thread-safe stand-in for the upstream API."""

    def __init__(
        self,
        fail_names: Optional[Iterable[str]] = None,
        fail_once_names: Optional[Iterable[str]] = None,
        fail_activate: bool = False,
        latency: float = 0.0,
    ):
        self._lock = threading.Lock()
        self._next_id = 0
        self.hospitals: Dict[int, dict] = {}
        self.activated_batches = []
        self.fail_names = set(fail_names or [])
        self.fail_once_names = set(fail_once_names or [])
        self._attempts: Dict[str, int] = {}
        self.fail_activate = fail_activate
        self.latency = latency
        self.create_calls = 0

    def create_hospital(self, payload: dict) -> dict:
        if self.latency:
            time.sleep(self.latency)
        name = payload.get("name")
        with self._lock:
            self.create_calls += 1
            self._attempts[name] = self._attempts.get(name, 0) + 1
            attempt = self._attempts[name]
            if name in self.fail_names:
                raise UpstreamError("simulated failure", status_code=500, detail="boom")
            if name in self.fail_once_names and attempt == 1:
                raise UpstreamError("transient failure", status_code=503, detail="retry me")
            self._next_id += 1
            hid = self._next_id
            record = dict(payload, id=hid, active=False)
            self.hospitals[hid] = record
        return dict(record)

    def activate_batch(self, batch_id: str) -> dict:
        if self.fail_activate:
            raise UpstreamError("activation failed", status_code=503)
        count = 0
        with self._lock:
            for record in self.hospitals.values():
                if str(record.get("creation_batch_id")) == str(batch_id):
                    record["active"] = True
                    count += 1
            self.activated_batches.append(batch_id)
        return {"activated_count": count, "message": "Activated {}".format(count)}

    def get_batch(self, batch_id: str):
        with self._lock:
            return [
                dict(r)
                for r in self.hospitals.values()
                if str(r.get("creation_batch_id")) == str(batch_id)
            ]

    def delete_batch(self, batch_id: str) -> dict:
        with self._lock:
            ids = [
                hid
                for hid, r in self.hospitals.items()
                if str(r.get("creation_batch_id")) == str(batch_id)
            ]
            for hid in ids:
                del self.hospitals[hid]
        return {"deleted_count": len(ids), "message": "Deleted {}".format(len(ids))}

    def health(self) -> bool:
        return True

    def close(self) -> None:
        pass


def make_config(**overrides) -> Config:
    defaults = dict(
        upstream_base_url="http://fake-upstream.local",
        max_hospitals=20,
        concurrency=4,
        activate_on_partial=False,
        job_workers=4,
    )
    defaults.update(overrides)
    return Config(**defaults)


@pytest.fixture
def make_app():
    """Factory: build an app with an injected fake client + config overrides."""

    def _make(fake: Optional[FakeClient] = None, **config_overrides):
        fake = fake or FakeClient()
        app = create_app(make_config(**config_overrides), client=fake)
        app.config["TESTING"] = True
        app._fake_client = fake  # convenience handle for assertions
        return app

    return _make


@pytest.fixture
def app(make_app):
    return make_app()


@pytest.fixture
def client(app):
    return app.test_client()


def wait_for_job(client, job_id: str, timeout: float = 5.0) -> dict:
    """Poll a job until it reaches a terminal state (or time out)."""
    deadline = time.monotonic() + timeout
    terminal = {"completed", "partial", "failed"}
    snapshot = {}
    while time.monotonic() < deadline:
        resp = client.get("/hospitals/bulk/{}".format(job_id))
        snapshot = resp.get_json()
        if snapshot and snapshot.get("status") in terminal:
            return snapshot
        time.sleep(0.02)
    raise AssertionError("Job {} did not finish in time: {}".format(job_id, snapshot))
