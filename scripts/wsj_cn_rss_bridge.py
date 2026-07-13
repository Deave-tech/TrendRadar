#!/usr/bin/env python3
"""Local RSS bridge for public WSJ Chinese listing pages."""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterable
from xml.sax.saxutils import escape


DEFAULT_SOURCES = [
    "https://cn.wsj.com/",
    "https://cn.wsj.com/zh-hans/news/world",
    "https://cn.wsj.com/zh-hans/news/china",
    "https://cn.wsj.com/zh-hans/news/markets",
    "https://cn.wsj.com/zh-hans/news/economy",
    "https://cn.wsj.com/zh-hans/news/business",
    "https://cn.wsj.com/zh-hans/news/technology",
    "https://cn.wsj.com/zh-hans/news/life",
    "https://cn.wsj.com/zh-hans/news/opinion",
]

USER_AGENT = (
    "Mozilla/5.0 (compatible; LocalWsjCnRssBridge/1.0; "
    "+https://cn.wsj.com/)"
)


@dataclass(frozen=True)
class FeedItem:
    title: str
    link: str
    description: str = ""
    source: str = ""


class BridgeState:
    def __init__(self, sources: list[str], ttl_seconds: int) -> None:
        self.sources = sources
        self.ttl_seconds = ttl_seconds
        self.last_fetch = 0.0
        self.last_error = ""
        self.items: list[FeedItem] = []

    def get_items(self) -> list[FeedItem]:
        now = time.time()
        if self.items and now - self.last_fetch < self.ttl_seconds:
            return self.items

        try:
            items = fetch_all(self.sources)
        except Exception as exc:  # noqa: BLE001 - keep bridge serving stale data
            self.last_error = f"{type(exc).__name__}: {exc}"
            if self.items:
                return self.items
            raise

        self.last_error = ""
        self.items = items
        self.last_fetch = now
        return items


def fetch_url(url: str, timeout: int) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def fetch_all(sources: Iterable[str]) -> list[FeedItem]:
    merged: list[FeedItem] = []
    seen: set[str] = set()
    for source in sources:
        try:
            page = fetch_url(source, timeout=15)
        except urllib.error.HTTPError as exc:
            print(f"skip {source}: HTTP {exc.code}", file=sys.stderr)
            continue
        except urllib.error.URLError as exc:
            print(f"skip {source}: {exc.reason}", file=sys.stderr)
            continue

        for item in parse_items(page, source):
            key = normalize_link(item.link)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
            if len(merged) >= 80:
                return merged
    return merged


def parse_items(page: str, source: str) -> list[FeedItem]:
    data_items = parse_next_data(page, source)
    if data_items:
        return data_items
    return parse_anchor_fallback(page, source)


def parse_next_data(page: str, source: str) -> list[FeedItem]:
    match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        page,
        re.DOTALL,
    )
    if not match:
        return []

    try:
        data = json.loads(html.unescape(match.group(1)))
    except json.JSONDecodeError:
        return []

    items: list[FeedItem] = []
    for obj in walk_json(data):
        if not isinstance(obj, dict):
            continue
        title = first_string(
            obj,
            "headline",
            "title",
            "name",
            "displayName",
            "seoTitle",
        )
        link = first_string(obj, "url", "link", "canonicalUrl", "articleUrl")
        if not title or not link:
            continue
        if not looks_like_article_url(link):
            continue
        description = first_string(
            obj,
            "summary",
            "description",
            "dek",
            "seoDescription",
            "standfirst",
        )
        item = FeedItem(
            title=clean_text(title),
            link=absolute_url(link),
            description=clean_text(description or ""),
            source=source,
        )
        if is_valid_item(item):
            items.append(item)
    return dedupe_items(items)


def parse_anchor_fallback(page: str, source: str) -> list[FeedItem]:
    items: list[FeedItem] = []
    pattern = re.compile(
        r'<a\b[^>]*href=["\'](?P<href>[^"\']+)["\'][^>]*>(?P<body>.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(page):
        link = html.unescape(match.group("href"))
        if not looks_like_article_url(link):
            continue
        title = clean_text(strip_tags(match.group("body")))
        if not title or len(title) < 6:
            continue
        item = FeedItem(title=title, link=absolute_url(link), source=source)
        if is_valid_item(item):
            items.append(item)
    return dedupe_items(items)


def walk_json(value: object) -> Iterable[object]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def first_string(obj: dict[str, object], *keys: str) -> str:
    for key in keys:
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def clean_text(value: str) -> str:
    value = strip_tags(value)
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def strip_tags(value: str) -> str:
    return re.sub(r"<[^>]+>", " ", value)


def looks_like_article_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(absolute_url(url))
    if parsed.netloc not in {"cn.wsj.com", "www.wsj.com"}:
        return False
    path = parsed.path.lower()
    if "/articles/" in path:
        return True
    if re.search(r"-[a-z0-9]{8,}$", path.rstrip("/")):
        return True
    return False


def absolute_url(url: str) -> str:
    return urllib.parse.urljoin("https://cn.wsj.com/", url)


def normalize_link(url: str) -> str:
    parsed = urllib.parse.urlparse(absolute_url(url))
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(k, v) for k, v in query if not k.lower().startswith("mod")]
    return urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", urllib.parse.urlencode(query), "")
    )


def is_valid_item(item: FeedItem) -> bool:
    if not item.title or not item.link:
        return False
    if len(item.title) > 160:
        return False
    bad_fragments = [
        "skip to main content",
        "explore our brands",
        "edition",
        "广告",
    ]
    return not any(fragment in item.title.lower() for fragment in bad_fragments)


def dedupe_items(items: Iterable[FeedItem]) -> list[FeedItem]:
    deduped: list[FeedItem] = []
    seen: set[str] = set()
    for item in items:
        key = normalize_link(item.link)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def rss_xml(items: list[FeedItem], state: BridgeState) -> bytes:
    now = dt.datetime.now(dt.timezone.utc)
    pub_date = email.utils.format_datetime(now)
    body = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "<channel>",
        "<title>华尔街日报中文网</title>",
        "<link>https://cn.wsj.com/</link>",
        "<description>Public headlines from WSJ Chinese listing pages.</description>",
        f"<lastBuildDate>{pub_date}</lastBuildDate>",
        "<language>zh-cn</language>",
        "<ttl>15</ttl>",
    ]
    if state.last_error:
        body.append(f"<description>Serving cached data. Last error: {escape(state.last_error)}</description>")
    for item in items:
        link = normalize_link(item.link)
        title = escape(item.title)
        description = escape(item.description or item.source or "华尔街日报中文网")
        guid = escape(link)
        body.extend(
            [
                "<item>",
                f"<title>{title}</title>",
                f"<link>{escape(link)}</link>",
                f"<guid isPermaLink=\"true\">{guid}</guid>",
                f"<description>{description}</description>",
                f"<pubDate>{pub_date}</pubDate>",
                "</item>",
            ]
        )
    body.extend(["</channel>", "</rss>"])
    return "\n".join(body).encode("utf-8")


def make_handler(state: BridgeState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "WsjCnRssBridge/1.0"

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path in {"/", "/health"}:
                self.respond_text("ok\n")
                return
            if parsed.path != "/wsj-cn.xml":
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                return
            try:
                items = state.get_items()
                payload = rss_xml(items, state)
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
            print(
                f"{self.address_string()} - [{self.log_date_time_string()}] {fmt % args}",
                file=sys.stderr,
            )

        def respond_text(self, text: str) -> None:
            payload = text.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Run WSJ Chinese RSS bridge.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4555)
    parser.add_argument("--ttl", type=int, default=900)
    parser.add_argument("--source", action="append", dest="sources")
    args = parser.parse_args()

    state = BridgeState(args.sources or DEFAULT_SOURCES, args.ttl)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(state))
    print(f"WSJ CN RSS bridge listening on http://{args.host}:{args.port}/wsj-cn.xml")
    server.serve_forever()


if __name__ == "__main__":
    main()
