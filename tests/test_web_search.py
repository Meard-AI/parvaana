"""Web search helper for theoretical Ask."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from web_search import needs_web, format_web_for_prompt  # noqa: E402


class NeedsWebTests(unittest.TestCase):
    def test_hypothetical(self):
        self.assertTrue(needs_web("How would Helm work?", intent="hypothetical"))

    def test_plain_content_no(self):
        # may still be false for simple factual - compose how without theoretical
        self.assertFalse(needs_web("list files in src", intent="content"))


class FormatWebTests(unittest.TestCase):
    def test_format(self):
        blob, cites = format_web_for_prompt(
            {
                "ok": True,
                "query": "helm compose",
                "results": [
                    {
                        "title": "T",
                        "url": "https://example.com",
                        "snippet": "hello",
                    }
                ],
            },
            start_n=3,
        )
        self.assertIn("[3]", blob)
        self.assertEqual(cites[0]["doc_type"], "web")
        self.assertEqual(cites[0]["n"], 3)


if __name__ == "__main__":
    unittest.main()
