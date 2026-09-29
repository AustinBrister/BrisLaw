"""Pin cites other courts have used for a case.

CourtListener has no Westlaw star pages ("2024 WL 123456, at *3") and no
S.W.3d page breaks for most cases decided after about 2018. Other courts'
opinions, which CourtListener does have, pin-cite those pages. This module
reads the opinions that cite a case and collects every pin cite they give
it, with the text around each, so a proposition can be matched to a page
when another court cited the same passage.

The text around each pin is the citing court's own words. It shows which
page another court used; it is not the cited case's text.
"""

from __future__ import annotations

import re

import httpx

from brislaw.display import strip_html

# How much text to keep before and after each pin, in characters.
CONTEXT_BEFORE = 320
CONTEXT_AFTER = 60

_PIN = r"\*?\d+(?:\s*[-–—]\s*\*?\d+)?(?:\s*(?:,|&|and)\s*\*?\d+(?:\s*[-–—]\s*\*?\d+)?)*(?:\s*(?:&\s*)?nn?\.\s*\d+(?:\s*[-–]\s*\d+)?)?"


def _reporter_pattern(reporter: str) -> str:
    """Regex for a reporter abbreviation that tolerates spacing: 'S.W.3d' -> 'S\\.\\s?W\\.\\s?3d'."""
    parts = re.findall(r"[A-Za-z0-9]+\.?|\S", reporter)
    return r"\s?".join(re.escape(p) for p in parts)


def build_patterns(cites: list[tuple[str, str, str]], wl_cites: list[str]) -> list[tuple[str, re.Pattern]]:
    """Regexes that capture pin cites to the case.

    Args:
        cites: (volume, reporter, page) reporter citations for the case.
        wl_cites: Westlaw or Lexis cites such as "2024 WL 345678".

    Returns:
        (label, pattern) pairs. Each pattern's ``pin`` group is the pin.
    """
    patterns: list[tuple[str, re.Pattern]] = []
    for volume, reporter, page in cites:
        rep = _reporter_pattern(reporter)
        label = f"{volume} {reporter}"
        # Full cite with pin: "457 S.W.3d 52, 57"
        # The lookahead keeps a parallel cite ("457 S.W.3d 52, 58 Tex. Sup. Ct. J.")
        # from being read as a pin.
        patterns.append((label, re.compile(
            rf"\b{volume}\s+{rep}\s+{page}\s*,\s*(?P<pin>{_PIN})(?!\d)(?!\s+[A-Z][A-Za-z]*\.)")))
        # Short cite: "457 S.W.3d at 57"
        patterns.append((label, re.compile(rf"\b{volume}\s+{rep}\s+at\s+(?P<pin>{_PIN})")))
    for wl in wl_cites:
        year, db, number = _split_wl(wl)
        if not number:
            continue
        db_pat = _reporter_pattern(db)
        patterns.append((f"{year} {db} {number}", re.compile(
            rf"\b{year}\s+{db_pat}\s+{number}\s*,?\s*at\s+(?P<pin>{_PIN})")))
    return patterns


def _split_wl(cite: str) -> tuple[str, str, str]:
    """'2024 WL 345678' -> ('2024', 'WL', '345678'); 'Tex. App. LEXIS' works too."""
    m = re.match(r"^(\d{4})\s+(.+?)\s+(\d+)$", cite.strip())
    return (m.group(1), m.group(2), m.group(3)) if m else ("", "", "")


def find_wl_by_docket(text: str, docket_number: str) -> list[str]:
    """WL or LEXIS cites that appear next to the case's docket number in a citing opinion.

    Unreported cases are cited "No. 01-21-00331-CV, 2024 WL 345678, at *3";
    this recovers the WL number when CourtListener does not list it.
    """
    if not docket_number:
        return []
    found = []
    pat = re.compile(
        rf"{re.escape(docket_number)}\s*,?\s*(?P<wl>(?:19|20)\d\d\s+(?:WL|Tex\.\s?App\.\s?LEXIS|Tex\.\s?LEXIS|U\.S\.\s?Dist\.\s?LEXIS|U\.S\.\s?App\.\s?LEXIS)\s+\d+)",
        re.IGNORECASE,
    )
    for m in pat.finditer(text):
        found.append(" ".join(m.group("wl").split()))
    return found


def plain_text(html_or_text: str) -> str:
    """Opinion text with tags stripped and whitespace collapsed."""
    text = strip_html(html_or_text) if "<" in html_or_text else html_or_text
    return " ".join(text.split())


def extract_pins(text: str, patterns: list[tuple[str, re.Pattern]]) -> list[dict]:
    """Every pin cite in ``text`` that matches the patterns, with context."""
    hits: list[dict] = []
    seen: set[int] = set()
    for label, pattern in patterns:
        for m in pattern.finditer(text):
            if m.start() in seen:
                continue
            seen.add(m.start())
            pin = " ".join(m.group("pin").split()).rstrip(",")
            before = text[max(0, m.start() - CONTEXT_BEFORE):m.start()]
            # Start the context at a sentence boundary when one is close
            cut = max(before.rfind(". "), before.rfind("; "))
            if 0 <= cut < CONTEXT_BEFORE - 80:
                before = before[cut + 2:]
            after = text[m.end():m.end() + CONTEXT_AFTER]
            hits.append({
                "cite": label,
                "pin": pin,
                "as_written": text[m.start():m.end()],
                "context": f"...{before}{text[m.start():m.end()]}{after}...",
            })
    return hits


def page_sort_key(pin: str) -> tuple[int, str]:
    m = re.search(r"\d+", pin)
    return (int(m.group()) if m else 10**9, pin)


def read_opinion_text(client, opinion_id: int) -> str:
    try:
        return plain_text(client.get_opinion_text(opinion_id))
    except httpx.HTTPError:
        return ""
