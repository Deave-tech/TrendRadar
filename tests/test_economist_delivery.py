# coding=utf-8

import json
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from trendradar.wsj_delivery.clients import (
    BPCClient,
    FeedClient,
    build_document_plan,
    build_summary_card,
    normalize_article_image_url,
)
from trendradar.wsj_delivery.models import (
    DeliveryConfig,
    DeliveryError,
    FeedArticle,
    FetchedArticle,
    make_article_key,
    normalize_economist_url,
)
from trendradar.wsj_delivery.outbox import Outbox
from trendradar.wsj_delivery.service import DeliveryRunner


class Response:
    def __init__(self, payload=None, status_code=200, *, content=b"", headers=None):
        self.payload = payload or {}
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

    def get(self, *args, **kwargs):
        return self.response


class PostSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class EconomistURLTests(unittest.TestCase):
    def test_normalizes_dated_articles_and_drops_tracking(self):
        cases = (
            "https://www.economist.com/business/2026/07/19/story?utm_source=x#top",
            "https://www.economist.com/1843/2026/07/09/story/",
            "https://www.economist.com/graphic-detail/2026/07/16/story",
        )
        for value in cases:
            with self.subTest(value=value):
                normalized = normalize_economist_url(value)
                self.assertTrue(normalized.startswith("https://www.economist.com/"))
                self.assertNotIn("?", normalized)
                self.assertNotIn("#", normalized)
                self.assertFalse(normalized.endswith("/"))

    def test_rejects_non_articles_and_deceptive_hosts(self):
        cases = (
            "http://www.economist.com/business/2026/07/19/story",
            "https://economist.com/business/2026/07/19/story",
            "https://www.economist.com.evil.test/business/2026/07/19/story",
            "https://user@www.economist.com/business/2026/07/19/story",
            "https://www.economist.com/podcasts/2026/07/19/story",
            "https://www.economist.com/interactive/1843/2026/07/19/story",
            "https://www.economist.com/video/2026/07/19/story",
            "https://www.economist.com/business/story",
            "https://www.economist.com/business/2026/02/30/not-a-real-date",
        )
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_economist_url(value)

    def test_guid_is_preferred_then_url_hash_is_stable(self):
        url = normalize_economist_url(
            "https://www.economist.com/business/2026/07/19/story"
        )
        guid = "d1557b9e-2871-4c24-8f24-c10ee13012b9"
        self.assertEqual(
            (f"economist:{guid}", guid),
            make_article_key(url, "economist", guid.upper()),
        )
        first = make_article_key(url, "economist")
        self.assertEqual(
            first,
            make_article_key(
                normalize_economist_url(url + "?ignored=1"), "economist"
            ),
        )
        self.assertTrue(first[0].startswith("economist-url:"))


class EconomistImageURLTests(unittest.TestCase):
    def test_cloudflare_format_matches_source_extension(self):
        cases = {
            "https://www.economist.com/cdn-cgi/image/width=1424,quality=80,format=auto/content-assets/images/photo.jpg":
                "https://www.economist.com/cdn-cgi/image/width=1424,quality=80,format=jpg/content-assets/images/photo.jpg",
            "https://www.economist.com/cdn-cgi/image/format=webp,width=1424/content-assets/images/photo.jpeg?tracking=1#x":
                "https://www.economist.com/cdn-cgi/image/width=1424,format=jpg/content-assets/images/photo.jpeg",
            "https://www.economist.com/cdn-cgi/image/width=1424,quality=100/content-assets/images/chart.png":
                "https://www.economist.com/cdn-cgi/image/width=1424,quality=100,format=png/content-assets/images/chart.png",
            "https://www.economist.com/content-assets/images/animation.gif#x":
                "https://www.economist.com/content-assets/images/animation.gif",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(
                    expected,
                    normalize_article_image_url(source, ("www.economist.com",)),
                )

    def test_rejects_non_editorial_and_unsupported_economist_images(self):
        for source in (
            "https://www.economist.com/assets/images/photo.jpg",
            "https://www.economist.com/content-assets/images/photo.avif",
            "https://www.economist.com/content-assets/images/vector.svg",
        ):
            with self.subTest(source=source), self.assertRaises(DeliveryError) as caught:
                normalize_article_image_url(source, ("www.economist.com",))
            self.assertEqual("IMAGE_URL_NOT_ALLOWED", caught.exception.code)

class EconomistClientTests(unittest.TestCase):
    def test_challenge_and_browser_failure_stop_the_publisher_batch(self):
        url = "https://www.economist.com/business/2026/07/19/story"
        for status, code in ((422, "CHALLENGE_DETECTED"), (503, "SERVICE_NOT_READY")):
            with self.subTest(code=code):
                client = BPCClient(
                    "http://127.0.0.1:8080",
                    "token",
                    2,
                    session=PostSession(
                        Response(
                            {"ok": False, "code": code, "retryable": True},
                            status,
                        )
                    ),
                    publisher="economist",
                    endpoint="/v1/economist/fetch",
                )
                with self.assertRaises(DeliveryError) as caught:
                    client.fetch(url, "request-id")
                self.assertTrue(caught.exception.systemic)

    def test_official_feed_guid_dedup_and_media_filter(self):
        xml = b"""<?xml version='1.0'?><rss version='2.0'><channel>
        <item><guid isPermaLink='false'>d1557b9e-2871-4c24-8f24-c10ee13012b9</guid>
          <title>One</title><link>https://www.economist.com/business/2026/07/19/story?utm_source=x</link></item>
        <item><guid isPermaLink='false'>d1557b9e-2871-4c24-8f24-c10ee13012b9</guid>
          <title>Duplicate</title><link>https://www.economist.com/business/2026/07/19/story</link></item>
        <item><title>Podcast</title><link>https://www.economist.com/podcasts/2026/07/19/show</link></item>
        </channel></rss>"""
        client = FeedClient(
            "http://127.0.0.1:4556/economist.xml",
            2,
            session=FeedSession(
                Response(content=xml, headers={"Content-Type": "application/rss+xml"})
            ),
            publisher="economist",
            display_name="Economist",
        )
        items = client.fetch()
        self.assertEqual(1, len(items))
        self.assertEqual(
            "economist:d1557b9e-2871-4c24-8f24-c10ee13012b9",
            items[0].article_key,
        )
        self.assertEqual("economist", items[0].publisher)

    def test_bpc_uses_dedicated_endpoint_and_accepts_only_article_images(self):
        url = "https://www.economist.com/business/2026/07/19/story"
        paragraphs = ["English article text " * 20 for _ in range(4)]
        payload = {
            "ok": True,
            "article": {
                "status": 200,
                "canonicalUrl": url + "?utm_source=feed",
                "title": "A full Economist story",
                "paragraphs": paragraphs,
                "images": [
                    {
                        "url": "https://www.economist.com/cdn-cgi/image/width=1424,quality=80/content-assets/images/20260725_BLP501.jpg",
                        "afterParagraph": -1,
                        "caption": "illustration: artist",
                    },
                    {
                        "url": "https://www.economist.com/cdn-cgi/image/width=640/content-assets/images/20260725_BLP501.jpg",
                        "afterParagraph": 0,
                    },
                    {
                        "url": "https://www.economist.com/cdn-cgi/image/width=1424/content-assets/images/20260725_FNC001.png",
                        "afterParagraph": 1,
                        "caption": "chart: the economist",
                    },
                    {
                        "url": "https://www.economist.com/assets/recommendation.jpg",
                        "afterParagraph": 1,
                    },
                    {
                        "url": "https://www.economist.com/content-assets/images/footer.jpg",
                        "afterParagraph": 3,
                    },
                ],
            },
        }
        session = PostSession(Response(payload))
        article = BPCClient(
            "http://127.0.0.1:8080",
            "token",
            2,
            session=session,
            publisher="economist",
            endpoint="/v1/economist/fetch",
        ).fetch(url, "request-id")
        self.assertTrue(session.calls[0][0].endswith("/v1/economist/fetch"))
        self.assertEqual(url, article.canonical_url)
        self.assertEqual(
            ["image", "paragraph", "paragraph", "image", "paragraph", "paragraph"],
            [item["type"] for item in article.body_items],
        )
        images = [item for item in article.body_items if item["type"] == "image"]
        self.assertEqual(2, len(images))
        self.assertEqual("chart: the economist", images[1]["caption"])

    def test_profile_builds_economist_document_and_card(self):
        row = {
            "article_key": "economist:test",
            "title": "Story",
            "author": "The Economist",
            "published_at": "2026-07-19",
            "fetched_at": "2026-07-20T00:00:00Z",
            "paragraphs_json": json.dumps(["body"]),
            "document_url": "https://tenant.feishu.cn/docx/token",
        }
        plan = build_document_plan(
            row,
            "https://www.economist.com/business/2026/07/19/story",
            source_name="The Economist",
        )
        first = plan[0]["block"]["text"]["elements"][0]["text_run"]["content"]
        self.assertEqual("来源：The Economist", first)
        card = build_summary_card([row], "Economist")
        self.assertEqual("Economist 新文章（1 篇）", card["header"]["title"]["content"])

    def test_economist_env_reuses_shared_db_and_global_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            shared = str(Path(tmp) / "news.db")
            env = {
                "BPC_API_TOKEN": "bpc",
                "FEISHU_APP_ID": "app",
                "FEISHU_APP_SECRET": "secret",
                "FEISHU_RECEIVE_ID": "oc_test",
                "FEISHU_DOC_URL_PREFIX": "https://tenant.feishu.cn/docx",
                "NEWS_DELIVERY_DB": shared,
                "NEWS_MAX_CLOUD_DOCUMENTS": "300",
                "ECONOMIST_INCLUDE_IMAGES": "true",
            }
            config = DeliveryConfig.from_env(env, publisher="economist")
            self.assertEqual(Path(shared), config.db_path)
            self.assertEqual(300, config.max_cloud_documents)
            self.assertTrue(config.include_images)
            self.assertEqual("/v1/economist/fetch", config.bpc_endpoint)
            self.assertEqual(("www.economist.com",), config.image_allowed_hosts)

            for name, value in (
                ("ECONOMIST_DELIVERY_DB", str(Path(tmp) / "split.db")),
                ("FEISHU_DOC_URL_PREFIX", "https://tenant.feishu.cn/wiki"),
                ("FEISHU_DOC_URL_PREFIX", "https://example.com/docx"),
            ):
                with self.subTest(name=name, value=value):
                    invalid = dict(env)
                    invalid[name] = value
                    with self.assertRaises(ValueError):
                        DeliveryConfig.from_env(invalid, publisher="economist")


def economist_item(suffix, guid):
    url = f"https://www.economist.com/business/2026/07/20/{suffix}"
    key, article_id = make_article_key(url, "economist", guid)
    return FeedArticle(
        article_key=key,
        article_id=article_id,
        normalized_url=url,
        source_url=url,
        title=suffix,
        publisher="economist",
    )


def fetched(canonical_url, request_id="request"):
    paragraphs = tuple("Full article paragraph " * 30 for _ in range(3))
    text = "\n\n".join(paragraphs)
    return FetchedArticle(
        canonical_url=canonical_url,
        title="Fetched story",
        author="The Economist",
        published_at="2026-07-20",
        paragraphs=paragraphs,
        text=text,
        sha256=hashlib.sha256(text.encode()).hexdigest(),
        fetched_at="2026-07-20T00:00:00Z",
        request_id=request_id,
    )


def config_for(path):
    return DeliveryConfig(
        bpc_api_token="bpc",
        feishu_app_id="app",
        feishu_app_secret="secret",
        feishu_receive_id="oc_test",
        feishu_doc_url_prefix="https://tenant.feishu.cn/docx",
        db_path=Path(path),
        publisher="economist",
    )


class NeverCalled:
    def __init__(self):
        self.calls = 0

    def __getattr__(self, _name):
        def fail(*_args, **_kwargs):
            self.calls += 1
            raise AssertionError("remote dependency must not be called")

        return fail


class StaticFeed:
    def __init__(self, items):
        self.items = items

    def fetch(self):
        return list(self.items)


class DurableIdentityTests(unittest.TestCase):
    def test_two_feed_identities_with_one_canonical_never_create_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            first = economist_item(
                "first-alias", "11111111-1111-4111-8111-111111111111"
            )
            second = economist_item(
                "second-alias", "22222222-2222-4222-8222-222222222222"
            )
            canonical = "https://www.economist.com/business/2026/07/20/canonical"
            with Outbox(path, publisher="economist") as outbox:
                outbox.initialize_with_items([first, second], 1000)
                outbox.mark_fetch_pending(first.article_key, "first", 1001)
                self.assertTrue(
                    outbox.mark_fetched(first.article_key, fetched(canonical, "first"), 1002)
                )
                outbox.mark_fetch_pending(second.article_key, "second", 1003)
                self.assertFalse(
                    outbox.mark_fetched(second.article_key, fetched(canonical, "second"), 1004)
                )
                duplicate = outbox.get(second.article_key)
                self.assertEqual("manual", duplicate["status"])
                self.assertEqual("DUPLICATE_CANONICAL", duplicate["last_error_code"])

    def test_initialize_current_only_is_remote_write_free_and_future_items_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            current = [
                economist_item(
                    f"baseline-{index}",
                    f"{index:08x}-1111-4111-8111-{index:012x}",
                )
                for index in range(3)
            ]
            bpc = NeverCalled()
            feishu = NeverCalled()
            images = NeverCalled()
            with Outbox(path, publisher="economist") as outbox:
                summary = DeliveryRunner(
                    config_for(path),
                    outbox,
                    StaticFeed(current),
                    bpc,
                    feishu,
                    images,
                ).run(initialize_current_only=True)
                self.assertEqual({"baseline": 3}, summary.status_counts)
                self.assertEqual([], outbox.get_work(2000, 20))
                self.assertEqual(0, bpc.calls)
                self.assertEqual(0, feishu.calls)
                self.assertEqual(0, images.calls)

                future = economist_item(
                    "future-story", "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
                )
                self.assertEqual(1, outbox.discover([future], 2001))
                self.assertEqual(
                    [future.article_key],
                    [row["article_key"] for row in outbox.get_work(2002, 20)],
                )

    def test_pre_v8_image_blob_dimensions_are_repaired_from_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.db"
            item = economist_item(
                "image-repair", "bbbbbbbb-1111-4111-8111-bbbbbbbbbbbb"
            )
            png = (
                b"\x89PNG\r\n\x1a\n"
                + (13).to_bytes(4, "big")
                + b"IHDR"
                + (1424).to_bytes(4, "big")
                + (801).to_bytes(4, "big")
                + b"\x08\x02\x00\x00\x00\x00\x00\x00\x00payload"
            )
            with Outbox(path, publisher="economist") as outbox:
                outbox.initialize_with_items([item], 1000)
                with outbox._conn:
                    outbox._conn.execute(
                        "UPDATE articles SET status='doc_created', block_cursor=2, "
                        "image_states_json=? WHERE article_key=?",
                        (
                            json.dumps(
                                {
                                    "2": {
                                        "state": "prepared",
                                        "source_url": "https://www.economist.com/content-assets/images/test.png",
                                    }
                                }
                            ),
                            item.article_key,
                        ),
                    )
                    outbox._conn.execute(
                        "INSERT INTO image_blobs(article_key,cursor,source_url,final_url,"
                        "mime_type,extension,sha256,size,width,height,data) "
                        "VALUES(?,?,?,?,?,?,?,?,0,0,?)",
                        (
                            item.article_key,
                            2,
                            "https://www.economist.com/content-assets/images/test.png",
                            "https://www.economist.com/content-assets/images/test.png",
                            "image/png",
                            "png",
                            hashlib.sha256(png).hexdigest(),
                            len(png),
                            sqlite3.Binary(png),
                        ),
                    )
                runner = DeliveryRunner(
                    config_for(path),
                    outbox,
                    StaticFeed([]),
                    NeverCalled(),
                    NeverCalled(),
                    NeverCalled(),
                )
                repaired = runner._prepared_image(item.article_key, 2)
                self.assertEqual((1424, 801), (repaired.width, repaired.height))
                stored = outbox.get_prepared_image(item.article_key, 2)
                self.assertEqual((1424, 801), (stored["width"], stored["height"]))


if __name__ == "__main__":
    unittest.main()
