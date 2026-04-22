"""SQLite-backed persistence layer for download history."""

import logging
import sqlite3
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DB_FILE = "sdm_history.db"

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS downloads (
    id            TEXT PRIMARY KEY,
    url           TEXT NOT NULL,
    filename      TEXT,
    save_path     TEXT,
    total_size    INTEGER,
    status        TEXT,
    segments      INTEGER,
    retries       INTEGER,
    start_time    TEXT,
    end_time      TEXT,
    avg_speed     REAL,
    error_message TEXT
)
"""

_COLUMNS = (
    "id", "url", "filename", "save_path", "total_size",
    "status", "segments", "retries", "start_time", "end_time",
    "avg_speed", "error_message",
)


class DownloadHistory:
    """Manages a SQLite database that records download lifecycle events.

    The database file (``sdm_history.db``) is created in the current working
    directory the first time :py:meth:`init_db` is called.
    """

    def __init__(self, db_path: str = _DB_FILE) -> None:
        """Initialise the history store.

        Args:
            db_path: Path to the SQLite database file.
        """
        self._db_path = db_path

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Open and return a new SQLite connection with row_factory set."""
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def init_db(self) -> None:
        """Create the downloads table if it does not already exist."""
        with self._connect() as conn:
            conn.execute(_CREATE_TABLE_SQL)
            conn.commit()
        logger.info("Database initialised at %s", self._db_path)

    def create(self, record: dict[str, Any]) -> None:
        """Insert a new download record.

        Args:
            record: A dict whose keys correspond to column names.  Unknown
                keys are silently ignored; missing keys default to ``None``.
        """
        values = tuple(record.get(col) for col in _COLUMNS)
        placeholders = ", ".join("?" * len(_COLUMNS))
        columns = ", ".join(_COLUMNS)
        sql = f"INSERT OR REPLACE INTO downloads ({columns}) VALUES ({placeholders})"
        with self._connect() as conn:
            conn.execute(sql, values)
            conn.commit()
        logger.debug("Created history record id=%s", record.get("id"))

    def update(self, download_id: str, **fields: Any) -> None:
        """Update one or more columns for an existing record.

        Args:
            download_id: Primary key of the record to update.
            **fields: Column-name/value pairs to update.
        """
        if not fields:
            return
        valid_fields = {k: v for k, v in fields.items() if k in _COLUMNS and k != "id"}
        if not valid_fields:
            logger.warning("update() called with no valid fields for id=%s", download_id)
            return
        set_clause = ", ".join(f"{col} = ?" for col in valid_fields)
        sql = f"UPDATE downloads SET {set_clause} WHERE id = ?"
        with self._connect() as conn:
            conn.execute(sql, (*valid_fields.values(), download_id))
            conn.commit()
        logger.debug("Updated history record id=%s fields=%s", download_id, list(valid_fields))

    def get(self, download_id: str) -> Optional[dict[str, Any]]:
        """Fetch a single record by primary key.

        Args:
            download_id: The download's unique identifier.

        Returns:
            A dict of column values, or ``None`` if not found.
        """
        sql = "SELECT * FROM downloads WHERE id = ?"
        with self._connect() as conn:
            row = conn.execute(sql, (download_id,)).fetchone()
        return dict(row) if row else None

    def get_all(self) -> list[dict[str, Any]]:
        """Return all download records ordered by start_time descending.

        Returns:
            A list of dicts, one per row.
        """
        sql = "SELECT * FROM downloads ORDER BY start_time DESC"
        with self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [dict(row) for row in rows]

    def delete(self, download_id: str) -> None:
        """Remove a download record from the database.

        Args:
            download_id: The download's unique identifier.
        """
        sql = "DELETE FROM downloads WHERE id = ?"
        with self._connect() as conn:
            conn.execute(sql, (download_id,))
            conn.commit()
        logger.debug("Deleted history record id=%s", download_id)
