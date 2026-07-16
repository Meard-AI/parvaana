"""Product v1 control plane API — catalog, jobs, search, graph, AI chat, settings."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ai_service import AIError, AIService
from catalog import Catalog, CatalogError
from jobs import JobRunner
from settings_store import SettingsStore

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("product")

ROOT = Path(os.environ.get("PRODUCT_ROOT", Path(__file__).resolve().parent.parent))
DATA = Path(os.environ.get("PRODUCT_DATA", ROOT / "data"))
REPOS = Path(os.environ.get("PRODUCT_REPOS", ROOT / "repos"))
EXPORT = Path(
    os.environ.get(
        "PRODUCT_EXPORT",
        "/home/library/code/gitatlas-export",
    )
)
GITATLAS_BIN = Path(os.environ.get("GITATLAS_BIN", "/home/library/gitatlas/gitatlas"))
GITATLAS_CWD = Path(os.environ.get("GITATLAS_CWD", "/home/library/gitatlas"))
HUGEGRAPH_URL = os.environ.get(
    "GITATLAS_HUGEGRAPH_URL",
    "http://127.0.0.1:18080/graphs/hugegraph",
)
DB_PATH = DATA / "catalog.db"
FRONTEND = ROOT / "frontend"

catalog = Catalog(DB_PATH)
settings = SettingsStore(DB_PATH)
runner = JobRunner(
    catalog,
    repos_root=REPOS,
    export_root=EXPORT,
    gitatlas_bin=GITATLAS_BIN if GITATLAS_BIN.exists() else None,
    gitatlas_cwd=GITATLAS_CWD,
    hugegraph_url=HUGEGRAPH_URL,
)
ai = AIService(catalog, settings, runner=runner)

# Startup cleanup: never ship Skeptic* junk
try:
    purged = catalog.purge_test_projects()
    if purged:
        log.info("purged test projects: %s", purged)
except Exception as e:
    log.warning("purge on startup failed: %s", e)

app = FastAPI(title="Product v1", version="1.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(CatalogError)
async def catalog_error_handler(_req: Request, exc: CatalogError):
    return JSONResponse(status_code=exc.status, content={"error": str(exc)})


@app.exception_handler(AIError)
async def ai_error_handler(_req: Request, exc: AIError):
    return JSONResponse(
        status_code=exc.status,
        content={"error": str(exc), "code": exc.code},
    )


# ── Models ────────────────────────────────────────────────────


class ProjectCreate(BaseModel):
    name: str


class ProjectPatch(BaseModel):
    name: Optional[str] = None


class RepoCreate(BaseModel):
    url: str
    index: bool = True
    name: Optional[str] = None


class SearchBody(BaseModel):
    q: str
    scope: Optional[dict[str, Any]] = None
    limit: int = 20


class ChatBody(BaseModel):
    message: str
    scope: Optional[dict[str, Any]] = None
    history: Optional[list[dict[str, Any]]] = None
    model: Optional[str] = None  # optional per-message model override


class SettingsPatch(BaseModel):
    ai_provider: Optional[str] = None
    api_provider_id: Optional[str] = None
    parvaana_base_url: Optional[str] = None
    openai_base_url: Optional[str] = None
    openai_api_key: Optional[str] = None
    openai_model: Optional[str] = None
    system_prompt_extra: Optional[str] = None


# ── Health ────────────────────────────────────────────────────


@app.get("/health")
def health():
    projects = catalog.list_projects()
    ai_st = ai.status()
    return {
        "ok": True,
        "service": "product-v1",
        "projects": len(projects),
        "default_project_id": catalog.preferred_default_project_id(),
        "ai_configured": ai_st.get("configured"),
        "ai_provider": ai_st.get("provider"),
        "gitatlas": bool(runner.gitatlas_bin and runner.gitatlas_bin.exists()),
        # Honesty labels (GAP-ANALYSIS): product search is local FTS; graph product path is git-derived
        "search_backend": _search_backend_label(),
        "graph_backend": "gremlin",
        "llm_backend": ai_st.get("provider") or "none",
        "llm_model": ai_st.get("model"),
    }


def _search_backend_label() -> str:
    try:
        from parvaana_client import ParvaanaClient

        h = ParvaanaClient().health()
        if h.get("ok"):
            return "parvaana_searcher"
    except Exception:
        pass
    return "fts_local_fallback"


# ── Catalog ───────────────────────────────────────────────────


@app.get("/projects")
def list_projects():
    return {
        "projects": catalog.list_projects(hide_test=True),
        "default_project_id": catalog.preferred_default_project_id(),
    }


@app.post("/projects", status_code=201)
def create_project(body: ProjectCreate):
    return catalog.create_project(body.name)


@app.get("/projects/{project_id}")
def get_project(project_id: str):
    return catalog.get_project(project_id)


@app.patch("/projects/{project_id}")
def patch_project(project_id: str, body: ProjectPatch):
    return catalog.patch_project(project_id, name=body.name)


@app.delete("/projects/{project_id}")
def delete_project(project_id: str):
    catalog.delete_project(project_id)
    return {"ok": True}


@app.post("/admin/purge-test-projects")
def purge_test_projects():
    names = catalog.purge_test_projects()
    return {"deleted": names, "count": len(names)}


@app.get("/projects/{project_id}/overview")
def project_overview(project_id: str):
    return catalog.project_overview(project_id)


@app.get("/projects/{project_id}/stats")
def project_stats(project_id: str):
    return catalog.project_stats(project_id)


@app.get("/projects/{project_id}/repos")
def list_repos(project_id: str):
    return {"repos": catalog.list_repos(project_id)}


@app.post("/projects/{project_id}/repos", status_code=201)
def add_repo(project_id: str, body: RepoCreate):
    repo = catalog.add_repo(project_id, body.url, name=body.name)
    job = None
    if body.index:
        job = runner.enqueue_add_index(project_id, repo["id"])
    return {"repo": repo, "job": job}


@app.delete("/projects/{project_id}/repos/{repo_id}")
def delete_repo(project_id: str, repo_id: str):
    catalog.delete_repo(project_id, repo_id)
    return {"ok": True}


@app.post("/repos/{repo_id}/sync")
def sync_repo(repo_id: str):
    job = runner.enqueue_sync(repo_id)
    return {"job": job}


@app.post("/repos/{repo_id}/reindex")
def reindex_repo(repo_id: str):
    job = runner.enqueue_reindex(repo_id)
    return {"job": job}


@app.get("/repos/{repo_id}")
def get_repo(repo_id: str):
    return catalog.get_repo(repo_id)


# ── Jobs ──────────────────────────────────────────────────────


@app.get("/jobs")
def list_jobs(limit: int = 50):
    return {"jobs": catalog.list_jobs(limit=limit)}


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    return catalog.get_job(job_id)


# ── Search (scope-locked) ─────────────────────────────────────


@app.post("/search")
def search(body: SearchBody):
    scope = body.scope or {}
    return catalog.search(body.q, scope.get("type"), scope.get("id"), limit=body.limit)


# ── Graph ─────────────────────────────────────────────────────


@app.get("/repos/{repo_id}/graph/stats")
def graph_stats(repo_id: str):
    try:
        return runner.graph_stats(repo_id)
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@app.get("/repos/{repo_id}/commits/{sha}/blast-radius")
def blast_radius(repo_id: str, sha: str):
    try:
        return runner.blast_radius(repo_id, sha)
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@app.get("/repos/{repo_id}/graph/commits-for-path")
def commits_for_path(repo_id: str, path: str, limit: int = 20):
    """GitAtlas/Gremlin: commits that MODIFIED this path."""
    try:
        return runner.commits_for_path(repo_id, path, limit=limit)
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@app.get("/graph/health")
def graph_health():
    from gremlin_client import GremlinClient

    return GremlinClient().health()


@app.get("/parvaana/health")
def parvaana_health():
    from parvaana_client import ParvaanaClient

    return ParvaanaClient().health()


@app.post("/admin/link-parvaana-sources")
def link_parvaana_sources():
    """Map product repo names → Parvaana source IDs from data/parvaana-source-map.json."""
    from parvaana_client import ParvaanaClient

    client = ParvaanaClient()
    m = client.load_source_map()
    n = catalog.apply_parvaana_source_map(m)
    # optional: trigger sync for mapped sources
    syncs = {name: client.trigger_sync(sid) for name, sid in m.items()}
    return {"linked": n, "map": m, "syncs": syncs}


@app.get("/repos/{repo_id}/what-changed")
def what_changed(repo_id: str, since: str = "HEAD~10"):
    try:
        return runner.what_changed(repo_id, since=since)
    except Exception as e:
        raise HTTPException(400, str(e)) from e


# ── AI ────────────────────────────────────────────────────────


@app.get("/ai/status")
def ai_status():
    return ai.status()


@app.get("/ai/providers")
def ai_providers():
    """Hermes-style provider catalog for Settings."""
    from providers_catalog import list_providers

    return {"providers": list_providers()}


@app.get("/ai/models")
def ai_models(fetch: bool = True):
    """Models available for the currently configured provider (+ optional remote /models)."""
    return ai.list_available_models(fetch_remote=bool(fetch))


@app.post("/ai/test")
def ai_test():
    return ai.test_connection()


@app.post("/ai/chat")
def ai_chat(body: ChatBody):
    scope = body.scope or {}
    return ai.chat(
        body.message,
        scope.get("type"),
        scope.get("id"),
        history=body.history,
        model_override=body.model,
    )


# ── Settings ──────────────────────────────────────────────────


@app.get("/settings")
def get_settings():
    return settings.public_view()


@app.patch("/settings")
def patch_settings(body: SettingsPatch):
    return settings.update(body.model_dump(exclude_unset=True))


# ── Frontend ──────────────────────────────────────────────────

if FRONTEND.is_dir():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND / "assets")), name="assets")


@app.get("/")
def index():
    index_path = FRONTEND / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return {"message": "product-v1", "ui": "missing"}


@app.get("/{path:path}")
def spa_fallback(path: str):
    blocked = (
        "projects",
        "repos",
        "jobs",
        "search",
        "health",
        "assets",
        "ai",
        "settings",
        "admin",
    )
    if path.startswith(blocked):
        raise HTTPException(404)
    index_path = FRONTEND / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    raise HTTPException(404)


def create_app() -> FastAPI:
    return app


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PRODUCT_PORT", "3847"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
