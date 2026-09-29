"""BrisLaw CLI - Search and retrieve Texas case law from CourtListener.

Provides explicit subcommands (search, get, auth, cache) and smart inference
that detects citations vs. search queries from positional arguments.

Smart inference: when a bare argument is provided (not a subcommand),
the CLI auto-detects whether it's a citation, result number, or search query.
"""

from __future__ import annotations

import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import httpx
import typer
import typer.core

from brislaw import __version__
from brislaw.api import ALL_STATUSES, extract_opinion_id_from_url, get_client, retrieve_with_cache
from brislaw.auth import delete_api_token, get_api_token, set_api_token
from brislaw.citations import looks_like_citation, looks_like_docket_number, parse_citation
from brislaw.courts import court_ids_for_filter, resolve_court_filter
from brislaw.display import (
    console,
    display_error,
    display_fetching_status,
    display_jurisdiction_notice,
    display_no_results,
    display_opinion,
    display_opinion_preview,
    display_pick_list,
    display_search_results,
    display_status,
    emit_json,
    emit_json_error,
    err_console,
    format_opinion_markdown,
    strip_html,
)
from brislaw.models import (
    OpinionDetail,
    SearchResult,
    is_main_opinion_type,
    opinion_type_label,
)
from brislaw.related import best_record, build_warnings, describe_record, docket_records
from brislaw.state import get_result_by_number, save_search_results

# --sort values -> CourtListener order_by
SORT_ORDERS: dict[str, str | None] = {
    "relevance": None,
    "newest": "dateFiled desc",
    "oldest": "dateFiled asc",
    "cited": "citeCount desc",
}

# One search call returns 20 results; larger limits page through.
MAX_LIMIT = 100


def _expand_user(value: Path | None) -> Path | None:
    """Expand a leading ~ in a path option (PowerShell passes it through literally)."""
    return value.expanduser() if value is not None else value


class FreeLawGroup(typer.core.TyperGroup):
    """Custom TyperGroup that supports smart inference for bare arguments.

    When the first argument is not a recognized subcommand, it's treated
    as a query for smart inference (citation -> get, text -> search).
    """

    def parse_args(self, ctx, args: list[str]) -> list[str]:
        """Intercept argument parsing to handle smart inference."""
        # If we have args and the first one is not a known command or option,
        # inject the 'infer' hidden command before the argument
        if args and not args[0].startswith("-") and args[0] not in self.commands:
            args = ["infer"] + args
        return super().parse_args(ctx, args)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"brislaw {__version__}")
        raise typer.Exit()


# --- Main app ---

app = typer.Typer(
    name="brislaw",
    help="Search and retrieve Texas case law from CourtListener.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    cls=FreeLawGroup,
)


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """Search and retrieve Texas case law from CourtListener."""
    # eyecite logs parser diagnostics ("Unknown overlap case...") to stderr,
    # which lands in the middle of JSON output in some shells.
    import logging

    logging.getLogger("eyecite").setLevel(logging.ERROR)
    # Windows pipes default to cp1252, which cannot print section signs,
    # curly quotes, or em dashes in case names and opinion text.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


@app.command(hidden=True)
def infer(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Citation, case name, or search query."),
) -> None:
    """Smart inference: auto-detect citation vs. search query."""
    # Numeric: "brislaw 3" -> get result by number
    if query.strip().isdigit():
        ctx.invoke(
            get, identifier=query, preview=False, court=None, quiet=False,
            json_mode=False, output_file=None, no_cache=False, link_citations=False,
            footnotes=False,
        )
        return

    # Citation or docket number: "brislaw '718 S.W.3d 214'", "brislaw 23-0676" -> get
    if looks_like_citation(query) or looks_like_docket_number(query):
        ctx.invoke(
            get, identifier=query, preview=False, court=None, quiet=False,
            json_mode=False, output_file=None, no_cache=False, link_citations=False,
            footnotes=False,
        )
        return

    # Free text: "brislaw 'royalty dispute'" -> route to search
    ctx.invoke(
        search, query=query, court=None, limit=10, mode="keyword", after=None,
        before=None, sort="relevance", unpublished=False, min_cites=None,
        highlight=True, quiet=False, json_mode=False, output_file=None,
    )


# --- Search command ---


def _get_court_description(court_arg: str) -> str:
    """Return a human-readable description for a --court flag value."""
    court_lower = court_arg.strip().lower()
    descriptions: dict[str, str] = {
        "state": "Texas state courts",
        "federal": "Texas federal courts",
        "txsc": "Texas Supreme Court",
        "cca": "Texas Court of Criminal Appeals",
        "5thcir": "5th Circuit Court of Appeals",
        "all": "all courts (no jurisdiction filter)",
    }
    if court_lower in descriptions:
        return descriptions[court_lower]
    # Bare court ID(s) -- pass through
    return court_arg


# CourtListener's v4 search API takes date limits only as separate URL
# parameters. An old habit (and older versions of SKILL.md) put
# "filed_after:YYYY-MM-DD" inside the query string, which the API reads as a
# search on a nonexistent field and answers with zero results. Such tokens are
# lifted out of the query and turned into real date filters.
_DATE_TOKEN_RE = re.compile(r"(?<!\S)(filed_after|filed_before):(\S+)", re.IGNORECASE)


def _normalize_date(value: str, end_of_year: bool) -> str:
    """Normalize a user date to YYYY-MM-DD.

    Accepts YYYY-MM-DD, MM/DD/YYYY, or a bare year. A bare year becomes
    January 1 for a start date and December 31 for an end date.

    Raises:
        ValueError: If the value is not a recognizable date.
    """
    value = value.strip()
    if re.fullmatch(r"\d{4}", value):
        return f"{value}-12-31" if end_of_year else f"{value}-01-01"
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date '{value}'. Use YYYY-MM-DD or a four-digit year.")


def _resolve_date_filters(
    query: str | None, after: str | None, before: str | None
) -> tuple[str | None, str | None, str | None, list[str]]:
    """Normalize --after/--before and lift filed_after:/filed_before: tokens out of a query.

    An explicit flag wins over a token in the query.

    Returns:
        (cleaned query, filed_after, filed_before, list of lifted tokens)

    Raises:
        ValueError: On an unrecognized date or a start date after the end date.
    """
    lifted: list[str] = []
    if query is not None:
        for match in _DATE_TOKEN_RE.finditer(query):
            lifted.append(match.group(0))
            if match.group(1).lower() == "filed_after" and after is None:
                after = match.group(2)
            elif match.group(1).lower() == "filed_before" and before is None:
                before = match.group(2)
        if lifted:
            query = " ".join(_DATE_TOKEN_RE.sub(" ", query).split())

    filed_after = _normalize_date(after, end_of_year=False) if after else None
    filed_before = _normalize_date(before, end_of_year=True) if before else None
    if filed_after and filed_before and date.fromisoformat(filed_after) > date.fromisoformat(filed_before):
        raise ValueError(f"Start date {filed_after} is after end date {filed_before}.")
    return query, filed_after, filed_before, lifted


def _date_range_label(filed_after: str | None, filed_before: str | None) -> str:
    """Describe a date filter for terminal output, or return '' when none is set."""
    if filed_after and filed_before:
        return f" (filed {filed_after} to {filed_before})"
    if filed_after:
        return f" (filed on or after {filed_after})"
    if filed_before:
        return f" (filed on or before {filed_before})"
    return ""


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query (keywords, phrases, or fielded search)"),
    court: Optional[str] = typer.Option(None, "--court", "-c", help="Court filter: court ID, alias (state/federal/txsc/cca/5thcir), or 'all'"),
    limit: int = typer.Option(10, "--limit", "-n", help="Number of results (default: 10)"),
    mode: str = typer.Option("keyword", "--mode", "-m", help="Search mode: keyword (default) or semantic"),
    after: Optional[str] = typer.Option(None, "--after", help="Only opinions filed on or after this date (YYYY-MM-DD or YYYY)"),
    before: Optional[str] = typer.Option(None, "--before", help="Only opinions filed on or before this date (YYYY-MM-DD or YYYY)"),
    sort: str = typer.Option("relevance", "--sort", "-s", help="Sort: relevance (default), newest, oldest, or cited"),
    unpublished: bool = typer.Option(False, "--unpublished", help="Include unpublished opinions (CourtListener leaves them out by default)"),
    min_cites: Optional[int] = typer.Option(None, "--min-cites", help="Only cases cited at least this many times"),
    highlight: bool = typer.Option(True, "--highlight/--no-highlight", help="Highlight matching terms in snippets"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress status messages"),
    json_mode: bool = typer.Option(False, "--json", help="Output structured JSON"),
    output_file: Optional[Path] = typer.Option(None, "--output", "-o", callback=_expand_user, help="Save output to file"),
) -> None:
    """Search CourtListener for Texas case law.

    Examples:
        brislaw search "royalty dispute"
        brislaw search "estoppel" --court txsc --limit 5
        brislaw search "estoppel" --after 2020-01-01
        brislaw search "estoppel" --after 2015 --before 2020
        brislaw search "estoppel" --court txsc --sort newest
        brislaw search "fraudulent concealment" --sort cited --min-cites 50
        brislaw search "royalty" --court 5thcir --unpublished
        brislaw search "produced water ownership" --mode semantic
        brislaw search "breach of contract" --court all --no-highlight
        brislaw search "estoppel" --json
        brislaw search "estoppel" --json -o results.json
    """
    def _emit_error(code: str, message: str, suggestion: str | None = None) -> None:
        if json_mode:
            emit_json_error(code, message)
        else:
            display_error(message, suggestion)

    # Validate mode
    use_semantic = False
    if mode not in ("keyword", "semantic"):
        _emit_error("invalid_mode", f"Unknown search mode '{mode}'. Use 'keyword' or 'semantic'.")
        raise typer.Exit(code=1)
    if mode == "semantic":
        use_semantic = True
    if sort not in SORT_ORDERS:
        _emit_error("invalid_sort", f"Unknown sort '{sort}'. Use relevance, newest, oldest, or cited.")
        raise typer.Exit(code=1)
    limit = max(1, min(limit, MAX_LIMIT))
    extra_params: dict[str, str | int] = {}
    if unpublished:
        extra_params.update(ALL_STATUSES)
    if min_cites is not None:
        extra_params["cited_gt"] = min_cites

    # Date filters (also lifts filed_after:/filed_before: tokens out of the query)
    try:
        query, filed_after, filed_before, lifted = _resolve_date_filters(query, after, before)
    except ValueError as e:
        _emit_error("invalid_date", str(e))
        raise typer.Exit(code=1)
    if lifted and not quiet:
        display_status(f"Applied {' '.join(lifted)} as a date filter, not as search text.")

    # Status message
    if not quiet:
        mode_label = "semantic" if use_semantic else "keyword"
        display_status(f"Searching CourtListener ({mode_label})...")

    # Resolve court filter
    resolved_filter = resolve_court_filter(court)

    # Show jurisdiction notice when --court overrides defaults
    if court is not None and not json_mode:
        display_jurisdiction_notice(_get_court_description(court))

    # API call
    try:
        client = get_client()
        response = client.search_paged(
            limit,
            query=query,
            courts=resolved_filter,
            highlight=highlight,
            page_size=limit,
            semantic=use_semantic,
            filed_after=filed_after,
            filed_before=filed_before,
            order_by=SORT_ORDERS[sort],
            extra_params=extra_params or None,
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            _emit_error(
                "auth_error",
                "Authentication failed.",
                "Check your API token with `brislaw auth status`.",
            )
            raise typer.Exit(code=1)
        if e.response.status_code == 429:
            _emit_error(
                "rate_limit",
                "Rate limit exceeded.",
                "Try again in a few minutes.",
            )
            raise typer.Exit(code=1)
        _emit_error("api_error", f"API error: {e.response.status_code}")
        raise typer.Exit(code=1)
    except httpx.TimeoutException:
        _emit_error(
            "timeout",
            "CourtListener did not respond in time (retried automatically).",
            "Try again in a moment. If semantic mode keeps timing out, use keyword mode.",
        )
        raise typer.Exit(code=1)
    except httpx.ConnectError:
        _emit_error(
            "connection_error",
            "Cannot connect to CourtListener.",
            "Check your internet connection.",
        )
        raise typer.Exit(code=1)
    except httpx.TransportError as e:
        _emit_error(
            "connection_error",
            f"Connection to CourtListener failed ({type(e).__name__}) after retries.",
            "Try again in a moment.",
        )
        raise typer.Exit(code=1)
    except Exception as e:
        _emit_error("unexpected_error", f"An error occurred: {e}")
        raise typer.Exit(code=1)
    finally:
        if "client" in locals():
            client.close()

    # search_paged has already paged through and trimmed to the limit.
    results_data = response.get("results", [])[:limit]
    total = response.get("count", 0)
    query_label = query + _date_range_label(filed_after, filed_before)

    # No results
    if not results_data:
        if json_mode:
            emit_json(
                {"query": query, "filed_after": filed_after, "filed_before": filed_before,
                 "total": 0, "results": []},
                {"source": "courtlistener", "cached": False, "mode": mode},
                output_file,
            )
            if output_file is not None and not quiet:
                err_console.print(f"[dim]Saved to {output_file}[/dim]")
        else:
            display_no_results(query_label)
        raise typer.Exit()

    # Map to SearchResult dataclass
    results = [SearchResult.from_api_search(r) for r in results_data]
    _annotate_docket_groups(results)

    # --- JSON mode ---
    if json_mode:
        results_dicts = []
        for r in results:
            d = r.to_dict()
            # Clean HTML from snippets for programmatic consumption
            if d.get("snippet"):
                d["snippet_text"] = strip_html(d["snippet"])
            results_dicts.append(d)

        emit_json(
            {"query": query, "filed_after": filed_after, "filed_before": filed_before,
             "total": total, "results": results_dicts},
            {"source": "courtlistener", "cached": False, "mode": mode, "sort": sort,
             "unpublished_included": unpublished, "min_cites": min_cites},
            output_file,
        )
        if output_file is not None and not quiet:
            err_console.print(f"[dim]Saved to {output_file}[/dim]")
        # Still save for `get N` follow-up
        save_search_results(results_data)
        return

    # Display results
    display_search_results(results, query_label, total, highlight=highlight)

    # Save raw API results for `get N` follow-up
    save_search_results(results_data)


def _annotate_docket_groups(results: list[SearchResult]) -> None:
    """Flag results that are separate records of the same case.

    Texas courts file the opinion, concurrences, dissents, the judgment, and
    substituted opinions as separate records with the same caption. When
    several appear in one result list, each gets a note naming the others.
    Records are grouped by docket number, or by case name and filing date
    when the docket numbers differ or are missing.
    """
    def _add_note(numbers: list[int], describe_group: str) -> None:
        for n in numbers:
            r = results[n - 1]
            others = ", ".join(str(x) for x in numbers if x != n)
            kinds = ", ".join(dict.fromkeys(r.opinion_types)) or "unknown type"
            plural = "s" if len(numbers) > 2 else ""
            note = (
                f"{describe_group} as result{plural} {others}. "
                f"This record: {kinds}, filed {r.date_filed}."
            )
            if note not in r.note:
                r.note = f"{r.note} {note}".strip()

    by_docket: dict[tuple[str, str], list[int]] = {}
    for i, r in enumerate(results, start=1):
        if r.docket_number:
            by_docket.setdefault((r.court_id, r.docket_number.upper()), []).append(i)
    grouped: set[int] = set()
    for (_, docket_number), numbers in by_docket.items():
        if len(numbers) > 1:
            _add_note(numbers, f"Same docket ({docket_number})")
            grouped.update(numbers)

    by_name_date: dict[tuple[str, str, str], list[int]] = {}
    for i, r in enumerate(results, start=1):
        key = (r.court_id, " ".join(r.case_name.lower().split()), r.date_filed)
        by_name_date.setdefault(key, []).append(i)
    for numbers in by_name_date.values():
        if len(numbers) > 1 and not set(numbers) <= grouped:
            _add_note(numbers, "Same case name and filing date")


# --- Get command ---


def _retrieve_opinion(
    client: "CourtListenerClient",
    cluster_id: int,
    identifier: str,
    preview: bool,
    quiet: bool,
    json_mode: bool = False,
    output_file: Path | None = None,
    use_cache: bool = True,
    link_citations: bool = False,
    footnotes: bool = False,
    extra_warnings: list[str] | None = None,
) -> None:
    """Shared opinion retrieval logic for all get modes.

    Supports three output paths:
    - **Terminal:** Rich Panel header + convert_html_to_markdown() body (paged if >100 lines)
    - **File (-o):** Pure markdown via format_opinion_markdown()
    - **JSON (--json):** Structured data via emit_json() with to_dict() data

    Args:
        client: An active CourtListenerClient.
        cluster_id: The cluster (case) ID to fetch.
        identifier: The original user identifier (for status message).
        preview: Whether to show preview mode.
        quiet: Whether to suppress status messages.
        json_mode: Output structured JSON instead of terminal display.
        output_file: Save output to this file path.
        use_cache: Whether to use the local opinion cache.
        link_citations: Whether to link cited cases to CourtListener in markdown.
        footnotes: Whether to run LLM-based footnote reformatting (~15s).
        extra_warnings: Warnings from resolving the identifier (an ambiguous
            citation, a case-name guess) to carry into the output.
    """
    if not quiet:
        display_fetching_status(identifier)

    # Fetch cluster + all opinions via cache-integrated retrieval
    cluster, opinions, was_cached = retrieve_with_cache(client, cluster_id, use_cache)

    if not opinions:
        if json_mode:
            emit_json_error("no_opinions", "No opinions found for this case.")
            raise typer.Exit(code=1)
        display_error(
            "No opinions found for this case.",
            "The case record may be incomplete on CourtListener.",
        )
        raise typer.Exit(code=1)

    # Build OpinionDetail from the court's opinion (not a concurrence or
    # dissent that happens to be listed first) + cluster
    primary = next(
        (op for op in opinions if is_main_opinion_type(op.get("type", ""))), opinions[0]
    )
    detail = OpinionDetail.from_api_response(primary, cluster)
    # Store all opinion dicts for multi-opinion rendering
    detail.sub_opinions_data = opinions
    detail.opinions_summary = [
        opinion_type_label(op.get("type", ""))
        + (f" ({op['author_str']})" if op.get("author_str") else "")
        for op in opinions
    ]

    # Other records on the same docket: a concurrence saved as a separate
    # record, the judgment, a substituted opinion. Not cached, because a
    # court can file a new one at any time.
    detail.warnings = list(extra_warnings or [])
    docket_id = cluster.get("docket_id")
    if docket_id:
        try:
            records = docket_records(client, int(docket_id))
        except httpx.HTTPError:
            records = []
        detail.warnings.extend(build_warnings(cluster_id, records))
        detail.related_records = [
            {**r, "description": describe_record(r)}
            for r in records
            if r["cluster_id"] != cluster_id
        ]

    if was_cached and not quiet:
        display_status("Served from cache.")

    # --- JSON mode ---
    if json_mode:
        from brislaw.citations import extract_citations_from_text

        data = detail.to_dict()
        # Add citations found in the opinion text
        try:
            data["citations_found"] = extract_citations_from_text(detail.text or "")
        except Exception:
            data["citations_found"] = []
        meta = {
            "source": "courtlistener",
            "cached": was_cached,
            "cluster_id": cluster_id,
        }
        emit_json(data, meta, output_file)
        if output_file is not None and not quiet:
            err_console.print(f"[dim]Saved to {output_file}[/dim]")
        return

    # --- File output (no JSON) ---
    if output_file is not None:
        md_text = format_opinion_markdown(
            detail, all_opinions=opinions, link_citations=link_citations,
            footnotes=footnotes,
        )
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(md_text, encoding="utf-8")
        if not quiet:
            err_console.print(f"[dim]Saved to {output_file}[/dim]")
        # Warnings go to stdout even with -q: skill workflows discard stderr,
        # and these must be seen before the file is relied on.
        for warning in detail.warnings:
            print(f"WARNING: {warning}")
        return

    # --- Terminal output ---
    if preview:
        display_opinion_preview(detail)
    else:
        display_opinion(
            detail,
            quiet=quiet,
            all_opinions=opinions,
            link_citations=link_citations,
        )


@app.command()
def get(
    identifier: str = typer.Argument(
        ...,
        help="Citation ('718 S.W.3d 214'), docket number ('11-23-00222-CV'), case name ('Apollo v Apache'), result number (3), or cluster ID ('cluster:123456')",
    ),
    preview: bool = typer.Option(
        False, "--preview", "-p", help="Show metadata and first portion of text only"
    ),
    court: Optional[str] = typer.Option(
        None, "--court", "-c", help="Court filter for name searches"
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q", help="Suppress status messages"
    ),
    json_mode: bool = typer.Option(
        False, "--json", help="Output structured JSON"
    ),
    output_file: Optional[Path] = typer.Option(None, "--output", "-o", callback=_expand_user, help="Save output to file"
    ),
    no_cache: bool = typer.Option(
        False, "--no-cache", help="Bypass local cache"
    ),
    link_citations: bool = typer.Option(
        False, "--link-citations", help="Link cited cases to CourtListener"
    ),
    footnotes: bool = typer.Option(
        False, "--footnotes", help="Run LLM footnote reformatting (slow, ~15s per opinion)"
    ),
) -> None:
    """Fetch a case by citation, docket number, name, or result number.

    Examples:
        brislaw get "718 S.W.3d 214"
        brislaw get "11-23-00222-CV"
        brislaw get "23-0676"
        brislaw get "Apollo v Apache"
        brislaw get 3
        brislaw get --preview "718 S.W.3d 214"
        brislaw get "718 SW3d 214"
        brislaw get "718 S.W.3d 214" --json
        brislaw get "718 S.W.3d 214" -o opinion.md
        brislaw get "718 S.W.3d 214" --no-cache
        brislaw get "718 S.W.3d 214" -o opinion.md --footnotes
    """
    identifier = identifier.strip()
    use_cache = not no_cache
    # Programmatic mode: auto-select for ambiguous queries (no pick list)
    programmatic = json_mode or not sys.stdout.isatty()

    def _emit_error(code: str, message: str, suggestion: str | None = None) -> None:
        """Route errors to JSON or Rich depending on mode."""
        if json_mode:
            emit_json_error(code, message)
        else:
            display_error(message, suggestion)

    try:
        client = get_client()

        # --- Mode 0: Explicit cluster ID (race-free, no shared state) ---
        # Result numbers resolve through last_search.json, a single global
        # file overwritten by every `brislaw search` in any session. When
        # multiple sessions/sub-agents run concurrently, `get N` can silently
        # fetch a different session's result. `cluster:ID` (from the search
        # JSON's cluster_id field) bypasses that state entirely.
        if identifier.lower().startswith("cluster:"):
            raw_id = identifier.split(":", 1)[1].strip()
            if not raw_id.isdigit():
                _emit_error(
                    "invalid_cluster",
                    f"Invalid cluster ID '{raw_id}'.",
                    "Use cluster:<number> with the cluster_id from search JSON results.",
                )
                raise typer.Exit(code=1)
            _retrieve_opinion(
                client, int(raw_id), identifier, preview, quiet,
                json_mode=json_mode, output_file=output_file,
                use_cache=use_cache, link_citations=link_citations,
                footnotes=footnotes,
            )
            return

        # --- Mode 1: Result number ---
        if identifier.isdigit():
            result = get_result_by_number(int(identifier))
            if result is None:
                # Distinguish between "no prior search" and "out of range"
                from brislaw.state import LAST_SEARCH_FILE
                import json as _json

                if not LAST_SEARCH_FILE.exists():
                    _emit_error(
                        "no_search",
                        "No recent search results.",
                        "Run `brislaw search` first.",
                    )
                else:
                    saved = _json.loads(LAST_SEARCH_FILE.read_text())
                    _emit_error(
                        "out_of_range",
                        f"Result #{identifier} not found. Last search had {len(saved)} results.",
                        f"Last search had {len(saved)} results.",
                    )
                raise typer.Exit(code=1)

            cluster_id = int(result.get("cluster_id", 0))
            if not cluster_id:
                _emit_error(
                    "invalid_result",
                    "Cannot retrieve this result. No valid cluster ID.",
                    "The search result does not have a valid cluster ID.",
                )
                raise typer.Exit(code=1)

            _retrieve_opinion(
                client, cluster_id, f"result #{identifier}", preview, quiet,
                json_mode=json_mode, output_file=output_file,
                use_cache=use_cache, link_citations=link_citations,
                footnotes=footnotes,
            )
            return

        # --- Mode 1b: Docket number (recent cases often have no reporter cite) ---
        docket_number = looks_like_docket_number(identifier)
        if docket_number:
            cluster_id, docket_warnings = _resolve_docket(client, docket_number, court)
            if not cluster_id:
                _emit_error(
                    "not_found",
                    f"No case found for docket number '{docket_number}'.",
                    "Check the number, or pass --court if the case is outside the default Texas courts.",
                )
                raise typer.Exit(code=1)
            _retrieve_opinion(
                client, cluster_id, identifier, preview, quiet,
                json_mode=json_mode, output_file=output_file,
                use_cache=use_cache, link_citations=link_citations,
                footnotes=footnotes, extra_warnings=docket_warnings,
            )
            return

        # --- Mode 2: Citation ---
        parsed = parse_citation(identifier)
        if parsed:
            # Look up by citation components
            response = client.lookup_by_citation(
                parsed.volume, parsed.reporter, parsed.page
            )
            results_list = response.get("results", [])

            if not results_list and parsed.pin_cite:
                # The page might be a pinpoint, not the starting page.
                # Fall back: query volume + reporter only, then client-side
                # filter for the cluster whose start page is <= given page.
                response = client._request(
                    "GET",
                    "/clusters/",
                    params={
                        "citations__volume": parsed.volume,
                        "citations__reporter": parsed.reporter,
                    },
                )
                results_list = response.get("results", [])
                # Client-side filter: find cluster with highest start page <= pin_cite
                candidates = []
                for r in results_list:
                    for cit in r.get("citations", []):
                        if (
                            cit.get("volume") == parsed.volume
                            and cit.get("reporter") == parsed.reporter
                        ):
                            start_page = int(cit.get("page", 0))
                            if start_page <= parsed.pin_cite:
                                candidates.append((start_page, r))
                if candidates:
                    # Take the one with the highest start page
                    candidates.sort(key=lambda x: x[0], reverse=True)
                    best = candidates[0][1]
                    cluster_id = best.get("id", 0)
                    if not quiet:
                        err_console.print(
                            f"[dim]Resolved pinpoint citation to case starting at page {candidates[0][0]}[/dim]"
                        )
                    _retrieve_opinion(
                        client, cluster_id, identifier, preview, quiet,
                        json_mode=json_mode, output_file=output_file,
                        use_cache=use_cache, link_citations=link_citations,
                    )
                    return
                else:
                    _emit_error(
                        "not_found",
                        f"No case found for citation '{identifier}'.",
                        "Verify the volume, reporter, and page.",
                    )
                    raise typer.Exit(code=1)

            if not results_list:
                _emit_error(
                    "not_found",
                    f"No case found for citation '{identifier}'.",
                    "Verify the volume, reporter, and page.",
                )
                raise typer.Exit(code=1)

            # Take the first (best) match
            cluster_id = results_list[0].get("id", 0)
            cite_warnings = []
            if len(results_list) > 1:
                others = ", ".join(
                    f"cluster:{r.get('id')} ({r.get('case_name', '')})" for r in results_list[1:5]
                )
                cite_warnings.append(
                    f"Citation {identifier} matched {len(results_list)} records; took "
                    f"cluster:{cluster_id} ({results_list[0].get('case_name', '')}). Others: {others}."
                )
            _retrieve_opinion(
                client, cluster_id, identifier, preview, quiet,
                json_mode=json_mode, output_file=output_file,
                use_cache=use_cache, link_citations=link_citations,
                footnotes=footnotes, extra_warnings=cite_warnings,
            )
            return

        # --- Mode 3: Case name ---
        resolved_filter = resolve_court_filter(court)
        response = client.search_by_case_name(identifier, courts=resolved_filter)
        results_data = response.get("results", [])
        count = response.get("count", 0)

        if count == 0 or not results_data:
            _emit_error(
                "not_found",
                f"No cases found matching '{identifier}'.",
                "Try a different spelling or use 'brislaw search' for broader results.",
            )
            raise typer.Exit(code=1)

        if count == 1 or len(results_data) == 1:
            # Single match: retrieve it
            cluster_id = int(results_data[0].get("cluster_id", 0))
            if not cluster_id:
                _emit_error(
                    "invalid_result",
                    "Cannot retrieve this case. No valid cluster ID.",
                    "The search result does not have a valid cluster ID.",
                )
                raise typer.Exit(code=1)
            _retrieve_opinion(
                client, cluster_id, identifier, preview, quiet,
                json_mode=json_mode, output_file=output_file,
                use_cache=use_cache, link_citations=link_citations,
                footnotes=footnotes,
            )
            return

        # Multiple matches: programmatic mode auto-selects first result
        if programmatic:
            first = results_data[0]
            cluster_id = int(first.get("cluster_id", 0))
            if not cluster_id:
                _emit_error(
                    "invalid_result",
                    "Cannot retrieve this case. No valid cluster ID.",
                )
                raise typer.Exit(code=1)
            name_warning = (
                f"Case-name lookup for '{identifier}' matched {count} cases; took the first: "
                f"{first.get('caseName', '')} ({first.get('court_id', '')}, {first.get('dateFiled', '')}). "
                f"Verify the caption, or rerun with cluster:ID from a search."
            )
            _retrieve_opinion(
                client, cluster_id, identifier, preview, quiet,
                json_mode=json_mode, output_file=output_file,
                use_cache=use_cache, link_citations=link_citations,
                footnotes=footnotes, extra_warnings=[name_warning],
            )
            return

        # Interactive: show pick list
        search_results = [SearchResult.from_api_search(r) for r in results_data]
        display_pick_list(search_results, identifier)

        # Save results so user can do `brislaw get N`
        save_search_results(results_data)

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            _emit_error(
                "auth_error",
                "Authentication failed.",
                "Check your API token with `brislaw auth status`.",
            )
            raise typer.Exit(code=1)
        if e.response.status_code == 429:
            _emit_error(
                "rate_limit",
                "Rate limit exceeded.",
                "Try again in a few minutes.",
            )
            raise typer.Exit(code=1)
        _emit_error("api_error", f"API error: {e.response.status_code}")
        raise typer.Exit(code=1)
    except httpx.TimeoutException:
        _emit_error(
            "timeout",
            "CourtListener did not respond in time (retried automatically).",
            "Try again in a moment. If semantic mode keeps timing out, use keyword mode.",
        )
        raise typer.Exit(code=1)
    except httpx.ConnectError:
        _emit_error(
            "connection_error",
            "Cannot connect to CourtListener.",
            "Check your internet connection.",
        )
        raise typer.Exit(code=1)
    except httpx.TransportError as e:
        _emit_error(
            "connection_error",
            f"Connection to CourtListener failed ({type(e).__name__}) after retries.",
            "Try again in a moment.",
        )
        raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except Exception as e:
        _emit_error("unexpected_error", f"An error occurred: {e}")
        raise typer.Exit(code=1)
    finally:
        if "client" in locals():
            client.close()


# --- Citing command ---


def _resolve_docket(
    client, docket_number: str, court: str | None
) -> tuple[int | None, list[str]]:
    """Find the case for a docket number and pick its current opinion.

    Returns (cluster_id, warnings). When the docket holds several records
    (opinion, judgment, concurrence, substituted opinion), picks the latest
    full-length record holding the court's opinion.
    """
    response = client.search_opinions(
        "",
        courts=resolve_court_filter(court),
        highlight=False,
        extra_params={"docket_number": docket_number, **ALL_STATUSES},
    )
    results = response.get("results", [])
    exact = [
        r for r in results
        if (r.get("docketNumber") or "").strip().upper() == docket_number.upper()
    ]
    results = exact or results
    if not results:
        return None, []

    warnings: list[str] = []
    first = results[0]
    courts_found = list(dict.fromkeys(r.get("court_id", "") for r in results))
    if len(courts_found) > 1:
        warnings.append(
            f"Docket number {docket_number} matched cases in {len(courts_found)} courts "
            f"({', '.join(courts_found)}); took the {first.get('court_id', '')} case. "
            f"Pass --court to choose another."
        )

    cluster_id = int(first.get("cluster_id", 0))
    cluster = client.get_cluster(cluster_id)
    docket_id = cluster.get("docket_id")
    records = docket_records(client, int(docket_id)) if docket_id else []
    best = best_record(records)
    if best:
        cluster_id = best["cluster_id"]
    return cluster_id, warnings


def _resolve_to_cluster_id(
    client, identifier: str, json_mode: bool
) -> tuple[int, list[str]]:
    """Resolve a cluster:ID, result number, docket number, citation, or case name.

    Returns (cluster_id, warnings). Exits with an error envelope on failure.
    """
    def _fail(code: str, message: str, suggestion: str | None = None) -> None:
        if json_mode:
            emit_json_error(code, message)
        else:
            display_error(message, suggestion)
        raise typer.Exit(code=1)

    ident = identifier.strip()

    if ident.lower().startswith("cluster:"):
        raw_id = ident.split(":", 1)[1].strip()
        if not raw_id.isdigit():
            _fail(
                "invalid_cluster",
                f"Invalid cluster ID '{raw_id}'.",
                "Use cluster:<number> with the cluster_id from search JSON results.",
            )
        return int(raw_id), []

    if ident.isdigit():
        result = get_result_by_number(int(ident))
        if result is None or not result.get("cluster_id"):
            _fail("no_search", "No recent search results.", "Run `brislaw search` first.")
        return int(result["cluster_id"]), []

    docket_number = looks_like_docket_number(ident)
    if docket_number:
        cluster_id, warnings = _resolve_docket(client, docket_number, None)
        if not cluster_id:
            _fail("not_found", f"No case found for docket number '{docket_number}'.")
        return cluster_id, warnings

    parsed = parse_citation(ident)
    if parsed:
        response = client.lookup_by_citation(parsed.volume, parsed.reporter, parsed.page)
        results = response.get("results", [])
        if not results:
            _fail("not_found", f"No case found for citation '{ident}'.")
        warnings = []
        if len(results) > 1:
            warnings.append(
                f"Citation {ident} matched {len(results)} records; used "
                f"cluster:{results[0].get('id')} ({results[0].get('case_name', '')})."
            )
        return int(results[0]["id"]), warnings

    response = client.search_by_case_name(ident, courts=resolve_court_filter(None))
    results = response.get("results", [])
    if not results:
        _fail("not_found", f"No cases found matching '{ident}'.")
    warnings = []
    if response.get("count", 0) > 1:
        first = results[0]
        warnings.append(
            f"Case-name lookup for '{ident}' matched {response.get('count')} cases; used "
            f"{first.get('caseName', '')} ({first.get('court_id', '')}, {first.get('dateFiled', '')}). "
            f"Rerun with the citation or cluster:ID if that is the wrong case."
        )
    return int(results[0]["cluster_id"]), warnings


CITING_SORTS = ("newest", "oldest", "depth", "cited")


@app.command()
def citing(
    identifier: str = typer.Argument(
        ...,
        help="Citation ('718 S.W.3d 214'), docket number, case name ('Hooks v Samson'), cluster:ID, or result number (3)",
    ),
    court: Optional[str] = typer.Option(
        None, "--court", "-c", help="Filter citing cases by court (default: Texas state and federal courts; 'all' for every court)"
    ),
    limit: int = typer.Option(
        20, "--limit", "-n", help="Number of citing cases to show (default: 20; 0 shows all)"
    ),
    after: Optional[str] = typer.Option(
        None, "--after", help="Only citing opinions filed on or after this date (YYYY-MM-DD or YYYY)"
    ),
    before: Optional[str] = typer.Option(
        None, "--before", help="Only citing opinions filed on or before this date (YYYY-MM-DD or YYYY)"
    ),
    sort: str = typer.Option(
        "newest", "--sort", "-s", help="Sort: newest (default), oldest, depth (most discussion first), or cited"
    ),
    max_scan: int = typer.Option(
        2000, "--max-scan", help="Stop reading the citation table after this many rows (20 per API call)"
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q", help="Suppress status messages"
    ),
    json_mode: bool = typer.Option(
        False, "--json", help="Output structured JSON"
    ),
    output_file: Optional[Path] = typer.Option(None, "--output", "-o", callback=_expand_user, help="Save output to file"
    ),
) -> None:
    """Find cases that cite a given opinion.

    Reads CourtListener's citation table, which is complete where the old
    `cites:` search was not, then looks up each citing case. Each result
    shows how many times it cites the opinion: a case that cites it once is
    usually a string cite; a case that cites it a dozen times discusses it.

    Examples:
        brislaw citing "457 S.W.3d 52"
        brislaw citing "Hooks v Samson"
        brislaw citing "23-0676"
        brislaw citing "457 S.W.3d 52" --sort depth
        brislaw citing "457 S.W.3d 52" --court txsc
        brislaw citing "457 S.W.3d 52" --after 2020 --limit 0
        brislaw citing "457 S.W.3d 52" --json
    """
    from brislaw.citing import (
        case_opinion_ids,
        citing_note,
        collect_citing,
        describe_citing,
        has_reporter_cite,
        name_query,
    )

    def _emit_error(code: str, message: str, suggestion: str | None = None) -> None:
        if json_mode:
            emit_json_error(code, message)
        else:
            display_error(message, suggestion)

    try:
        _, filed_after, filed_before, _ = _resolve_date_filters(None, after, before)
    except ValueError as e:
        _emit_error("invalid_date", str(e))
        raise typer.Exit(code=1)
    if sort not in CITING_SORTS:
        _emit_error("invalid_sort", f"Unknown sort '{sort}'. Use {', '.join(CITING_SORTS)}.")
        raise typer.Exit(code=1)

    try:
        client = get_client()

        # Step 1: Resolve identifier to the cited case
        if not quiet:
            display_status(f"Resolving '{identifier}'...")
        cluster_id, warnings = _resolve_to_cluster_id(client, identifier, json_mode)
        cluster = client.get_cluster(cluster_id)

        # Separate records on the same docket (a concurrence, a substituted
        # opinion) are the same case; citations to them count too. Short
        # records (judgments) are skipped.
        target_ids, exclude_clusters = case_opinion_ids(client, cluster, docket_records)
        if not target_ids:
            _emit_error("no_opinions", "No opinions found for this case.")
            raise typer.Exit(code=1)

        # Step 2: Read the citation table
        if not quiet:
            display_status("Reading CourtListener's citation table...")
        depths, truncated = collect_citing(client, target_ids, max_scan)

        # Step 3: Look up case name, court, and date for each citing opinion
        if not quiet:
            display_status(f"{len(depths)} citing opinions. Looking up the cases...")
        entries, unresolved = describe_citing(client, depths, exclude_clusters)

        # Step 4: Filter, sort, trim
        court_ids = court_ids_for_filter(court)

        def _keep(entry: dict) -> bool:
            r = entry["result"]
            if court_ids is not None and r.get("court_id") not in court_ids:
                return False
            filed = (r.get("dateFiled") or "")[:10]
            if filed_after and filed and filed < filed_after:
                return False
            if filed_before and filed and filed > filed_before:
                return False
            return True

        matching = [e for e in entries if _keep(e)]
        if sort == "depth":
            matching.sort(key=lambda e: (e["depth"], e["result"].get("dateFiled") or ""), reverse=True)
        elif sort == "cited":
            matching.sort(key=lambda e: int(e["result"].get("citeCount") or 0), reverse=True)
        else:
            matching.sort(key=lambda e: e["result"].get("dateFiled") or "", reverse=(sort == "newest"))
        shown = matching if limit <= 0 else matching[:limit]

        results: list[SearchResult] = []
        for entry in shown:
            sr = SearchResult.from_api_search(entry["result"])
            sr.snippet = ""
            sr.depth = entry["depth"]
            sr.note = citing_note(entry["depth"], entry["citing_types"])
            results.append(sr)

        # A case with no reporter cite yet is invisible to the citation
        # table: later opinions cite it by WL number or slip opinion, which
        # CourtListener does not link. Search for its party names instead.
        name_matches: list[SearchResult] = []
        name_search = None
        if not has_reporter_cite(cluster):
            name_search = name_query(cluster.get("case_name", ""))
        if name_search:
            decided = (cluster.get("date_filed") or "")[:10]
            start = max(d for d in (decided, filed_after or "") if d) if (decided or filed_after) else None
            listed = {int(e["result"].get("cluster_id") or 0) for e in entries}
            response = client.search_paged(
                100,
                query=name_search,
                courts=resolve_court_filter(court),
                highlight=False,
                filed_after=start,
                filed_before=filed_before,
                order_by="dateFiled desc",
                extra_params=ALL_STATUSES,
            )
            for r in response.get("results", []):
                cid = int(r.get("cluster_id") or 0)
                if cid in exclude_clusters or cid in listed:
                    continue
                sr = SearchResult.from_api_search(r)
                sr.snippet = ""
                sr.note = (
                    "Mentions the case by name; the citation table does not link it. "
                    "Confirm it actually cites this case."
                )
                name_matches.append(sr)
            warnings.append(
                f"This case has no reporter citation yet, so CourtListener's citation table "
                f"misses opinions that cite it by WL number or slip opinion. A search for "
                f"{name_search} found {len(name_matches)} more that mention it by name; "
                f"they are listed separately."
            )

        if truncated:
            warnings.append(
                f"Stopped after {max_scan} citation-table rows; older citing cases may be "
                f"missing. Rerun with a higher --max-scan to read them all."
            )
        if unresolved:
            warnings.append(
                f"{len(unresolved)} citing opinions could not be identified and are not listed "
                f"(opinion IDs: {', '.join(str(x) for x in unresolved[:10])}"
                f"{'...' if len(unresolved) > 10 else ''})."
            )

        case_name = cluster.get("case_name", "") or identifier
        court_label = court or "Texas state and federal courts (default)"

        if json_mode:
            results_dicts = [r.to_dict() for r in results]
            emit_json(
                {
                    "identifier": identifier,
                    "cluster_id": cluster_id,
                    "case_name": case_name,
                    "opinion_id": target_ids[0],
                    "opinion_ids": target_ids,
                    "citing_cases_all_courts": len(entries),
                    "total": len(matching),
                    "shown": len(results),
                    "court_filter": court_label,
                    "filed_after": filed_after,
                    "filed_before": filed_before,
                    "sort": sort,
                    "truncated": truncated,
                    "unresolved_opinion_ids": unresolved,
                    "warnings": warnings,
                    "results": results_dicts,
                    "name_search": name_search,
                    "name_matches": [r.to_dict() for r in name_matches],
                },
                {"source": "courtlistener", "type": "citing", "method": "citation table"},
                output_file,
            )
            if output_file is not None and not quiet:
                err_console.print(f"[dim]Saved to {output_file}[/dim]")
            save_search_results([e["result"] for e in shown])
            return

        if not matching:
            console.print(
                f"\n  [bold]{len(entries)} cases cite {case_name}, but none match "
                f"the filters ({court_label}"
                f"{_date_range_label(filed_after, filed_before)}).[/bold]\n"
            )
        else:
            console.print(
                f"\n  [dim]{len(entries)} cases in all courts cite this opinion; "
                f"{len(matching)} are in {court_label}"
                f"{_date_range_label(filed_after, filed_before)}. Sorted by {sort}.[/dim]"
            )
            display_search_results(
                results,
                f"cases citing {case_name}",
                len(matching),
                highlight=False,
            )
        if name_matches:
            display_search_results(
                name_matches,
                f"opinions mentioning {name_search} (not in the citation table)",
                len(name_matches),
                highlight=False,
            )
        for warning in warnings:
            console.print(f"  [yellow]Warning:[/yellow] {warning}")
        console.print("  [dim]CourtListener's citation table is not a full citator. Check Lexis or Westlaw before relying on it for good-law status.[/dim]\n")
        save_search_results([e["result"] for e in shown])

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            _emit_error("auth_error", "Authentication failed.", "Check your API token.")
            raise typer.Exit(code=1)
        if e.response.status_code == 429:
            _emit_error("rate_limit", "Rate limit exceeded.", "Try again in a few minutes.")
            raise typer.Exit(code=1)
        _emit_error("api_error", f"API error: {e.response.status_code}")
        raise typer.Exit(code=1)
    except httpx.TimeoutException:
        _emit_error(
            "timeout",
            "CourtListener did not respond in time (retried automatically).",
            "Try again in a moment.",
        )
        raise typer.Exit(code=1)
    except httpx.ConnectError:
        _emit_error("connection_error", "Cannot connect to CourtListener.")
        raise typer.Exit(code=1)
    except httpx.TransportError as e:
        _emit_error(
            "connection_error",
            f"Connection to CourtListener failed ({type(e).__name__}) after retries.",
        )
        raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except Exception as e:
        _emit_error("unexpected_error", f"An error occurred: {e}")
        raise typer.Exit(code=1)
    finally:
        if "client" in locals():
            client.close()


# --- Pins command ---


@app.command()
def pins(
    identifier: str = typer.Argument(
        ..., help="Citation, docket number ('01-21-00331-CV'), case name, or cluster:ID"
    ),
    sources: int = typer.Option(
        25, "--sources", "-n", help="How many citing opinions to read (default 25)"
    ),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress status messages"),
    json_mode: bool = typer.Option(False, "--json", help="Output structured JSON"),
    output_file: Optional[Path] = typer.Option(None, "--output", "-o", callback=_expand_user, help="Save the report to this file"
    ),
) -> None:
    """List the pin cites other courts have used for a case.

    For cases where CourtListener has no page breaks: unreported opinions
    cited by WL number, and reported cases decided after about 2018. Reads
    the opinions that cite the case (those that discuss it most first, plus
    any that mention it by name or docket number) and collects every
    "S.W.3d at N" and "WL ___, at *N" pin they give it, with the text around
    each. Also recovers the case's WL number from citing opinions when
    CourtListener does not list it.

    Examples:
        brislaw pins "01-21-00331-CV"
        brislaw pins "457 S.W.3d 52"
        brislaw pins "cluster:9471980" --sources 40 -o pins.md
    """
    import re as _re
    from collections import Counter

    from brislaw.citing import (
        case_opinion_ids,
        collect_citing,
        describe_citing,
        has_reporter_cite,
        name_query,
    )
    from brislaw.courts import get_court_display_name
    from brislaw.pins import (
        build_patterns,
        extract_pins,
        find_wl_by_docket,
        page_sort_key,
        read_opinion_text,
    )

    def _emit_error(code: str, message: str, suggestion: str | None = None) -> None:
        if json_mode:
            emit_json_error(code, message)
        else:
            display_error(message, suggestion)

    def _status(message: str) -> None:
        if not quiet:
            display_status(message)

    try:
        client = get_client()
        _status(f"Resolving '{identifier}'...")
        cluster_id, warnings = _resolve_to_cluster_id(client, identifier, json_mode)
        cluster, opinions, _ = retrieve_with_cache(client, cluster_id, True)
        case_name = cluster.get("case_name", "") or identifier
        docket_number = cluster.get("docket_number", "") or ""
        decided = (cluster.get("date_filed") or "")[:10]
        court_id = cluster.get("court_id", "") or ""

        reporter_cites = [
            (str(c.get("volume")), c.get("reporter", ""), str(c.get("page")))
            for c in cluster.get("citations", []) or []
            if c.get("type") in (1, 2, 3, 5)
        ]
        wl_cites = [
            f"{c.get('volume')} {c.get('reporter')} {c.get('page')}"
            for c in cluster.get("citations", []) or []
            if c.get("type") in (6, 7)
        ]

        # Does CourtListener's own copy already have page breaks?
        own_pages: list[int] = []
        for op in opinions:
            html = op.get("html_with_citations") or op.get("html") or ""
            own_pages += [int(n) for n in _re.findall(r'star-pagination[^>]*?label="(\d+)"', html)]
            own_pages += [int(n) for n in _re.findall(r'label="(\d+)"[^>]*?star-pagination', html)]
        own_pages = sorted(set(own_pages))

        # Sources, part 1: the citing opinions that discuss the case most
        target_ids, exclude_clusters = case_opinion_ids(client, cluster, docket_records)
        _status("Reading CourtListener's citation table...")
        depths, _ = collect_citing(client, target_ids, max_scan=2000)
        top = dict(sorted(depths.items(), key=lambda kv: -kv[1])[:sources])
        entries, _ = describe_citing(client, top, exclude_clusters) if top else ([], [])
        source_list: list[tuple[int, dict, str]] = []
        for e in entries:
            for oid in e["opinion_ids"]:
                source_list.append((oid, e["result"], f"cites it {e['depth']} times"))

        # Sources, part 2: opinions that mention it by docket number or name.
        # Unreported and recent cases are cited by WL number, which the
        # citation table does not link.
        listed = {int(r.get("cluster_id") or 0) for _, r, _ in source_list} | exclude_clusters
        queries = []
        if docket_number:
            queries.append(f'"{docket_number}"')
        if not has_reporter_cite(cluster):
            q = name_query(case_name)
            if q:
                queries.append(q)
        for q in queries:
            if len(source_list) >= sources:
                break
            response = client.search_paged(
                min(100, sources * 2),
                query=q,
                courts=None,
                highlight=False,
                filed_after=decided or None,
                order_by="dateFiled desc",
                extra_params=ALL_STATUSES,
            )
            for r in response.get("results", []):
                cid = int(r.get("cluster_id") or 0)
                if cid in listed:
                    continue
                listed.add(cid)
                for op in r.get("opinions", []) or []:
                    if op.get("id"):
                        source_list.append((int(op["id"]), r, "mentions it by name or docket number"))
                if len(source_list) >= sources:
                    break
        source_list = source_list[: max(sources, 1)]

        # Read each source; recover the WL number from docket-number cites
        _status(f"Reading {len(source_list)} citing opinions...")
        texts: list[tuple[int, dict, str, str]] = []
        found_wl: Counter = Counter()
        for oid, meta, why in source_list:
            text = read_opinion_text(client, oid)
            if not text:
                continue
            texts.append((oid, meta, why, text))
            for wl in find_wl_by_docket(text, docket_number):
                found_wl[wl] += 1
        discovered = [wl for wl, _ in found_wl.most_common() if wl not in wl_cites]
        patterns = build_patterns(reporter_cites, wl_cites + discovered)

        hits: list[dict] = []
        for oid, meta, why, text in texts:
            for h in extract_pins(text, patterns):
                h.update({
                    "citing_case": meta.get("caseName", ""),
                    "citing_court": get_court_display_name(meta.get("court_id", "")) or meta.get("court_id", ""),
                    "citing_date": meta.get("dateFiled", ""),
                    "citing_cluster_id": meta.get("cluster_id"),
                    "citing_opinion_id": oid,
                    "why_read": why,
                })
                hits.append(h)
        hits.sort(key=lambda h: (h["cite"], page_sort_key(h["pin"])))

    except httpx.HTTPStatusError as e:
        _emit_error("api_error", f"API error: {e.response.status_code}")
        raise typer.Exit(code=1)
    except httpx.TransportError as e:
        _emit_error("connection_error", f"Connection to CourtListener failed ({type(e).__name__}).")
        raise typer.Exit(code=1)
    finally:
        if "client" in locals():
            client.close()

    all_cites = [f"{v} {r} {p}" for v, r, p in reporter_cites] + wl_cites
    if json_mode:
        emit_json(
            {
                "identifier": identifier,
                "cluster_id": cluster_id,
                "case_name": case_name,
                "court_id": court_id,
                "date_filed": decided,
                "docket_number": docket_number,
                "citations": all_cites,
                "wl_found_in_citing_opinions": dict(found_wl),
                "own_page_markers": [own_pages[0], own_pages[-1]] if own_pages else None,
                "opinions_read": len(texts),
                "warnings": warnings,
                "pins": hits,
            },
            {"source": "courtlistener", "type": "pins"},
            output_file,
        )
        return

    out: list[str] = [f"# Pin cites to {case_name}", ""]
    court_label = get_court_display_name(court_id) or court_id
    out.append(
        f"{court_label}, {decided}" + (f", No. {docket_number}" if docket_number else "")
        + ". Cites in CourtListener: " + ("; ".join(all_cites) if all_cites else "none") + "."
    )
    out.append("")
    if own_pages:
        out.append(
            f"CourtListener's own copy of this opinion has reporter page markers (pages "
            f"{own_pages[0]}-{own_pages[-1]}). Pin from the opinion itself with `brislaw get`; "
            f"the list below is a cross-check."
        )
        out.append("")
    if discovered:
        found = "; ".join(f"{wl} (in {found_wl[wl]} opinion{'s' if found_wl[wl] != 1 else ''})" for wl in discovered)
        out.append(f"Westlaw or Lexis cite found in citing opinions, next to the docket number: {found}.")
        out.append("")
        other_years = sorted({wl[:4] for wl in discovered if decided and wl[:4] != decided[:4]})
        if other_years:
            out.append(
                f"Caution: CourtListener dates this record {decided}, but the cite found is from "
                f"{', '.join(other_years)}. The citing courts may be citing a different opinion in the "
                f"same case (a later opinion on the merits or on rehearing). Check the case on Westlaw."
            )
            out.append("")
    for w in warnings:
        out.append(f"Note: {w}")
        out.append("")
    out.append(
        f"Read {len(texts)} opinions that cite this case. The text around each pin is the "
        f"citing court's words, not this case's. It shows which page another court used for a "
        f"point. Confirm on Westlaw before filing if the pin matters."
    )
    out.append("")
    if not hits:
        out.append("No pin cites found.")
    current = None
    for h in hits:
        heading = f"{h['cite']}, at {h['pin']}" if h["pin"].startswith("*") else f"{h['cite']} at {h['pin']}"
        if heading != current:
            current = heading
            out.append(f"## {heading}")
            out.append("")
        out.append(
            f"- *{h['citing_case']}* ({h['citing_court']}, {h['citing_date']}, "
            f"cluster:{h['citing_cluster_id']}): \"{' '.join(h['context'].split())}\""
        )
        out.append("")
    report = "\n".join(out)
    if output_file is not None:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(report, encoding="utf-8")
        pages = len({(h["cite"], h["pin"]) for h in hits})
        print(f"Found {len(hits)} pin cites to {pages} pages in {len(texts)} opinions. Report saved to {output_file}")
    else:
        print(report)


# --- Check command ---


@app.command()
def check(
    draft: Path = typer.Argument(..., callback=_expand_user, help="Word (.docx), markdown, or text file to check"),
    output_file: Optional[Path] = typer.Option(None, "--output", "-o", callback=_expand_user, help="Save the report to this file (markdown, or JSON with --json)"
    ),
    json_mode: bool = typer.Option(False, "--json", help="Output structured JSON"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress status messages"),
) -> None:
    """Check every case citation in a draft against CourtListener.

    Reads a Word, markdown, or text draft from disk (for Word files, line
    numbers are paragraph numbers, footnotes last) and sends only the citation strings, not the
    draft, to CourtListener. Flags citations whose case name does not match
    the case at that cite, wrong first pages, pin pages before the first
    page, year mismatches, and short cites with no full citation. Checks 60
    citations a minute (CourtListener's limit).

    Examples:
        brislaw check brief.md
        brislaw check brief.md -o "Claude (admin)/cite-check.md"
        brislaw check brief.md --json
    """
    from brislaw.check import extract, find_missing_by_name, lookup, render_markdown, to_dict

    def _emit_error(code: str, message: str, suggestion: str | None = None) -> None:
        if json_mode:
            emit_json_error(code, message)
        else:
            display_error(message, suggestion)

    if not draft.exists():
        _emit_error("not_found", f"File not found: {draft}")
        raise typer.Exit(code=1)
    suffix = draft.suffix.lower()
    if suffix not in (".md", ".markdown", ".txt", ".docx"):
        _emit_error(
            "unsupported_file",
            f"brislaw check reads Word (.docx), markdown, or text files, not {draft.suffix} files.",
            "Check the Word version of the document, or convert it to text first.",
        )
        raise typer.Exit(code=1)

    if suffix == ".docx":
        from brislaw.check import read_docx

        try:
            text = read_docx(draft)
        except Exception as e:
            _emit_error("unreadable_file", f"Could not read {draft.name} as a Word document ({e}).")
            raise typer.Exit(code=1)
    else:
        text = draft.read_text(encoding="utf-8", errors="replace")
    cites, short_issues, other = extract(text)
    if not quiet:
        display_status(f"Found {len(cites)} cases cited in {draft.name}. Checking them with CourtListener...")

    if cites:
        try:
            client = get_client()
            lookup(client, cites, status=None if quiet else display_status)
            find_missing_by_name(client, cites)
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            _emit_error(
                "rate_limit" if code == 429 else "api_error",
                f"CourtListener returned HTTP {code} during the check.",
                "Wait a minute and run the check again." if code == 429 else None,
            )
            raise typer.Exit(code=1)
        except httpx.TransportError as e:
            _emit_error("connection_error", f"Connection to CourtListener failed ({type(e).__name__}).")
            raise typer.Exit(code=1)
        finally:
            if "client" in locals():
                client.close()

    if json_mode:
        emit_json(to_dict(draft, cites, short_issues, other), {"source": "courtlistener", "type": "check"}, output_file)
        if output_file is not None and not quiet:
            err_console.print(f"[dim]Saved to {output_file}[/dim]")
        return

    report = render_markdown(draft, cites, short_issues, other)
    if output_file is not None:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(report, encoding="utf-8")
        problems = sum(c.outcome == "problem" for c in cites.values())
        missing = sum(c.outcome in ("not_found", "unknown_reporter", "not_checked", "case_found") for c in cites.values())
        print(
            f"Checked {len(cites)} cases: {problems} with problems, {missing} not in CourtListener, "
            f"{len(short_issues)} short-cite issues. Report saved to {output_file}"
        )
    else:
        print(report)


# --- Auth subcommands ---

auth_app = typer.Typer(help="Manage CourtListener API authentication.")
app.add_typer(auth_app, name="auth")


@auth_app.command("login")
def auth_login(
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Print the token page address instead of opening it"
    ),
) -> None:
    """Connect your CourtListener account: opens the token page, then asks you to paste the token.

    The token is saved in the Mac Keychain or the Windows Credential Manager.
    Run this in your own terminal; never paste the token into a chat.
    """
    import webbrowser

    from brislaw.auth import TOKEN_PAGE, check_api_token

    console.print(
        "\n  [bold]Connect brislaw to your CourtListener account[/bold]\n\n"
        "  1. Sign in to CourtListener (or create a free account and confirm your email).\n"
        "  2. Copy the API token shown on the token page.\n"
        "  3. Paste it below. It will not be shown as you paste.\n"
    )
    opened = False
    if not no_browser:
        try:
            opened = webbrowser.open(TOKEN_PAGE)
        except Exception:
            opened = False
    if opened:
        console.print(f"  Opened the token page in your browser: {TOKEN_PAGE}\n")
    else:
        console.print(f"  Open this page in your browser: {TOKEN_PAGE}\n")

    token = typer.prompt("CourtListener API token", hide_input=True).strip()
    if not token:
        err_console.print("[red]Error:[/red] Token cannot be empty.")
        raise typer.Exit(code=1)

    valid = check_api_token(token)
    if valid is False:
        err_console.print(
            "[red]CourtListener rejected that token.[/red] Copy it again from "
            f"{TOKEN_PAGE} and rerun [bold]brislaw auth login[/bold]."
        )
        raise typer.Exit(code=1)

    set_api_token(token)
    if valid is None:
        console.print(
            "[green]Token saved.[/green] CourtListener could not be reached to confirm it; "
            "if searches fail with an authentication error, run this again."
        )
    else:
        console.print("[green]Token saved and confirmed with CourtListener. brislaw is ready.[/green]")


@auth_app.command("status")
def auth_status(
    json_mode: bool = typer.Option(False, "--json", help="Output structured JSON"),
) -> None:
    """Check whether a CourtListener API token is configured."""
    from brislaw.auth import TOKEN_PAGE, find_api_token

    token, source = find_api_token()
    if json_mode:
        emit_json(
            {
                "configured": bool(token),
                "source": source,
                "setup_command": "brislaw auth login",
                "token_page": TOKEN_PAGE,
            }
        )
        return
    if token:
        where = "Keychain or Credential Manager" if source == "keychain" else "COURTLISTENER_API_TOKEN variable"
        console.print(f"[green]Configured[/green]  (from the {where}; ends in ...{token[-4:]})")
    else:
        console.print(
            "[yellow]Not configured[/yellow]  "
            "Run [bold]brislaw auth login[/bold] in a terminal to set up."
        )


@auth_app.command("logout")
def auth_logout() -> None:
    """Remove the stored API token from the Keychain or Credential Manager."""
    delete_api_token()
    console.print("[green]API token removed.[/green]")


# --- Cache subcommands ---

cache_app = typer.Typer(help="Manage the local opinion cache.")
app.add_typer(cache_app, name="cache")


@cache_app.command("stats")
def cache_stats_cmd() -> None:
    """Show local cache statistics."""
    from brislaw.cache import cache_stats

    stats = cache_stats()
    count = stats["count"]
    size_mb = stats["size_bytes"] / (1024 * 1024)
    db_path = stats["db_path"]

    if count == 0:
        console.print("Cache is empty.")
    else:
        console.print(f"  Cached opinions: [bold]{count}[/bold]")
        console.print(f"  Cache size:      [bold]{size_mb:.2f} MB[/bold]")
    console.print(f"  Database:        [dim]{db_path}[/dim]")


@cache_app.command("clear")
def cache_clear_cmd(
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation prompt"),
) -> None:
    """Clear all cached opinions."""
    from brislaw.cache import cache_stats, clear_cache

    stats = cache_stats()
    count = stats["count"]

    if count == 0:
        console.print("Cache is already empty.")
        return

    if not force:
        typer.confirm(
            f"Delete {count} cached opinion{'s' if count != 1 else ''}?",
            abort=True,
        )

    deleted = clear_cache()
    console.print(f"[green]Cleared {deleted} cached opinion{'s' if deleted != 1 else ''}.[/green]")
