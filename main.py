"""main.py -- Cloudflare Workers (Python) entry point for IPTV Spider.

Python Workers have had TWO entrypoint conventions, and which one the runtime
looks for depends on ``compatibility_date``:

  * Newer dates (~2025-08 onward): a ``Default`` class extending
    ``WorkerEntrypoint``, with ``fetch(self, request)`` and bindings on
    ``self.env``. This requires the ``workers`` package to be vendored into
    ``python_modules/`` by pywrangler (workers-py >= 1.9), otherwise deploy
    fails with ``ModuleNotFoundError: No module named 'workers'`` [10021].

  * Older dates (e.g. 2025-01-01): the runtime looks for a module-level
    ``on_fetch(request, env)`` function. Using the class form there fails at
    request time with ``TypeError: Method on_fetch does not exist`` -> 1101.

This module therefore exposes BOTH entrypoints (``on_fetch`` and
``Default.fetch``) sharing one implementation, so it works under either
convention. ``wrangler.toml`` pins ``compatibility_date = "2025-01-01"``,
which uses the built-in SDK and needs no pywrangler packaging.

Every request is wrapped in try/except that returns a 500 with the captured
traceback, so a runtime failure is readable instead of Cloudflare's 1101 page.
"""

from __future__ import annotations

import json
import traceback
from urllib.parse import urlsplit

from workers import WorkerEntrypoint, Response

import auth
from collector import KIND_LABELS, collect
from router import route, validate_channels
from storage import get_store, _to_py


def _log(*parts) -> None:
    """Log to the JS console on Workers; fall back to print() locally."""
    msg = " ".join(str(p) for p in parts)
    try:
        from js import console  # provided by the Workers runtime (Pyodide)
        console.log(msg)
        return
    except Exception:
        pass
    try:
        print(msg)
    except Exception:
        pass


def _get_fetch():
    """Resolve the Workers ``fetch`` callable.

    Depending on the runtime / compatibility date it is either injected as a
    Python global or only reachable through the ``js`` module (older layouts
    raise ``NameError: name 'fetch' is not defined``). Resolve lazily at call
    time so either layout works.
    """
    fn = globals().get("fetch")
    if fn is not None:
        return fn
    try:
        from js import fetch as js_fetch
        return js_fetch
    except Exception:
        return None


async def _worker_fetcher(url: str, method: str = "GET", timeout: float = 5.0,
                          headers=None, **_kw):
    """Wrap the Workers ``fetch`` into iptv_core's contract.

    A best-effort abort timeout is attached via JS ``AbortSignal.timeout`` when
    available; if that global is missing we simply skip it (never fatal).
    """
    fn = _get_fetch()
    if fn is None:
        raise RuntimeError("fetch is unavailable in this Workers runtime")
    opts = {"method": method}
    if headers:
        opts["headers"] = headers
    try:
        from js import AbortSignal
        opts["signal"] = AbortSignal.timeout(int(timeout * 1000))
    except Exception:
        pass
    try:
        return await fn(url, opts)
    except Exception:
        # Some builds reject the options object; fall back to plain kwargs.
        return await fn(url, method=method, headers=headers or {})


def _cors(headers: dict) -> dict:
    headers["Access-Control-Allow-Origin"] = "*"
    headers["Access-Control-Allow-Methods"] = "GET,POST,DELETE,OPTIONS"
    headers["Access-Control-Allow-Headers"] = "Content-Type"
    return headers


async def _purge_cache(kv) -> None:
    """Drop any cached playlist entries after a mutation.

    ``kv.list()`` returns a Pyodide JS proxy, not a dict -- it must be
    normalised first or ``isinstance(..., dict)`` is False and the whole purge
    silently fails (leaving stale/empty playlists cached for the full TTL).
    """
    try:
        listed = _to_py(await kv.list())
        keys = listed.get("keys", []) if isinstance(listed, dict) else []
        for k in keys:
            key = k.get("name") if isinstance(k, dict) else k
            if key.startswith("/m3u") or key.startswith("/txt"):
                await kv.delete(key)
    except Exception:
        pass


async def _dispatch(request, env) -> Response:
    # CORS preflight
    if request.method == "OPTIONS":
        return Response("", headers=_cors({}), status=204)

    split = urlsplit(request.url)
    path = split.path
    body = await request.text() if request.method in ("POST", "PUT") else ""

    store = get_store(env)

    # --- session (signed cookie) ---------------------------------------
    raw_cookie = ""
    try:
        raw_cookie = request.headers.get("Cookie") or ""
    except Exception:
        raw_cookie = ""
    authed = False
    if raw_cookie:
        try:
            token = auth.parse_cookies(str(raw_cookie)).get(auth.COOKIE_NAME, "")
            authed = auth.verify(await auth.get_secret(store), token)
        except Exception:
            authed = False

    kv = getattr(env, "CACHE", None)

    # Cache-aside for playlist endpoints (KV binding is optional).
    if kv is not None and request.method == "GET" and path in ("/m3u", "/txt"):
        cache_key = f"{path}?{split.query}"
        try:
            cached = await kv.get(cache_key)
        except Exception:
            cached = None
        if cached is not None:
            ct = ("application/x-mpegurl; charset=utf-8" if path == "/m3u"
                  else "text/plain; charset=utf-8")
            return Response(cached, status=200,
                            headers=_cors({"Content-Type": ct}))

    result = await route(request.method, path, split.query, body, store,
                         _worker_fetcher, env=env, authed=authed)

    # Invalidate cache when data changes.
    is_mut = (request.method in ("POST", "DELETE", "PUT")
              and path.startswith("/api/"))
    if kv is not None and is_mut and 200 <= result["status"] < 300:
        await _purge_cache(kv)
    # Populate cache for freshly generated playlists. NEVER cache an empty
    # result: hitting /m3u once before the first collection would otherwise
    # serve stale emptiness for the whole TTL -- which looks exactly like
    # "collection ran but stored nothing".
    elif (kv is not None and result["status"] == 200
          and path in ("/m3u", "/txt")
          and len(result["body"].strip()) > 20):
        cache_key = f"{path}?{split.query}"
        try:
            await kv.put(cache_key, result["body"], expiration_ttl=600)
        except TypeError:
            try:
                await kv.put(cache_key, result["body"])
            except Exception:
                pass
        except Exception:
            pass

    return Response(
        result["body"],
        status=result["status"],
        headers=_cors(dict(result["headers"])),
    )


async def _handle(request, env) -> Response:
    """Single request implementation shared by both entrypoint styles."""
    try:
        return await _dispatch(request, env)
    except Exception as exc:
        # Surface the real error instead of Cloudflare's 1101 page.
        tb = traceback.format_exc()
        _log("[iptv-spider] uncaught exception:", tb)
        payload = json.dumps(
            {
                "error": "internal_error",
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": tb,
            },
            ensure_ascii=False,
        )
        return Response(
            payload,
            status=500,
            headers=_cors({"Content-Type": "application/json; charset=utf-8"}),
        )


# ---------------------------------------------------------------------------
# Entrypoints
# ---------------------------------------------------------------------------

async def on_fetch(request, env=None):
    """Legacy Python Workers entrypoint (module-level function).

    Required for compatibility_date values before the WorkerEntrypoint class
    convention was introduced. ``env`` defaults to None so the handler still
    runs if the runtime only passes ``request`` (it then falls back to the
    in-memory store).
    """
    return await _handle(request, env)


class Default(WorkerEntrypoint):

    async def fetch(self, request):
        """Modern Python Workers entrypoint (class-based)."""
        return await _handle(request, getattr(self, "env", None))

    async def on_fetch(self, request, env=None):
        """Legacy method-style entrypoint.

        Older compatibility dates resolve the handler by looking for an
        ``on_fetch`` method on the entrypoint object (newer ones use
        ``fetch``). Provide both so either runtime finds a handler.
        """
        return await _handle(request, env or getattr(self, "env", None))

    async def scheduled(self, controller, env, ctx):
        """Cron Trigger: auto-collect sources, then validate what we have."""
        try:
            store = get_store(env)

            async def _log(action, message="", level="info"):
                await store.log(action, message, level)

            # 1) auto-collect (toggle via the 设置 page)
            if (await store.get("auto_collect") or "1") == "1":
                kinds = await store.get("collect_kinds") or "hotel,multicast,migu"
                pages = int(await store.get("collect_pages") or 1)
                max_sources = int(await store.get("collect_max") or 12)
                total = 0
                for kind in str(kinds).split(","):
                    kind = kind.strip()
                    if kind in KIND_LABELS:
                        res = await collect(kind, _worker_fetcher, store,
                                            pages=pages,
                                            max_sources=max_sources, log=_log)
                        total += res.get("channels", 0)
                await _log("collect", f"定时采集完成，共 {total} 个频道")

            # 2) validate everything we currently store
            results = await validate_channels(store, _worker_fetcher, 5.0)
            await _log("validate", f"定时校验 {len(results)} 个频道")
        except Exception:
            # Logged only -- re-raising would just turn into a red cron run.
            _log("[iptv-spider] scheduled failed:", traceback.format_exc())
