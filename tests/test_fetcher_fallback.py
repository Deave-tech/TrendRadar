import json
import tempfile
import unittest
from pathlib import Path

from trendradar.crawler.fetcher import DataFetcher


class DataFetcherFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.temp_dir.name) / "hotlist-cache.json"
        self.response = json.dumps(
            {
                "status": "success",
                "items": [
                    {
                        "title": "cached headline",
                        "url": "https://example.com/news/1",
                        "mobileUrl": "",
                    }
                ],
            }
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_last_success_is_used_after_upstream_failure(self):
        fetcher = DataFetcher(fallback_cache_path=self.cache_path)
        fetcher.fetch_data = lambda _id: (self.response, "source", "source")

        first_results, _, first_failed = fetcher.crawl_websites(["source"])

        self.assertEqual([], first_failed)
        self.assertIn("cached headline", first_results["source"])
        self.assertTrue(self.cache_path.exists())

        fallback_fetcher = DataFetcher(fallback_cache_path=self.cache_path)
        fallback_fetcher.fetch_data = lambda _id: (None, "source", "source")

        fallback_results, _, fallback_failed = fallback_fetcher.crawl_websites(["source"])

        self.assertIn("cached headline", fallback_results["source"])
        self.assertEqual(["source"], fallback_fetcher.fallback_ids)
        self.assertEqual(["source"], fallback_failed)

    def test_expired_cache_is_not_used(self):
        self.cache_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "sources": {
                        "source": {"fetched_at": 0, "response": self.response}
                    },
                }
            ),
            encoding="utf-8",
        )
        fetcher = DataFetcher(
            fallback_cache_path=self.cache_path,
            fallback_max_age_seconds=60,
        )
        fetcher.fetch_data = lambda _id: (None, "source", "source")

        results, _, failed = fetcher.crawl_websites(["source"])

        self.assertEqual({}, results)
        self.assertEqual([], fetcher.fallback_ids)
        self.assertEqual(["source"], failed)


if __name__ == "__main__":
    unittest.main()
