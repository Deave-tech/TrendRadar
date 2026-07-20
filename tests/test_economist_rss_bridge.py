import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "economist_rss_bridge.py"
)
SPEC = importlib.util.spec_from_file_location("economist_rss_bridge", SCRIPT_PATH)
assert SPEC and SPEC.loader
BRIDGE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BRIDGE
SPEC.loader.exec_module(BRIDGE)


def rss_payload(items):
    body = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<rss version=\"2.0\"><channel>",
    ]
    for item in items:
        body.extend(
            [
                "<item>",
                f"<title><![CDATA[{item['title']}]]></title>",
                f"<description><![CDATA[{item.get('description', '')}]]></description>",
                f"<link>{item['link']}</link>",
                f"<guid>{item.get('guid', '')}</guid>",
                f"<pubDate>{item.get('pub_date', '')}</pubDate>",
                "</item>",
            ]
        )
    body.extend(["</channel></rss>"])
    return "\n".join(body).encode("utf-8")


def story(index, *, section="business", suffix=""):
    return {
        "title": f"A valid Economist headline number {index}",
        "description": f"Summary {index}",
        "link": (
            f"https://www.economist.com/{section}/2026/07/19/story-{index}{suffix}"
        ),
        "guid": f"guid-{index}",
        "pub_date": "Sun, 19 Jul 2026 18:42:30 +0000",
    }


class UrlFilteringTests(unittest.TestCase):
    def test_default_source_is_the_official_latest_feed(self):
        self.assertEqual(
            ["https://www.economist.com/latest/rss.xml"],
            BRIDGE.DEFAULT_SOURCES,
        )
        self.assertTrue(BRIDGE.is_valid_source_url(BRIDGE.DEFAULT_UPSTREAM))

    def test_only_direct_canonical_dated_article_urls_are_allowed(self):
        valid = (
            "https://www.economist.com/the-americas/2026/07/19/"
            "brazils-beloved-payments-system?utm_source=rss#top"
        )
        interactive = (
            "https://www.economist.com/interactive/united-states/2026/06/17/"
            "boomers-have-the-good-life"
        )
        self.assertTrue(BRIDGE.looks_like_article_url(valid))
        self.assertFalse(BRIDGE.looks_like_article_url(interactive))
        self.assertEqual(
            "https://www.economist.com/the-americas/2026/07/19/"
            "brazils-beloved-payments-system",
            BRIDGE.normalize_link(valid),
        )

        invalid = [
            "http://www.economist.com/business/2026/07/19/story",
            "https://economist.com/business/2026/07/19/story",
            "https://www.economist.com:443/business/2026/07/19/story",
            "https://www.economist.com.evil.test/business/2026/07/19/story",
            "https://news.google.com/rss/articles/example",
            "https://www.economist.com/business/story-without-a-date",
            "https://www.economist.com/business/2026/02/30/not-a-real-date",
            "https://www.economist.com/business/2026/07/19/",
            "https://user@www.economist.com/business/2026/07/19/story",
        ]
        for url in invalid:
            with self.subTest(url=url):
                self.assertFalse(BRIDGE.looks_like_article_url(url))

    def test_podcast_video_and_audio_are_excluded(self):
        invalid = [
            "https://www.economist.com/podcasts/2026/07/18/a-daily-show",
            "https://www.economist.com/video/2026/07/18/a-video",
            "https://www.economist.com/audio/2026/07/18/an-audio-story",
            "https://www.economist.com/films/2026/07/18/a-film",
            "https://www.economist.com/special/business/2026/07/18/nested-section",
            "https://www.economist.com/business/2026/07/18/story?type=video",
            "https://www.economist.com/business/2026/07/18/story?format=podcast",
        ]
        for url in invalid:
            with self.subTest(url=url):
                self.assertFalse(BRIDGE.looks_like_article_url(url))

        title_filtered = BRIDGE.FeedItem(
            title="Podcast: a weekly Economist show",
            link="https://www.economist.com/business/2026/07/18/weekly-show",
        )
        self.assertFalse(BRIDGE.is_valid_item(title_filtered))

    def test_parser_outputs_only_direct_deduped_article_links(self):
        payload = rss_payload(
            [
                {
                    **story(1),
                    "description": "An &amp; <b>HTML</b> summary",
                    "link": story(1)["link"] + "?utm_source=latest",
                    "guid": "fc9053f0-2023-4ad1-90f5-c927662dd96a",
                },
                {**story(1), "title": "Duplicate copy"},
                story(2, section="podcasts"),
                {
                    **story(3),
                    "link": "https://news.google.com/rss/articles/not-direct",
                },
                {
                    **story(4),
                    "title": "Video: a clip rather than an article",
                },
            ]
        )

        items = BRIDGE.parse_feed(payload)

        self.assertEqual(1, len(items))
        self.assertEqual(
            "https://www.economist.com/business/2026/07/19/story-1",
            items[0].link,
        )
        self.assertEqual("An & HTML summary", items[0].description)
        self.assertEqual(
            "fc9053f0-2023-4ad1-90f5-c927662dd96a", items[0].guid
        )
        self.assertEqual(
            "Sun, 19 Jul 2026 18:42:30 +0000", items[0].pub_date
        )

    def test_xml_doctype_and_malformed_xml_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "document types"):
            BRIDGE.parse_feed("<!DOCTYPE rss><rss />")
        with self.assertRaisesRegex(ValueError, "not valid XML"):
            BRIDGE.parse_feed("<rss>")


class FakeResponse:
    def __init__(self, payload, url):
        self.payload = payload
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def geturl(self):
        return self.url

    def read(self, size=-1):
        return self.payload[:size]


class FetchSecurityTests(unittest.TestCase):
    def test_fetch_uses_bounded_official_request_and_rejects_final_url_change(self):
        source = BRIDGE.DEFAULT_UPSTREAM
        opener = mock.Mock()
        opener.open.return_value = FakeResponse(b"<rss />", source)
        with mock.patch.object(
            BRIDGE.urllib.request, "build_opener", return_value=opener
        ):
            self.assertEqual(b"<rss />", BRIDGE.fetch_url(source, timeout=3))

        request = opener.open.call_args.args[0]
        self.assertEqual(source, request.full_url)
        self.assertIn("application/rss+xml", request.get_header("Accept"))
        self.assertEqual(3, opener.open.call_args.kwargs["timeout"])

        opener.open.return_value = FakeResponse(
            b"<rss />", "https://example.test/latest/rss.xml"
        )
        with mock.patch.object(
            BRIDGE.urllib.request, "build_opener", return_value=opener
        ):
            with self.assertRaisesRegex(ValueError, "unexpected final URL"):
                BRIDGE.fetch_url(source, timeout=3)

    def test_non_official_source_is_rejected_before_network(self):
        with mock.patch.object(BRIDGE.urllib.request, "build_opener") as build:
            with self.assertRaisesRegex(ValueError, "official Economist"):
                BRIDGE.fetch_url("https://news.google.com/rss.xml", timeout=1)
        build.assert_not_called()


class ConcurrentFetchTests(unittest.TestCase):
    def test_all_sources_finish_before_global_dedupe_and_truncate(self):
        sources = [
            f"https://www.economist.com/source-{index}/rss.xml"
            for index in range(5)
        ]
        active = 0
        max_active = 0
        called = []
        lock = threading.Lock()

        def fake_fetch(source, timeout):
            nonlocal active, max_active
            with lock:
                called.append(source)
                active += 1
                max_active = max(max_active, active)
            try:
                index = int(source.split("source-")[1].split("/")[0])
                time.sleep((5 - index) * 0.003)
                article_index = index if index < 4 else 0
                return rss_payload([story(article_index)])
            finally:
                with lock:
                    active -= 1

        with mock.patch.object(BRIDGE, "fetch_url", side_effect=fake_fetch):
            batch = BRIDGE.fetch_all_with_status(
                sources,
                timeout=1,
                max_workers=3,
                max_items=3,
            )

        self.assertCountEqual(sources, called)
        self.assertGreater(max_active, 1)
        self.assertLessEqual(max_active, 3)
        self.assertEqual(sources, batch.successful_sources)
        self.assertEqual({}, batch.failed_sources)
        self.assertEqual(
            [
                "https://www.economist.com/business/2026/07/19/story-0",
                "https://www.economist.com/business/2026/07/19/story-1",
                "https://www.economist.com/business/2026/07/19/story-2",
            ],
            [item.link for item in batch.items],
        )

    def test_failure_is_isolated_and_global_cap_never_exceeds_80(self):
        sources = [BRIDGE.DEFAULT_UPSTREAM, "https://www.economist.com/bad/rss.xml"]

        def fake_fetch(source, timeout):
            if "/bad/" in source:
                raise TimeoutError("slow")
            return rss_payload([story(index) for index in range(100)])

        with mock.patch.object(BRIDGE, "fetch_url", side_effect=fake_fetch):
            batch = BRIDGE.fetch_all_with_status(
                sources,
                max_workers=2,
                max_items=999,
            )

        self.assertEqual([BRIDGE.DEFAULT_UPSTREAM], batch.successful_sources)
        self.assertEqual({sources[1]: "timeout"}, batch.failed_sources)
        self.assertEqual(80, len(batch.items))


class SnapshotAndHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.snapshot = Path(self.temp_dir.name) / "state" / "economist.json"
        self.item = BRIDGE.FeedItem(
            title="A valid last known good Economist article",
            link="https://www.economist.com/business/2026/07/19/last-known-good",
            description="summary",
            pub_date="Sun, 19 Jul 2026 18:42:30 +0000",
            guid="article-uuid",
            source="source-a",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_empty_refresh_does_not_replace_atomic_last_known_good_snapshot(self):
        good = BRIDGE.FetchBatch([self.item], ["source-a"], {})
        empty = BRIDGE.FetchBatch([], ["source-a"], {})
        state = BRIDGE.BridgeState(
            ["source-a"],
            60,
            snapshot_path=self.snapshot,
        )

        with mock.patch.object(
            BRIDGE, "fetch_all_with_status", side_effect=[good, empty]
        ):
            self.assertTrue(state.refresh_now())
            saved = self.snapshot.read_bytes()
            self.assertEqual(0o600, os.stat(self.snapshot).st_mode & 0o777)
            self.assertFalse(state.refresh_now())

        self.assertEqual(saved, self.snapshot.read_bytes())
        self.assertEqual([self.item.link], [item.link for item in state.get_items()])
        self.assertFalse(list(self.snapshot.parent.glob("*.tmp")))

        restored = BRIDGE.BridgeState(
            ["source-a"],
            60,
            snapshot_path=self.snapshot,
        )
        self.assertEqual(self.item.link, restored.get_items()[0].link)
        self.assertEqual("article-uuid", restored.get_items()[0].guid)

    def test_partial_refresh_merges_new_items_before_last_known_good(self):
        old_second = BRIDGE.FeedItem(
            title="A second article from the previous snapshot",
            link="https://www.economist.com/europe/2026/07/18/old-second",
            source="source-b",
        )
        new_item = BRIDGE.FeedItem(
            title="A newly discovered Economist article",
            link="https://www.economist.com/business/2026/07/20/new-article",
            source="source-a",
        )
        initial = BRIDGE.FetchBatch(
            [self.item, old_second], ["source-a", "source-b"], {}
        )
        partial = BRIDGE.FetchBatch(
            [new_item], ["source-a"], {"source-b": "timeout"}
        )
        complete = BRIDGE.FetchBatch(
            [new_item], ["source-a", "source-b"], {}
        )
        state = BRIDGE.BridgeState(
            ["source-a", "source-b"],
            60,
            snapshot_path=self.snapshot,
            max_items=3,
        )

        with mock.patch.object(
            BRIDGE,
            "fetch_all_with_status",
            side_effect=[initial, partial, complete],
        ):
            self.assertTrue(state.refresh_now())
            self.assertTrue(state.refresh_now())
            self.assertEqual(
                [new_item.link, self.item.link, old_second.link],
                [item.link for item in state.get_items()],
            )
            self.assertTrue(state.refresh_now())
            self.assertEqual([new_item.link], [item.link for item in state.get_items()])

    def test_health_reports_counts_sources_last_success_and_staleness(self):
        state = BRIDGE.BridgeState(
            ["source-a", "source-b"],
            10,
            stale_after_seconds=20,
        )
        state.items = [self.item]
        state.last_success = 100.0
        state.last_attempt = 105.0
        state.successful_sources = ["source-a"]
        state.failed_sources = {"source-b": "timeout"}

        healthy = state.health(now=115.0)
        self.assertTrue(healthy["ok"])
        self.assertFalse(healthy["stale"])
        self.assertEqual(1, healthy["item_count"])
        self.assertEqual(1, healthy["sources"]["successful_count"])
        self.assertEqual(1, healthy["sources"]["failed_count"])
        self.assertIsNotNone(healthy["last_success"])

        stale = state.health(now=121.0)
        self.assertFalse(stale["ok"])
        self.assertTrue(stale["stale"])

    def test_rss_shape_preserves_canonical_link_metadata_and_limit(self):
        state = BRIDGE.BridgeState([], 900)
        payload = BRIDGE.rss_xml([self.item], state)
        root = ET.fromstring(payload)
        self.assertEqual("rss", root.tag)
        item = root.find("./channel/item")
        self.assertIsNotNone(item)
        self.assertEqual(self.item.link, item.findtext("link"))
        self.assertEqual("article-uuid", item.findtext("guid"))
        self.assertEqual("false", item.find("guid").attrib["isPermaLink"])
        self.assertEqual(self.item.pub_date, item.findtext("pubDate"))

    def test_http_handlers_never_fetch_and_report_structured_health(self):
        state = BRIDGE.BridgeState([], 60)
        handler_type = BRIDGE.make_handler(state)

        rss_handler = handler_type.__new__(handler_type)
        rss_handler.path = "/economist.xml"
        rss_handler.send_response = mock.Mock()
        rss_handler.send_header = mock.Mock()
        rss_handler.end_headers = mock.Mock()
        rss_handler.wfile = io.BytesIO()
        started = time.monotonic()
        rss_handler.do_GET()
        self.assertLess(time.monotonic() - started, 0.1)
        rss_handler.send_response.assert_called_once_with(503)
        self.assertIn(b"not ready", rss_handler.wfile.getvalue())

        health_handler = handler_type.__new__(handler_type)
        health_handler.path = "/health"
        health_handler.send_response = mock.Mock()
        health_handler.send_header = mock.Mock()
        health_handler.end_headers = mock.Mock()
        health_handler.wfile = io.BytesIO()
        health_handler.do_GET()
        health = json.loads(health_handler.wfile.getvalue())
        self.assertEqual(0, health["item_count"])
        self.assertTrue(health["stale"])
        self.assertIn("successful", health["sources"])
        self.assertIn("failed", health["sources"])


if __name__ == "__main__":
    unittest.main()
