"""collector.py -- auto-collector for the upstream source feed.

The original IPTV-Spider was a **TonkiangCrawler** build. It collected sources
by scraping the author's own aggregation site (verified against the live site):

    https://api.cqshushu.com/hotel.php      酒店源  (hotel / LAN hotel IPTV)
    https://api.cqshushu.com/multicast.php  组播源  (multicast)
    https://api.cqshushu.com/migu.php       咪咕源  (China Mobile Migu)

Pipeline
--------
    list page      HTML table: IP:port | 节目数 | 地区名称 | 上线/更新时间 | 状态
                   every row carries a detail token  ->  ?p=<token>
    detail page    (?p=<token>) links to the channel list -> ?s=<token>
    channel list   ?s=<token>&download=m3u  returns a *standard M3U playlist*
                   whose group-title is the source/region name.

So "collecting" is just: parse the table, then download each M3U. No ffmpeg,
no HTML scraping of individual channels required.

Only the stdlib plus the injected async ``fetch`` callable are used, so this
runs identically on Cloudflare Workers and in local development.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass

from iptv_core import Channel, parse_m3u

BASE = "https://api.cqshushu.com/"
FEEDS = {
    "hotel": BASE + "hotel.php",
    "multicast": BASE + "multicast.php",
    "migu": BASE + "migu.php",
}
KIND_LABELS = {"hotel": "酒店源", "multicast": "组播源", "migu": "咪咕源"}
ALL_KINDS = ("hotel", "multicast", "migu")

_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_P_TOKEN = re.compile(r"\?p=([A-Za-z0-9_\-]+)")
_S_TOKEN = re.compile(r"\?s=([A-Za-z0-9_\-]+)")
_ADDR = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})\s*:\s*(\d{1,5})")
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}")
_HAN = re.compile(r"[\u4e00-\u9fff]")
# Upstream status labels -> our status codes.
_STATUS = (
    ("新上线", "new"), ("存活", "alive"), ("在线", "alive"),
    ("失效", "fail"), ("停播", "fail"), ("未知", "unknown"),
)


def _plain(fragment: str) -> str:
    out = _TAG_RE.sub("", fragment or "")
    return (out.replace("&nbsp;", " ").replace("&#39;", "'")
               .replace("&amp;", "&").strip())


@dataclass
class Source:
    """One upstream source server (a row on a cqshushu list page)."""
    kind: str
    addr: str                 # "110.72.103.127:808"
    ip: str
    port: int
    name: str = ""            # region/name, e.g. "广西贵港酒店 广西联通"
    program_count: int = 0    # program count reported upstream
    up_status: str = "unknown"
    online_time: str = ""
    update_time: str = ""
    token: str = ""           # cqshushu ?s= token
    channel_count: int = 0
    failed_times: int = 0
    fetched_at: float = 0.0
    created_at: float = 0.0

    @property
    def id(self) -> str:
        return hashlib.sha1(f"{self.kind}|{self.addr}".encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        data = dict(self.__dict__)
        data["id"] = self.id
        return data


def parse_source_table(kind: str, html: str) -> list[Source]:
    """Parse a cqshushu list page into :class:`Source` rows."""
    rows: list[Source] = []
    now = time.time()
    for raw in _ROW_RE.findall(html or ""):
        cells = [_plain(c) for c in _CELL_RE.findall(raw)]
        if len(cells) < 3:
            continue
        match = None
        for cell in cells:
            match = _ADDR.search(cell)
            if match:
                break
        if not match:
            continue
        ip, port = match.group(1), int(match.group(2))

        numbers = [int(c) for c in cells if c.isdigit()]
        program = max(numbers) if numbers else 0

        name = ""
        for cell in cells:
            if (cell and not cell.isdigit() and not _ADDR.search(cell)
                    and cell not in dict(_STATUS) and _HAN.search(cell)):
                name = cell
                break

        times = _TIME.findall(raw)
        status = "unknown"
        for label, code in _STATUS:
            if label in raw:
                status = code
                break

        token = _P_TOKEN.search(raw)
        rows.append(Source(
            kind=kind, addr=f"{ip}:{port}", ip=ip, port=port, name=name,
            program_count=program, up_status=status,
            online_time=times[0] if times else "",
            update_time=times[1] if len(times) > 1 else "",
            token=token.group(1) if token else "",
            fetched_at=now, created_at=now,
        ))
    return rows


async def fetch_source_list(kind: str, fetcher, pages: int = 1,
                            timeout: float = 20.0) -> list[Source]:
    """Fetch (and follow pagination of) one cqshushu list page."""
    out: list[Source] = []
    for page in range(1, max(1, pages) + 1):
        url = FEEDS[kind] + (f"?page={page}" if page > 1 else "")
        resp = await fetcher(url, method="GET", timeout=timeout)
        html = await resp.text()
        rows = parse_source_table(kind, html)
        if not rows:
            break
        out.extend(rows)
        # Stop early if the page advertises no further pages.
        if page == 1 and "?page=2" not in (html or ""):
            break
    return out


async def fetch_channels(source: Source, fetcher,
                         timeout: float = 30.0) -> list[Channel]:
    """Download the upstream M3U for one source and parse it into channels."""
    if not source.token:
        return []
    url = f"{FEEDS[source.kind]}?s={source.token}&download=m3u"
    resp = await fetcher(url, method="GET", timeout=timeout)
    text = await resp.text()
    if "#EXTM3U" not in (text or ""):
        return []
    channels = parse_m3u(text, source=source.kind)
    # Tag every channel with its originating source server + region name.
    for ch in channels:
        ch.note = ch.note or source.name
        if not ch.group:
            ch.group = source.name
    return channels


async def collect(kind: str, fetcher, store, *, pages: int = 1,
                  max_sources: int = 20, timeout: float = 30.0,
                  log=None) -> dict:
    """Run the full pipeline for one kind and persist the result.

    ``store`` must provide ``upsert_sources(list[dict])``,
    ``add_many(list[Channel])`` and ``mark_source_channels(id, count)``.
    ``max_sources`` caps how many servers we download in one pass -- Workers
    limits how many subrequests a single invocation may make, so the Cron job
    collects in batches across runs.
    """
    summary = {"kind": kind, "sources": 0, "channels": 0, "errors": []}

    try:
        sources = await fetch_source_list(kind, fetcher, pages=pages)
    except Exception as exc:  # network/parse failure
        summary["errors"].append(f"list: {exc}")
        return summary

    summary["sources"] = len(sources)
    if not sources:
        return summary

    await store.upsert_sources([s.to_dict() for s in sources])

    # Prefer sources upstream marks as alive/new, and those with most programs.
    rank = {"alive": 0, "new": 1, "unknown": 2, "fail": 3}
    ordered = sorted(sources, key=lambda s: (rank.get(s.up_status, 9),
                                             -s.program_count))
    total = 0
    for src in ordered[:max_sources]:
        try:
            channels = await fetch_channels(src, fetcher, timeout=timeout)
        except Exception as exc:
            summary["errors"].append(f"{src.addr}: {exc}")
            continue
        if channels:
            added = await store.add_many(channels)
            total += added
            await store.mark_source_channels(src.id, len(channels))
    summary["channels"] = total

    if log:
        await log("collect", kind, f"{summary['sources']} sources, "
                                    f"{summary['channels']} channels")
    return summary


# ---------------------------------------------------------------------------
# Geo / carrier parsing -- used by the 订阅 (subscription) filters
# ---------------------------------------------------------------------------

PROVINCES = [
    "北京", "天津", "上海", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江",
    "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
    "广东", "广西", "海南", "四川", "贵州", "云南", "西藏", "陕西", "甘肃",
    "青海", "宁夏", "新疆", "内蒙古", "香港", "澳门", "台湾",
]
ISPS = ["联通", "电信", "移动", "广电", "铁通"]


def parse_geo(name: str) -> tuple:
    """Best-effort parse of a source name into (province, carrier).

    Upstream names look like "广西贵港酒店 广西联通" / "北京组播 北京联通" /
    "辽宁沈阳咪咕 辽宁联通", so both are simple substrings.
    """
    text = name or ""
    province = next((p for p in PROVINCES if p in text), "")
    isp = next((s for s in ISPS if s in text), "")
    return province, isp
