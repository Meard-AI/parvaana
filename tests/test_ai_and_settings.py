"""AI status/chat + settings + junk purge — drives shipped modules."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from catalog import Catalog, is_test_project_name  # noqa: E402
from settings_store import SettingsStore  # noqa: E402
from ai_service import AIService, AIError  # noqa: E402
from jobs import JobRunner  # noqa: E402


def make_git_repo(path: Path, message: str, filename: str, content: str) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(["git", "init"], cwd=path, stdout=subprocess.DEVNULL)
    subprocess.check_call(["git", "config", "user.email", "t@example.com"], cwd=path)
    subprocess.check_call(["git", "config", "user.name", "t"], cwd=path)
    (path / filename).write_text(content, encoding="utf-8")
    subprocess.check_call(["git", "add", filename], cwd=path)
    subprocess.check_call(["git", "commit", "-m", message], cwd=path, stdout=subprocess.DEVNULL)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, text=True).strip()


class JunkAndDefaultTests(unittest.TestCase):
    def test_is_test_project_name(self):
        self.assertTrue(is_test_project_name("SkepticBrokenProject_1"))
        self.assertTrue(is_test_project_name("SkepticAddRepo_x"))
        self.assertFalse(is_test_project_name("HugeGraph"))
        self.assertFalse(is_test_project_name("Personal"))

    def test_purge_and_default_prefers_hugegraph(self):
        with tempfile.TemporaryDirectory() as td:
            cat = Catalog(Path(td) / "c.db")
            hg = cat.create_project("HugeGraph")
            cat.create_project("SkepticBrokenProject_999")
            cat.create_project("Personal")
            seed = Path(td) / "seed"
            make_git_repo(seed, "msg", "a.txt", "hello")
            cat.add_repo(hg["id"], str(seed), name="seed")
            # give HugeGraph a repo count via update after fake stats
            cat.update_repo(cat.list_repos(hg["id"])[0]["id"], commit_count=1, file_count=1)
            deleted = cat.purge_test_projects()
            self.assertTrue(any("Skeptic" in d for d in deleted))
            names = [p["name"] for p in cat.list_projects()]
            self.assertNotIn("SkepticBrokenProject_999", names)
            self.assertEqual(cat.preferred_default_project_id(), hg["id"])


class SettingsTests(unittest.TestCase):
    def test_mask_api_key(self):
        with tempfile.TemporaryDirectory() as td:
            s = SettingsStore(Path(td) / "c.db")
            s.update({"openai_api_key": "sk-abcdefghijklmnop", "ai_provider": "openai_compatible"})
            pub = s.public_view()
            self.assertTrue(pub["openai_api_key_set"])
            self.assertNotIn("sk-abcdefghijklmnop", pub["openai_api_key_masked"])
            self.assertTrue(pub["openai_api_key_masked"].endswith("mnop"))


class AIChatGroundingTests(unittest.TestCase):
    def test_chat_uses_scoped_context_and_provider(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cat = Catalog(root / "c.db")
            settings = SettingsStore(root / "c.db")
            runner = JobRunner(cat, repos_root=root / "repos", export_root=root / "export", gitatlas_bin=None)
            ai = AIService(cat, settings, runner=runner)

            p = cat.create_project("HugeGraph")
            seed = root / "repo"
            make_git_repo(seed, "add UNIQUE_RAG_TOKEN graph extract", "extract.py", "UNIQUE_RAG_TOKEN impl")
            repo = cat.add_repo(p["id"], str(seed), name="demo")
            job = cat.create_job("add_index", project_id=p["id"], repo_id=repo["id"])
            runner.run_job_sync(job["id"])

            # Force provider path with mock generate
            fake_resolved = {
                "ok": True,
                "provider": "mock",
                "model": "mock-model",
                "mode": "parvaana_prompt",
                "base_url": "http://example.invalid",
            }
            with mock.patch.object(ai, "_resolve_provider", return_value=fake_resolved):
                with mock.patch.object(ai, "_generate", return_value="The UNIQUE_RAG_TOKEN lives in extract.py [1]") as gen:
                    out = ai.chat("Where is UNIQUE_RAG_TOKEN?", "project", p["id"])
            self.assertIn("UNIQUE_RAG_TOKEN", out["answer"])
            self.assertGreaterEqual(out["context_count"], 1)
            self.assertEqual(out["scope"]["type"], "project")
            self.assertTrue(out["citations"])
            # prompt must have included retrieved context
            prompt_arg = gen.call_args[0][1]
            self.assertIn("UNIQUE_RAG_TOKEN", prompt_arg)

    def test_chat_rejects_missing_scope(self):
        with tempfile.TemporaryDirectory() as td:
            cat = Catalog(Path(td) / "c.db")
            settings = SettingsStore(Path(td) / "c.db")
            ai = AIService(cat, settings)
            with self.assertRaises(AIError) as cm:
                ai.chat("hi", None, None)
            self.assertEqual(cm.exception.status, 400)

    def test_chat_not_configured(self):
        with tempfile.TemporaryDirectory() as td:
            cat = Catalog(Path(td) / "c.db")
            settings = SettingsStore(Path(td) / "c.db")
            settings.update({"ai_provider": "disabled"})
            p = cat.create_project("P")
            ai = AIService(cat, settings)
            with self.assertRaises(AIError) as cm:
                ai.chat("hi", "project", p["id"])
            self.assertEqual(cm.exception.code, "not_configured")


class LiveParvaanaAIOptional(unittest.TestCase):
    """Integration: real Parvaana AI if container reachable."""

    def test_live_status_or_skip(self):
        from ai_service import discover_parvaana_ai_url
        import httpx

        url = discover_parvaana_ai_url()
        if not url:
            self.skipTest("parvaana-ai not discoverable")
        try:
            r = httpx.get(f"{url}/health", timeout=3.0)
        except Exception:
            self.skipTest("parvaana-ai not reachable")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get("llm_health", True))


if __name__ == "__main__":
    unittest.main()
