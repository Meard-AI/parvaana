"""Settings model/key + index failure surfaces."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from catalog import Catalog  # noqa: E402
from settings_store import SettingsStore, DEFAULTS  # noqa: E402
from jobs import JobRunner  # noqa: E402


class SettingsModelTests(unittest.TestCase):
    def test_default_model_is_minimax_m3(self):
        self.assertEqual(DEFAULTS.get("openai_model"), "MiniMax-M3")

    def test_update_model_and_key_masked(self):
        with tempfile.TemporaryDirectory() as td:
            s = SettingsStore(Path(td) / "c.db")
            s.update(
                {
                    "openai_model": "MiniMax-M3",
                    "openai_api_key": "sk-test-secret-key-xyz",
                    "openai_base_url": "https://api.minimax.chat/v1",
                    "ai_provider": "openai_compatible",
                }
            )
            pub = s.public_view()
            self.assertEqual(pub["openai_model"], "MiniMax-M3")
            self.assertTrue(pub["openai_api_key_set"])
            self.assertNotIn("sk-test-secret-key-xyz", pub["openai_api_key_masked"])
            self.assertIn("minimax", pub["openai_base_url"])


class IndexFailureTests(unittest.TestCase):
    def test_failed_job_sets_last_error_and_failed_states(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cat = Catalog(root / "c.db")
            runner = JobRunner(
                cat,
                repos_root=root / "repos",
                export_root=root / "export",
                gitatlas_bin=None,
            )
            p = cat.create_project("P")
            # invalid remote URL forces clone failure
            repo = cat.add_repo(p["id"], "https://127.0.0.1:1/nope-does-not-exist.git", name="bad")
            job = cat.create_job("add_index", project_id=p["id"], repo_id=repo["id"])
            out = runner.run_job_sync(job["id"])
            self.assertEqual(out["status"], "failed")
            r2 = cat.get_repo(repo["id"])
            self.assertEqual(r2["graph_state"], "failed")
            self.assertEqual(r2["search_state"], "failed")
            self.assertTrue(r2.get("last_error"))
            ov = cat.project_overview(p["id"])
            row = next(x for x in ov["repos"] if x["id"] == repo["id"])
            self.assertTrue(row.get("last_error"))


class AskUsesGraphToolsUnit(unittest.TestCase):
    def test_chat_includes_graph_tools_for_compose_question(self):
        from ai_service import AIService
        from settings_store import SettingsStore

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cat = Catalog(root / "c.db")
            settings = SettingsStore(root / "c.db")
            settings.update({"ai_provider": "disabled"})  # will fail generate — we patch
            p = cat.create_project("HugeGraph")
            # no need full index for graph tool path
            ai = AIService(cat, settings, runner=None)

            fake_bundle = {
                "temporal": True,
                "paths": ["docker/docker-compose.yml"],
                "tools": [
                    {
                        "tool": "commits_touching_path",
                        "path": "docker/docker-compose.yml",
                        "source": "gremlin",
                        "ok": True,
                        "count": 1,
                        "repo_name": "hugegraph",
                        "commits": [
                            {
                                "sha": "ef5d4e0b4539ec2ace955e473a90787c9c49e691",
                                "short_sha": "ef5d4e0",
                                "message": "docs: document -d flag and Docker process supervision model",
                                "author_name": "KAI",
                                "author_email": "kai@example.com",
                                "authored_at": "2026-06-09T15:54:21+05:30",
                                "files": [
                                    "docker/docker-compose.yml",
                                    "docker/docker-compose.dev.yml",
                                ],
                                "change_summary": [
                                    "docker/docker-compose.yml: added (new file), +103/-0 lines",
                                    "services defined: pd, store, server",
                                    "image: pd → hugegraph/pd:latest",
                                ],
                            }
                        ],
                    }
                ],
                "graph_health": {"ok": True},
            }

            with mock.patch("graph_tools.GraphTools") as GT:
                GT.return_value.gather_for_question.return_value = fake_bundle
                with mock.patch.object(ai, "_resolve_provider") as rp:
                    rp.return_value = {
                        "ok": True,
                        "provider": "mock",
                        "model": "MiniMax-M3",
                        "mode": "parvaana_prompt",
                        "base_url": "http://x",
                    }
                    with mock.patch.object(ai, "_generate", return_value="should not be called") as gen:
                        out = ai.chat(
                            "In which commit did docker-compose change recently?",
                            "project",
                            p["id"],
                        )
            # Golden card: WHO / WHEN / short id / files / what-changed — not 40-char hero
            ans = out["answer"]
            self.assertIn("ef5d4e0", ans)
            self.assertNotIn("ef5d4e0b4539ec2ace955e473a90787c9c49e691", ans.split("What they changed")[0])
            self.assertIn("KAI", ans)
            self.assertIn("kai@example.com", ans)
            self.assertIn("2026", ans)  # temporal
            self.assertIn("docker/docker-compose.yml", ans)
            self.assertIn("hugegraph", ans)
            self.assertIn("services defined", ans)
            self.assertEqual(out.get("mode"), "deterministic_graph")
            self.assertEqual(out.get("provider"), "graph_tools")
            gen.assert_not_called()
            self.assertTrue(out.get("graph_tools"))
            cites = [c for c in out["citations"] if c.get("doc_type") == "graph_commit"]
            self.assertTrue(cites)
            self.assertEqual(cites[0].get("sha"), "ef5d4e0")


if __name__ == "__main__":
    unittest.main()
