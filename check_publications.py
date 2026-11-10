"""Search Europe PMC / OpenAlex for papers acknowledging ACCEM and compare
them with the website's Publications page.

Usage:
    python check_publications.py [--csv out.csv]

Needs network access and the usual DATABASE_URL (for managed publications).
"""

import argparse
import sys

from flask import Flask

import publication_finder
from blueprints.public import get_site_publications


def run(csv_path=None, out=sys.stdout):
    app = Flask(__name__)
    with app.app_context():
        site = get_site_publications()
    report = {}
    candidates = publication_finder.find_candidates(report)
    comparison = publication_finder.compare_with_site(candidates, site)

    print("Queries:", file=out)
    for query in report.get("queries", []):
        status = f"error: {query['error']}" if query.get("error") else f"{query['hit_count']} hits"
        print(f"  {query['id']:<70} {status}", file=out)
    high = sum(1 for c in candidates if c["confidence"] == "high")
    print(
        f"\nCandidates: {len(candidates)} ({high} high, {len(candidates) - high} medium)\n"
        f"Website publications: {len(site)}\n"
        f"Already on website: {len(comparison['on_website'])}\n"
        f"Not on website: {len(comparison['not_on_website'])}\n"
        f"On website but not found online: {len(comparison['on_website_not_found_online'])}",
        file=out,
    )
    if comparison["not_on_website"]:
        print("\nFound online, not on website:", file=out)
        for c in comparison["not_on_website"]:
            notes = [c["confidence"], publication_finder.describe_matches(c)]
            if c.get("is_preprint"):
                notes.append("preprint")
            if c.get("mentions_national_facility"):
                notes.append("also thanks National Cryo-EM Facility")
            print(f"  {c.get('year') or '----'}  {c.get('title')}", file=out)
            print(f"        {c.get('doi_url') or c.get('europe_pmc_url') or ''}  [{'; '.join(notes)}]", file=out)
    if csv_path:
        with open(csv_path, "w", encoding="utf-8", newline="") as handle:
            handle.write(publication_finder.results_csv(comparison))
        print(f"\nWrote {csv_path}", file=out)
    return comparison


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", help="write all results to this CSV file")
    args = parser.parse_args(argv)
    try:
        run(args.csv)
    except publication_finder.FinderError as exc:
        print(f"Search failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
