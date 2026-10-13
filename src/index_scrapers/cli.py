import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import httpx

from .artifacts import restore, snapshot
from .delivery import flush
from .runner import discover, initial_status, load_targets, review
from .state import State


def main(argv=None):
    parser = argparse.ArgumentParser(description="Index scrapers: durable HTTP collection")
    parser.add_argument("mode", choices=["discover", "initial", "prices", "withdrawals", "health", "restore", "snapshot", "deliver"])
    parser.add_argument("--operation", choices=["rent", "sale"])
    parser.add_argument("--state", default="state/catalog.sqlite")
    parser.add_argument("--targets", default="config/targets.json")
    parser.add_argument("--municipalities", default="data/municipalities.csv")
    parser.add_argument("--health", default="health.json")
    parser.add_argument("--snapshot-dir", default="state/artifact")
    parser.add_argument("--initialize", action="store_true")
    parser.add_argument("--no-delivery", action="store_true")
    parser.add_argument("--continue-initial", action="store_true", help="Resume eligible initial collection before regular discovery")
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--initial-max-pages", type=int, default=36, help="Maximum pages for automatic initial continuation (default: 36)")
    parser.add_argument("--limit", type=int, default=100, help="Maximum reviewed listings or explicitly delivered items (default: 100)")
    args = parser.parse_args(argv)
    report = {"mode": args.mode, "anomalies": []}
    try:
        if args.mode not in {"restore", "snapshot"} and (args.max_pages < 1 or args.initial_max_pages < 1 or args.limit < 1):
            raise ValueError("limits must be positive")
        with httpx.Client(timeout=25, headers={"User-Agent": "Index/0.1 (+authorized portal data collection)"}) as client:
            if args.mode == "restore":
                result = restore(client, os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"], args.state, args.initialize, os.environ.get("DEFAULT_BRANCH", "main"))
                print(json.dumps({"restored": result}))
                return 0
            if args.mode == "snapshot":
                if not Path(args.state).is_file():
                    raise ValueError("missing state; cannot publish empty snapshot")
                with State(args.state) as state:
                    snapshot(state, args.snapshot_dir)
                print(json.dumps({"snapshot": "saved"}))
                return 0
            targets = load_targets(args.targets, args.municipalities) if args.mode != "deliver" else []
            if args.mode == "deliver" and not Path(args.state).is_file():
                raise ValueError("missing durable state before delivery")
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            with State(args.state) as state:
                if args.mode in {"discover", "initial", "health"}:
                    all_targets = targets
                    continuing_initial = args.mode == "discover" and args.continue_initial and initial_status(state, all_targets, now)["eligible"] > 0
                    if not continuing_initial:
                        targets = [target for target in targets if args.operation is None or target["operation"] == args.operation]
                    if not targets:
                        raise ValueError("no configured targets for this operation")
                    page_limit = args.initial_max_pages if continuing_initial else args.max_pages
                    report.update(discover(state, client, targets, now, max_pages=1 if args.mode == "health" else page_limit, hash_images=args.mode != "health", resume=args.mode == "initial" or continuing_initial, read_only=args.mode == "health"))
                    if args.mode == "discover" and args.continue_initial:
                        report.setdefault("initial", {}).update(initial_status(state, all_targets, now))
                elif args.mode != "deliver":
                    report.update(review(state, client, args.operation, now, mode=args.mode, limit=args.limit))
                if not args.no_delivery and args.mode != "health":
                    delivery_options = {"max_items": args.limit} if args.mode == "deliver" else {}
                    report["delivery"] = flush(state, client, os.environ["INGEST_URL"], os.environ["INGEST_TOKEN"], **delivery_options)
    except Exception as exc:
        report["anomalies"].append({"portal": "runner", "reason": type(exc).__name__})
    path = Path(args.health)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)
    print(json.dumps(report, ensure_ascii=True))
    return 1 if report["anomalies"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
