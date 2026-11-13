BEGIN TRANSACTION;
CREATE TABLE batches(
    seq INTEGER PRIMARY KEY,
    accepted_json TEXT NOT NULL);
INSERT INTO "batches" VALUES(1,'[["a",1,"basin",0,100,"low",false],["b",2,"basin",20,40,null,false],["c",1,"coast",-10,null,"sea",false]]');
INSERT INTO "batches" VALUES(2,'[["a",3,"basin",10,70,"high",false],["c",2,"basin",60,90,"moved",false]]');
INSERT INTO "batches" VALUES(3,'[["a",4,"basin",10,70,"unused",true]]');
INSERT INTO "batches" VALUES(4,'[["z",1,"basin",25,35,"shadow",false]]');
INSERT INTO "batches" VALUES(5,'[["b",5,"basin",30,50,"revived",false]]');
INSERT INTO "batches" VALUES(6,'[["b",6,"coast",-5,null,"gone",true]]');
INSERT INTO "batches" VALUES(7,'[]');
CREATE TABLE meta(
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    version INTEGER NOT NULL,
    head INTEGER NOT NULL);
INSERT INTO "meta" VALUES(1,1,7);
CREATE TABLE receipts(
    request TEXT PRIMARY KEY COLLATE BINARY,
    digest TEXT NOT NULL,
    reply_json TEXT NOT NULL);
INSERT INTO "receipts" VALUES('initial','fa2d8acf8655aab9da4cf2065f4d3a82efca6c6cc82f486b10de90a445c63bae','{"accepted":["a","b","c"],"seq":1}');
INSERT INTO "receipts" VALUES('move','831e8395c8853b5a9ea58142d3502fd1d5cc4b46b985909be08b84aa8ae9ddeb','{"accepted":["a","c"],"seq":2}');
INSERT INTO "receipts" VALUES('withdraw','dc3b88a0aa9a0b7878724c8696f3da0454b4fc8f3385468a0540ddb78dd648ec','{"accepted":["a"],"seq":3}');
INSERT INTO "receipts" VALUES('hidden','ee8bd68d8e670c3c516ca23d4d6f2f828b70650d4429434edd87b433653ef01d','{"accepted":["z"],"seq":4}');
INSERT INTO "receipts" VALUES('replace','12230cdebc5b889f0103552cc96b0d6b33f0b2f6f101073ba0f8638971889096','{"accepted":["b"],"seq":5}');
INSERT INTO "receipts" VALUES('tombstone-move','df091970cd054b4343c836cd96fd732b793406f02662f5f6479dee1403f4c6a8','{"accepted":["b"],"seq":6}');
INSERT INTO "receipts" VALUES('empty','4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945','{"accepted":[],"seq":7}');
COMMIT;
