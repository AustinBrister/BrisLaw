"""Rich display module for terminal output and markdown/JSON formatters.

All terminal formatting goes through this module -- CLI commands call
display functions here, never Rich directly.

Phase 2 additions:
    - ``format_opinion_markdown()`` produces complete markdown documents
    - ``emit_json()`` / ``emit_json_error()`` provide consistent JSON output

Consoles:
    console (stdout): results, opinion text
    err_console (stderr): status messages ("Searching...", "Found N results")
"""

from __future__ import annotations

import json
import re
import sys
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING

import click
from rich.console import Console
from rich.markup import escape as rich_escape
from rich.panel import Panel

if TYPE_CHECKING:
    from brislaw.models import OpinionDetail, SearchResult

# Main console for stdout (results, opinion text)
console = Console()

# Status/progress to stderr (so piping still works)
err_console = Console(stderr=True)


# ---------------------------------------------------------------------------
# HTML stripping
# ---------------------------------------------------------------------------

class _HTMLStripper(HTMLParser):
    """Strip HTML tags from text, optionally preserving <mark> as Rich markup.

    When ``preserve_paragraphs`` is True, ``<p>``, ``<br>``, ``<div>``,
    and heading tags are converted to newlines so that paragraph structure
    is maintained in the plain-text output.
    """

    def __init__(
        self,
        keep_marks: bool = False,
        preserve_paragraphs: bool = False,
    ) -> None:
        super().__init__()
        self._keep_marks = keep_marks
        self._preserve_paragraphs = preserve_paragraphs
        self._output = StringIO()

    # Tags that should produce a paragraph break (double newline)
    _BLOCK_TAGS = frozenset({
        "p", "div", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6",
        "li", "tr", "section", "article",
    })

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._keep_marks and tag == "mark":
            self._output.write("[bold yellow]")
        if self._preserve_paragraphs:
            if tag == "br":
                self._output.write("\n")

    def handle_endtag(self, tag: str) -> None:
        if self._keep_marks and tag == "mark":
            self._output.write("[/bold yellow]")
        if self._preserve_paragraphs and tag in self._BLOCK_TAGS:
            self._output.write("\n\n")

    def handle_data(self, data: str) -> None:
        self._output.write(data)

    def handle_entityref(self, name: str) -> None:
        from html import unescape
        self._output.write(unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        from html import unescape
        self._output.write(unescape(f"&#{name};"))

    def get_text(self) -> str:
        return self._output.getvalue()


def strip_html(
    text: str,
    keep_marks: bool = False,
    preserve_paragraphs: bool = False,
) -> str:
    """Strip HTML tags from text.

    Args:
        text: The HTML-containing string.
        keep_marks: If True, convert <mark> tags to Rich markup
            ``[bold yellow]...[/bold yellow]`` before stripping
            the remaining HTML tags.
        preserve_paragraphs: If True, convert block-level tags to
            newlines so paragraph structure is maintained.

    Returns:
        Cleaned text string.
    """
    if not text:
        return ""
    stripper = _HTMLStripper(
        keep_marks=keep_marks,
        preserve_paragraphs=preserve_paragraphs,
    )
    stripper.feed(text)
    result = stripper.get_text()
    if preserve_paragraphs:
        # Collapse runs of 3+ newlines to double newlines
        result = re.sub(r"\n{3,}", "\n\n", result)
        result = result.strip()
    return result


# ---------------------------------------------------------------------------
# Display functions
# ---------------------------------------------------------------------------

def display_search_results(
    results: list[SearchResult],
    query: str,
    total: int,
    highlight: bool = True,
) -> None:
    """Render a list of search results with Rich formatting.

    Output format per result (generous spacing, modern CLI feel like ``gh``):

    - Line 1: ``{number}.  {case_name}`` -- number bold yellow, case name bold white
    - Line 2: ``{citation}  {court}  {date}`` -- cyan / green / dim
    - Line 3: ``{snippet}`` -- dim, with highlighted terms if enabled
    - Blank line between results

    Args:
        results: SearchResult dataclass instances.
        query: The original search query (shown in header).
        total: Total result count from the API.
        highlight: Convert ``<mark>`` tags to bold yellow Rich markup.
    """
    from brislaw.courts import get_court_display_name
    from brislaw.models import is_main_opinion_type

    # Header
    console.print(f"\n  [bold]Found {total:,} results for \"{rich_escape(query)}\"[/bold]\n")

    for i, r in enumerate(results, start=1):
        # Line 1: number + case name
        console.print(f"  [bold yellow]{i}.[/bold yellow]  [bold white]{rich_escape(r.case_name)}[/bold white]")

        # Line 2: citation (or docket number), court, date, and the opinion
        # type when the record is not the court's opinion
        court_display = get_court_display_name(r.court_id) if r.court_id else r.court
        cite = r.citation or (f"No. {r.docket_number}" if r.docket_number else "")
        raw_types = [o.get("type", "") for o in r.opinions if isinstance(o, dict)]
        type_tag = ""
        if raw_types and not any(is_main_opinion_type(t) for t in raw_types):
            type_tag = f"  [magenta]\\[{rich_escape(', '.join(r.opinion_types))}][/magenta]"
        console.print(
            f"      [cyan]{rich_escape(cite)}[/cyan]  "
            f"[green]{rich_escape(court_display)}[/green]  "
            f"[dim]{rich_escape(r.date_filed)}[/dim]{type_tag}"
        )

        # Line 3: note (same-docket records, citing depth)
        if r.note:
            console.print(f"      [yellow]{rich_escape(r.note)}[/yellow]")

        # Line 3: snippet (if present)
        if r.snippet:
            cleaned = strip_html(r.snippet, keep_marks=highlight)
            if not highlight:
                cleaned = rich_escape(cleaned)
            console.print(f"      [dim]{cleaned}[/dim]")

        # Blank line between results (but not after the last one)
        if i < len(results):
            console.print()


def display_jurisdiction_notice(court_filter_description: str) -> None:
    """Show which jurisdiction filter is active.

    Called when the ``--court`` flag overrides the default Texas filter.

    Args:
        court_filter_description: Human-readable description of the active filter.
    """
    console.print(f"  [dim italic]Showing results from: {rich_escape(court_filter_description)}[/dim italic]")


def display_status(message: str) -> None:
    """Print a status message to stderr.

    Uses plain text output (no spinner) to keep things simple and
    compatible with piped/non-TTY environments.

    Args:
        message: Status text to display (e.g., "Searching CourtListener...").
    """
    err_console.print(f"[dim]{rich_escape(message)}[/dim]")


def display_error(message: str, suggestion: str | None = None) -> None:
    """Print an error message in red bold, with optional suggestion.

    Args:
        message: The error message.
        suggestion: A helpful follow-up suggestion (shown in dim below).
    """
    err_console.print(f"[bold red]{rich_escape(message)}[/bold red]")
    if suggestion:
        err_console.print(f"[dim]{rich_escape(suggestion)}[/dim]")


def display_no_results(query: str) -> None:
    """Show a helpful no-results message with suggestions.

    Args:
        query: The search query that returned no results.
    """
    console.print(f'\n  [bold]No results found for "{rich_escape(query)}".[/bold]\n')
    console.print("  [dim]Suggestions:[/dim]")
    console.print("  [dim]  - Try broader terms[/dim]")
    console.print("  [dim]  - Check spelling[/dim]")
    console.print("  [dim]  - Use --court all to search beyond Texas courts[/dim]")


# ---------------------------------------------------------------------------
# Opinion display
# ---------------------------------------------------------------------------


def _build_opinion_header(opinion: OpinionDetail) -> Panel:
    """Build a Rich Panel with opinion metadata.

    The panel shows: case name, citation, court, date, docket number,
    judges, precedential status, opinion type, cite count, and
    disposition -- each with distinct colors for quick scanning.
    """
    lines: list[str] = []
    for warning in opinion.warnings:
        lines.append(f"[bold yellow]WARNING:[/bold yellow] {rich_escape(warning)}")
    lines.append(f"[bold white]{rich_escape(opinion.case_name)}[/bold white]")
    if opinion.citation:
        lines.append(f"[cyan]{rich_escape(opinion.citation)}[/cyan]")
    from brislaw.courts import get_court_display_name

    court = get_court_display_name(opinion.court_id) if opinion.court_id else opinion.court
    if court:
        lines.append(f"[green]{rich_escape(court)}[/green]")
    if opinion.date_filed:
        lines.append(f"[dim]{rich_escape(opinion.date_filed)}[/dim]")
    if opinion.docket_number:
        lines.append(f"Docket: {rich_escape(opinion.docket_number)}")
    if opinion.judges:
        lines.append(f"Judges: {rich_escape(opinion.judges)}")
    if opinion.status:
        lines.append(f"Status: {rich_escape(opinion.status)}")
    if opinion.opinions_summary:
        lines.append(f"Opinions: {rich_escape('; '.join(opinion.opinions_summary))}")
    elif opinion.opinion_type:
        lines.append(f"Opinion: {rich_escape(opinion.opinion_type)}")
    if opinion.cite_count and opinion.cite_count > 0:
        lines.append(f"Cited: {opinion.cite_count} times")
    if opinion.disposition:
        lines.append(f"Disposition: {rich_escape(opinion.disposition)}")
    for record in opinion.related_records:
        lines.append(f"Same docket: {rich_escape(record.get('description', ''))}")
    return Panel("\n".join(lines), expand=False)


def _clean_opinion_text(html_text: str) -> str:
    """Convert HTML opinion text to clean readable format.

    Strips all HTML tags, decodes entities, and preserves paragraph
    structure with double newlines between paragraphs.
    """
    return strip_html(html_text, preserve_paragraphs=True)


def display_opinion(
    opinion: OpinionDetail,
    quiet: bool = False,
    all_opinions: list[dict] | None = None,
    link_citations: bool = False,
) -> None:
    """Display a full opinion with metadata header and markdown-formatted text.

    Uses ``convert_html_to_markdown()`` from the markdown module for the body
    text, producing proper blockquote ``>`` formatting, preserved headings, and
    clean paragraph structure.

    When ``all_opinions`` is provided, renders each opinion as a labeled section
    (Majority Opinion, Concurring Opinion, etc.).

    When stdout is a TTY and the output exceeds 100 lines, the full
    rendered text (header + body) is sent through a pager that respects
    the ``$PAGER`` environment variable.

    Args:
        opinion: The fully populated OpinionDetail.
        quiet: If True, suppress the metadata header panel.
        all_opinions: Optional list of opinion dicts (all sub_opinions).
            When provided, all opinions are rendered with type labels.
        link_citations: If True, convert citation links to CourtListener URLs.
    """
    # Lazy import to avoid loading markdownify when not needed
    from brislaw.markdown import convert_html_to_markdown
    from brislaw.models import TYPE_LABELS

    # Opinion type mapping for API type codes
    type_map = {
        "010combined": "combined",
        "020lead": "majority",
        "030concurrence": "concurrence",
        "040dissent": "dissent",
        "050addendum": "addendum",
        "060remittitur": "remittitur",
        "070rehearing": "rehearing",
        "080on-motion": "on-motion",
        "090original-proceedings": "original-proceedings",
    }

    # Build the text content
    parts: list[str] = []

    if not quiet:
        # Render the header panel to a string for potential paging
        header_buf = StringIO()
        header_console = Console(file=header_buf, force_terminal=True, width=console.width)
        header_console.print(_build_opinion_header(opinion))
        header_console.print()  # blank line after panel
        parts.append(header_buf.getvalue())

    # Render opinion body/bodies
    if all_opinions and len(all_opinions) > 0:
        for op_dict in all_opinions:
            raw_type = op_dict.get("type", "")
            op_type = type_map.get(raw_type, raw_type)
            label = TYPE_LABELS.get(op_type, op_type.title())

            # Extract text (same priority as models.py)
            op_text = ""
            for text_field in (
                "html_with_citations",
                "html",
                "plain_text",
                "html_lawbox",
                "html_columbia",
                "html_anon_2020",
            ):
                content = op_dict.get(text_field, "")
                if content:
                    op_text = content
                    break

            # Only show section header if multiple opinions
            if len(all_opinions) > 1:
                parts.append(f"\n--- {label} ---\n")

            if op_text:
                md_body = convert_html_to_markdown(op_text, link_citations=link_citations)
                parts.append(md_body)
            else:
                parts.append("No text available for this opinion.")
    elif opinion.text:
        md_body = convert_html_to_markdown(opinion.text, link_citations=link_citations)
        parts.append(md_body)
    else:
        parts.append("No opinion text available for this case.")

    full_output = "\n".join(parts)

    # Pager behavior: auto-page when output > 100 lines AND stdout is a TTY
    line_count = full_output.count("\n")
    if sys.stdout.isatty() and line_count > 100:
        click.echo_via_pager(full_output)
    else:
        # Use console.print only for the Rich-formatted header
        if not quiet:
            console.print(_build_opinion_header(opinion))
            console.print()

        # Print the body text (skip the header part we already printed)
        if all_opinions and len(all_opinions) > 0:
            for op_dict in all_opinions:
                raw_type = op_dict.get("type", "")
                op_type = type_map.get(raw_type, raw_type)
                label = TYPE_LABELS.get(op_type, op_type.title())

                op_text = ""
                for text_field in (
                    "html_with_citations",
                    "html",
                    "plain_text",
                    "html_lawbox",
                    "html_columbia",
                    "html_anon_2020",
                ):
                    content = op_dict.get(text_field, "")
                    if content:
                        op_text = content
                        break

                if len(all_opinions) > 1:
                    console.print(f"\n[bold]--- {rich_escape(label)} ---[/bold]\n")

                if op_text:
                    md_body = convert_html_to_markdown(op_text, link_citations=link_citations)
                    console.print(md_body)
                else:
                    console.print("[dim italic]No text available for this opinion.[/dim italic]")
        elif opinion.text:
            md_body = convert_html_to_markdown(opinion.text, link_citations=link_citations)
            console.print(md_body)
        else:
            console.print("[dim italic]No opinion text available for this case.[/dim italic]")


def display_opinion_preview(
    opinion: OpinionDetail,
    max_chars: int = 2000,
) -> None:
    """Display opinion metadata and a truncated preview of the text.

    Never uses the pager since preview output is always short.

    Args:
        opinion: The fully populated OpinionDetail.
        max_chars: Maximum characters of opinion text to show.
    """
    console.print(_build_opinion_header(opinion))
    console.print()

    if opinion.text:
        cleaned = _clean_opinion_text(opinion.text)
        if len(cleaned) > max_chars:
            console.print(cleaned[:max_chars])
            console.print(
                "\n  [dim]... [Use `brislaw get` without --preview for full text][/dim]\n"
            )
        else:
            console.print(cleaned)
    else:
        console.print("[dim italic]No opinion text available for this case.[/dim italic]")


def display_pick_list(results: list[SearchResult], query: str) -> None:
    """Display a numbered pick list of matching cases.

    Used when a case name search matches multiple results.
    Shows numbered results without snippets, with instructions
    to use ``brislaw get N`` to select.

    Args:
        results: List of matching SearchResult instances.
        query: The original query that produced multiple matches.
    """
    from brislaw.courts import get_court_display_name

    console.print(f'\n  [bold]Multiple matches for "{rich_escape(query)}":[/bold]\n')

    for i, r in enumerate(results, start=1):
        # Line 1: number + case name
        console.print(
            f"  [bold yellow]{i}.[/bold yellow]  "
            f"[bold white]{rich_escape(r.case_name)}[/bold white]"
        )
        # Line 2: citation, court, date (no snippet)
        court_display = get_court_display_name(r.court_id) if r.court_id else r.court
        console.print(
            f"      [cyan]{rich_escape(r.citation)}[/cyan]  "
            f"[green]{rich_escape(court_display)}[/green]  "
            f"[dim]{rich_escape(r.date_filed)}[/dim]"
        )
        if i < len(results):
            console.print()

    console.print(
        "\n  Use [bold]brislaw get N[/bold] to select a case.\n"
    )


def display_fetching_status(identifier: str) -> None:
    """Print a fetching status message to stderr.

    Shows a brief status so the user knows retrieval is in progress.

    Args:
        identifier: The citation, name, or number being fetched.
    """
    err_console.print(f"[dim]Fetching {rich_escape(identifier)}...[/dim]")


def display_cache_status(was_cached: bool) -> None:
    """Print a dim cache indicator to stderr.

    Args:
        was_cached: Whether the result was served from cache.
    """
    if was_cached:
        err_console.print("[dim][cached][/dim]")
    else:
        err_console.print("[dim][from API][/dim]")


# ---------------------------------------------------------------------------
# Markdown opinion formatter (Phase 2)
# ---------------------------------------------------------------------------


def _table_cell(text: str) -> str:
    """Make text safe for one markdown table cell."""
    return " ".join(str(text).split()).replace("|", "\\|")


def format_opinion_markdown(
    opinion: OpinionDetail,
    all_opinions: list[dict] | None = None,
    link_citations: bool = False,
    footnotes: bool = False,
) -> str:
    """Produce a complete markdown document from an opinion.

    Renders a full legal document with metadata table, opinion sections
    (majority, concurrence, dissent), optional syllabus/headnotes,
    footnotes, and source attribution.

    Args:
        opinion: The primary OpinionDetail with all metadata.
        all_opinions: Optional list of opinion dicts (from sub_opinions_data
            or fetched separately). Each dict should have ``type`` and one
            of the text fields (``html_with_citations``, ``html``, etc.).
            If None, only the primary opinion.text is rendered.
        link_citations: If True, convert citation ``<a>`` tags to full
            CourtListener URLs in the markdown output.
        footnotes: If True, run LLM-based footnote reformatting for
            ``<pre>``-wrapped opinions (~15s per opinion). Default False.

    Returns:
        Complete markdown document as a string.
    """
    # Lazy imports to avoid loading markdownify when not needed
    from brislaw.markdown import convert_html_to_markdown, extract_footnotes
    from brislaw.models import TYPE_LABELS

    from brislaw.courts import get_court_display_name

    parts: list[str] = []

    # Title
    parts.append(f"# {opinion.case_name}")
    parts.append("")

    # Metadata table -- only include rows with non-empty values
    parts.append("| Field | Value |")
    parts.append("|-------|-------|")

    # Warnings first, so a caption check that reads only the top of the
    # file still sees them.
    for warning in opinion.warnings:
        parts.append(f"| **WARNING** | {_table_cell(warning)} |")

    if opinion.citation:
        parts.append(f"| **Citation** | {opinion.citation} |")

    if opinion.other_citations:
        parts.append(f"| **Other cites** | {'; '.join(opinion.other_citations)} |")

    court_display = get_court_display_name(opinion.court_id) if opinion.court_id else opinion.court
    if court_display:
        parts.append(f"| **Court** | {court_display} |")

    if opinion.date_filed:
        parts.append(f"| **Date** | {opinion.date_filed} |")

    if opinion.docket_number:
        parts.append(f"| **Docket** | {opinion.docket_number} |")

    if opinion.opinions_summary:
        parts.append(f"| **Opinions** | {_table_cell('; '.join(opinion.opinions_summary))} |")

    if opinion.judges:
        parts.append(f"| **Judges** | {opinion.judges} |")

    if opinion.status:
        parts.append(f"| **Status** | {opinion.status} |")

    if opinion.cite_count:
        parts.append(f"| **Cited** | {opinion.cite_count} times |")

    if opinion.disposition:
        parts.append(f"| **Disposition** | {opinion.disposition} |")

    if opinion.related_records:
        described = "; ".join(r.get("description", "") for r in opinion.related_records)
        parts.append(f"| **Other records on this docket** | {_table_cell(described)} |")

    # Pagination row is inserted here after the body is rendered, so the
    # reader (human or model) knows whether pin cites can be taken from
    # this text. Remember the slot.
    pagination_idx = len(parts)

    parts.append("")

    # Syllabus / headnotes block (non-judicial per user decision)
    has_syllabus = bool(opinion.syllabus and opinion.syllabus.strip())
    has_headnotes = bool(opinion.headnotes and opinion.headnotes.strip())
    if has_syllabus or has_headnotes:
        parts.append("---")
        parts.append("")
        parts.append("> **Note:** The following syllabus/headnotes are not part of the judicial opinion.")
        parts.append("")
        if has_syllabus:
            parts.append(convert_html_to_markdown(opinion.syllabus, link_citations=link_citations))
            parts.append("")
        if has_headnotes:
            parts.append(convert_html_to_markdown(opinion.headnotes, link_citations=link_citations))
            parts.append("")

    parts.append("---")
    parts.append("")

    # Collect all footnotes for rendering at the bottom
    all_footnotes: list[tuple[str, str]] = []

    # Opinion type mapping for API type codes
    type_map = {
        "010combined": "combined",
        "020lead": "majority",
        "030concurrence": "concurrence",
        "040dissent": "dissent",
        "050addendum": "addendum",
        "060remittitur": "remittitur",
        "070rehearing": "rehearing",
        "080on-motion": "on-motion",
        "090original-proceedings": "original-proceedings",
    }

    if all_opinions:
        # Multi-opinion rendering
        for op_dict in all_opinions:
            raw_type = op_dict.get("type", "")
            op_type = type_map.get(raw_type, raw_type)
            label = TYPE_LABELS.get(op_type, op_type.title())

            # Extract text from opinion dict (same priority as models.py)
            op_text = ""
            for text_field in (
                "html_with_citations",
                "html",
                "plain_text",
                "html_lawbox",
                "html_columbia",
                "html_anon_2020",
            ):
                content = op_dict.get(text_field, "")
                if content:
                    op_text = content
                    break

            parts.append(f"## {label}")
            parts.append("")

            if op_text:
                # Try to extract footnotes from this opinion's HTML
                fns = extract_footnotes(op_text)
                all_footnotes.extend(fns)

                md_text = convert_html_to_markdown(op_text, link_citations=link_citations, footnotes=footnotes)
                parts.append(md_text)
            else:
                parts.append("*No text available for this opinion.*")

            parts.append("")
            parts.append("---")
            parts.append("")
    else:
        # Single opinion rendering
        label = TYPE_LABELS.get(opinion.opinion_type, opinion.opinion_type.title())
        parts.append(f"## {label}")
        parts.append("")

        # Author attribution from judges field if available
        if opinion.judges:
            parts.append(f"*{opinion.judges}*")
            parts.append("")

        if opinion.text:
            fns = extract_footnotes(opinion.text)
            all_footnotes.extend(fns)

            md_text = convert_html_to_markdown(opinion.text, link_citations=link_citations, footnotes=footnotes)
            parts.append(md_text)
        else:
            parts.append("*No opinion text available for this case.*")

        parts.append("")
        parts.append("---")
        parts.append("")

    # Source attribution
    parts.append(f"*Source: CourtListener (https://www.courtlistener.com/opinion/{opinion.cluster_id}/)*")
    parts.append("")

    # Footnotes at the bottom
    if all_footnotes:
        for num, text in all_footnotes:
            parts.append(f"[^{num}]: {text}")
        parts.append("")

    # Pagination status row: reporter page markers ([*N]) survive conversion
    # only when the CourtListener source carries them. When absent, pin
    # cites cannot be verified from this text and must not be invented.
    body = "\n".join(parts[pagination_idx:])
    marker_pages = sorted({int(n) for n in re.findall(r"\[\*(\d+)\]", body)})
    if marker_pages:
        parts.insert(
            pagination_idx,
            f"| **Pagination** | [*N] markers present, pages {marker_pages[0]}-{marker_pages[-1]} |",
        )
    else:
        parts.insert(
            pagination_idx,
            "| **Pagination** | NONE — do not pin cite from this text; "
            "verify pins via Lexis/Westlaw or citing opinions |",
        )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# JSON output helpers (Phase 2)
# ---------------------------------------------------------------------------


def emit_json(
    data: dict,
    meta: dict | None = None,
    output_file: Path | None = None,
) -> None:
    """Emit a JSON response envelope to stdout or to a file.

    All JSON output uses this function to ensure a consistent envelope:
    ``{"status": "ok", "data": {...}, "meta": {...}}``.

    **Important:** Output goes to stdout via ``print()``, NOT via
    ``console.print()``, to ensure no Rich formatting leaks into JSON.

    Args:
        data: The main response data dict.
        meta: Optional metadata dict (source, cached, etc.).
        output_file: If provided, write JSON to this file path instead
            of stdout.
    """
    response: dict = {
        "status": "ok",
        "data": data,
    }
    if meta:
        response["meta"] = meta

    output = json.dumps(response, indent=2, default=str, ensure_ascii=False)

    if output_file is not None:
        output_file.write_text(output, encoding="utf-8")
    else:
        # Print to stdout without Rich formatting
        print(output)


def emit_json_error(code: str, message: str) -> None:
    """Emit a JSON error response to stdout.

    Error envelope: ``{"status": "error", "error": {"code": ..., "message": ...}}``.

    Args:
        code: Machine-readable error code (e.g., "not_found", "auth_error").
        message: Human-readable error description.
    """
    response = {
        "status": "error",
        "error": {
            "code": code,
            "message": message,
        },
    }
    print(json.dumps(response, indent=2, ensure_ascii=False))
