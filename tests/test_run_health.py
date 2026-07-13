import json
import tempfile
import unittest
from pathlib import Path

from trendradar.core.run_health import append_run_health


class RunHealthTests(unittest.TestCase):
    def test_records_are_appended_as_json_lines(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first = {"status": "ok", "hotlist": {"failed_ids": []}}
            second = {"status": "degraded", "hotlist": {"failed_ids": ["baidu"]}}

            path = append_run_health(first, temp_dir)
            append_run_health(second, temp_dir)

            records = [
                json.loads(line)
                for line in Path(path).read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([first, second], records)


if __name__ == "__main__":
    unittest.main()
