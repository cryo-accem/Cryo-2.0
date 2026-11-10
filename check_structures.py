"""Check EMDB for structures linked to ACCEM research and store the count
shown on the home page (same as the admin "Check EMDB now" button).

Usage:
    python check_structures.py [--csv out.csv] [--no-save]

Needs network access and the usual DATABASE_URL.
"""

import argparse
import sys

from flask import Flask

import emdb_structures
from blueprints import public


def run(csv_path=None, save=True, out=sys.stdout):
    app = Flask(__name__)
    with app.app_context():
        if save:
            payload = public.refresh_emdb_structures()
        else:
            previous = public.load_cache_payload(public.EMDB_STRUCTURES_CACHE_KEY)
            finder_run = public.load_cache_payload(public.PUBLICATION_FINDER_CACHE_KEY) or {}
            review = public.load_emdb_review_state()
            report = {}
            result = emdb_structures.compute_structures(
                public.get_site_publications(), finder_run.get("candidates"),
                review["approved"], review["dismissed"], report=report,
            )
            payload = emdb_structures.build_payload(result, previous=previous, report=report)

    print("Queries:", file=out)
    for query in payload.get("queries") or []:
        print(f"  {query['label']:<45} {query['hits']} hits, {query.get('kept', 0)} kept", file=out)
    by_source = {}
    for entry in payload["entries"]:
        for source in entry["sources"]:
            by_source[source] = by_source.get(source, 0) + 1
    print(f"\nStructures counted: {payload['count']}", file=out)
    for source, count in by_source.items():
        print(f"  {emdb_structures.SOURCE_LABELS.get(source, source)}: {count}", file=out)
    new_ids = set(payload.get("new_ids") or [])
    print(f"New since last check: {len(new_ids)}", file=out)
    for entry in payload["entries"]:
        if entry["emdb_id"] in new_ids:
            print(f"  {entry['emdb_id']}  {entry['release_date']}  {entry['title'][:80]}", file=out)
    print(f"Needs review (not counted): {len(payload['needs_review'])}", file=out)
    for entry in payload["needs_review"]:
        print(f"  {entry['emdb_id']}  {entry['release_date']}  {entry['depositors'][:40]}  | {entry['title'][:60]}", file=out)
    if csv_path:
        with open(csv_path, "w", encoding="utf-8", newline="") as handle:
            handle.write(emdb_structures.payload_csv(payload))
        print(f"\nWrote {csv_path}", file=out)
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", help="write all entries to this CSV file")
    parser.add_argument("--no-save", action="store_true", help="do not store the result")
    args = parser.parse_args(argv)
    try:
        run(args.csv, save=not args.no_save)
    except emdb_structures.EmdbError as exc:
        print(f"EMDB check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
