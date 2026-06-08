"""Application factory for the Hospital Bulk Processing System."""
from __future__ import annotations

import atexit
import logging
from typing import Optional

from flask import Flask

from .api.errors import register_error_handlers
from .api.routes import bp as api_bp
from .config import Config
from .core.upstream import HospitalDirectoryClient
from .services import Services

__version__ = "1.0.0"


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def create_app(
    config: Optional[Config] = None,
    *,
    client: Optional[HospitalDirectoryClient] = None,
) -> Flask:
    """Build and configure the Flask application.

    ``config`` and ``client`` can be injected for tests (e.g. a fake upstream
    client) without touching the network.
    """
    if config is None:
        # Load a local .env if python-dotenv is installed (dev convenience).
        try:  # pragma: no cover - optional dependency
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:  # pragma: no cover
            pass
    config = config or Config.from_env()
    _configure_logging(config.log_level)

    app = Flask(__name__, static_folder="static")
    app.config["MAX_CONTENT_LENGTH"] = config.max_content_length
    # Flask 3.x: preserve insertion order so responses come out in contract order
    # (the old JSON_SORT_KEYS config key is a no-op on 3.x).
    app.json.sort_keys = False

    services = Services(config, client=client)
    app.extensions["bulk"] = services

    register_error_handlers(app)
    app.register_blueprint(api_bp)

    atexit.register(services.shutdown)
    return app
