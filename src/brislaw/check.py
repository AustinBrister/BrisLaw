"""Check the case citations in a draft against CourtListener.

Reads the draft from disk, finds every case citation locally with eyecite,
and sends only the citation strings (never the draft's text) to
CourtListener's citation-lookup API. For each full citation it checks:

- whether CourtListener has a case at that volume, reporter, and page;
- whether the cited page is the case's first page (the lookup service
  matches a page inside a case to that case, so a cite with the wrong first
  page still comes back as found);
- whether the case name in the draft matches the case at that cite, which
  catches citations that point to a different case entirely;
- whether the year matches, and whether any pin cite falls before the first page.

Short cites ("457 S.W.3d at 58") are checked locally: each needs a full
citation of the same volume and reporter somewhere in the draft.
"""

from __future__ import annotations

import bisect
import contextlib
import io
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# CourtListener checks at most 60 citations a minute.
CHUNK_SIZE = 60
THROTTLE_SECONDS = 61

# Words too common in case names to show that two names match.
_NAME_STOPWORDS = {
    "the", "and", "of", "in", "re", "ex", "parte", "rel", "v", "vs", "estate",
    "matter", "state", "texas", "united", "states", "city", "county",
    "company", "co", "corp", "corporation", "inc", "llc", "ltd", "lp", "llp",
    "limited", "partnership", "pship", "trust", "trustee", "individually",
    "operating", "energy", "oil", "gas", "resources", "petroleum",
    "exploration", "production", "properties", "royalty", "minerals",
    "services", "servs", "holdings", "partners", "group", "america", "north",
    "south", "east", "west", "national", "bank", "association", "assn",
    "insurance", "ins", "et", "al", "nka", "fka", "dba",
}


@dataclass
class CiteUse:
    """One place a citation appears in the draft."""

    line: int
    as_written: str
    pin: str | None = None
    name: str = ""
    year: str = ""


@dataclass
class DraftCitation:
    """One unique full case citation (volume, reporter, first page) in the draft."""

    key: str
    volume: str
    reporter: str
    page: str
    plaintiff: str = ""
    defendant: str = ""
    year: str = ""
    uses: list[CiteUse] = field(default_factory=list)
    outcome: str = "not_checked"  # verified, problem, minor, not_found, unknown_reporter, not_checked
    problems: list[str] = field(default_factory=list)
    minor: list[str] = field(default_factory=list)
    match_name: str = ""
    match_cite: str = ""
    match_date: str = ""
    match_cluster: int | None = None
    match_url: str = ""

    @property
    def draft_name(self) -> str:
        if self.plaintiff and self.defendant:
            return f"{self.plaintiff} v. {self.defendant}"
        return self.plaintiff or self.defendant


def _today() -> str:
    """'September 29, 2026' (strftime's %-d does not work on Windows)."""
    now = datetime.now()
    return f"{now:%B} {now.day}, {now.year}"


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def read_docx(path: Path) -> str:
    """Text of a Word document: one line per body paragraph, then one per footnote.

    Line numbers in the report are then paragraph numbers, counting the body
    first and the footnotes after it. Footnote lines start "[Footnote N]".
    """
    import zipfile
    import xml.etree.ElementTree as ET

    def paragraphs(xml_bytes: bytes) -> list[str]:
        root = ET.fromstring(xml_bytes)
        out = []
        for p in root.iter(f"{_W}p"):
            parts = []
            for node in p.iter():
                if node.tag == f"{_W}t" and node.text:
                    parts.append(node.text)
                elif node.tag == f"{_W}tab":
                    parts.append(" ")
            out.append("".join(parts))
        return out

    with zipfile.ZipFile(path) as z:
        lines = paragraphs(z.read("word/document.xml"))
        if "word/footnotes.xml" in z.namelist():
            root = ET.fromstring(z.read("word/footnotes.xml"))
            for fn in root.iter(f"{_W}footnote"):
                fid = fn.get(f"{_W}id", "")
                if fid.lstrip("-").isdigit() and int(fid) > 0:
                    text = " ".join(t for t in paragraphs(ET.tostring(fn)) if t)
                    lines.append(f"[Footnote {fid}] {text}")
    return "\n".join(lines)


def _clean_markdown(text: str) -> str:
    """Remove markdown emphasis and escapes without changing line breaks."""
    return text.replace("*", "").replace("\\", "").replace("_", " ")


def _line_finder(text: str):
    starts = [0] + [m.end() for m in re.finditer("\n", text)]
    return lambda offset: bisect.bisect_right(starts, offset)


def extract(text: str) -> tuple[dict[str, DraftCitation], list[dict], int]:
    """Find case citations in the draft.

    Returns:
        (full citations keyed by "volume reporter page", short-cite issues,
        count of statute and other non-case citations seen)
    """
    from eyecite import get_citations
    from eyecite.models import FullCaseCitation, ShortCaseCitation, IdCitation, SupraCitation

    cleaned = _clean_markdown(text)
    line_of = _line_finder(cleaned)
    # eyecite prints diagnostics; keep them out of the report and JSON output
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        found = get_citations(cleaned)

    full: dict[str, DraftCitation] = {}
    shorts: list[tuple[ShortCaseCitation, int]] = []
    other = 0
    for c in found:
        start, end = c.span()
        line = line_of(start)
        if isinstance(c, FullCaseCitation):
            g = c.groups
            volume, page = str(g.get("volume", "")), str(g.get("page", ""))
            reporter = c.corrected_reporter() if hasattr(c, "corrected_reporter") else g.get("reporter", "")
            if not (volume.isdigit() and page.isdigit()):
                other += 1
                continue
            key = f"{volume} {reporter} {page}"
            md = c.metadata
            entry = full.setdefault(key, DraftCitation(key=key, volume=volume, reporter=reporter, page=page))
            if not entry.plaintiff and not entry.defendant:
                entry.plaintiff = (md.plaintiff or "").strip()
                entry.defendant = (md.defendant or "").strip()
            if not entry.year and md.year:
                entry.year = str(md.year)
            use_name = " v. ".join(x for x in ((md.plaintiff or "").strip(), (md.defendant or "").strip()) if x)
            use_year = _year_after(cleaned, end) or str(md.year or "")
            if use_year and entry.year != use_year and len(entry.uses) == 0:
                entry.year = use_year
            entry.uses.append(CiteUse(
                line=line, as_written=cleaned[start:end], pin=md.pin_cite,
                name=use_name, year=use_year,
            ))
        elif isinstance(c, ShortCaseCitation):
            shorts.append((c, line))
        elif isinstance(c, (IdCitation, SupraCitation)):
            continue
        else:
            other += 1

    issues: list[dict] = []
    for c, line in shorts:
        g = c.groups
        volume, reporter = str(g.get("volume", "")), (
            c.corrected_reporter() if hasattr(c, "corrected_reporter") else g.get("reporter", "")
        )
        pin = _first_int(c.metadata.pin_cite or g.get("page"))
        written = c.corrected_citation() if hasattr(c, "corrected_citation") else str(c)
        matches = [d for d in full.values() if d.volume == volume and d.reporter == reporter]
        if not matches:
            issues.append({
                "line": line,
                "citation": written,
                "problem": f"Short cite with no full citation to {volume} {reporter} anywhere in the draft.",
            })
            continue
        if pin is not None and all(pin < int(d.page) for d in matches):
            first_pages = ", ".join(sorted({d.page for d in matches}))
            issues.append({
                "line": line,
                "citation": written,
                "problem": f"Pin page {pin} comes before the first page of the case it refers to ({volume} {reporter} {first_pages}).",
            })
    return full, issues, other


def _year_after(text: str, end: int) -> str:
    """Year in the court-and-date parenthetical right after a citation.

    eyecite sometimes takes the year from the next citation in a string cite
    (it read "(Tex. App.--San Antonio 2014, pet. denied); ... (Tex. 2024)" as
    2024), so the year is read from the first parenthetical directly.
    """
    m = re.match(r"[^()\n;]{0,40}?\(([^()]{0,120})\)", text[end:end + 200])
    if not m:
        return ""
    years = re.findall(r"\b(1[89]\d\d|20\d\d)\b", m.group(1))
    return years[-1] if years else ""


def _first_int(value) -> int | None:
    m = re.search(r"\d+", str(value or ""))
    return int(m.group()) if m else None


def _tokens(name: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", name.lower().replace("'", ""))
    return {w for w in words if len(w) >= 3 and w not in _NAME_STOPWORDS}


def names_match(draft_name: str, cluster_names: list[str]) -> bool:
    """True if a distinctive word in the draft's case name appears in CourtListener's.

    Lenient on purpose: abbreviations ("Servs." for "Services") and short
    captions ("Hooks v. Samson") should pass. Only a name sharing no
    distinctive word with the case at that cite is treated as a mismatch.
    """
    draft = _tokens(draft_name)
    if not draft:
        return True
    theirs: set[str] = set()
    for n in cluster_names:
        theirs |= _tokens(n or "")
    for a in draft:
        for b in theirs:
            if a == b or (len(a) >= 4 and len(b) >= 4 and a[:4] == b[:4]):
                return True
    return False


def _norm_reporter(r: str) -> str:
    return re.sub(r"[\s.]", "", r or "").lower()


def _evaluate(cite: DraftCitation, item: dict) -> None:
    status = item.get("status")
    if status == 404:
        cite.outcome = "not_found"
        return
    if status == 400:
        cite.outcome = "unknown_reporter"
        return
    if status not in (200, 300):
        cite.outcome = "not_checked"
        cite.problems.append(item.get("error_message") or f"Lookup returned status {status}.")
        return

    clusters = item.get("clusters") or []
    if not clusters:
        cite.outcome = "not_found"
        return

    def names(cl: dict) -> list[str]:
        return [cl.get("case_name", ""), cl.get("case_name_full", ""), cl.get("case_name_short", "")]

    # With several matches, prefer the one whose name fits the draft
    chosen = next((cl for cl in clusters if names_match(cite.draft_name, names(cl))), clusters[0])
    cite.match_name = chosen.get("case_name", "")
    cite.match_date = chosen.get("date_filed", "") or ""
    cite.match_cluster = chosen.get("id")
    cite.match_url = "https://www.courtlistener.com" + (chosen.get("absolute_url") or "")

    start_page = None
    for c in chosen.get("citations", []) or []:
        if str(c.get("volume")) == cite.volume and _norm_reporter(c.get("reporter", "")) == _norm_reporter(cite.reporter):
            start_page = str(c.get("page"))
            cite.match_cite = f"{c.get('volume')} {c.get('reporter')} {c.get('page')}"
            break
    if not cite.match_cite:
        cite.match_cite = cite.key

    if status == 300 and len(clusters) > 1:
        others = "; ".join(cl.get("case_name", "") for cl in clusters if cl is not chosen)
        cite.minor.append(f"CourtListener has more than one case at this cite (also: {others}).")

    if start_page and start_page != cite.page:
        cite.problems.append(
            f"Wrong first page. Page {cite.page} falls inside {cite.match_name}, "
            f"which starts at {cite.match_cite}."
        )

    # Check the name and year at every place the cite appears, not just the first
    bad_names: dict[str, list[int]] = {}
    for use in cite.uses:
        if use.name and not names_match(use.name, names(chosen)):
            bad_names.setdefault(use.name, []).append(use.line)
    for name, lines in bad_names.items():
        cite.problems.append(
            f"Case name does not match (line {', '.join(map(str, lines))}). The draft says "
            f"{name}; the case at this cite is {cite.match_name}."
        )
    if not any(use.name for use in cite.uses):
        cite.minor.append("No case name found next to this citation, so the name was not checked.")

    year = cite.match_date[:4]
    bad_years: dict[str, list[int]] = {}
    for use in cite.uses:
        if use.year and year and use.year != year:
            bad_years.setdefault(use.year, []).append(use.line)
    for draft_year, lines in bad_years.items():
        cite.minor.append(
            f"Year (line {', '.join(map(str, lines))}): the draft says {draft_year}; "
            f"CourtListener dates the opinion {cite.match_date}."
        )

    first = _first_int(start_page) or _first_int(cite.page)
    for use in cite.uses:
        pin = _first_int(use.pin)
        if pin is not None and first is not None and pin < first:
            cite.problems.append(f"Pin page {pin} (line {use.line}) comes before the first page, {first}.")

    if cite.problems:
        cite.outcome = "problem"
    elif cite.minor:
        cite.outcome = "minor"
    else:
        cite.outcome = "verified"


def lookup(client, cites: dict[str, DraftCitation], status=None) -> None:
    """Check each unique citation with CourtListener, 60 per minute."""
    keys = list(cites)
    chunks = [keys[i:i + CHUNK_SIZE] for i in range(0, len(keys), CHUNK_SIZE)]
    last_start = 0.0
    for n, chunk in enumerate(chunks):
        if n > 0:
            wait = THROTTLE_SECONDS - (time.monotonic() - last_start)
            if wait > 0:
                if status:
                    status(f"Checked {n * CHUNK_SIZE} of {len(keys)}. Waiting {int(wait)}s for "
                           f"CourtListener's limit of 60 citations a minute...")
                time.sleep(wait)
        last_start = time.monotonic()

        payload_parts: list[str] = []
        offsets: dict[int, str] = {}
        pos = 0
        for key in chunk:
            offsets[pos] = key
            payload_parts.append(key)
            pos += len(key) + 2  # "; "
        items = client.citation_lookup("; ".join(payload_parts))

        by_norm = {re.sub(r"\s+", " ", k).lower(): k for k in chunk}
        for item in items:
            key = offsets.get(item.get("start_index"))
            if key is None:
                for norm in item.get("normalized_citations") or [item.get("citation", "")]:
                    key = by_norm.get(re.sub(r"\s+", " ", norm).lower())
                    if key:
                        break
            if key is None or key not in cites:
                continue
            _evaluate(cites[key], item)


def find_missing_by_name(client, cites: dict[str, DraftCitation]) -> None:
    """For cites CourtListener lacks, look for the case itself by name.

    CourtListener often has a recent case but not its S.W.3d cite. Finding
    the case by name separates "real case, cite not in CourtListener" from
    "no such case," and catches a cite whose volume or page is wrong when
    CourtListener has the case at a different cite.
    """
    from brislaw.api import ALL_STATUSES
    from brislaw.citing import name_query

    for cite in cites.values():
        if cite.outcome != "not_found" or not cite.draft_name:
            continue
        q = name_query(cite.draft_name)
        if not q:
            continue
        year = _first_int(cite.year)
        try:
            response = client.search_opinions(
                f"caseName:({q})",
                courts=None,
                highlight=False,
                filed_after=f"{year - 1}-01-01" if year else None,
                filed_before=f"{year + 1}-12-31" if year else None,
                extra_params=ALL_STATUSES,
            )
        except Exception:
            continue
        hit = next(
            (r for r in response.get("results", [])[:5] if names_match(cite.draft_name, [r.get("caseName", "")])),
            None,
        )
        if hit is None:
            continue
        cite.match_name = hit.get("caseName", "")
        cite.match_date = hit.get("dateFiled", "") or ""
        cite.match_cluster = hit.get("cluster_id")
        cite.match_url = "https://www.courtlistener.com" + (hit.get("absolute_url") or "")
        where = f"{hit.get('court_id', '')}, {cite.match_date}"
        if hit.get("docketNumber"):
            where += f", No. {hit['docketNumber']}"
        same_reporter = [
            c for c in hit.get("citation", []) or []
            if _norm_reporter(c.split(" ", 1)[1].rsplit(" ", 1)[0] if " " in c else "") == _norm_reporter(cite.reporter)
        ]
        if same_reporter:
            cite.outcome = "problem"
            cite.problems.append(
                f"No case at {cite.key}, but CourtListener has {cite.match_name} ({where}) "
                f"at {'; '.join(same_reporter)}. Check the volume and page."
            )
        else:
            cite.outcome = "case_found"
            listed = "; ".join(hit.get("citation", []) or []) or "no reporter cite"
            cite.minor.append(
                f"CourtListener has this case ({cite.match_name}, {where}) but not this cite "
                f"(it lists: {listed}). The case is real; check the cite on Lexis or Westlaw."
            )


def _lines(cite: DraftCitation) -> str:
    return ", ".join(str(n) for n in dict.fromkeys(u.line for u in cite.uses))


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def render_markdown(source: Path, cites: dict[str, DraftCitation], short_issues: list[dict], other: int) -> str:
    """Markdown report, problems first."""
    all_cites = sorted(cites.values(), key=lambda c: c.uses[0].line if c.uses else 0)
    problems = [c for c in all_cites if c.outcome == "problem"]
    not_found = [c for c in all_cites if c.outcome in ("not_found", "unknown_reporter", "not_checked", "case_found")]
    minor = [c for c in all_cites if c.outcome == "minor"]
    verified = [c for c in all_cites if c.outcome == "verified"]
    uses = sum(len(c.uses) for c in all_cites)

    def n(count: int, one: str, many: str) -> str:
        return f"{count} {one if count == 1 else many}"

    out: list[str] = [f"# Citation check: {source.name}", ""]
    if source.suffix.lower() == ".docx":
        out.append(
            "Line numbers are paragraph numbers in the Word document, counting the body "
            "first and then the footnotes."
        )
        out.append("")
    out.append(
        f"Checked {_today()} against CourtListener. The draft has "
        f"{n(uses, 'full case citation', 'full case citations')} to {n(len(all_cites), 'case', 'cases')}. "
        f"Problems: {len(problems)}. Not in CourtListener: {len(not_found)}. Minor notes: {len(minor)}. "
        f"Checked out: {len(verified)}. Short cites needing attention: {len(short_issues)}."
    )
    if other:
        out.append("")
        out.append(
            f"The draft also has {n(other, 'statute, rule, or other citation', 'statute, rule, or other citations')}. "
            f"This check does not cover them."
        )
    out.append("")

    out.append(f"## Problems ({len(problems)})")
    out.append("")
    if problems:
        out.append("| Line | Citation in the draft | Problem |")
        out.append("|------|-----------------------|---------|")
        for c in problems:
            written = f"{c.draft_name}, {c.key}" if c.draft_name else c.key
            out.append(f"| {_lines(c)} | {_cell(written)} | {_cell(' '.join(c.problems + c.minor))} |")
    else:
        out.append("None.")
    out.append("")

    out.append(f"## Not in CourtListener ({len(not_found)})")
    out.append("")
    if not_found:
        out.append(
            "CourtListener has no case at these cites. That does not make them wrong: CourtListener "
            "lacks the reporter cites for many recent cases. Check each on Lexis or Westlaw."
        )
        out.append("")
        out.append("| Line | Citation in the draft | Result |")
        out.append("|------|-----------------------|--------|")
        for c in not_found:
            written = f"{c.draft_name}, {c.key}" if c.draft_name else c.key
            result = {
                "not_found": "No case at this cite, and no case by that name nearby in time",
                "unknown_reporter": "CourtListener does not know this reporter",
                "case_found": " ".join(c.minor),
            }.get(c.outcome, " ".join(c.problems) or "Not checked")
            out.append(f"| {_lines(c)} | {_cell(written)} | {_cell(result)} |")
    else:
        out.append("None.")
    out.append("")

    out.append(f"## Short cites ({len(short_issues)})")
    out.append("")
    if short_issues:
        out.append("| Line | Short cite | Problem |")
        out.append("|------|------------|---------|")
        for s in short_issues:
            out.append(f"| {s['line']} | {_cell(s['citation'])} | {_cell(s['problem'])} |")
    else:
        out.append("None.")
    out.append("")

    out.append(f"## Minor notes ({len(minor)})")
    out.append("")
    if minor:
        out.append("| Line | Citation | Case in CourtListener | Note |")
        out.append("|------|----------|-----------------------|------|")
        for c in minor:
            out.append(f"| {_lines(c)} | {_cell(c.key)} | {_cell(c.match_name)} | {_cell(' '.join(c.minor))} |")
    else:
        out.append("None.")
    out.append("")

    out.append(f"## Checked out ({len(verified)})")
    out.append("")
    if verified:
        out.append("| Line | Citation | Case in CourtListener | Date |")
        out.append("|------|----------|-----------------------|------|")
        for c in verified:
            out.append(f"| {_lines(c)} | {_cell(c.key)} | {_cell(c.match_name)} | {c.match_date} |")
    else:
        out.append("None.")
    out.append("")

    out.append("## What this check covers")
    out.append("")
    out.append(
        "\"Checked out\" means CourtListener has a case that starts on the cited page and "
        "shares a distinctive word with the case name in the draft. The check does not "
        "verify pin pages beyond confirming they come after the first page, quotations, "
        "parentheticals, or subsequent history. Only citation strings were sent to "
        "CourtListener, not the text of the draft."
    )
    out.append("")
    return "\n".join(out)


def to_dict(source: Path, cites: dict[str, DraftCitation], short_issues: list[dict], other: int) -> dict:
    return {
        "file": str(source),
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "counts": {
            "cases": len(cites),
            "full_citations": sum(len(c.uses) for c in cites.values()),
            "problems": sum(c.outcome == "problem" for c in cites.values()),
            "not_found": sum(c.outcome in ("not_found", "unknown_reporter", "not_checked", "case_found") for c in cites.values()),
            "case_found_cite_missing": sum(c.outcome == "case_found" for c in cites.values()),
            "minor": sum(c.outcome == "minor" for c in cites.values()),
            "verified": sum(c.outcome == "verified" for c in cites.values()),
            "short_cite_issues": len(short_issues),
            "other_citations_not_checked": other,
        },
        "citations": [
            {
                "citation": c.key,
                "draft_case_name": c.draft_name,
                "draft_year": c.year,
                "lines": [u.line for u in c.uses],
                "pins": [u.pin for u in c.uses if u.pin],
                "outcome": c.outcome,
                "problems": c.problems,
                "minor": c.minor,
                "match": {
                    "case_name": c.match_name,
                    "citation": c.match_cite,
                    "date_filed": c.match_date,
                    "cluster_id": c.match_cluster,
                    "url": c.match_url,
                } if c.match_cluster else None,
            }
            for c in sorted(cites.values(), key=lambda c: c.uses[0].line if c.uses else 0)
        ],
        "short_cite_issues": short_issues,
    }
