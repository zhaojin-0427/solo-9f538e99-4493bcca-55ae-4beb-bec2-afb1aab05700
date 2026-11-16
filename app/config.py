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

# Version identifier of the connectivity-check rule set implemented in
# ``app.connectivity``.  Bump this value whenever the graph/classification
# rules change; reports keep the version they were produced with.  A value
# never seen in the database is registered as the newest rule revision at
# startup, which makes earlier reports stale ("过期") and keeps them from
# being presented as the current conclusion.
CONNECTIVITY_RULE_VERSION = os.environ.get(
    "OSM_CONNECTIVITY_RULE_VERSION", "connectivity-v1"
).strip() or "connectivity-v1"

# Hard upper bound on saved start/end node pairs.  The requirement is "at most
# 20", so the environment can lower this but never raise it beyond 20.
MAX_WATCH_PAIRS = min(_env_int("OSM_MAX_WATCH_PAIRS", 20), 20)
