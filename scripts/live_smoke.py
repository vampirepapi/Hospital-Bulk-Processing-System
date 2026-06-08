"""Manual live smoke test against the REAL upstream Hospital Directory API.

Creates a tiny batch through the bulk endpoint, prints the result, then
deletes the batch to stay polite to the shared upstream.

    python scripts/live_smoke.py
"""
from __future__ import annotations

import io
import json
import os
import sys

# Allow running as `python scripts/live_smoke.py` from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app


def main() -> int:
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    csv = (
        "name,address,phone\n"
        "Smoke Test Alpha,1 Integration Way,555-0101\n"
        "Smoke Test Bravo,2 Integration Way,\n"
        "Smoke Test Charlie,3 Integration Way,555-0103\n"
    )
    resp = client.post(
        "/hospitals/bulk",
        data={"file": (io.BytesIO(csv.encode("utf-8")), "smoke.csv")},
        content_type="multipart/form-data",
    )
    body = resp.get_json()
    print("status:", resp.status_code)
    print(json.dumps(body, indent=2))

    if resp.status_code != 200:
        return 1

    svc = app.extensions["bulk"]
    batch_id = body["batch_id"]
    fetched = svc.client.get_batch(batch_id)
    print("\nupstream batch now has {} hospital(s), all active={}".format(
        len(fetched), all(h["active"] for h in fetched)))

    print("cleanup:", svc.client.delete_batch(batch_id))
    ok = (
        body["processed_hospitals"] == 3
        and body["failed_hospitals"] == 0
        and body["batch_activated"] is True
    )
    print("\nSMOKE", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
