"""JSON error handlers so every failure returns a consistent envelope."""
from __future__ import annotations

import logging

from flask import Flask, jsonify
from werkzeug.exceptions import HTTPException

from ..core.csv_parser import CsvValidationError
from ..core.upstream import UpstreamError

logger = logging.getLogger(__name__)


def _envelope(error: str, message: str, status: int, **extra):
    body = {"error": error, "message": message}
    body.update(extra)
    response = jsonify(body)
    response.status_code = status
    return response


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(CsvValidationError)
    def _handle_csv_error(exc: CsvValidationError):
        return _envelope("invalid_csv", exc.message, exc.status, details=exc.errors)

    @app.errorhandler(UpstreamError)
    def _handle_upstream_error(exc: UpstreamError):
        # The upstream is unavailable / misbehaving -> 502 Bad Gateway.
        return _envelope(
            "upstream_error",
            exc.message,
            502,
            upstream_status=exc.status_code,
            detail=exc.detail,
        )

    @app.errorhandler(413)
    def _handle_too_large(exc):
        return _envelope(
            "payload_too_large",
            "Uploaded file exceeds the maximum allowed size.",
            413,
        )

    @app.errorhandler(HTTPException)
    def _handle_http_exception(exc: HTTPException):
        return _envelope(
            exc.name.lower().replace(" ", "_"),
            exc.description or exc.name,
            exc.code or 500,
        )

    @app.errorhandler(Exception)
    def _handle_unexpected(exc: Exception):
        logger.exception("Unhandled exception")
        return _envelope(
            "internal_error",
            "An unexpected error occurred.",
            500,
        )
