import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trendradar.report.generator import generate_html_report


class ReportOutputPathTests(unittest.TestCase):
    def test_server_run_does_not_overwrite_tracked_root_index(self):
        original_cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
                os.environ, {"GITHUB_ACTIONS": ""}, clear=False
            ):
                os.chdir(temp_dir)
                Path("index.html").write_text("editor", encoding="utf-8")

                generate_html_report(
                    stats=[],
                    total_titles=0,
                    output_dir="output",
                    date_folder="2026-07-11",
                    time_filename="03-00",
                    render_html_func=lambda *_args: "report",
                )

                self.assertEqual("editor", Path("index.html").read_text(encoding="utf-8"))
                self.assertEqual(
                    "report", Path("output/index.html").read_text(encoding="utf-8")
                )
        finally:
            os.chdir(original_cwd)


if __name__ == "__main__":
    unittest.main()
