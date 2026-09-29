"""SQLite opinion cache for previously fetched court opinions.

Stores raw API JSON (cluster + opinions) keyed by cluster_id. Court opinions
are immutable, so cached data never expires. Uses WAL mode for safe concurrent
reads (CLI + potential skill access).

Database location: ~/Library/Application Support/brislaw/cache.db (macOS)
via platformdirs.user_data_dir.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from platformdirs import user_data_dir

# Database location: persistent app data (distinct from ~/.cache/brislaw/ used
# by state.py for ephemeral search results)
DB_DIR = Path(user_data_dir("brislaw"))
DB_PATH = DB_DIR / "cache.db"


def _get_connection() -> sqlite3.Connection:
    """Create database directory, open connection, set WAL mode, ensure table exists.

    Returns:
        An open sqlite3 connection ready for queries.
    """
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS opinions (
            cluster_id INTEGER PRIMARY KEY,
            cluster_json TEXT NOT NULL,
            opinions_json TEXT NOT NULL,
            cached_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    return conn


def get_cached_opinion(cluster_id: int) -> dict | None:
    """Retrieve a cached opinion by cluster ID.

    Args:
        cluster_id: The CourtListener cluster ID.

    Returns:
        A dict with ``cluster`` and ``opinions`` keys containing parsed JSON,
        or None if the opinion is not cached.
    """
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT cluster_json, opinions_json FROM opinions WHERE cluster_id = ?",
            (cluster_id,),
        ).fetchone()
        if row:
            return {
                "cluster": json.loads(row[0]),
                "opinions": json.loads(row[1]),
            }
        return None
    finally:
        conn.close()


def cache_opinion(
    cluster_id: int, cluster: dict, opinions: list[dict]
) -> None:
    """Store cluster metadata and opinion data in the cache.

    Uses INSERT OR REPLACE so re-fetching an opinion updates the cached copy.

    Args:
        cluster_id: The CourtListener cluster ID.
        cluster: Raw cluster API response dict.
        opinions: List of raw opinion API response dicts.
    """
    conn = _get_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO opinions "
            "(cluster_id, cluster_json, opinions_json) VALUES (?, ?, ?)",
            (cluster_id, json.dumps(cluster), json.dumps(opinions)),
        )
        conn.commit()
    finally:
        conn.close()


def cache_stats() -> dict:
    """Return cache statistics.

    Returns:
        A dict with ``count`` (number of cached opinions), ``size_bytes``
        (total size of stored JSON data), and ``db_path`` (path to the
        database file as a string).
    """
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*), "
            "COALESCE(SUM(LENGTH(cluster_json) + LENGTH(opinions_json)), 0) "
            "FROM opinions"
        ).fetchone()
        return {
            "count": row[0],
            "size_bytes": row[1],
            "db_path": str(DB_PATH),
        }
    finally:
        conn.close()


def clear_cache() -> int:
    """Delete all cached opinions.

    Returns:
        The number of opinions that were deleted.
    """
    conn = _get_connection()
    try:
        count = conn.execute("SELECT COUNT(*) FROM opinions").fetchone()[0]
        conn.execute("DELETE FROM opinions")
        conn.commit()
        return count
    finally:
        conn.close()


def remove_from_cache(cluster_id: int) -> bool:
    """Delete a specific cached opinion.

    Args:
        cluster_id: The CourtListener cluster ID to remove.

    Returns:
        True if a row was deleted, False if the opinion was not cached.
    """
    conn = _get_connection()
    try:
        cursor = conn.execute(
            "DELETE FROM opinions WHERE cluster_id = ?", (cluster_id,)
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()
