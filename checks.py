import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from ledger import Store
from samples import BATCHES, populate

ROOT = Path(__file__).resolve().parent

def expected(batches, valid, known):
    # Independent finite reference: choose version BEFORE filtering its interval.
    versions = {}
    for _, rows in batches[:known]:
        for r in rows:
            if r[0] not in versions or r[1] > versions[r[0]][1]:
                versions[r[0]] = r
    candidates = {}
    for r in versions.values():
        if not r[6] and r[3] <= valid and (r[4] is None or valid < r[4]):
            candidates.setdefault(r[2], []).append(r)
    answer = []
    for entity, rows in sorted(candidates.items()):
        r = max(rows, key=lambda x: (x[1], x[0]))
        answer.append([entity, r[5], r[0], r[1]])
    return answer

def dump(db):
    return list(db.iterdump())

def must_fail(fn, error, message):
    try:
        fn()
    except error as exc:
        assert str(exc) == message, (str(exc), message)
    else:
        raise AssertionError("expected " + message)

def check_views(store, batches):
    endpoints = {r[3] for _, rows in batches for r in rows}
    endpoints.update(r[4] for _, rows in batches for r in rows if r[4] is not None)
    points = endpoints | {min(endpoints) - 1, max(endpoints) + 1}
    for known in range(len(batches) + 1):
        for t in sorted(points):
            actual = store.read(t, known)
            wanted = expected(batches, t, known)
            assert actual == wanted, (known, t, wanted, actual)

def normal():
    store = Store()
    replies = populate(store)
    assert replies == [
        {"seq": 1, "accepted": ["a", "b", "c"]},
        {"seq": 2, "accepted": ["a", "c"]},
        {"seq": 3, "accepted": ["a"]},
        {"seq": 4, "accepted": ["z"]},
        {"seq": 5, "accepted": ["b"]},
        {"seq": 6, "accepted": ["b"]},
        {"seq": 7, "accepted": []},
    ]
    check_views(store, BATCHES)
    assert store.read(25, 1) == [
        ["basin", None, "b", 2], ["coast", "sea", "c", 1]]
    assert store.read(5, 2) == []
    assert store.read(27, 5) == [["basin", "shadow", "z", 1]]
    before = dump(store.db)
    assert store.apply(*BATCHES[0]) == replies[0]
    assert dump(store.db) == before
    must_fail(lambda: store.apply("initial", []), ValueError, "REQUEST_CONFLICT")
    bad = [["z", 1, "basin", 25, 35, "different", False]]
    must_fail(lambda: store.apply("conflict", bad), ValueError, "REVISION_CONFLICT")
    fresh = [["new", 9, "basin", -20, None, "new", False]]
    for hook in ("after_log", "after_receipt"):
        must_fail(lambda: store.apply("retry", fresh, fail_at=hook),
                  RuntimeError, "INJECTED")
        assert dump(store.db) == before
    assert store.apply("retry", fresh) == {"seq": 8, "accepted": ["new"]}
    check_views(store, BATCHES + [("retry", fresh)])
    before = dump(store.db)
    must_fail(lambda: store.apply("bad", [["x", True, "a", 0, 1, None, False]]),
              ValueError, "INPUT")
    must_fail(lambda: store.read(0, 99), ValueError, "INPUT")
    assert dump(store.db) == before
    store.close()
    # Verify the frozen, executable v1 database sample independently of Store.
    old = sqlite3.connect(":memory:")
    old.executescript((ROOT / "seed.sql").read_text(encoding="utf-8"))
    assert old.execute("SELECT version,head FROM meta").fetchone() == (1, 7)
    assert old.execute("SELECT count(*) FROM batches").fetchone()[0] == 7
    assert old.execute("SELECT count(*) FROM receipts").fetchone()[0] == 7
    old.close()
    print("NORMAL_REGRESSION_OK")

def check_segments(store, batches):
    db = store.db
    segments = db.execute(
        "SELECT entity,v_from,v_to,k_from,k_to,winner,winner_rev,value_json "
        "FROM winner_segments").fetchall()
    points = {r[3] for _, rows in batches for r in rows}
    points.update(r[4] for _, rows in batches for r in rows if r[4] is not None)
    points.update(s[1] for s in segments)
    points.update(s[2] for s in segments if s[2] is not None)
    points |= {min(points) - 1, max(points) + 1}
    for known in range(len(batches) + 1):
        for t in sorted(points):
            rows = db.execute(
                "SELECT entity,value_json,winner,winner_rev FROM winner_segments "
                "WHERE v_from<=? AND (v_to IS NULL OR ?<v_to) "
                "AND k_from<=? AND (k_to IS NULL OR ?<k_to) ORDER BY entity",
                (t, t, known, known)).fetchall()
            actual = [[e, json.loads(v), rid, rev] for e, v, rid, rev in rows]
            assert actual == expected(batches, t, known), ("rectangles", known, t, actual)
    for entity, start, end, first, last, rid, rev, value_json in segments:
        assert 1 <= first <= len(batches)
        assert last is None or first < last <= len(batches)
        signature = [entity, json.loads(value_json), rid, rev]
        for t in sorted(p for p in points if start <= p and (end is None or p < end)):
            old = [r for r in expected(batches, t, first - 1) if r[0] == entity]
            assert old != [signature], ("artificial opening", entity, t, first)
            if last is not None:
                later = [r for r in expected(batches, t, last) if r[0] == entity]
                assert later != [signature], ("artificial closing", entity, t, last)
    traced = []
    db.set_trace_callback(traced.append)
    store.read(27, 1)
    db.set_trace_callback(None)
    reads = [q.lower() for q in traced if q.lstrip().lower().startswith(("select", "with"))]
    assert any("winner_segments" in q for q in reads), traced
    assert not any("source_versions" in q or "batches" in q for q in reads), traced

def target():
    from migration import upgrade
    normal()
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "observations.db")
        old = sqlite3.connect(path, isolation_level=None)
        old.executescript((ROOT / "seed.sql").read_text(encoding="utf-8"))
        old_receipts = old.execute("SELECT * FROM receipts ORDER BY request").fetchall()
        before = dump(old)
        old.close()
        must_fail(lambda: upgrade(path, fail_at="before_publish"), RuntimeError, "INJECTED")
        raw = sqlite3.connect(path)
        assert dump(raw) == before
        raw.close()
        upgrade(path)
        store = Store(path)
        assert store.db.execute("SELECT version,head FROM meta").fetchone() == (2, 7)
        assert store.db.execute("SELECT * FROM receipts ORDER BY request").fetchall() == old_receipts
        assert store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='batches'").fetchall() == []
        assert store.db.execute("SELECT count(*) FROM source_versions").fetchone()[0] == 9
        assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
        check_views(store, BATCHES)
        check_segments(store, BATCHES)
        before = dump(store.db)
        store.close()
        upgrade(path)
        store = Store(path)
        assert dump(store.db) == before
        follow = [
            ["c", 7, "far", 0, None, None, False],
            ["b", 7, "coast", 0, 10, "back", False],
            ["q", 8, "basin", 25, 40, "Q", False],
        ]
        for hook in ("after_log", "after_receipt"):
            must_fail(lambda: store.apply("post", follow, fail_at=hook),
                      RuntimeError, "INJECTED")
            assert dump(store.db) == before
        store.apply("post", follow)
        assert store.apply(*BATCHES[0]) == {"seq": 1, "accepted": ["a", "b", "c"]}
        store.close()
        store = Store(path)
        all_batches = BATCHES + [("post", follow)]
        check_views(store, all_batches)
        check_segments(store, all_batches)
        assert store.read(27) == [["basin", "Q", "q", 8], ["far", None, "c", 7]]
        assert store.read(27, 6) == [["basin", "shadow", "z", 1]]
        print(json.dumps({"head": store.head, "current": store.read(27),
                          "old": store.read(27, 1)}, ensure_ascii=False))
        store.close()
    print("MIGRATION_COMPATIBILITY_OK")

def demo():
    store = Store()
    populate(store)
    print(json.dumps({"head": store.head, "views": [
        {"known": k, "valid": t, "rows": store.read(t, k)}
        for k, t in [(1, 25), (2, 5), (3, 25), (4, 27), (5, 27), (6, 35)]
    ]}, ensure_ascii=False, indent=2))
    store.close()

if __name__ == "__main__":
    {"normal": normal, "target": target, "demo": demo}[sys.argv[1]]()
