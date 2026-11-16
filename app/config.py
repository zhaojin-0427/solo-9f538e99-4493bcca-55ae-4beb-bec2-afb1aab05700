"""Runtime configuration for the OSM batch ingestion service.

All settings can be overridden through environment variables so the same
image works both in Docker Compose and on a developer machine.
"""
from __future__ import annotations

import os
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


# Directory holding the SQLite database file.
DATA_DIR = Path(os.environ.get("OSM_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.environ.get("OSM_DB_PATH", str(DATA_DIR / "osm_batches.db")))

# Reject absurdly large uploads (default 256 MiB).
MAX_CONTENT_LENGTH = _env_int("OSM_MAX_UPLOAD_BYTES", 256 * 1024 * 1024)

# Accepted filename extensions.
ALLOWED_EXTENSIONS = {".osm", ".osc"}

# Flask secret key (session/flash messages). A random one is generated when
# the variable is absent; restarting the container then invalidates sessions.
SECRET_KEY = os.environ.get("OSM_SECRET_KEY", "dev-insecure-secret-change-me")
