"""Citing cases from CourtListener's citation table.

The older approach searched for ``cites:<opinion_id>``. That operator reads
the search index, which can miss most citing opinions: for Hooks v. Samson,
457 S.W.3d 52, it returned 24 where the citation table lists 121 (the same
number CourtListener shows as the case's citation count). This module reads
the citation table directly and then looks up each citing opinion's case
name, court, and date.
"""

from __future__ import annotations

import re

import httpx

from brislaw.models import (
    is_main_opinion_type,
    opinion_type_label,
    preferred_citations,
)

# Citing opinions missing from the search index are looked up one at a time
# (three API calls each). Past this many, they are reported as unresolved.
FALLBACK_LIMIT = 30


def collect_citing(
    client, target_opinion_ids: list[int], max_scan: int
) -> tuple[dict[int, int], bool]:
    """Map citing opinion ID -> total depth across all target opinions.

    Args:
        client: An active CourtListenerClient.
        target_opinion_ids: Every opinion of the cited case (majority,
            concurrences, and any separate records on the same docket).
        max_scan: Stop after this many citation-table rows.

    Returns:
        (depths, truncated) where truncated is True if max_scan was hit.
    """
    targets = set(target_opinion_ids)
    depths: dict[int, int] = {}
    scanned = 0
    for opinion_id in target_opinion_ids:
        for citing_id, depth in client.iter_citing(opinion_id):
            scanned += 1
            if citing_id not in targets:
                depths[citing_id] = depths.get(citing_id, 0) + depth
            if scanned >= max_scan:
                return depths, True
    return depths, False


def _fallback_result(client, opinion_id: int) -> dict | None:
    """Build a search-shaped result for an opinion the search index lacks."""
    try:
        op = client._request(
            "GET", f"/opinions/{opinion_id}/", params={"fields": "id,cluster_id,type"}
        )
        cluster = client._request(
            "GET",
            f"/clusters/{op['cluster_id']}/",
            params={
                "fields": "id,case_name,date_filed,citations,precedential_status,"
                          "citation_count,docket_id",
            },
        )
        docket = client.get_docket(int(cluster["docket_id"])) if cluster.get("docket_id") else {}
    except (httpx.HTTPError, KeyError, ValueError):
        return None
    cite, others = preferred_citations(cluster.get("citations", []))
    return {
        "caseName": cluster.get("case_name", ""),
        "citation": [c for c in [cite, *others] if c],
        "court_id": docket.get("court_id", ""),
        "dateFiled": cluster.get("date_filed", ""),
        "cluster_id": cluster.get("id"),
        "status": cluster.get("precedential_status", ""),
        "citeCount": cluster.get("citation_count", 0),
        "docketNumber": docket.get("docket_number", ""),
        "opinions": [{"id": opinion_id, "type": op.get("type", "")}],
        "snippet": "",
    }


def describe_citing(
    client, depths: dict[int, int], exclude_clusters: set[int]
) -> tuple[list[dict], list[int]]:
    """Look up the citing opinions and group them by case.

    Returns:
        (entries, unresolved_opinion_ids). Each entry has ``result`` (a
        search-API-shaped dict), ``depth`` (total times the case cites the
        target), and ``citing_types`` (which opinions in that case cite it).
    """
    ids = list(depths)
    by_opinion: dict[int, dict] = {}
    for result in client.search_by_opinion_ids(ids):
        for op in result.get("opinions", []) or []:
            oid = op.get("id")
            if oid in depths:
                by_opinion[oid] = result

    missing = [oid for oid in ids if oid not in by_opinion]
    for oid in missing[:FALLBACK_LIMIT]:
        result = _fallback_result(client, oid)
        if result is not None:
            by_opinion[oid] = result
    unresolved = [oid for oid in ids if oid not in by_opinion]

    grouped: dict[int, dict] = {}
    for oid, result in by_opinion.items():
        cluster_id = int(result.get("cluster_id") or 0)
        if cluster_id in exclude_clusters:
            continue
        entry = grouped.setdefault(
            cluster_id, {"result": result, "depth": 0, "citing_types": [], "opinion_ids": []}
        )
        entry["depth"] += depths[oid]
        entry["opinion_ids"].append(oid)
        raw_type = next(
            (op.get("type", "") for op in result.get("opinions", []) if op.get("id") == oid), ""
        )
        entry["citing_types"].append(raw_type)
    return list(grouped.values()), unresolved


def citing_note(depth: int, citing_types: list[str]) -> str:
    """'Cites it 25 times.' plus a flag when only a separate writing cites it."""
    note = f"Cites it {depth} time{'' if depth == 1 else 's'}."
    known = [t for t in citing_types if t]
    if known and len(known) == len(citing_types) and not any(
        is_main_opinion_type(t) for t in known
    ):
        labels = ", ".join(dict.fromkeys(opinion_type_label(t).lower() for t in known))
        note += f" Only the {labels} cites it."
    return note


# Citation types that CourtListener links reliably: federal, state, and
# regional reporters, and early Supreme Court reporters.
_REPORTER_TYPES = {1, 2, 3, 5}

_ENTITY_WORDS = {
    "llc", "l.l.c.", "inc", "inc.", "co", "co.", "corp", "corp.", "ltd", "ltd.",
    "lp", "l.p.", "llp", "l.l.p.", "company", "corporation", "limited",
    "partnership", "n/k/a", "f/k/a", "et", "al", "al.", "the",
}
_GENERIC_PARTIES = {"state", "texas", "united", "states", "city", "county", "people"}


def has_reporter_cite(cluster: dict) -> bool:
    """True if the cluster has a reporter citation the citation table can link to."""
    return any(c.get("type") in _REPORTER_TYPES for c in cluster.get("citations", []) or [])


def _party_phrase(party: str) -> str | None:
    """First two meaningful words of a party name: 'Cactus Water Services, LLC' -> 'Cactus Water'."""
    party = re.sub(r"^(in re|ex parte|in the matter of|in the estate of)\s+", "", party.strip(), flags=re.IGNORECASE)
    party = party.split(",")[0]
    words = [w.strip(".\"'()") for w in party.split()]
    words = [w for w in words if w and w.lower() not in _ENTITY_WORDS | {"of", "and", "a", "an"}]
    if not words or all(w.lower() in _GENERIC_PARTIES for w in words[:2]):
        return None
    return " ".join(words[:2])


def name_query(case_name: str) -> str | None:
    """Keyword query that finds opinions mentioning a case by its party names.

    Used for cases with no reporter citation yet, which the citation table
    cannot link. 'Cactus Water Services, LLC v. Cog Operating, LLC' becomes
    '"Cactus Water" AND "Cog Operating"'.
    """
    sides = re.split(r"\s+v\.?\s+", case_name, maxsplit=1, flags=re.IGNORECASE)
    phrases = [p for p in (_party_phrase(side) for side in sides) if p]
    if not phrases:
        return None
    return " AND ".join(f'"{p}"' for p in phrases)


def case_opinion_ids(client, cluster: dict, docket_records_fn) -> tuple[list[int], set[int]]:
    """Every opinion of a case, including separate records on the same docket.

    Returns (opinion_ids, clusters_on_the_docket). Short records (judgments)
    are left out of the opinion IDs but included in the cluster set, so the
    case's own records can be excluded from its citing cases.
    """
    from brislaw.api import extract_opinion_id_from_url

    cluster_id = int(cluster.get("id") or 0)
    ids = [extract_opinion_id_from_url(u) for u in cluster.get("sub_opinions", [])]
    clusters = {cluster_id}
    docket_id = cluster.get("docket_id")
    if docket_id:
        records = docket_records_fn(client, int(docket_id))
        clusters |= {r["cluster_id"] for r in records}
        for r in records:
            if r["cluster_id"] != cluster_id and not r["short"]:
                ids.extend(r["opinion_ids"])
    return list(dict.fromkeys(ids)), clusters
