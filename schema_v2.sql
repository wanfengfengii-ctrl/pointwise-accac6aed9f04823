-- Destination schema. Populate it and adapt Store; no migration is implemented here.
-- meta and receipts retain their v1 definitions and exact receipt contents.
CREATE TABLE source_versions(
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
CREATE INDEX source_by_entity ON source_versions(entity,learned);
CREATE TABLE winner_segments(
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
CREATE INDEX segments_by_entity ON winner_segments(entity,v_from,k_from);
CREATE INDEX segments_by_knowledge ON winner_segments(k_from,k_to);
