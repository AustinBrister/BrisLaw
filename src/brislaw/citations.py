"""Citation parsing for legal citations using eyecite.

Primary parsing uses eyecite (2,102 reporter variations via reporters-db).
Regex fallback preserved for edge cases eyecite might miss.
Fast ``looks_like_citation()`` pre-filter uses regex only (no eyecite import).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# Parsed citation result
@dataclass
class ParsedCitation:
    """A parsed legal citation with volume, reporter, page, and optional pin cite."""

    volume: int
    reporter: str
    page: int
    pin_cite: int | None = None


# Texas-relevant reporters: canonical form -> regex pattern
# Kept as fallback for edge cases eyecite might miss
REPORTERS: dict[str, str] = {
    "S.W.3d": r"S\.?\s*W\.?\s*3d",
    "S.W.2d": r"S\.?\s*W\.?\s*2d",
    "S.W.": r"S\.?\s*W\.?\s*(?!2d|3d)",
    "Tex.": r"Tex\.?(?!\s*(?:App|Crim))",
    "F.4th": r"F\.?\s*4th",
    "F.3d": r"F\.?\s*3d",
    "F.2d": r"F\.?\s*2d",
    "F. Supp. 3d": r"F\.?\s*Supp\.?\s*3d",
    "F. Supp. 2d": r"F\.?\s*Supp\.?\s*2d",
    "U.S.": r"U\.?\s*S\.?",
}

# Normalization map: lowercase-stripped variant -> canonical form
REPORTER_NORMALIZE: dict[str, str] = {
    "sw3d": "S.W.3d",
    "s.w.3d": "S.W.3d",
    "sw2d": "S.W.2d",
    "s.w.2d": "S.W.2d",
    "sw": "S.W.",
    "s.w.": "S.W.",
    "tex": "Tex.",
    "tex.": "Tex.",
    "f4th": "F.4th",
    "f.4th": "F.4th",
    "f3d": "F.3d",
    "f.3d": "F.3d",
    "f2d": "F.2d",
    "f.2d": "F.2d",
    "fsupp3d": "F. Supp. 3d",
    "f.supp.3d": "F. Supp. 3d",
    "fsuppd3d": "F. Supp. 3d",
    "f. supp. 3d": "F. Supp. 3d",
    "fsupp2d": "F. Supp. 2d",
    "f.supp.2d": "F. Supp. 2d",
    "fsuppd2d": "F. Supp. 2d",
    "f. supp. 2d": "F. Supp. 2d",
    "us": "U.S.",
    "u.s.": "U.S.",
}

# Mapping from eyecite reporter output to CourtListener expected format.
# eyecite's reporter strings mostly match CourtListener's, but this map
# provides a safe normalization layer.
EYECITE_TO_CL_REPORTER: dict[str, str] = {
    "S.W.3d": "S.W.3d",
    "S.W.2d": "S.W.2d",
    "S.W.": "S.W.",
    "Tex.": "Tex.",
    "F.4th": "F.4th",
    "F.3d": "F.3d",
    "F.2d": "F.2d",
    "F. Supp. 3d": "F. Supp. 3d",
    "F. Supp. 2d": "F. Supp. 2d",
    "U.S.": "U.S.",
}


def _build_citation_pattern() -> re.Pattern:
    """Build a combined regex pattern for all known reporters."""
    reporter_alts = "|".join(
        f"(?:{pat})" for pat in REPORTERS.values()
    )
    # Full citation: volume (digits) + reporter + page (digits) + optional pin cite
    # e.g., "718 S.W.3d 214" or "718 S.W.3d 214 at 220"
    pattern = (
        rf"(\d+)\s+({reporter_alts})\s+(\d+)"
        rf"(?:\s*(?:,\s*)?at\s+(\d+))?"
    )
    return re.compile(pattern, re.IGNORECASE)


def _build_short_form_pattern() -> re.Pattern:
    """Build a pattern for short-form pin cites: 'volume reporter at page'.

    Handles citations like '718 S.W.3d at 215' where the 'at' page is the
    only page reference (short-form citation referencing a previously cited case).
    """
    reporter_alts = "|".join(
        f"(?:{pat})" for pat in REPORTERS.values()
    )
    pattern = rf"(\d+)\s+({reporter_alts})\s+at\s+(\d+)"
    return re.compile(pattern, re.IGNORECASE)


_CITATION_RE = _build_citation_pattern()
_SHORT_FORM_RE = _build_short_form_pattern()

# Quick pattern for smart inference: digits + something + digits
_LOOKS_LIKE_RE = re.compile(r"\d+\s+\S+\s+\d+")


def normalize_reporter(text: str) -> str | None:
    """Normalize a reporter abbreviation to canonical form.

    Strips periods and spaces, lowercases, then looks up in the normalization map.

    Args:
        text: A reporter abbreviation like "SW3d", "S.W.3d", "S. W. 3d".

    Returns:
        Canonical form like "S.W.3d", or None if not recognized.
    """
    # Try direct lookup first (handles "S.W.3d", "F.3d", etc.)
    stripped = text.strip().lower()
    if stripped in REPORTER_NORMALIZE:
        return REPORTER_NORMALIZE[stripped]

    # Strip all periods and spaces for fuzzy matching
    compact = stripped.replace(".", "").replace(" ", "")
    if compact in REPORTER_NORMALIZE:
        return REPORTER_NORMALIZE[compact]

    return None


def _parse_pin_cite(raw: str | None) -> int | None:
    """Extract a numeric pin cite from eyecite's metadata.pin_cite.

    eyecite may return pin cites as ``"220"`` or ``"at 220"``.
    This strips any non-numeric prefix and returns the integer.

    Args:
        raw: The raw pin_cite string from eyecite metadata, or None.

    Returns:
        The pin cite page number as an integer, or None.
    """
    if not raw:
        return None
    # Strip "at " prefix if present, then extract digits
    cleaned = raw.strip()
    if cleaned.lower().startswith("at "):
        cleaned = cleaned[3:].strip()
    # Handle comma-separated pin cites: take the first number
    cleaned = cleaned.split(",")[0].strip()
    try:
        return int(cleaned)
    except (ValueError, TypeError):
        return None


def _normalize_eyecite_reporter(reporter: str) -> str:
    """Normalize an eyecite reporter string to CourtListener format.

    Tries the direct eyecite-to-CL mapping first, then falls back to
    the general ``normalize_reporter()`` function for fuzzy matching
    (handles cases like ``SW3d`` -> ``S.W.3d``).

    Args:
        reporter: Reporter string from eyecite's groups dict.

    Returns:
        Normalized reporter string suitable for CourtListener API queries.
    """
    # Direct match (most common case -- eyecite usually returns canonical form)
    if reporter in EYECITE_TO_CL_REPORTER:
        return EYECITE_TO_CL_REPORTER[reporter]

    # Fuzzy fallback via Phase 1 normalization map
    normalized = normalize_reporter(reporter)
    if normalized is not None:
        return normalized

    # Return as-is if no normalization found
    return reporter


def _parse_with_eyecite(text: str) -> ParsedCitation | None:
    """Parse a citation using eyecite.

    Lazy imports eyecite to avoid ~100-200ms startup cost on non-citation
    commands.

    Args:
        text: Citation string to parse.

    Returns:
        ParsedCitation if eyecite finds a valid citation, None otherwise.
    """
    import warnings

    from eyecite import clean_text, get_citations
    from eyecite.models import FullCaseCitation, ShortCaseCitation

    cleaned = clean_text(text, ["all_whitespace"])
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore")
        cites = get_citations(cleaned, remove_ambiguous=True)

    # Try full citations first
    full_cites = [c for c in cites if isinstance(c, FullCaseCitation)]
    if full_cites:
        c = full_cites[0]
        volume = int(c.groups["volume"])
        reporter = c.groups["reporter"]
        page = int(c.groups["page"])

        # Normalize reporter to CourtListener format
        reporter = _normalize_eyecite_reporter(reporter)

        pin_cite = _parse_pin_cite(c.metadata.pin_cite)

        return ParsedCitation(
            volume=volume,
            reporter=reporter,
            page=page,
            pin_cite=pin_cite,
        )

    # Try short-form citations (e.g., "718 S.W.3d at 215")
    short_cites = [c for c in cites if isinstance(c, ShortCaseCitation)]
    if short_cites:
        c = short_cites[0]
        volume = int(c.groups["volume"])
        reporter = c.groups["reporter"]
        page = int(c.groups["page"])

        reporter = _normalize_eyecite_reporter(reporter)

        return ParsedCitation(
            volume=volume,
            reporter=reporter,
            page=page,
            pin_cite=page,  # In short-form, the page IS the pin cite
        )

    return None


def _parse_with_regex(text: str) -> ParsedCitation | None:
    """Parse a citation using the Phase 1 regex patterns (fallback).

    Args:
        text: Citation string to parse.

    Returns:
        ParsedCitation if regex matches, None otherwise.
    """
    m = _CITATION_RE.search(text)
    if m:
        volume = int(m.group(1))
        reporter_text = m.group(2).strip()
        page = int(m.group(3))
        pin_cite = int(m.group(4)) if m.group(4) else None

        normalized = normalize_reporter(reporter_text)
        if normalized is None:
            normalized = reporter_text

        return ParsedCitation(
            volume=volume,
            reporter=normalized,
            page=page,
            pin_cite=pin_cite,
        )

    # Try short-form pin cite: "718 S.W.3d at 215"
    m = _SHORT_FORM_RE.search(text)
    if m:
        volume = int(m.group(1))
        reporter_text = m.group(2).strip()
        page = int(m.group(3))

        normalized = normalize_reporter(reporter_text)
        if normalized is None:
            normalized = reporter_text

        return ParsedCitation(
            volume=volume,
            reporter=normalized,
            page=page,
            pin_cite=page,  # In short-form, the page IS the pin cite
        )

    return None


def parse_citation(text: str) -> ParsedCitation | None:
    """Parse a citation string into its components.

    Uses eyecite (2,102 reporter variations) as the primary parser with
    a regex fallback for edge cases. Handles both full-form citations
    (``718 S.W.3d 214``) and short-form pin cites (``718 S.W.3d at 215``).

    Args:
        text: A citation string like "718 S.W.3d 214", "718 SW3d 214 at 220",
              or "718 S.W.3d at 215" (short-form pin cite).

    Returns:
        A ParsedCitation with volume, normalized reporter, page, and optional
        pin cite. Returns None if no citation pattern is found.
    """
    # Try eyecite first (handles 2,102 reporter variations)
    result = _parse_with_eyecite(text)
    if result is not None:
        return result

    # Fall back to regex for edge cases eyecite might miss
    return _parse_with_regex(text)


def looks_like_citation(text: str) -> bool:
    """Quick check whether text looks like a legal citation.

    Uses regex only -- does NOT import eyecite, so there is no startup cost.
    Used by the CLI smart inference callback to decide between
    search (free text query) and get (citation lookup).

    Pattern: digits + non-space + digits (e.g., "718 SW3d 214").

    Args:
        text: The user's input string.

    Returns:
        True if the text matches a citation-like pattern.
    """
    return bool(_LOOKS_LIKE_RE.search(text))


def extract_citations_from_text(text: str) -> list[dict]:
    """Extract all case citations found in opinion text.

    Uses eyecite to parse the text and extract full case citations.
    Results are deduplicated by (volume, reporter, page).

    Used for: JSON ``citations_found`` field, optional citation linking.

    Args:
        text: Opinion text body (may contain HTML).

    Returns:
        A list of dicts, each with ``citation`` (display string),
        ``volume``, ``reporter``, ``page``, and ``span`` (character
        positions in the cleaned text).
    """
    import warnings

    from eyecite import clean_text, get_citations
    from eyecite.models import FullCaseCitation

    cleaned = clean_text(text, ["html", "all_whitespace"])
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore")
        cites = get_citations(cleaned, remove_ambiguous=True)

    results = []
    seen = set()

    for c in cites:
        if isinstance(c, FullCaseCitation):
            volume = c.groups.get("volume", "")
            reporter = c.groups.get("reporter", "")
            page = c.groups.get("page", "")

            # Deduplicate by (volume, reporter, page)
            key = (volume, reporter, page)
            if key in seen:
                continue
            seen.add(key)

            results.append(
                {
                    "citation": c.corrected_citation(),
                    "volume": volume,
                    "reporter": reporter,
                    "page": page,
                    "span": c.span(),
                }
            )

    return results


# Docket numbers that identify a case without a reporter citation.
_DOCKET_PATTERNS: list[re.Pattern] = [
    re.compile(r"^\d{2}-\d{2}-\d{5}-C[VR]$", re.IGNORECASE),     # Tex. App.: 11-23-00222-CV
    re.compile(r"^\d{2}-\d{4}$"),                                # Tex. Sup. Ct.: 23-0676
    re.compile(r"^(?:PD|AP|WR)-\d{4}-\d{2}$", re.IGNORECASE),     # Tex. Crim. App.: PD-0123-24
    re.compile(r"^\d{2}-BC\d{2}[A-Z]?-\d{4}$", re.IGNORECASE),    # Tex. Bus. Ct.: 24-BC03B-0007
    re.compile(r"^\d{2}-\d{5}$"),                                # 5th Cir.: 19-50860
]


def looks_like_docket_number(text: str) -> str | None:
    """Return the docket number if ``text`` is one, else None.

    Accepts an explicit ``docket:`` prefix for any format, and recognizes
    Texas Supreme Court, court of appeals, Court of Criminal Appeals,
    Business Court, and Fifth Circuit docket numbers without it.
    """
    value = text.strip()
    if value.lower().startswith("docket:"):
        value = value.split(":", 1)[1].strip()
        return value or None
    value = re.sub(r"^No\.\s*", "", value, flags=re.IGNORECASE)
    for pattern in _DOCKET_PATTERNS:
        if pattern.match(value):
            return value.upper()
    return None
