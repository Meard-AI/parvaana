"""Heavy tests for Gremlin client + graph tools (GitAtlas schema)."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from gremlin_client import GremlinClient, parse_server_and_graph, GremlinError  # noqa: E402
from graph_tools import (  # noqa: E402
    GraphTools,
    extract_path_candidates,
    looks_temporal,
    file_vertex_id,
    commit_vertex_id,
)


class ParseUrlTests(unittest.TestCase):
    def test_parse_server_graph(self):
        s, g = parse_server_and_graph("http://127.0.0.1:18080/graphs/hugegraph")
        self.assertEqual(s, "http://127.0.0.1:18080")
        self.assertEqual(g, "hugegraph")


class IntentAndPathTests(unittest.TestCase):
    def test_looks_temporal(self):
        self.assertTrue(looks_temporal("In which commit did docker-compose change recently?"))
        self.assertTrue(looks_temporal("blast radius of abcdef1"))
        self.assertFalse(looks_temporal("what is a vertex label?"))

    def test_extract_compose_paths(self):
        paths = extract_path_candidates("which commit changed docker compose build?")
        self.assertTrue(any("docker-compose" in p for p in paths))

    def test_extract_bare_compose(self):
        paths = extract_path_candidates("When was compose last changed and by whom?")
        self.assertTrue(any("docker-compose" in p for p in paths))
        self.assertTrue(looks_temporal("When was compose last changed and by whom?"))

    def test_extract_explicit_path(self):
        paths = extract_path_candidates("history of src/foo/bar.py please")
        self.assertIn("src/foo/bar.py", paths)

    def test_ids(self):
        self.assertEqual(file_vertex_id("docker/docker-compose.yml"), "file:docker/docker-compose.yml")
        self.assertEqual(commit_vertex_id("abc"), "commit:abc")


class GremlinLiveOptional(unittest.TestCase):
    """Hit live HugeGraph if up — real shipped query path."""

    def setUp(self):
        self.client = GremlinClient("http://127.0.0.1:18080/graphs/hugegraph")
        h = self.client.health()
        if not h.get("ok"):
            self.skipTest(f"HugeGraph not healthy: {h}")

    def test_health_has_labels(self):
        h = self.client.health()
        labels = h.get("vertex_labels") or {}
        self.assertIn("File", labels)
        self.assertIn("Commit", labels)

    def test_commits_touching_compose_via_tools(self):
        tools = GraphTools(self.client)
        res = tools.commits_touching_path("docker/docker-compose.yml", limit=10)
        # Graph may or may not have this path for every install; if file vertex exists we expect commits
        file_check = self.client.execute(
            "g.V().hasLabel('File').hasId('file:docker/docker-compose.yml').count()"
        )
        count = int(file_check[0]) if file_check else 0
        if count == 0:
            self.skipTest("compose file not in graph index yet")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["source"], "gremlin")
        self.assertGreaterEqual(res["count"], 1)
        self.assertTrue(res["commits"][0].get("sha"))
        self.assertGreaterEqual(len(res["commits"][0]["sha"]), 7)

    def test_blast_radius_live_if_commit_exists(self):
        tools = GraphTools(self.client)
        rows = self.client.execute("g.V().hasLabel('Commit').limit(1).id()")
        if not rows:
            self.skipTest("no commits in graph")
        cid = str(rows[0])
        sha = cid.replace("commit:", "")
        br = tools.blast_radius(sha, limit_files=20)
        self.assertEqual(br["tool"], "blast_radius")
        # may have files if MODIFIED edges present
        self.assertIn(br["source"], ("gremlin", "git_fallback"))


class GitFallbackTests(unittest.TestCase):
    def test_small_repo_commits_touching_path(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "r"
            repo.mkdir()
            subprocess.check_call(["git", "init"], cwd=repo, stdout=subprocess.DEVNULL)
            subprocess.check_call(["git", "config", "user.email", "t@t.com"], cwd=repo)
            subprocess.check_call(["git", "config", "user.name", "t"], cwd=repo)
            (repo / "docker").mkdir()
            (repo / "docker" / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
            subprocess.check_call(["git", "add", "."], cwd=repo)
            subprocess.check_call(
                ["git", "commit", "-m", "add compose"],
                cwd=repo,
                stdout=subprocess.DEVNULL,
            )
            # Force gremlin failure → fallback
            fake = mock.Mock()
            fake.execute.side_effect = GremlinError("down")
            tools = GraphTools(fake)
            res = tools.commits_touching_path(
                "docker/docker-compose.yml", local_repo=repo, limit=5
            )
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["source"], "git_fallback")
            self.assertEqual(len(res["commits"]), 1)
            self.assertIn("compose", (res["commits"][0].get("message") or "").lower())

    def test_gather_for_temporal_question(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "r"
            repo.mkdir()
            subprocess.check_call(["git", "init"], cwd=repo, stdout=subprocess.DEVNULL)
            subprocess.check_call(["git", "config", "user.email", "t@t.com"], cwd=repo)
            subprocess.check_call(["git", "config", "user.name", "t"], cwd=repo)
            (repo / "a.yml").write_text("x: 1\n", encoding="utf-8")
            subprocess.check_call(["git", "add", "."], cwd=repo)
            subprocess.check_call(
                ["git", "commit", "-m", "add a.yml"], cwd=repo, stdout=subprocess.DEVNULL
            )
            fake = mock.Mock()
            fake.execute.side_effect = GremlinError("down")
            fake.health.return_value = {"ok": False}
            tools = GraphTools(fake)
            bag = tools.gather_for_question(
                "In which commit did a.yml change recently?",
                local_repo=repo,
            )
            self.assertTrue(bag["temporal"])
            self.assertTrue(bag["tools"])
            self.assertTrue(any(t.get("ok") for t in bag["tools"]))


class MockGremlinUnit(unittest.TestCase):
    def test_commits_parses_valuemap(self):
        client = mock.Mock()
        client.execute.return_value = [
            {
                "id": "commit:deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                "label": "Commit",
                "message": ["fix compose"],
            }
        ]
        tools = GraphTools(client)
        res = tools.commits_touching_path("docker/docker-compose.yml")
        self.assertTrue(res["ok"])
        self.assertEqual(res["source"], "gremlin")
        self.assertEqual(res["commits"][0]["sha"], "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
        self.assertEqual(res["commits"][0]["message"], "fix compose")


class GoldenCardMetaTests(unittest.TestCase):
    def test_path_scoped_meta_and_compose_substance(self):
        from graph_tools import _git_commit_meta, _compose_yaml_bullets, _focus_files

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "r"
            repo.mkdir()
            subprocess.check_call(["git", "init"], cwd=repo, stdout=subprocess.DEVNULL)
            subprocess.check_call(["git", "config", "user.email", "jane@example.com"], cwd=repo)
            subprocess.check_call(["git", "config", "user.name", "Jane Doe"], cwd=repo)
            (repo / "docker").mkdir()
            compose = (
                "name: demo\n"
                "services:\n"
                "  web:\n"
                "    image: nginx:1.25\n"
                "    ports:\n"
                "      - \"8080:80\"\n"
                "    volumes:\n"
                "      - data:/var/www\n"
                "  db:\n"
                "    image: postgres:16\n"
            )
            (repo / "docker" / "docker-compose.yml").write_text(compose, encoding="utf-8")
            (repo / "other.txt").write_text("noise\n", encoding="utf-8")
            subprocess.check_call(["git", "add", "."], cwd=repo)
            subprocess.check_call(
                ["git", "commit", "-m", "add compose stack\n\n- bring up web+db"],
                cwd=repo,
                stdout=subprocess.DEVNULL,
            )
            sha = subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
            ).strip()
            meta = _git_commit_meta(
                repo, sha, focus_path="docker/docker-compose.yml"
            )
            self.assertIsNotNone(meta)
            self.assertEqual(meta["author_name"], "Jane Doe")
            self.assertEqual(meta["author_email"], "jane@example.com")
            self.assertTrue(meta["authored_at"])
            self.assertIn("docker/docker-compose.yml", meta["files"])
            self.assertNotIn("other.txt", meta["files"])
            joined = " ".join(meta["change_summary"]).lower()
            self.assertTrue(
                "web" in joined or "services" in joined or "nginx" in joined,
                meta["change_summary"],
            )
            # pure yaml helper
            bullets = _compose_yaml_bullets(compose, "docker/docker-compose.yml")
            self.assertTrue(any("web" in b for b in bullets), bullets)
            self.assertTrue(any("nginx" in b for b in bullets), bullets)

        focused = _focus_files(
            ["docker/docker-compose.yml", "pom.xml", "docker/README.md", "src/x.java"],
            "docker/docker-compose.yml",
        )
        self.assertIn("docker/docker-compose.yml", focused)
        self.assertNotIn("pom.xml", focused)
        self.assertNotIn("src/x.java", focused)


class FormatCardTests(unittest.TestCase):
    def test_format_has_who_when_short_files_what(self):
        sys.path.insert(0, str(ROOT / "backend"))
        from ai_service import _format_commit_card

        card = _format_commit_card(
            {
                "sha": "ef5d4e0b4539ec2ace955e473a90787c9c49e691",
                "short_sha": "ef5d4e0",
                "message": "docs: document -d flag",
                "author_name": "KAI",
                "author_email": "kai@example.com",
                "authored_at": "2026-06-09T15:54:21+05:30",
                "files": ["docker/docker-compose.yml"],
                "change_summary": [
                    "docker/docker-compose.yml: added (new file), +103/-0 lines",
                    "services defined: pd, store",
                ],
            },
            path="docker/docker-compose.yml",
            repo_name="hugegraph",
            graph_source="gremlin",
        )
        self.assertIn("ef5d4e0", card)
        self.assertNotIn("ef5d4e0b4539ec2ace955e473a90787c9c49e691", card.split("detail")[0])
        self.assertIn("KAI", card)
        self.assertIn("kai@example.com", card)
        self.assertRegex(card, r"09 Jun 2026|2026")
        self.assertIn("hugegraph → docker/docker-compose.yml", card)
        self.assertIn("services defined", card)
        self.assertIn("Author:", card)
        self.assertIn("When:", card)


if __name__ == "__main__":
    unittest.main()
