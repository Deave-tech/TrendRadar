import unittest

from trendradar.__main__ import NewsAnalyzer
from trendradar.report.generator import prepare_report_data
from trendradar.storage.base import RSSData, RSSItem


class _RSSContext:
    timezone = "Asia/Shanghai"
    rss_config = {
        "FRESHNESS_FILTER": {"ENABLED": True, "MAX_AGE_DAYS": 3},
    }
    rss_feeds = []
    config = {
        "DISPLAY": {"REGIONS": {"RSS": True}},
        "MAX_NEWS_PER_KEYWORD": 1,
        "SORT_BY_POSITION_FIRST": False,
        "TIMEZONE": "Asia/Shanghai",
        "DEBUG": False,
    }

    @staticmethod
    def load_frequency_words(_frequency_file):
        return (
            [
                {
                    "required": [],
                    "normal": [{"word": "AI", "is_regex": False, "pattern": None}],
                    "group_key": "AI",
                    "display_name": "AI",
                    "max_count": 0,
                }
            ],
            [],
            [],
        )


class _RSSStorage:
    def __init__(self, items):
        self.items = items

    def detect_new_rss_items(self, _rss_data):
        return self.items


class UnfilteredNewItemsTests(unittest.TestCase):
    def test_hotlist_new_items_do_not_require_a_stats_match(self):
        report = prepare_report_data(
            stats=[
                {
                    "word": "AI",
                    "count": 1,
                    "titles": [
                        {
                            "title": "AI headline",
                            "source_name": "Source",
                            "time_display": "",
                            "count": 1,
                            "ranks": [1],
                            "rank_threshold": 5,
                            "url": "",
                            "is_new": True,
                        }
                    ],
                }
            ],
            new_titles={
                "source": {
                    "AI headline": {"ranks": [1], "url": ""},
                    "Typhoon headline": {"ranks": [2], "url": ""},
                }
            },
            id_to_name={"source": "Source"},
            mode="incremental",
        )

        titles = [
            item["title"]
            for source in report["new_titles"]
            for item in source["titles"]
        ]
        self.assertEqual(2, report["total_new_count"])
        self.assertEqual(["AI headline", "Typhoon headline"], titles)

    def test_incremental_push_is_valid_with_only_unfiltered_new_titles(self):
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.report_mode = "incremental"

        self.assertTrue(
            analyzer._has_valid_content(
                stats=[],
                new_titles={"source": {"Typhoon headline": {"ranks": [1]}}},
            )
        )

    def test_incremental_rss_new_items_ignore_keyword_filters(self):
        rss_items = {
            "feed": [
                RSSItem(
                    title="No configured keyword one",
                    feed_id="feed",
                    feed_name="Feed",
                    url="https://example.com/1",
                    published_at="2020-01-01T00:00:00+08:00",
                ),
                RSSItem(
                    title="No configured keyword two",
                    feed_id="feed",
                    feed_name="Feed",
                    url="https://example.com/2",
                    published_at="2020-01-02T00:00:00+08:00",
                ),
            ]
        }
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = _RSSContext()
        analyzer.storage_manager = _RSSStorage(rss_items)
        analyzer.report_mode = "incremental"
        analyzer.frequency_file = None
        analyzer.rank_threshold = 5
        analyzer._rss_total_count = 0

        rss_data = RSSData(
            date="2026-07-11",
            crawl_time="22:01",
            items=rss_items,
            id_to_name={"feed": "Feed"},
        )
        rss_stats, rss_new_stats, _, _ = analyzer._process_rss_data_by_mode(rss_data)

        self.assertIsNone(rss_stats)
        self.assertEqual(1, len(rss_new_stats))
        self.assertEqual(2, len(rss_new_stats[0]["titles"]))


if __name__ == "__main__":
    unittest.main()
