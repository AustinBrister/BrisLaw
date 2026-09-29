"""CourtListener API client with rate limiting and retry handling."""

from __future__ import annotations

import os
import random
import re
import sys
import time
from types import TracebackType

import httpx

# Timeouts and retry policy. CourtListener is normally fast (well under 2s) but
# occasionally hangs or returns a 502/503/504 under load; semantic search is
# computed server-side and is slower. Override with environment variables.
DEFAULT_TIMEOUT = float(os.environ.get("BRISLAW_TIMEOUT", "30"))
SEMANTIC_TIMEOUT = float(os.environ.get("BRISLAW_SEMANTIC_TIMEOUT", "60"))
CONNECT_TIMEOUT = 10.0
MAX_ATTEMPTS = max(1, int(os.environ.get("BRISLAW_RETRIES", "3")))
RETRYABLE_STATUS = {500, 502, 503, 504}
MAX_RETRY_AFTER = 120

# The v4 search API returns only precedential ("Published") opinions unless
# status filters are given. These turn every status on.
ALL_STATUSES: dict[str, str] = {
    "stat_Published": "on",
    "stat_Unpublished": "on",
    "stat_Errata": "on",
    "stat_Separate": "on",
    "stat_In-chambers": "on",
    "stat_Relating-to": "on",
    "stat_Unknown": "on",
}

from brislaw.auth import get_api_token
from brislaw.ratelimit import TokenBucket


def extract_opinion_id_from_url(url: str) -> int:
    """Extract the opinion ID from a CourtListener API URL.

    Examples:
        >>> extract_opinion_id_from_url("https://www.courtlistener.com/api/rest/v4/opinions/12345/")
        12345
        >>> extract_opinion_id_from_url("/api/rest/v4/opinions/12345/")
        12345

    Args:
        url: A CourtListener opinion API URL.

    Returns:
        The integer opinion ID.

    Raises:
        ValueError: If the URL does not contain a parseable opinion ID.
    """
    m = re.search(r"/opinions/(\d+)/?", url)
    if not m:
        raise ValueError(f"Cannot extract opinion ID from URL: {url}")
    return int(m.group(1))


class CourtListenerClient:
    """HTTP client for the CourtListener REST API v4.

    Handles authentication, rate limiting, and HTTP 429 retry.
    Supports context manager usage for automatic resource cleanup.
    """

    BASE_URL = "https://www.courtlistener.com/api/rest/v4"

    def __init__(self, token: str) -> None:
        """Initialize the client with an API token.

        Args:
            token: CourtListener API token for authentication.
        """
        self._client = httpx.Client(
            base_url=self.BASE_URL,
            headers={"Authorization": f"Token {token}"},
            timeout=httpx.Timeout(DEFAULT_TIMEOUT, connect=CONNECT_TIMEOUT),
        )
        self._rate_limiter = TokenBucket(capacity=5000, refill_rate=5000 / 3600)

    def search_opinions(
        self,
        query: str,
        courts: str | None = None,
        highlight: bool = True,
        page_size: int = 10,
        semantic: bool = False,
        filed_after: str | None = None,
        filed_before: str | None = None,
        order_by: str | None = None,
        extra_params: dict | None = None,
    ) -> dict:
        """Search CourtListener for opinions matching a query.

        Args:
            query: Search query string. Supports fielded search operators.
            courts: Space-separated court IDs to filter, or None for no filter.
            highlight: Enable <mark> tag highlighting in snippets.
            page_size: Number of results per page (default 10).
            semantic: Enable semantic (vector) search. When True, CourtListener
                uses server-side embeddings to find conceptually related cases
                beyond keyword matching.
            filed_after: Only opinions filed on or after this date (YYYY-MM-DD).
            filed_before: Only opinions filed on or before this date (YYYY-MM-DD).
                Both dates must be separate URL parameters. The v4 search API
                has no ``filed_after:`` operator inside ``q``; putting one
                there returns zero results.
            order_by: Sort order, e.g. "dateFiled desc" or "citeCount desc".
                None keeps CourtListener's relevance order.
            extra_params: Additional search parameters passed through as-is
                (status filters, cite-count bounds, docket_number, ...).

        Returns:
            Raw JSON response from the search API.
        """
        params: dict[str, str | int] = {
            "type": "o",
            "q": query,
            "page_size": page_size,
        }
        if courts:
            params["court"] = courts
        if filed_after:
            params["filed_after"] = filed_after
        if filed_before:
            params["filed_before"] = filed_before
        if highlight:
            params["highlight"] = "on"
        if semantic:
            params["semantic"] = "true"
        if order_by:
            params["order_by"] = order_by
        if extra_params:
            params.update(extra_params)

        return self._request(
            "GET",
            "/search/",
            params=params,
            timeout=SEMANTIC_TIMEOUT if semantic else None,
        )

    def get_cluster(self, cluster_id: int) -> dict:
        """Fetch cluster (case) metadata by ID.

        Args:
            cluster_id: The CourtListener cluster ID.

        Returns:
            Raw JSON response with cluster details.
        """
        return self._request("GET", f"/clusters/{cluster_id}/")

    def get_opinion(self, opinion_id: int) -> dict:
        """Fetch a single opinion by ID.

        Args:
            opinion_id: The CourtListener opinion ID.

        Returns:
            Raw JSON response with opinion details and text.
        """
        return self._request("GET", f"/opinions/{opinion_id}/")

    def lookup_by_citation(
        self, volume: int, reporter: str, page: int
    ) -> dict:
        """Look up clusters by citation components.

        Args:
            volume: Citation volume number.
            reporter: Canonical reporter abbreviation (e.g., "S.W.3d").
            page: Starting page number.

        Returns:
            Raw JSON response with matching clusters.
        """
        return self._request(
            "GET",
            "/clusters/",
            params={
                "citations__volume": volume,
                "citations__reporter": reporter,
                "citations__page": page,
            },
        )

    def search_by_case_name(
        self, name: str, courts: str | None = None
    ) -> dict:
        """Search for cases by case name.

        Args:
            name: Case name to search for (wrapped in fielded query).
            courts: Space-separated court IDs to filter, or None.

        Returns:
            Raw JSON response from the search API.
        """
        params: dict[str, str] = {
            "type": "o",
            "q": f'caseName:"{name}"',
        }
        if courts:
            params["court"] = courts

        return self._request("GET", "/search/", params=params)

    def get_docket(self, docket_id: int) -> dict:
        """Fetch the court and docket number for a docket.

        CourtListener keeps the court and docket number on the docket
        record, not on the cluster, so every retrieval needs this call to
        fill the Court and Docket rows.
        """
        return self._request(
            "GET",
            f"/dockets/{docket_id}/",
            params={"fields": "id,court_id,docket_number,case_name"},
        )

    def list_docket_clusters(self, docket_id: int) -> list[dict]:
        """Every cluster (case record) filed under one docket.

        Texas courts post separately filed documents (the Court's opinion,
        a concurrence, a dissent, the judgment, a substituted opinion) as
        separate CourtListener clusters that share one docket.
        """
        return self.get_all(
            "/clusters/",
            params={
                "docket": docket_id,
                "fields": "id,date_filed,case_name,judges,citations,precedential_status",
            },
            max_items=100,
        )

    def list_docket_opinions(self, docket_id: int) -> list[dict]:
        """Every opinion under one docket, with type, author, and page count."""
        return self.get_all(
            "/opinions/",
            params={
                "cluster__docket": docket_id,
                "fields": "id,cluster_id,type,author_str,per_curiam,page_count",
            },
            max_items=200,
        )

    def iter_citing(self, opinion_id: int):
        """Yield (citing_opinion_id, depth) rows from the citation table.

        The citation table (``/opinions-cited/``) is CourtListener's full
        citation graph; its row count matches the cluster's citation count.
        The ``cites:`` search operator reads the search index instead, which
        can miss most of them. ``depth`` is how many times the citing opinion
        cites this one.
        """
        params: dict | None = {"cited_opinion": opinion_id, "fields": "citing_opinion,depth"}
        url: str | None = "/opinions-cited/"
        while url:
            page = self._request("GET", url, params=params)
            for row in page.get("results", []):
                try:
                    citing_id = extract_opinion_id_from_url(row.get("citing_opinion", ""))
                except ValueError:
                    continue
                yield citing_id, int(row.get("depth") or 1)
            url = page.get("next")
            params = None  # the next URL already carries the query

    def search_by_opinion_ids(self, opinion_ids: list[int]) -> list[dict]:
        """Search results (case name, court, date, cites) for specific opinion IDs.

        Uses the search index's ``id:`` field, 20 IDs per request. Opinions
        not yet in the search index are simply absent from the results.
        """
        results: list[dict] = []
        for i in range(0, len(opinion_ids), 20):
            chunk = opinion_ids[i:i + 20]
            page = self._request(
                "GET",
                "/search/",
                params={
                    "type": "o",
                    "q": "id:(" + " OR ".join(str(x) for x in chunk) + ")",
                    **ALL_STATUSES,
                },
            )
            results.extend(page.get("results", []))
        return results

    def citation_lookup(self, text: str) -> list[dict]:
        """Look up every citation in ``text`` with CourtListener's citation-lookup API.

        Limits (per CourtListener): 64,000 characters and 250 citations per
        request, 60 citations per minute. A throttled request is retried
        after the wait CourtListener names.
        """
        return self._request("POST", "/citation-lookup/", data={"text": text}, timeout=90)

    def get_all(
        self, path: str, params: dict | None = None, max_items: int = 100
    ) -> list[dict]:
        """Follow ``next`` links on a list endpoint and return up to max_items rows."""
        results: list[dict] = []
        url: str | None = path
        while url and len(results) < max_items:
            page = self._request("GET", url, params=params)
            results.extend(page.get("results", []))
            url = page.get("next")
            params = None
        return results[:max_items]

    def search_paged(self, limit: int, **kwargs) -> dict:
        """Run search_opinions and follow ``next`` links until ``limit`` results.

        The v4 search API returns 20 results per page and ignores page_size.
        Returns the first page's response with ``results`` extended.
        """
        first = self.search_opinions(**kwargs)
        results = list(first.get("results", []))
        next_url = first.get("next")
        timeout = SEMANTIC_TIMEOUT if kwargs.get("semantic") else None
        while next_url and len(results) < limit:
            page = self._request("GET", next_url, timeout=timeout)
            results.extend(page.get("results", []))
            next_url = page.get("next")
        first["results"] = results[:limit]
        return first

    def get_opinion_text(self, opinion_id: int) -> str:
        """Fetch an opinion and extract its text content.

        Checks text fields in priority order:
        html_with_citations > html > plain_text > html_lawbox > html_columbia > html_anon_2020

        Args:
            opinion_id: The CourtListener opinion ID.

        Returns:
            The opinion text (may be HTML), or empty string if all fields are empty.
        """
        opinion = self.get_opinion(opinion_id)
        for field in (
            "html_with_citations",
            "html",
            "plain_text",
            "html_lawbox",
            "html_columbia",
            "html_anon_2020",
        ):
            content = opinion.get(field, "")
            if content:
                return content
        return ""

    def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        timeout: float | None = None,
        data: dict | None = None,
    ) -> dict:
        """Make a rate-limited API request with retry handling.

        Retries (up to MAX_ATTEMPTS total) with short exponential backoff on:
        read/connect timeouts, dropped connections, and 5xx responses.
        HTTP 429 waits for the Retry-After header (capped) and retries.
        Other 4xx errors raise immediately.

        Args:
            method: HTTP method (GET, POST, etc.).
            path: API endpoint path, or an absolute ``next`` URL from a
                paginated response.
            params: Query parameters.
            timeout: Per-request read timeout override in seconds.
            data: Form fields for a POST body.

        Returns:
            Parsed JSON response.

        Raises:
            httpx.HTTPStatusError: On non-retryable HTTP errors, or a
                retryable one that persisted through every attempt.
            httpx.TransportError: On a timeout or connection failure that
                persisted through every attempt.
        """
        self._rate_limiter.acquire()

        kwargs: dict = {"params": params}
        if data is not None:
            kwargs["data"] = data
        if timeout is not None:
            kwargs["timeout"] = httpx.Timeout(timeout, connect=CONNECT_TIMEOUT)

        for attempt in range(1, MAX_ATTEMPTS + 1):
            last_attempt = attempt >= MAX_ATTEMPTS
            try:
                resp = self._client.request(method, path, **kwargs)
            except httpx.TransportError as e:
                # Timeouts, connection resets, protocol errors
                if last_attempt:
                    raise
                self._backoff(attempt, f"{type(e).__name__}")
                continue

            if resp.status_code == 429:
                if last_attempt:
                    resp.raise_for_status()
                retry_after = _retry_after_seconds(resp)
                retry_after = min(max(retry_after, 1), MAX_RETRY_AFTER)
                print(
                    f"Rate limited. Retrying in {retry_after}s "
                    f"(attempt {attempt}/{MAX_ATTEMPTS})...",
                    file=sys.stderr,
                )
                time.sleep(retry_after)
                continue

            if resp.status_code in RETRYABLE_STATUS and not last_attempt:
                self._backoff(attempt, f"HTTP {resp.status_code}")
                continue

            resp.raise_for_status()
            return resp.json()

        # Unreachable: every loop path returns, raises, or continues.
        raise RuntimeError("request retry loop exited unexpectedly")

    @staticmethod
    def _backoff(attempt: int, reason: str) -> None:
        """Sleep before a retry: 1s, 2s, 4s... plus a little jitter."""
        delay = 2 ** (attempt - 1) + random.uniform(0, 0.5)
        print(
            f"CourtListener {reason}; retrying in {delay:.1f}s "
            f"(attempt {attempt}/{MAX_ATTEMPTS})...",
            file=sys.stderr,
        )
        time.sleep(delay)

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> CourtListenerClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()


def _retry_after_seconds(resp: httpx.Response) -> int:
    """Seconds to wait after a 429: Retry-After header, else the JSON wait_until time."""
    header = resp.headers.get("Retry-After")
    if header:
        try:
            return int(header)
        except ValueError:
            pass
    try:
        body = resp.json()
    except Exception:
        return 60
    # The citation-lookup API names the time it will accept the next request.
    # Its docs spell the key "wait_util"; accept either spelling.
    when = (body.get("wait_until") or body.get("wait_util")) if isinstance(body, dict) else None
    if when:
        from datetime import datetime, timezone

        try:
            target = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            return max(1, int((target - datetime.now(timezone.utc)).total_seconds()) + 1)
        except ValueError:
            pass
    return 60


def retrieve_with_cache(
    client: CourtListenerClient,
    cluster_id: int,
    use_cache: bool = True,
) -> tuple[dict, list[dict], bool]:
    """Fetch cluster + all opinions, with cache layer.

    Checks the local SQLite cache before making API calls. On cache miss,
    fetches the cluster and ALL sub_opinions (not just the primary).

    Args:
        client: An active CourtListenerClient.
        cluster_id: The cluster (case) ID to fetch.
        use_cache: Whether to check/store in the local cache.

    Returns:
        (cluster_dict, opinions_list, was_cached)
        - cluster_dict: raw cluster API response
        - opinions_list: list of raw opinion API responses (all sub_opinions)
        - was_cached: True if served from cache
    """
    # Check cache first (lazy import to avoid sqlite3/platformdirs when not needed)
    if use_cache:
        from brislaw.cache import cache_opinion, get_cached_opinion

        cached = get_cached_opinion(cluster_id)
        if cached is not None:
            cluster = cached["cluster"]
            # Entries cached before the docket fix lack court and docket number
            if "docket_number" not in cluster and _attach_docket(client, cluster):
                cache_opinion(cluster_id, cluster, cached["opinions"])
            return cluster, cached["opinions"], True

    # Cache miss or cache disabled: fetch from API
    cluster = client.get_cluster(cluster_id)
    _attach_docket(client, cluster)

    # Fetch ALL sub_opinions (not just the primary like Phase 1)
    opinions: list[dict] = []
    for url in cluster.get("sub_opinions", []):
        opinion_id = extract_opinion_id_from_url(url)
        opinion_data = client.get_opinion(opinion_id)
        opinions.append(opinion_data)

    # Store in cache
    if use_cache:
        from brislaw.cache import cache_opinion

        cache_opinion(cluster_id, cluster, opinions)

    return cluster, opinions, False


def _attach_docket(client: CourtListenerClient, cluster: dict) -> bool:
    """Copy the court ID and docket number from the docket record onto the cluster.

    The v4 cluster record has neither; without this, saved opinions have no
    Court or Docket row. Returns True if the cluster was updated.
    """
    docket_id = cluster.get("docket_id")
    if not docket_id:
        return False
    try:
        docket = client.get_docket(int(docket_id))
    except (httpx.HTTPError, ValueError):
        return False
    cluster["court_id"] = docket.get("court_id", "") or ""
    cluster["docket_number"] = docket.get("docket_number", "") or ""
    return True


def get_client() -> CourtListenerClient:
    """Create a CourtListenerClient using the stored API token.

    Retrieves the token from macOS Keychain and constructs a client.

    Returns:
        A configured CourtListenerClient instance.

    Raises:
        SystemExit: If no API token is configured.
    """
    token = get_api_token()
    if not token:
        print(
            "No API token found. Run `brislaw auth login` to configure.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return CourtListenerClient(token)
