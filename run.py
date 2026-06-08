"""Local development entrypoint.

    python run.py

Uses Flask's built-in server. For production use gunicorn via wsgi:app
(see Dockerfile / render.yaml). On Windows, gunicorn is unavailable, so this
script (or `flask --app wsgi run`) is the way to run locally.
"""
from __future__ import annotations

import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "").lower() in {"1", "true", "yes"}
    # threaded=True lets the dev server handle the concurrent upstream fan-out.
    app.run(host="0.0.0.0", port=port, debug=debug, threaded=True)
