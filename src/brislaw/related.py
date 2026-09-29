"""Other CourtListener records filed under the same docket.

Texas courts post each separately filed document as its own PDF, and
CourtListener stores each one as its own cluster: the Court's opinion, a
concurrence, a dissent, the one-page judgment, and any substituted opinion.
All of them carry the same caption and docket number, so a caption check
cannot tell them apart. This module lists the records on a docket and turns
what it finds into plain-language warnings.
"""

from __future__ import annotations

from brislaw.models import is_main_opinion_type, opinion_type_label

# A record this short (in PDF pages) next to a longer one is almost always the
# judgment or an order, not the opinion.
SHORT_RECORD_PAGES = 2


def docket_records(client, docket_id: int) -> list[dict]:
    """Describe every cluster on a docket. Returns [] when the docket has only one.

    Each record: cluster_id, date_filed, labels, authors, page_count,
    has_main (holds the court's opinion), short (probably a judgment).
    Costs one API call, or two when the docket has more than one cluster.
    """
    clusters = client.list_docket_clusters(docket_id)
    if len(clusters) <= 1:
        return []

    by_cluster: dict[int, list[dict]] = {}
    for op in client.list_docket_opinions(docket_id):
        by_cluster.setdefault(int(op.get("cluster_id") or 0), []).append(op)

    records: list[dict] = []
    for c in clusters:
        ops = by_cluster.get(int(c.get("id") or 0), [])
        pages = [op.get("page_count") for op in ops if op.get("page_count")]
        page_count = sum(pages) if pages else None
        records.append({
            "cluster_id": int(c.get("id") or 0),
            "opinion_ids": [int(op["id"]) for op in ops if op.get("id")],
            "date_filed": c.get("date_filed") or "",
            "labels": [opinion_type_label(op.get("type", "")) for op in ops],
            "authors": [op.get("author_str") for op in ops if op.get("author_str")],
            "page_count": page_count,
            "has_main": any(is_main_opinion_type(op.get("type", "")) for op in ops),
            "short": page_count is not None and page_count <= SHORT_RECORD_PAGES,
        })
    return records


def describe_record(record: dict) -> str:
    """One-line description: 'cluster:10618530 (2025-06-27, Majority Opinion by Devine, 29 pages)'."""
    parts = [record.get("date_filed") or "undated"]
    labels = ", ".join(dict.fromkeys(record.get("labels") or [])) or "no opinion text"
    authors = ", ".join(dict.fromkeys(record.get("authors") or []))
    parts.append(f"{labels} by {authors}" if authors else labels)
    pages = record.get("page_count")
    if pages:
        parts.append(f"{pages} page" + ("" if pages == 1 else "s"))
    return f"cluster:{record['cluster_id']} ({', '.join(parts)})"


def best_record(records: list[dict]) -> dict | None:
    """The record most likely to be the current opinion of the court.

    Prefers a full-length record holding the court's opinion, then the
    latest date, then the longest.
    """
    if not records:
        return None
    return sorted(
        records,
        key=lambda r: (
            r["has_main"] and not r["short"],
            r["date_filed"],
            r["page_count"] or 0,
        ),
        reverse=True,
    )[0]


def build_warnings(cluster_id: int, records: list[dict]) -> list[str]:
    """Warnings about the fetched cluster, judged against its docket siblings."""
    if not records:
        return []
    current = next((r for r in records if r["cluster_id"] == cluster_id), None)
    if current is None:
        return []
    others = [r for r in records if r["cluster_id"] != cluster_id]
    full_opinions = [r for r in others if r["has_main"] and not r["short"]]
    best = best_record(full_opinions)

    warnings: list[str] = []
    if not current["has_main"]:
        what = ", ".join(dict.fromkeys(label.lower() for label in current["labels"])) or "separate writing"
        authors = ", ".join(dict.fromkeys(current["authors"]))
        if authors:
            what = f"{what} ({authors})"
        if best:
            warnings.append(
                f"This record holds only a {what}, not the court's opinion. "
                f"The court's opinion is {describe_record(best)}."
            )
        else:
            warnings.append(
                f"This record holds only a {what}, not the court's opinion, and no "
                f"record of the court's opinion was found on this docket."
            )
    elif current["short"] and best:
        pages = current["page_count"]
        warnings.append(
            f"This record is {pages} page{'' if pages == 1 else 's'} long and is probably "
            f"the judgment or an order, not the opinion. The opinion is {describe_record(best)}."
        )

    if current["has_main"] and not current["short"]:
        later = [r for r in full_opinions if r["date_filed"] > current["date_filed"]]
        if later:
            newest = best_record(later)
            warnings.append(
                f"A later opinion was filed on this docket: {describe_record(newest)}. "
                f"It may be a corrected or substituted opinion. Check which one is "
                f"current before relying on this one."
            )
    return warnings
