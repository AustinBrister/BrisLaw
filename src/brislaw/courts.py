"""Texas court constants and court ID utilities for CourtListener."""

from __future__ import annotations

import re

# Texas state courts: court_id -> citation string
TEXAS_STATE_COURTS: dict[str, str] = {
    "tex": "Tex.",
    "texcrimapp": "Tex. Crim. App.",
    "texapp": "Tex. App.",
    "txctapp1": "Tex. App.-Houston [1st Dist.]",
    "txctapp2": "Tex. App.-Fort Worth",
    "txctapp3": "Tex. App.-Austin",
    "txctapp4": "Tex. App.-San Antonio",
    "txctapp5": "Tex. App.-Dallas",
    "txctapp6": "Tex. App.-Texarkana",
    "txctapp7": "Tex. App.-Amarillo",
    "txctapp8": "Tex. App.-El Paso",
    "txctapp9": "Tex. App.-Beaumont",
    "txctapp10": "Tex. App.-Waco",
    "txctapp11": "Tex. App.-Eastland",
    "txctapp12": "Tex. App.-Tyler",
    "txctapp13": "Tex. App.-Edinburg-Corpus Christi",
    "txctapp14": "Tex. App.-Houston [14th Dist.]",
    "txctapp15": "Tex. App.-Houston [15th Dist.]",
    "texbizct": "Tex. Bus. Ct.",
}

# Texas federal courts: court_id -> citation string
TEXAS_FEDERAL_COURTS: dict[str, str] = {
    "ca5": "5th Cir.",
    "txed": "E.D. Tex.",
    "txnd": "N.D. Tex.",
    "txsd": "S.D. Tex.",
    "txwd": "W.D. Tex.",
}

# Texas bankruptcy courts: court_id -> citation string. Not in the default
# filter (they add a lot of noise to ordinary searches); reach them with
# --court bankruptcy or by court ID.
TEXAS_BANKRUPTCY_COURTS: dict[str, str] = {
    "txsb": "Bankr. S.D. Tex.",
    "txnb": "Bankr. N.D. Tex.",
    "txwb": "Bankr. W.D. Tex.",
    "txeb": "Bankr. E.D. Tex.",
}

# All Texas-relevant court IDs (state + federal)
DEFAULT_COURTS: list[str] = list(TEXAS_STATE_COURTS.keys()) + list(
    TEXAS_FEDERAL_COURTS.keys()
)

# Space-joined string for API parameter
DEFAULT_COURT_FILTER: str = " ".join(DEFAULT_COURTS)

# All courts combined for lookups
_ALL_COURTS: dict[str, str] = {
    **TEXAS_STATE_COURTS, **TEXAS_FEDERAL_COURTS, **TEXAS_BANKRUPTCY_COURTS,
}

# Aliases for --court flag convenience
COURT_ALIASES: dict[str, list[str]] = {
    "state": list(TEXAS_STATE_COURTS.keys()),
    "federal": list(TEXAS_FEDERAL_COURTS.keys()),
    "txsc": ["tex"],
    "cca": ["texcrimapp"],
    "5thcir": ["ca5"],
    "bizct": ["texbizct"],
    "business": ["texbizct"],
    "bankruptcy": list(TEXAS_BANKRUPTCY_COURTS.keys()),
    "all": [],  # Empty list signals "no filter"
}


def get_court_display_name(court_id: str) -> str:
    """Return the abbreviated legal citation string for a court ID.

    Examples:
        >>> get_court_display_name("tex")
        'Tex.'
        >>> get_court_display_name("txctapp14")
        'Tex. App.-Houston [14th Dist.]'
        >>> get_court_display_name("ca5")
        '5th Cir.'
    """
    return _ALL_COURTS.get(court_id, court_id)


def resolve_court_filter(court_arg: str | None) -> str | None:
    """Resolve a --court flag value to a space-separated court ID string for the API.

    Args:
        court_arg: The value from the --court flag, or None for defaults.

    Returns:
        Space-separated court ID string for the API court parameter,
        or None if "all" is specified (meaning no court filter).

    Examples:
        >>> resolve_court_filter(None)  # Returns DEFAULT_COURT_FILTER
        >>> resolve_court_filter("txsc")  # Returns "tex"
        >>> resolve_court_filter("federal")  # Returns "ca5 txed txnd txsd txwd"
        >>> resolve_court_filter("tex ca5")  # Passthrough
        >>> resolve_court_filter("txsc bizct")  # Returns "tex texbizct"
        >>> resolve_court_filter("all")  # Returns None (no filter)
    """
    if court_arg is None:
        return DEFAULT_COURT_FILTER

    court_arg = court_arg.strip().lower()

    # "all" alone means no filter
    if court_arg == "all":
        return None

    # Accept several aliases or court IDs at once: "txsc bizct", "tex,ca5"
    tokens = [t for t in re.split(r"[\s,]+", court_arg) if t]
    ids: list[str] = []
    unknown: list[str] = []
    for token in tokens:
        if token in COURT_ALIASES and COURT_ALIASES[token]:
            ids.extend(COURT_ALIASES[token])
        else:
            if token not in _ALL_COURTS:
                unknown.append(token)
            ids.append(token)

    # Warn about unrecognized tokens before passing them through
    if unknown:
        import sys

        print(
            f"Warning: {', '.join(repr(t) for t in unknown)} is not a recognized Texas "
            f"court alias or ID; passing it to CourtListener as a court ID. "
            f"Use '--court all' for non-Texas jurisdictions. "
            f"Known aliases: {', '.join(sorted(COURT_ALIASES.keys()))}",
            file=sys.stderr,
        )
    return " ".join(dict.fromkeys(ids))


def court_ids_for_filter(court_arg: str | None) -> set[str] | None:
    """Return the set of court IDs a --court value selects, or None for all courts.

    Used to filter results locally (for example, citing cases found through
    the citation table rather than a court-filtered search).
    """
    resolved = resolve_court_filter(court_arg)
    if resolved is None:
        return None
    return set(resolved.split())
