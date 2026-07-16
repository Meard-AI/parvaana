"""Job runner: clone → GitAtlas index → search ingest (export cards + FTS).

Graph queries (blast radius / what-changed) use local git + optional GitAtlas.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from catalog import Catalog

log = logging.getLogger("product.jobs")

DEFAULT_STAGES = ["clone", "index", "ingest"]


class JobRunner:
    def __init__(
        self,
        catalog: Catalog,
        *,
        repos_root: Path,
        export_root: Path,
        gitatlas_bin: Optional[Path] = None,
        gitatlas_cwd: Optional[Path] = None,
        hugegraph_url: str = "http://127.0.0.1:18080/graphs/hugegraph",
    ):
        self.catalog = catalog
        self.repos_root = Path(repos_root)
        self.export_root = Path(export_root)
        self.gitatlas_bin = Path(gitatlas_bin) if gitatlas_bin else None
        self.gitatlas_cwd = Path(gitatlas_cwd) if gitatlas_cwd else None
        self.hugegraph_url = hugegraph_url
        self.repos_root.mkdir(parents=True, exist_ok=True)
        self.export_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def enqueue_add_index(self, project_id: str, repo_id: str) -> dict:
        job = self.catalog.create_job(
            "add_index",
            project_id=project_id,
            repo_id=repo_id,
            stages=DEFAULT_STAGES,
        )
        t = threading.Thread(target=self._run_job, args=(job["id"],), daemon=True)
        t.start()
        return job

    def enqueue_sync(self, repo_id: str) -> dict:
        repo = self.catalog.get_repo(repo_id)
        job = self.catalog.create_job(
            "sync",
            project_id=repo["project_id"],
            repo_id=repo_id,
            stages=["clone", "index", "ingest"],
        )
        t = threading.Thread(target=self._run_job, args=(job["id"],), daemon=True)
        t.start()
        return job

    def enqueue_reindex(self, repo_id: str) -> dict:
        return self.enqueue_sync(repo_id)

    def run_job_sync(self, job_id: str) -> dict:
        """Synchronous run for tests."""
        return self._run_job(job_id)

    def _run_job(self, job_id: str) -> dict:
        with self._lock:
            job = self.catalog.get_job(job_id)
            repo_id = job["repo_id"]
            if not repo_id:
                return self.catalog.update_job(job_id, status="failed", error="no repo_id")
            try:
                self.catalog.update_job(job_id, status="running", stage="clone")
                repo = self.catalog.get_repo(repo_id)
                self.catalog.update_repo(repo_id, last_error=None)
                local = self._stage_clone(job_id, repo)
                self._stage_index(job_id, repo, local)
                self._stage_ingest(job_id, repo, local)
                return self.catalog.get_job(job_id)
            except Exception as e:
                log.exception("job %s failed", job_id)
                try:
                    self.catalog.set_job_stage(job_id, self.catalog.get_job(job_id)["stage"], "failed")
                except Exception:
                    pass
                try:
                    self.catalog.update_repo(
                        repo_id,
                        graph_state="failed",
                        search_state="failed",
                        last_error=str(e)[:2000],
                    )
                except Exception:
                    pass
                return self.catalog.update_job(job_id, status="failed", error=str(e))

    def _stage_clone(self, job_id: str, repo: dict) -> Path:
        self.catalog.set_job_stage(job_id, "clone", "running")
        dest = self.repos_root / repo["project_id"] / repo["name"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = repo["url"]

        # Local path source: full local clone (preserve available history)
        if Path(url).exists() and (Path(url) / ".git").exists():
            src = Path(url).resolve()
            if dest.exists():
                _run(["git", "-C", str(dest), "fetch", "--all"], check=False)
            else:
                # --no-hardlinks still local; avoid shallow so stats reflect real history when present
                _run(["git", "clone", "--local", "--no-hardlinks", str(src), str(dest)])
        elif dest.exists() and (dest / ".git").exists():
            _run(["git", "-C", str(dest), "fetch", "--all"], check=False)
            _run(["git", "-C", str(dest), "pull", "--ff-only"], check=False)
        else:
            if dest.exists():
                shutil.rmtree(dest)
            # Remote: shallow by default; PRODUCT_FULL_CLONE=1 for full history
            if os.environ.get("PRODUCT_FULL_CLONE", "").strip() in ("1", "true", "yes"):
                _run(["git", "clone", url, str(dest)])
            else:
                _run(["git", "clone", "--depth", "50", url, str(dest)])

        # Stats
        commit_count = int(_run_out(["git", "-C", str(dest), "rev-list", "--all", "--count"]) or "0")
        files = _run_out(["git", "-C", str(dest), "ls-files"]).splitlines()
        file_count = len([f for f in files if f.strip()])
        head = _run_out(["git", "-C", str(dest), "rev-parse", "HEAD"]).strip()
        prev = repo.get("sync_to_sha")
        self.catalog.update_repo(
            repo["id"],
            local_path=str(dest),
            sync_from_sha=prev,
            sync_to_sha=head,
            last_sync_at=time.time(),
            commit_count=commit_count,
            file_count=file_count,
        )
        self.catalog.set_job_stage(job_id, "clone", "completed")
        return dest

    def _stage_index(self, job_id: str, repo: dict, local: Path) -> None:
        self.catalog.set_job_stage(job_id, "index", "running")
        self.catalog.update_repo(repo["id"], graph_state="indexing")
        # Prefer GitAtlas CLI when available and HugeGraph is up; always succeed with local graph index.
        used_gitatlas = False
        if self.gitatlas_bin and self.gitatlas_bin.exists():
            env = os.environ.copy()
            env["GITATLAS_HUGEGRAPH_URL"] = self.hugegraph_url
            cwd = str(self.gitatlas_cwd or self.gitatlas_bin.parent)
            # Index by local path (CLI supports local paths via extractRepoName)
            try:
                r = subprocess.run(
                    [str(self.gitatlas_bin), "index", str(local)],
                    cwd=cwd,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
                if r.returncode == 0:
                    used_gitatlas = True
                    log.info("gitatlas index ok for %s", repo["name"])
                else:
                    log.warning("gitatlas index failed (continuing with local): %s", r.stderr[-500:] if r.stderr else r.stdout)
            except Exception as e:
                log.warning("gitatlas index error: %s", e)

        # Always write a lightweight local graph snapshot for product queries
        self._write_local_graph(local, repo)
        self.catalog.update_repo(
            repo["id"],
            graph_state="ready" if used_gitatlas else "ready_local",
        )
        self.catalog.set_job_stage(job_id, "index", "completed")

    def _write_local_graph(self, local: Path, repo: dict) -> None:
        """Cache recent commits for blast-radius / what-changed without Hubble."""
        graph_dir = local / ".product-graph"
        graph_dir.mkdir(exist_ok=True)
        log_fmt = "%H%x1f%an%x1f%ae%x1f%cI%x1f%s"
        out = _run_out(["git", "-C", str(local), "log", "-n", "200", f"--format={log_fmt}"])
        commits_path = graph_dir / "commits.tsv"
        commits_path.write_text(out, encoding="utf-8")
        # per-commit files
        files_dir = graph_dir / "files"
        files_dir.mkdir(exist_ok=True)
        for line in out.splitlines()[:80]:
            if not line.strip():
                continue
            sha = line.split("\x1f", 1)[0]
            files = _run_out(["git", "-C", str(local), "show", "--name-only", "--format=", sha])
            (files_dir / f"{sha}.txt").write_text(files, encoding="utf-8")

    def _stage_ingest(self, job_id: str, repo: dict, local: Path) -> None:
        self.catalog.set_job_stage(job_id, "ingest", "running")
        self.catalog.update_repo(repo["id"], search_state="ingesting")
        self.catalog.clear_repo_docs(repo["id"])

        export_repo = self.export_root / "repos" / repo["name"]
        commits_dir = export_repo / "commits"
        commits_dir.mkdir(parents=True, exist_ok=True)

        log_fmt = "%H%x1f%an%x1f%ae%x1f%cI%x1f%s"
        out = _run_out(["git", "-C", str(local), "log", "-n", "100", f"--format={log_fmt}"])
        count = 0
        for line in out.splitlines():
            if not line.strip():
                continue
            parts = line.split("\x1f")
            if len(parts) < 5:
                continue
            sha, author, email, when, subject = parts[0], parts[1], parts[2], parts[3], parts[4]
            files = _run_out(["git", "-C", str(local), "show", "--name-only", "--format=", sha])
            file_list = [f for f in files.splitlines() if f.strip()]
            body = (
                f"Commit {sha}\n"
                f"Author: {author} <{email}>\n"
                f"Date: {when}\n"
                f"Subject: {subject}\n"
                f"Repository: {repo['name']}\n"
                f"Files:\n" + "\n".join(f"- {f}" for f in file_list[:50])
            )
            title = f"{repo['name']}: {subject}"
            md = f"# {title}\n\n{body}\n"
            (commits_dir / f"{sha[:12]}.md").write_text(md, encoding="utf-8")
            self.catalog.add_search_doc(
                project_id=repo["project_id"],
                repo_id=repo["id"],
                repo_name=repo["name"],
                doc_type="commit",
                title=title,
                body=body,
                sha=sha,
                path=None,
            )
            count += 1

        # File path docs (HEAD tree sample)
        files = _run_out(["git", "-C", str(local), "ls-files"]).splitlines()
        files_dir = export_repo / "files"
        files_dir.mkdir(parents=True, exist_ok=True)
        for f in files[:200]:
            f = f.strip()
            if not f:
                continue
            title = f"{repo['name']}: {f}"
            body = f"File path {f} in repository {repo['name']}"
            safe = f.replace("/", "__")[:120]
            (files_dir / f"{safe}.md").write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
            self.catalog.add_search_doc(
                project_id=repo["project_id"],
                repo_id=repo["id"],
                repo_name=repo["name"],
                doc_type="file",
                title=title,
                body=body,
                path=f,
            )

        self.catalog.update_repo(repo["id"], search_state="ready")
        self.catalog.set_job_stage(job_id, "ingest", "completed")
        log.info("ingested %s commits for %s", count, repo["name"])

    # ── Graph product queries ─────────────────────────────────

    def blast_radius(self, repo_id: str, sha: str) -> dict:
        """Prefer GitAtlas/HugeGraph Gremlin; fall back to git on local clone."""
        from graph_tools import GraphTools

        repo = self.catalog.get_repo(repo_id)
        local = Path(repo["local_path"] or "")
        local_arg = local if local.exists() else None
        tools = GraphTools()
        br = tools.blast_radius(sha, local_repo=local_arg)
        related = []
        for c in br.get("related_commits") or []:
            related.append(
                {
                    "sha": c.get("sha"),
                    "subject": c.get("message") or c.get("subject"),
                    "via_file": c.get("via_file"),
                }
            )
        return {
            "repo_id": repo_id,
            "repo_name": repo["name"],
            "sha": br.get("sha") or sha,
            "subject": br.get("message"),
            "files_modified": br.get("files_modified") or [],
            "related_commits": related[:40],
            "source": br.get("source_label") or br.get("source") or "gremlin",
            "graph_tool": br,
        }

    def commits_for_path(self, repo_id: str, path: str, limit: int = 20) -> dict:
        from graph_tools import GraphTools

        repo = self.catalog.get_repo(repo_id)
        local = Path(repo["local_path"] or "")
        tools = GraphTools()
        res = tools.commits_touching_path(
            path, limit=limit, local_repo=local if local.exists() else None
        )
        return {
            "repo_id": repo_id,
            "repo_name": repo["name"],
            **res,
        }

    def what_changed(self, repo_id: str, since: str = "HEAD~10") -> dict:
        repo = self.catalog.get_repo(repo_id)
        local = Path(repo["local_path"] or "")
        if not local.exists():
            raise RuntimeError("repository not cloned yet")
        rev_range = f"{since}..HEAD" if ".." not in since and not since.startswith("HEAD") else (
            since if ".." in since else f"{since}..HEAD"
        )
        # if since is a date-ish or just HEAD~N
        if since.startswith("HEAD") or re_is_sha_or_rev(since):
            log_args = ["git", "-C", str(local), "log", since if ".." in since else f"{since}", "-n", "50", "--format=%H%x1f%an%x1f%cI%x1f%s"]
            if ".." not in since and since.startswith("HEAD~"):
                log_args = ["git", "-C", str(local), "log", f"-n", since.replace("HEAD~", "") or "10", "--format=%H%x1f%an%x1f%cI%x1f%s"]
        else:
            log_args = ["git", "-C", str(local), "log", f"--since={since}", "-n", "50", "--format=%H%x1f%an%x1f%cI%x1f%s"]

        out = _run_out(log_args)
        commits = []
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) < 4:
                continue
            sha, author, when, subject = parts[0], parts[1], parts[2], parts[3]
            files = [
                f
                for f in _run_out(["git", "-C", str(local), "show", "--name-only", "--format=", sha]).splitlines()
                if f.strip()
            ]
            commits.append(
                {
                    "sha": sha,
                    "author": author,
                    "date": when,
                    "subject": subject,
                    "files": files[:30],
                }
            )
        return {
            "repo_id": repo_id,
            "repo_name": repo["name"],
            "since": since,
            "commits": commits,
            "source": "product-control-plane",
        }

    def graph_stats(self, repo_id: str) -> dict:
        repo = self.catalog.get_repo(repo_id)
        return {
            "repo_id": repo_id,
            "repo_name": repo["name"],
            "commits": repo["commit_count"],
            "files": repo["file_count"],
            "sync_from_sha": repo["sync_from_sha"],
            "sync_to_sha": repo["sync_to_sha"],
            "graph_state": repo["graph_state"],
            "search_state": repo["search_state"],
            "source": "product-control-plane",
        }


def re_is_sha_or_rev(s: str) -> bool:
    return bool(s) and (s.startswith("HEAD") or all(c in "0123456789abcdef" for c in s[:7].lower()))


def _run(cmd: list[str], check: bool = True, timeout: int = 300) -> subprocess.CompletedProcess:
    log.info("run: %s", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"command failed ({r.returncode}): {' '.join(cmd)}\n{r.stderr or r.stdout}")
    return r


def _run_out(cmd: list[str], timeout: int = 120) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        return ""
    return r.stdout or ""
