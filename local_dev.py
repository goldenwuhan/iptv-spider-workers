"""local_dev.py -- Run the IPTV core locally without Cloudflare tooling.

This uses only the Python standard library so you can develop and test the
pure-Python core on any machine:

    python local_dev.py            # serves http://127.0.0.1:8000
    python local_dev.py --port 9000 --data ./data.json

It wires router.py to a stdlib ``urllib``-based fetcher and a JSON-backed
MemoryStore (persists to --data between restarts). The same router.py is used
unchanged by main.py on Cloudflare Workers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from iptv_core import Channel
from router import route
from storage import MemoryStore


class SimpleResp:
    """Mimics the parts of a Workers Response that iptv_core uses."""
    def __init__(self, status: int, headers: dict, text: str):
        self.status = status
        self.headers = headers
        self._text = text

    async def text(self) -> str:
        return self._text


async def local_fetcher(url: str, method: str = "GET", timeout: float = 5.0,
                        headers=None, **_kw) -> SimpleResp:
    def _do() -> SimpleResp:
        req = urllib.request.Request(url, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return SimpleResp(r.status, dict(r.headers),
                                  r.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as e:
            return SimpleResp(e.code, dict(e.headers),
                              e.read().decode("utf-8", "ignore"))
    return await asyncio.to_thread(_do)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _respond(self, result: dict) -> None:
        body = result["body"]
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(result["status"])
        for k, v in result["headers"].items():
            self.send_header(k, v)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        body = raw.decode("utf-8", "ignore")
        split = urlsplit(self.path)
        store = self.server.store  # type: ignore[attr-defined]
        result = asyncio.run(route(self.command, split.path, split.query,
                                  body, store, local_fetcher))
        self._respond(result)

    def do_GET(self): self._handle()
    def do_POST(self): self._handle()
    def do_DELETE(self): self._handle()
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):  # quieter logs
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="IPTV Spider local dev server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data", default="iptv_data.json",
                        help="JSON file to persist channels (local dev only)")
    args = parser.parse_args()

    store = MemoryStore(path=args.data)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.store = store  # type: ignore[attr-defined]
    print(f"IPTV Spider dev server on http://{args.host}:{args.port}")
    print(f"  data file : {args.data}")
    print(f"  try       : curl -s http://{args.host}:{args.port}/api/channels")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
