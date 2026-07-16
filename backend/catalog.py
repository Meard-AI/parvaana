"""Catalog DB: projects, flat repositories, jobs, search documents, aggregates.

Pure logic separated from HTTP so unit tests drive the real functions.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterator, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS repositories (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    local_path TEXT,
    sync_from_sha TEXT,
    sync_to_sha TEXT,
    last_sync_at REAL,
    commit_count INTEGER NOT NULL DEFAULT 0,
    file_count INTEGER NOT NULL DEFAULT 0,
    graph_state TEXT NOT NULL DEFAULT 'pending',
    search_state TEXT NOT NULL DEFAULT 'pending',
    last_error TEXT,
    parvaana_source_id TEXT,
    created_at REAL NOT NULL,
    UNIQUE(project_id, name)
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    project_id TEXT,
    repo_id TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    stage TEXT NOT NULL DEFAULT 'queued',
    stages_json TEXT NOT NULL DEFAULT '[]',
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS search_docs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    repo_id TEXT NOT NULL,
    repo_name TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    sha TEXT,
    path TEXT,
    created_at REAL NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    title, body, repo_name, path,
    content='search_docs',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS search_docs_ai AFTER INSERT ON search_docs BEGIN
  INSERT INTO search_fts(rowid, title, body, repo_name, path)
  VALUES (new.rowid, new.title, new.body, new.repo_name, new.path);
END;

CREATE TRIGGER IF NOT EXISTS search_docs_ad AFTER DELETE ON search_docs BEGIN
  INSERT INTO search_fts(search_fts, rowid, title, body, repo_name, path)
  VALUES ('delete', old.rowid, old.title, old.body, old.repo_name, old.path);
END;
"""


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def now() -> float:
    return time.time()


class CatalogError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Catalog:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = self._connect()
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def _init(self) -> None:
        with self.conn() as c:
            c.executescript(SCHEMA)
            # lightweight migrations for older DBs
            cols = {r[1] for r in c.execute("PRAGMA table_info(repositories)").fetchall()}
            if "last_error" not in cols:
                c.execute("ALTER TABLE repositories ADD COLUMN last_error TEXT")
            if "parvaana_source_id" not in cols:
                c.execute("ALTER TABLE repositories ADD COLUMN parvaana_source_id TEXT")

    # ── Projects ──────────────────────────────────────────────

    def create_project(self, name: str) -> dict:
        name = (name or "").strip()
        if not name:
            raise CatalogError("project name required")
        pid = new_id()
        try:
            with self.conn() as c:
                c.execute(
                    "INSERT INTO projects (id, name, created_at) VALUES (?, ?, ?)",
                    (pid, name, now()),
                )
        except sqlite3.IntegrityError as e:
            raise CatalogError(f"project already exists: {name}", 409) from e
        return self.get_project(pid)

    def list_projects(self, *, hide_test: bool = True) -> list[dict]:
        with self.conn() as c:
            rows = c.execute(
                """
                SELECT p.*, COUNT(r.id) AS repo_count
                FROM projects p
                LEFT JOIN repositories r ON r.project_id = p.id
                GROUP BY p.id
                ORDER BY p.name COLLATE NOCASE
                """
            ).fetchall()
        out = [dict(r) for r in rows]
        if hide_test:
            out = [p for p in out if not is_test_project_name(p.get("name") or "")]
        return out

    def purge_test_projects(self) -> list[str]:
        """Delete Skeptic*/test junk projects. Returns deleted names."""
        deleted = []
        with self.conn() as c:
            rows = c.execute("SELECT id, name FROM projects").fetchall()
            for row in rows:
                name = row["name"] or ""
                if not is_test_project_name(name):
                    continue
                pid = row["id"]
                c.execute("DELETE FROM search_docs WHERE project_id = ?", (pid,))
                c.execute("DELETE FROM jobs WHERE project_id = ?", (pid,))
                c.execute("DELETE FROM repositories WHERE project_id = ?", (pid,))
                c.execute("DELETE FROM projects WHERE id = ?", (pid,))
                deleted.append(name)
        return deleted

    def preferred_default_project_id(self) -> Optional[str]:
        """HugeGraph if present with repos; else first real project with repos; else first real."""
        projects = self.list_projects(hide_test=True)
        if not projects:
            return None
        for p in projects:
            if p["name"] == "HugeGraph" and int(p.get("repo_count") or 0) > 0:
                return p["id"]
        for p in projects:
            if int(p.get("repo_count") or 0) > 0:
                return p["id"]
        for p in projects:
            if p["name"] == "HugeGraph":
                return p["id"]
        return projects[0]["id"]

    def get_project(self, project_id: str) -> dict:
        with self.conn() as c:
            row = c.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not row:
            raise CatalogError("project not found", 404)
        return dict(row)

    def delete_project(self, project_id: str) -> None:
        with self.conn() as c:
            cur = c.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            if cur.rowcount == 0:
                raise CatalogError("project not found", 404)
            c.execute("DELETE FROM repositories WHERE project_id = ?", (project_id,))
            c.execute("DELETE FROM search_docs WHERE project_id = ?", (project_id,))

    def patch_project(self, project_id: str, name: Optional[str] = None) -> dict:
        self.get_project(project_id)
        if name is not None:
            name = name.strip()
            if not name:
                raise CatalogError("name cannot be empty")
            with self.conn() as c:
                try:
                    c.execute("UPDATE projects SET name = ? WHERE id = ?", (name, project_id))
                except sqlite3.IntegrityError as e:
                    raise CatalogError(f"name taken: {name}", 409) from e
        return self.get_project(project_id)

    # ── Repos ─────────────────────────────────────────────────

    def add_repo(
        self,
        project_id: str,
        url: str,
        *,
        name: Optional[str] = None,
        local_path: Optional[str] = None,
    ) -> dict:
        self.get_project(project_id)
        url = (url or "").strip()
        if not url:
            raise CatalogError("repository url required")
        if not name:
            name = repo_name_from_url(url)
        rid = new_id()
        try:
            with self.conn() as c:
                c.execute(
                    """
                    INSERT INTO repositories
                    (id, project_id, name, url, local_path, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (rid, project_id, name, url, local_path, now()),
                )
        except sqlite3.IntegrityError as e:
            raise CatalogError(f"repo already in project: {name}", 409) from e
        return self.get_repo(rid)

    def list_repos(self, project_id: str) -> list[dict]:
        self.get_project(project_id)
        with self.conn() as c:
            rows = c.execute(
                """
                SELECT * FROM repositories
                WHERE project_id = ?
                ORDER BY name COLLATE NOCASE
                """,
                (project_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_repo(self, repo_id: str) -> dict:
        with self.conn() as c:
            row = c.execute("SELECT * FROM repositories WHERE id = ?", (repo_id,)).fetchone()
        if not row:
            raise CatalogError("repository not found", 404)
        return dict(row)

    def delete_repo(self, project_id: str, repo_id: str) -> None:
        with self.conn() as c:
            cur = c.execute(
                "DELETE FROM repositories WHERE id = ? AND project_id = ?",
                (repo_id, project_id),
            )
            if cur.rowcount == 0:
                raise CatalogError("repository not found", 404)
            c.execute("DELETE FROM search_docs WHERE repo_id = ?", (repo_id,))

    def update_repo(self, repo_id: str, **fields: Any) -> dict:
        allowed = {
            "local_path",
            "sync_from_sha",
            "sync_to_sha",
            "last_sync_at",
            "commit_count",
            "file_count",
            "graph_state",
            "search_state",
            "last_error",
            "parvaana_source_id",
        }
        sets = []
        vals: list[Any] = []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k} = ?")
                vals.append(v)
        if not sets:
            return self.get_repo(repo_id)
        vals.append(repo_id)
        with self.conn() as c:
            c.execute(f"UPDATE repositories SET {', '.join(sets)} WHERE id = ?", vals)
        return self.get_repo(repo_id)

    def project_stats(self, project_id: str) -> dict:
        self.get_project(project_id)
        with self.conn() as c:
            row = c.execute(
                """
                SELECT COUNT(*) AS repos,
                       COALESCE(SUM(commit_count), 0) AS commits,
                       COALESCE(SUM(file_count), 0) AS files
                FROM repositories WHERE project_id = ?
                """,
                (project_id,),
            ).fetchone()
        return {
            "repos": int(row["repos"]),
            "commits": int(row["commits"]),
            "files": int(row["files"]),
        }

    def project_overview(self, project_id: str) -> dict:
        project = self.get_project(project_id)
        stats = self.project_stats(project_id)
        repos = self.list_repos(project_id)
        return {
            "project": project,
            "stats": stats,
            "repos": [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "url": r["url"],
                    "sync_from_sha": r["sync_from_sha"],
                    "sync_to_sha": r["sync_to_sha"],
                    "last_sync_at": r["last_sync_at"],
                    "commit_count": r["commit_count"],
                    "file_count": r["file_count"],
                    "graph_state": r["graph_state"],
                    "search_state": r["search_state"],
                    "last_error": dict(r).get("last_error"),
                    "parvaana_source_id": dict(r).get("parvaana_source_id"),
                }
                for r in repos
            ],
        }

    # ── Jobs ──────────────────────────────────────────────────

    def create_job(
        self,
        kind: str,
        *,
        project_id: Optional[str] = None,
        repo_id: Optional[str] = None,
        stages: Optional[list[str]] = None,
    ) -> dict:
        jid = new_id()
        stages = stages or ["clone", "index", "ingest"]
        t = now()
        stages_json = json.dumps(
            [{"name": s, "status": "pending", "started_at": None, "finished_at": None} for s in stages]
        )
        with self.conn() as c:
            c.execute(
                """
                INSERT INTO jobs (id, kind, project_id, repo_id, status, stage, stages_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'queued', 'queued', ?, ?, ?)
                """,
                (jid, kind, project_id, repo_id, stages_json, t, t),
            )
        return self.get_job(jid)

    def get_job(self, job_id: str) -> dict:
        with self.conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            raise CatalogError("job not found", 404)
        d = dict(row)
        d["stages"] = json.loads(d.pop("stages_json") or "[]")
        return d

    def list_jobs(self, limit: int = 50) -> list[dict]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["stages"] = json.loads(d.pop("stages_json") or "[]")
            out.append(d)
        return out

    def update_job(
        self,
        job_id: str,
        *,
        status: Optional[str] = None,
        stage: Optional[str] = None,
        error: Optional[str] = None,
        stages: Optional[list] = None,
    ) -> dict:
        job = self.get_job(job_id)
        stages_json = json.dumps(stages if stages is not None else job["stages"])
        with self.conn() as c:
            c.execute(
                """
                UPDATE jobs SET
                    status = COALESCE(?, status),
                    stage = COALESCE(?, stage),
                    error = COALESCE(?, error),
                    stages_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (status, stage, error, stages_json, now(), job_id),
            )
        return self.get_job(job_id)

    def set_job_stage(self, job_id: str, stage_name: str, stage_status: str) -> dict:
        job = self.get_job(job_id)
        stages = job["stages"]
        t = now()
        for s in stages:
            if s["name"] == stage_name:
                s["status"] = stage_status
                if stage_status == "running" and not s.get("started_at"):
                    s["started_at"] = t
                if stage_status in ("completed", "failed"):
                    s["finished_at"] = t
        overall = "running"
        if all(s["status"] == "completed" for s in stages):
            overall = "completed"
        elif any(s["status"] == "failed" for s in stages):
            overall = "failed"
        return self.update_job(job_id, status=overall, stage=stage_name, stages=stages)

    # ── Search ────────────────────────────────────────────────

    def clear_repo_docs(self, repo_id: str) -> None:
        with self.conn() as c:
            # FTS content table: delete rows (triggers keep fts in sync)
            c.execute("DELETE FROM search_docs WHERE repo_id = ?", (repo_id,))

    def add_search_doc(
        self,
        *,
        project_id: str,
        repo_id: str,
        repo_name: str,
        doc_type: str,
        title: str,
        body: str,
        sha: Optional[str] = None,
        path: Optional[str] = None,
    ) -> str:
        did = new_id()
        with self.conn() as c:
            c.execute(
                """
                INSERT INTO search_docs
                (id, project_id, repo_id, repo_name, doc_type, title, body, sha, path, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (did, project_id, repo_id, repo_name, doc_type, title, body, sha, path, now()),
            )
        return did

    def resolve_scope_source_ids(
        self, scope_type: Optional[str], scope_id: Optional[str]
    ) -> tuple[str, str, list[str], list[dict]]:
        """Validate scope; return (type, id, parvaana_source_ids, repo_rows)."""
        q_type = (scope_type or "").strip().lower()
        if not q_type or not scope_id:
            raise CatalogError(
                "scope required: { type: 'project'|'repo', id }. "
                "Install-wide / root search is not supported.",
                400,
            )
        if q_type in ("all", "root", "install", "workspaces"):
            raise CatalogError(
                "scope type 'all'/root is not supported; use project or repo",
                400,
            )
        if q_type not in ("project", "repo"):
            raise CatalogError("scope.type must be 'project' or 'repo'", 400)

        if q_type == "project":
            self.get_project(scope_id)
            repos = self.list_repos(scope_id)
        else:
            repo = self.get_repo(scope_id)
            repos = [repo]
        source_ids = [
            r["parvaana_source_id"]
            for r in repos
            if r.get("parvaana_source_id")
        ]
        return q_type, scope_id, source_ids, repos

    def search(
        self,
        q: str,
        scope_type: Optional[str],
        scope_id: Optional[str],
        limit: int = 20,
        *,
        prefer_parvaana: bool = True,
    ) -> dict:
        """Search with hard scope lock: only project|repo. Prefer Parvaana searcher."""
        q = (q or "").strip()
        if not q:
            raise CatalogError("query q required")
        st, sid, source_ids, repos = self.resolve_scope_source_ids(scope_type, scope_id)
        limit = max(1, min(int(limit or 20), 100))

        # Prefer Parvaana content brain when sources are mapped and searcher is up
        if prefer_parvaana and source_ids:
            try:
                from parvaana_client import ParvaanaClient

                client = ParvaanaClient()
                h = client.health()
                if h.get("ok"):
                    raw = client.search(q, source_ids=source_ids, limit=limit)
                    # map source_id → repo for product fields
                    by_src = {
                        r["parvaana_source_id"]: r
                        for r in repos
                        if r.get("parvaana_source_id")
                    }
                    results = []
                    for hit in raw.get("results") or []:
                        repo = by_src.get(hit.get("source_id") or "")
                        results.append(
                            {
                                "id": hit.get("id"),
                                "project_id": repo["project_id"] if repo else (
                                    sid if st == "project" else None
                                ),
                                "repo_id": repo["id"] if repo else None,
                                "repo_name": repo["name"] if repo else None,
                                "doc_type": "parvaana_document",
                                "title": hit.get("title"),
                                "sha": None,
                                "path": hit.get("path") or hit.get("external_id"),
                                "snippet": (hit.get("snippet") or "")[:500],
                            }
                        )
                    return {
                        "q": q,
                        "scope": {"type": st, "id": sid},
                        "count": len(results),
                        "results": results,
                        "backend": "parvaana_searcher",
                    }
            except Exception:
                pass  # fall through to local FTS

        return self._search_fts(q, st, sid, limit)

    def _search_fts(self, q: str, st: str, scope_id: str, limit: int) -> dict:
        """Demoted local FTS fallback (path/commit cards) — not content SoR."""
        fts_q = _fts_query(q)
        with self.conn() as c:
            if st == "project":
                if not c.execute("SELECT 1 FROM projects WHERE id = ?", (scope_id,)).fetchone():
                    raise CatalogError("project not found", 404)
                rows = c.execute(
                    """
                    SELECT d.id, d.project_id, d.repo_id, d.repo_name, d.doc_type,
                           d.title, d.sha, d.path, d.body,
                           snippet(search_fts, 1, '<b>', '</b>', '…', 24) AS snippet
                    FROM search_fts
                    JOIN search_docs d ON d.rowid = search_fts.rowid
                    WHERE search_fts MATCH ? AND d.project_id = ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (fts_q, scope_id, limit),
                ).fetchall()
            else:
                if not c.execute("SELECT 1 FROM repositories WHERE id = ?", (scope_id,)).fetchone():
                    raise CatalogError("repository not found", 404)
                rows = c.execute(
                    """
                    SELECT d.id, d.project_id, d.repo_id, d.repo_name, d.doc_type,
                           d.title, d.sha, d.path, d.body,
                           snippet(search_fts, 1, '<b>', '</b>', '…', 24) AS snippet
                    FROM search_fts
                    JOIN search_docs d ON d.rowid = search_fts.rowid
                    WHERE search_fts MATCH ? AND d.repo_id = ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (fts_q, scope_id, limit),
                ).fetchall()

        results = []
        for r in rows:
            d = dict(r)
            body = d.pop("body", "") or ""
            results.append(
                {
                    "id": d["id"],
                    "project_id": d["project_id"],
                    "repo_id": d["repo_id"],
                    "repo_name": d["repo_name"],
                    "doc_type": d["doc_type"],
                    "title": d["title"],
                    "sha": d["sha"],
                    "path": d["path"],
                    "snippet": d.get("snippet") or body[:200],
                }
            )
        return {
            "q": q,
            "scope": {"type": st, "id": scope_id},
            "count": len(results),
            "results": results,
            "backend": "fts_local_fallback",
        }

    def apply_parvaana_source_map(self, name_to_source_id: dict[str, str]) -> int:
        """Set parvaana_source_id on repos matching names. Returns update count."""
        n = 0
        with self.conn() as c:
            for name, sid in (name_to_source_id or {}).items():
                cur = c.execute(
                    "UPDATE repositories SET parvaana_source_id = ? WHERE name = ?",
                    (sid, name),
                )
                n += cur.rowcount
        return n


def is_test_project_name(name: str) -> bool:
    n = (name or "").strip()
    if not n:
        return False
    low = n.lower()
    if n.startswith("Skeptic") or "Skeptic" in n:
        return True
    if low.startswith("test_") or low.startswith("tmp_"):
        return True
    if low.startswith("seed-ui"):
        return True
    return False


def repo_name_from_url(url: str) -> str:
    u = url.rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    base = u.split("/")[-1] if "/" in u else u
    # local path
    base = Path(base).name
    return base or "repo"


def _fts_query(q: str) -> str:
    # Build a safe FTS5 query: quote tokens, join with AND for multi-word
    tokens = re.findall(r"[A-Za-z0-9_./-]+", q)
    if not tokens:
        # fallback: strip quotes
        safe = q.replace('"', " ").strip()
        return f'"{safe}"' if safe else '""'
    parts = []
    for t in tokens:
        if len(t) < 1:
            continue
        parts.append(f'"{t}"')
    return " AND ".join(parts) if parts else '""'
