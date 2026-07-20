#!/usr/bin/env python3
"""Hardened local RSS bridge for The Economist's official latest feed.

The request handlers only serve an in-memory last-known-good snapshot.  A
background worker refreshes the official feed, validates and canonicalises every
article URL, and atomically persists successful snapshots for restart recovery.
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
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable
from xml.sax.saxutils import escape


DEFAULT_UPSTREAM = "https://www.economist.com/latest/rss.xml"
DEFAULT_SOURCES = [DEFAULT_UPSTREAM]
DEFAULT_MAX_ITEMS = 80
DEFAULT_MAX_WORKERS = 4
DEFAULT_FETCH_TIMEOUT = 15
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
SNAPSHOT_VERSION = 1
USER_AGENT = (
    "Mozilla/5.0 (compatible; TrendRadarEconomistRssBridge/2.0; "
    "+https://www.economist.com/)"
)

_DATED_ARTICLE_PATH = re.compile(
    r"^/[a-z0-9][a-z0-9-]*/"
    r"(?P<year>20\d{2})/(?P<month>0[1-9]|1[0-2])/"
    r"(?P<day>0[1-9]|[12]\d|3[01])/(?P<slug>[a-z0-9][a-z0-9-]*)/?$",
    re.IGNORECASE,
)
_NON_ARTICLE_SEGMENTS = {
    "audio",
    "audios",
    "film",
    "films",
    "interactive",
    "podcast",
    "podcasts",
    "video",
    "videos",
}


@dataclass(frozen=True)
class FeedItem:
    title: str
    link: str
    description: str = ""
    pub_date: str = ""
    guid: str = ""
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
        fetch_timeout: int = DEFAULT_FETCH_TIMEOUT,
        max_workers: int = DEFAULT_MAX_WORKERS,
        max_items: int = DEFAULT_MAX_ITEMS,
        stale_after_seconds: int | None = None,
    ) -> None:
        self.sources = list(dict.fromkeys(sources))
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.fetch_timeout = max(1, int(fetch_timeout))
        self.max_workers = max(1, int(max_workers))
        self.max_items = _bounded_item_limit(max_items)
        self.stale_after_seconds = (
            max(1, int(stale_after_seconds))
            if stale_after_seconds is not None
            else self.ttl_seconds * 2
        )
        self.snapshot_path = Path(snapshot_path).expanduser() if snapshot_path else None

        # Public attributes are retained for compatibility with the former bridge.
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
        """Return immediately with a copy of the current snapshot."""
        with self._lock:
            return list(self.items)

    def refresh_now(self) -> bool:
        """Refresh every source once without replacing a good snapshot with empty data."""
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
            finished_at = time.time()
            with self._lock:
                self.last_attempt = finished_at
                self.successful_sources = list(batch.successful_sources)
                self.failed_sources = dict(batch.failed_sources)

            if not batch.items:
                with self._lock:
                    self.last_error = "refresh returned no valid Economist articles"
                    if batch.failed_sources:
                        self.last_error += (
                            f"; {len(batch.failed_sources)} source(s) failed"
                        )
                return False

            effective_batch = batch
            if batch.failed_sources:
                with self._lock:
                    previous_items = list(self.items)
                if previous_items:
                    effective_batch = FetchBatch(
                        items=dedupe_items([*batch.items, *previous_items])[
                            : self.max_items
                        ],
                        successful_sources=batch.successful_sources,
                        failed_sources=batch.failed_sources,
                    )

            try:
                self._write_snapshot(effective_batch, finished_at)
            except OSError as exc:
                with self._lock:
                    self.last_error = f"snapshot write failed: {_safe_error(exc)}"
                return False

            with self._lock:
                self.items = list(effective_batch.items)
                self.last_fetch = finished_at
                self.last_success = finished_at
                self.last_error = ""
            return True
        except Exception as exc:  # noqa: BLE001 - preserve last-known-good data
            with self._lock:
                self.last_error = f"{type(exc).__name__}: {_safe_error(exc)}"
            return False
        finally:
            with self._lock:
                self._refreshing = False
            self._refresh_lock.release()

    def start_background(self) -> None:
        with self._lock:
            if self._refresh_thread and self._refresh_thread.is_alive():
                return
            self._stop_event.clear()
            self._refresh_thread = threading.Thread(
                target=self._refresh_loop,
                name="economist-rss-refresh",
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
        checked_at = time.time() if now is None else float(now)
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
                    pub_date=str(value.get("pub_date", "")),
                    guid=str(value.get("guid", "")),
                    source=str(value.get("source", "")),
                )
                if is_valid_item(item):
                    items.append(item)
            items = dedupe_items(items)[: self.max_items]
            if not items:
                raise ValueError("snapshot has no valid article items")

            saved_at = float(raw.get("last_success") or raw.get("saved_at") or 0)
            source_health = raw.get("sources", {})
            successful = (
                source_health.get("successful", [])
                if isinstance(source_health, dict)
                else []
            )
            failed = (
                source_health.get("failed", {})
                if isinstance(source_health, dict)
                else {}
            )
            with self._lock:
                self.items = items
                self.last_fetch = saved_at
                self.last_success = saved_at
                self.last_attempt = saved_at
                self.successful_sources = [
                    value for value in successful if isinstance(value, str)
                ]
                self.failed_sources = (
                    {str(key): str(value) for key, value in failed.items()}
                    if isinstance(failed, dict)
                    else {}
                )
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
                    "pub_date": item.pub_date,
                    "guid": item.guid,
                    "source": item.source,
                }
                for item in batch.items
            ],
        }
        _atomic_write_json(self.snapshot_path, payload)


def default_snapshot_path() -> Path:
    configured = os.environ.get("ECONOMIST_RSS_SNAPSHOT")
    if configured:
        return Path(configured).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    root = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return root / "trendradar" / "economist-rss-bridge.json"


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
            # Same-directory rename remains atomic where directory fsync is unavailable.
            pass
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Do not let an official-feed request silently cross origins."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


def is_valid_source_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.lower() == "https"
        and (parsed.hostname or "").lower() == "www.economist.com"
        and parsed.username is None
        and parsed.password is None
        and port is None
        and not parsed.query
        and not parsed.fragment
        and parsed.path.startswith("/")
        and parsed.path.lower().endswith(("/rss.xml", ".rss.xml"))
    )


def fetch_url(url: str, timeout: int) -> bytes:
    """Fetch one allowlisted official Economist RSS source with a hard size cap."""
    if not is_valid_source_url(url):
        raise ValueError("source must be an official Economist HTTPS RSS URL")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/rss+xml, application/xml, text/xml",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": USER_AGENT,
        },
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        NoRedirectHandler(),
    )
    with opener.open(request, timeout=max(1, int(timeout))) as response:
        final_url = response.geturl()
        if final_url != url or not is_valid_source_url(final_url):
            raise ValueError("upstream returned an unexpected final URL")
        payload = response.read(MAX_RESPONSE_BYTES + 1)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ValueError("Economist RSS response is too large")
    return payload


def fetch_all(
    sources: Iterable[str],
    *,
    timeout: int = DEFAULT_FETCH_TIMEOUT,
    max_workers: int = DEFAULT_MAX_WORKERS,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> list[FeedItem]:
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
    timeout: int = DEFAULT_FETCH_TIMEOUT,
    max_workers: int = DEFAULT_MAX_WORKERS,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> FetchBatch:
    """Fetch all sources concurrently, then globally dedupe and cap the result."""
    source_list = list(dict.fromkeys(sources))
    if not source_list:
        return FetchBatch([], [], {})

    results: dict[str, list[FeedItem]] = {}
    failures: dict[str, str] = {}
    worker_count = min(max(1, int(max_workers)), len(source_list))
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="economist-feed",
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
            except Exception as exc:  # noqa: BLE001 - isolate individual sources
                failures[source] = f"{type(exc).__name__}: {_safe_error(exc)}"

    # Merge in configured source order rather than non-deterministic completion order.
    merged: list[FeedItem] = []
    for source in source_list:
        merged.extend(results.get(source, []))
    merged = dedupe_items(merged)[: _bounded_item_limit(max_items)]
    successful = [source for source in source_list if source in results]
    ordered_failures = {
        source: failures[source] for source in source_list if source in failures
    }
    return FetchBatch(merged, successful, ordered_failures)


def _fetch_source(source: str, timeout: int) -> list[FeedItem]:
    payload = fetch_url(source, timeout=timeout)
    return parse_feed(payload, source)


def parse_feed(payload: bytes | str, source: str = DEFAULT_UPSTREAM) -> list[FeedItem]:
    """Parse official RSS and retain only dated, direct Economist article URLs."""
    if isinstance(payload, bytes):
        raw = payload
    else:
        raw = payload.encode("utf-8")
    if b"<!DOCTYPE" in raw.upper():
        raise ValueError("RSS document types are not allowed")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError("Economist RSS is not valid XML") from exc

    items: list[FeedItem] = []
    for node in root.findall("./channel/item"):
        title = clean_text(node.findtext("title") or "")
        link = clean_text(node.findtext("link") or "")
        if not title or not link or not looks_like_article_url(link):
            continue
        item = FeedItem(
            title=title,
            link=normalize_link(link),
            description=clean_description(node.findtext("description") or ""),
            pub_date=normalize_pub_date(node.findtext("pubDate") or ""),
            guid=clean_guid(node.findtext("guid") or ""),
            source=source,
        )
        if is_valid_item(item):
            items.append(item)
    return dedupe_items(items)


def absolute_url(url: str) -> str:
    return urllib.parse.urljoin(
        "https://www.economist.com/", html.unescape(url).strip()
    )


def looks_like_article_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(absolute_url(url))
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != "www.economist.com"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or "\\" in parsed.path
    ):
        return False

    path = re.sub(r"/{2,}", "/", parsed.path)
    match = _DATED_ARTICLE_PATH.fullmatch(path)
    if not match:
        return False
    segments = [segment.lower() for segment in path.split("/") if segment]
    if _NON_ARTICLE_SEGMENTS.intersection(segments):
        return False
    try:
        dt.date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
    except ValueError:
        return False
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        marker = f"{key}={value}".lower()
        if any(word in marker for word in _NON_ARTICLE_SEGMENTS):
            return False
    return True


def normalize_link(url: str) -> str:
    parsed = urllib.parse.urlparse(absolute_url(url))
    path = re.sub(r"/{2,}", "/", parsed.path).rstrip("/")
    return urllib.parse.urlunparse(("https", "www.economist.com", path, "", "", ""))


def is_valid_item(item: FeedItem) -> bool:
    if not item.title or not item.link or not looks_like_article_url(item.link):
        return False
    if len(item.title) > 300 or _is_non_article_title(item.title):
        return False
    return True


def dedupe_items(items: Iterable[FeedItem]) -> list[FeedItem]:
    deduped: list[FeedItem] = []
    seen: set[str] = set()
    for item in items:
        if not is_valid_item(item):
            continue
        link = normalize_link(item.link)
        if link in seen:
            continue
        seen.add(link)
        deduped.append(
            FeedItem(
                title=clean_text(item.title),
                link=link,
                description=clean_description(item.description),
                pub_date=normalize_pub_date(item.pub_date),
                guid=clean_guid(item.guid),
                source=item.source,
            )
        )
    return deduped


def clean_text(value: str) -> str:
    value = html.unescape(str(value))
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def clean_description(value: str) -> str:
    return clean_text(value)[:1000]


def clean_guid(value: str) -> str:
    value = clean_text(value)
    if len(value) > 300 or any(ord(character) < 0x20 for character in value):
        return ""
    return value


def normalize_pub_date(value: str) -> str:
    value = clean_text(value)
    if not value:
        return ""
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed is None:
            return ""
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return email.utils.format_datetime(parsed)
    except (TypeError, ValueError, OverflowError):
        return ""


def _is_non_article_title(title: str) -> bool:
    value = title.strip().lower()
    prefixes = (
        "audio:",
        "audio |",
        "audio—",
        "audio –",
        "podcast:",
        "podcast |",
        "podcast—",
        "podcast –",
        "video:",
        "video |",
        "video—",
        "video –",
    )
    return value.startswith(prefixes)


def _bounded_item_limit(value: int) -> int:
    return min(DEFAULT_MAX_ITEMS, max(1, int(value)))


def _safe_error(value: object) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()[:300]


def _format_timestamp(value: float) -> str | None:
    if not value:
        return None
    return (
        dt.datetime.fromtimestamp(value, tz=dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def rss_xml(items: list[FeedItem], state: BridgeState) -> bytes:
    now = dt.datetime.now(dt.timezone.utc)
    build_date = email.utils.format_datetime(now)
    ttl_minutes = max(1, state.ttl_seconds // 60)
    body = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "<channel>",
        "<title>The Economist</title>",
        "<link>https://www.economist.com/latest</link>",
        (
            "<description>Validated articles from The Economist official "
            "latest RSS feed.</description>"
        ),
        f"<lastBuildDate>{build_date}</lastBuildDate>",
        "<language>en</language>",
        f"<ttl>{ttl_minutes}</ttl>",
    ]
    for item in items[:DEFAULT_MAX_ITEMS]:
        link = normalize_link(item.link)
        pub_date = item.pub_date or build_date
        guid = item.guid or link
        guid_is_permalink = "false" if item.guid else "true"
        body.extend(
            [
                "<item>",
                f"<title>{escape(item.title)}</title>",
                f"<link>{escape(link)}</link>",
                f'<guid isPermaLink="{guid_is_permalink}">{escape(guid)}</guid>',
                f"<description>{escape(item.description or 'The Economist')}</description>",
                f"<pubDate>{escape(pub_date)}</pubDate>",
                "</item>",
            ]
        )
    body.extend(["</channel>", "</rss>"])
    return "\n".join(body).encode("utf-8")


def make_handler(state: BridgeState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "EconomistRssBridge/2.0"

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                self.respond_text("ok\n")
                return
            if parsed.path == "/health":
                self.respond_json(state.health())
                return
            if parsed.path != "/economist.xml":
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                return

            # Network access is deliberately confined to the background worker.
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
            payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
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
    parser = argparse.ArgumentParser(description="Run The Economist RSS bridge.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4556)
    parser.add_argument("--ttl", type=int, default=900)
    parser.add_argument("--source", action="append", dest="sources")
    parser.add_argument("--snapshot", default=str(default_snapshot_path()))
    parser.add_argument("--fetch-timeout", type=int, default=DEFAULT_FETCH_TIMEOUT)
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    parser.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS)
    # Preserve the old bridge CLI while enforcing the same hard 80-item cap.
    parser.add_argument("--limit", type=int, dest="legacy_limit")
    parser.add_argument("--stale-after", type=int)
    args = parser.parse_args()

    sources = args.sources or DEFAULT_SOURCES
    invalid_sources = [source for source in sources if not is_valid_source_url(source)]
    if invalid_sources:
        parser.error("--source must use an official Economist HTTPS RSS URL")
    max_items = args.legacy_limit if args.legacy_limit is not None else args.max_items
    state = BridgeState(
        sources,
        args.ttl,
        snapshot_path=args.snapshot,
        fetch_timeout=args.fetch_timeout,
        max_workers=args.max_workers,
        max_items=max_items,
        stale_after_seconds=args.stale_after,
    )
    state.start_background()
    server = BridgeHTTPServer((args.host, args.port), make_handler(state))
    print(
        f"Economist RSS bridge listening on http://{args.host}:{args.port}/economist.xml"
    )
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
