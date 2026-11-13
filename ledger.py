"""Bitemporal observation store: v1 replay log and v2 interval tables."""
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

V1_DDL = """
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

# Kept identical in shape to schema_v2.sql; the migration executes that file.
V2_DDL = """
CREATE TABLE IF NOT EXISTS meta(
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    version INTEGER NOT NULL,
    head INTEGER NOT NULL);
INSERT OR IGNORE INTO meta VALUES(1,2,0);
CREATE TABLE IF NOT EXISTS source_versions(
    rid TEXT NOT NULL COLLATE BINARY,
    rev INTEGER NOT NULL CHECK(rev>0),
    learned INTEGER NOT NULL CHECK(learned>0),
    entity TEXT NOT NULL COLLATE BINARY,
    v_from INTEGER NOT NULL,
    v_to INTEGER,
    value_json TEXT NOT NULL,
    deleted INTEGER NOT NULL CHECK(deleted IN (0,1)),
    PRIMARY KEY(rid,rev),
    CHECK(v_to IS NULL OR v_to>v_from)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS source_by_entity ON source_versions(entity,learned);
CREATE TABLE IF NOT EXISTS winner_segments(
    segment_id INTEGER PRIMARY KEY,
    entity TEXT NOT NULL COLLATE BINARY,
    v_from INTEGER NOT NULL,
    v_to INTEGER,
    k_from INTEGER NOT NULL CHECK(k_from>0),
    k_to INTEGER,
    winner TEXT NOT NULL COLLATE BINARY,
    winner_rev INTEGER NOT NULL,
    value_json TEXT NOT NULL,
    CHECK(v_to IS NULL OR v_to>v_from),
    CHECK(k_to IS NULL OR k_to>k_from),
    FOREIGN KEY(winner,winner_rev) REFERENCES source_versions(rid,rev)
);
CREATE INDEX IF NOT EXISTS segments_by_entity ON winner_segments(entity,v_from,k_from);
CREATE INDEX IF NOT EXISTS segments_by_knowledge ON winner_segments(k_from,k_to);
CREATE TABLE IF NOT EXISTS receipts(
    request TEXT PRIMARY KEY COLLATE BINARY,
    digest TEXT NOT NULL,
    reply_json TEXT NOT NULL);
"""


def entity_slabs(versions):
    """Constant valid-time slabs for one entity's candidate rids.

    Every version ever held by those rids contributes its endpoints, so the
    union of slabs always covers the entity's covered ranges even after a
    rid moves to another entity; a null-ended version adds an unbounded tail.
    """
    endpoints = set()
    unbounded = False
    for ver in versions:
        endpoints.add(ver[4])
        if ver[5] is None:
            unbounded = True
        else:
            endpoints.add(ver[5])
    ordered = sorted(endpoints)
    slabs = list(zip(ordered, ordered[1:]))
    if unbounded:
        slabs.append((ordered[-1], None))
    return slabs


def winner_at(versions, entity, valid_at, known):
    """Pure reference winner rule shared by migration and incremental writes.

    Per id keep only the highest rev accepted up to ``known`` (full-version
    replacement), then filter deletion, entity and interval, finally take the
    greatest (rev, rid). Versions are (rid, rev, learned, entity, v_from,
    v_to, value_json, deleted).
    """
    latest = {}
    for ver in versions:
        rid, rev, learned = ver[0], ver[1], ver[2]
        if learned > known:
            continue
        old = latest.get(rid)
        if old is None or rev > old[1]:
            latest[rid] = ver
    winner = None
    for ver in latest.values():
        _, rev, _, ver_entity, v_from, v_to, value_json, deleted = ver
        if deleted or ver_entity != entity:
            continue
        if valid_at < v_from or (v_to is not None and valid_at >= v_to):
            continue
        if winner is None or (rev, ver[0]) > (winner[1], winner[0]):
            winner = ver
    return winner


class Store:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA foreign_keys=ON")
        existed = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
        if existed:
            self.version = self.db.execute("SELECT version FROM meta").fetchone()[0]
            if self.version not in (1, 2):
                self.db.close()
                raise RuntimeError("UNSUPPORTED_SCHEMA")
            if self.version == 2:
                self.db.executescript(V2_DDL)
        else:
            # New stores are created directly at v2.
            self.db.executescript(V2_DDL)
            self.version = 2

    @property
    def head(self):
        return self.db.execute("SELECT head FROM meta WHERE singleton=1").fetchone()[0]

    def close(self):
        self.db.close()

    # -- v1 legacy path --------------------------------------------------
    def _state(self, known):
        state = {}
        for (payload,) in self.db.execute(
                "SELECT accepted_json FROM batches WHERE seq<=? ORDER BY seq", (known,)):
            for row in json.loads(payload):
                state[row[0]] = row
        return state

    def apply(self, request, rows, fail_at=None):
        validate(request, rows)
        if self.version == 1:
            return self._apply_v1(request, rows, fail_at)
        return self._apply_v2(request, rows, fail_at)

    def _apply_v1(self, request, rows, fail_at):
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

    # -- v2 interval-table path ------------------------------------------
    def _apply_v2(self, request, rows, fail_at):
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
            # Point lookups of each id's highest version for revision handling.
            accepted = []
            affected_entities = set()
            for row in rows:
                rid, rev, entity, start, end, value, deleted = row
                old = self.db.execute(
                    "SELECT rev,entity,v_from,v_to,value_json,deleted,learned,rid "
                    "FROM source_versions WHERE rid=? ORDER BY rev DESC LIMIT 1",
                    (rid,)).fetchone()
                if old is not None:
                    old_rev, old_entity, old_start, old_end, old_value_json, old_deleted, _, _ = old
                    if rev == old_rev:
                        old_row = [rid, old_rev, old_entity, old_start, old_end,
                                   json.loads(old_value_json), bool(old_deleted)]
                        if row != old_row:
                            raise ValueError("REVISION_CONFLICT")
                        continue
                    if rev < old_rev:
                        continue
                    affected_entities.add(old_entity)
                accepted.append(row)
                affected_entities.add(entity)
            seq = head + 1
            for row in accepted:
                rid, rev, entity, start, end, value, deleted = row
                self.db.execute(
                    "INSERT INTO source_versions(rid,rev,learned,entity,v_from,v_to,"
                    "value_json,deleted) VALUES(?,?,?,?,?,?,?,?)",
                    (rid, rev, seq, entity, start, end, canonical(value), 1 if deleted else 0))
            if fail_at == "after_log":
                raise RuntimeError("INJECTED")
            result = {"seq": seq, "accepted": sorted(row[0] for row in accepted)}
            self.db.execute("UPDATE meta SET head=? WHERE singleton=1", (seq,))
            for entity in affected_entities:
                self._refresh_entity(entity, seq)
            self.db.execute("INSERT INTO receipts VALUES(?,?,?)",
                            (request, digest, canonical(result)))
            if fail_at == "after_receipt":
                raise RuntimeError("INJECTED")
            self.db.execute("COMMIT")
            return result
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def _refresh_entity(self, entity, seq):
        """Rebuild only this entity's still-open rectangles at generation seq.

        Closed historical rectangles (k_to < seq) are never touched or
        rebuilt. The valid axis is cut into constant winner slabs from the
        entity's candidate versions. For each slab the winner immediately
        before (seq-1) and at (seq) the new generation is evaluated:

        * a pre-seq winner becomes a history rectangle closed at seq, reusing
          the opening generation (k_from) of whichever old rectangle covers
          the slab, so unchanged history never gains a knowledge boundary;
        * a winner visible at seq opens a rectangle from seq; if it is the
          same winner that was already visible before seq and the covering
          rectangle was open, its original opening generation is preserved.
        """
        versions = self.db.execute(
            "SELECT rid,rev,learned,entity,v_from,v_to,value_json,deleted "
            "FROM source_versions WHERE rid IN "
            "(SELECT rid FROM source_versions WHERE entity=?) "
            "ORDER BY rid,rev",
            (entity,)).fetchall()
        all_segments = self.db.execute(
            "SELECT segment_id,v_from,v_to,k_from,k_to,winner,winner_rev,value_json "
            "FROM winner_segments WHERE entity=? ORDER BY segment_id",
            (entity,)).fetchall()
        open_ids = {seg[0] for seg in all_segments if seg[4] is None}

        def covering(v_lo, v_hi):
            """Rectangle covering the slab and visible at generation seq-1.

            Prefer an open rectangle; otherwise the closed rectangle whose
            knowledge interval reaches seq-1. Falls back to any geometrically
            covering rectangle for opening-generation lookup.
            """
            geometric = []
            for seg in all_segments:
                _, s_lo, s_hi, _, _, _, _, _ = seg
                if s_lo > v_lo:
                    continue
                if s_hi is not None and (v_hi is None or s_hi < v_hi):
                    continue
                geometric.append(seg)
            for seg in geometric:
                if seg[0] in open_ids:
                    return seg
            for seg in geometric:
                if seg[4] == seq:
                    return seg
            return geometric[0] if geometric else None

        out = []
        for lo, hi in entity_slabs(versions):
            before = winner_at(versions, entity, lo, seq - 1)
            after = winner_at(versions, entity, lo, seq)
            sig_before = None if before is None else (
                before[0], before[1], before[6])
            sig_after = None if after is None else (
                after[0], after[1], after[6])
            old = covering(lo, hi)
            old_sig = None if old is None else (old[5], old[6], old[7])
            old_open = old is not None and old[0] in open_ids
            # An open rectangle whose winner stays the winner is inherited
            # open with its original opening generation; the valid axis is
            # only subdivided, so no knowledge boundary is introduced.
            if old_open and sig_after is not None and sig_after == old_sig:
                out.append((lo, hi, old[3], None,
                            sig_after[0], sig_after[1], sig_after[2]))
                continue
            # A slab not inherited open. Closed rectangles are immutable, so
            # any pre-seq history not already represented by a closed
            # rectangle starts where the covering closed rectangle ended.
            if sig_before is not None:
                if old is None:
                    history_from = 1
                elif old_open:
                    history_from = old[3]
                else:
                    history_from = old[4]  # k_to of the covering closed rect
                if history_from < seq:
                    out.append((lo, hi, history_from, seq,
                                sig_before[0], sig_before[1], sig_before[2]))
            if sig_after is not None:
                out.append((lo, hi, seq, None,
                            sig_after[0], sig_after[1], sig_after[2]))
        if open_ids:
            self.db.execute(
                "DELETE FROM winner_segments WHERE entity=? AND k_to IS NULL",
                (entity,))
        for lo, hi, k_from, k_to, winner, winner_rev, value_json in out:
            self.db.execute(
                "INSERT INTO winner_segments(entity,v_from,v_to,k_from,k_to,"
                "winner,winner_rev,value_json) VALUES(?,?,?,?,?,?,?,?)",
                (entity, lo, hi, k_from, k_to, winner, winner_rev, value_json))

    def read(self, valid_at, known_at=None):
        if self.version == 1:
            return self._read_v1(valid_at, known_at)
        return self._read_v2(valid_at, known_at)

    def _read_v1(self, valid_at, known_at):
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

    def _read_v2(self, valid_at, known_at):
        self.db.execute("BEGIN")
        try:
            head = self.head
            known = head if known_at is None else known_at
            if not integer(valid_at) or not integer(known) or not 0 <= known <= head:
                raise ValueError("INPUT")
            # Only meta and winner_segments are consulted: no source replay,
            # no per-generation snapshots, no point-by-point expansion.
            rows = self.db.execute(
                "SELECT entity,value_json,winner,winner_rev FROM winner_segments "
                "WHERE v_from<=? AND (v_to IS NULL OR ?<v_to) "
                "AND k_from<=? AND (k_to IS NULL OR ?<k_to) ORDER BY entity",
                (valid_at, valid_at, known, known)).fetchall()
            result = [[entity, json.loads(value_json), rid, rev]
                      for entity, value_json, rid, rev in rows]
            self.db.execute("COMMIT")
            return result
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
