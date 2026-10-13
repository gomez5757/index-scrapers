"""Run bounded directory discovery in Actions; output never promotes production targets."""

import argparse
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from index_scrapers.target_discovery import DEFAULT_SEEDS, HOSTS, MunicipalityIndex, run_discovery


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--municipalities", type=Path, default=ROOT / "data/municipalities.csv")
    parser.add_argument("--provinces-html", type=Path, default=ROOT / "tests/fixtures/targets/ine-provinces.directory.html")
    parser.add_argument("--capitals-html", type=Path, default=ROOT / "tests/fixtures/targets/boe-capitals.html")
    parser.add_argument("--existing-targets", type=Path, default=ROOT / "config/targets.json")
    parser.add_argument("--checkpoint", type=Path, default=ROOT / "data/target-discovery.checkpoint.json")
    parser.add_argument("--output", type=Path, default=ROOT / "data/targets.observed.json")
    parser.add_argument("--max-pages", type=int, default=5)
    parser.add_argument("--max-depth", type=int, default=2)
    parser.add_argument("--max-page-bytes", type=int, default=4 * 1024 * 1024)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--portal", action="append", choices=list(HOSTS), help="Limit initial seeds; existing checkpoint keeps its durable frontier")
    args = parser.parse_args(argv)
    lock = args.checkpoint.with_name(args.checkpoint.name + ".lock")
    if args.output.resolve() == lock.resolve():
        parser.error("Output cannot replace the checkpoint lock")
    protected = {p.resolve() for p in (args.municipalities, args.provinces_html, args.capitals_html, args.existing_targets,
                                      ROOT / "config/targets.json", ROOT / "config/targets.source.json")}
    if args.output.resolve() in protected or args.checkpoint.resolve() in protected or args.output.resolve() == args.checkpoint.resolve():
        parser.error("Checkpoint/output must be separate files and cannot overwrite source data or production configuration")
    if args.max_pages < 1 or args.max_depth < 0 or args.max_page_bytes < 1 or args.timeout <= 0:
        parser.error("Work limits and timeout must be positive (max-depth may be zero)")
    existing = json.loads(args.existing_targets.read_text(encoding="utf-8"))
    city_slugs = {}
    for target in existing:
        code, slug = target["municipality"], target["city_slug"]
        if code in city_slugs and city_slugs[code] != slug:
            parser.error("Existing targets have conflicting city slugs for an INE municipality")
        city_slugs[code] = slug
    geography = MunicipalityIndex.from_csv(args.municipalities, provinces_html=args.provinces_html, capitals_html=args.capitals_html,
                                         major_codes={t["municipality"] for t in existing if t.get("priority") == "major"}, city_slugs=city_slugs)
    seeds = [url for url in DEFAULT_SEEDS if not args.portal or any(urlsplit_host(url) in HOSTS[p] for p in args.portal)]
    with httpx.Client(timeout=args.timeout, follow_redirects=False, trust_env=False) as client:
        summary = run_discovery(client, geography, args.checkpoint, args.output, seeds=seeds, max_pages=args.max_pages,
                                max_depth=args.max_depth, max_page_bytes=args.max_page_bytes)
    print(json.dumps(summary, ensure_ascii=True, sort_keys=True))
    return 0


def urlsplit_host(url):
    from urllib.parse import urlsplit
    return urlsplit(url).hostname


if __name__ == "__main__":
    raise SystemExit(main())
