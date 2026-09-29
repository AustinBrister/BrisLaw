"""Data models for CourtListener API responses."""

from __future__ import annotations

import re

from dataclasses import asdict, dataclass, field


# Display labels for opinion type codes
TYPE_LABELS = {
    "combined": "Combined Opinion",
    "majority": "Majority Opinion",
    "concurrence": "Concurring Opinion",
    "dissent": "Dissenting Opinion",
    "addendum": "Addendum",
    "remittitur": "Remittitur",
    "rehearing": "Rehearing",
    "on-motion": "On Motion",
    "original-proceedings": "Original Proceedings",
}


# Opinion type labels, keyed by fragments that appear in both the REST codes
# ("020lead", "035concurrenceinpart") and the search codes ("lead-opinion",
# "concur-in-part"). Order matters: more specific fragments first.
_TYPE_FRAGMENTS: list[tuple[str, str]] = [
    ("combined", "Combined Opinion"),
    ("unanimous", "Unanimous Opinion"),
    ("unamimous", "Unanimous Opinion"),  # CourtListener's own spelling of the code
    ("lead", "Majority Opinion"),
    ("plurality", "Plurality Opinion"),
    ("concurrenceinpart", "Concurring in Part"),
    ("concur-in-part", "Concurring in Part"),
    ("concurrence", "Concurring Opinion"),
    ("dissent", "Dissenting Opinion"),
    ("addendum", "Addendum"),
    ("remittitur", "Remittitur"),
    ("rehearing", "Opinion on Rehearing"),
    ("onthemerits", "On the Merits"),
    ("on-the-merits", "On the Merits"),
    ("onmotiontostrike", "On Motion to Strike"),
    ("on-motion-to-strike", "On Motion to Strike"),
    ("trialcourt", "Trial Court Document"),
    ("trial-court", "Trial Court Document"),
]

# Types that carry the court's decision. A cluster with none of these holds
# only a separate writing (concurrence, dissent) or an ancillary document.
_MAIN_FRAGMENTS = ("combined", "unanimous", "unamimous", "lead", "plurality",
                   "rehearing", "onthemerits", "on-the-merits")


def opinion_type_label(raw_type: str) -> str:
    """Readable label for any CourtListener opinion type code."""
    raw = (raw_type or "").lower()
    for fragment, label in _TYPE_FRAGMENTS:
        if fragment in raw:
            return label
    return raw_type or "Opinion"


def is_main_opinion_type(raw_type: str) -> bool:
    """True if the opinion type carries the court's decision."""
    raw = (raw_type or "").lower()
    if "concur" in raw or "dissent" in raw:
        return False
    return any(fragment in raw for fragment in _MAIN_FRAGMENTS)


# Citation types on cluster records, most preferred first: federal reporter,
# regional reporter (S.W.), state reporter, early SCOTUS, neutral, specialty
# (Tex. Sup. Ct. J.), journal, WL, LEXIS.
_CITATION_TYPE_RANK = {1: 0, 3: 1, 2: 2, 5: 3, 8: 4, 4: 5, 9: 6, 7: 7, 6: 8}


def _citation_string(c: dict) -> str:
    return f"{c.get('volume', '')} {c.get('reporter', '')} {c.get('page', '')}".strip()


def preferred_citations(citations: list[dict]) -> tuple[str, list[str]]:
    """Split a cluster's citation records into (preferred cite, other cites)."""
    if not citations:
        return "", []
    ranked = sorted(citations, key=lambda c: _CITATION_TYPE_RANK.get(c.get("type"), 9))
    strings = [_citation_string(c) for c in ranked]
    return strings[0], strings[1:]


def _string_cite_rank(cite: str) -> int:
    if " WL " in cite or "LEXIS" in cite:
        return 2
    if "Sup. Ct. J." in cite:
        return 1
    return 0


def preferred_citation_string(citations: list[str]) -> str:
    """Pick the reporter cite from a search result's citation strings."""
    if not citations:
        return ""
    return sorted(citations, key=_string_cite_rank)[0]


@dataclass
class Court:
    """A court in the CourtListener system."""

    id: str
    name: str
    citation_string: str
    short_name: str


@dataclass
class SearchResult:
    """A single result from a CourtListener opinion search."""

    case_name: str
    citation: str
    court: str
    court_id: str
    date_filed: str
    snippet: str
    cluster_id: int
    status: str
    cite_count: int
    docket_number: str
    opinions: list[dict] = field(default_factory=list)
    # Labels for each opinion in the cluster ("Majority Opinion", ...)
    opinion_types: list[str] = field(default_factory=list)
    # Citing workflow only: times the citing opinion cites the target
    depth: int = 0
    # Short explanation shown under the result (same-docket records, depth)
    note: str = ""

    @classmethod
    def from_api_search(cls, data: dict) -> SearchResult:
        """Create a SearchResult from a CourtListener search API response item.

        The search API uses camelCase field names which we map to snake_case.
        """
        # The search API returns citations as a list; join them for display
        citations = data.get("citation", [])
        citation_str = preferred_citation_string(citations)
        opinions = data.get("opinions", []) or []

        return cls(
            # Search highlighting can wrap matched words in the case name
            case_name=re.sub(r"</?mark>", "", data.get("caseName", "") or ""),
            citation=citation_str,
            court=data.get("court_citation_string", data.get("court", "")),
            court_id=data.get("court_id", ""),
            date_filed=data.get("dateFiled", ""),
            snippet=data.get("snippet", ""),
            cluster_id=int(data.get("cluster_id", 0)),
            status=data.get("status", ""),
            cite_count=int(data.get("citeCount", 0)),
            docket_number=data.get("docketNumber", ""),
            opinions=opinions,
            opinion_types=[
                opinion_type_label(o.get("type", "")) for o in opinions if isinstance(o, dict)
            ],
        )

    def to_dict(self) -> dict:
        """Return a JSON-serializable dict with all fields.

        Uses snake_case keys matching field names.
        """
        return asdict(self)


@dataclass
class OpinionDetail:
    """Full opinion detail from CourtListener API."""

    id: int
    cluster_id: int
    case_name: str
    citation: str
    court: str
    court_id: str
    date_filed: str
    docket_number: str
    status: str
    text: str
    opinion_type: str
    download_url: str
    # Expanded metadata fields (Phase 2)
    judges: str = ""
    attorneys: str = ""
    case_name_full: str = ""
    syllabus: str = ""
    headnotes: str = ""
    nature_of_suit: str = ""
    disposition: str = ""
    cite_count: int = 0
    sub_opinions_data: list[dict] = field(default_factory=list)
    # Parallel cites (Tex. Sup. Ct. J., WL, LEXIS) besides ``citation``
    other_citations: list[str] = field(default_factory=list)
    # One entry per opinion in the cluster: "Concurring Opinion (Busby)"
    opinions_summary: list[str] = field(default_factory=list)
    # Other clusters filed under the same docket (see brislaw.related)
    related_records: list[dict] = field(default_factory=list)
    # Plain-language warnings about what was fetched
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_api_response(
        cls, opinion: dict, cluster: dict
    ) -> OpinionDetail:
        """Create an OpinionDetail from opinion + cluster API responses.

        Requires both the opinion endpoint data and the parent cluster data
        since opinion text lives on the opinion but metadata lives on the cluster.
        """
        # Extract citations from cluster
        citation_str, other_citations = preferred_citations(cluster.get("citations", []))

        # Map opinion type from API string
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
        raw_type = opinion.get("type", "")
        opinion_type = type_map.get(raw_type, raw_type)

        # Extract text: priority order per research
        text = ""
        for text_field in (
            "html_with_citations",
            "html",
            "plain_text",
            "html_lawbox",
            "html_columbia",
            "html_anon_2020",
        ):
            content = opinion.get(text_field, "")
            if content:
                text = content
                break

        return cls(
            id=opinion.get("id", 0),
            cluster_id=cluster.get("id", 0),
            case_name=cluster.get("case_name", ""),
            citation=citation_str.strip(),
            court=cluster.get("court_citation_string", ""),
            court_id=cluster.get("court_id", ""),
            date_filed=cluster.get("date_filed", ""),
            docket_number=cluster.get("docket_number", ""),
            status=cluster.get("precedential_status", ""),
            text=text,
            opinion_type=opinion_type,
            download_url=opinion.get("download_url", ""),
            # Expanded metadata from cluster
            judges=cluster.get("judges", "") or "",
            attorneys=cluster.get("attorneys", "") or "",
            case_name_full=cluster.get("case_name_full", "") or "",
            syllabus=cluster.get("syllabus", "") or "",
            headnotes=cluster.get("headnotes", "") or "",
            nature_of_suit=cluster.get("nature_of_suit", "") or "",
            disposition=cluster.get("disposition", "") or "",
            cite_count=int(cluster.get("citation_count", 0) or 0),
            sub_opinions_data=cluster.get("sub_opinions", []) or [],
            other_citations=other_citations,
        )

    def to_dict(self) -> dict:
        """Return a JSON-serializable dict with all fields.

        Uses snake_case keys matching field names. All values are
        JSON-serializable (no date objects, no non-string keys).
        """
        return asdict(self)
