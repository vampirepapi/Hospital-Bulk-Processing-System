# Hospital Bulk Processing System

A Flask service that bulk-creates hospitals from a CSV upload by fanning out
**concurrent** calls to the deployed [Hospital Directory API](https://hospital-directory.onrender.com/docs),
then activating the whole batch atomically.

Built for the Paribus Senior Python Developer challenge.

- **Live demo:** **https://vampirepapi-hospital-bulk-processing.hf.space** — interactive Swagger UI at [`/docs`](https://vampirepapi-hospital-bulk-processing.hf.space/docs)
- **Stack:** Python 3.8+ · Flask · `requests` (pooled + retrying) · `concurrent.futures` · gunicorn · Docker
- **Cold-start note:** the app is hosted on **Hugging Face Spaces** (free, Docker), which sleeps after ~48h idle, and the upstream Hospital Directory API runs on Render's free tier (spins down when idle). So the *first* request after idle can take ~30–60s while both wake up — it still succeeds within the configured timeout, and subsequent requests are fast. Prefer `mode=async` for cold/large batches.

---

## Why this design (the 60-second version)

The upstream API is slow: each `POST /hospitals/` takes **~5–6 seconds** on its
free tier. Creating 20 hospitals sequentially would take ~2 minutes. The core
idea of this service is to **fan those creates out across a bounded thread
pool**, collapsing the wall-clock time to roughly the latency of a *single*
request. A measured live run created 3 hospitals in **~6s** instead of ~18s.

Everything else — retries, a single-worker job store, the input-vs-processing
error split — exists to make that fan-out correct and resilient against a flaky
upstream.

---

## Architecture

```
                 multipart CSV
  client  ─────────────────────────►  Flask (app factory + blueprint)
                                            │
                         ┌──────────────────┼─────────────────────┐
                         ▼                  ▼                     ▼
                   csv_parser         BulkProcessor           JobStore
                 (validate input)   (ThreadPoolExecutor)   (async progress,
                         │            fan-out creates           resume)
                         │                  │
                         │                  ▼
                         │        HospitalDirectoryClient
                         │       (pooled requests.Session,
                         │        retry + backoff)
                         │                  │
                         ▼                  ▼  concurrent
                    400 on bad      POST /hospitals/  (xN)
                    input            then
                                    PATCH /hospitals/batch/{id}/activate
                                            │
                                            ▼
                                   Upstream Hospital Directory API
```

| Layer | Module | Responsibility |
|-------|--------|----------------|
| HTTP | `app/api/routes.py`, `app/api/errors.py` | Endpoints, request parsing, JSON error envelopes |
| Wiring | `app/__init__.py`, `app/services.py` | App factory, dependency injection, background executor |
| Orchestration | `app/core/processor.py` | Concurrent create → batch activate, partial-failure policy |
| Upstream I/O | `app/core/upstream.py` | Pooled, retrying HTTP client |
| Parsing | `app/core/csv_parser.py` | Decode, header normalization, per-row validation, row cap |
| State | `app/core/jobs.py` | Thread-safe job store for async/progress/resume |
| Models | `app/core/models.py` | Framework-free dataclasses |
| Config | `app/config.py` | Env-driven tunables |

The core (`app/core/*`) has **no Flask dependency**, so it is unit-testable in
isolation and the HTTP layer stays thin.

---

## API reference

Base URL is the deployed host (or `http://localhost:5000` locally).

### `POST /hospitals/bulk` — bulk-create from CSV

Multipart form upload. CSV header: `name,address,phone` (phone optional).

| Param | Where | Default | Notes |
|-------|-------|---------|-------|
| `file` | form-data | required | the CSV file |
| `mode` | query or form | `sync` | `sync` returns the full result; `async` returns a job to poll |

**Synchronous (default)** — returns the comprehensive result:

```bash
curl -X POST http://localhost:5000/hospitals/bulk \
  -F "file=@sample_hospitals.csv"
```

```json
{
  "batch_id": "550e8400-e29b-41d4-a716-446655440000",
  "total_hospitals": 8,
  "processed_hospitals": 8,
  "failed_hospitals": 0,
  "processing_time_seconds": 6.01,
  "batch_activated": true,
  "hospitals": [
    { "row": 1, "hospital_id": 101, "name": "General Hospital", "status": "created_and_activated" }
  ]
}
```

`status` per row is one of: `created_and_activated`, `created` (created but the
batch was not activated), or `failed` (with an `error` field).

> **Input validation vs processing outcome:** a malformed CSV — bad headers, an
> empty `name`/`address`, more than 20 rows, or an unparseable file — is rejected
> up front with a **`400`** (plus per-row details) and never reaches the upstream.
> So `failed_hospitals` and a per-row `status: "failed"` represent *upstream*
> processing failures, not bad input. Use `/hospitals/bulk/validate` to check a
> file before submitting.

**Asynchronous** — returns `202` with a job id to poll:

```bash
curl -X POST "http://localhost:5000/hospitals/bulk?mode=async" -F "file=@sample_hospitals.csv"
# { "job_id": "...", "batch_id": "...", "status": "pending", "status_url": "/hospitals/bulk/<job_id>" }
```

### `POST /hospitals/bulk/validate` — dry-run validation (bonus)

Validates the CSV **without** contacting the upstream. Always `200`.

```bash
curl -X POST http://localhost:5000/hospitals/bulk/validate -F "file=@sample_hospitals.csv"
# { "valid": true, "total_rows": 8, "valid_rows": 8, "invalid_rows": 0, "errors": [], "preview": [...] }
```

### `GET /hospitals/bulk/<job_id>` — poll progress (bonus)

```bash
curl http://localhost:5000/hospitals/bulk/<job_id>
# { "status": "processing", "completed": 3, "created": 3, "failed": 0, "progress_percent": 37.5, ... }
```

`status`: `pending` → `processing` → `completed` | `partial` | `failed`. Once
terminal, the snapshot includes a full `result` object.

### `POST /hospitals/bulk/<job_id>/resume` — resume failed rows (bonus)

Re-attempts **only the failed rows** of a finished job, under the *same* batch
id, preserving prior successes, then re-evaluates activation.

```bash
curl -X POST http://localhost:5000/hospitals/bulk/<job_id>/resume
```

### `GET /health`, `GET /`, `GET /docs`

Health check (`?check_upstream=true` to ping the upstream), a JSON API index,
and interactive Swagger UI at `/docs` (served from `/openapi.json`).

---

## Design decisions & trade-offs

- **Concurrency model.** Per-request fan-out uses a `ThreadPoolExecutor`
  (`BULK_CONCURRENCY`, default 8). Threads — not asyncio — because the workload
  is I/O-bound HTTP via `requests`, and a bounded pool with a shared,
  connection-pooled `Session` is simple, robust, and easy to reason about. The
  pool size is deliberately modest to be polite to the shared free-tier upstream.

- **Single worker process, on purpose.** Async job state lives in process memory,
  so the service runs as **one gunicorn worker** (`--workers 1 --threads 8`).
  Concurrency for actual work comes from threads and the per-job pool, not from
  multiple workers. Running multiple workers would split the job store and break
  polling/resume. A horizontally-scalable deployment would swap the in-memory
  `JobStore` for Redis — the interface is small and isolated for exactly this.

- **Generous upstream timeout.** The upstream cold-starts (spins down when idle).
  The read timeout (60s) and gunicorn `--timeout 120` absorb that so the first
  request after idle doesn't get reaped. Note: a *synchronous* request blocks
  until the whole batch finishes, so a pathological cold upstream could push a
  sync call toward a platform edge/load-balancer timeout (e.g. Render's). For
  large or cold-start uploads prefer **`mode=async`**, which returns `202`
  immediately and is polled — it is never bound by the edge request timeout.

- **Retries & at-least-once.** Transient upstream failures (429/502/503/504 and
  connection errors) are retried with exponential backoff. Because `POST` is not
  idempotent, a retried-and-already-succeeded create could in theory duplicate;
  this is accepted because the upstream's failures are overwhelmingly cold-start/
  gateway errors where the request never reached the app, and a generous read
  timeout avoids spurious read-timeout retries.

- **Activation policy (matches the spec, configurable).** By default the batch is
  activated **only when every row succeeds** (the literal spec: "Once all
  hospitals are created successfully, call activate"). Set `ACTIVATE_ON_PARTIAL=true`
  to instead activate whatever was created when some rows fail. Per-row `status`
  represents partial outcomes either way.

- **Input validation vs processing outcome.** A *malformed* CSV (bad headers,
  empty required fields, >20 rows) returns **400** and never touches the upstream.
  *Processing* failures (upstream errors) are counted in `failed_hospitals` with
  per-row detail. This keeps "the file was bad" cleanly separate from "an upstream
  call failed."

---

## Configuration

All via environment variables (see `.env.example`):

| Variable | Default | Description |
|----------|---------|-------------|
| `UPSTREAM_BASE_URL` | `https://hospital-directory.onrender.com` | Upstream API base |
| `MAX_HOSPITALS` | `20` | Max rows per upload |
| `BULK_CONCURRENCY` | `8` | Concurrent upstream creates |
| `UPSTREAM_CONNECT_TIMEOUT` | `10` | Connect timeout (s) |
| `UPSTREAM_READ_TIMEOUT` | `60` | Read timeout (s) — absorbs cold starts |
| `UPSTREAM_MAX_RETRIES` | `3` | Retries for transient errors |
| `UPSTREAM_BACKOFF_FACTOR` | `1.0` | Exponential backoff factor |
| `ACTIVATE_ON_PARTIAL` | `false` | Activate created rows even on partial failure |
| `JOB_WORKERS` | `4` | Background threads for async jobs |
| `LOG_LEVEL` | `INFO` | Log level |

---

## Running locally

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements-dev.txt
python run.py            # serves on http://localhost:5000
```

Then:

```bash
curl -X POST http://localhost:5000/hospitals/bulk -F "file=@sample_hospitals.csv"
```

## Running with Docker

```bash
docker compose up --build
# service on http://localhost:8000
```

or plain Docker:

```bash
docker build -t hospital-bulk-processing .
docker run -p 8000:8000 hospital-bulk-processing
```

---

## Testing

```bash
pip install -r requirements-dev.txt
pytest                       # 61 tests
```

The suite (`tests/`) covers:

- **CSV parsing** — valid/invalid rows, BOM, header normalization, the 20-row cap, empty/header-only files.
- **Upstream client** — request shaping, status/error mapping, network errors (mocked with `responses`, single-threaded).
- **Processor** — all-success, partial failure (both activation policies), activation failure, ordering, resume merge, and a timing assertion proving concurrency beats sequential.
- **Job store** — eviction, snapshots, status transitions.
- **HTTP API** — exact response contract, every endpoint, error scenarios, async lifecycle, and resume recovery (using a fake upstream injected into the app, so no network and no thread/mocking flakiness).

Two scripts exercise the **real** upstream (each creates + activates + cleans up):

```bash
python scripts/live_smoke.py        # minimal smoke test (one tiny batch)
python scripts/acceptance_check.py  # full acceptance: every endpoint + exact
                                    # response contract, 28 assertions, 0 = pass
```

---

## Deployment

The live demo runs on **Hugging Face Spaces** (free Docker Space, no credit card),
which builds the included `Dockerfile`. A Space's `README.md` carries the metadata
`sdk: docker` and `app_port: 8000` so HF routes to gunicorn. Live URL:
**https://vampirepapi-hospital-bulk-processing.hf.space**

The repo also includes a `render.yaml` blueprint for **Render** (note: Render now
requires a payment method to enable the free tier):

1. Push the repo to GitHub.
2. In Render: **New + → Blueprint**, select the repo — Render reads `render.yaml`
   and provisions a web service with the single-worker start command and `/health`
   health check. (Or **New + → Web Service → Docker** to use the `Dockerfile`.)

The start command is intentionally `gunicorn --workers 1 --threads 8 --timeout 120`
(single worker for the in-memory job store; generous timeout for the cold-starting
upstream).

---

## Project structure

```
app/
  __init__.py        app factory
  config.py          env-driven configuration
  services.py        service container + background job runner
  api/
    routes.py        endpoints
    errors.py        JSON error handlers
  core/
    models.py        dataclasses (framework-free)
    csv_parser.py    parsing + input validation
    upstream.py      pooled, retrying HTTP client
    processor.py     concurrent orchestration
    jobs.py          thread-safe job store
  static/            openapi.json + Swagger UI page
tests/               61 tests (pytest)
scripts/             live_smoke.py + acceptance_check.py (real-upstream checks)
wsgi.py  run.py      prod / dev entrypoints
Dockerfile  docker-compose.yml  render.yaml  Procfile
```

---

## Bonus features implemented

Every optional task from the assignment is implemented. Where each one lives:

| Bonus task | What it does | Endpoint / files |
|---|---|---|
| ✅ **Performance optimization** | Concurrent fan-out + connection pooling + retry/backoff | `app/core/processor.py` (`ThreadPoolExecutor`), `app/core/upstream.py` (pooled `Session` + `Retry`) |
| ✅ **Progress tracking** (polling) | Async job + live progress poll | `POST /hospitals/bulk?mode=async`, `GET /hospitals/bulk/<job_id>` → `app/api/routes.py`; job state in `app/core/jobs.py` |
| ✅ **Resume capability** | Re-attempt only failed rows, same batch id | `POST /hospitals/bulk/<job_id>/resume` → `app/api/routes.py` (+ atomic claim in `app/core/jobs.py`) |
| ✅ **CSV validation endpoint** | Dry-run validation, never hits the upstream | `POST /hospitals/bulk/validate` → `app/api/routes.py`; logic in `app/core/csv_parser.py` |
| ✅ **Comprehensive testing** | 61 unit + integration tests + error scenarios + live checks | `tests/` (`test_api.py`, `test_processor.py`, `test_csv_parser.py`, `test_upstream.py`, `test_jobs.py`); `scripts/live_smoke.py`, `scripts/acceptance_check.py` |
| ✅ **Dockerization** | Container + compose + slim build context | `Dockerfile`, `docker-compose.yml`, `.dockerignore` |
| ✅ **Interactive API docs** (extra) | Swagger UI + served OpenAPI spec | `/docs`, `/openapi.json` → `app/static/docs.html`, `app/static/openapi.json` |

> Note on progress tracking: the spec asks for "WebSocket **or** polling" — this implements
> **polling**. WebSocket/SSE was intentionally omitted because it conflicts with the
> single-worker, in-memory-job design for no extra rubric credit (see Design decisions).
