"""iptv_core.py -- Pure-Python IPTV source core (Cloudflare Workers compatible).

This module has NO Workers-specific imports so it can be unit-tested and run
locally with the standard library. The only external dependency is the `fetch`
callable passed in by the runtime:
  * On Cloudflare Workers, the global ``fetch`` is used (see main.py).
  * For local development, local_dev.py provides a stdlib-based shim.

What this keeps from the original "IPTV Spider" (fnOS app, v2.1.8):
  * M3U / TXT parsing and generation (standard #EXTM3U format)
  * Channel model + grouping / sorting
  * Source validation via HTTP  (replaces the original ffmpeg/ffprobe probing)
  * Remote subscription fetching (fetch an M3U URL and parse it)

What is intentionally NOT ported (documented in README.md):
  * ffmpeg / ffprobe stream resolution + speed test  -> native 79 MB binary
  * qqwry.dat IP geolocation                          -> 26 MB data file
  * SQLite local database                              -> replaced by D1 / KV
  * Long-running scheduler threads + full web UI       -> Workers is stateless
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class Channel:
    id: str
    name: str
    url: str
    group: str = ""
    logo: str = ""
    tvg_id: str = ""
    tvg_name: str = ""
    status: str = "unknown"            # ok | dead | unknown
    latency_ms: Optional[int] = None
    http_status: Optional[int] = None
    last_checked: Optional[float] = None
    source: str = "manual"             # manual | subscription | multicast
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# M3U parsing / generation
# ---------------------------------------------------------------------------

_EXTINF_RE = re.compile(r"#EXTINF[^\n]*", re.IGNORECASE)
_ATTR_RE = re.compile(r'(\w[\w-]*)=("([^"]*)"|\'([^\']*)\')')


def _esc(value: str) -> str:
    """Escape a value for use inside an M3U attribute or text field."""
    return (str(value)
            .replace("&", "&amp;")
            .replace('"', "&quot;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


def _clean_url(url: str) -> str:
    """Undo HTML escaping that some upstream M3Us bake into URLs.

    cqshushu serves URLs like
        ...?key=txiptv&amp;playlive=1&amp;authid=0
    which players choke on -- the ``&amp;`` has to become a real ``&``.
    """
    if not url:
        return url
    return (url.replace("&amp;", "&")
               .replace("&quot;", '"')
               .replace("&#39;", "'")
               .replace("&lt;", "<")
               .replace("&gt;", ">"))


# Beijing time = UTC+8. Workers' runtime clock is UTC, so we shift it.
def _beijing_now_str() -> str:
    """Current 北京时间 (UTC+8) as ``YYYY-MM-DD HH:MM:SS`` (no timezone label)."""
    local = time.gmtime(time.time() + 8 * 3600)
    return time.strftime("%Y-%m-%d %H:%M:%S", local)


def _make_id(url: str, name: str) -> str:
    import hashlib
    return hashlib.sha1(f"{name}|{url}".encode("utf-8")).hexdigest()[:16]


def parse_m3u(text: str, source: str = "subscription") -> list[Channel]:
    """Parse an M3U / EXTM3U playlist into a list of Channel objects."""
    channels: list[Channel] = []
    pending: dict = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            attrs: dict = {}
            for m in _ATTR_RE.finditer(line):
                key = m.group(1).lower()
                val = m.group(3) if m.group(3) is not None else m.group(4)
                attrs[key] = val
            comma = line.rfind(",")
            name = line[comma + 1:].strip() if comma != -1 else ""
            pending = {
                "tvg_id": attrs.get("tvg-id", ""),
                "tvg_name": attrs.get("tvg-name", name),
                "logo": attrs.get("tvg-logo", ""),
                "group": attrs.get("group-title", ""),
                "name": name,
            }
        elif line.upper().startswith("#EXTGRP:"):
            grp = line.split(":", 1)[1].strip()
            if pending:
                pending["group"] = grp
        elif line.startswith("#"):
            continue  # other comments / directives
        else:
            url = _clean_url(line)
            meta = pending or {"name": url, "group": ""}
            ch = Channel(
                id=_make_id(url, meta.get("name", "")),
                name=meta.get("name") or url,
                url=url,
                group=meta.get("group", ""),
                logo=meta.get("logo", ""),
                tvg_id=meta.get("tvg_id", ""),
                tvg_name=meta.get("tvg_name", meta.get("name", "")),
                source=source,
            )
            channels.append(ch)
            pending = {}
    return channels


def generate_m3u(channels: list[Channel]) -> str:
    """Render channels as a standard #EXTM3U playlist."""
    out = ["#EXTM3U", f"#更新时间{_beijing_now_str()}"]
    for c in channels:
        attrs = (
            f' tvg-id="{_esc(c.tvg_id)}"'
            f' tvg-name="{_esc(c.tvg_name or c.name)}"'
            f' tvg-logo="{_esc(c.logo)}"'
        )
        if c.group:
            attrs += f' group-title="{_esc(c.group)}"'
        out.append(f"#EXTINF:-1{attrs},{_esc(c.name)}")
        out.append(_clean_url(c.url))
    return "\n".join(out) + "\n"


def generate_txt(channels: list[Channel]) -> str:
    """Render channels as a grouped TXT playlist (group,#genre# / name,url)."""
    groups: dict[str, list[Channel]] = {}
    order: list[str] = []
    for c in channels:
        g = c.group or "默认"
        if g not in groups:
            groups[g] = []
            order.append(g)
        groups[g].append(c)
    lines: list[str] = []
    lines.append(f"更新时间{_beijing_now_str()}")
    for g in order:
        lines.append(f"{g},#genre#")
        for c in groups[g]:
            lines.append(f"{c.name},{_clean_url(c.url)}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def categorize_and_sort(channels: list[Channel]) -> dict:
    """Group channels by their group-title, keeping insertion order."""
    groups: dict[str, list[Channel]] = {}
    order: list[str] = []
    for c in channels:
        g = c.group or "默认"
        if g not in groups:
            groups[g] = []
            order.append(g)
        groups[g].append(c)
    return {g: groups[g] for g in order}


# ---------------------------------------------------------------------------
# Channel merging: same channel from many sources -> one entry, many URLs
# ---------------------------------------------------------------------------

# Decorative suffixes that upstream sources append to channel names.
_SUFFIX_RE = re.compile(r"(?:综合|高清|标清|超清|HD|hd|4K|4k|测试|备用|直播)+$")
_SORT_MAX = 10 ** 6


def normalize_channel_name(name: str) -> str:
    """Canonical name so the *same* channel from different sources merges.

    One channel is spelled many ways across sources:

        "CCTV-1" / "CCTV1" / "CCTV1综合" / "CCTV 1"  ->  "CCTV1"
        "CCTV5+"                                    ->  "CCTV5+"
    """
    s = (name or "").strip()
    if not s:
        return ""
    s = s.replace("－", "-").replace("—", "-").replace("–", "-")
    s = re.sub(r"\s+", "", s)
    s = s.replace("-", "")
    prev = None
    while prev != s:
        prev = s
        s = _SUFFIX_RE.sub("", s)
    return s


def _sort_key(name: str):
    """Numeric-aware order: CCTV1 < CCTV2 < CCTV9 < CCTV10 (not lexical)."""
    match = re.search(r"(\d+)", name or "")
    return (int(match.group(1)) if match else _SORT_MAX, name or "")


def merge_channels(channels: list[Channel]) -> dict:
    """canonical name -> deduped list of URLs, numerically sorted.

    This is what makes a subscription actually usable: every source for CCTV1
    is listed together instead of being scattered by source/region.
    """
    merged: dict[str, list[str]] = {}
    for ch in channels:
        key = normalize_channel_name(ch.name)
        if not key or not ch.url:
            continue
        urls = merged.setdefault(key, [])
        if ch.url not in urls:
            urls.append(ch.url)
    return {k: merged[k] for k in sorted(merged, key=_sort_key)}


_CCTV_RE = re.compile(r"^(?:CCTV|央视)", re.I)
_SATELLITE_RE = re.compile(
    r"(卫视|BTV|湖南|浙江|江苏|东方|山东|安徽|江西|辽宁|黑龙江|广东|深圳|"
    r"天津|重庆|东南|旅游|海峡|北京)", re.I)
CATEGORY_ORDER = ["央视", "卫视", "其他"]


def channel_category(name: str) -> str:
    """Rough bucket so merged output reads as 央视 / 卫视 / 其他."""
    text = name or ""
    if _CCTV_RE.search(text):
        return "央视"
    if _SATELLITE_RE.search(text):
        return "卫视"
    return "其他"


def _bucket(merged: dict) -> dict:
    buckets: dict[str, dict[str, list[str]]] = {}
    for name, urls in merged.items():
        buckets.setdefault(channel_category(name), {})[name] = urls
    return buckets


def generate_merged_txt(channels: list[Channel]) -> str:
    """TXT playlist where each channel lists *all* its sources together.

        央视,#genre#
        CCTV1,http://...      <- one line per source for CCTV1
        CCTV1,http://...
        CCTV2,http://...
    """
    buckets = _bucket(merge_channels(channels))
    lines: list[str] = []
    lines.append(f"更新时间{_beijing_now_str()}")
    for cat in CATEGORY_ORDER:
        group = buckets.get(cat)
        if not group:
            continue
        lines.append(f"{cat},#genre#")
        for name, urls in group.items():
            for url in urls:
                lines.append(f"{name},{_clean_url(url)}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def generate_merged_m3u(channels: list[Channel]) -> str:
    """M3U playlist with each channel's sources grouped consecutively."""
    buckets = _bucket(merge_channels(channels))
    out = ["#EXTM3U", f"#更新时间{_beijing_now_str()}"]
    for cat in CATEGORY_ORDER:
        group = buckets.get(cat)
        if not group:
            continue
        for name, urls in group.items():
            for url in urls:
                out.append(
                    f'#EXTINF:-1 tvg-name="{_esc(name)}" '
                    f'group-title="{_esc(cat)}",{_esc(name)}'
                )
                out.append(_clean_url(url))
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Network operations (async, fetcher-injected)
# ---------------------------------------------------------------------------

async def validate_source(url: str, fetcher, timeout: float = 5.0) -> dict:
    """Probe a single source URL. Returns a status dict.

    The original app used ffprobe/ffmpeg to verify streams. On Workers we can
    only do HTTP-level checks, so we try HEAD then a ranged GET.
    """
    t0 = time.monotonic()
    try:
        resp = await fetcher(url, method="HEAD", timeout=timeout)
        status = getattr(resp, "status", None)
        if status is None or status >= 400:
            resp = await fetcher(
                url, method="GET", timeout=timeout,
                headers={"Range": "bytes=0-0"},
            )
            status = getattr(resp, "status", None)
        latency = int((time.monotonic() - t0) * 1000)
        ok = status is not None and 200 <= status < 400
        return {
            "url": url,
            "status": "ok" if ok else "dead",
            "http_status": status,
            "latency_ms": latency,
        }
    except Exception as e:  # noqa: BLE001 - network errors are expected
        latency = int((time.monotonic() - t0) * 1000)
        return {
            "url": url,
            "status": "dead",
            "http_status": None,
            "latency_ms": latency,
            "error": str(e),
        }


async def fetch_subscription(url: str, fetcher, timeout: float = 15.0,
                             source: str = "subscription") -> list[Channel]:
    """Fetch a remote M3U subscription and parse it into channels."""
    resp = await fetcher(url, method="GET", timeout=timeout)
    text = await resp.text()
    return parse_m3u(text, source=source)
