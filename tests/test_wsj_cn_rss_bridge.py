import importlib.util
import io
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "wsj_cn_rss_bridge.py"
SPEC = importlib.util.spec_from_file_location("wsj_cn_rss_bridge", SCRIPT_PATH)
assert SPEC and SPEC.loader
BRIDGE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BRIDGE
SPEC.loader.exec_module(BRIDGE)


class UrlFilteringTests(unittest.TestCase):
    def test_default_sources_use_current_life_arts_section(self):
        self.assertEqual(9, len(BRIDGE.DEFAULT_SOURCES))
        self.assertIn(
            "https://cn.wsj.com/zh-hans/news/life-arts",
            BRIDGE.DEFAULT_SOURCES,
        )
        self.assertNotIn(
            "https://cn.wsj.com/zh-hans/news/life",
            BRIDGE.DEFAULT_SOURCES,
        )

    def test_only_https_canonical_wsj_article_urls_are_allowed(self):
        valid = "https://cn.wsj.com/articles/example-story-12345678?mod=hp#top"
        self.assertTrue(BRIDGE.looks_like_article_url(valid))
        self.assertEqual(
            "https://cn.wsj.com/articles/example-story-12345678",
            BRIDGE.normalize_link(valid),
        )

        invalid = [
            "http://cn.wsj.com/articles/example-story-12345678",
            "https://www.wsj.com/articles/example-story-12345678",
            "https://cn.wsj.com/zh-hans/news/china",
            "https://cn.wsj.com.evil.test/articles/example-story-12345678",
            "https://cn.wsj.com:443/articles/example-story-12345678",
            "https://cn.wsj.com/articles/",
        ]
        for url in invalid:
            with self.subTest(url=url):
                self.assertFalse(BRIDGE.looks_like_article_url(url))

    def test_video_items_are_excluded(self):
        page = (
            '<a href="/video/watch-this">Video page long title</a>'
            '<a href="/articles/video-item-12345678?type=video">Video item long title</a>'
            '<a href="/articles/normal-item-12345678">视频：这是一个视频条目</a>'
            '<a href="/articles/real-item-12345678">这是一个有效的文章标题</a>'
        )
        items = BRIDGE.parse_anchor_fallback(page, "https://cn.wsj.com/")
        self.assertEqual(
            ["https://cn.wsj.com/articles/real-item-12345678"],
            [item.link for item in items],
        )

    def test_next_data_video_metadata_is_excluded(self):
        payload = {
            "props": {
                "items": [
                    {
                        "type": "video",
                        "title": "A long video title",
                        "url": "/articles/video-metadata-12345678",
                    },
                    {
                        "type": "article",
                        "title": "A valid article title",
                        "url": "/articles/article-metadata-12345678",
                    },
                ]
            }
        }
        page = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(payload) + "</script>"
        items = BRIDGE.parse_next_data(page, "https://cn.wsj.com/")
        self.assertEqual(1, len(items))
        self.assertIn("article-metadata", items[0].link)


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self, size=-1):
        return self.payload if size < 0 else self.payload[:size]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class BpcListClientTests(unittest.TestCase):
    source = "https://cn.wsj.com/zh-hans/news/china"
    request_id = "wsj-list:unit-test"

    def _success(self, **overrides):
        listing = {
            "url": self.source,
            "canonicalUrl": self.source,
            "status": 200,
            "html": '<a href="/articles/story-12345678">A valid article title</a>',
            "fetchedAt": "2026-07-20T00:00:00Z",
        }
        listing.update(overrides)
        return {
            "ok": True,
            "code": "OK",
            "retryable": False,
            "requestId": self.request_id,
            "list": listing,
        }

    def test_posts_authenticated_json_to_loopback_bpc(self):
        opener = mock.Mock()
        opener.open.return_value = FakeResponse(self._success())
        with (
            mock.patch.dict(
                BRIDGE.os.environ,
                {
                    "BPC_BASE_URL": "http://127.0.0.1:8080",
                    "BPC_API_TOKEN": "test-token",
                },
                clear=True,
            ),
            mock.patch.object(BRIDGE, "_new_list_request_id", return_value=self.request_id),
            mock.patch.object(BRIDGE.urllib.request, "build_opener", return_value=opener),
        ):
            page = BRIDGE.fetch_url(self.source, timeout=120)

        request = opener.open.call_args.args[0]
        self.assertEqual("http://127.0.0.1:8080/v1/list", request.full_url)
        self.assertEqual("POST", request.get_method())
        self.assertEqual("Bearer test-token", request.get_header("Authorization"))
        self.assertIsNone(request.get_header("Cookie"))
        self.assertEqual(
            {"url": self.source, "requestId": self.request_id},
            json.loads(request.data),
        )
        self.assertEqual(120, opener.open.call_args.kwargs["timeout"])
        self.assertIn("valid article title", page)

    def test_non_loopback_base_url_is_rejected_before_open(self):
        with (
            mock.patch.dict(
                BRIDGE.os.environ,
                {"BPC_BASE_URL": "https://example.test", "BPC_API_TOKEN": "must-not-leak"},
                clear=True,
            ),
            mock.patch.object(BRIDGE.urllib.request, "build_opener") as build_opener,
        ):
            with self.assertRaisesRegex(ValueError, "loopback"):
                BRIDGE.fetch_url(self.source, timeout=1)
        build_opener.assert_not_called()

    def test_missing_bpc_token_fails_closed(self):
        with mock.patch.dict(
            BRIDGE.os.environ,
            {"BPC_BASE_URL": "http://127.0.0.1:8080", "BPC_API_TOKEN": ""},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "BPC_API_TOKEN is required"):
                BRIDGE.fetch_url(self.source, timeout=1)

    def test_structured_bpc_error_is_safe_and_preserves_retryable(self):
        body = io.BytesIO(
            json.dumps(
                {"ok": False, "code": "SERVICE_NOT_READY", "retryable": True}
            ).encode("utf-8")
        )
        error = urllib.error.HTTPError(
            "http://127.0.0.1:8080/v1/list",
            503,
            "Service Unavailable",
            {},
            body,
        )
        opener = mock.Mock()
        opener.open.side_effect = error
        with (
            mock.patch.dict(
                BRIDGE.os.environ,
                {
                    "BPC_BASE_URL": "http://127.0.0.1:8080",
                    "BPC_API_TOKEN": "test-token",
                },
                clear=True,
            ),
            mock.patch.object(BRIDGE, "_new_list_request_id", return_value=self.request_id),
            mock.patch.object(BRIDGE.urllib.request, "build_opener", return_value=opener),
        ):
            with self.assertRaises(BRIDGE.BpcListError) as raised:
                BRIDGE.fetch_url(self.source, timeout=1)

        self.assertEqual(503, raised.exception.status)
        self.assertEqual("SERVICE_NOT_READY", raised.exception.code)
        self.assertTrue(raised.exception.retryable)

    def test_untrusted_final_url_and_mismatched_request_id_are_rejected(self):
        cases = [
            self._success(canonicalUrl="https://example.test/listing"),
            {**self._success(), "requestId": "another-request"},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                opener = mock.Mock()
                opener.open.return_value = FakeResponse(payload)
                with (
                    mock.patch.dict(
                        BRIDGE.os.environ,
                        {
                            "BPC_BASE_URL": "http://127.0.0.1:8080",
                            "BPC_API_TOKEN": "test-token",
                        },
                        clear=True,
                    ),
                    mock.patch.object(
                        BRIDGE,
                        "_new_list_request_id",
                        return_value=self.request_id,
                    ),
                    mock.patch.object(
                        BRIDGE.urllib.request,
                        "build_opener",
                        return_value=opener,
                    ),
                ):
                    with self.assertRaises(ValueError):
                        BRIDGE.fetch_url(self.source, timeout=1)


class ConcurrentFetchTests(unittest.TestCase):
    def test_all_sources_finish_before_global_dedupe_and_truncate(self):
        sources = [f"https://cn.wsj.com/listing/{index}" for index in range(9)]
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
                # Reverse delays force completion order to differ from source order.
                index = int(source.rsplit("/", 1)[1])
                time.sleep((9 - index) * 0.002)
                article_index = index if index < 8 else 0
                return (
                    '<a href="/articles/story-'
                    f'{article_index:02d}-12345678?mod=listing">'
                    f"Valid article title {article_index}</a>"
                )
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
        self.assertLessEqual(max_active, 3)
        self.assertEqual(sources, batch.successful_sources)
        self.assertEqual({}, batch.failed_sources)
        self.assertEqual(
            [
                "https://cn.wsj.com/articles/story-00-12345678",
                "https://cn.wsj.com/articles/story-01-12345678",
                "https://cn.wsj.com/articles/story-02-12345678",
            ],
            [item.link for item in batch.items],
        )

    def test_one_failed_source_does_not_discard_successful_sources(self):
        sources = ["good", "bad"]

        def fake_fetch(source, timeout):
            if source == "bad":
                raise TimeoutError("slow")
            return '<a href="/articles/good-story-12345678">A sufficiently long headline</a>'

        with mock.patch.object(BRIDGE, "fetch_url", side_effect=fake_fetch):
            batch = BRIDGE.fetch_all_with_status(sources, max_workers=2)

        self.assertEqual(["good"], batch.successful_sources)
        self.assertEqual({"bad": "timeout"}, batch.failed_sources)
        self.assertEqual(1, len(batch.items))


class SnapshotAndHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.snapshot = Path(self.temp_dir.name) / "state" / "wsj.json"
        self.item = BRIDGE.FeedItem(
            title="A valid last known good article",
            link="https://cn.wsj.com/articles/last-known-good-12345678?mod=hp",
            description="summary",
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

        with mock.patch.object(BRIDGE, "fetch_all_with_status", side_effect=[good, empty]):
            self.assertTrue(state.refresh_now())
            saved = self.snapshot.read_bytes()
            self.assertFalse(state.refresh_now())

        self.assertEqual(saved, self.snapshot.read_bytes())
        self.assertEqual(1, len(state.get_items()))
        self.assertFalse(list(self.snapshot.parent.glob("*.tmp")))

        restored = BRIDGE.BridgeState(
            ["source-a"],
            60,
            snapshot_path=self.snapshot,
        )
        self.assertEqual(
            "https://cn.wsj.com/articles/last-known-good-12345678",
            restored.get_items()[0].link,
        )

    def test_partial_refresh_merges_new_items_before_last_known_good(self):
        old_second = BRIDGE.FeedItem(
            title="A second article from the previous snapshot",
            link="https://cn.wsj.com/articles/old-second-12345678",
            source="source-b",
        )
        new_item = BRIDGE.FeedItem(
            title="A newly discovered article",
            link="https://cn.wsj.com/articles/new-article-12345678",
            source="source-a",
        )
        duplicate_old = BRIDGE.FeedItem(
            title="The retained old article from a successful source",
            link="https://cn.wsj.com/articles/last-known-good-12345678?mod=latest",
            source="source-a",
        )
        initial = BRIDGE.FetchBatch(
            [self.item, old_second],
            ["source-a", "source-b"],
            {},
        )
        partial = BRIDGE.FetchBatch(
            [new_item, duplicate_old],
            ["source-a"],
            {"source-b": "timeout"},
        )
        complete = BRIDGE.FetchBatch(
            [new_item],
            ["source-a", "source-b"],
            {},
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
                [
                    "https://cn.wsj.com/articles/new-article-12345678",
                    "https://cn.wsj.com/articles/last-known-good-12345678",
                    "https://cn.wsj.com/articles/old-second-12345678",
                ],
                [item.link for item in state.get_items()],
            )

            restored = BRIDGE.BridgeState(
                ["source-a", "source-b"],
                60,
                snapshot_path=self.snapshot,
                max_items=3,
            )
            self.assertEqual(
                [item.link for item in state.get_items()],
                [item.link for item in restored.get_items()],
            )

            # A complete refresh remains an exact replacement, dropping stale LKG.
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

    def test_rss_shape_and_canonical_guid_remain_compatible(self):
        state = BRIDGE.BridgeState([], 60)
        payload = BRIDGE.rss_xml([self.item], state)
        root = ET.fromstring(payload)
        self.assertEqual("rss", root.tag)
        item = root.find("./channel/item")
        self.assertIsNotNone(item)
        self.assertEqual(
            "https://cn.wsj.com/articles/last-known-good-12345678",
            item.findtext("link"),
        )
        self.assertEqual(item.findtext("link"), item.findtext("guid"))

    def test_rss_returns_fast_503_until_first_snapshot_is_ready(self):
        state = BRIDGE.BridgeState([], 60)
        handler_type = BRIDGE.make_handler(state)
        handler = handler_type.__new__(handler_type)
        handler.path = "/wsj-cn.xml"
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.wfile = io.BytesIO()

        started = time.monotonic()
        handler.do_GET()
        elapsed = time.monotonic() - started

        handler.send_response.assert_called_once_with(503)
        self.assertIn(b"not ready", handler.wfile.getvalue())
        self.assertLess(elapsed, 0.1)


if __name__ == "__main__":
    unittest.main()
