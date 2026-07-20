# coding=utf-8

import json
import os
import socket
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import requests

from trendradar.crawler.rss import filter_notification_items
from trendradar.ai.filter import AIFilterResult
from trendradar.ai.filter_pipeline import AIFilterPipeline
from trendradar.wsj_delivery.clients import (
    BPCClient,
    DownloadedImage,
    FeedClient,
    FeishuClient,
    ImageDownloader,
    build_document_plan,
    build_document_blocks,
    build_summary_card,
    message_uuid,
    partition_summary_cards,
)
from trendradar.wsj_delivery.models import (
    DeliveryConfig,
    DeliveryError,
    FeedArticle,
    FetchedArticle,
    InitializationRequired,
    UncertainRemoteResult,
    extract_article_id,
    make_article_key,
    normalize_wsj_url,
)
from trendradar.wsj_delivery.outbox import Outbox
from trendradar.wsj_delivery.service import DeliveryRunner


def png_bytes(width=2, height=2, payload=b"payload"):
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + int(width).to_bytes(4, "big")
        + int(height).to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
        + b"\x00\x00\x00\x00"
        + payload
    )


class MockResponse:
    def __init__(self, payload=None, status_code=200, *, content=b"", headers=None):
        self.payload = {} if payload is None else payload
        self.status_code = status_code
        self.content = content
        self.text = content.decode("utf-8", "replace")
        self.headers = headers or {}

    def json(self):
        return self.payload


class FeedSession:
    def __init__(self, response):
        self.response = response
        self.headers = {}
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        return self.response


class PostSession:
    def __init__(self, responses):
        self.responses = list(responses)

    def post(self, *args, **kwargs):
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class FeishuSession:
    def __init__(self, request_responses=None):
        self.request_responses = list(request_responses or [])
        self.requests = []
        self.token_calls = 0

    def post(self, url, **kwargs):
        self.token_calls += 1
        return MockResponse({"code": 0, "tenant_access_token": "test-token", "expire": 7200})

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if self.request_responses:
            value = self.request_responses.pop(0)
            if isinstance(value, BaseException):
                raise value
            return value
        return MockResponse({"code": 0, "data": {}})


class UploadSession(FeishuSession):
    def __init__(self, upload_response):
        super().__init__()
        self.upload_response = upload_response
        self.upload_requests = []

    def post(self, url, **kwargs):
        if url.endswith("/drive/v1/medias/upload_all"):
            self.upload_requests.append((url, kwargs))
            if isinstance(self.upload_response, BaseException):
                raise self.upload_response
            return self.upload_response
        return super().post(url, **kwargs)


class FakeClock:
    def __init__(self, value=1000.0):
        self.value = float(value)
        self.sleeps = []

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds

    def advance(self, seconds):
        self.value += seconds


class FakeFeed:
    def __init__(self, items, error=None):
        self.items = items
        self.error = error
        self.calls = 0

    def fetch(self):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self.items)


class FakeBPC:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def fetch(self, url, request_id, fallback_title=""):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result or fetched_article(request_id=request_id, title=fallback_title)


class FakeImageDownloader:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def download(self, url):
        self.calls.append(url)
        if self.error:
            raise self.error
        data = png_bytes(payload=b"image-data")
        import hashlib

        return DownloadedImage(
            source_url=url,
            final_url=url,
            data=data,
            mime_type="image/png",
            extension="png",
            sha256=hashlib.sha256(data).hexdigest(),
            width=640,
            height=360,
        )


class ImageResponse(MockResponse):
    def __init__(self, data=b"", status_code=200, headers=None):
        super().__init__({}, status_code, content=data, headers=headers)
        self.data = data
        self.closed = False

    def iter_content(self, chunk_size=65536):
        for index in range(0, len(self.data), chunk_size):
            yield self.data[index : index + chunk_size]

    def close(self):
        self.closed = True


class ImageSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class FakeFeishu:
    def __init__(self):
        self.create_calls = 0
        self.append_calls = []
        self.share_calls = 0
        self.card_calls = []
        self.alert_calls = []
        self.share_failures = 0
        self.card_failures = 0
        self.create_error = None
        self.append_error = None
        self.share_error = None
        self.card_error = None
        self.upload_calls = []
        self.replace_calls = []
        self.upload_error = None
        self.replace_error = None
        self.delete_calls = []
        self.delete_error = None

    def create_document(self, title):
        self.create_calls += 1
        if self.create_error:
            raise self.create_error
        return f"docTokenModern{self.create_calls:013d}"

    def append_blocks(self, document_id, blocks, index, client_token):
        self.append_calls.append((document_id, list(blocks), index, client_token))
        if self.append_error:
            raise self.append_error
        if len(blocks) == 1 and blocks[0].get("block_type") == 27:
            return [{"block_id": "doxcnImageBlock0000000000001", "block_type": 27}]
        return []

    def upload_image(self, document_id, image_block_id, image):
        self.upload_calls.append((document_id, image_block_id, image.sha256))
        if self.upload_error:
            raise self.upload_error
        return "boxcnImageToken0000000000001"

    def replace_image(
        self, document_id, image_block_id, file_token, client_token, width=0, height=0
    ):
        self.replace_calls.append(
            (document_id, image_block_id, file_token, client_token, width, height)
        )
        if self.replace_error:
            raise self.replace_error

    def share_document(self, document_id):
        self.share_calls += 1
        if self.share_error:
            raise self.share_error
        if self.share_failures:
            self.share_failures -= 1
            raise DeliveryError("FEISHU_SHARE_503", "share failed", retryable=True)

    def send_card(self, card, value):
        self.card_calls.append((card, value))
        if self.card_error:
            raise self.card_error
        if self.card_failures:
            self.card_failures -= 1
            raise DeliveryError("FEISHU_SEND_503", "send failed", retryable=True)

    def send_alert(self, title, content, value):
        self.alert_calls.append((title, content, value))

    def delete_document(self, document_id):
        self.delete_calls.append(document_id)
        if self.delete_error:
            raise self.delete_error


def feed_article(suffix="abcdef123456", query=""):
    url = f"https://cn.wsj.com/articles/test-story-{suffix}{query}"
    normalized = normalize_wsj_url(url)
    key, article_id = make_article_key(normalized)
    return FeedArticle(
        article_key=key,
        article_id=article_id,
        normalized_url=normalized,
        source_url=url,
        title=f"测试文章 {suffix}",
        published_at="2026-07-19T10:00:00+08:00",
        author="记者",
    )


def fetched_article(request_id="request", title="测试标题", paragraph_count=3):
    paragraphs = tuple(f"第 {index} 段 " + "正文" * 120 for index in range(paragraph_count))
    text = "\n\n".join(paragraphs)
    import hashlib

    return FetchedArticle(
        canonical_url="https://cn.wsj.com/articles/test-story-abcdef123456",
        title=title or "测试标题",
        author="记者",
        published_at="2026-07-19T10:00:00+08:00",
        paragraphs=paragraphs,
        text=text,
        sha256=hashlib.sha256(text.encode()).hexdigest(),
        fetched_at="2026-07-19T02:01:00Z",
        request_id=request_id,
    )


def fetched_article_with_image(request_id="request"):
    article = fetched_article(request_id=request_id)
    return replace(
        article,
        body_items=(
            {"type": "paragraph", "text": article.paragraphs[0]},
            {
                "type": "image",
                "url": "https://images.wsj.net/im-article-1?width=1200",
                "alt": "article image",
                "caption": "图片说明",
            },
            {"type": "paragraph", "text": article.paragraphs[1]},
            {"type": "paragraph", "text": article.paragraphs[2]},
        ),
    )


def config_for(path, **overrides):
    values = dict(
        bpc_api_token="test-bpc",
        feishu_app_id="test-app",
        feishu_app_secret="test-secret",
        feishu_receive_id="oc_test",
        feishu_doc_url_prefix="https://tenant.feishu.cn/docx",
        db_path=Path(path),
        max_items_per_run=20,
        retry_base_seconds=1,
        retry_max_seconds=10,
        circuit_min_seconds=10,
        max_drain_seconds=60,
        alert_cooldown_seconds=21600,
        block_interval_seconds=0.4,
    )
    values.update(overrides)
    return DeliveryConfig(**values)


class URLAndFeedTests(unittest.TestCase):
    def test_normalizes_tracking_and_prefers_article_id(self):
        normalized = normalize_wsj_url(
            "https://cn.wsj.com/articles/story-ecb497c1/?mod=hp_lead_pos1&utm_source=x#frag"
        )
        self.assertEqual("https://cn.wsj.com/articles/story-ecb497c1", normalized)
        self.assertEqual("ecb497c1", extract_article_id(normalized))
        key, article_id = make_article_key(normalized)
        self.assertEqual("wsj:ecb497c1", key)
        self.assertEqual("ecb497c1", article_id)

    def test_rejects_wrong_host_non_https_and_video(self):
        for value in (
            "http://cn.wsj.com/articles/a",
            "https://www.wsj.com/articles/a",
            "https://cn.wsj.com/video/articles/a",
            "https://cn.wsj.com/articles/a?type=video",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_wsj_url(value)

    def test_feed_global_dedup_and_video_filter(self):
        xml = b"""<?xml version='1.0'?><rss version='2.0'><channel>
        <item><title>One</title><link>https://cn.wsj.com/articles/story-abcdef123456?mod=home</link></item>
        <item><title>Duplicate</title><link>https://cn.wsj.com/articles/story-abcdef123456?utm_source=x</link></item>
        <item><title>[\xe8\xa7\x86\xe9\xa2\x91] Skip</title><link>https://cn.wsj.com/articles/video-story-fedcba654321</link></item>
        <item><title>Wrong host</title><link>https://www.wsj.com/articles/story-111111111111</link></item>
        </channel></rss>"""
        session = FeedSession(MockResponse(content=xml, headers={"Content-Type": "application/rss+xml"}))
        items = FeedClient("http://127.0.0.1/feed", 2, session=session).fetch()
        self.assertEqual(1, len(items))
        self.assertEqual("wsj:abcdef123456", items[0].article_key)

    def test_notify_false_filters_presentation_but_not_input(self):
        items = {"wsj-cn": [1], "economist": [2]}
        result = filter_notification_items(
            items,
            [
                {"id": "wsj-cn", "notify": False},
                {"id": "economist", "notify": True},
            ],
        )
        self.assertEqual({"economist": [2]}, result)
        self.assertEqual({"wsj-cn": [1], "economist": [2]}, items)

    def test_ai_filter_also_suppresses_notify_false_rss(self):
        pipeline = AIFilterPipeline(
            {
                "RSS": {
                    "ENABLED": True,
                    "FEEDS": [
                        {"id": "wsj-cn", "notify": False},
                        {"id": "economist", "notify": True},
                    ],
                }
            },
            storage_manager=None,
            get_time_func=lambda: None,
        )
        result = AIFilterResult(
            success=True,
            tags=[
                {
                    "tag": "财经",
                    "items": [
                        {
                            "title": "WSJ hidden",
                            "source_id": "wsj-cn",
                            "source_name": "WSJ",
                            "source_type": "rss",
                            "url": "https://cn.wsj.com/articles/x-ecb497c1",
                        },
                        {
                            "title": "Economist visible",
                            "source_id": "economist",
                            "source_name": "Economist",
                            "source_type": "rss",
                            "url": "https://example.com/visible",
                        },
                    ],
                }
            ],
        )
        _, rss_stats, _ = pipeline.convert_to_report_data(result)
        self.assertEqual(1, rss_stats[0]["count"])
        self.assertEqual("Economist visible", rss_stats[0]["titles"][0]["title"])


class BPCClientTests(unittest.TestCase):
    def test_loopback_clients_ignore_environment_proxies(self):
        feed_session = requests.Session()
        bpc_session = requests.Session()
        self.assertTrue(feed_session.trust_env)
        self.assertTrue(bpc_session.trust_env)
        FeedClient("http://127.0.0.1:4555/feed", 1, session=feed_session)
        BPCClient("http://127.0.0.1:8080", "token", 1, session=bpc_session)
        self.assertFalse(feed_session.trust_env)
        self.assertFalse(bpc_session.trust_env)

    def test_error_contract_classification(self):
        cases = [
            (403, {"ok": False, "code": "URL_NOT_ALLOWED", "retryable": False}, False),
            (422, {"ok": False, "code": "DATADOME_CHALLENGE", "retryable": True}, True),
            (429, {"ok": False, "code": "QUEUE_FULL", "retryable": True}, False),
            (503, {"ok": False, "code": "BROWSER_CRASH", "retryable": True}, False),
            (503, {"ok": False, "code": "SESSION_PERSIST_FAILED", "retryable": True}, True),
        ]
        for status_code, payload, systemic in cases:
            with self.subTest(payload=payload):
                client = BPCClient(
                    "http://127.0.0.1:8080",
                    "token",
                    1,
                    session=PostSession([MockResponse(payload, status_code)]),
                )
                with self.assertRaises(DeliveryError) as caught:
                    client.fetch(feed_article().normalized_url, "req")
                self.assertEqual(systemic, caught.exception.systemic)

    def test_timeout_and_short_body_are_rejected(self):
        timeout_client = BPCClient(
            "http://127.0.0.1:8080", "token", 1, session=PostSession([requests.Timeout()])
        )
        with self.assertRaises(DeliveryError) as caught:
            timeout_client.fetch(feed_article().normalized_url, "req")
        self.assertEqual("BPC_TIMEOUT", caught.exception.code)

        short = {
            "ok": True,
            "article": {
                "status": 200,
                "canonicalUrl": feed_article().normalized_url,
                "title": "short",
                "paragraphs": ["a", "b", "c"],
            },
        }
        short_client = BPCClient(
            "http://127.0.0.1:8080", "token", 1, session=PostSession([MockResponse(short)])
        )
        with self.assertRaises(DeliveryError) as caught:
            short_client.fetch(feed_article().normalized_url, "req")
        self.assertEqual("BPC_QUALITY_GATE", caught.exception.code)

    def test_complete_short_chinese_article_just_under_500_chars_is_accepted(self):
        paragraphs = ["甲" * 166, "乙" * 164, "丙" * 164]
        payload = {
            "ok": True,
            "article": {
                "status": 200,
                "canonicalUrl": feed_article().normalized_url,
                "title": "legitimate short article",
                "paragraphs": paragraphs,
            },
        }
        article = BPCClient(
            "http://127.0.0.1:8080",
            "token",
            1,
            session=PostSession([MockResponse(payload)]),
        ).fetch(feed_article().normalized_url, "req")
        self.assertEqual(498, len(article.text))

    def test_article_images_are_strictly_merged_by_paragraph_slot(self):
        paragraphs = ["正文" * 100, "第二段" * 80, "第三段" * 80]
        payload = {
            "ok": True,
            "article": {
                "status": 200,
                "canonicalUrl": feed_article().normalized_url,
                "title": "with images",
                "paragraphs": paragraphs,
                "images": [
                    {
                        "url": "https://images.wsj.net/im-1?width=800#fragment",
                        "afterParagraph": -1,
                        "caption": "头图",
                    },
                    {
                        "url": "https://images.wsj.net/im-2",
                        "afterParagraph": 0,
                    },
                    # OpenGraph `/social` and in-body resized renditions are
                    # the same immutable WSJ `im-*` asset.
                    {
                        "url": "https://images.wsj.net/im-1/social",
                        "afterParagraph": 0,
                    },
                    # A final-paragraph/footer slot is deliberately refused.
                    {"url": "https://images.wsj.net/recommend", "afterParagraph": 2},
                    # A publisher-proven article-tail gallery image is allowed.
                    {
                        "url": "https://images.wsj.net/im-tail",
                        "afterParagraph": 2,
                        "articleTail": True,
                    },
                    # Exact host match: deceptive subdomains are not accepted.
                    {"url": "https://evil.images.wsj.net/im-3", "afterParagraph": 0},
                ],
            },
        }
        article = BPCClient(
            "http://127.0.0.1:8080",
            "token",
            1,
            session=PostSession([MockResponse(payload)]),
        ).fetch(feed_article().normalized_url, "req")
        self.assertEqual(
            ["image", "paragraph", "image", "paragraph", "paragraph", "image"],
            [item["type"] for item in article.body_items],
        )
        self.assertEqual("https://images.wsj.net/im-1?width=800", article.body_items[0]["url"])


class ImageDownloaderTests(unittest.TestCase):
    @staticmethod
    def public_dns(*args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ]

    def test_valid_png_is_bounded_and_no_credentials_are_forwarded(self):
        data = png_bytes()
        session = ImageSession(
            [ImageResponse(data, headers={"Content-Type": "image/png", "Content-Length": str(len(data))})]
        )
        session.headers.update({"Cookie": "secret", "Authorization": "Bearer bpc-secret"})
        downloader = ImageDownloader(session=session, resolver=self.public_dns)
        result = downloader.download("https://images.wsj.net/im-1#fragment")
        self.assertEqual("image/png", result.mime_type)
        self.assertEqual("https://images.wsj.net/im-1", result.final_url)
        self.assertNotIn("Cookie", session.headers)
        self.assertNotIn("Authorization", session.headers)
        self.assertFalse(session.calls[0][1]["allow_redirects"])

    def test_exact_host_port_and_credentials_are_enforced(self):
        bad_urls = (
            "https://evil.images.wsj.net/im-1",
            "https://images.wsj.net.evil.test/im-1",
            "https://images.wsj.net:444/im-1",
            "https://user:pass" + "@images.wsj.net/im-1",
            "http://images.wsj.net/im-1",
        )
        for url in bad_urls:
            with self.subTest(url=url):
                session = ImageSession([])
                downloader = ImageDownloader(session=session, resolver=self.public_dns)
                with self.assertRaises(DeliveryError) as caught:
                    downloader.download(url)
                self.assertEqual("IMAGE_URL_NOT_ALLOWED", caught.exception.code)
                self.assertEqual([], session.calls)

    def test_each_redirect_is_revalidated(self):
        first = ImageResponse(
            b"", status_code=302, headers={"Location": "https://evil.images.wsj.net/tracker.png"}
        )
        session = ImageSession([first])
        downloader = ImageDownloader(session=session, resolver=self.public_dns)
        with self.assertRaises(DeliveryError) as caught:
            downloader.download("https://images.wsj.net/im-1")
        self.assertEqual("IMAGE_URL_NOT_ALLOWED", caught.exception.code)
        self.assertEqual(1, len(session.calls))
        self.assertTrue(first.closed)

    def test_private_dns_size_and_magic_are_blocked(self):
        private = ImageDownloader(
            session=ImageSession([]),
            resolver=lambda *a, **k: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
            ],
        )
        with self.assertRaises(DeliveryError) as caught:
            private.download("https://images.wsj.net/im-1")
        self.assertEqual("IMAGE_SSRF_BLOCKED", caught.exception.code)

        too_large = ImageDownloader(
            session=ImageSession(
                [ImageResponse(b"", headers={"Content-Type": "image/png", "Content-Length": "999"})]
            ),
            resolver=self.public_dns,
            max_bytes=100,
        )
        with self.assertRaises(DeliveryError) as caught:
            too_large.download("https://images.wsj.net/im-1")
        self.assertEqual("IMAGE_SIZE_INVALID", caught.exception.code)

        bad_magic = ImageDownloader(
            session=ImageSession(
                [ImageResponse(b"<html>not an image</html>", headers={"Content-Type": "image/png"})]
            ),
            resolver=self.public_dns,
        )
        with self.assertRaises(DeliveryError) as caught:
            bad_magic.download("https://images.wsj.net/im-1")
        self.assertEqual("IMAGE_TYPE_INVALID", caught.exception.code)

        pixel_bomb = ImageDownloader(
            session=ImageSession(
                [ImageResponse(png_bytes(10_000, 10_000), headers={"Content-Type": "image/png"})]
            ),
            resolver=self.public_dns,
            max_pixels=40_000_000,
        )
        with self.assertRaises(DeliveryError) as caught:
            pixel_bomb.download("https://images.wsj.net/im-1")
        self.assertEqual("IMAGE_DIMENSIONS_INVALID", caught.exception.code)

    def test_stream_failure_and_declared_length_mismatch_are_retryable(self):
        class BrokenStream(ImageResponse):
            def iter_content(self, chunk_size=65536):
                yield b"\x89PNG\r\n\x1a\n"
                raise requests.exceptions.ChunkedEncodingError("truncated")

        broken = ImageDownloader(
            session=ImageSession(
                [BrokenStream(b"", headers={"Content-Type": "image/png"})]
            ),
            resolver=self.public_dns,
        )
        with self.assertRaises(DeliveryError) as caught:
            broken.download("https://images.wsj.net/im-1")
        self.assertEqual("IMAGE_DOWNLOAD_INTERRUPTED", caught.exception.code)
        self.assertTrue(caught.exception.retryable)

        data = png_bytes()
        mismatch = ImageDownloader(
            session=ImageSession(
                [
                    ImageResponse(
                        data,
                        headers={"Content-Type": "image/png", "Content-Length": str(len(data) + 5)},
                    )
                ]
            ),
            resolver=self.public_dns,
        )
        with self.assertRaises(DeliveryError) as caught:
            mismatch.download("https://images.wsj.net/im-1")
        self.assertEqual("IMAGE_LENGTH_MISMATCH", caught.exception.code)
        self.assertTrue(caught.exception.retryable)


class FeishuClientTests(unittest.TestCase):
    def test_image_upload_and_replace_follow_official_three_step_contract(self):
        session = UploadSession(
            MockResponse({"code": 0, "data": {"file_token": "boxcnToken"}})
        )
        client = FeishuClient("app", "secret", "oc_test", "chat_id", session=session)
        client._access_token = "test-token"
        client._token_expires_at = 10**12
        image = FakeImageDownloader().download("https://images.wsj.net/im-1")
        token = client.upload_image("doxcnDocument", "doxcnImageBlock", image)
        self.assertEqual("boxcnToken", token)
        upload = session.upload_requests[0][1]
        self.assertEqual("docx_image", upload["data"]["parent_type"])
        self.assertEqual("doxcnImageBlock", upload["data"]["parent_node"])
        self.assertEqual(
            {"drive_route_token": "doxcnDocument"},
            json.loads(upload["data"]["extra"]),
        )
        self.assertEqual(image.data, upload["files"]["file"][1])

        client.replace_image(
            "doxcnDocument",
            "doxcnImageBlock",
            token,
            "bind-client-token",
            image.width,
            image.height,
        )
        request = session.requests[-1]
        self.assertEqual("PATCH", request[0])
        self.assertTrue(request[1].endswith("/documents/doxcnDocument/blocks/doxcnImageBlock"))
        self.assertEqual(
            {
                "replace_image": {
                    "token": token,
                    "width": image.width,
                    "height": image.height,
                }
            },
            request[2]["json"],
        )
        self.assertEqual("bind-client-token", request[2]["params"]["client_token"])

    def test_image_upload_network_result_is_unknown_not_retried(self):
        session = UploadSession(requests.ConnectionError("lost"))
        client = FeishuClient("app", "secret", "oc_test", "chat_id", session=session)
        client._access_token = "test-token"
        client._token_expires_at = 10**12
        image = FakeImageDownloader().download("https://images.wsj.net/im-1")
        with self.assertRaises(UncertainRemoteResult):
            client.upload_image("doc", "block", image)
        self.assertEqual(1, len(session.upload_requests))

    def test_block_batches_and_400ms_spacing(self):
        clock = FakeClock(0)
        session = FeishuSession()
        client = FeishuClient(
            "app",
            "secret",
            "oc_test",
            "chat_id",
            session=session,
            clock=clock,
            sleep=clock.sleep,
            block_interval=0.4,
        )
        block = {"block_type": 2, "text": {"elements": [], "style": {}}}
        client.append_blocks("doc", [block] * 50, 0, "token-1")
        client.append_blocks("doc", [block], 50, "token-2")
        self.assertEqual([0.4], clock.sleeps)
        self.assertEqual(50, len(session.requests[0][2]["json"]["children"]))
        self.assertEqual("token-2", session.requests[1][2]["params"]["client_token"])

    def test_append_then_replace_image_share_400ms_docx_edit_spacing(self):
        clock = FakeClock(0)
        session = FeishuSession()
        client = FeishuClient(
            "app",
            "secret",
            "oc_test",
            "chat_id",
            session=session,
            clock=clock,
            sleep=clock.sleep,
            block_interval=0.4,
        )
        client.append_blocks(
            "doc", [{"block_type": 27, "image": {}}], 0, "create-image"
        )
        client.replace_image("doc", "image-block", "file-token", "bind-image")
        self.assertEqual([0.4], clock.sleeps)
        self.assertEqual("PATCH", session.requests[1][0])

    def test_document_delete_uses_sync_docx_contract_and_is_rate_limited(self):
        clock = FakeClock(0)
        session = FeishuSession(
            [MockResponse({"code": 0, "data": {}}), MockResponse({"code": 0})]
        )
        client = FeishuClient(
            "app", "secret", "oc_test", "chat_id",
            session=session, clock=clock, sleep=clock.sleep,
        )
        client.delete_document("AbCdEfGhIjKlMnOpQrStUvWxYz1")
        client.delete_document("AbCdEfGhIjKlMnOpQrStUvWxYz2")
        self.assertEqual([0.4], clock.sleeps)
        first = session.requests[0]
        self.assertEqual("DELETE", first[0])
        self.assertTrue(first[1].endswith("/drive/v1/files/AbCdEfGhIjKlMnOpQrStUvWxYz1"))
        self.assertEqual({"type": "docx"}, first[2]["params"])

    def test_document_delete_retries_internal_error_and_accepts_absence(self):
        clock = FakeClock(0)
        session = FeishuSession(
            [
                MockResponse({"code": 1061001, "msg": "internal error"}, 400),
                MockResponse({"code": 1061003, "msg": "not found"}, 404),
            ]
        )
        client = FeishuClient(
            "app", "secret", "oc_test", "chat_id",
            session=session, clock=clock, sleep=clock.sleep,
        )
        client.delete_document("AbCdEfGhIjKlMnOpQrStUvWxYz1")
        self.assertEqual(2, len(session.requests))
        self.assertEqual([1.0], clock.sleeps)

    def test_document_delete_retries_lost_response_then_converges(self):
        clock = FakeClock(0)
        session = FeishuSession(
            [requests.ConnectionError("lost"), MockResponse({"code": 1061007}, 404)]
        )
        client = FeishuClient(
            "app", "secret", "oc_test", "chat_id",
            session=session, clock=clock, sleep=clock.sleep,
        )
        client.delete_document("AbCdEfGhIjKlMnOpQrStUvWxYz1")
        self.assertEqual(2, len(session.requests))
        self.assertEqual([1], clock.sleeps)

    def test_document_delete_task_id_stays_pending(self):
        session = FeishuSession(
            [MockResponse({"code": 0, "data": {"task_id": "task-delete-1"}})]
        )
        client = FeishuClient(
            "app", "secret", "oc_test", "chat_id", session=session
        )
        with self.assertRaises(DeliveryError) as raised:
            client.delete_document("AbCdEfGhIjKlMnOpQrStUvWxYz1")
        self.assertEqual(
            "FEISHU_DELETE_DOCUMENT_ASYNC_PENDING", raised.exception.code
        )
        self.assertTrue(raised.exception.retryable)

    def test_document_delete_task_id_is_pending_not_false_success(self):
        session = FeishuSession(
            [MockResponse({"code": 0, "data": {"task_id": "opaque-task"}})]
        )
        client = FeishuClient("app", "secret", "oc_test", "chat_id", session=session)
        with self.assertRaises(DeliveryError) as caught:
            client.delete_document("AbCdEfGhIjKlMnOpQrStUvWxYz1")
        self.assertEqual(
            "FEISHU_DELETE_DOCUMENT_ASYNC_PENDING", caught.exception.code
        )
        self.assertTrue(caught.exception.retryable)

    def test_rate_limit_code_retries_with_backoff(self):
        clock = FakeClock(0)
        session = FeishuSession(
            [
                MockResponse({"code": 99991400, "msg": "rate limit"}, 400),
                MockResponse({"code": 0, "data": {}}),
            ]
        )
        client = FeishuClient(
            "app", "secret", "oc_test", "chat_id",
            session=session, clock=clock, sleep=clock.sleep,
        )
        client.share_document("doc")
        self.assertEqual(2, len(session.requests))
        self.assertGreaterEqual(clock.sleeps[0], 1)

    def test_message_uuid_is_in_json_body_not_query(self):
        session = FeishuSession()
        client = FeishuClient("app", "secret", "oc_test", "chat_id", session=session)
        client.send_card({"schema": "2.0", "body": {"elements": []}}, "message-uuid")
        request = session.requests[0][2]
        self.assertEqual({"receive_id_type": "chat_id"}, request["params"])
        self.assertEqual("message-uuid", request["json"]["uuid"])

    def test_create_500_is_unknown_and_never_retried(self):
        session = FeishuSession([MockResponse({"code": 1771001}, 500)])
        client = FeishuClient("app", "secret", "oc_test", "chat_id", session=session)
        with self.assertRaises(UncertainRemoteResult):
            client.create_document("title")
        self.assertEqual(1, len(session.requests))

    def test_share_network_result_is_unknown_and_never_retried(self):
        session = FeishuSession([requests.ConnectionError("lost")])
        client = FeishuClient("app", "secret", "oc_test", "chat_id", session=session)
        with self.assertRaises(UncertainRemoteResult):
            client.share_document("doc")
        self.assertEqual(1, len(session.requests))

    def test_share_500_is_unknown_and_never_retried(self):
        session = FeishuSession([MockResponse({"code": 1771001}, 500)])
        client = FeishuClient("app", "secret", "oc_test", "chat_id", session=session)
        with self.assertRaises(UncertainRemoteResult):
            client.share_document("doc")
        self.assertEqual(1, len(session.requests))

    def test_send_500_is_unknown_and_never_retried(self):
        session = FeishuSession([MockResponse({"code": 500, "msg": "server"}, 500)])
        client = FeishuClient("app", "secret", "oc_test", "chat_id", session=session)
        with self.assertRaises(UncertainRemoteResult):
            client.send_card({"schema": "2.0", "body": {"elements": []}}, "message-uuid")
        self.assertEqual(1, len(session.requests))

    def test_success_response_requires_explicit_code_zero(self):
        session = FeishuSession([MockResponse({}, 200)])
        client = FeishuClient("app", "secret", "oc_test", "chat_id", session=session)
        with self.assertRaises(DeliveryError):
            client.share_document("doc")

    def test_im_group_rate_limit_code_is_retryable(self):
        clock = FakeClock(0)
        session = FeishuSession(
            [MockResponse({"code": 230020, "msg": "limited"}, 400), MockResponse({"code": 0})]
        )
        client = FeishuClient(
            "app", "secret", "oc_test", "chat_id",
            session=session, clock=clock, sleep=clock.sleep,
        )
        client.send_card({"schema": "2.0"}, "uuid")
        self.assertEqual(2, len(session.requests))

    def test_only_global_feishu_4xx_are_classified_systemic(self):
        permission = FeishuClient(
            "app", "secret", "oc_test", "chat_id",
            session=FeishuSession([MockResponse({"code": 1770032}, 403)]),
        )
        with self.assertRaises(DeliveryError) as caught:
            permission.append_blocks(
                "doc", [{"block_type": 2, "text": {"elements": [], "style": {}}}],
                0, "token",
            )
        self.assertTrue(caught.exception.systemic)

        openchat = FeishuClient(
            "app", "secret", "oc_test", "chat_id",
            session=FeishuSession([MockResponse({"code": 1063001}, 400)]),
        )
        with self.assertRaises(DeliveryError) as caught:
            openchat.share_document("doc")
        self.assertTrue(caught.exception.systemic)

        article_content = FeishuClient(
            "app", "secret", "oc_test", "chat_id",
            session=FeishuSession([MockResponse({"code": 1770033}, 400)]),
        )
        with self.assertRaises(DeliveryError) as caught:
            article_content.append_blocks(
                "doc", [{"block_type": 2, "text": {"elements": [], "style": {}}}],
                0, "token",
            )
        self.assertFalse(caught.exception.systemic)

    def test_invalid_cached_token_is_refreshed_once(self):
        session = FeishuSession(
            [MockResponse({"code": 99991663}, 401), MockResponse({"code": 0})]
        )
        client = FeishuClient("app", "secret", "oc_test", "chat_id", session=session)
        client.share_document("doc")
        self.assertEqual(2, session.token_calls)
        self.assertEqual(2, len(session.requests))

    def test_card_partition_and_uuid_are_deterministic(self):
        rows = [
            {
                "article_key": f"key-{index}",
                "title": "标题" * 30,
                "document_url": f"https://tenant.feishu.cn/docx/doc{index}",
                "published_at": "2026-07-19",
            }
            for index in range(30)
        ]
        first = message_uuid(rows)
        self.assertEqual(first, message_uuid(list(reversed(rows))))
        parts = partition_summary_cards(rows, max_bytes=4096)
        self.assertGreater(len(parts), 1)
        for part in parts:
            size = len(json.dumps(build_summary_card(part), ensure_ascii=False, separators=(",", ":")).encode())
            self.assertLessEqual(size, 4096)


class DeliveryRunnerTests(unittest.TestCase):
    def test_missing_state_fails_closed_without_fetching_feed(self):
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Outbox(Path(tmp) / "delivery.db")
            feed = FakeFeed([feed_article()])
            runner = DeliveryRunner(
                config_for(Path(tmp) / "delivery.db"), outbox, feed, FakeBPC(), FakeFeishu()
            )
            with self.assertRaises(InitializationRequired):
                runner.run()
            self.assertEqual(0, feed.calls)
            outbox.close()

    def test_backfill_full_pipeline_is_deduplicated_and_db_is_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            outbox = Outbox(path)
            fake_feishu = FakeFeishu()
            item = feed_article()
            runner = DeliveryRunner(
                config_for(path), outbox, FakeFeed([item]), FakeBPC(), fake_feishu
            )
            summary = runner.run(backfill_current=True)
            self.assertEqual(1, summary.notified)
            self.assertEqual("notified", outbox.get(item.article_key)["status"])
            self.assertEqual(1, fake_feishu.create_calls)
            self.assertEqual(1, fake_feishu.share_calls)
            self.assertEqual(1, len(fake_feishu.card_calls))
            self.assertEqual(0o600, stat.S_IMODE(os.stat(path).st_mode))

            second = DeliveryRunner(
                config_for(path), outbox, FakeFeed([item]), FakeBPC(), fake_feishu
            ).run()
            self.assertEqual(0, second.discovered)
            self.assertEqual(1, fake_feishu.create_calls)
            self.assertEqual(1, len(fake_feishu.card_calls))
            outbox.close()

    def test_retention_deletes_only_oldest_notified_overflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            items = [feed_article(f"{index:08x}") for index in range(1, 4)]
            clock = FakeClock()
            outbox = Outbox(path)
            feishu = FakeFeishu()
            summary = DeliveryRunner(
                config_for(path, max_cloud_documents=2),
                outbox,
                FakeFeed(items),
                FakeBPC(),
                feishu,
                clock=clock,
            ).run(backfill_current=True, drain=True)
            self.assertEqual(3, summary.notified)
            self.assertEqual(1, summary.retention_deleted)
            self.assertEqual(0, summary.retention_excess)
            self.assertEqual(["docTokenModern0000000000001"], feishu.delete_calls)
            oldest = outbox.get(items[0].article_key)
            self.assertEqual("notified", oldest["status"])
            self.assertEqual("deleted", oldest["document_retention_state"])
            self.assertIsNotNone(oldest["document_deleted_at"])
            self.assertEqual(2, outbox.occupied_document_count())

            DeliveryRunner(
                config_for(path, max_cloud_documents=2),
                outbox,
                FakeFeed(items),
                FakeBPC(),
                feishu,
                clock=clock,
            ).run()
            self.assertEqual(3, feishu.create_calls)
            self.assertEqual(1, len(feishu.delete_calls))
            outbox.close()

    def test_retention_counts_but_never_deletes_manual_or_unknown_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            items = [feed_article(f"{index:08x}") for index in range(1, 5)]
            outbox = Outbox(path)
            outbox.initialize_with_items(items, 1000)
            statuses = ("manual", "unknown", "notified", "notified")
            with outbox._conn:
                for index, (item, status) in enumerate(zip(items, statuses), 1):
                    token = f"docTokenModern{index:013d}"
                    outbox._conn.execute(
                        """
                        UPDATE articles SET status=?, document_id=?, document_url=?,
                            document_create_started_at=?, notified_at=?
                        WHERE article_key=?
                        """,
                        (
                            status,
                            token,
                            f"https://tenant.feishu.cn/docx/{token}",
                            1000 + index,
                            1000 + index if status == "notified" else None,
                            item.article_key,
                        ),
                    )
            feishu = FakeFeishu()
            summary = DeliveryRunner(
                config_for(path, max_cloud_documents=2),
                outbox,
                FakeFeed(items),
                FakeBPC(),
                feishu,
                clock=FakeClock(2000),
            ).run()
            self.assertEqual(
                ["docTokenModern0000000000003", "docTokenModern0000000000004"],
                feishu.delete_calls,
            )
            self.assertEqual(2, summary.retention_deleted)
            self.assertEqual(0, summary.retention_excess)
            self.assertEqual("manual", outbox.get(items[0].article_key)["status"])
            self.assertEqual("active", outbox.get(items[0].article_key)["document_retention_state"])
            self.assertEqual("unknown", outbox.get(items[1].article_key)["status"])
            self.assertEqual("active", outbox.get(items[1].article_key)["document_retention_state"])
            outbox.close()

    def test_retention_intent_replays_after_crash_without_touching_article_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            items = [feed_article(f"{index:08x}") for index in range(1, 4)]
            outbox = Outbox(path)
            first_feishu = FakeFeishu()
            first_feishu.delete_error = KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt):
                DeliveryRunner(
                    config_for(path, max_cloud_documents=2),
                    outbox,
                    FakeFeed(items),
                    FakeBPC(),
                    first_feishu,
                    clock=FakeClock(),
                ).run(backfill_current=True, drain=True)
            row = outbox.get(items[0].article_key)
            self.assertEqual("notified", row["status"])
            self.assertEqual("delete_pending", row["document_retention_state"])
            token = row["document_id"]
            outbox.close()

            reopened = Outbox(path)
            second_feishu = FakeFeishu()
            summary = DeliveryRunner(
                config_for(path, max_cloud_documents=2),
                reopened,
                FakeFeed(items),
                FakeBPC(),
                second_feishu,
                clock=FakeClock(2000),
            ).run()
            self.assertEqual([token], second_feishu.delete_calls)
            self.assertEqual(1, summary.retention_deleted)
            self.assertEqual("notified", reopened.get(items[0].article_key)["status"])
            self.assertEqual(
                "deleted", reopened.get(items[0].article_key)["document_retention_state"]
            )
            reopened.close()

    def test_retention_failure_backs_off_and_never_skips_to_newer_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            items = [feed_article(f"{index:08x}") for index in range(1, 4)]
            clock = FakeClock()
            outbox = Outbox(path)
            feishu = FakeFeishu()
            feishu.delete_error = DeliveryError(
                "FEISHU_DELETE_DOCUMENT_RETRY_EXHAUSTED",
                "temporary",
                retryable=True,
            )
            first = DeliveryRunner(
                config_for(path, max_cloud_documents=2),
                outbox,
                FakeFeed(items),
                FakeBPC(),
                feishu,
                clock=clock,
            ).run(backfill_current=True, drain=True)
            self.assertEqual(1, len(feishu.delete_calls))
            self.assertEqual(1, first.retention_excess)
            oldest = outbox.get(items[0].article_key)
            self.assertEqual("delete_pending", oldest["document_retention_state"])
            self.assertEqual("notified", oldest["status"])

            DeliveryRunner(
                config_for(path, max_cloud_documents=2),
                outbox,
                FakeFeed(items),
                FakeBPC(),
                feishu,
                clock=clock,
            ).run()
            self.assertEqual(1, len(feishu.delete_calls))
            feishu.delete_error = None
            clock.advance(2)
            recovered = DeliveryRunner(
                config_for(path, max_cloud_documents=2),
                outbox,
                FakeFeed(items),
                FakeBPC(),
                feishu,
                clock=clock,
            ).run()
            self.assertEqual(2, len(feishu.delete_calls))
            self.assertEqual(feishu.delete_calls[0], feishu.delete_calls[1])
            self.assertEqual(1, recovered.retention_deleted)
            self.assertEqual(0, recovered.retention_excess)
            outbox.close()

    def test_existing_overflow_failure_blocks_new_document_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            items = [feed_article(f"{index:08x}") for index in range(1, 4)]
            outbox = Outbox(path)
            outbox.initialize_with_items(items, 1000)
            with outbox._conn:
                for index, item in enumerate(items[:2], 1):
                    token = f"docTokenModern{index:013d}"
                    outbox._conn.execute(
                        """
                        UPDATE articles SET status='notified', document_id=?,
                            document_url=?, document_create_started_at=?, notified_at=?
                        WHERE article_key=?
                        """,
                        (
                            token,
                            f"https://tenant.feishu.cn/docx/{token}",
                            1000 + index,
                            1000 + index,
                            item.article_key,
                        ),
                    )
            feishu = FakeFeishu()
            feishu.delete_error = DeliveryError(
                "FEISHU_DELETE_DOCUMENT_RETRY_EXHAUSTED",
                "temporary",
                retryable=True,
            )
            summary = DeliveryRunner(
                config_for(path, max_cloud_documents=1),
                outbox,
                FakeFeed(items),
                FakeBPC(),
                feishu,
                clock=FakeClock(2000),
            ).run()
            self.assertEqual(0, feishu.create_calls)
            self.assertEqual("fetched", outbox.get(items[2].article_key)["status"])
            self.assertTrue(summary.retention_capacity_blocked)
            self.assertEqual(1, summary.retention_excess)
            outbox.close()

    def test_nonretryable_delete_error_is_blocked_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            items = [feed_article(f"{index:08x}") for index in range(1, 3)]
            outbox = Outbox(path)
            outbox.initialize_with_items(items, 1000)
            with outbox._conn:
                for index, item in enumerate(items, 1):
                    token = f"docTokenModern{index:013d}"
                    outbox._conn.execute(
                        """
                        UPDATE articles SET status='notified', document_id=?,
                            document_url=?, document_create_started_at=?, notified_at=?
                        WHERE article_key=?
                        """,
                        (
                            token,
                            f"https://tenant.feishu.cn/docx/{token}",
                            1000 + index,
                            1000 + index,
                            item.article_key,
                        ),
                    )
            feishu = FakeFeishu()
            feishu.delete_error = DeliveryError(
                "FEISHU_DELETE_DOCUMENT_1061002",
                "bad params",
                retryable=False,
            )
            first = DeliveryRunner(
                config_for(path, max_cloud_documents=1),
                outbox,
                FakeFeed(items),
                FakeBPC(),
                feishu,
                clock=FakeClock(2000),
            ).run()
            self.assertEqual(1, len(feishu.delete_calls))
            self.assertEqual(
                "blocked", outbox.get(items[0].article_key)["document_retention_state"]
            )
            self.assertEqual(1, first.retention_excess)
            DeliveryRunner(
                config_for(path, max_cloud_documents=1),
                outbox,
                FakeFeed(items),
                FakeBPC(),
                feishu,
                clock=FakeClock(3000),
            ).run()
            self.assertEqual(1, len(feishu.delete_calls))
            outbox.close()

    def test_retention_rejects_non_feishu_document_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            items = [feed_article(f"{index:08x}") for index in range(1, 3)]
            outbox = Outbox(path)
            outbox.initialize_with_items(items, 1000)
            with outbox._conn:
                for index, item in enumerate(items, 1):
                    token = f"docTokenModern{index:013d}"
                    host = "evil.example" if index == 1 else "tenant.feishu.cn"
                    outbox._conn.execute(
                        """
                        UPDATE articles SET status='notified', document_id=?,
                            document_url=?, document_create_started_at=?, notified_at=?
                        WHERE article_key=?
                        """,
                        (
                            token,
                            f"https://{host}/docx/{token}",
                            1000 + index,
                            1000 + index,
                            item.article_key,
                        ),
                    )
            feishu = FakeFeishu()
            DeliveryRunner(
                config_for(path, max_cloud_documents=1),
                outbox,
                FakeFeed(items),
                FakeBPC(),
                feishu,
                clock=FakeClock(2000),
            ).run()
            self.assertEqual([], feishu.delete_calls)
            self.assertEqual(
                "blocked", outbox.get(items[0].article_key)["document_retention_state"]
            )
            outbox.close()

    def test_retention_does_not_resume_pending_when_no_longer_over_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            items = [feed_article(f"{index:08x}") for index in range(1, 3)]
            outbox = Outbox(path)
            outbox.initialize_with_items(items, 1000)
            with outbox._conn:
                for index, item in enumerate(items, 1):
                    token = f"docTokenModern{index:013d}"
                    outbox._conn.execute(
                        """
                        UPDATE articles SET status='notified', document_id=?,
                            document_url=?, document_create_started_at=?, notified_at=?
                        WHERE article_key=?
                        """,
                        (
                            token,
                            f"https://tenant.feishu.cn/docx/{token}",
                            1000 + index,
                            1000 + index,
                            item.article_key,
                        ),
                    )
            self.assertTrue(outbox.begin_document_delete(items[0].article_key, 2000))
            feishu = FakeFeishu()
            summary = DeliveryRunner(
                config_for(path, max_cloud_documents=2),
                outbox,
                FakeFeed(items),
                FakeBPC(),
                feishu,
                clock=FakeClock(2001),
            ).run()
            self.assertEqual([], feishu.delete_calls)
            self.assertEqual(0, summary.retention_excess)
            self.assertEqual(
                "delete_pending",
                outbox.get(items[0].article_key)["document_retention_state"],
            )
            outbox.close()

    def test_uncertain_create_without_token_reserves_one_retention_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            items = [feed_article(f"{index:08x}") for index in range(1, 4)]
            outbox = Outbox(path)
            outbox.initialize_with_items(items, 1000)
            with outbox._conn:
                for index, item in enumerate(items[:2], 1):
                    token = f"docTokenModern{index:013d}"
                    outbox._conn.execute(
                        """
                        UPDATE articles SET status='notified', document_id=?,
                            document_url=?, document_create_started_at=?, notified_at=?
                        WHERE article_key=?
                        """,
                        (
                            token,
                            f"https://tenant.feishu.cn/docx/{token}",
                            1000 + index,
                            1000 + index,
                            item.article_key,
                        ),
                    )
                outbox._conn.execute(
                    """
                    UPDATE articles SET status='unknown',
                        document_create_started_at=1003
                    WHERE article_key=?
                    """,
                    (items[2].article_key,),
                )
            self.assertEqual(3, outbox.occupied_document_count())
            feishu = FakeFeishu()
            summary = DeliveryRunner(
                config_for(path, max_cloud_documents=2),
                outbox,
                FakeFeed(items),
                FakeBPC(),
                feishu,
                clock=FakeClock(2000),
            ).run()
            self.assertEqual(["docTokenModern0000000000001"], feishu.delete_calls)
            self.assertEqual(1, summary.retention_deleted)
            self.assertEqual(0, summary.retention_excess)
            self.assertEqual("unknown", outbox.get(items[2].article_key)["status"])
            outbox.close()

    def test_enabled_images_preserve_order_and_complete_three_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            item = feed_article()
            outbox = Outbox(path)
            feishu = FakeFeishu()
            images = FakeImageDownloader()
            summary = DeliveryRunner(
                config_for(path, include_images=True),
                outbox,
                FakeFeed([item]),
                FakeBPC(result=fetched_article_with_image()),
                feishu,
                image_downloader=images,
            ).run(backfill_current=True)
            self.assertEqual(1, summary.notified)
            image_call_index = next(
                index
                for index, call in enumerate(feishu.append_calls)
                if call[1][0].get("block_type") == 27
            )
            self.assertGreater(image_call_index, 0)
            self.assertLess(image_call_index, len(feishu.append_calls) - 1)
            self.assertEqual(1, len(feishu.upload_calls))
            self.assertEqual(1, len(feishu.replace_calls))
            self.assertEqual((640, 360), feishu.replace_calls[0][4:6])
            self.assertEqual("notified", outbox.get(item.article_key)["status"])
            # Bound images are removed from the temporary SQLite spool.
            self.assertIsNone(outbox.get_prepared_image(item.article_key, 6))
            outbox.close()

    def test_nonretryable_bad_image_is_skipped_before_empty_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            item = feed_article()
            outbox = Outbox(path)
            feishu = FakeFeishu()
            images = FakeImageDownloader(
                DeliveryError("IMAGE_TYPE_INVALID", "bad image", retryable=False)
            )
            summary = DeliveryRunner(
                config_for(path, include_images=True),
                outbox,
                FakeFeed([item]),
                FakeBPC(result=fetched_article_with_image()),
                feishu,
                image_downloader=images,
            ).run(backfill_current=True)
            self.assertEqual(1, summary.notified)
            self.assertFalse(
                any(call[1][0].get("block_type") == 27 for call in feishu.append_calls)
            )
            self.assertEqual([], feishu.upload_calls)
            rendered_text = json.dumps(feishu.append_calls, ensure_ascii=False)
            self.assertNotIn("图片说明：图片说明", rendered_text)
            states = json.loads(outbox.get(item.article_key)["image_states_json"])
            self.assertIn("skipped", {value["state"] for value in states.values()})
            outbox.close()

    def test_retryable_image_download_does_not_share_incomplete_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            item = feed_article()
            outbox = Outbox(path)
            feishu = FakeFeishu()
            summary = DeliveryRunner(
                config_for(path, include_images=True),
                outbox,
                FakeFeed([item]),
                FakeBPC(result=fetched_article_with_image()),
                feishu,
                image_downloader=FakeImageDownloader(
                    DeliveryError("IMAGE_DOWNLOAD_TIMEOUT", "timeout", retryable=True)
                ),
            ).run(backfill_current=True)
            self.assertEqual(0, summary.notified)
            self.assertEqual("doc_created", outbox.get(item.article_key)["status"])
            self.assertEqual(0, feishu.share_calls)
            self.assertFalse(
                any(call[1][0].get("block_type") == 27 for call in feishu.append_calls)
            )
            outbox.close()

    def test_render_plan_is_frozen_across_feature_flag_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            item = feed_article()
            clock = FakeClock()
            outbox = Outbox(path)
            first_feishu = FakeFeishu()
            first_feishu.append_error = DeliveryError(
                "FEISHU_APPEND_TEMP", "temporary", retryable=True
            )
            DeliveryRunner(
                config_for(path, include_images=True),
                outbox,
                FakeFeed([item]),
                FakeBPC(result=fetched_article_with_image()),
                first_feishu,
                image_downloader=FakeImageDownloader(),
                clock=clock,
            ).run(backfill_current=True)
            frozen = outbox.get(item.article_key)["render_plan_json"]
            self.assertIn('"kind":"image"', frozen)
            clock.advance(2)
            second_feishu = FakeFeishu()
            DeliveryRunner(
                config_for(path, include_images=False),
                outbox,
                FakeFeed([item]),
                FakeBPC(result=fetched_article_with_image()),
                second_feishu,
                image_downloader=FakeImageDownloader(),
                clock=clock,
            ).run()
            self.assertEqual(1, len(second_feishu.upload_calls))
            self.assertEqual(frozen, outbox.get(item.article_key)["render_plan_json"])
            outbox.close()

    def test_already_notified_row_is_not_rewritten_when_images_are_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            item = feed_article()
            outbox = Outbox(path)
            DeliveryRunner(
                config_for(path, include_images=False),
                outbox,
                FakeFeed([item]),
                FakeBPC(result=fetched_article_with_image()),
                FakeFeishu(),
                image_downloader=FakeImageDownloader(),
            ).run(backfill_current=True)
            before = outbox.get(item.article_key)
            guard = FakeImageDownloader(
                AssertionError("notified rows must never download images")
            )
            DeliveryRunner(
                config_for(path, include_images=True),
                outbox,
                FakeFeed([item]),
                FakeBPC(result=fetched_article_with_image()),
                FakeFeishu(),
                image_downloader=guard,
            ).run()
            self.assertEqual([], guard.calls)
            self.assertEqual(before, outbox.get(item.article_key))
            outbox.close()

    def test_legacy_doc_created_without_plan_remains_text_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            item = feed_article()
            outbox = Outbox(path)
            first_feishu = FakeFeishu()
            first_feishu.share_failures = 1
            DeliveryRunner(
                config_for(path, include_images=False),
                outbox,
                FakeFeed([item]),
                FakeBPC(result=fetched_article_with_image()),
                first_feishu,
                image_downloader=FakeImageDownloader(),
            ).run(backfill_current=True)
            self.assertEqual("doc_created", outbox.get(item.article_key)["status"])
            with outbox._conn:
                outbox._conn.execute(
                    "UPDATE articles SET render_plan_json='', next_attempt_at=0 WHERE article_key=?",
                    (item.article_key,),
                )
            guard = FakeImageDownloader(
                AssertionError("legacy doc_created must stay text-only")
            )
            DeliveryRunner(
                config_for(path, include_images=True),
                outbox,
                FakeFeed([item]),
                FakeBPC(result=fetched_article_with_image()),
                FakeFeishu(),
                image_downloader=guard,
            ).run()
            self.assertEqual([], guard.calls)
            self.assertEqual("notified", outbox.get(item.article_key)["status"])
            self.assertNotIn(
                '"kind":"image"', outbox.get(item.article_key)["render_plan_json"]
            )
            outbox.close()

    def test_image_upload_write_ahead_closes_crash_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            item = feed_article()
            outbox = Outbox(path)
            feishu = FakeFeishu()
            feishu.upload_error = KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt):
                DeliveryRunner(
                    config_for(path, include_images=True),
                    outbox,
                    FakeFeed([item]),
                    FakeBPC(result=fetched_article_with_image()),
                    feishu,
                    image_downloader=FakeImageDownloader(),
                ).run(backfill_current=True)
            row = outbox.get(item.article_key)
            states = json.loads(row["image_states_json"])
            image_state = next(value for value in states.values() if value["state"] == "block_created")
            self.assertIsNotNone(image_state["upload_started_at"])
            self.assertIsNotNone(outbox.get_prepared_image(item.article_key, row["block_cursor"]))
            outbox.close()

            reopened = Outbox(path)
            next_feishu = FakeFeishu()
            DeliveryRunner(
                config_for(path, include_images=True),
                reopened,
                FakeFeed([item]),
                FakeBPC(result=fetched_article_with_image()),
                next_feishu,
                image_downloader=FakeImageDownloader(),
            ).run()
            self.assertEqual("unknown", reopened.get(item.article_key)["status"])
            self.assertEqual(0, next_feishu.create_calls)
            self.assertEqual([], next_feishu.upload_calls)
            reopened.close()

    def test_image_patch_restart_reuses_block_upload_and_client_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            item = feed_article()
            outbox = Outbox(path)
            feishu = FakeFeishu()
            feishu.replace_error = KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt):
                DeliveryRunner(
                    config_for(path, include_images=True),
                    outbox,
                    FakeFeed([item]),
                    FakeBPC(result=fetched_article_with_image()),
                    feishu,
                    image_downloader=FakeImageDownloader(),
                ).run(backfill_current=True)
            self.assertEqual(1, len(feishu.upload_calls))
            first_token = feishu.replace_calls[0][3]
            feishu.replace_error = None
            DeliveryRunner(
                config_for(path, include_images=False),
                outbox,
                FakeFeed([item]),
                FakeBPC(result=fetched_article_with_image()),
                feishu,
                image_downloader=FakeImageDownloader(),
            ).run()
            self.assertEqual("notified", outbox.get(item.article_key)["status"])
            self.assertEqual(1, len(feishu.upload_calls))
            self.assertEqual(1, sum(
                1 for call in feishu.append_calls if call[1][0].get("block_type") == 27
            ))
            self.assertEqual(first_token, feishu.replace_calls[1][3])
            outbox.close()

    def test_all_bpc_failures_never_create_document(self):
        errors = [
            DeliveryError("FORBIDDEN", "forbidden", retryable=False, systemic=True),
            DeliveryError("DATADOME_CHALLENGE", "challenge", retryable=True, systemic=True),
            DeliveryError("BPC_QUALITY_GATE", "short", retryable=False),
            DeliveryError("BPC_TIMEOUT", "timeout", retryable=True),
            DeliveryError("QUEUE_FULL", "queue", retryable=True),
            DeliveryError("BROWSER_CRASH", "browser", retryable=True),
        ]
        for index, error in enumerate(errors):
            with self.subTest(error=error.code), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "delivery.db"
                outbox = Outbox(path)
                fake_feishu = FakeFeishu()
                DeliveryRunner(
                    config_for(path), outbox, FakeFeed([feed_article(f"abcde{index:07x}")]),
                    FakeBPC(error=error), fake_feishu,
                ).run(backfill_current=True)
                self.assertEqual(0, fake_feishu.create_calls)
                outbox.close()

    def test_drain_stops_without_busy_loop_when_circuit_blocks_remaining_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            clock = FakeClock()
            items = [feed_article(f"abcdef1234{index:02x}") for index in range(3)]
            error = DeliveryError(
                "DATADOME_CHALLENGE", "challenge", retryable=True, systemic=True
            )
            bpc = FakeBPC(error=error)
            outbox = Outbox(path)
            summary = DeliveryRunner(
                config_for(path), outbox, FakeFeed(items), bpc, FakeFeishu(), clock=clock
            ).run(backfill_current=True, drain=True)
            self.assertEqual(1, bpc.calls)
            self.assertFalse(summary.drain_deadline_reached)
            self.assertEqual(1000.0, clock.value)
            outbox.close()

    def test_session_persistence_failure_stops_batch_before_more_fetches(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            items = [feed_article(f"1234567890{index:02x}") for index in range(3)]
            error = DeliveryError(
                "SESSION_PERSIST_FAILED",
                "BPC session persistence failed",
                retryable=True,
                systemic=True,
            )
            bpc = FakeBPC(error=error)
            feishu = FakeFeishu()
            outbox = Outbox(path)
            DeliveryRunner(
                config_for(path), outbox, FakeFeed(items), bpc, feishu
            ).run(backfill_current=True, drain=True)
            self.assertEqual(1, bpc.calls)
            self.assertEqual(0, feishu.create_calls)
            self.assertEqual(1, len(feishu.alert_calls))
            outbox.close()

    def test_drain_backfill_over_20_items_sends_one_summary_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            items = [feed_article(f"{index + 1:08x}") for index in range(44)]
            feishu = FakeFeishu()
            outbox = Outbox(path)
            summary = DeliveryRunner(
                config_for(path), outbox, FakeFeed(items), FakeBPC(), feishu
            ).run(backfill_current=True, drain=True)
            self.assertEqual(44, summary.notified)
            self.assertEqual(44, feishu.create_calls)
            self.assertEqual(1, len(feishu.card_calls))
            outbox.close()

    def test_feishu_permission_or_openchat_failure_halts_current_drain(self):
        for stage in ("blocks", "share"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "delivery.db"
                items = [feed_article(f"abcdef1234{index:02x}") for index in range(3)]
                feishu = FakeFeishu()
                error = DeliveryError(
                    f"FEISHU_{stage.upper()}_PERMISSION",
                    "global permission/configuration failure",
                    retryable=False,
                    systemic=True,
                )
                if stage == "blocks":
                    feishu.append_error = error
                else:
                    feishu.share_error = error
                bpc = FakeBPC()
                outbox = Outbox(path)

                DeliveryRunner(
                    config_for(path), outbox, FakeFeed(items), bpc, feishu
                ).run(backfill_current=True, drain=True)

                self.assertEqual(1, bpc.calls)
                self.assertEqual(1, feishu.create_calls)
                self.assertEqual("doc_created", outbox.get(items[0].article_key)["status"])
                self.assertEqual(
                    ["discovered", "discovered"],
                    [outbox.get(item.article_key)["status"] for item in items[1:]],
                )
                outbox.close()

    def test_article_specific_block_4xx_does_not_halt_following_articles(self):
        class FailFirstArticleOnly(FakeFeishu):
            def append_blocks(self, document_id, blocks, index, client_token):
                super().append_blocks(document_id, blocks, index, client_token)
                if len(self.append_calls) == 1:
                    raise DeliveryError(
                        "FEISHU_APPEND_BLOCKS_1770033",
                        "article content too large",
                        retryable=False,
                        systemic=False,
                    )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            items = [feed_article("abcdef123401"), feed_article("abcdef123402")]
            feishu = FailFirstArticleOnly()
            outbox = Outbox(path)
            DeliveryRunner(
                config_for(path), outbox, FakeFeed(items), FakeBPC(), feishu
            ).run(backfill_current=True, drain=True)
            self.assertEqual(2, feishu.create_calls)
            self.assertEqual("manual", outbox.get(items[0].article_key)["status"])
            self.assertEqual("notified", outbox.get(items[1].article_key)["status"])
            outbox.close()

    def test_doc_created_resume_does_not_create_duplicate_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            clock = FakeClock()
            item = feed_article()
            feishu = FakeFeishu()
            feishu.share_failures = 1
            outbox = Outbox(path)
            DeliveryRunner(
                config_for(path), outbox, FakeFeed([item]), FakeBPC(), feishu, clock=clock
            ).run(backfill_current=True)
            self.assertEqual("doc_created", outbox.get(item.article_key)["status"])
            self.assertEqual(1, feishu.create_calls)
            outbox.close()

            clock.advance(2)
            reopened = Outbox(path)
            DeliveryRunner(
                config_for(path), reopened, FakeFeed([item]), FakeBPC(), feishu, clock=clock
            ).run()
            self.assertEqual("notified", reopened.get(item.article_key)["status"])
            self.assertEqual(1, feishu.create_calls)
            # Blocks were committed before the failed share and are not repeated.
            self.assertEqual(1, len(feishu.append_calls))
            reopened.close()

    def test_feed_outage_does_not_block_doc_created_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            clock = FakeClock()
            item = feed_article()
            feishu = FakeFeishu()
            feishu.share_failures = 1
            outbox = Outbox(path)
            DeliveryRunner(
                config_for(path), outbox, FakeFeed([item]), FakeBPC(), feishu, clock=clock
            ).run(backfill_current=True)
            self.assertEqual("doc_created", outbox.get(item.article_key)["status"])
            clock.advance(2)
            DeliveryRunner(
                config_for(path),
                outbox,
                FakeFeed([], error=DeliveryError("FEED_DOWN", "down", retryable=True)),
                FakeBPC(),
                feishu,
                clock=clock,
            ).run()
            self.assertEqual("notified", outbox.get(item.article_key)["status"])
            self.assertEqual(1, feishu.create_calls)
            outbox.close()

    def test_message_retry_reuses_uuid_without_recreating_doc(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            clock = FakeClock()
            item = feed_article()
            feishu = FakeFeishu()
            feishu.card_failures = 1
            outbox = Outbox(path)
            DeliveryRunner(
                config_for(path), outbox, FakeFeed([item]), FakeBPC(), feishu, clock=clock
            ).run(backfill_current=True)
            first_uuid = feishu.card_calls[0][1]
            self.assertEqual("shared", outbox.get(item.article_key)["status"])
            clock.advance(2)
            DeliveryRunner(
                config_for(path), outbox, FakeFeed([item]), FakeBPC(), feishu, clock=clock
            ).run()
            self.assertEqual(first_uuid, feishu.card_calls[1][1])
            self.assertEqual(1, feishu.create_calls)
            self.assertEqual("notified", outbox.get(item.article_key)["status"])
            outbox.close()

    def test_feed_outage_does_not_block_shared_message_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            clock = FakeClock()
            item = feed_article()
            feishu = FakeFeishu()
            feishu.card_failures = 1
            outbox = Outbox(path)
            DeliveryRunner(
                config_for(path), outbox, FakeFeed([item]), FakeBPC(), feishu, clock=clock
            ).run(backfill_current=True)
            self.assertEqual("shared", outbox.get(item.article_key)["status"])
            clock.advance(2)
            DeliveryRunner(
                config_for(path),
                outbox,
                FakeFeed([], error=DeliveryError("FEED_DOWN", "down", retryable=True)),
                FakeBPC(),
                feishu,
                clock=clock,
            ).run()
            self.assertEqual("notified", outbox.get(item.article_key)["status"])
            self.assertEqual(2, len(feishu.card_calls))
            outbox.close()

    def test_message_retry_group_commit_survives_crash_without_uuid_subset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            clock = FakeClock()
            items = [feed_article("ecb497c1"), feed_article("807814de")]
            feishu = FakeFeishu()
            feishu.card_error = DeliveryError(
                "FEISHU_SEND_TEMP", "temporary", retryable=True
            )
            outbox = Outbox(path)
            original = outbox.schedule_message_retry

            def commit_then_crash(*args, **kwargs):
                original(*args, **kwargs)
                raise KeyboardInterrupt()

            outbox.schedule_message_retry = commit_then_crash
            with self.assertRaises(KeyboardInterrupt):
                DeliveryRunner(
                    config_for(path),
                    outbox,
                    FakeFeed(items),
                    FakeBPC(),
                    feishu,
                    clock=clock,
                ).run(backfill_current=True)
            rows = [outbox.get(item.article_key) for item in items]
            self.assertEqual(1, len({row["message_uuid"] for row in rows}))
            self.assertEqual(1, len({row["next_attempt_at"] for row in rows}))
            self.assertTrue(all(row["status"] == "shared" for row in rows))
            self.assertTrue(all(row["notification_started_at"] is None for row in rows))
            with outbox._conn:
                outbox._conn.execute(
                    "UPDATE articles SET next_attempt_at=? WHERE article_key=?",
                    (clock.value + 2, items[1].article_key),
                )
            self.assertEqual([], outbox.pending_shared(clock.value + 1.5))
            self.assertEqual(2, len(outbox.pending_shared(clock.value + 2)))
            outbox.close()

            clock.advance(2)
            reopened = Outbox(path)
            feishu.card_error = None
            DeliveryRunner(
                config_for(path),
                reopened,
                FakeFeed(items),
                FakeBPC(),
                feishu,
                clock=clock,
            ).run()
            self.assertEqual(2, len(feishu.card_calls))
            self.assertEqual(
                ["notified", "notified"],
                [reopened.get(item.article_key)["status"] for item in items],
            )
            second_card = feishu.card_calls[1][0]
            self.assertEqual("WSJ 新文章（2 篇）", second_card["header"]["title"]["content"])
            reopened.close()

    def test_document_create_write_ahead_marker_closes_kill_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            item = feed_article()
            feishu = FakeFeishu()
            feishu.create_error = KeyboardInterrupt()
            outbox = Outbox(path)
            with self.assertRaises(KeyboardInterrupt):
                DeliveryRunner(
                    config_for(path), outbox, FakeFeed([item]), FakeBPC(), feishu
                ).run(backfill_current=True)
            self.assertIsNotNone(outbox.get(item.article_key)["document_create_started_at"])
            outbox.close()

            reopened = Outbox(path)
            feishu.create_error = None
            DeliveryRunner(
                config_for(path), reopened, FakeFeed([item]), FakeBPC(), feishu
            ).run()
            self.assertEqual("unknown", reopened.get(item.article_key)["status"])
            self.assertEqual(1, feishu.create_calls)
            reopened.close()

    def test_message_kill_window_marks_whole_group_unknown_without_resend(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            items = [feed_article("ecb497c1"), feed_article("807814de")]
            feishu = FakeFeishu()
            feishu.card_error = KeyboardInterrupt()
            outbox = Outbox(path)
            with self.assertRaises(KeyboardInterrupt):
                DeliveryRunner(
                    config_for(path), outbox, FakeFeed(items), FakeBPC(), feishu
                ).run(backfill_current=True)
            self.assertTrue(all(outbox.get(item.article_key)["notification_started_at"] for item in items))
            outbox.close()

            reopened = Outbox(path)
            feishu.card_error = None
            DeliveryRunner(
                config_for(path), reopened, FakeFeed(items), FakeBPC(), feishu
            ).run()
            self.assertEqual(["unknown", "unknown"], [reopened.get(item.article_key)["status"] for item in items])
            self.assertEqual(1, len(feishu.card_calls))
            reopened.close()

    def test_uncertain_message_response_marks_group_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            items = [feed_article("ecb497c1"), feed_article("807814de")]
            feishu = FakeFeishu()
            feishu.card_error = UncertainRemoteResult("SEND_UNKNOWN", "unknown")
            outbox = Outbox(path)
            DeliveryRunner(
                config_for(path), outbox, FakeFeed(items), FakeBPC(), feishu
            ).run(backfill_current=True)
            self.assertEqual(["unknown", "unknown"], [outbox.get(item.article_key)["status"] for item in items])
            DeliveryRunner(
                config_for(path), outbox, FakeFeed(items), FakeBPC(), feishu
            ).run()
            self.assertEqual(1, len(feishu.card_calls))
            outbox.close()

    def test_uncertain_document_create_enters_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            item = feed_article()
            feishu = FakeFeishu()
            feishu.create_error = UncertainRemoteResult("CREATE_UNKNOWN", "unknown")
            outbox = Outbox(path)
            DeliveryRunner(
                config_for(path), outbox, FakeFeed([item]), FakeBPC(), feishu
            ).run(backfill_current=True)
            self.assertEqual("unknown", outbox.get(item.article_key)["status"])
            self.assertEqual(0, feishu.share_calls)
            outbox.close()

    def test_large_article_is_written_in_batches_of_at_most_50(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            feishu = FakeFeishu()
            outbox = Outbox(path)
            DeliveryRunner(
                config_for(path), outbox, FakeFeed([feed_article()]),
                FakeBPC(result=fetched_article(paragraph_count=121)), feishu,
            ).run(backfill_current=True)
            sizes = [len(call[1]) for call in feishu.append_calls]
            self.assertGreater(len(sizes), 2)
            self.assertTrue(all(1 <= size <= 50 for size in sizes))
            self.assertEqual(list(range(0, sum(sizes), 50)), [call[2] for call in feishu.append_calls])
            outbox.close()

    def test_alert_cooldown_and_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            clock = FakeClock()
            item = feed_article()
            feishu = FakeFeishu()
            error = DeliveryError(
                "DATADOME_CHALLENGE", "challenge", retryable=True, systemic=True
            )
            outbox = Outbox(path)
            DeliveryRunner(
                config_for(path), outbox, FakeFeed([item]), FakeBPC(error=error), feishu,
                clock=clock,
            ).run(backfill_current=True)
            self.assertEqual(1, len(feishu.alert_calls))

            clock.advance(11)
            DeliveryRunner(
                config_for(path), outbox, FakeFeed([item]), FakeBPC(error=error), feishu,
                clock=clock,
            ).run()
            self.assertEqual(1, len(feishu.alert_calls), "same alert must be limited to 6 hours")

            clock.advance(21601)
            DeliveryRunner(
                config_for(path), outbox, FakeFeed([item]), FakeBPC(error=error), feishu,
                clock=clock,
            ).run()
            self.assertEqual(2, len(feishu.alert_calls))

            clock.advance(11)
            DeliveryRunner(
                config_for(path), outbox, FakeFeed([item]), FakeBPC(), feishu,
                clock=clock,
            ).run()
            self.assertEqual("WSJ 全文抓取已恢复", feishu.alert_calls[-1][0])
            self.assertEqual("notified", outbox.get(item.article_key)["status"])
            outbox.close()


if __name__ == "__main__":
    unittest.main()
