#!/usr/bin/env python3
"""Local RSS bridge for public WSJ Chinese listing pages.

The HTTP handlers only serve the most recent in-memory snapshot.  Listing pages are
refreshed by a background worker so a slow WSJ response never stalls RSS clients.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import email.utils
import html
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
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
    "https://cn.wsj.com/zh-hans/news/life-arts",
    "https://cn.wsj.com/zh-hans/news/opinion",
]

USER_AGENT = (
    "Mozilla/5.0 (compatible; LocalWsjCnRssBridge/2.0; "
    "+https://cn.wsj.com/)"
)
DEFAULT_MAX_ITEMS = 80
DEFAULT_MAX_WORKERS = 4
SNAPSHOT_VERSION = 1


@dataclass(frozen=True)
class FeedItem:
    title: str
    link: str
    description: str = ""
    source: str = ""


@dataclass(frozen=True)
class FetchBatch:
    items: list[FeedItem]
    successful_sources: list[str]
    failed_sources: dict[str, str]


class BridgeState:
    """Thread-safe last-known-good feed state."""

    def __init__(
        self,
        sources: list[str],
        ttl_seconds: int,
        *,
        snapshot_path: str | Path | None = None,
        fetch_timeout: int = 15,
        max_workers: int = DEFAULT_MAX_WORKERS,
        max_items: int = DEFAULT_MAX_ITEMS,
        stale_after_seconds: int | None = None,
    ) -> None:
        self.sources = list(sources)
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.fetch_timeout = max(1, int(fetch_timeout))
        self.max_workers = max(1, int(max_workers))
        self.max_items = max(1, int(max_items))
        self.stale_after_seconds = (
            max(1, int(stale_after_seconds))
            if stale_after_seconds is not None
            else self.ttl_seconds * 2
        )
        self.snapshot_path = Path(snapshot_path).expanduser() if snapshot_path else None

        # Keep these public attributes for compatibility with the original helper.
        self.last_fetch = 0.0
        self.last_success = 0.0
        self.last_attempt = 0.0
        self.last_error = ""
        self.items: list[FeedItem] = []
        self.successful_sources: list[str] = []
        self.failed_sources: dict[str, str] = {}

        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._refresh_thread: threading.Thread | None = None
        self._refreshing = False
        self._load_snapshot()

    def get_items(self) -> list[FeedItem]:
        """Return immediately with a copy of the current last-known-good items."""
        with self._lock:
            return list(self.items)

    def refresh_now(self) -> bool:
        """Refresh all sources once; never replace a good snapshot with no items."""
        if not self._refresh_lock.acquire(blocking=False):
            return False
        with self._lock:
            self._refreshing = True
            self.last_attempt = time.time()

        try:
            batch = fetch_all_with_status(
                self.sources,
                timeout=self.fetch_timeout,
                max_workers=self.max_workers,
                max_items=self.max_items,
            )
            attempt_finished = time.time()
            with self._lock:
                self.last_attempt = attempt_finished
                self.successful_sources = list(batch.successful_sources)
                self.failed_sources = dict(batch.failed_sources)

            if not batch.items:
                with self._lock:
                    if batch.failed_sources:
                        self.last_error = (
                            "refresh returned no valid articles; "
                            f"{len(batch.failed_sources)} source(s) failed"
                        )
                    else:
                        self.last_error = "refresh returned no valid articles"
                return False

            effective_batch = batch
            if batch.failed_sources:
                with self._lock:
                    previous_items = list(self.items)
                if previous_items:
                    merged_items = dedupe_items(
                        [*batch.items, *previous_items]
                    )[: self.max_items]
                    effective_batch = FetchBatch(
                        items=merged_items,
                        successful_sources=batch.successful_sources,
                        failed_sources=batch.failed_sources,
                    )

            try:
                self._write_snapshot(effective_batch, attempt_finished)
            except OSError as exc:
                with self._lock:
                    self.last_error = f"snapshot write failed: {_safe_error(exc)}"
                return False

            with self._lock:
                self.items = list(effective_batch.items)
                self.last_fetch = attempt_finished
                self.last_success = attempt_finished
                self.last_error = ""
            return True
        except Exception as exc:  # noqa: BLE001 - preserve the last-known-good feed
            with self._lock:
                self.last_error = f"{type(exc).__name__}: {_safe_error(exc)}"
            return False
        finally:
            with self._lock:
                self._refreshing = False
            self._refresh_lock.release()

    def start_background(self) -> None:
        """Start one daemon refresh loop. Calling this more than once is harmless."""
        with self._lock:
            if self._refresh_thread and self._refresh_thread.is_alive():
                return
            self._stop_event.clear()
            self._refresh_thread = threading.Thread(
                target=self._refresh_loop,
                name="wsj-cn-rss-refresh",
                daemon=True,
            )
            self._refresh_thread.start()

    def stop_background(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        with self._lock:
            thread = self._refresh_thread
        if thread and thread.is_alive():
            thread.join(timeout=max(0.0, timeout))

    def health(self, now: float | None = None) -> dict[str, object]:
        checked_at = time.time() if now is None else now
        with self._lock:
            item_count = len(self.items)
            last_success = self.last_success
            last_attempt = self.last_attempt
            successful = list(self.successful_sources)
            failed = dict(self.failed_sources)
            refreshing = self._refreshing
            last_error = self.last_error

        age = max(0.0, checked_at - last_success) if last_success else None
        stale = not last_success or age is None or age > self.stale_after_seconds
        return {
            "ok": bool(item_count) and not stale,
            "item_count": item_count,
            "sources": {
                "total": len(self.sources),
                "successful_count": len(successful),
                "failed_count": len(failed),
                "successful": successful,
                "failed": [
                    {"url": source, "error": error}
                    for source, error in failed.items()
                ],
            },
            "last_success": _format_timestamp(last_success),
            "last_attempt": _format_timestamp(last_attempt),
            "age_seconds": round(age, 3) if age is not None else None,
            "stale_after_seconds": self.stale_after_seconds,
            "stale": stale,
            "refreshing": refreshing,
            "last_error": last_error or None,
        }

    def _refresh_loop(self) -> None:
        while not self._stop_event.is_set():
            started = time.monotonic()
            self.refresh_now()
            elapsed = time.monotonic() - started
            delay = max(1.0, self.ttl_seconds - elapsed)
            if self._stop_event.wait(delay):
                return

    def _load_snapshot(self) -> None:
        if not self.snapshot_path or not self.snapshot_path.exists():
            return
        try:
            raw = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
            if raw.get("version") != SNAPSHOT_VERSION:
                raise ValueError("unsupported snapshot version")
            raw_items = raw.get("items")
            if not isinstance(raw_items, list):
                raise ValueError("snapshot items are missing")

            items: list[FeedItem] = []
            for value in raw_items:
                if not isinstance(value, dict):
                    continue
                item = FeedItem(
                    title=str(value.get("title", "")),
                    link=str(value.get("link", "")),
                    description=str(value.get("description", "")),
                    source=str(value.get("source", "")),
                )
                if is_valid_item(item):
                    items.append(item)
            items = dedupe_items(items)[: self.max_items]
            if not items:
                raise ValueError("snapshot has no valid article items")

            saved_at = float(raw.get("last_success") or raw.get("saved_at") or 0)
            source_health = raw.get("sources", {})
            successful = source_health.get("successful", []) if isinstance(source_health, dict) else []
            failed = source_health.get("failed", {}) if isinstance(source_health, dict) else {}
            with self._lock:
                self.items = items
                self.last_fetch = saved_at
                self.last_success = saved_at
                self.last_attempt = saved_at
                self.successful_sources = [
                    value for value in successful if isinstance(value, str)
                ]
                self.failed_sources = {
                    str(key): str(value)
                    for key, value in failed.items()
                } if isinstance(failed, dict) else {}
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self.last_error = f"snapshot load failed: {_safe_error(exc)}"

    def _write_snapshot(self, batch: FetchBatch, saved_at: float) -> None:
        if not self.snapshot_path:
            return
        payload = {
            "version": SNAPSHOT_VERSION,
            "saved_at": saved_at,
            "last_success": saved_at,
            "sources": {
                "successful": batch.successful_sources,
                "failed": batch.failed_sources,
            },
            "items": [
                {
                    "title": item.title,
                    "link": item.link,
                    "description": item.description,
                    "source": item.source,
                }
                for item in batch.items
            ],
        }
        _atomic_write_json(self.snapshot_path, payload)


def default_snapshot_path() -> Path:
    configured = os.environ.get("WSJ_CN_RSS_SNAPSHOT")
    if configured:
        return Path(configured).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    root = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return root / "trendradar" / "wsj-cn-rss-bridge.json"


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            os.chmod(temp_name, 0o600)
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        temp_name = ""
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # The rename is already atomic; directory fsync is not available everywhere.
            pass
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def _is_trusted_wsj_https_url(url: str) -> bool:
    """Return true only for the exact origin allowed to receive WSJ cookies."""
    parsed = urllib.parse.urlparse(url)
    return (
        parsed.scheme.lower() == "https"
        and parsed.netloc.lower() == "cn.wsj.com"
    )


def _request_headers(url: str) -> dict[str, str]:
    user_agent = os.environ.get("WSJ_CN_USER_AGENT", "").strip() or USER_AGENT
    headers = {
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "User-Agent": user_agent,
    }
    datadome_token = os.environ.get("WSJ_CN_DATADOME_COOKIE", "").strip()
    if datadome_token:
        if any(character in datadome_token for character in ("\r", "\n", ";")):
            raise ValueError(
                "WSJ_CN_DATADOME_COOKIE must contain only the cookie token value"
            )
        if _is_trusted_wsj_https_url(url):
            headers["Cookie"] = f"datadome={datadome_token}"
    return headers


class WsjSessionRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects without forwarding the WSJ session across origins."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> urllib.request.Request | None:
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and not _is_trusted_wsj_https_url(newurl):
            redirected.remove_header("Cookie")
        return redirected


def fetch_url(url: str, timeout: int) -> str:
    req = urllib.request.Request(
        url,
        headers=_request_headers(url),
    )
    opener = urllib.request.build_opener(WsjSessionRedirectHandler())
    with opener.open(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def fetch_all(
    sources: Iterable[str],
    *,
    timeout: int = 15,
    max_workers: int = DEFAULT_MAX_WORKERS,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> list[FeedItem]:
    """Compatibility wrapper returning only the merged feed items."""
    batch = fetch_all_with_status(
        sources,
        timeout=timeout,
        max_workers=max_workers,
        max_items=max_items,
    )
    for source, error in batch.failed_sources.items():
        print(f"skip {source}: {error}", file=sys.stderr)
    return batch.items


def fetch_all_with_status(
    sources: Iterable[str],
    *,
    timeout: int = 15,
    max_workers: int = DEFAULT_MAX_WORKERS,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> FetchBatch:
    """Fetch every source concurrently, then globally normalize/dedupe/truncate."""
    source_list = list(dict.fromkeys(sources))
    if not source_list:
        return FetchBatch([], [], {})

    results: dict[str, list[FeedItem]] = {}
    failures: dict[str, str] = {}
    worker_count = min(max(1, int(max_workers)), len(source_list))
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="wsj-listing",
    ) as executor:
        futures = {
            executor.submit(_fetch_source, source, max(1, int(timeout))): source
            for source in source_list
        }
        for future in concurrent.futures.as_completed(futures):
            source = futures[future]
            try:
                results[source] = future.result()
            except urllib.error.HTTPError as exc:
                failures[source] = f"HTTP {exc.code}"
            except urllib.error.URLError as exc:
                failures[source] = _safe_error(exc.reason)
            except TimeoutError:
                failures[source] = "timeout"
            except Exception as exc:  # noqa: BLE001 - isolate each listing page
                failures[source] = f"{type(exc).__name__}: {_safe_error(exc)}"

    merged: list[FeedItem] = []
    # Merge in configured source order, not completion order, for stable RSS output.
    for source in source_list:
        merged.extend(results.get(source, []))
    merged = dedupe_items(merged)
    merged = merged[: max(1, int(max_items))]
    successful = [source for source in source_list if source in results]
    ordered_failures = {
        source: failures[source]
        for source in source_list
        if source in failures
    }
    return FetchBatch(merged, successful, ordered_failures)


def _fetch_source(source: str, timeout: int) -> list[FeedItem]:
    page = fetch_url(source, timeout=timeout)
    return parse_items(page, source)


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
        if not isinstance(obj, dict) or _is_video_object(obj):
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
        if not title or not link or not looks_like_article_url(link):
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
            link=normalize_link(link),
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
        item = FeedItem(
            title=title,
            link=normalize_link(link),
            source=source,
        )
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
    try:
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname is None
        or parsed.hostname.lower() != "cn.wsj.com"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        return False
    path = re.sub(r"/{2,}", "/", parsed.path)
    if not path.lower().startswith("/articles/") or path.rstrip("/").lower() == "/articles":
        return False
    return not _is_video_url(parsed)


def absolute_url(url: str) -> str:
    return urllib.parse.urljoin("https://cn.wsj.com/", html.unescape(url).strip())


def normalize_link(url: str) -> str:
    parsed = urllib.parse.urlparse(absolute_url(url))
    path = re.sub(r"/{2,}", "/", parsed.path).rstrip("/")
    return urllib.parse.urlunparse(("https", "cn.wsj.com", path, "", "", ""))


def is_valid_item(item: FeedItem) -> bool:
    if not item.title or not item.link or not looks_like_article_url(item.link):
        return False
    if len(item.title) > 160 or _is_video_title(item.title):
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
        if not is_valid_item(item):
            continue
        key = normalize_link(item.link)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(
            FeedItem(
                title=item.title,
                link=key,
                description=item.description,
                source=item.source,
            )
        )
    return deduped


def _is_video_url(parsed: urllib.parse.ParseResult) -> bool:
    segments = [segment.lower() for segment in parsed.path.split("/") if segment]
    if any(segment in {"video", "videos"} for segment in segments):
        return True
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        key = key.lower()
        value = value.lower()
        if key in {"video", "videos"} or (key in {"type", "contenttype", "mod"} and "video" in value):
            return True
    return False


def _is_video_object(obj: dict[str, object]) -> bool:
    for key in ("type", "contentType", "articleType", "__typename"):
        value = obj.get(key)
        if isinstance(value, str) and "video" in value.lower():
            return True
    return False


def _is_video_title(title: str) -> bool:
    value = title.strip().lower()
    return value.startswith(("视频：", "视频:", "视频｜", "视频 |", "[视频]", "【视频】", "video:", "video |"))


def _safe_error(value: object) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()[:300]


def _format_timestamp(value: float) -> str | None:
    if not value:
        return None
    return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")


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
        server_version = "WsjCnRssBridge/2.0"

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                self.respond_text("ok\n")
                return
            if parsed.path == "/health":
                self.respond_json(state.health())
                return
            if parsed.path != "/wsj-cn.xml":
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                return

            # Never perform network I/O in a request handler.
            items = state.get_items()
            if not items:
                self.respond_unavailable()
                return
            payload = rss_xml(items, state)
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

        def respond_json(self, value: dict[str, object]) -> None:
            payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def respond_unavailable(self) -> None:
            payload = b"Feed snapshot not ready\n"
            self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Retry-After", "5")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


class BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


def main() -> None:
    parser = argparse.ArgumentParser(description="Run WSJ Chinese RSS bridge.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4555)
    parser.add_argument("--ttl", type=int, default=900)
    parser.add_argument("--source", action="append", dest="sources")
    parser.add_argument("--snapshot", default=str(default_snapshot_path()))
    parser.add_argument("--fetch-timeout", type=int, default=15)
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    parser.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS)
    parser.add_argument("--stale-after", type=int)
    args = parser.parse_args()

    state = BridgeState(
        args.sources or DEFAULT_SOURCES,
        args.ttl,
        snapshot_path=args.snapshot,
        fetch_timeout=args.fetch_timeout,
        max_workers=args.max_workers,
        max_items=args.max_items,
        stale_after_seconds=args.stale_after,
    )
    state.start_background()
    server = BridgeHTTPServer((args.host, args.port), make_handler(state))
    print(f"WSJ CN RSS bridge listening on http://{args.host}:{args.port}/wsj-cn.xml")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        state.stop_background()


if __name__ == "__main__":
    main()
