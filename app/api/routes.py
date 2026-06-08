"""HTTP endpoints for the Hospital Bulk Processing System."""
from __future__ import annotations

import uuid

from flask import Blueprint, current_app, jsonify, request, send_from_directory
from werkzeug.exceptions import BadRequest, Conflict, NotFound

from ..core.csv_parser import CsvValidationError, parse_csv
from ..core.jobs import STATUS_PENDING, STATUS_PROCESSING, Job
from ..services import Services, utcnow_iso

bp = Blueprint("api", __name__)


def _services() -> Services:
    return current_app.extensions["bulk"]


def _read_upload() -> bytes:
    """Extract the uploaded CSV bytes from a multipart request."""
    file = request.files.get("file")
    if file is None and len(request.files) == 1:
        # Be lenient about the form field name if only one file was sent.
        file = next(iter(request.files.values()))
    if file is None or not file.filename:
        raise BadRequest(
            "No CSV file uploaded. Send multipart/form-data with a 'file' field."
        )
    return file.read()


def _wants_async() -> bool:
    mode = (request.form.get("mode") or request.args.get("mode") or "sync").lower()
    return mode in {"async", "background"}


# -- discovery ---------------------------------------------------------------


@bp.get("/")
def index():
    cfg = _services().config
    return jsonify(
        {
            "service": "Hospital Bulk Processing System",
            "version": "1.0.0",
            "description": (
                "Bulk-creates hospitals from a CSV by fanning out concurrent "
                "calls to the upstream Hospital Directory API, then activates "
                "the batch."
            ),
            "upstream": cfg.upstream_base_url,
            "docs": "/docs",
            "openapi": "/openapi.json",
            "endpoints": {
                "bulk_create": "POST /hospitals/bulk",
                "validate_csv": "POST /hospitals/bulk/validate",
                "job_status": "GET /hospitals/bulk/<job_id>",
                "resume_job": "POST /hospitals/bulk/<job_id>/resume",
                "health": "GET /health",
            },
        }
    )


@bp.get("/health")
def health():
    svc = _services()
    body = {
        "status": "ok",
        "service": "hospital-bulk-processing",
        "active_jobs": len(svc.job_store),
        "config": svc.config.to_public_dict(),
    }
    if request.args.get("check_upstream", "").lower() in {"1", "true", "yes"}:
        body["upstream_reachable"] = svc.client.health()
    return jsonify(body)


@bp.get("/openapi.json")
def openapi_spec():
    return send_from_directory(current_app.static_folder, "openapi.json")


@bp.get("/docs")
def docs():
    return send_from_directory(current_app.static_folder, "docs.html")


# -- bulk processing ---------------------------------------------------------


@bp.post("/hospitals/bulk")
def bulk_create():
    """Bulk-create hospitals from an uploaded CSV.

    Default (``mode=sync``) processes inline and returns the full result.
    ``mode=async`` returns ``202`` with a job id to poll.
    """
    svc = _services()
    raw = _read_upload()
    valid_rows, row_errors, total = parse_csv(raw, svc.config.max_hospitals)

    # Reject malformed input up front; only well-formed rows reach the upstream.
    if row_errors:
        raise CsvValidationError(
            "CSV contains {} invalid row(s). Fix them or call "
            "/hospitals/bulk/validate to inspect.".format(len(row_errors)),
            errors=[e.to_dict() for e in row_errors],
        )

    batch_id = str(uuid.uuid4())

    if _wants_async():
        job = Job(
            job_id=str(uuid.uuid4()),
            batch_id=batch_id,
            rows=valid_rows,
            created_at=utcnow_iso(),
        )
        svc.submit_job(job)
        response = jsonify(
            {
                "job_id": job.job_id,
                "batch_id": batch_id,
                "status": STATUS_PENDING,
                "total_hospitals": job.total,
                "status_url": "/hospitals/bulk/{}".format(job.job_id),
                "message": "Processing started. Poll status_url for progress.",
            }
        )
        response.status_code = 202
        return response

    result = svc.processor.process(valid_rows, batch_id)
    return jsonify(result.to_dict()), 200


@bp.post("/hospitals/bulk/validate")
def validate_csv():
    """Validate a CSV without contacting the upstream (dry run)."""
    svc = _services()
    raw = _read_upload()
    try:
        valid_rows, row_errors, total = parse_csv(raw, svc.config.max_hospitals)
    except CsvValidationError as exc:
        # For the validate endpoint, a structural problem is a *report*, not an
        # error response — return 200 with valid=false so clients can branch.
        return (
            jsonify(
                {
                    "valid": False,
                    "message": exc.message,
                    "total_rows": 0,
                    "valid_rows": 0,
                    "invalid_rows": 0,
                    "errors": exc.errors,
                }
            ),
            200,
        )

    return jsonify(
        {
            "valid": len(row_errors) == 0,
            "total_rows": total,
            "valid_rows": len(valid_rows),
            "invalid_rows": len(row_errors),
            "errors": [e.to_dict() for e in row_errors],
            "preview": [
                {"row": r.row, "name": r.name, "address": r.address, "phone": r.phone}
                for r in valid_rows[:5]
            ],
        }
    )


@bp.get("/hospitals/bulk/<job_id>")
def job_status(job_id: str):
    """Poll the progress/result of an asynchronous bulk job."""
    job = _services().job_store.get(job_id)
    if job is None:
        raise NotFound("No job found with id {}".format(job_id))
    return jsonify(job.snapshot())


@bp.post("/hospitals/bulk/<job_id>/resume")
def resume_job(job_id: str):
    """Resume a job by re-attempting only its failed rows (same batch id)."""
    svc = _services()
    job = svc.job_store.get(job_id)
    if job is None:
        raise NotFound("No job found with id {}".format(job_id))

    if job.status in {STATUS_PENDING, STATUS_PROCESSING}:
        raise Conflict("Job {} is still {}; wait for it to finish.".format(job_id, job.status))

    failed = job.failed_rows()
    if not failed:
        return jsonify(
            {
                "message": "Nothing to resume; no failed rows.",
                **job.snapshot(),
            }
        )

    result = svc.processor.process(
        failed,
        job.batch_id,
        preserved=job.successful_results(),
    )
    job.mark_finished(result, utcnow_iso())
    return jsonify(
        {
            "message": "Resumed: re-attempted {} failed row(s).".format(len(failed)),
            **job.snapshot(),
        }
    )
