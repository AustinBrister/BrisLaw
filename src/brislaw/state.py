"""Search result persistence for the `brislaw get N` workflow.

Stores the last search results in a JSON file so users can reference
results by number in follow-up commands (e.g., `brislaw get 3`).
"""

from __future__ import annotations

import json
from pathlib import Path

# State directory in user's cache
STATE_DIR = Path.home() / ".cache" / "brislaw"
LAST_SEARCH_FILE = STATE_DIR / "last_search.json"


def save_search_results(results: list[dict]) -> None:
    """Serialize search results to JSON for later retrieval by number.

    Creates the state directory if it does not exist.

    Args:
        results: List of search result dictionaries to persist.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LAST_SEARCH_FILE.write_text(json.dumps(results, default=str))


def get_result_by_number(n: int) -> dict | None:
    """Load the last search results and return the 1-indexed result.

    Args:
        n: 1-based result number (1 = first result).

    Returns:
        The result dictionary, or None if no prior search or out of range.
    """
    if not LAST_SEARCH_FILE.exists():
        return None

    results = json.loads(LAST_SEARCH_FILE.read_text())
    if 1 <= n <= len(results):
        return results[n - 1]
    return None


def clear_search_results() -> None:
    """Delete the search state file."""
    if LAST_SEARCH_FILE.exists():
        LAST_SEARCH_FILE.unlink()
