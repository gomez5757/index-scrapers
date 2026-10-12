import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import httpx

from .artifacts import restore, snapshot
from .delivery import flush
from .runner import discover, load_targets, review
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
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)
    report = {"mode": args.mode, "anomalies": []}
    try:
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
            if args.max_pages < 1 or args.limit < 1:
                raise ValueError("limits must be positive")
            targets = load_targets(args.targets, args.municipalities) if args.mode != "deliver" else []
            if args.mode == "deliver" and not Path(args.state).is_file():
                raise ValueError("missing durable state before delivery")
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            with State(args.state) as state:
                if args.mode in {"discover", "initial", "health"}:
                    targets = [target for target in targets if args.operation is None or target["operation"] == args.operation]
                    if not targets:
                        raise ValueError("no configured targets for this operation")
                    report.update(discover(state, client, targets, now, max_pages=1 if args.mode == "health" else args.max_pages, hash_images=args.mode != "health", resume=args.mode == "initial"))
                elif args.mode != "deliver":
                    report.update(review(state, client, args.operation, now, mode=args.mode, limit=args.limit))
                if not args.no_delivery:
                    report["delivery"] = flush(state, client, os.environ["INGEST_URL"], os.environ["INGEST_TOKEN"])
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
