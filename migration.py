"""Atomic v1 -> v2 migration for the bitemporal observation store."""
import json
import sqlite3
from pathlib import Path

from ledger import canonical, entity_slabs, winner_at

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema_v2.sql"


def _schema_statements():
    statements = []
    for line in _SCHEMA_PATH.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("--"):
            continue
        statements.append(line)
    text = "\n".join(statements)
    return [stmt.strip() for stmt in text.split(";") if stmt.strip()]


def _build_segments(conn, head):
    """Build winner rectangles from accepted source versions.

    Valid time is cut into constant slabs by every version endpoint; for
    each slab the winner is evaluated per knowledge generation and run-length
    encoded. Rectangles open/close only where the visible winner actually
    changes, so unchanged history keeps its original knowledge boundary.
    """
    versions = conn.execute(
        "SELECT rid,rev,learned,entity,v_from,v_to,value_json,deleted "
        "FROM source_versions ORDER BY rid,rev").fetchall()
    entities = sorted({ver[3] for ver in versions})
    rectangles = []
    for entity in entities:
        entity_versions = [ver for ver in versions if ver[3] == entity]
        for lo, hi in entity_slabs(entity_versions):
            run_sig = None
            run_start = None
            for k in range(1, head + 1):
                winner = winner_at(versions, entity, lo, k)
                sig = None if winner is None else (winner[0], winner[1], winner[6])
                if sig != run_sig:
                    if run_sig is not None:
                        rectangles.append((entity, lo, hi, run_start, k,
                                           run_sig[0], run_sig[1], run_sig[2]))
                    run_sig = sig
                    run_start = k
            if run_sig is not None:
                rectangles.append((entity, lo, hi, run_start, None,
                                   run_sig[0], run_sig[1], run_sig[2]))
    return rectangles


def upgrade(path, fail_at=None):
    """Upgrade a v1 database at ``path`` to the v2 interval schema.

    The whole upgrade runs in one transaction: any failure (including the
    injected before_publish fault) rolls back to the untouched v1 schema,
    data and receipts. Upgrading an already-v2 database is a no-op.
    """
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
        if row is None:
            raise RuntimeError("UNSUPPORTED_SCHEMA")
        version = conn.execute("SELECT version FROM meta WHERE singleton=1").fetchone()[0]
        if version == 2:
            # Repeated upgrades have no side effects.
            return
        if version != 1:
            raise RuntimeError("UNSUPPORTED_SCHEMA")
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _schema_statements():
                conn.execute(statement)
            for seq, payload in conn.execute(
                    "SELECT seq,accepted_json FROM batches ORDER BY seq"):
                for rid, rev, entity, start, end, value, deleted in json.loads(payload):
                    conn.execute(
                        "INSERT INTO source_versions(rid,rev,learned,entity,"
                        "v_from,v_to,value_json,deleted) VALUES(?,?,?,?,?,?,?,?)",
                        (rid, rev, seq, entity, start, end,
                         canonical(value), 1 if deleted else 0))
            head = conn.execute("SELECT head FROM meta WHERE singleton=1").fetchone()[0]
            for (entity, lo, hi, k_from, k_to, winner, winner_rev,
                 value_json) in _build_segments(conn, head):
                conn.execute(
                    "INSERT INTO winner_segments(entity,v_from,v_to,k_from,k_to,"
                    "winner,winner_rev,value_json) VALUES(?,?,?,?,?,?,?,?)",
                    (entity, lo, hi, k_from, k_to, winner, winner_rev, value_json))
            if fail_at == "before_publish":
                raise RuntimeError("INJECTED")
            # Publication is the last step: nothing is visible as v2 before it.
            conn.execute("UPDATE meta SET version=2 WHERE singleton=1")
            conn.execute("DROP TABLE batches")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
