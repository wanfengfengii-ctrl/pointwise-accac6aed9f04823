"""Version-one effective/knowledge-time observation store."""
import hashlib
import json
import sqlite3

I64_MIN = -(1 << 63)
I64_MAX = (1 << 63) - 1

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)

def integer(value):
    return type(value) is int and I64_MIN <= value <= I64_MAX

def text(value, nonempty=False):
    if not isinstance(value, str) or (nonempty and not value):
        return False
    try:
        value.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False

def validate(request, rows):
    if not text(request, True) or not isinstance(rows, list):
        raise ValueError("INPUT")
    seen = set()
    for row in rows:
        if not isinstance(row, list) or len(row) != 7:
            raise ValueError("INPUT")
        rid, rev, entity, start, end, value, deleted = row
        if (not text(rid, True) or not integer(rev) or rev <= 0
                or not text(entity, True) or not integer(start)
                or (end is not None and (not integer(end) or end <= start))
                or (value is not None and not text(value))
                or type(deleted) is not bool or rid in seen):
            raise ValueError("INPUT")
        seen.add(rid)

DDL = """
CREATE TABLE IF NOT EXISTS meta(
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    version INTEGER NOT NULL,
    head INTEGER NOT NULL);
INSERT OR IGNORE INTO meta VALUES(1,1,0);
CREATE TABLE IF NOT EXISTS batches(
    seq INTEGER PRIMARY KEY,
    accepted_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS receipts(
    request TEXT PRIMARY KEY COLLATE BINARY,
    digest TEXT NOT NULL,
    reply_json TEXT NOT NULL);
"""

class Store:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(DDL)
        if self.db.execute("SELECT version FROM meta").fetchone()[0] != 1:
            self.db.close()
            raise RuntimeError("UNSUPPORTED_SCHEMA")

    @property
    def head(self):
        return self.db.execute("SELECT head FROM meta WHERE singleton=1").fetchone()[0]

    def close(self):
        self.db.close()

    def _state(self, known):
        state = {}
        for (payload,) in self.db.execute(
                "SELECT accepted_json FROM batches WHERE seq<=? ORDER BY seq", (known,)):
            for row in json.loads(payload):
                state[row[0]] = row
        return state

    def apply(self, request, rows, fail_at=None):
        validate(request, rows)
        digest = hashlib.sha256(canonical(rows).encode("utf-8")).hexdigest()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            previous = self.db.execute(
                "SELECT digest,reply_json FROM receipts WHERE request=?", (request,)).fetchone()
            if previous:
                if previous[0] != digest:
                    raise ValueError("REQUEST_CONFLICT")
                result = json.loads(previous[1])
                self.db.execute("COMMIT")
                return result
            head = self.head
            state = self._state(head)
            accepted = []
            for row in rows:
                old = state.get(row[0])
                if old and row[1] == old[1] and row != old:
                    raise ValueError("REVISION_CONFLICT")
                if old is None or row[1] > old[1]:
                    accepted.append(row)
            seq = head + 1
            self.db.execute("INSERT INTO batches VALUES(?,?)", (seq, canonical(accepted)))
            if fail_at == "after_log":
                raise RuntimeError("INJECTED")
            result = {"seq": seq, "accepted": sorted(row[0] for row in accepted)}
            self.db.execute("UPDATE meta SET head=? WHERE singleton=1", (seq,))
            self.db.execute("INSERT INTO receipts VALUES(?,?,?)",
                            (request, digest, canonical(result)))
            if fail_at == "after_receipt":
                raise RuntimeError("INJECTED")
            self.db.execute("COMMIT")
            return result
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def read(self, valid_at, known_at=None):
        self.db.execute("BEGIN")
        try:
            head = self.head
            known = head if known_at is None else known_at
            if not integer(valid_at) or not integer(known) or not 0 <= known <= head:
                raise ValueError("INPUT")
            winners = {}
            for row in self._state(known).values():
                rid, rev, entity, start, end, value, deleted = row
                if deleted or valid_at < start or (end is not None and valid_at >= end):
                    continue
                old = winners.get(entity)
                if old is None or (rev, rid) > (old[1], old[0]):
                    winners[entity] = row
            result = [[entity, row[5], row[0], row[1]]
                      for entity, row in sorted(winners.items())]
            self.db.execute("COMMIT")
            return result
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
