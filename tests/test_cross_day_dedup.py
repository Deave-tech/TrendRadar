import tempfile
import unittest

from trendradar.core.data import detect_latest_new_titles_from_storage
from trendradar.storage.base import NewsData, NewsItem, RSSData, RSSItem
from trendradar.storage.local import LocalStorageBackend


class DatePinnedStorage:
    def __init__(self, backend, current_date):
        self.backend = backend
        self.current_date = current_date

    def get_latest_crawl_data(self, date=None):
        return self.backend.get_latest_crawl_data(date or self.current_date)

    def get_today_all_data(self, date=None):
        return self.backend.get_today_all_data(date or self.current_date)

    def __getattr__(self, name):
        return getattr(self.backend, name)


class CrossDayDedupTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = LocalStorageBackend(
            data_dir=self.temp_dir.name,
            enable_txt=False,
            enable_html=False,
            timezone="Asia/Shanghai",
        )

    def tearDown(self):
        self.storage.cleanup()
        self.temp_dir.cleanup()

    def test_hotlist_first_crawl_of_day_excludes_yesterdays_titles(self):
        previous = NewsData(
            date="2026-07-11",
            crawl_time="23-00",
            items={
                "source": [
                    NewsItem(
                        title="Still on list",
                        source_id="source",
                        source_name="Source",
                        rank=1,
                        url="https://example.com/old",
                    )
                ]
            },
            id_to_name={"source": "Source"},
        )
        current = NewsData(
            date="2026-07-12",
            crawl_time="00-00",
            items={
                "source": [
                    NewsItem(
                        title="Still on list",
                        source_id="source",
                        source_name="Source",
                        rank=1,
                        url="https://example.com/old",
                    ),
                    NewsItem(
                        title="Actually new",
                        source_id="source",
                        source_name="Source",
                        rank=2,
                        url="https://example.com/new",
                    ),
                ]
            },
            id_to_name={"source": "Source"},
        )

        self.assertTrue(self.storage.save_news_data(previous))
        self.assertTrue(self.storage.save_news_data(current))

        pinned_storage = DatePinnedStorage(self.storage, "2026-07-12")
        new_titles = detect_latest_new_titles_from_storage(pinned_storage, ["source"])

        self.assertEqual(["Actually new"], list(new_titles["source"]))

    def test_rss_first_crawl_of_day_excludes_yesterdays_urls(self):
        previous = RSSData(
            date="2026-07-11",
            crawl_time="23:00",
            items={
                "feed": [
                    RSSItem(
                        title="Still in feed",
                        feed_id="feed",
                        feed_name="Feed",
                        url="https://example.com/old",
                    )
                ]
            },
            id_to_name={"feed": "Feed"},
        )
        current = RSSData(
            date="2026-07-12",
            crawl_time="00:00",
            items={
                "feed": [
                    RSSItem(
                        title="Still in feed",
                        feed_id="feed",
                        feed_name="Feed",
                        url="https://example.com/old",
                    ),
                    RSSItem(
                        title="Actually new RSS",
                        feed_id="feed",
                        feed_name="Feed",
                        url="https://example.com/new",
                    ),
                ]
            },
            id_to_name={"feed": "Feed"},
        )

        self.assertTrue(self.storage.save_rss_data(previous))
        self.assertTrue(self.storage.save_rss_data(current))

        new_items = self.storage.detect_new_rss_items(current)

        self.assertEqual(["Actually new RSS"], [item.title for item in new_items["feed"]])


if __name__ == "__main__":
    unittest.main()
