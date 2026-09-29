-- IPTV Spider Workers - D1 schema (run via: wrangler d1 execute iptv-spider --local --file=./migrations/0001_init.sql)
CREATE TABLE IF NOT EXISTS channels (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    url           TEXT NOT NULL,
    grp           TEXT DEFAULT '',
    logo          TEXT DEFAULT '',
    tvg_id        TEXT DEFAULT '',
    tvg_name      TEXT DEFAULT '',
    status        TEXT DEFAULT 'unknown',
    latency_ms    INTEGER,
    http_status   INTEGER,
    last_checked  REAL,
    source        TEXT DEFAULT 'manual',
    note          TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_channels_grp ON channels(grp);
CREATE INDEX IF NOT EXISTS idx_channels_status ON channels(status);
