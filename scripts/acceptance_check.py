"""End-to-end acceptance check against the REAL upstream.

Exercises every endpoint and asserts the assignment's functional contract,
cleaning up any batches it creates. Run manually:

    python scripts/acceptance_check.py

Exit code 0 = all checks passed.
"""
from __future__ import annotations

import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402

RESULTS = []
CREATED_BATCHES = []


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition), detail))
    mark = "PASS" if condition else "FAIL"
    print("[{}] {}{}".format(mark, name, (" -- " + detail) if detail else ""))


def upload(client, text, path="/hospitals/bulk", **data):
    data = {**data, "file": (io.BytesIO(text.encode("utf-8")), "h.csv")}
    return client.post(path, data=data, content_type="multipart/form-data")


def main() -> int:
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    svc = app.extensions["bulk"]

    good = (
        "name,address,phone\n"
        "Acceptance Alpha,1 Way,555-0001\n"
        "Acceptance Bravo,2 Way,\n"
        "Acceptance Charlie,3 Way,555-0003\n"
    )

    try:
        # --- discovery ----------------------------------------------------
        r = client.get("/")
        check("GET / index", r.status_code == 200 and "endpoints" in r.get_json())

        r = client.get("/health")
        check("GET /health", r.status_code == 200 and r.get_json()["status"] == "ok")

        r = client.get("/health?check_upstream=true")
        body = r.get_json()
        check(
            "GET /health?check_upstream=true reaches upstream",
            r.status_code == 200 and body.get("upstream_reachable") is True,
            "upstream_reachable={}".format(body.get("upstream_reachable")),
        )

        r = client.get("/openapi.json")
        check("GET /openapi.json", r.status_code == 200 and "openapi" in r.get_json())

        r = client.get("/docs")
        check("GET /docs (Swagger UI)", r.status_code == 200)

        # --- CSV validation endpoint -------------------------------------
        r = upload(client, good, path="/hospitals/bulk/validate")
        b = r.get_json()
        check(
            "validate: good CSV -> valid",
            r.status_code == 200 and b["valid"] and b["valid_rows"] == 3,
        )

        malformed = "name,address,phone\n" + '"' + ("a" * 200000) + ",1 St,555\n"
        r = upload(client, malformed, path="/hospitals/bulk/validate")
        check(
            "validate: malformed CSV -> 200 valid:false",
            r.status_code == 200 and r.get_json()["valid"] is False,
        )

        # --- input rejection ---------------------------------------------
        r = client.post("/hospitals/bulk", data={}, content_type="multipart/form-data")
        check("bulk: missing file -> 400", r.status_code == 400)

        r = upload(client, "name,phone\nA,5\n")
        check("bulk: missing required column -> 400", r.status_code == 400)

        r = upload(client, "name,address,phone\n,1 St,5\n")
        check("bulk: empty name row -> 400", r.status_code == 400)

        rows = "\n".join("H{0},Addr {0},5".format(i) for i in range(21))
        r = upload(client, "name,address,phone\n" + rows + "\n")
        check("bulk: >20 rows -> 400", r.status_code == 400)

        r = upload(client, malformed)
        check("bulk: malformed CSV -> 400 (not 500)", r.status_code == 400)

        # --- SYNC bulk create (the core workflow) ------------------------
        t0 = time.monotonic()
        r = upload(client, good)
        b = r.get_json()
        if r.status_code == 200:
            CREATED_BATCHES.append(b["batch_id"])
        keys = set(b.keys()) if isinstance(b, dict) else set()
        expected_keys = {
            "batch_id", "total_hospitals", "processed_hospitals", "failed_hospitals",
            "processing_time_seconds", "batch_activated", "hospitals",
        }
        check("bulk sync: HTTP 200", r.status_code == 200)
        check("bulk sync: exact response key set", keys == expected_keys,
              "extra/missing={}".format(keys ^ expected_keys))
        check("bulk sync: total=3 processed=3 failed=0",
              b.get("total_hospitals") == 3 and b.get("processed_hospitals") == 3
              and b.get("failed_hospitals") == 0)
        check("bulk sync: batch_activated true", b.get("batch_activated") is True)
        rowkeys = set(b["hospitals"][0].keys()) if b.get("hospitals") else set()
        check("bulk sync: per-row keys {row,hospital_id,name,status}",
              rowkeys == {"row", "hospital_id", "name", "status"})
        check("bulk sync: rows 1-based & ordered",
              [h["row"] for h in b.get("hospitals", [])] == [1, 2, 3])
        check("bulk sync: all created_and_activated",
              all(h["status"] == "created_and_activated" for h in b.get("hospitals", [])))
        check("bulk sync: key order matches contract",
              list(keys) and list(b.keys())[0] == "batch_id" and list(b.keys())[-1] == "hospitals")

        # confirm upstream actually has them active
        if b.get("batch_id"):
            fetched = svc.client.get_batch(b["batch_id"])
            check("upstream: batch present & all active",
                  len(fetched) == 3 and all(h["active"] for h in fetched))
        print("  (sync bulk wall time: {:.1f}s)".format(time.monotonic() - t0))

        # --- ASYNC bulk + polling + resume(409) --------------------------
        r = upload(client, good, mode="async")
        b = r.get_json()
        job_id = b.get("job_id")
        if b.get("batch_id"):
            CREATED_BATCHES.append(b["batch_id"])
        check("bulk async: HTTP 202 + job_id + status_url",
              r.status_code == 202 and job_id and b.get("status_url"))

        # While processing, a resume must 409 (atomic claim guard)
        r409 = client.post("/hospitals/bulk/{}/resume".format(job_id))
        check("resume while in-flight -> 409", r409.status_code == 409,
              "got {}".format(r409.status_code))

        # poll to completion
        final = {}
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            snap = client.get("/hospitals/bulk/{}".format(job_id)).get_json()
            if snap.get("status") in {"completed", "partial", "failed"}:
                final = snap
                break
            time.sleep(0.3)
        check("bulk async: job completed", final.get("status") == "completed",
              "status={}".format(final.get("status")))
        check("bulk async: result activated & 3 processed",
              final.get("result", {}).get("batch_activated") is True
              and final.get("result", {}).get("processed_hospitals") == 3)

        # resume after success -> nothing to resume
        r = client.post("/hospitals/bulk/{}/resume".format(job_id))
        check("resume (no failures) -> 200 nothing to resume",
              r.status_code == 200 and "Nothing to resume" in r.get_json().get("message", ""))

        # --- not-found paths ---------------------------------------------
        check("job status unknown -> 404",
              client.get("/hospitals/bulk/nope").status_code == 404)
        check("resume unknown -> 404",
              client.post("/hospitals/bulk/nope/resume").status_code == 404)

    finally:
        # cleanup every batch we created on the shared upstream
        for bid in CREATED_BATCHES:
            try:
                res = svc.client.delete_batch(bid)
                print("  cleanup {}: {}".format(bid, res.get("deleted_count")))
            except Exception as exc:  # noqa: BLE001
                print("  cleanup {} FAILED: {}".format(bid, exc))

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n==== {}/{} checks passed ====".format(passed, total))
    failed = [n for n, ok, _ in RESULTS if not ok]
    if failed:
        print("FAILED:", json.dumps(failed, indent=2))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
