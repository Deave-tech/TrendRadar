import os
import tempfile
import unittest

from trendradar.__main__ import NewsAnalyzer


class _Context:
    platforms = [{"id": "fresh", "name": "Fresh"}, {"id": "cached", "name": "Cached"}]

    @staticmethod
    def format_time():
        return "03:00"

    @staticmethod
    def format_date():
        return "2026-07-11"


class _Fetcher:
    fallback_ids = ["cached"]

    @staticmethod
    def crawl_websites(_ids, _interval, domain_rules=None):
        return (
            {
                "fresh": {"new": {"ranks": [1], "url": "https://example.com/new"}},
                "cached": {"old": {"ranks": [1], "url": "https://example.com/old"}},
            },
            {"fresh": "Fresh", "cached": "Cached"},
            ["cached"],
        )


class _Storage:
    backend_name = "local"

    def __init__(self):
        self.saved = None

    def save_news_data(self, data):
        self.saved = data
        return True

    @staticmethod
    def save_txt_snapshot(_data):
        return None


class AnalyzerFallbackPersistenceTests(unittest.TestCase):
    def test_fallback_items_are_returned_but_not_persisted(self):
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = _Context()
        analyzer.data_fetcher = _Fetcher()
        analyzer.storage_manager = _Storage()
        analyzer.request_interval = 0
        analyzer._platform_failed_ids = []
        analyzer._platform_fallback_ids = []

        original_cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                os.chdir(temp_dir)
                results, _, failed = analyzer._crawl_data()
        finally:
            os.chdir(original_cwd)

        self.assertIn("cached", results)
        self.assertEqual(["cached"], failed)
        self.assertEqual(["cached"], analyzer._platform_fallback_ids)
        self.assertEqual({"fresh"}, set(analyzer.storage_manager.saved.items))
        self.assertEqual(["cached"], analyzer.storage_manager.saved.failed_ids)


if __name__ == "__main__":
    unittest.main()
