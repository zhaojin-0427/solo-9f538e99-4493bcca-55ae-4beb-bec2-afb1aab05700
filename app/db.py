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

-- Rule revisions are append-only: each registered label is a new revision.
-- Connectivity reports are stamped with the revision they were computed
-- under; a later revision makes every earlier report stale without deleting
-- it.
CREATE TABLE IF NOT EXISTS rule_versions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    version      TEXT    NOT NULL UNIQUE,
    registered_at TEXT   NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Saved OSM node start/end pairs used by the connectivity quality check.
-- At most 20 are kept (enforced transactionally by the application).
CREATE TABLE IF NOT EXISTS connectivity_pairs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    start_node_id INTEGER NOT NULL CHECK (start_node_id > 0),
    end_node_id   INTEGER NOT NULL CHECK (end_node_id > 0),
    label         TEXT,
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS connectivity_reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    from_batch_id   INTEGER NOT NULL REFERENCES batches(id),
    to_batch_id     INTEGER NOT NULL REFERENCES batches(id),
    rule_version_id INTEGER NOT NULL REFERENCES rule_versions(id),
    stale           INTEGER NOT NULL DEFAULT 0 CHECK (stale IN (0, 1)),
    stale_reason    TEXT,
    from_pending_json TEXT NOT NULL DEFAULT '[]',
    to_pending_json   TEXT NOT NULL DEFAULT '[]',
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (from_batch_id <> to_batch_id)
);

CREATE TABLE IF NOT EXISTS connectivity_report_items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id        INTEGER NOT NULL REFERENCES connectivity_reports(id) ON DELETE CASCADE,
    pair_id          INTEGER,             -- snapshot only; pair may be deleted later
    start_node_id    INTEGER NOT NULL,
    end_node_id      INTEGER NOT NULL,
    label            TEXT,
    old_state        TEXT    NOT NULL,
    new_state        TEXT    NOT NULL,
    category         TEXT    NOT NULL,
    old_missing_json TEXT    NOT NULL DEFAULT '[]',
    new_missing_json TEXT    NOT NULL DEFAULT '[]',
    old_endpoint_json TEXT   NOT NULL DEFAULT '{}',
    new_endpoint_json TEXT   NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_connectivity_report_items
    ON connectivity_report_items (report_id);
CREATE INDEX IF NOT EXISTS idx_connectivity_reports_created
    ON connectivity_reports (id);
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
    # Set the busy handler before touching journal mode: switching to WAL
    # needs a short exclusive lock, and several workers may start at once.
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the first release, for existing databases.

    ``CREATE TABLE IF NOT EXISTS`` cannot add columns to a table that already
    exists, so a database created by an older build is upgraded here.  New
    columns all have defaults, so the migration is safe and idempotent.
    """
    additions = {
        "connectivity_reports": {
            "from_pending_json": "TEXT NOT NULL DEFAULT '[]'",
            "to_pending_json": "TEXT NOT NULL DEFAULT '[]'",
        },
        "connectivity_report_items": {
            "old_endpoint_json": "TEXT NOT NULL DEFAULT '{}'",
            "new_endpoint_json": "TEXT NOT NULL DEFAULT '{}'",
        },
    }
    for table, columns in additions.items():
        existing = {
            row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
        }
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init_db(db_path: Path | str | None = None) -> None:
    """Create schema, register the current rule revision, recover crashes."""
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        # Register this build's connectivity rule revision (append-only) and
        # flag every report produced under an older revision stale.  Reports
        # are kept for audit but can never be shown as the current conclusion.
        # Done after the schema exists, before any request can run.
        from .connectivity import register_rule_version
        register_rule_version(conn, config.CONNECTIVITY_RULE_VERSION)
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
