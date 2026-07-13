#!/usr/bin/env python3
"""Local RSS bridge for The Economist via Google News RSS.

The Economist public website currently presents a Cloudflare challenge to this
server, so this bridge uses Google News RSS search results constrained to
economist.com and source "The Economist". It exposes a stable local RSS URL for
TrendRadar.
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import html
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape


DEFAULT_QUERY = "site:economist.com Economist"
DEFAULT_UPSTREAM = "https://news.google.com/rss/search"
USER_AGENT = (
    "Mozilla/5.0 (compatible; LocalEconomistRssBridge/1.0; "
    "+https://www.economist.com/)"
)


@dataclass(frozen=True)
class FeedItem:
    title: str
    link: str
    description: str = ""
    pub_date: str = ""
    guid: str = ""


class BridgeState:
    def __init__(self, query: str, ttl_seconds: int, limit: int) -> None:
        self.query = query
        self.ttl_seconds = ttl_seconds
        self.limit = limit
        self.last_fetch = 0.0
        self.last_error = ""
        self.items: list[FeedItem] = []

    def get_items(self) -> list[FeedItem]:
        now = time.time()
        if self.items and now - self.last_fetch < self.ttl_seconds:
            return self.items

        try:
            items = fetch_items(self.query, self.limit)
        except Exception as exc:  # noqa: BLE001 - keep bridge serving stale data
            self.last_error = f"{type(exc).__name__}: {exc}"
            if self.items:
                return self.items
            raise

        self.last_error = ""
        self.items = items
        self.last_fetch = now
        return items


def upstream_url(query: str) -> str:
    params = {
        "q": query,
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
    }
    return f"{DEFAULT_UPSTREAM}?{urllib.parse.urlencode(params)}"


def fetch_items(query: str, limit: int) -> list[FeedItem]:
    req = urllib.request.Request(
        upstream_url(query),
        headers={
            "Accept": "application/rss+xml, application/xml, text/xml",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=12) as resp:
        payload = resp.read()

    root = ET.fromstring(payload)
    items: list[FeedItem] = []
    seen: set[str] = set()

    for node in root.findall("./channel/item"):
        source = clean_text(node.findtext("source") or "")
        if source and source.lower() != "the economist":
            continue

        raw_title = clean_text(node.findtext("title") or "")
        title = clean_title(raw_title)
        link = clean_text(node.findtext("link") or "")
        if not title or not link:
            continue

        guid = clean_text(node.findtext("guid") or link)
        key = guid or link
        if key in seen:
            continue
        seen.add(key)

        description = clean_description(node.findtext("description") or "")
        pub_date = clean_text(node.findtext("pubDate") or "")
        items.append(
            FeedItem(
                title=title,
                link=link,
                description=description,
                pub_date=pub_date,
                guid=guid,
            )
        )
        if len(items) >= limit:
            break

    return items


def clean_title(value: str) -> str:
    value = re.sub(r"\s+-\s+The Economist\s*$", "", value)
    return clean_text(value)


def clean_description(value: str) -> str:
    value = html.unescape(value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    value = value.strip()
    value = re.sub(r"\s+-\s+The Economist\s*$", "", value)
    return value[:500]


def clean_text(value: str) -> str:
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def rss_xml(items: list[FeedItem], state: BridgeState) -> bytes:
    now = dt.datetime.now(dt.timezone.utc)
    build_date = email.utils.format_datetime(now)
    body = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "<channel>",
        "<title>The Economist</title>",
        "<link>https://www.economist.com/</link>",
        "<description>Economist headlines via Google News RSS.</description>",
        f"<lastBuildDate>{build_date}</lastBuildDate>",
        "<language>en-us</language>",
        "<ttl>15</ttl>",
    ]
    if state.last_error:
        body.append(f"<description>Serving cached data. Last error: {escape(state.last_error)}</description>")

    fallback_pub_date = build_date
    for item in items:
        pub_date = item.pub_date or fallback_pub_date
        description = item.description or "The Economist"
        body.extend(
            [
                "<item>",
                f"<title>{escape(item.title)}</title>",
                f"<link>{escape(item.link)}</link>",
                f"<guid isPermaLink=\"false\">{escape(item.guid or item.link)}</guid>",
                f"<description>{escape(description)}</description>",
                f"<pubDate>{escape(pub_date)}</pubDate>",
                "</item>",
            ]
        )

    body.extend(["</channel>", "</rss>"])
    return "\n".join(body).encode("utf-8")


def make_handler(state: BridgeState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "EconomistRssBridge/1.0"

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path in {"/", "/health"}:
                self.respond_text("ok\n")
                return
            if parsed.path != "/economist.xml":
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                return

            try:
                payload = rss_xml(state.get_items(), state)
            except Exception as exc:  # noqa: BLE001 - return clear local failure
                self.send_error(HTTPStatus.BAD_GATEWAY, f"Fetch failed: {exc}")
                return

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/rss+xml; charset=utf-8")
            self.send_header("Cache-Control", f"public, max-age={state.ttl_seconds}")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt: str, *args: object) -> None:
            return

        def respond_text(self, text: str) -> None:
            payload = text.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Economist RSS bridge.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4556)
    parser.add_argument("--ttl", type=int, default=900)
    parser.add_argument("--query", default=DEFAULT_QUERY)
    parser.add_argument("--limit", type=int, default=80)
    args = parser.parse_args()

    state = BridgeState(args.query, args.ttl, args.limit)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(state))
    print(f"Economist RSS bridge listening on http://{args.host}:{args.port}/economist.xml")
    server.serve_forever()


if __name__ == "__main__":
    main()
