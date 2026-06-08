"""HTTP client for the upstream Hospital Directory API.

Wraps a pooled :class:`requests.Session` with automatic retry/backoff so the
bulk processor can fan out concurrently against a slow, free-tier upstream
without each worker re-implementing resilience.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


class UpstreamError(Exception):
    """Raised when an upstream call fails (network error or unexpected status)."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        detail: Any = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.detail = detail


class HospitalDirectoryClient:
    """Thin, resilient client over the deployed Hospital Directory API.

    Retries are applied for transient gateway/throttle statuses (429/502/503/
    504) and connection errors. Note the trade-off: because ``POST`` is not
    idempotent, a retried create that already succeeded server-side could in
    principle duplicate. We accept at-least-once semantics here because the
    free-tier upstream's failures are overwhelmingly cold-start/gateway errors
    where the request never reached the application. A generous read timeout
    further avoids spurious read-timeout retries during cold starts.
    """

    def __init__(
        self,
        base_url: str,
        *,
        connect_timeout: float = 10.0,
        read_timeout: float = 60.0,
        max_retries: int = 3,
        backoff_factor: float = 1.0,
        pool_size: int = 10,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = (connect_timeout, read_timeout)

        retry = Retry(
            total=max_retries,
            connect=max_retries,
            read=max_retries,
            status=max_retries,
            backoff_factor=backoff_factor,
            status_forcelist=(429, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST", "PATCH", "DELETE"]),
            raise_on_status=False,
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(
            max_retries=retry,
            pool_connections=pool_size,
            pool_maxsize=pool_size,
        )
        self.session = requests.Session()
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.headers.update(
            {"Content-Type": "application/json", "Accept": "application/json"}
        )

    # -- public API ----------------------------------------------------------

    def create_hospital(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/hospitals/", json=payload, expected=(200, 201))

    def activate_batch(self, batch_id: str) -> Dict[str, Any]:
        return self._request(
            "PATCH", "/hospitals/batch/{}/activate".format(batch_id), expected=(200,)
        )

    def get_batch(self, batch_id: str) -> List[Dict[str, Any]]:
        result = self._request(
            "GET", "/hospitals/batch/{}".format(batch_id), expected=(200,)
        )
        return result if isinstance(result, list) else []

    def delete_batch(self, batch_id: str) -> Dict[str, Any]:
        return self._request(
            "DELETE", "/hospitals/batch/{}".format(batch_id), expected=(200, 204)
        )

    def health(self) -> bool:
        """Lightweight liveness probe of the upstream."""
        try:
            self._request("GET", "/", expected=(200,))
            return True
        except UpstreamError:
            return False

    # -- internals -----------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[Dict[str, Any]] = None,
        expected: tuple = (200,),
    ) -> Any:
        url = self.base_url + path
        try:
            response = self.session.request(
                method, url, json=json, timeout=self.timeout
            )
        except requests.exceptions.RequestException as exc:
            logger.warning("Upstream network error on %s %s: %s", method, path, exc)
            raise UpstreamError(
                "Network error calling upstream: {}".format(exc), detail=str(exc)
            )

        if response.status_code not in expected:
            detail: Any
            try:
                detail = response.json()
            except ValueError:
                detail = (response.text or "")[:500]
            raise UpstreamError(
                "Upstream returned {} for {} {}".format(
                    response.status_code, method, path
                ),
                status_code=response.status_code,
                detail=detail,
            )

        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    def close(self) -> None:
        self.session.close()
