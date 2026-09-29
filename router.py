"""router.py -- environment-agnostic routing for the JSON API + web console.

Contains the actual request logic but NO Workers/HTTP-server specific code: it
receives plain Python values (method, path, query, body) plus the store, the
`fetch` callable, the env bindings and an `authed` flag, and returns a plain
dict ``{"status": int, "headers": dict, "body": str}``. That keeps it fully
testable and reusable from both main.py (Workers) and local_dev.py.

Access model
------------
* ``/login``, ``/logout``                        always reachable
* ``/m3u`` ``/txt``  (player endpoints)          public
* every ``/api/...`` (except ``/api/health``)    requires a session -> 401
* every console page                             redirects to ``/login``
"""

from __future__ import annotations

import hashlib
import json
import secrets
import string
from typing import Any, Optional
from urllib.parse import parse_qs

import auth
from collector import ALL_KINDS, KIND_LABELS, collect, parse_geo
from iptv_core import (
    Channel, parse_m3u, generate_m3u, generate_txt,
    categorize_and_sort, validate_source, fetch_subscription,
    generate_merged_txt, generate_merged_m3u,
)
from webui import (
    page_login, page_status, page_collection, page_ip_manager,
    page_subscriptions, page_logs, page_settings,
)

PAGES = {
    "/": page_status,
    "/status": page_status,
    "/collection": page_collection,
    "/ip_manager": page_ip_manager,
    "/subscriptions": page_subscriptions,
    "/logs": page_logs,
    "/settings": page_settings,
}

DEFAULT_SETTINGS = {
    "collect_kinds": "hotel,multicast,migu",
    "collect_pages": "1",
    "collect_max": "12",
    "auto_collect": "1",
}


def _json(body: Any, status: int = 200) -> dict:
    return {"status": status,
            "headers": {"Content-Type": "application/json; charset=utf-8"},
            "body": json.dumps(body, ensure_ascii=False)}


def _text(body: str, content_type: str, status: int = 200) -> dict:
    return {"status": status, "headers": {"Content-Type": content_type},
            "body": body}


def _html(body: str, status: int = 200, extra: Optional[dict] = None) -> dict:
    headers = {"Content-Type": "text/html; charset=utf-8"}
    if extra:
        headers.update(extra)
    return {"status": status, "headers": headers, "body": body}


def _redirect(location: str, extra: Optional[dict] = None) -> dict:
    headers = {"Location": location}
    if extra:
        headers.update(extra)
    return {"status": 302, "headers": headers, "body": ""}


# ---------------------------------------------------------------------------
# main router
# ---------------------------------------------------------------------------

async def route(method: str, path: str, query_string: str, body: Any,
                store, fetcher, *, env=None, authed: bool = False) -> dict:
    # ---------------------------------------------------------------- login
    if path == "/login":
        if method == "POST":
            password = _form_field(body, "password")
            expected = await auth.get_password(store, env)
            if auth.check_password(password, expected):
                secret = await auth.get_secret(store)
                token = auth.issue(secret)
                await store.log("login", "登录成功")
                return _redirect("/status",
                                 {"Set-Cookie": auth.cookie_header(token)})
            await store.log("login", "密码错误", level="warn")
            return _html(page_login("密码错误"), status=401)
        hint = "1" if not authed else ""
        return _html(page_login(hint=hint))

    if path == "/logout":
        return _redirect("/login", {"Set-Cookie": auth.clear_cookie()})

    # -------------------------------------------------- public playlist feeds
    if method == "GET" and path in ("/m3u", "/txt"):
        group = _first(query_string, "group")
        channels = await store.list(group=group or None)
        if path == "/m3u":
            return _text(generate_m3u(channels),
                         "application/x-mpegurl; charset=utf-8")
        return _text(generate_txt(channels), "text/plain; charset=utf-8")

    if method == "GET" and path == "/api/health":
        return _json({"service": "iptv-spider-workers", "status": "ok"})

    # ------------------------------------------------- shareable subscriptions
    # Public on purpose -- the whole point is to hand this URL to a player.
    if method == "GET" and path.startswith("/sub/"):
        rest = path[len("/sub/"):]
        fmt = "m3u"
        if rest.endswith("/txt"):
            rest, fmt = rest[:-len("/txt")], "txt"
        sub = next((s for s in await _subs_raw(store)
                    if s.get("token") == rest), None)
        if not sub:
            return _json({"error": "unknown token"}, 404)
        channels = _match_channels(await store.list(),
                                   await store.list_sources(), sub)
        # Subscriptions merge the *same* channel across sources: CCTV1 lists
        # every URL together, and ordering is numeric (CCTV2 before CCTV10).
        if fmt == "txt":
            return _text(generate_merged_txt(channels),
                         "text/plain; charset=utf-8")
        return _text(generate_merged_m3u(channels),
                     "application/x-mpegurl; charset=utf-8")

    # ------------------------------------------------------------ auth gate
    if not authed:
        if path.startswith("/api/"):
            return _json({"error": "unauthorized"}, 401)
        return _redirect("/login")

    # ---------------------------------------------------------------- pages
    if method == "GET" and path in PAGES:
        return _html(PAGES[path]())

    # ----------------------------------------------------------------- api
    if method == "GET" and path == "/api/stats":
        return _json(await _stats(store))

    if method == "GET" and path == "/api/sources":
        kind = _first(query_string, "kind")
        return _json(await store.list_sources(kind or None))

    if method == "DELETE" and path.startswith("/api/sources/"):
        sid = path.rsplit("/", 1)[-1]
        return _json({"deleted": await store.delete_source(sid)})

    if method == "POST" and path == "/api/collect":
        data = _as_json(body) or {}
        kinds = data.get("kind") or "all"
        kinds = list(ALL_KINDS) if kinds == "all" else [kinds]
        pages = max(1, min(10, int(data.get("pages") or 1)))
        max_sources = max(1, min(40, int(data.get("max_sources") or 12)))

        async def _log(action, message="", level="info"):
            await store.log(action, message, level)

        results = []
        for kind in kinds:
            if kind not in KIND_LABELS:
                continue
            res = await collect(kind, fetcher, store, pages=pages,
                                max_sources=max_sources, log=_log)
            results.append(res)
        total_ch = sum(r.get("channels", 0) for r in results)
        await store.log("collect", f"共入库 {total_ch} 个频道")
        return _json({"results": results, "channels": total_ch})

    if method == "GET" and path == "/api/logs":
        limit = int(_first(query_string, "limit") or 200)
        return _json(await store.list_logs(limit))

    if method == "DELETE" and path == "/api/logs":
        await store.clear_logs()
        return _json({"cleared": True})

    if method == "GET" and path == "/api/settings":
        cfg = dict(DEFAULT_SETTINGS)
        for key in DEFAULT_SETTINGS:
            value = await store.get(key)
            if value is not None:
                cfg[key] = value
        # never leak the password hash/plaintext
        return _json(cfg)

    if method == "POST" and path == "/api/settings":
        data = _as_json(body) or {}
        if data.get("admin_password"):
            await auth.set_password(store, str(data["admin_password"]))
            await store.log("settings", "管理员密码已更新")
        for key in ("collect_kinds", "collect_pages", "collect_max"):
            if key in data:
                await store.set(key, str(data[key]))
        if "auto_collect" in data:
            await store.set("auto_collect",
                            "1" if data["auto_collect"] in (True, "1", 1) else "0")
        return _json({"saved": True})

    # -------------------------------------------------------- subscriptions
    # NOTE: a "subscription" here is a *filter over our own collected sources*
    # that produces a shareable playlist URL (/sub/<token>) -- exactly what the
    # original 订阅 page does: choose 类型 / 省份 / 运营商, get a link whose
    # contents are the matching channels. (It is NOT "import a remote M3U".)
    if method == "GET" and path == "/api/subscriptions":
        return _json(await _subs_list(store))

    if method == "GET" and path == "/api/subscriptions/meta":
        # Options for the pickers, derived from what we actually collected.
        provs, isps = set(), set()
        for src in await store.list_sources():
            province, isp = parse_geo(src.get("name") or "")
            if province:
                provs.add(province)
            if isp:
                isps.add(isp)
        return _json({"provinces": sorted(provs), "isps": sorted(isps),
                      "kinds": list(KIND_LABELS)})

    if method == "POST" and path == "/api/subscriptions":
        data = _as_json(body) or {}
        token = str(data.get("token") or "").strip() or _gen_token()
        if not token.replace("-", "").replace("_", "").isalnum():
            return _json({"error": "token 只能包含字母、数字、- 和 _"}, 400)
        entry = {
            "token": token,
            "kinds": _csv(data.get("kinds")),
            "provinces": _csv(data.get("provinces")),
            "isps": _csv(data.get("isps")),
            "min_speed": float(data.get("min_speed") or 0),
            "min_res": int(data.get("min_res") or 0),
            "created_at": __import__("time").time(),
        }
        subs = [s for s in await _subs_raw(store) if s.get("token") != token]
        subs.append(entry)
        await store.set("subscriptions", json.dumps(subs, ensure_ascii=False))
        await store.log("subscription", f"保存订阅 {token}")
        return _json(entry, 201)

    if method == "DELETE" and path.startswith("/api/subscriptions/"):
        token = path.rsplit("/", 1)[-1]
        subs = [s for s in await _subs_raw(store) if s.get("token") != token]
        await store.set("subscriptions", json.dumps(subs, ensure_ascii=False))
        await store.log("subscription", f"删除订阅 {token}")
        return _json({"deleted": True})

    # Import an external M3U URL straight into the collection.
    if method == "POST" and path == "/api/import-url":
        data = _as_json(body) or {}
        url = data.get("url")
        if not url or not str(url).startswith(("http://", "https://")):
            return _json({"error": "valid url is required"}, 400)
        try:
            channels = await fetch_subscription(url, fetcher, source="external")
        except Exception as exc:  # noqa: BLE001
            return _json({"error": f"import failed: {exc}"}, 502)
        added = await store.add_many(channels)
        await store.log("import", f"{url}: 解析 {len(channels)} 个频道，新增 {added}")
        return _json({"parsed": len(channels), "added": added, "url": url})

    # ----------------------------------------------------------- raw channel API
    if method == "GET" and path == "/api/channels":
        group = _first(query_string, "group")
        status = _first(query_string, "status")
        channels = await store.list(group=group or None, status=status or None)
        return _json([c.to_dict() for c in channels])

    if method == "DELETE" and path == "/api/channels":
        await store.clear()
        await store.log("channels", "已清空全部频道")
        return _json({"cleared": True})

    if method == "GET" and path == "/api/groups":
        groups = await store.groups()
        grouped = categorize_and_sort(await store.list())
        return _json({"groups": groups,
                      "counts": {g: len(v) for g, v in grouped.items()}})

    if method == "POST" and path == "/api/channels":
        data = _as_json(body)
        if not data or not data.get("url"):
            return _json({"error": "url is required"}, 400)
        ch = Channel(
            id=_cid(data),
            name=data.get("name") or data["url"],
            url=data["url"],
            group=data.get("group", ""),
            logo=data.get("logo", ""),
            tvg_id=data.get("tvg_id", ""),
            tvg_name=data.get("tvg_name", ""),
            source=data.get("source", "manual"),
            note=data.get("note", ""),
        )
        await store.add(ch)
        return _json(ch.to_dict(), 201)

    if method == "POST" and path == "/api/import":
        data = _as_json(body)
        text = data.get("m3u") or data.get("text") or ""
        if not text.strip():
            return _json({"error": "m3u text is required"}, 400)
        source = data.get("source", "manual")
        channels = parse_m3u(text, source=source)
        added = await store.add_many(channels)
        return _json({"parsed": len(channels), "added": added})

    if method == "POST" and path == "/api/validate":
        data = _as_json(body) or {}
        target = data.get("url")
        if target:
            channels = [c for c in await store.list() if c.url == target]
        else:
            channels = await store.list()
        results = await validate_channels(
            store, fetcher, float(data.get("timeout", 5)), channels)
        return _json({"checked": len(results), "results": results})

    if method == "DELETE" and path.startswith("/api/channels/"):
        cid = path.rsplit("/", 1)[-1]
        ok = await store.delete(cid)
        return _json({"deleted": ok}, 200 if ok else 404)

    return _json({"error": "not found", "path": path}, 404)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

async def validate_channels(store, fetcher, timeout: float = 5.0,
                            channels=None) -> list[dict]:
    """Probe a list of channels and persist their status.

    Shared by the ``/api/validate`` route and the Cron scheduled handler.
    """
    if channels is None:
        channels = await store.list()
    results: list[dict] = []
    for ch in channels:
        res = await validate_source(ch.url, fetcher, timeout=timeout)
        await store.update_status(ch.id, res["status"],
                                  res.get("latency_ms"),
                                  res.get("http_status"))
        results.append({"id": ch.id, "url": ch.url, **res})
    return results


async def _stats(store) -> dict:
    channels = await store.list()
    sources = await store.list_sources()
    libs = await store.source_stats()
    cfg = {}
    for key in DEFAULT_SETTINGS:
        cfg[key] = await store.get(key) or DEFAULT_SETTINGS[key]
    return {
        "ip_count": len(sources),
        "source_count": len(sources),
        "channel_count": len(channels),
        "new_count": sum(1 for s in sources if s.get("up_status") == "new"),
        "group_count": len(await store.groups()),
        "libs": libs,
        "auto_collect": cfg["auto_collect"] == "1",
        "collect_cron": "0 9,18,22 * * *",
    }


async def _subs_raw(store) -> list:
    raw = await store.get("subscriptions")
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception:
        return []


async def _subs(store) -> list:
    return await _subs_raw(store)


def _first(query_string: str, key: str) -> Optional[str]:
    parsed = parse_qs(query_string or "")
    vals = parsed.get(key)
    return vals[0] if vals else None


def _as_json(body: Any) -> dict:
    if isinstance(body, dict):
        return body
    if isinstance(body, str):
        try:
            return json.loads(body)
        except Exception:
            return {}
    return {}


def _form_field(body: Any, name: str) -> str:
    """Read a field from a form-encoded body (or JSON, as a fallback)."""
    if isinstance(body, dict):
        return str(body.get(name) or "")
    if isinstance(body, str):
        text = body.strip()
        if text.startswith("{"):
            return str((_as_json(text) or {}).get(name) or "")
        vals = parse_qs(text).get(name)
        return vals[0] if vals else ""
    return ""


def _cid(data: dict) -> str:
    raw = f"{data.get('name','')}|{data.get('url','')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# subscription helpers
# ---------------------------------------------------------------------------

def _gen_token(n: int = 8) -> str:
    """Random share token, 8 chars by default (matches the original app)."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _csv(value) -> str:
    """Normalise a list-or-string selection into a comma-joined string."""
    if isinstance(value, (list, tuple, set)):
        return ",".join(str(v).strip() for v in value if str(v).strip())
    return str(value or "").strip()


def _match_channels(channels: list, sources: list, sub: dict) -> list:
    """Filter channels by a subscription's 类型 / 省份 / 运营商 selection.

    Filters are applied to the *source* first (its name carries the province
    and carrier, e.g. "广西贵港酒店 广西联通"), then channels are kept when
    their group matches an allowed source name. An empty selection means
    "no filter", matching the original UI's「不选则全选」.
    """
    kinds = {v for v in (sub.get("kinds") or "").split(",") if v}
    provinces = {v for v in (sub.get("provinces") or "").split(",") if v}
    isps = {v for v in (sub.get("isps") or "").split(",") if v}
    if not (kinds or provinces or isps):
        return list(channels)
    allowed = set()
    for src in sources:
        if kinds and src.get("kind") not in kinds:
            continue
        province, isp = parse_geo(src.get("name") or "")
        if provinces and province not in provinces:
            continue
        if isps and isp not in isps:
            continue
        allowed.add(src.get("name") or "")
    return [c for c in channels if c.group in allowed]


async def _subs_list(store) -> list[dict]:
    """Subscriptions enriched with matched-channel counts and share URLs."""
    sources = await store.list_sources()
    channels = await store.list()
    out = []
    for sub in await _subs_raw(store):
        token = sub.get("token", "")
        out.append({
            **sub,
            "count": len(_match_channels(channels, sources, sub)),
            "m3u_url": f"/sub/{token}",
            "txt_url": f"/sub/{token}/txt",
        })
    return out
