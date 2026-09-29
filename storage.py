"""storage.py -- Storage abstraction for the Workers IPTV core.

Two implementations share the same async interface used by router.py:
  * MemoryStore  -- in-memory dict + optional JSON file (used for local dev and
                   as a fallback when no D1 binding is configured).
  * D1Store      -- backed by a Cloudflare D1 database binding (env.DB).

get_store(env) returns a D1Store when a D1 binding is present, otherwise a
MemoryStore (optionally persisting to KV or a JSON file depending on env).
"""

from __future__ import annotations

import json
import os
from typing import Optional

from iptv_core import Channel


# ---------------------------------------------------------------------------
# In-memory store (with optional JSON persistence for local development)
# ---------------------------------------------------------------------------

class MemoryStore:
    def __init__(self, path: Optional[str] = None):
        self._channels: dict[str, Channel] = {}
        # Web-console data (sources / settings / logs) persists alongside the
        # channels so a single JSON file holds everything in local dev.
        self._sources: dict[str, dict] = {}
        self._settings: dict[str, str] = {}
        self._logs: list[dict] = []
        self._path = path
        self._lock = None  # lazy import asyncio only when needed
        if path and os.path.exists(path):
            self._load()

    def _lock_obj(self):
        if self._lock is None:
            import asyncio
            self._lock = asyncio.Lock()
        return self._lock

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        # The original format was a bare list of channels; still accept it.
        if isinstance(data, list):
            data = {"channels": data}
        if not isinstance(data, dict):
            return
        for d in data.get("channels", []) or []:
            try:
                self._channels[d["id"]] = Channel(**d)
            except (KeyError, TypeError):
                continue
        for s in data.get("sources", []) or []:
            if isinstance(s, dict) and s.get("id"):
                self._sources[s["id"]] = s
        self._settings.update(data.get("settings") or {})
        self._logs = list(data.get("logs") or [])

    def _save(self) -> None:
        if not self._path:
            return
        try:
            tmp = self._path + ".tmp"
            payload = {
                "channels": [c.to_dict() for c in self._channels.values()],
                "sources": list(self._sources.values()),
                "settings": self._settings,
                "logs": self._logs[-500:],
            }
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, self._path)
        except OSError:
            # Persistence is best-effort; never break a request over it.
            pass

    async def add(self, ch: Channel) -> None:
        async with self._lock_obj():
            self._channels[ch.id] = ch
            self._save()

    async def add_many(self, chs: list[Channel]) -> int:
        added = 0
        async with self._lock_obj():
            for ch in chs:
                if ch.id not in self._channels:
                    added += 1
                self._channels[ch.id] = ch
            self._save()
        return added

    async def list(self, group: Optional[str] = None,
                   status: Optional[str] = None) -> list[Channel]:
        async with self._lock_obj():
            items = list(self._channels.values())
        if group:
            items = [c for c in items if c.group == group]
        if status:
            items = [c for c in items if c.status == status]
        return items

    async def get(self, cid: str) -> Optional[Channel]:
        async with self._lock_obj():
            return self._channels.get(cid)

    async def delete(self, cid: str) -> bool:
        async with self._lock_obj():
            if cid in self._channels:
                del self._channels[cid]
                self._save()
                return True
        return False

    async def update_status(self, cid: str, status: str,
                           latency_ms: Optional[int],
                           http_status: Optional[int]) -> bool:
        async with self._lock_obj():
            ch = self._channels.get(cid)
            if not ch:
                return False
            ch.status = status
            ch.latency_ms = latency_ms
            ch.http_status = http_status
            ch.last_checked = __import__("time").time()
            self._save()
            return True

    async def clear(self) -> None:
        async with self._lock_obj():
            self._channels.clear()
            self._save()

    async def groups(self) -> list[str]:
        async with self._lock_obj():
            return sorted({c.group or "默认" for c in self._channels.values()})

    # --- web console: sources / settings / logs -------------------------

    async def upsert_sources(self, rows: list[dict]) -> int:
        async with self._lock_obj():
            for row in rows:
                prev = self._sources.get(row["id"])
                if prev:
                    row = dict(row)
                    row["created_at"] = prev.get("created_at") or row.get("created_at")
                    row["failed_times"] = prev.get("failed_times", 0)
                    row["channel_count"] = max(int(row.get("channel_count") or 0),
                                               int(prev.get("channel_count") or 0))
                self._sources[row["id"]] = dict(row)
            self._save()
        return len(rows)

    async def list_sources(self, kind: Optional[str] = None) -> list[dict]:
        async with self._lock_obj():
            items = [dict(s) for s in self._sources.values()]
        if kind:
            items = [s for s in items if s.get("kind") == kind]
        return sorted(items, key=lambda s: (s.get("kind", ""),
                                           -int(s.get("program_count") or 0)))

    async def get_source(self, sid: str):
        async with self._lock_obj():
            row = self._sources.get(sid)
            return dict(row) if row else None

    async def mark_source_channels(self, sid: str, count: int) -> None:
        async with self._lock_obj():
            row = self._sources.get(sid)
            if row:
                row["channel_count"] = int(count)
                row["fetched_at"] = __import__("time").time()
                self._save()

    async def delete_source(self, sid: str) -> bool:
        async with self._lock_obj():
            if sid in self._sources:
                del self._sources[sid]
                self._save()
                return True
        return False

    async def source_stats(self) -> dict:
        async with self._lock_obj():
            out: dict[str, dict] = {}
            for s in self._sources.values():
                d = out.setdefault(s.get("kind", "?"),
                                   {"count": 0, "programs": 0, "channels": 0,
                                    "statuses": {}})
                d["count"] += 1
                d["programs"] += int(s.get("program_count") or 0)
                d["channels"] += int(s.get("channel_count") or 0)
                st = s.get("up_status", "unknown")
                d["statuses"][st] = d["statuses"].get(st, 0) + 1
        return out

    async def get(self, key: str):
        async with self._lock_obj():
            return self._settings.get(key)

    async def set(self, key: str, value: str) -> None:
        async with self._lock_obj():
            self._settings[key] = value
            self._save()

    async def log(self, action: str, message: str = "",
                  level: str = "info") -> None:
        async with self._lock_obj():
            self._logs.append({"ts": __import__("time").time(),
                               "level": level, "action": action,
                               "message": message})
            del self._logs[:-500]
            self._save()

    async def list_logs(self, limit: int = 200) -> list[dict]:
        async with self._lock_obj():
            return list(reversed(self._logs[-int(limit):]))

    async def clear_logs(self) -> None:
        async with self._lock_obj():
            self._logs = []
            self._save()


# ---------------------------------------------------------------------------
# D1-backed store (Cloudflare Workers)
# ---------------------------------------------------------------------------

def _to_py(obj):
    """Convert a Pyodide JS proxy into plain Python where possible.

    Bindings (D1, KV, ...) return JavaScript objects through the FFI as
    ``pyodide.ffi.JsProxy`` instances -- NOT Python dicts. On those, ``.get()``,
    ``in`` and iteration either behave oddly or raise
    ``TypeError: 'pyodide.ffi.JsProxy' object is not iterable``.
    Normalising with ``to_py()`` first keeps the rest of the code plain Python.
    """
    if obj is None or isinstance(obj, (dict, list, str, int, float, bool)):
        return obj
    to_py = getattr(obj, "to_py", None)
    if callable(to_py):
        try:
            return to_py()
        except Exception:
            pass
    return obj


def _rows(res) -> list:
    """Normalise a D1 ``.all()`` result into a list of row dicts."""
    res = _to_py(res)
    if isinstance(res, dict):
        return res.get("results") or []
    if isinstance(res, list):
        return res
    try:  # fall back to attribute access on the JS object
        return list(getattr(res, "results", None) or [])
    except Exception:
        return []


class D1Store:
    """Async store backed by a Cloudflare D1 binding (env.DB).

    Expected schema (see README.md for the wrangler / SQL):

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
    """

    def __init__(self, db):
        self.db = db  # Workers D1 binding

    @staticmethod
    def _row_to_channel(row) -> Channel:
        row = _to_py(row) or {}
        return Channel(
            id=row.get("id", ""),
            name=row.get("name", ""),
            url=row.get("url", ""),
            group=row.get("grp", ""),
            logo=row.get("logo", ""),
            tvg_id=row.get("tvg_id", ""),
            tvg_name=row.get("tvg_name", ""),
            status=row.get("status", "unknown"),
            latency_ms=row.get("latency_ms"),
            http_status=row.get("http_status"),
            last_checked=row.get("last_checked"),
            source=row.get("source", "manual"),
            note=row.get("note", ""),
        )

    @staticmethod
    def _bind_args(ch: Channel) -> tuple:
        """Concrete (never None) values for the channels INSERT.

        D1 rejects Python None: it crosses the FFI as JS `undefined` and fails
        with `D1_TYPE_ERROR: Type 'undefined' not supported for value`.
        """
        return (
            ch.id, ch.name, ch.url, ch.group or "", ch.logo or "",
            ch.tvg_id or "", ch.tvg_name or "", ch.status or "unknown",
            ch.latency_ms if ch.latency_ms is not None else 0,
            ch.http_status if ch.http_status is not None else 0,
            ch.last_checked if ch.last_checked is not None else 0.0,
            ch.source or "manual", ch.note or "",
        )

    async def add(self, ch: Channel) -> None:
        await self.db.prepare(
            "INSERT OR REPLACE INTO channels "
            "(id,name,url,grp,logo,tvg_id,tvg_name,status,latency_ms,"
            "http_status,last_checked,source,note) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?)"
        ).bind(*self._bind_args(ch)).run()

    async def add_many(self, chs: list[Channel]) -> int:
        """Insert many channels via D1's ``batch()``.

        A ~300 channel playlist otherwise costs one DB round trip per row,
        which made a single collection take 200+ seconds. Batching in chunks
        of 50 turns that into a handful of calls.
        """
        if not chs:
            return 0
        stmt = self.db.prepare(
            "INSERT OR REPLACE INTO channels "
            "(id,name,url,grp,logo,tvg_id,tvg_name,status,latency_ms,"
            "http_status,last_checked,source,note) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?)"
        )
        try:
            for i in range(0, len(chs), 50):
                batch = [stmt.bind(*self._bind_args(ch))
                         for ch in chs[i:i + 50]]
                await self.db.batch(batch)
        except Exception:
            # Batch unsupported or quota exceeded: degrade to one-by-one.
            for ch in chs:
                try:
                    await self.add(ch)
                except Exception:
                    pass
        return len(chs)

    async def list(self, group: Optional[str] = None,
                   status: Optional[str] = None) -> list[Channel]:
        if group and status:
            res = await self.db.prepare(
                "SELECT * FROM channels WHERE grp=? AND status=? ORDER BY name"
            ).bind(group, status).all()
        elif group:
            res = await self.db.prepare(
                "SELECT * FROM channels WHERE grp=? ORDER BY name"
            ).bind(group).all()
        elif status:
            res = await self.db.prepare(
                "SELECT * FROM channels WHERE status=? ORDER BY name"
            ).bind(status).all()
        else:
            res = await self.db.prepare(
                "SELECT * FROM channels ORDER BY name"
            ).all()
        return [self._row_to_channel(r) for r in _rows(res)]

    async def get(self, cid: str) -> Optional[Channel]:
        row = _to_py(await self.db.prepare(
            "SELECT * FROM channels WHERE id=?"
        ).bind(cid).first())
        return self._row_to_channel(row) if row else None

    async def delete(self, cid: str) -> bool:
        res = _to_py(await self.db.prepare(
            "DELETE FROM channels WHERE id=?"
        ).bind(cid).run())
        return bool(res.get("success")) if isinstance(res, dict) else True

    async def update_status(self, cid: str, status: str,
                           latency_ms: Optional[int],
                           http_status: Optional[int]) -> bool:
        import time
        res = _to_py(await self.db.prepare(
            "UPDATE channels SET status=?, latency_ms=?, http_status=?, "
            "last_checked=? WHERE id=?"
        ).bind(status or "unknown",
               latency_ms if latency_ms is not None else 0,
               http_status if http_status is not None else 0,
               time.time(), cid).run())
        return bool(res.get("success")) if isinstance(res, dict) else True

    async def clear(self) -> None:
        await self.db.prepare("DELETE FROM channels").run()

    async def groups(self) -> list[str]:
        res = await self.db.prepare(
            "SELECT DISTINCT grp FROM channels ORDER BY grp"
        ).all()
        return sorted({(_to_py(r) or {}).get("grp") or "默认"
                       for r in _rows(res)})

    # --- web console: sources / settings / logs -------------------------

    async def upsert_sources(self, rows: list[dict]) -> int:
        stmt = self.db.prepare(
            "INSERT INTO sources (id,kind,addr,ip,port,name,program_count,"
            "up_status,online_time,update_time,token,channel_count,"
            "failed_times,fetched_at,created_at) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "name=excluded.name, program_count=excluded.program_count, "
            "up_status=excluded.up_status, online_time=excluded.online_time, "
            "update_time=excluded.update_time, token=excluded.token, "
            "fetched_at=excluded.fetched_at"
        )
        for row in rows:
            await stmt.bind(
                row["id"], row["kind"], row["addr"], row["ip"],
                int(row.get("port") or 0), row.get("name") or "",
                int(row.get("program_count") or 0),
                row.get("up_status") or "unknown",
                row.get("online_time") or "", row.get("update_time") or "",
                row.get("token") or "",
                int(row.get("channel_count") or 0),
                int(row.get("failed_times") or 0),
                float(row.get("fetched_at") or 0),
                float(row.get("created_at") or 0),
            ).run()
        return len(rows)

    async def list_sources(self, kind: Optional[str] = None) -> list[dict]:
        if kind:
            res = await self.db.prepare(
                "SELECT * FROM sources WHERE kind=? ORDER BY program_count DESC"
            ).bind(kind).all()
        else:
            res = await self.db.prepare(
                "SELECT * FROM sources ORDER BY kind, program_count DESC"
            ).all()
        return [_to_py(r) for r in _rows(res)]

    async def get_source(self, sid: str):
        row = _to_py(await self.db.prepare(
            "SELECT * FROM sources WHERE id=?"
        ).bind(sid).first())
        return row

    async def mark_source_channels(self, sid: str, count: int) -> None:
        import time
        await self.db.prepare(
            "UPDATE sources SET channel_count=?, fetched_at=? WHERE id=?"
        ).bind(int(count), time.time(), sid).run()

    async def delete_source(self, sid: str) -> bool:
        res = _to_py(await self.db.prepare(
            "DELETE FROM sources WHERE id=?"
        ).bind(sid).run())
        return bool(res.get("success")) if isinstance(res, dict) else True

    async def source_stats(self) -> dict:
        res = await self.db.prepare(
            "SELECT kind, COUNT(*) AS n, SUM(program_count) AS p, "
            "SUM(channel_count) AS c FROM sources GROUP BY kind"
        ).all()
        out: dict[str, dict] = {}
        for raw in _rows(res):
            r = _to_py(raw) or {}
            out[r.get("kind", "?")] = {
                "count": int(r.get("n") or 0),
                "programs": int(r.get("p") or 0),
                "channels": int(r.get("c") or 0),
            }
        return out

    async def get(self, key: str):
        row = _to_py(await self.db.prepare(
            "SELECT value FROM settings WHERE key=?"
        ).bind(key).first())
        return row.get("value") if row else None

    async def set(self, key: str, value: str) -> None:
        await self.db.prepare(
            "INSERT INTO settings (key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        ).bind(key, value if value is not None else "").run()

    async def log(self, action: str, message: str = "",
                  level: str = "info") -> None:
        import time
        await self.db.prepare(
            "INSERT INTO logs (ts,level,action,message) VALUES (?,?,?,?)"
        ).bind(time.time(), level or "info", action or "",
               message or "").run()

    async def list_logs(self, limit: int = 200) -> list[dict]:
        res = await self.db.prepare(
            "SELECT * FROM logs ORDER BY ts DESC LIMIT ?"
        ).bind(int(limit)).all()
        return [_to_py(r) for r in _rows(res)]

    async def clear_logs(self) -> None:
        await self.db.prepare("DELETE FROM logs").run()


# Module-level fallback store. Cached here (rather than on ``env``) because
# ``env`` is a JS proxy object on Workers and stashing arbitrary Python
# attributes onto it is not safe across the FFI boundary.
_fallback_store = None


def get_store(env) -> "object":
    """Return a store implementation based on the available bindings.

    ``env`` is the Workers Environment object. If it exposes a ``DB`` D1
    binding we use D1Store; otherwise we fall back to an in-memory store
    (persisted to a JSON file only in local dev). The in-memory store is
    cached at module level so it survives across requests within one warm
    instance, but is still lost on cold start.
    """
    global _fallback_store
    db = getattr(env, "DB", None) if env is not None else None
    if db is not None:
        return D1Store(db)
    if _fallback_store is not None:
        return _fallback_store
    data_file = getattr(env, "DATA_FILE", None) if env is not None else None
    _fallback_store = MemoryStore(path=data_file)
    return _fallback_store
