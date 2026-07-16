"""Parvaana searcher integration + scope isolation."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from catalog import Catalog, CatalogError  # noqa: E402
from parvaana_client import ParvaanaClient  # noqa: E402


class ParvaanaClientUnit(unittest.TestCase):
    def test_filter_results_by_source_ids(self):
        client = ParvaanaClient(searcher_url="http://example.invalid:3001")
        raw = {
            "results": [
                {
                    "document": {
                        "id": "a",
                        "source_id": "SRC1",
                        "title": "compose",
                        "external_id": "/repos/x/docker-compose.yml",
                        "metadata": {"path": "/repos/x/docker-compose.yml"},
                        "content": "services: web",
                    },
                    "snippet": "services: <b>web</b>",
                },
                {
                    "document": {
                        "id": "b",
                        "source_id": "SRC2",
                        "title": "other",
                        "external_id": "/repos/y/a.md",
                        "metadata": {},
                        "content": "nope",
                    }
                },
            ]
        }
        with mock.patch.object(client, "searcher_url", "http://example.invalid:3001"):
            with mock.patch("urllib.request.urlopen") as uo:
                class R:
                    def read(self):
                        return json.dumps(raw).encode()

                    def __enter__(self):
                        return self

                    def __exit__(self, *a):
                        return False

                uo.return_value = R()
                out = client.search("compose", source_ids=["SRC1"], limit=10)
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["results"][0]["source_id"], "SRC1")
        self.assertIn("compose", (out["results"][0]["path"] or "").lower() + out["results"][0]["title"].lower())


class CatalogParvaanaSearch(unittest.TestCase):
    def test_search_uses_parvaana_when_mapped(self):
        with tempfile.TemporaryDirectory() as td:
            cat = Catalog(Path(td) / "c.db")
            p = cat.create_project("HugeGraph")
            ra = cat.add_repo(p["id"], "/tmp/a", name="hugegraph-ai")
            rb = cat.add_repo(p["id"], "/tmp/b", name="other-repo")
            cat.update_repo(ra["id"], parvaana_source_id="SRC_AI")
            cat.update_repo(rb["id"], parvaana_source_id="SRC_OTHER")

            fake_hits = {
                "query": "docker-compose",
                "count": 1,
                "results": [
                    {
                        "id": "d1",
                        "source_id": "SRC_AI",
                        "title": "docker-compose.yml",
                        "path": "/repos/hg-family/hugegraph-ai/docker-compose.yml",
                        "snippet": "services:\n  llm:",
                        "doc_type": "parvaana_document",
                    }
                ],
                "backend": "parvaana_searcher",
            }
            with mock.patch("parvaana_client.ParvaanaClient") as PC:
                inst = PC.return_value
                inst.health.return_value = {"ok": True}
                inst.search.return_value = fake_hits
                out = cat.search("docker-compose", "project", p["id"], limit=5)
            self.assertEqual(out["backend"], "parvaana_searcher")
            self.assertEqual(out["count"], 1)
            self.assertEqual(out["results"][0]["repo_name"], "hugegraph-ai")
            # isolation: repo scope only SRC_AI
            with mock.patch("parvaana_client.ParvaanaClient") as PC:
                inst = PC.return_value
                inst.health.return_value = {"ok": True}
                inst.search.return_value = fake_hits
                cat.search("docker-compose", "repo", ra["id"], limit=5)
                self.assertIn("SRC_AI", str(inst.search.call_args))
                self.assertNotIn("SRC_OTHER", str(inst.search.call_args.get("kwargs") or {}) + str(inst.search.call_args))

    def test_apply_source_map(self):
        with tempfile.TemporaryDirectory() as td:
            cat = Catalog(Path(td) / "c.db")
            p = cat.create_project("P")
            cat.add_repo(p["id"], "/x", name="hugegraph")
            n = cat.apply_parvaana_source_map({"hugegraph": "SRC123"})
            self.assertEqual(n, 1)
            r = cat.list_repos(p["id"])[0]
            self.assertEqual(r["parvaana_source_id"], "SRC123")


class ParvaanaLiveOptional(unittest.TestCase):
    def test_live_searcher_and_hg_content(self):
        client = ParvaanaClient()
        h = client.health()
        if not h.get("ok"):
            self.skipTest(f"searcher down: {h}")
        m = client.load_source_map()
        if not m:
            self.skipTest("no source map")
        sids = list(m.values())
        out = client.search("docker-compose", source_ids=sids, limit=10)
        # may be empty if sync still running — still assert backend path
        self.assertEqual(out["backend"], "parvaana_searcher")
        if out["count"] == 0:
            self.skipTest("no compose hits yet (index still warming)")
        # real content snippet, not path stub
        snip = (out["results"][0].get("snippet") or "") + (out["results"][0].get("title") or "")
        self.assertFalse(snip.startswith("File path "))


if __name__ == "__main__":
    unittest.main()
