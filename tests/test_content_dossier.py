"""Content dossier + intent routing — DeepWiki-style compose answers."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from content_tools import (  # noqa: E402
    classify_intent,
    build_content_dossier,
    format_compose_deepwiki_answer,
    discover_files,
    summarize_compose_yaml,
)


class IntentTests(unittest.TestCase):
    def test_content_how_compose(self):
        self.assertEqual(
            classify_intent("How is Docker Compose used in HugeGraph?"),
            "content",
        )

    def test_temporal_who_touched(self):
        self.assertEqual(
            classify_intent("Who last touched docker/docker-compose.yml?"),
            "temporal",
        )

    def test_hybrid_broke(self):
        self.assertEqual(
            classify_intent("When did docker compose break in production?"),
            "hybrid",
        )

    def test_temporal_three_cluster_commit(self):
        self.assertEqual(
            classify_intent(
                "Check the last commit where docker-compose was edited when the three cluster change was updated."
            ),
            "temporal",
        )

    def test_temporal_supervision(self):
        self.assertEqual(
            classify_intent("When did the supervision change?"),
            "temporal",
        )

    def test_compound_how_and_when_is_hybrid(self):
        self.assertEqual(
            classify_intent(
                "How does the huge graph docker compose work? "
                "When was the three-cluster Docker compose upgraded last time?"
            ),
            "hybrid",
        )

    def test_hypothetical_helm(self):
        self.assertEqual(
            classify_intent("How would a Helm chart work in this repo?"),
            "hypothetical",
        )


class DossierTests(unittest.TestCase):
    def test_discover_and_format_compose(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # two fake repos
            hg = root / "hugegraph"
            ai = root / "hugegraph-ai"
            (hg / "docker").mkdir(parents=True)
            (ai / "docker").mkdir(parents=True)
            (hg / "docker" / "docker-compose.yml").write_text(
                "name: hugegraph-single\n"
                "services:\n"
                "  pd:\n"
                "    image: hugegraph/pd:latest\n"
                "    ports:\n"
                "      - \"8620:8620\"\n"
                "  store:\n"
                "    image: hugegraph/store:latest\n"
                "  server:\n"
                "    image: hugegraph/server:latest\n",
                encoding="utf-8",
            )
            (hg / "docker" / "README.md").write_text(
                "# HugeGraph Docker Deployment\n\n| File | Description |\n",
                encoding="utf-8",
            )
            (ai / "docker" / "docker-compose-llm.yml").write_text(
                "services:\n"
                "  hugegraph-llm:\n"
                "    build:\n"
                "      context: ..\n"
                "    ports:\n"
                "      - \"8001:8001\"\n",
                encoding="utf-8",
            )
            repos = [
                {"name": "hugegraph", "local_path": hg},
                {"name": "hugegraph-ai", "local_path": ai},
            ]
            dossier = build_content_dossier(
                repos, "How is Docker Compose used?", max_files=10
            )
            self.assertTrue(dossier["ok"])
            self.assertGreaterEqual(dossier["file_count"], 2)
            dw = format_compose_deepwiki_answer(dossier)
            self.assertIsNotNone(dw)
            ans = dw["answer"]
            self.assertTrue(
                "Docker Compose" in ans and ("Summary" in ans or "summary" in ans.lower()),
                ans[:400],
            )
            self.assertIn("hugegraph", ans)
            self.assertIn("pd", ans)
            self.assertIn("docker-compose.yml", ans)
            self.assertNotIn("```yaml", ans)
            self.assertTrue(dw["citations"])

    def test_summarize_yaml(self):
        body = (
            "name: demo\nservices:\n  web:\n    image: nginx:1\n"
            "    ports:\n      - \"80:80\"\n"
        )
        s = summarize_compose_yaml(body, "docker/docker-compose.yml")
        self.assertIn("web", s["services"])
        self.assertTrue(any("nginx" in i for i in s["images"]))


class ChatContentPathTests(unittest.TestCase):
    def test_chat_compose_how_uses_content_mode(self):
        from ai_service import AIService
        from catalog import Catalog
        from settings_store import SettingsStore

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cat = Catalog(root / "c.db")
            settings = SettingsStore(root / "c.db")
            p = cat.create_project("HugeGraph")
            # seed a local clone-like tree and register repo
            repo_path = root / "hugegraph"
            (repo_path / "docker").mkdir(parents=True)
            (repo_path / "docker" / "docker-compose.yml").write_text(
                "name: hg\nservices:\n  pd:\n    image: hugegraph/pd:latest\n"
                "  store:\n    image: hugegraph/store:latest\n"
                "  server:\n    image: hugegraph/server:latest\n",
                encoding="utf-8",
            )
            (repo_path / "docker" / "README.md").write_text(
                "# Docker\n| File | Description |\n", encoding="utf-8"
            )
            # Catalog API for add repo may be heavy — patch list_repos/get
            ai = AIService(cat, settings, runner=None)
            fake_repo = {
                "id": "r1",
                "name": "hugegraph",
                "local_path": str(repo_path),
                "project_id": p["id"],
            }
            with mock.patch.object(cat, "get_project", return_value=p):
                with mock.patch.object(cat, "list_repos", return_value=[fake_repo]):
                    with mock.patch.object(ai, "retrieve_context", return_value=[]):
                        with mock.patch.object(ai, "graph_context", return_value={}):
                            with mock.patch(
                                "graph_tools.GraphTools"
                            ) as GT:
                                GT.return_value.graph_health.return_value = {
                                    "ok": False
                                }
                                GT.return_value.gather_for_question.return_value = {
                                    "tools": [],
                                    "temporal": False,
                                }
                                with mock.patch.object(
                                    ai,
                                    "_resolve_provider",
                                    return_value={
                                        "ok": True,
                                        "provider": "mock",
                                        "model": "test",
                                        "mode": "parvaana_prompt",
                                        "base_url": "http://x",
                                    },
                                ):
                                    with mock.patch.object(
                                        ai,
                                        "_generate",
                                        return_value=(
                                            "Docker Compose runs pd/store/server from "
                                            "workspace files — reasoned from evidence."
                                        ),
                                    ) as gen:
                                        out = ai.chat(
                                            "How is Docker Compose used across this project?",
                                            "project",
                                            p["id"],
                                        )
            # Model reasons over dossier (not hard-coded final essay)
            self.assertEqual(out.get("mode"), "llm")
            self.assertEqual(out.get("intent"), "content")
            self.assertIn("pd", out["answer"])
            gen.assert_called_once()
            prompt = gen.call_args[0][1]
            self.assertIn("docker-compose", prompt.lower())


if __name__ == "__main__":
    unittest.main()
