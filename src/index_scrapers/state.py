"""Transactional catalogue and append-only delivery queue in one durable SQLite file."""
import hashlib
from contextlib import closing
import json
from pathlib import Path
import sqlite3


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(listing):
    stable = {key: value for key, value in listing.items() if key not in {"first_seen", "last_seen", "last_checked", "photo_hash"}}
    return hashlib.sha256(encode(stable).encode()).hexdigest()


class State:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS catalog(id TEXT PRIMARY KEY,payload TEXT NOT NULL,fingerprint TEXT NOT NULL,misses INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS queue(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS queue_id ON queue(id,seq);
            CREATE TABLE IF NOT EXISTS progress(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        """)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def observe(self, listing):
        with self.db:
            previous = self.db.execute("SELECT payload,fingerprint FROM catalog WHERE id=?", (listing["id"],)).fetchone()
            current = dict(listing)
            if previous:
                old = json.loads(previous["payload"])
                if current["property_id"] != old["property_id"]:
                    raise ValueError("a listing cannot change its property_id")
                current["first_seen"] = old["first_seen"]
            digest = fingerprint(current)
            changed = previous is None or previous["fingerprint"] != digest
            self.db.execute("INSERT INTO catalog(id,payload,fingerprint) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,fingerprint=excluded.fingerprint,misses=0", (current["id"], encode(current), digest))
            if changed:
                wire = {key: value for key, value in current.items() if key != "photo_hash"}
                self.db.execute("INSERT INTO queue(id,payload) VALUES(?,?)", (current["id"], encode(wire)))
            return changed

    def pending(self, limit=5):
        if not 1 <= limit <= 5:
            raise ValueError("ingest batch size must be between one and five")
        rows = self.db.execute("SELECT q.seq,q.payload FROM queue q WHERE q.seq=(SELECT min(other.seq) FROM queue other WHERE other.id=q.id) ORDER BY q.seq LIMIT ?", (limit,))
        return [{"seq": item["seq"], "listing": json.loads(item["payload"])} for item in rows]

    def acknowledge(self, batch, accepted):
        accepted = set(accepted)
        with self.db:
            for item in batch:
                if item["listing"]["id"] in accepted:
                    self.db.execute("DELETE FROM queue WHERE seq=? AND id=?", (item["seq"], item["listing"]["id"]))

    def catalog(self):
        return [json.loads(row["payload"]) for row in self.db.execute("SELECT payload FROM catalog ORDER BY id")]

    def pending_count(self):
        return self.db.execute("SELECT count(*) FROM queue").fetchone()[0]

    def check(self, identifier, status, now):
        with self.db:
            found = self.db.execute("SELECT payload,misses FROM catalog WHERE id=?", (identifier,)).fetchone()
            if not found:
                raise KeyError(identifier)
            listing = json.loads(found["payload"])
            misses = found["misses"] + 1 if status in {404, 410} else 0
            listing["last_checked"] = now
            changed = misses >= 2 and listing["status"] != "removed"
            if changed:
                listing["status"] = "removed"
                wire = {key: value for key, value in listing.items() if key != "photo_hash"}
                self.db.execute("INSERT INTO queue(id,payload) VALUES(?,?)", (identifier, encode(wire)))
            self.db.execute("UPDATE catalog SET payload=?,fingerprint=?,misses=? WHERE id=?", (encode(listing), fingerprint(listing), misses, identifier))
            return changed

    def backup(self, path):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(target)) as snapshot:
            self.db.backup(snapshot)

    def progress(self, key, value=None):
        if value is not None:
            with self.db:
                self.db.execute("INSERT INTO progress(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, encode(value)))
            return value
        found = self.db.execute("SELECT value FROM progress WHERE key=?", (key,)).fetchone()
        return json.loads(found["value"]) if found else None
