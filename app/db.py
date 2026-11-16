"""SQLite persistence layer.

Design overview
---------------
The service keeps two kinds of history:

* ``batches``      -- one row per uploaded file / ingestion attempt.
* ``object_versions`` -- every distinct (object type, id, version) ever seen.

The object-version table is an append-only event log.  The state of the map
*as of* any batch B is reconstructed by taking, for each object, the highest
version whose ``batch_id <= B``.  This makes batches immutable: later uploads
append rows but never overwrite or erase what an earlier batch produced, so
the GeoJSON downloaded for batch 3 stays identical after batch 7 arrives.

A batch is fully applied inside one SQLite transaction.  If validation or
commit fails the transaction is rolled back and the batch is marked
``rejected`` -- partial state can never survive a failure or a process
restart (rows left in the ``processing`` state by a crash are recovered at
startup).
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = FULL;

CREATE TABLE IF NOT EXISTS batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT    NOT NULL,
    status      TEXT    NOT NULL CHECK (status IN ('processing', 'committed', 'rejected')),
    error       TEXT,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS object_versions (
    object_type  TEXT    NOT NULL CHECK (object_type IN ('node', 'way')),
    object_id    INTEGER NOT NULL,
    version      INTEGER NOT NULL,
    visible      INTEGER NOT NULL,
    lat          REAL,
    lon          REAL,
    tags_json    TEXT    NOT NULL DEFAULT '{}',
    refs_json    TEXT,
    batch_id     INTEGER NOT NULL REFERENCES batches(id),
    PRIMARY KEY (object_type, object_id, version)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_object_versions_batch
    ON object_versions (batch_id);
CREATE INDEX IF NOT EXISTS idx_batches_status
    ON batches (status, id);
"""


def get_db_path() -> Path:
    return Path(config.DB_PATH)


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a connection with the pragmas the application relies on."""
    path = Path(db_path) if db_path is not None else get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        timeout=30.0,
        isolation_level=None,  # explicit transaction control
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    """Create schema and recover batches interrupted by a crash/restart."""
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        # A batch left in 'processing' never committed (the commit and the
        # status update are atomic). Roll back any dangling rows and mark
        # the attempt rejected so no half-batch remains visible.
        stuck = conn.execute(
            "SELECT id FROM batches WHERE status = 'processing' ORDER BY id"
        ).fetchall()
        for row in stuck:
            with conn:  # single DML transaction
                conn.execute(
                    "DELETE FROM object_versions WHERE batch_id = ?", (row["id"],)
                )
                conn.execute(
                    "UPDATE batches SET status = 'rejected', "
                    "error = COALESCE(error, '') || ' [interrupted by restart; rolled back]' "
                    "WHERE id = ?",
                    (row["id"],),
                )
    finally:
        conn.close()


@contextmanager
def db_session(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()
