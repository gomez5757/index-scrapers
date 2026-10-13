"""Consistent versioned snapshots; failed restoration never starts an empty queue."""
import gzip
from contextlib import closing
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
from zipfile import ZipFile, BadZipFile

import httpx

from .state import State

MAX_STATE_BYTES = 64 * 1024 * 1024


class StateUnavailable(RuntimeError):
    pass


def _latest_snapshot(artifacts):
    try:
        dated = [(datetime.fromisoformat(item["created_at"]), item) for item in artifacts]
        if any(timestamp.tzinfo is None for timestamp, _ in dated):
            raise ValueError("snapshot timestamp has no timezone")
    except (KeyError, TypeError, ValueError) as exc:
        raise StateUnavailable("invalid snapshot timestamp; refused rollback") from exc
    latest_time = max(timestamp for timestamp, _ in dated)
    latest = [item for timestamp, item in dated if timestamp == latest_time]
    if len(latest) == 1:
        return latest[0]
    names = [re.fullmatch(r"index-state-([0-9]+)-([0-9]+)-(pre|final)", item["name"]) for item in latest]
    if (not all(names) or len({match.groups()[:2] for match in names}) != 1
            or len({match[3] for match in names}) != len(latest)):
        raise StateUnavailable("latest snapshot order ambiguous; recover a snapshot explicitly")
    return next(item for item, match in zip(latest, names) if match[3] == "final")


def restore(client, repository, token, path, initialize=False, default_branch="main"):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    path = Path(path)
    if path.exists():
        raise StateUnavailable("refusing to overwrite local state")
    available = []
    for page in range(1, 101):
        response = client.get(f"https://api.github.com/repos/{repository}/actions/artifacts", headers=headers, params={"per_page": 100, "page": page})
        response.raise_for_status()
        artifacts = response.json()["artifacts"]
        available.extend(item for item in artifacts if item["name"].startswith("index-state-"))
        if len(artifacts) < 100:
            break
    else:
        raise StateUnavailable("artifact listing incomplete")
    if not available:
        if not initialize:
            raise StateUnavailable("missing durable state; explicit first-run initialization required")
        with State(path):
            pass
        return "initialized"
    available = [item for item in available if (item.get("workflow_run") or {}).get("head_branch") == default_branch and (item.get("workflow_run") or {}).get("repository_id") == (item.get("workflow_run") or {}).get("head_repository_id") and (item.get("workflow_run") or {}).get("repository_id") is not None]
    if not available:
        raise StateUnavailable("state namespace exists but has no trusted production snapshot")
    latest = _latest_snapshot(available)
    if latest["expired"]:
        raise StateUnavailable("latest state expired; recover a snapshot explicitly")
    response = client.get(f"https://api.github.com/repos/{repository}/actions/artifacts/{latest['id']}/zip", headers=headers, follow_redirects=True)
    response.raise_for_status()
    temporary = path.with_suffix(".restore.tmp")
    try:
        with ZipFile(io.BytesIO(response.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            with gzip.GzipFile(fileobj=io.BytesIO(archive.read("catalog.sqlite.gz"))) as stream:
                content = stream.read(MAX_STATE_BYTES + 1)
        if len(content) > MAX_STATE_BYTES or manifest["schema"] != 1 or hashlib.sha256(content).hexdigest() != manifest["sha256"]:
            raise StateUnavailable("snapshot checksum, size or schema mismatch")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(content)
        with closing(sqlite3.connect(temporary)) as check:
            if check.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise StateUnavailable("SQLite integrity check failed")
            tables = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"catalog", "queue", "progress"} <= tables:
                raise StateUnavailable("snapshot missing required tables")
        os.replace(temporary, path)
    except (BadZipFile, KeyError, ValueError, OSError, sqlite3.DatabaseError) as exc:
        temporary.unlink(missing_ok=True)
        raise StateUnavailable("invalid latest snapshot; refused rollback") from exc
    return str(latest["id"])


def snapshot(state, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    raw = destination / "snapshot.tmp.sqlite"
    state.backup(raw)
    content = raw.read_bytes()
    raw.unlink()
    if len(content) > MAX_STATE_BYTES:
        raise StateUnavailable("snapshot exceeds supported state size; capacity planning required")
    zipped = gzip.compress(content, compresslevel=6, mtime=0)
    (destination / "catalog.sqlite.gz").write_bytes(zipped)
    (destination / "manifest.json").write_text(json.dumps({"schema": 1, "sha256": hashlib.sha256(content).hexdigest(), "compressed_bytes": len(zipped)}), encoding="utf-8")
