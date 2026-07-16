"""Unit/integration tests against shipped catalog + job + graph paths."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from catalog import Catalog, CatalogError  # noqa: E402
from jobs import JobRunner  # noqa: E402


def make_git_repo(path: Path, *, message: str, filename: str, content: str) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(["git", "init"], cwd=path, stdout=subprocess.DEVNULL)
    subprocess.check_call(["git", "config", "user.email", "test@example.com"], cwd=path)
    subprocess.check_call(["git", "config", "user.name", "Test"], cwd=path)
    (path / filename).write_text(content, encoding="utf-8")
    subprocess.check_call(["git", "add", filename], cwd=path)
    subprocess.check_call(["git", "commit", "-m", message], cwd=path, stdout=subprocess.DEVNULL)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, text=True).strip()
    return sha


class CatalogSearchJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "catalog.db"
        self.repos_root = self.root / "repos"
        self.export = self.root / "export"
        self.catalog = Catalog(self.db)
        self.runner = JobRunner(
            self.catalog,
            repos_root=self.repos_root,
            export_root=self.export,
            gitatlas_bin=None,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_project_and_overview_stats(self):
        p = self.catalog.create_project("HugeGraph")
        self.assertEqual(p["name"], "HugeGraph")
        ov = self.catalog.project_overview(p["id"])
        self.assertEqual(ov["stats"]["repos"], 0)
        self.assertEqual(ov["stats"]["commits"], 0)
        self.assertEqual(ov["stats"]["files"], 0)

    def test_search_rejects_missing_and_all_scope(self):
        p = self.catalog.create_project("P")
        with self.assertRaises(CatalogError) as cm:
            self.catalog.search("hello", None, None)
        self.assertEqual(cm.exception.status, 400)

        with self.assertRaises(CatalogError) as cm:
            self.catalog.search("hello", "all", p["id"])
        self.assertEqual(cm.exception.status, 400)
        self.assertIn("not supported", str(cm.exception).lower())

        with self.assertRaises(CatalogError):
            self.catalog.search("hello", "root", "x")

    def test_add_index_job_and_scope_isolation(self):
        # Two local seed repos with disjoint unique tokens
        seed_a = self.root / "seed-alpha"
        seed_b = self.root / "seed-beta"
        sha_a = make_git_repo(
            seed_a,
            message="introduce UNIQUE_ALPHA_TOKEN for alpharepo",
            filename="alpha.txt",
            content="alpha only content UNIQUE_ALPHA_TOKEN",
        )
        sha_b = make_git_repo(
            seed_b,
            message="introduce UNIQUE_BETA_TOKEN for betarepo",
            filename="beta.txt",
            content="beta only content UNIQUE_BETA_TOKEN",
        )

        p = self.catalog.create_project("Demo")
        ra = self.catalog.add_repo(p["id"], str(seed_a), name="seed-alpha")
        rb = self.catalog.add_repo(p["id"], str(seed_b), name="seed-beta")

        ja = self.catalog.create_job("add_index", project_id=p["id"], repo_id=ra["id"])
        jb = self.catalog.create_job("add_index", project_id=p["id"], repo_id=rb["id"])
        self.runner.run_job_sync(ja["id"])
        self.runner.run_job_sync(jb["id"])

        ja2 = self.catalog.get_job(ja["id"])
        self.assertEqual(ja2["status"], "completed", ja2)
        self.assertTrue(all(s["status"] == "completed" for s in ja2["stages"]))

        ov = self.catalog.project_overview(p["id"])
        self.assertEqual(ov["stats"]["repos"], 2)
        self.assertGreater(ov["stats"]["commits"], 0)
        self.assertGreater(ov["stats"]["files"], 0)

        # Project scope finds both tokens
        res_proj_a = self.catalog.search("UNIQUE_ALPHA_TOKEN", "project", p["id"])
        self.assertGreaterEqual(res_proj_a["count"], 1)
        res_proj_b = self.catalog.search("UNIQUE_BETA_TOKEN", "project", p["id"])
        self.assertGreaterEqual(res_proj_b["count"], 1)

        # Repo scope isolation
        only_a = self.catalog.search("UNIQUE_ALPHA_TOKEN", "repo", ra["id"])
        self.assertGreaterEqual(only_a["count"], 1)
        for hit in only_a["results"]:
            self.assertEqual(hit["repo_id"], ra["id"])

        cross = self.catalog.search("UNIQUE_ALPHA_TOKEN", "repo", rb["id"])
        self.assertEqual(cross["count"], 0, "beta repo must not contain alpha token")

        only_b = self.catalog.search("UNIQUE_BETA_TOKEN", "repo", rb["id"])
        self.assertGreaterEqual(only_b["count"], 1)

        # Graph: blast radius for real commit
        br = self.runner.blast_radius(ra["id"], sha_a[:12])
        self.assertTrue(
            str(br["source"]).startswith("product-control-plane")
            or br["source"] in ("gremlin", "git_fallback", "product-control-plane+git", "product-control-plane+gremlin")
        )
        self.assertIn("alpha.txt", br["files_modified"])
        self.assertTrue(br["sha"].startswith(sha_a[:12]) or br["sha"] == sha_a)

        wc = self.runner.what_changed(ra["id"], since="HEAD~5")
        self.assertGreaterEqual(len(wc["commits"]), 1)
        self.assertEqual(wc["commits"][0]["sha"], sha_a)

        # Sync lines present after index
        ra2 = self.catalog.get_repo(ra["id"])
        self.assertTrue(ra2["sync_to_sha"])
        self.assertEqual(ra2["search_state"], "ready")

    def test_repo_name_from_url(self):
        from catalog import repo_name_from_url

        self.assertEqual(repo_name_from_url("https://github.com/apache/hugegraph-ai.git"), "hugegraph-ai")
        self.assertEqual(repo_name_from_url("/tmp/foo/bar"), "bar")


class UiSourceStructureTests(unittest.TestCase):
    """Static checks that locked IA is present in shipped UI source."""

    def test_ui_has_workspaces_and_no_root_search(self):
        html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        js = (ROOT / "frontend" / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn("Workspaces", html)
        self.assertTrue(
            "Add Repository" in html
            or "Add repository" in html
            or "btn-add-repo" in html
        )
        self.assertTrue("Add & Index" in html or "Add &amp; Index" in html)
        self.assertIn("project-stats", html)
        # AI product surface
        self.assertTrue("Ask about this project" in html or "project-chat" in html)
        self.assertIn("Settings", html)
        self.assertIn("/ai/chat", js)
        self.assertIn("ai/status", js)
        # no root/all-projects search control
        self.assertNotIn('value="all"', html)
        self.assertNotIn("Everything", html)
        self.assertIn('value="project"', html)
        blob = html.lower() + js.lower()
        self.assertTrue(
            "no root search" in blob
            or "no install-wide search" in blob
            or "project & repo scope only" in blob
            or "project &amp; repo scope only" in blob
        )
        self.assertIn("blast-radius", js)
        self.assertIn("what-changed", js)


if __name__ == "__main__":
    unittest.main()
