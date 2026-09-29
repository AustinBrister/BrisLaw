"""HTML-to-Markdown conversion for CourtListener legal opinions.

Provides a custom markdownify MarkdownConverter subclass that handles
CourtListener-specific HTML patterns: citation links, blockquotes,
footnote markers, and case name italicization.

Usage:
    from brislaw.markdown import convert_html_to_markdown

    markdown_text = convert_html_to_markdown(html_opinion_text)
    # With citation links enabled:
    markdown_text = convert_html_to_markdown(html, link_citations=True)
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path

from markdownify import MarkdownConverter

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"

# Reporter page-break markup arrives in at least three formats depending on
# the upstream CourtListener source: Harvard <page-number> tags, Lawbox
# star-pagination spans, and anchor links like <a href="#p627">*627</a>.
# Each is tokenized before markdownify (so the asterisk is never escaped)
# and rendered as a [*N] marker afterward.
_PAGE_NUMBER_TAG = re.compile(
    r'<page-number[^>]*?\blabel="(\d+)"[^>]*>.*?</page-number>', re.I | re.S
)
_STAR_PAGINATION_SPAN = re.compile(
    r'<span[^>]*\bclass="[^"]*star-pagination[^"]*"[^>]*>(.*?)</span>', re.I | re.S
)
_PAGE_ANCHOR = re.compile(
    r'<a[^>]*\b(?:href="#p|name="p|id="p)(\d+)"[^>]*>\s*\*?\d*\s*</a>', re.I | re.S
)


def _tokenize_page_markers(html: str) -> str:
    """Replace reporter page-break markup with @@PGn@@ placeholder tokens."""

    def _span_repl(m: re.Match) -> str:
        digits = re.search(r"(\d+)", re.sub(r"<[^>]+>", "", m.group(1)))
        return f" @@PG{digits.group(1)}@@ " if digits else " "

    html = _PAGE_NUMBER_TAG.sub(lambda m: f" @@PG{m.group(1)}@@ ", html)
    html = _STAR_PAGINATION_SPAN.sub(_span_repl, html)
    html = _PAGE_ANCHOR.sub(lambda m: f" @@PG{m.group(1)}@@ ", html)
    return html


def _render_page_markers(text: str) -> str:
    """Render @@PGn@@ tokens as [*n] markers; collapse duplicates."""
    text = re.sub(r"\s*@@PG(\d+)@@\s*", r" [*\1] ", text)
    # The same page break can carry both a visible label and a target anchor
    text = re.sub(r"\[\*(\d+)\](?:\s*\[\*\1\])+", r"[*\1]", text)
    return text


class LegalOpinionConverter(MarkdownConverter):
    """Convert CourtListener opinion HTML to clean legal markdown.

    Extends markdownify's MarkdownConverter with legal-specific handling:
    - Pre blocks: treated as prose text, not code blocks (CourtListener
      wraps many Texas appellate opinions in ``<pre class="inline">``)
    - Citation links: optionally stripped or converted to full CourtListener URLs
    - Blockquotes: proper markdown ``>`` formatting
    - Footnote markers: ``<sup>`` numbers become ``[^N]`` markers
    """

    def __init__(self, link_citations: bool = False, **kwargs):
        self.link_citations = link_citations
        super().__init__(
            heading_style="ATX",
            bullets="*",
            strong_em_symbol="*",
            **kwargs,
        )

    def convert_pre(self, el, text, parent_tags):
        """Treat <pre> blocks as regular prose, not code blocks.

        CourtListener wraps many Texas appellate opinions in
        ``<pre class="inline">`` tags. The default markdownify behavior
        converts these to triple-backtick code fences, which is wrong
        for opinion text. Instead, return the inner text so it flows
        through the normal post-processing cleanup.
        """
        return f"\n\n{text}\n\n"

    def convert_a(self, el, text, parent_tags):
        """Handle citation links: optionally convert to markdown links.

        CourtListener's ``html_with_citations`` contains ``<a>`` tags linking
        to other opinions with relative paths like ``/opinion/12345/slug/``.
        When ``link_citations`` is False, these links are stripped (text only).
        When True, they become full CourtListener URLs.
        """
        href = el.get("href", "")
        if "/opinion/" in href:
            if not self.link_citations:
                # Strip the link, keep the citation text
                return text
            # Build full URL from relative path
            if href.startswith("/"):
                full_url = f"https://www.courtlistener.com{href}"
            else:
                full_url = href
            return f"[{text}]({full_url})"
        return super().convert_a(el, text, parent_tags)

    def convert_blockquote(self, el, text, parent_tags):
        """Convert blockquotes to markdown ``>`` format."""
        lines = text.strip().splitlines()
        quoted = "\n".join(f"> {line}" for line in lines)
        return f"\n\n{quoted}\n\n"

    def convert_sup(self, el, text, parent_tags):
        """Convert superscript numbers to markdown footnote markers ``[^N]``."""
        stripped = text.strip()
        if stripped.isdigit():
            return f"[^{stripped}]"
        return stripped


def convert_html_to_markdown(
    html: str,
    link_citations: bool = False,
    footnotes: bool = False,
) -> str:
    """Convert opinion HTML to clean legal markdown.

    Args:
        html: CourtListener opinion HTML (typically from
            ``html_with_citations`` or ``html`` fields).
        link_citations: If True, convert citation ``<a>`` tags to full
            CourtListener URLs. If False (default), strip links and keep
            only the citation text.
        footnotes: If True, run LLM-based footnote reformatting for
            ``<pre>``-wrapped opinions. Adds ~15 seconds per opinion.
            If False (default), skip the expensive reformatting step.

    Returns:
        Clean markdown string with ATX headings, blockquote ``>`` format,
        footnote markers, and italicized case names.
    """
    import warnings

    try:
        from bs4 import XMLParsedAsHTMLWarning
    except ImportError:
        XMLParsedAsHTMLWarning = None

    if not html:
        return ""

    html = _tokenize_page_markers(html)

    # Detect whether the HTML is a <pre>-wrapped opinion (common for
    # Texas appellate courts on CourtListener). If so, the text after
    # markdownify conversion will need paragraph-joining cleanup.
    needs_pre_cleanup = bool(re.search(r"<pre[\s>]", html, re.I))

    converter = LegalOpinionConverter(link_citations=link_citations)
    with warnings.catch_warnings():
        if XMLParsedAsHTMLWarning is not None:
            warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
        result = converter.convert(html)

    if needs_pre_cleanup:
        result = _clean_preformatted_text(result)
        if footnotes:
            result = reformat_footnotes(result)

    result = _render_page_markers(result)

    # Clean up: collapse 3+ consecutive newlines to double
    result = re.sub(r"\n{3,}", "\n\n", result)
    result = result.strip()

    # Apply case name italicization
    result = _italicize_case_names(result)

    return result


def _get_anthropic_api_key() -> str | None:
    """Retrieve the Anthropic API key from the environment or the system keychain.

    The keychain entry is service ANTHROPIC_API_KEY under the current login
    name. Only the optional --footnotes cleanup needs this key.
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    import getpass

    account = getpass.getuser()
    if sys.platform == "darwin":
        try:
            key = subprocess.check_output(
                ["security", "find-generic-password", "-a", account, "-s", "ANTHROPIC_API_KEY", "-w"],
                stderr=subprocess.DEVNULL,
            ).decode().strip()
            return key or None
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None
    try:
        import keyring

        return keyring.get_password("ANTHROPIC_API_KEY", account)
    except Exception:
        return None


def reformat_footnotes(text: str) -> str:
    """Use Claude to extract and reformat footnotes in <pre>-sourced opinions.

    Sends the already-cleaned markdown to Claude with a specialized prompt
    that identifies inline footnote definitions, inserts [^N] markers at
    reference points, and appends [^N]: definitions at the end.

    On any error (no API key, API failure), logs a warning and returns the
    input text unchanged.
    """
    api_key = _get_anthropic_api_key()
    if not api_key:
        logger.warning("No Anthropic API key available; skipping footnote reformatting")
        return text

    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic package not installed; skipping footnote reformatting")
        return text

    prompt_text = (PROMPTS_DIR / "footnotes.txt").read_text()

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=16384,
            temperature=0.0,
            system=prompt_text,
            messages=[{"role": "user", "content": text}],
        )
        result = message.content[0].text
        return result
    except Exception:
        logger.warning("Footnote reformatting failed; returning original text", exc_info=True)
        return text


def _clean_preformatted_text(text: str) -> str:
    """Clean up text that originated from a <pre> block.

    CourtListener's <pre>-wrapped opinions preserve the original PDF
    layout: hard line breaks within paragraphs, double-spaced lines,
    bare page numbers, form feeds, and monospaced column alignment.
    This function joins paragraph lines into flowing prose and removes
    PDF artifacts.

    Paragraph detection strategy:
    - If the text is double-spaced (>40% blank lines), strip interleaved
      blanks and use leading whitespace (indent) to detect new paragraphs.
    - New paragraphs start with significant indent (5+ spaces).
    - Continuation lines have no indent.
    - Headings are all-caps or heavily centered.
    """
    # Strip form feeds (page breaks)
    text = text.replace("\x0c", "")

    lines = text.split("\n")

    # Detect double-spacing: if >40% of lines are blank, strip them
    non_empty = [ln for ln in lines if ln.strip()]
    blank_ratio = 1 - (len(non_empty) / max(len(lines), 1))
    if blank_ratio > 0.4:
        lines = non_empty

    paragraphs: list[str] = []
    current: list[str] = []

    prev_indent = 0
    for line in lines:
        raw = line.rstrip()

        # Bare page number (standalone digit(s), possibly with whitespace)
        if re.match(r"^\s*\d{1,3}\s*$", raw):
            continue

        # Empty line = paragraph break (for non-double-spaced text)
        if not raw:
            if current:
                paragraphs.append(" ".join(current))
                current = []
            prev_indent = 0
            continue

        content = raw.lstrip()
        indent = len(raw) - len(content)

        # Case caption block: lines with § column-alignment characters.
        # Keep each line separate (don't join into prose).
        if "\u00a7" in content or "§" in content:
            if current:
                paragraphs.append(" ".join(current))
                current = []
            paragraphs.append(content)
            prev_indent = indent
            continue

        # Heavily centered lines (20+ spaces indent, short content) = headings
        is_centered = indent >= 20 and len(content) < 80

        # Section headings: all-caps with optional Roman/letter numbering
        is_allcaps_heading = bool(re.match(
            r"^(?:[IVX]+\.\s+)?(?:[A-Z]\.\s+)?[A-Z][A-Z\s,.;:\-\x27\"()&/0-9]+$",
            content,
        )) and len(content) < 100

        # Subheadings: indented lines starting with letter/number prefix
        # e.g. "A. Factual background", "(1) Federal standing law"
        is_subheading = indent >= 5 and bool(re.match(
            r"^(?:[A-Z]\.|(?:\(\d+\)))\s+[A-Z]",
            content,
        )) and len(content) < 100

        # Signature/date/panel lines at the end
        is_structural = len(content) < 80 and bool(re.match(
            r"^(?:Before |[A-Z][a-z]+ \d{1,2}, \d{4}|.*(?:C\.J\.|, JJ?\.))",
            content,
        ))

        if is_centered or is_allcaps_heading or is_subheading or is_structural:
            if current:
                paragraphs.append(" ".join(current))
                current = []
            paragraphs.append(content)
            prev_indent = indent
            continue

        # Indented line (5+ spaces) = start of new paragraph,
        # UNLESS the previous line was also indented (block quote continuation).
        if indent >= 5 and current and prev_indent < 5:
            paragraphs.append(" ".join(current))
            current = []

        current.append(content)
        prev_indent = indent

    if current:
        paragraphs.append(" ".join(current))

    return "\n\n".join(paragraphs)


def _italicize_case_names(text: str) -> str:
    """Wrap case name patterns in markdown italics per legal convention.

    Matches patterns like ``Smith v. Jones``, ``State of Texas v. Williams``,
    ``In re Estate of Brown``, ``Ex parte Garcia``.

    Does NOT double-italicize text already wrapped in ``*...*``.
    Does NOT match inside markdown link text ``[...](...)``.
    """
    # Pattern: Match "Name v. Name" style case citations.
    # - Before v.: 1-4 capitalized words, optionally joined by "of", "the", "ex rel."
    # - v. or vs. separator
    # - After v.: 1-4 capitalized words, optionally joined by "of", "the"
    # - Not already italicized (no surrounding *)
    # - Not inside markdown link text (no preceding [)
    #
    # Also matches: "In re Name", "Ex parte Name"
    _name_part = r'[A-Z][A-Za-z\'.-]+'
    _connector = r'(?:\s+(?:of|the|for|and|ex rel\.)\s+)'
    _name_group = rf'{_name_part}(?:{_connector}{_name_part})?(?:\s+{_name_part}){{0,2}}'

    pattern = re.compile(
        r'(?<!\*)'           # not preceded by *
        r'(?<!\[)'           # not inside link text
        r'\b('
        # In re / Ex parte patterns (no v. needed)
        r'(?:In re|Ex parte)\s+' + _name_group +
        r'|'                 # OR standard v. pattern
        + _name_group +
        r'\s+v\.?\s+'
        + _name_group +
        r')'
        r'(?!\*)',           # not followed by *
        re.MULTILINE,
    )

    def _replace(match: re.Match) -> str:
        name = match.group(1).rstrip()
        return f"*{name}*"

    return pattern.sub(_replace, text)


def extract_footnotes(html: str) -> list[tuple[str, str]]:
    """Extract footnote definitions from opinion HTML.

    Parses HTML looking for footnote definitions, typically found at
    the bottom of opinion text in elements with footnote anchors.

    This is best-effort -- CourtListener's footnote HTML format varies
    across sources (Lawbox, Columbia, etc.).

    Args:
        html: Raw opinion HTML.

    Returns:
        List of (number, text) tuples for each footnote found.
    """
    if not html:
        return []

    import warnings

    try:
        from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
    except ImportError:
        return []

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(html, "html.parser")
    footnotes: list[tuple[str, str]] = []

    # Pattern 1: Elements with id like "fn1", "fn2", "footnote_1", etc.
    fn_elements = soup.find_all(id=re.compile(r"(?:fn|footnote)[_-]?(\d+)", re.I))
    for el in fn_elements:
        match = re.search(r"(\d+)", el.get("id", ""))
        if match:
            num = match.group(1)
            text = el.get_text(strip=True)
            # Remove leading footnote number if present
            text = re.sub(r"^\d+\.?\s*", "", text)
            if text:
                footnotes.append((num, text))

    if footnotes:
        return footnotes

    # Pattern 2: <a> tags with name/id like "fn1" inside a container
    fn_anchors = soup.find_all("a", attrs={"name": re.compile(r"(?:fn|footnote)\d+", re.I)})
    for anchor in fn_anchors:
        name = anchor.get("name", "")
        match = re.search(r"(\d+)", name)
        if match:
            num = match.group(1)
            parent = anchor.parent
            if parent:
                text = parent.get_text(strip=True)
                text = re.sub(r"^\d+\.?\s*", "", text)
                if text:
                    footnotes.append((num, text))

    return footnotes
