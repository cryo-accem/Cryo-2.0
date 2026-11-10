"""Count EMDB structures linked to ACCEM research.

Uses the EMDB search API (Solr syntax, CSV output):
https://www.ebi.ac.uk/emdb/api/search/<query>?rows=...&fl=...

An entry is counted when any of these hold (the union, deduplicated by EMDB id):
  * website_paper   - it cites a publication on the website's Publications page
                      (by DOI, PMID as a fallback where the finder knows one);
  * orcid / dutta_india / iisc_group - it belongs to one of ACCEM_GROUPS
                      (see that constant for the per-group rules; the non-India
                      "Dutta S" entries are a different, earlier era and are
                      never counted);
  * acknowledgement - it cites a high-confidence paper the publication finder
                      found online that is not on the website yet;
  * approved        - an admin approved it from the "needs review" list.

"Needs review" = Talos Arctica (the ACCEM microscope) + India-funded entries
not in the union; they are shown to admins but not counted until approved.
"""

import csv
import io
import re
import urllib.error
import urllib.parse
import urllib.request

EMDB_SEARCH_URL = "https://www.ebi.ac.uk/emdb/api/search/"
EMDB_ENTRY_URL = "https://www.ebi.ac.uk/emdb/{}"
USER_AGENT = "ACCEM-structure-check/1.0"
REQUEST_TIMEOUT_SECONDS = 30
REQUEST_ATTEMPTS = 2
MAX_ROWS = 5000
BATCH_SIZE = 20

SOMNATH_DUTTA_ORCID = "0000-0002-3181-0108"
ACCEM_MICROSCOPE = "FEI TALOS ARCTICA"

# IISc groups whose EMDB entries count as ACCEM structures. To add a group,
# append an entry (owner-approved only). Rules:
#   "facility_head": every entry with the ORCID (any microscope), plus entries
#       deposited under "depositor" with Indian grant funding (any microscope).
#   "talos_india": entries deposited under "depositor" (or with the ORCID, if
#       given) that are India-funded AND collected on the ACCEM microscope
#       (FEI TALOS ARCTICA).
# Individual entries can also be approved from the admin "Needs review" list.
ACCEM_GROUPS = (
    {"name": "Somnath Dutta (ACCEM)", "depositor": "Dutta S",
     "orcid": SOMNATH_DUTTA_ORCID, "rule": "facility_head"},
    {"name": "Vidya Mangala Prasad", "depositor": "Prasad VM",
     "orcid": None, "rule": "talos_india"},
    {"name": "Tanweer Hussain", "depositor": "Hussain T",
     "orcid": None, "rule": "talos_india"},
)
# Individual entries the facility owner approved after the IISc PI survey
# (Nov 2026). They are counted like admin-approved entries.
#   EMD-35216, EMD-35233: Hussain T, Talos Arctica, Biochemistry 2024
#       (10.1021/acs.biochem.4c00489); the preprint acknowledges the IISc
#       cryo-EM facility.
#   EMD-36619, EMD-36620: Hussain T, Talos Arctica, Proteins 2024
#       (10.1002/prot.26655); IISc affiliation, Wellcome-funded (so the
#       India-funding rule misses them).
OWNER_APPROVED_IDS = ("EMD-35216", "EMD-35233", "EMD-36619", "EMD-36620")
FIELDS = (
    "emdb_id,title,release_date,microscope_name,grant_country,xref_DOI,xref_PUBMED,"
    "primary_citation_title,primary_citation_year,primary_citation_author_string,dep_author_name"
)
# Groups at other institutes that run their own Talos Arctica: their entries
# are hidden from "Needs review" (never counted). Matched on EMDB depositor
# names (dep_author_name, comma-separated).
EXCLUDED_GROUPS = (
    {"name": "Chowdhury S", "depositor": "Chowdhury S", "institute": "CSIR-CCMB, Hyderabad"},
)

SOURCE_LABELS = {
    "website_paper": "Website paper",
    "orcid": "S. Dutta ORCID",
    "dutta_india": "Dutta S (India-funded)",
    "iisc_group": "Approved IISc group",
    "acknowledgement": "Paper found by finder",
    "approved": "Approved by admin",
}
_SOURCE_ORDER = tuple(SOURCE_LABELS)
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>,;|]+", re.IGNORECASE)


class EmdbError(Exception):
    """Raised when EMDB could not be queried."""


def entry_url(emdb_id):
    return EMDB_ENTRY_URL.format(emdb_id)


def _dois_in(value):
    return {match.rstrip(".)]").lower() for match in _DOI_RE.findall(value or "")}


def _pmids_in(value):
    return set(re.findall(r"\d{5,9}", value or ""))


def search(query, fields=FIELDS):
    """Run one EMDB search and return the CSV rows as dicts."""
    url = (
        EMDB_SEARCH_URL + urllib.parse.quote(query, safe="") + "?"
        + urllib.parse.urlencode({"rows": MAX_ROWS, "fl": fields})
    )
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/csv"})
    for attempt in range(REQUEST_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                text = response.read().decode("utf-8")
            return list(csv.DictReader(io.StringIO(text)))
        except urllib.error.HTTPError as exc:
            if exc.code < 500 or attempt + 1 == REQUEST_ATTEMPTS:
                raise
        except OSError:
            if attempt + 1 == REQUEST_ATTEMPTS:
                raise
    return []


def _batches(values, size=BATCH_SIZE):
    values = sorted(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _entry(row):
    return {
        "emdb_id": row.get("emdb_id"),
        "title": (row.get("title") or "").strip(),
        "release_date": (row.get("release_date") or "")[:10],
        "microscope": row.get("microscope_name") or "",
        "grant_country": row.get("grant_country") or "",
        "paper_title": (row.get("primary_citation_title") or "").strip(),
        "paper_year": row.get("primary_citation_year") or "",
        "paper_authors": (row.get("primary_citation_author_string") or "").strip(),
        "paper_doi": sorted(_dois_in(row.get("xref_DOI")))[0] if _dois_in(row.get("xref_DOI")) else None,
        "pmid": sorted(_pmids_in(row.get("xref_PUBMED")))[0] if _pmids_in(row.get("xref_PUBMED")) else None,
        "depositors": row.get("dep_author_name") or "",
        "url": entry_url(row.get("emdb_id")),
        "sources": [],
        "groups": [],
    }


def excluded_group(row):
    """Return the EXCLUDED_GROUPS entry matching the row's depositors, or None."""
    depositors = {name.strip().lower() for name in re.split(r"[,;]", row.get("dep_author_name") or "")}
    for group in EXCLUDED_GROUPS:
        if group["depositor"].lower() in depositors:
            return group
    return None


def is_india_funded(row):
    return "india" in (row.get("grant_country") or "").lower()


def _paper_keys(site_publications, finder_candidates):
    """Return (site_dois, site_pmids, finder_dois, finder_pmids)."""
    from publication_finder import compare_with_site, normalize_doi

    site_dois = {normalize_doi(p.get("doi")) for p in site_publications} - {None}
    site_pmids, finder_dois, finder_pmids = set(), set(), set()
    if finder_candidates:
        comparison = compare_with_site(finder_candidates, site_publications)
        for candidate in comparison["on_website"]:
            if candidate.get("pmid"):
                site_pmids.add(str(candidate["pmid"]))
        for candidate in comparison["not_on_website"]:
            # Only high-confidence finds: medium ones (phrase A outside the
            # acknowledgements) are often other facilities' papers.
            if candidate.get("confidence") != "high":
                continue
            dois = {candidate.get("doi")} | {p.get("doi") for p in candidate.get("also_preprint") or []}
            finder_dois |= {doi for doi in dois if doi} - site_dois
            if candidate.get("pmid"):
                finder_pmids.add(str(candidate["pmid"]))
    return site_dois, site_pmids, finder_dois, finder_pmids - site_pmids


def compute_structures(site_publications, finder_candidates=None, approved_ids=(),
                       dismissed_ids=(), report=None):
    """Return {"entries": [...], "needs_review": [...]} (see module docstring).

    Raises EmdbError if any EMDB query fails: a partial union would undercount
    and silently shrink the home-page number.
    """
    if report is None:
        report = {}
    report["queries"] = []
    entries = {}
    approved_ids = set(approved_ids or ()) | set(OWNER_APPROVED_IDS)
    dismissed_ids = set(dismissed_ids or ())

    def run(label, query, source, keep=lambda row: True, group=None):
        try:
            rows = search(query)
        except (OSError, ValueError, csv.Error) as exc:
            report["queries"].append({"label": label, "query": query, "hits": None, "error": str(exc)[:200]})
            raise EmdbError(f"EMDB query failed ({label}): {exc}") from exc
        kept = [row for row in rows if row.get("emdb_id") and keep(row)]
        report["queries"].append({"label": label, "query": query, "hits": len(rows), "kept": len(kept), "error": None})
        for row in kept:
            entry = entries.setdefault(row["emdb_id"], _entry(row))
            if source not in entry["sources"]:
                entry["sources"].append(source)
            if group and group not in entry["groups"]:
                entry["groups"].append(group)
        return rows

    site_dois, site_pmids, finder_dois, finder_pmids = _paper_keys(site_publications, finder_candidates)
    for label, keys, field, source in (
        ("website papers (DOI)", site_dois, "xref_DOI", "website_paper"),
        ("website papers (PMID)", site_pmids, "xref_PUBMED", "website_paper"),
        ("finder papers (DOI)", finder_dois, "xref_DOI", "acknowledgement"),
        ("finder papers (PMID)", finder_pmids, "xref_PUBMED", "acknowledgement"),
    ):
        for batch in _batches(keys):
            values = " OR ".join(f'"{key}"' for key in batch)
            run(label, f"{field}:({values})", source)
    talos = f'microscope_name:"{ACCEM_MICROSCOPE}" AND grant_country:India'
    for group in ACCEM_GROUPS:
        name, depositor, orcid = group["name"], group.get("depositor"), group.get("orcid")
        if group["rule"] == "facility_head":
            if orcid:
                run(f"{name}: ORCID", f'author_orcid:"{orcid}"', "orcid", group=name)
            if depositor:
                run(f"{name}: depositor + India", f'dep_author_name:"{depositor}" AND grant_country:India',
                    "dutta_india", keep=is_india_funded, group=name)
        elif group["rule"] == "talos_india":
            on_talos = lambda row: is_india_funded(row) and ACCEM_MICROSCOPE in (row.get("microscope_name") or "").upper()
            if depositor:
                run(f"{name}: depositor + Talos + India", f'dep_author_name:"{depositor}" AND {talos}',
                    "iisc_group", keep=on_talos, group=name)
            if orcid:
                run(f"{name}: ORCID + Talos + India", f'author_orcid:"{orcid}" AND {talos}',
                    "iisc_group", keep=on_talos, group=name)
        else:
            raise ValueError(f"Unknown ACCEM_GROUPS rule: {group['rule']}")
    if approved_ids:
        for batch in _batches(approved_ids):
            values = " OR ".join(f'"{key}"' for key in batch)
            run("approved by admin", f"emdb_id:({values})", "approved")

    review_rows = run("Talos Arctica + India (review)", talos, "_review", keep=lambda row: False)
    needs_review, excluded = [], {}
    for row in review_rows:
        if (not row.get("emdb_id") or row["emdb_id"] in entries
                or row["emdb_id"] in dismissed_ids or not is_india_funded(row)):
            continue
        group = excluded_group(row)
        if group:
            label = f"{group['name']} ({group['institute']})"
            excluded[label] = excluded.get(label, 0) + 1
            continue
        needs_review.append(_entry(row))
    for entry in entries.values():
        entry["sources"].sort(key=_SOURCE_ORDER.index)
    order = lambda e: (e.get("release_date") or "", e["emdb_id"])
    return {
        "entries": sorted(entries.values(), key=order, reverse=True),
        "needs_review": sorted(needs_review, key=order, reverse=True),
        "excluded": excluded,
    }


def build_payload(result, previous=None, checked_at=None, report=None):
    """Stored shape for site_stats_cache['emdb_structures']."""
    ids = [entry["emdb_id"] for entry in result["entries"]]
    previous_ids = list((previous or {}).get("ids") or [])
    new_ids = sorted(set(ids) - set(previous_ids)) if previous else []
    return {
        "checked_at": checked_at,
        "count": len(ids),
        "ids": ids,
        "previous_ids": previous_ids,
        "new_ids": new_ids,
        "entries": result["entries"],
        "needs_review": result["needs_review"],
        "excluded": result.get("excluded") or {},
        "queries": (report or {}).get("queries", []),
    }


def approve_entry(payload, emdb_id):
    """Move a needs-review entry into the counted list (no network)."""
    entry = next((e for e in payload.get("needs_review") or [] if e["emdb_id"] == emdb_id), None)
    if entry is None:
        return payload, False
    entry = dict(entry, sources=["approved"])
    entries = [entry] + list(payload.get("entries") or [])
    entries.sort(key=lambda e: (e.get("release_date") or "", e["emdb_id"]), reverse=True)
    payload = dict(
        payload,
        entries=entries,
        ids=[e["emdb_id"] for e in entries],
        count=len(entries),
        needs_review=[e for e in payload["needs_review"] if e["emdb_id"] != emdb_id],
    )
    return payload, True


def dismiss_entry(payload, emdb_id):
    before = payload.get("needs_review") or []
    after = [e for e in before if e["emdb_id"] != emdb_id]
    return dict(payload, needs_review=after), len(after) != len(before)


def group_by_paper(entries):
    """[{"paper": {...}, "entries": [...]}] grouped by citation DOI, newest
    first; entries whose EMDB record has no DOI form one final group."""
    groups = {}
    for entry in entries:
        doi = entry.get("paper_doi")
        group = groups.setdefault(doi or "", {"paper": {
            "title": entry.get("paper_title") if doi else "No DOI-linked paper in EMDB (unpublished or citation pending)",
            "doi": doi,
            "year": entry.get("paper_year") if doi else "",
        }, "entries": []})
        group["entries"].append(entry)
    return sorted(
        groups.values(),
        key=lambda g: (bool(g["paper"]["doi"]), str(g["paper"]["year"] or ""),
                       max(e.get("release_date") or "" for e in g["entries"])),
        reverse=True,
    )


CSV_COLUMNS = (
    "status", "emdb_id", "url", "title", "release_date", "microscope", "grant_country",
    "sources", "groups", "paper_title", "paper_doi", "paper_year", "new_since_last_check",
)


def payload_csv(payload):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    new_ids = set(payload.get("new_ids") or [])
    for status, entries in (("counted", payload.get("entries") or []),
                            ("needs_review", payload.get("needs_review") or [])):
        for entry in entries:
            writer.writerow({
                **entry,
                "status": status,
                "sources": "; ".join(SOURCE_LABELS.get(s, s) for s in entry.get("sources") or []),
                "groups": "; ".join(entry.get("groups") or []),
                "new_since_last_check": "yes" if entry["emdb_id"] in new_ids else "",
            })
    return output.getvalue()
