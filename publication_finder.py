"""Find publications that acknowledge the ACCEM cryo-EM facility at IISc.

Sources (no API keys, standard library only):

* Europe PMC REST search. ``ACK_FUND`` searches the acknowledgement/funding
  sections and ``AFF`` the affiliations of open-access full texts; an
  unfielded query searches everything Europe PMC indexes (title, abstract and
  open-access full text).
* OpenAlex ``fulltext.search`` for the exact centre-name phrases.

Only open-access papers expose their acknowledgements, so the finder can never
be exhaustive: it suggests candidates for an admin to review, it does not
decide what appears on the website.

``find_candidates()`` returns merged candidate dicts; ``compare_with_site()``
splits them against the website's Publications list.
"""

from concurrent.futures import ThreadPoolExecutor
import csv
import difflib
import html
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

# ── What the finder searches for ─────────────────────────────────────────────
# The two "tags" below were specified by the facility owner. Each tag is a set
# of phrase variants that must co-occur with the institution words (and, for
# tag A, a city word). Every tag is searched in the acknowledgement/funding
# text, the full text and the affiliations (Europe PMC); tag B's centre-name
# phrases are also searched in OpenAlex full text.
#   * Tag B (the centre's exact name) is always "high" confidence.
#   * Tag A ("cryo-EM facility" + IISc + city) is "high" in acknowledgements
#     and "medium" anywhere else (an IISc paper may mention some other cryo-EM
#     facility in its methods).
# Europe PMC cannot restrict a word to the same sentence, so the institution
# words are required in the same section as the phrase, and the city words
# anywhere in the paper (requiring "Bangalore" inside the acknowledgement as
# well dropped 13 of 34 genuine facility papers in testing).
# "ACCEM" alone matches an unrelated Spanish NGO; it is only searched together
# with the institution words, and never as an OpenAlex free-text search.
SEARCH_TAGS = {
    "A": {
        "label": "cryo-EM facility, IISc Bangalore",
        "phrases": (
            "cryo-EM facility",
            "cryo-electron microscopy facility",
            "cryoEM facility",
            "cryo EM facility",
        ),
        "institution": ("IISc", "Indian Institute of Science"),
        "city": ("Bangalore", "Bengaluru"),
    },
    "B": {
        "label": "Advanced Center for Cryo-Electron Microscopy, Indian Institute of Science, Bangalore",
        "phrases": (
            "Advanced Center for Cryo-Electron Microscopy",
            "Advanced Centre for Cryo-Electron Microscopy",
            "Advanced Center for Cryo Electron Microscopy",
            "Advanced Centre for Cryo Electron Microscopy",
            "ACCEM",
        ),
        "institution": ("IISc", "Indian Institute of Science"),
        # Optional for tag B: the centre name plus the institute is specific.
        "city": (),
    },
    # The facility's official acknowledgement cites this DBT-BUILDER grant.
    # The code is unique to ACCEM, so no institution or city words are needed
    # and every match is "high" confidence. (The DST-FIST grant
    # SR/FST/LSII-039/2015 is not searched: it funds the whole Molecular
    # Biophysics Unit, not only the facility.)
    "C": {
        "label": "DBT-BUILDER Program (BT/INF/22/SP22844/2017)",
        "phrases": ("SP22844",),
        "institution": (),
        "city": (),
    },
}

# A different facility (NCBS, Bengaluru; also a US NCI facility of the same
# name). Papers that only mention it are never added; papers matching a tag
# that also mention it are flagged for the reviewer.
EXCLUDED_FACILITY_PHRASE = "National Cryo-EM Facility"

FIELD_LABELS = {
    "acknowledgement": "acknowledgement",
    "fulltext": "full text",
    "affiliation": "affiliation",
}

EUROPE_PMC_SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
OPENALEX_WORKS_URL = "https://api.openalex.org/works"
REQUEST_TIMEOUT_SECONDS = 25  # searches run in the background, so slow sources can finish
MAX_PARALLEL_REQUESTS = 6
USER_AGENT = "ACCEM-publication-finder/1.0"
EUROPE_PMC_PAGE_SIZE = 1000
EUROPE_PMC_MAX_PAGES = 3
TITLE_SIMILARITY_THRESHOLD = 0.9
PREPRINT_DOI_PREFIXES = ("10.1101/", "10.64898/", "10.21203/", "10.2139/ssrn")
CANDIDATE_FIELDS = (
    "title", "authors", "journal", "year", "doi", "pmid", "pmcid", "sources",
    "matched_queries", "matches", "confidence", "is_preprint", "also_preprint",
    "other_dois", "mentions_national_facility", "europe_pmc_url", "doi_url",
)


class FinderError(Exception):
    """Raised when no source could be queried at all."""


# ── Normalisation helpers ────────────────────────────────────────────────────

_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)


def normalize_doi(value):
    """Return a bare lowercase DOI ("10.x/y") or None."""
    if not value:
        return None
    match = _DOI_RE.search(urllib.parse.unquote(str(value)))
    if not match:
        return None
    return match.group(0).rstrip(".,;)]").lower()


def strip_markup(text):
    """Remove HTML tags (also HTML-escaped ones, as Europe PMC sends)."""
    text = html.unescape(html.unescape(text or ""))
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def normalize_title(text):
    text = strip_markup(text).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


def title_similarity(left, right):
    left, right = normalize_title(left), normalize_title(right)
    if not left or not right:
        return 0.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def is_preprint_doi(doi):
    return bool(doi) and doi.startswith(PREPRINT_DOI_PREFIXES)


def doi_url(doi):
    return f"https://doi.org/{doi}" if doi else None


_STOPWORDS = frozenset(
    "a an and are as at by for from in into is its of on or reveal reveals "
    "revealed the to via with using cryo em electron microscopy structure "
    "structures structural insights".split()
)
_CORRECTION_RE = re.compile(r"\b(corrigendum|erratum|correction|retraction)\b", re.I)


def author_key(name):
    """'Majumder P' / 'Padmanabhan, S.' -> 'majumder'; tolerant of spelling."""
    name = strip_markup(name).strip().strip(".")
    if not name:
        return ""
    surname = name.split(",")[0].split()[0] if "," in name else name.split()[0]
    return re.sub(r"[^a-z]", "", surname.lower())


def _content_words(title):
    return {word for word in normalize_title(title).split() if word not in _STOPWORDS}


def is_same_work_versions(preprint, published):
    """Heuristic preprint -> published link when the titles were rewritten.

    Requires the same first author, at least half the authors shared, the
    published version not older than the preprint, and at least three (and
    half of the shorter title's) content words shared.
    """
    pre_authors, pub_authors = preprint.get("_author_keys") or [], published.get("_author_keys") or []
    if not pre_authors or not pub_authors or pre_authors[0] != pub_authors[0]:
        return False
    pre_set, pub_set = set(pre_authors), set(pub_authors)
    if len(pre_set & pub_set) / len(pre_set | pub_set) < 0.5:
        return False
    if (preprint.get("year") or 0) > (published.get("year") or 9999):
        return False
    pre_words, pub_words = _content_words(preprint.get("title")), _content_words(published.get("title"))
    shared = len(pre_words & pub_words)
    # Same-lab papers share authors, so the titles must still overlap clearly.
    return shared >= 3 and shared / min(len(pre_words), len(pub_words)) >= 0.5


def _short_authors(names, limit=3):
    names = [name for name in names if name]
    if not names:
        return ""
    if len(names) > limit:
        return ", ".join(names[:limit]) + " et al."
    return ", ".join(names)


# ── Query construction ───────────────────────────────────────────────────────

def _quote(term):
    return f'"{term}"' if re.search(r"[\s\-]", term) else term


def _group(terms, field=None):
    joined = " OR ".join(_quote(term) for term in terms)
    group = f"({joined})"
    return f"{field}:{group}" if field else group


def build_queries(tags=None):
    """Return the Europe PMC and OpenAlex queries derived from SEARCH_TAGS."""
    tags = tags or SEARCH_TAGS
    field_codes = {"acknowledgement": "ACK_FUND", "fulltext": None, "affiliation": "AFF"}
    queries = []
    for tag, spec in tags.items():
        for field, code in field_codes.items():
            if not spec["institution"] and field == "affiliation":
                continue  # a grant code never appears in author affiliations
            parts = [_group(spec["phrases"], code)]
            if spec["institution"]:
                parts.append(_group(spec["institution"], code))
            if spec.get("city"):
                # City words: same section for affiliations (one string per
                # author), anywhere otherwise (see the SEARCH_TAGS comment).
                parts.append(_group(spec["city"], code if field == "affiliation" else None))
            confidence = "high" if tag in ("B", "C") or field == "acknowledgement" else "medium"
            queries.append({
                "id": f"{tag}:{field}",
                "tag": tag,
                "field": field,
                "source": "Europe PMC",
                "query": " AND ".join(parts),
                "confidence": confidence,
            })
        if tag == "B":
            for phrase in spec["phrases"]:
                if phrase == "ACCEM":
                    continue  # never an OpenAlex free-text term
                queries.append({
                    "id": f"{tag}:fulltext:openalex:{phrase}",
                    "tag": tag,
                    "field": "fulltext",
                    "source": "OpenAlex",
                    "query": f'"{phrase}"',
                    "confidence": "high",
                })
    return queries


def national_facility_query(tags=None):
    institution = (tags or SEARCH_TAGS)["A"]["institution"]
    return (
        f'ACK_FUND:"{EXCLUDED_FACILITY_PHRASE}" AND {_group(institution, "ACK_FUND")}'
    )


def search_terms_summary(tags=None):
    """Human-readable description of the tags for the admin page."""
    summary = []
    for tag, spec in (tags or SEARCH_TAGS).items():
        summary.append({
            "tag": tag,
            "label": spec["label"],
            "phrases": list(spec["phrases"]),
            "institution": list(spec["institution"]),
            "city": list(spec.get("city") or ()),
        })
    return summary


# ── HTTP ─────────────────────────────────────────────────────────────────────

REQUEST_ATTEMPTS = 2  # one retry: Europe PMC full-text queries occasionally stall


def _get_json(url, params):
    full_url = url + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        full_url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    for attempt in range(REQUEST_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code < 500 or attempt + 1 == REQUEST_ATTEMPTS:
                raise
        except OSError:
            if attempt + 1 == REQUEST_ATTEMPTS:
                raise


def search_europe_pmc(query):
    """Return (hit_count, results) for a Europe PMC query, following cursorMark."""
    results = []
    cursor = "*"
    hit_count = 0
    for _ in range(EUROPE_PMC_MAX_PAGES):
        payload = _get_json(EUROPE_PMC_SEARCH_URL, {
            "query": query,
            "format": "json",
            "resultType": "lite",
            "pageSize": EUROPE_PMC_PAGE_SIZE,
            "cursorMark": cursor,
        })
        hit_count = int(payload.get("hitCount") or 0)
        page = (payload.get("resultList") or {}).get("result") or []
        results.extend(page)
        next_cursor = payload.get("nextCursorMark")
        if not page or not next_cursor or next_cursor == cursor or len(results) >= hit_count:
            break
        cursor = next_cursor
    return hit_count, results


def search_openalex_fulltext(phrase):
    params = {
        "filter": f"fulltext.search:{phrase}",
        "per-page": 50,
        "select": "id,doi,title,publication_year,type,primary_location,authorships,ids",
    }
    contact = os.environ.get("PUBLICATION_FINDER_CONTACT", "").strip()
    if contact:
        params["mailto"] = contact
    payload = _get_json(OPENALEX_WORKS_URL, params)
    results = payload.get("results") or []
    return int((payload.get("meta") or {}).get("count") or len(results)), results


# ── Record conversion ────────────────────────────────────────────────────────

def _europe_pmc_record(result):
    source = result.get("source") or ""
    record_id = result.get("id") or ""
    doi = normalize_doi(result.get("doi"))
    authors = [a.strip() for a in (result.get("authorString") or "").rstrip(".").split(",")]
    year = result.get("pubYear")
    return {
        "title": strip_markup(result.get("title")).rstrip("."),
        "authors": _short_authors(authors),
        "_author_keys": [key for key in map(author_key, authors) if key],
        "journal": strip_markup(result.get("journalTitle")) or ("Preprint" if source == "PPR" else ""),
        "year": int(year) if str(year or "").isdigit() else None,
        "doi": doi,
        "pmid": result.get("pmid"),
        "pmcid": result.get("pmcid"),
        "is_preprint": source == "PPR" or is_preprint_doi(doi),
        "europe_pmc_url": (
            f"https://europepmc.org/article/{source}/{record_id}" if source and record_id else None
        ),
        "source": "Europe PMC",
    }


def _openalex_has_iisc(work, institution_words):
    words = [word.lower() for word in institution_words]
    for authorship in work.get("authorships") or []:
        texts = [inst.get("display_name") or "" for inst in authorship.get("institutions") or []]
        texts += authorship.get("raw_affiliation_strings") or []
        for text in texts:
            lowered = text.lower()
            if any(word in lowered for word in words):
                return True
    return False


def _openalex_record(work):
    ids = work.get("ids") or {}
    doi = normalize_doi(work.get("doi"))
    pmid = (ids.get("pmid") or "").rstrip("/").rsplit("/", 1)[-1] or None
    pmcid = (ids.get("pmcid") or "").rstrip("/").rsplit("/", 1)[-1] or None
    if pmcid and not pmcid.upper().startswith("PMC"):
        pmcid = f"PMC{pmcid}"
    source = ((work.get("primary_location") or {}).get("source") or {})
    authors = [
        ((authorship.get("author") or {}).get("display_name") or "")
        for authorship in work.get("authorships") or []
    ]
    is_preprint = work.get("type") == "preprint" or is_preprint_doi(doi)
    surnames = [name.split()[-1] if name.split() else "" for name in authors]
    return {
        "title": strip_markup(work.get("title")).rstrip("."),
        "authors": _short_authors(authors),
        "_author_keys": [key for key in map(author_key, surnames) if key],
        "journal": source.get("display_name") or ("Preprint" if is_preprint else ""),
        "year": work.get("publication_year"),
        "doi": doi,
        "pmid": pmid,
        "pmcid": pmcid,
        "is_preprint": is_preprint,
        "europe_pmc_url": (
            f"https://europepmc.org/article/MED/{pmid}" if pmid else None
        ),
        "source": "OpenAlex",
    }


# ── Merging ──────────────────────────────────────────────────────────────────

_CONFIDENCE_RANK = {"medium": 1, "high": 2}


def _new_candidate(record):
    candidate = {field: None for field in CANDIDATE_FIELDS}
    candidate.update({
        key: record.get(key) for key in (
            "title", "authors", "journal", "year", "doi", "pmid", "pmcid",
            "is_preprint", "europe_pmc_url", "_author_keys",
        )
    })
    candidate.update(
        sources=[], matched_queries=[], matches=[], confidence="medium",
        also_preprint=[], other_dois=[], mentions_national_facility=False,
    )
    return candidate


def _add_match(candidate, record, query):
    if record["source"] not in candidate["sources"]:
        candidate["sources"].append(record["source"])
    if query["id"] not in candidate["matched_queries"]:
        candidate["matched_queries"].append(query["id"])
    match = {"tag": query["tag"], "field": query["field"]}
    if match not in candidate["matches"]:
        candidate["matches"].append(match)
    if _CONFIDENCE_RANK[query["confidence"]] > _CONFIDENCE_RANK[candidate["confidence"]]:
        candidate["confidence"] = query["confidence"]
    for key in ("title", "authors", "journal", "year", "pmid", "pmcid", "europe_pmc_url", "_author_keys"):
        if not candidate.get(key) and record.get(key):
            candidate[key] = record[key]
    if record.get("doi") and record["doi"] != candidate.get("doi"):
        if not candidate.get("doi"):
            candidate["doi"] = record["doi"]
        elif record["doi"] not in candidate["other_dois"]:
            candidate["other_dois"].append(record["doi"])


def _absorb(primary, other):
    """Merge candidate ``other`` into ``primary`` (same paper, other record)."""
    for key in ("sources", "matched_queries", "matches", "other_dois"):
        for item in other[key]:
            if item not in primary[key]:
                primary[key].append(item)
    if other.get("doi") and other["doi"] != primary.get("doi") and other["doi"] not in primary["other_dois"]:
        primary["other_dois"].append(other["doi"])
    for item in other["also_preprint"]:
        if item not in primary["also_preprint"]:
            primary["also_preprint"].append(item)
    if _CONFIDENCE_RANK[other["confidence"]] > _CONFIDENCE_RANK[primary["confidence"]]:
        primary["confidence"] = other["confidence"]
    primary["mentions_national_facility"] = (
        primary["mentions_national_facility"] or other["mentions_national_facility"]
    )
    for key in ("title", "authors", "journal", "year", "pmid", "pmcid", "europe_pmc_url"):
        if not primary.get(key) and other.get(key):
            primary[key] = other[key]


def _preprint_summary(candidate):
    return {
        "doi": candidate.get("doi"),
        "year": candidate.get("year"),
        "title": candidate.get("title"),
        "europe_pmc_url": candidate.get("europe_pmc_url"),
        "doi_url": doi_url(candidate.get("doi")),
    }


def merge_records(matched_records):
    """Merge (record, query) pairs into candidates.

    Records are merged by DOI, then by normalised-title similarity
    (difflib ratio >= 0.9). When a preprint and a published version match,
    the published version is kept and the preprint is listed in
    ``also_preprint``.
    """
    by_doi = {}
    without_doi = []
    for record, query in matched_records:
        doi = record.get("doi")
        if doi and doi in by_doi:
            candidate = by_doi[doi]
        else:
            candidate = _new_candidate(record)
            if doi:
                by_doi[doi] = candidate
            else:
                without_doi.append(candidate)
        _add_match(candidate, record, query)

    candidates = list(by_doi.values()) + without_doi
    # Published originals first so they become the primary record; preprints
    # and corrections are folded into them.
    candidates.sort(key=lambda c: (
        bool(c["is_preprint"]),
        bool(_CORRECTION_RE.search(c.get("title") or "")),
        c.get("year") or 9999,
    ))
    merged = []
    for candidate in candidates:
        target = None
        for existing in merged:
            if title_similarity(existing["title"], candidate["title"]) >= TITLE_SIMILARITY_THRESHOLD:
                target = existing
                break
        if target is None and candidate["is_preprint"]:
            for existing in merged:
                if not existing["is_preprint"] and is_same_work_versions(candidate, existing):
                    target = existing
                    break
        if target is None:
            merged.append(candidate)
            continue
        if candidate["is_preprint"] and not target["is_preprint"]:
            target["also_preprint"].append(_preprint_summary(candidate))
            other = dict(candidate, doi=None)  # keep the preprint DOI out of other_dois
            _absorb(target, other)
        else:
            _absorb(target, candidate)

    for candidate in merged:
        candidate.pop("_author_keys", None)
        candidate["doi_url"] = doi_url(candidate.get("doi"))
        if not candidate.get("europe_pmc_url") and candidate.get("pmid"):
            candidate["europe_pmc_url"] = f"https://europepmc.org/article/MED/{candidate['pmid']}"
    merged.sort(key=lambda c: (-(c.get("year") or 0), (c.get("title") or "").lower()))
    return merged


# ── Main entry point ─────────────────────────────────────────────────────────

def find_candidates(report=None):
    """Run every query and return merged candidate publications.

    ``report`` (optional dict) receives per-query hit counts and errors:
    ``report["queries"]`` is a list of {id, tag, field, source, query,
    hit_count, kept, error}. Raises FinderError only if every query failed.
    """
    if report is None:
        report = {}
    queries = build_queries()

    def run_query(query):
        entry = {key: query[key] for key in ("id", "tag", "field", "source", "query")}
        try:
            if query["source"] == "Europe PMC":
                hit_count, results = search_europe_pmc(query["query"])
                records = [_europe_pmc_record(result) for result in results]
            else:
                hit_count, results = search_openalex_fulltext(query["query"])
                institution = SEARCH_TAGS[query["tag"]]["institution"]
                records = [
                    _openalex_record(work) for work in results
                    if _openalex_has_iisc(work, institution)
                ]
            entry.update(hit_count=hit_count, kept=len(records), error=None)
            return entry, records
        except Exception as exc:  # one bad source/record must not sink the whole search
            entry.update(hit_count=None, kept=0, error=f"{type(exc).__name__}: {exc}"[:200])
            return entry, []

    # The sources are queried in parallel (each request waits on the network),
    # which keeps a full search well inside the web server's request time.
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_REQUESTS) as pool:
        outcomes = list(pool.map(run_query, queries))
    report["queries"] = [entry for entry, _ in outcomes]
    matched = [
        (record, query)
        for query, (_, records) in zip(queries, outcomes)
        for record in records
    ]
    failures = sum(1 for entry in report["queries"] if entry["error"])

    if failures == len(queries):
        raise FinderError("No publication source could be reached.")

    candidates = merge_records(matched)

    # Flag papers that also thank the (different) National Cryo-EM Facility.
    national_entry = {
        "id": "flag:national-facility", "tag": "-", "field": "acknowledgement",
        "source": "Europe PMC", "query": national_facility_query(),
    }
    try:
        hit_count, results = search_europe_pmc(national_entry["query"])
        flagged_dois = {normalize_doi(r.get("doi")) for r in results if r.get("doi")}
        flagged_titles = [normalize_title(r.get("title")) for r in results]
        for candidate in candidates:
            if candidate.get("doi") in flagged_dois or normalize_title(candidate["title"]) in flagged_titles:
                candidate["mentions_national_facility"] = True
        national_entry.update(hit_count=hit_count, kept=0, error=None)
    except Exception as exc:
        national_entry.update(hit_count=None, kept=0, error=f"{type(exc).__name__}: {exc}"[:200])
    report["queries"].append(national_entry)
    return candidates


# ── Dismissed candidates ─────────────────────────────────────────────────────

def candidate_key(candidate):
    """Stable identity of a search result: its DOI, else its normalised title."""
    if candidate.get("doi"):
        return candidate["doi"]
    return "title:" + normalize_title(candidate.get("title"))[:120]


def without_dismissed(candidates, dismissed_keys):
    """Drop candidates an admin marked "Not ours" (also matched via preprint DOIs)."""
    dismissed_keys = set(dismissed_keys or ())
    if not dismissed_keys:
        return list(candidates or [])
    kept = []
    for candidate in candidates or []:
        keys = {candidate_key(candidate)} | _candidate_dois(candidate)
        if not keys & dismissed_keys:
            kept.append(candidate)
    return kept


# ── Comparison with the website ──────────────────────────────────────────────

def _site_entry_title_text(entry):
    return normalize_title(entry.get("text") or entry.get("html") or "")


def _title_in_citation(title, citation_text):
    title = normalize_title(title)
    if not title or not citation_text:
        return False
    if title in citation_text:
        return True
    # Citations reorder punctuation; compare against same-length windows.
    words, cite_words = title.split(), citation_text.split()
    size = len(words)
    if size < 4:
        return False
    # real_quick_ratio() >= quick_ratio() >= ratio() are cheap upper bounds,
    # so windows that cannot reach the threshold are skipped without running
    # the expensive ratio(). Results are identical; the page is ~20x faster,
    # which keeps the finder page well inside the gunicorn timeout on Render.
    for start in range(0, max(1, len(cite_words) - size + 1)):
        window = " ".join(cite_words[start:start + size])
        matcher = difflib.SequenceMatcher(None, title, window)
        if matcher.real_quick_ratio() < TITLE_SIMILARITY_THRESHOLD:
            continue
        if matcher.quick_ratio() < TITLE_SIMILARITY_THRESHOLD:
            continue
        if matcher.ratio() >= TITLE_SIMILARITY_THRESHOLD:
            return True
    return False


def _candidate_dois(candidate):
    dois = {candidate.get("doi")} | set(candidate.get("other_dois") or [])
    dois |= {p.get("doi") for p in candidate.get("also_preprint") or []}
    return {doi for doi in dois if doi}


def _candidate_titles(candidate):
    titles = [candidate.get("title")]
    titles += [p.get("title") for p in candidate.get("also_preprint") or []]
    return [title for title in titles if title]


def compare_with_site(candidates, site_publications):
    """Split candidates by whether the website already lists them.

    ``site_publications`` items need "doi" and "text" or "html" (the shape
    returned by blueprints.public.get_site_publications()).
    Returns a dict with lists "not_on_website", "on_website" (candidates,
    each with a "status" key) and "on_website_not_found_online" (site entries).
    """
    site = []
    for entry in site_publications:
        site.append({
            "entry": entry,
            "doi": normalize_doi(entry.get("doi")),
            "text": _site_entry_title_text(entry),
            "matched": False,
        })
    on_website, not_on_website = [], []
    for candidate in candidates:
        dois = _candidate_dois(candidate)
        titles = _candidate_titles(candidate)
        hit = None
        for item in site:
            if item["doi"] and item["doi"] in dois:
                hit = item
                break
        if hit is None:
            for item in site:
                if any(_title_in_citation(title, item["text"]) for title in titles):
                    hit = item
                    break
        result = dict(candidate)
        if hit is not None:
            hit["matched"] = True
            result["status"] = "on_website"
            on_website.append(result)
        else:
            result["status"] = "not_on_website"
            not_on_website.append(result)
    missing = []
    for item in site:
        if not item["matched"]:
            entry = item["entry"]
            missing.append({
                "year": entry.get("year") or None,
                "citation": strip_markup(entry.get("text") or entry.get("html") or ""),
                "doi": item["doi"],
                "doi_url": doi_url(item["doi"]) or entry.get("doi"),
                "origin": entry.get("origin"),
                "status": "on_website_not_found_online",
            })
    return {
        "not_on_website": not_on_website,
        "on_website": on_website,
        "on_website_not_found_online": missing,
    }


def describe_matches(candidate):
    """E.g. "A: acknowledgement, full text · B: full text"."""
    grouped = {}
    for match in candidate.get("matches") or []:
        grouped.setdefault(match["tag"], [])
        label = FIELD_LABELS.get(match["field"], match["field"])
        if label not in grouped[match["tag"]]:
            grouped[match["tag"]].append(label)
    return " · ".join(f"{tag}: {', '.join(fields)}" for tag, fields in sorted(grouped.items()))


def citation_for(candidate):
    """Plain-text citation for adding a candidate to managed_publications."""
    parts = []
    if candidate.get("authors"):
        parts.append(candidate["authors"].rstrip(".") + ".")
    if candidate.get("year"):
        parts.append(f"({candidate['year']}).")
    if candidate.get("title"):
        parts.append(candidate["title"].rstrip(".") + ".")
    if candidate.get("journal"):
        parts.append(candidate["journal"].rstrip(".") + ".")
    return " ".join(parts)


CSV_COLUMNS = (
    "status", "year", "title", "authors", "journal", "doi", "doi_url", "pmid",
    "pmcid", "europe_pmc_url", "confidence", "matched", "sources",
    "is_preprint", "preprint_dois", "mentions_national_facility",
)


class _SafeDictWriter(csv.DictWriter):
    """DictWriter that stops spreadsheet apps reading cells as formulas.

    Titles and author names come from external sources; a cell starting with
    =, +, - or @ is prefixed with an apostrophe (as in the inventory CSVs).
    """

    def writerow(self, rowdict):
        def cell(value):
            text = "" if value is None else str(value)
            if text.startswith(("\t", "\r", "\n")) or text.lstrip(" \t").startswith(("=", "+", "-", "@")):
                return "'" + text
            return value
        return super().writerow({key: cell(value) for key, value in rowdict.items()})


def results_csv(comparison):
    output = io.StringIO(newline="")
    writer = _SafeDictWriter(output, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for group in ("not_on_website", "on_website"):
        for candidate in comparison.get(group) or []:
            writer.writerow({
                **{key: candidate.get(key) for key in CSV_COLUMNS},
                "matched": describe_matches(candidate),
                "sources": "; ".join(candidate.get("sources") or []),
                "preprint_dois": "; ".join(p.get("doi") or "" for p in candidate.get("also_preprint") or []),
            })
    for entry in comparison.get("on_website_not_found_online") or []:
        writer.writerow({
            "status": entry["status"], "year": entry.get("year"),
            "title": entry.get("citation"), "doi": entry.get("doi"),
            "doi_url": entry.get("doi_url"),
        })
    return output.getvalue()
