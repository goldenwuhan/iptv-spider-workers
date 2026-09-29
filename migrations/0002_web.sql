-- 0002_web.sql -- tables backing the web console + auto collector.
-- Safe to run repeatedly (IF NOT EXISTS).

-- Source servers discovered from the upstream feed
-- (api.cqshushu.com hotel.php / multicast.php / migu.php).
CREATE TABLE IF NOT EXISTS sources (
    id            TEXT PRIMARY KEY,          -- sha1(kind|addr)[:16]
    kind          TEXT NOT NULL,             -- hotel | multicast | migu
    addr          TEXT NOT NULL,             -- "110.72.103.127:808"
    ip            TEXT NOT NULL,
    port          INTEGER,
    name          TEXT DEFAULT '',           -- region/name from upstream
    program_count INTEGER DEFAULT 0,         -- program count reported upstream
    up_status     TEXT DEFAULT 'unknown',    -- new | alive | fail | unknown
    online_time   TEXT DEFAULT '',
    update_time   TEXT DEFAULT '',
    token         TEXT DEFAULT '',           -- cqshushu ?s= token
    channel_count INTEGER DEFAULT 0,         -- channels actually collected
    failed_times  INTEGER DEFAULT 0,         -- consecutive validation failures
    fetched_at    REAL DEFAULT 0,
    created_at    REAL DEFAULT 0,
    UNIQUE (kind, addr)
);

-- Simple key/value settings (admin password, session secret, collector opts).
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Activity log shown on the "日志" page.
CREATE TABLE IF NOT EXISTS logs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL,
    level   TEXT DEFAULT 'info',             -- info | warn | error
    action  TEXT DEFAULT '',                 -- collect | validate | login | ...
    message TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_sources_kind   ON sources(kind);
CREATE INDEX IF NOT EXISTS idx_sources_update ON sources(update_time);
CREATE INDEX IF NOT EXISTS idx_logs_ts        ON logs(ts DESC);
