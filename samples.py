"""Complete old-database constructor and its original request bodies."""
BATCHES = [
    ("initial", [
        ["a", 1, "basin", 0, 100, "low", False],
        ["b", 2, "basin", 20, 40, None, False],
        ["c", 1, "coast", -10, None, "sea", False],
    ]),
    ("move", [
        ["a", 3, "basin", 10, 70, "high", False],
        ["c", 2, "basin", 60, 90, "moved", False],
    ]),
    ("withdraw", [
        ["a", 4, "basin", 10, 70, "unused", True],
    ]),
    ("hidden", [
        ["z", 1, "basin", 25, 35, "shadow", False],
        ["b", 1, "basin", 0, None, "ignored", False],
    ]),
    ("replace", [
        ["b", 5, "basin", 30, 50, "revived", False],
    ]),
    ("tombstone-move", [
        ["b", 6, "coast", -5, None, "gone", True],
    ]),
    ("empty", []),
]

def populate(store):
    return [store.apply(request, rows) for request, rows in BATCHES]
