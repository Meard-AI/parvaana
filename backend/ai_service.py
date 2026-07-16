"""AI chat grounded in project/repo-scoped index + optional graph facts.

Providers:
  - parvaana: host → Parvaana AI container /prompt (NVIDIA NIM on this install)
  - openai_compatible: user-configured base URL + API key
  - auto: prefer parvaana if healthy, else openai if key set
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from typing import Any, Optional

import httpx

from catalog import Catalog, CatalogError
from jobs import JobRunner
from settings_store import SettingsStore

log = logging.getLogger("product.ai")


class AIError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "ai_error"):
        super().__init__(message)
        self.status = status
        self.code = code


def discover_parvaana_ai_url() -> Optional[str]:
    """Resolve parvaana-ai container IP on omni_parvaana-network (host-accessible)."""
    try:
        out = subprocess.check_output(
            [
                "docker",
                "inspect",
                "parvaana-ai",
                "--format",
                "{{range $k,$v := .NetworkSettings.Networks}}{{$k}}={{$v.IPAddress}} {{end}}",
            ],
            text=True,
            timeout=5,
        ).strip()
    except Exception as e:
        log.debug("docker inspect failed: %s", e)
        return None
    # Prefer omni_parvaana-network
    preferred = None
    for part in out.split():
        if "=" not in part:
            continue
        net, ip = part.split("=", 1)
        if not ip:
            continue
        if "parvaana" in net:
            preferred = f"http://{ip}:3003"
            break
        if not preferred:
            preferred = f"http://{ip}:3003"
    return preferred


class AIService:
    def __init__(
        self,
        catalog: Catalog,
        settings: SettingsStore,
        runner: Optional[JobRunner] = None,
    ):
        self.catalog = catalog
        self.settings = settings
        self.runner = runner

    def status(self) -> dict[str, Any]:
        raw = self.settings.get_all_raw()
        provider_pref = (raw.get("ai_provider") or "auto").lower()
        resolved = self._resolve_provider(prefer=provider_pref, probe=True)
        models_info = self.list_available_models(fetch_remote=False)
        return {
            "configured": bool(resolved.get("ok")),
            "provider": resolved.get("provider"),
            "provider_label": resolved.get("provider_label")
            or resolved.get("provider"),
            "api_provider_id": raw.get("api_provider_id") or "auto",
            "model": resolved.get("model"),
            "detail": resolved.get("detail"),
            "source": resolved.get("source"),
            "base_url": resolved.get("base_url_public"),
            "setup_hint": resolved.get("setup_hint"),
            "active_display": self._active_display(resolved, raw),
            "models": models_info.get("models") or [],
            "settings": self.settings.public_view(),
        }

    def _active_display(self, resolved: dict, raw: dict) -> str:
        """Short string for the AI chip — always the *resolved* backend, not the dropdown default."""
        if not resolved.get("ok"):
            return "AI not configured"
        prov = resolved.get("provider_label") or resolved.get("provider") or "?"
        model = resolved.get("model") or "?"
        # Shorten long model ids for the chip
        short = model.split("/")[-1] if "/" in model else model
        if len(short) > 28:
            short = short[:26] + "…"
        return f"AI · {prov} · {short}"

    def list_available_models(self, *, fetch_remote: bool = True) -> dict[str, Any]:
        """Models for Settings + Ask bar, based on selected catalog provider + optional /models."""
        from providers_catalog import get_provider, models_for_provider, list_providers

        raw = self.settings.get_all_raw()
        pid = (raw.get("api_provider_id") or "auto").strip().lower()
        kind = (raw.get("ai_provider") or "auto").lower()
        catalog = get_provider(pid) or get_provider("auto")
        models: list[str] = []
        source = "catalog"

        # Resolved active backend influences default list when Auto
        resolved = self._resolve_provider(prefer=kind, probe=False)
        active = resolved.get("provider") if resolved.get("ok") else None

        if pid == "parvaana" or (pid == "auto" and active == "parvaana") or kind == "parvaana":
            models = models_for_provider("parvaana") or [
                resolved.get("model") or "meta/llama-3.1-8b-instruct"
            ]
            # Prefer live model from health
            if resolved.get("ok") and resolved.get("provider") == "parvaana" and resolved.get("model"):
                m0 = resolved["model"]
                if m0 not in models:
                    models = [m0] + models
            source = "parvaana"
        elif pid not in ("", "auto", "parvaana") or kind == "openai_compatible":
            models = list(models_for_provider(pid) if pid not in ("", "auto") else [])
            saved = (raw.get("openai_model") or "").strip()
            if saved and saved not in models:
                models = [saved] + models
            # Live list from provider when key present
            if fetch_remote and (raw.get("openai_api_key") or "").strip():
                remote = self._fetch_remote_models(
                    (raw.get("openai_base_url") or "").rstrip("/"),
                    (raw.get("openai_api_key") or "").strip(),
                )
                if remote:
                    source = "remote"
                    # merge: remote first, keep catalog extras
                    merged = list(remote)
                    for m in models:
                        if m not in merged:
                            merged.append(m)
                    models = merged
            source = source if models else "catalog"
        else:
            # auto, no clear side — show both hints
            models = []
            if resolved.get("ok") and resolved.get("model"):
                models.append(resolved["model"])
            models.extend(models_for_provider("parvaana"))
            models.extend(models_for_provider("minimax")[:3])
            # dedupe
            seen = set()
            models = [m for m in models if not (m in seen or seen.add(m))]
            source = "mixed"

        return {
            "api_provider_id": pid,
            "active_provider": active,
            "active_model": resolved.get("model") if resolved.get("ok") else None,
            "models": models[:80],
            "source": source,
            "catalog_provider": catalog,
            "providers": list_providers(),
        }

    def _fetch_remote_models(self, base_url: str, api_key: str) -> list[str]:
        if not base_url or not api_key:
            return []
        url = base_url if base_url.endswith("/models") else f"{base_url.rstrip('/')}/models"
        try:
            with httpx.Client(timeout=8.0) as client:
                r = client.get(
                    url,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                if r.status_code >= 400:
                    return []
                data = r.json()
                items = data.get("data") or data.get("models") or []
                out = []
                for it in items:
                    if isinstance(it, str):
                        out.append(it)
                    elif isinstance(it, dict):
                        mid = it.get("id") or it.get("name")
                        if mid:
                            out.append(str(mid))
                return out[:80]
        except Exception as e:
            log.debug("fetch models failed: %s", e)
            return []

    def _resolve_provider(self, prefer: str = "auto", probe: bool = False) -> dict[str, Any]:
        raw = self.settings.get_all_raw()
        prefer = (prefer or "auto").lower()
        api_pid = (raw.get("api_provider_id") or "auto").strip().lower()
        if prefer == "disabled":
            return {
                "ok": False,
                "provider": "disabled",
                "detail": "AI is disabled in Settings.",
                "setup_hint": "Open Settings and pick a provider + API key.",
            }

        def try_parvaana() -> Optional[dict]:
            base = (raw.get("parvaana_base_url") or "").strip() or discover_parvaana_ai_url()
            if not base:
                return None
            base = base.rstrip("/")
            if not probe:
                return {
                    "ok": True,
                    "provider": "parvaana",
                    "provider_label": "Parvaana",
                    "model": "meta/llama-3.1-8b-instruct",
                    "detail": "Parvaana AI (NIM)",
                    "source": "parvaana",
                    "base_url": base,
                    "base_url_public": base,
                    "mode": "parvaana_prompt",
                }
            try:
                with httpx.Client(timeout=5.0) as client:
                    r = client.get(f"{base}/health")
                    if r.status_code != 200:
                        return None
                    h = r.json()
                    if not h.get("llm_health", True):
                        return {
                            "ok": False,
                            "provider": "parvaana",
                            "provider_label": "Parvaana",
                            "detail": "Parvaana AI reachable but llm_health=false",
                            "setup_hint": "Check NVIDIA NIM / model provider in Parvaana admin.",
                            "base_url_public": base,
                        }
                    return {
                        "ok": True,
                        "provider": "parvaana",
                        "provider_label": "Parvaana",
                        "model": h.get("llm_model") or "meta/llama-3.1-8b-instruct",
                        "detail": f"Parvaana AI healthy ({h.get('llm_provider')})",
                        "source": "parvaana",
                        "base_url": base,
                        "base_url_public": base,
                        "mode": "parvaana_prompt",
                    }
            except Exception as e:
                log.info("parvaana probe failed: %s", e)
                return None

        def try_openai() -> Optional[dict]:
            key = (raw.get("openai_api_key") or "").strip()
            base = (raw.get("openai_base_url") or "https://api.openai.com/v1").rstrip("/")
            # Local ollama/lmstudio often need no key
            if not key and api_pid not in ("ollama", "lmstudio"):
                return None
            if not key:
                key = "ollama"
            model = (
                getattr(self, "_model_override", None)
                or raw.get("openai_model")
                or "gpt-4o-mini"
            )
            from providers_catalog import get_provider

            cat = get_provider(api_pid)
            label = (cat or {}).get("name") or "API"
            return {
                "ok": True,
                "provider": "openai_compatible",
                "provider_label": label,
                "model": model,
                "detail": f"{label} (OpenAI-compatible)",
                "source": "settings",
                "base_url": base,
                "base_url_public": base,
                "api_key": key,
                "mode": "openai_chat",
            }

        order = []
        if prefer == "parvaana" or api_pid == "parvaana":
            order = [try_parvaana, try_openai]
        elif prefer == "openai_compatible" or (
            api_pid not in ("", "auto", "parvaana") and (raw.get("openai_api_key") or "").strip()
        ):
            # Explicit external provider + key → prefer that over local Parvaana
            order = [try_openai, try_parvaana]
        else:  # auto
            # If user configured an external catalog provider with a key, prefer it
            if api_pid not in ("", "auto", "parvaana") and (raw.get("openai_api_key") or "").strip():
                order = [try_openai, try_parvaana]
            else:
                order = [try_parvaana, try_openai]

        last_fail = None
        for fn in order:
            res = fn()
            if res and res.get("ok"):
                return res
            if res:
                last_fail = res
        if last_fail:
            return last_fail
        return {
            "ok": False,
            "provider": prefer,
            "detail": "No AI provider available",
            "setup_hint": (
                "Pick a provider in Settings, paste an API key (if required), and Save. "
                "Or use Parvaana AI on this host (Auto / Parvaana)."
            ),
        }

    def retrieve_context(
        self,
        q: str,
        scope_type: str,
        scope_id: str,
        *,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        try:
            # Prefer Parvaana searcher (content SoR); FTS only if search falls back
            res = self.catalog.search(
                q, scope_type, scope_id, limit=limit, prefer_parvaana=True
            )
            hits = res.get("results") or []
            # annotate backend for prompt quality
            for h in hits:
                h["_backend"] = res.get("backend")
            return hits
        except CatalogError:
            return self._fallback_docs(scope_type, scope_id, limit=limit)

    def _fallback_docs(self, scope_type: str, scope_id: str, limit: int = 8) -> list[dict]:
        with self.catalog.conn() as c:
            if scope_type == "project":
                rows = c.execute(
                    """
                    SELECT id, project_id, repo_id, repo_name, doc_type, title, sha, path, body
                    FROM search_docs WHERE project_id = ?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (scope_id, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    """
                    SELECT id, project_id, repo_id, repo_name, doc_type, title, sha, path, body
                    FROM search_docs WHERE repo_id = ?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (scope_id, limit),
                ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            out.append(
                {
                    "id": d["id"],
                    "project_id": d["project_id"],
                    "repo_id": d["repo_id"],
                    "repo_name": d["repo_name"],
                    "doc_type": d["doc_type"],
                    "title": d["title"],
                    "sha": d["sha"],
                    "path": d["path"],
                    "snippet": (d.get("body") or "")[:240],
                }
            )
        return out

    def graph_context(self, scope_type: str, scope_id: str) -> dict[str, Any]:
        if not self.runner:
            return {}
        if scope_type == "repo":
            try:
                stats = self.runner.graph_stats(scope_id)
                wc = self.runner.what_changed(scope_id, since="HEAD~8")
                commits = (wc.get("commits") or [])[:5]
                return {"graph_stats": stats, "recent_commits": commits}
            except Exception as e:
                return {"graph_error": str(e)}
        # project: always list member repos + stats when available
        try:
            repos = self.catalog.list_repos(scope_id)
            stats = self.catalog.project_stats(scope_id)
            agg = []
            for r in repos:
                item = {
                    "repo": r["name"],
                    "repo_id": r["id"],
                    "commits": r.get("commit_count"),
                    "files": r.get("file_count"),
                    "head": r.get("sync_to_sha"),
                    "url": r.get("url"),
                }
                if self.runner:
                    try:
                        st = self.runner.graph_stats(r["id"])
                        item["commits"] = st.get("commits", item["commits"])
                        item["files"] = st.get("files", item["files"])
                        item["head"] = st.get("sync_to_sha", item["head"])
                    except Exception:
                        pass
                agg.append(item)
            return {"project_stats": stats, "repos": agg}
        except Exception as e:
            return {"graph_error": str(e)}

    def chat(
        self,
        message: str,
        scope_type: str,
        scope_id: str,
        *,
        history: Optional[list[dict]] = None,
        model_override: Optional[str] = None,
    ) -> dict[str, Any]:
        message = (message or "").strip()
        if not message:
            raise AIError("message required")
        if scope_type not in ("project", "repo") or not scope_id:
            raise AIError(
                "scope required: { type: 'project'|'repo', id }. Install-wide AI is not supported.",
                400,
                "scope_required",
            )
        self._model_override = (model_override or "").strip() or None

        # Validate scope exists
        if scope_type == "project":
            proj = self.catalog.get_project(scope_id)
            scope_label = f"project «{proj['name']}»"
        else:
            repo = self.catalog.get_repo(scope_id)
            scope_label = f"repository «{repo['name']}»"

        from pathlib import Path as _P
        from content_tools import (
            classify_intent,
            build_content_dossier,
            format_compose_deepwiki_answer,
            dossier_as_prompt_blocks,
            COMPOSE_TOPIC,
            HELM_K8S_TOPIC,
            looks_hypothetical,
        )
        from graph_tools import GraphTools, looks_temporal, extract_path_candidates

        intent = classify_intent(message)
        is_hypothetical = intent == "hypothetical" or looks_hypothetical(message)
        # Resolve scoped repos once (content + graph tools)
        repos_for_tools: list[dict] = []
        local = None
        repo_name = None
        if scope_type == "repo":
            r0 = self.catalog.get_repo(scope_id)
            repo_name = r0.get("name")
            if r0.get("local_path"):
                p = _P(r0["local_path"])
                local = p if p.exists() else None
            repos_for_tools = [{"name": repo_name, "local_path": local}]
        else:
            for r0 in self.catalog.list_repos(scope_id):
                lp = r0.get("local_path")
                p = _P(lp) if lp else None
                repos_for_tools.append(
                    {
                        "name": r0.get("name"),
                        "local_path": p if (p and p.exists()) else None,
                    }
                )

            def _repo_rank(r: dict) -> int:
                n = (r.get("name") or "").lower()
                if n == "hugegraph":
                    return 0
                if "ai" in n:
                    return 1
                if "toolchain" in n:
                    return 2
                return 5

            repos_for_tools.sort(key=_repo_rank)
            if repos_for_tools:
                local = repos_for_tools[0].get("local_path")
                repo_name = repos_for_tools[0].get("name")

        # Content brain: search + file dossier (DeepWiki / hypothetical path)
        search_limit = (
            18
            if is_hypothetical
            or COMPOSE_TOPIC.search(message)
            or HELM_K8S_TOPIC.search(message)
            or intent == "content"
            else 8
        )
        hits = self.retrieve_context(message, scope_type, scope_id, limit=search_limit)
        # For theoretical deploy questions, also search real anchors (compose, helm, k8s)
        if is_hypothetical or HELM_K8S_TOPIC.search(message):
            extra_q = []
            if HELM_K8S_TOPIC.search(message) or is_hypothetical:
                extra_q.extend(["helm Chart.yaml", "docker-compose", "values.yaml"])
            for eq in extra_q:
                more = self.retrieve_context(eq, scope_type, scope_id, limit=8)
                # merge unique by path
                seen_p = {(h.get("path"), h.get("repo_name")) for h in hits}
                for h in more:
                    key = (h.get("path"), h.get("repo_name"))
                    if key not in seen_p:
                        hits.append(h)
                        seen_p.add(key)
                if len(hits) >= 24:
                    break
        if not hits:
            tokens = re.findall(r"[A-Za-z0-9_./-]{3,}", message)
            for t in tokens[:4]:
                hits = self.retrieve_context(t, scope_type, scope_id, limit=10)
                if hits:
                    break
        if not hits:
            hits = self._fallback_docs(scope_type, scope_id, limit=6)

        content_dossier = None
        if (
            intent in ("content", "hybrid", "hypothetical")
            or COMPOSE_TOPIC.search(message)
            or HELM_K8S_TOPIC.search(message)
            or is_hypothetical
        ):
            try:
                content_dossier = build_content_dossier(
                    repos_for_tools,
                    message,
                    max_files=16 if is_hypothetical else 12,
                    max_chars_each=4500,
                )
            except Exception as e:
                log.warning("content dossier failed: %s", e)
                content_dossier = {"ok": False, "error": str(e)}

        gctx = self.graph_context(scope_type, scope_id)

        # Graph tools: temporal/hybrid (or path mentions on hybrid)
        graph_tools_bundle = None
        if intent in ("temporal", "hybrid") or (
            intent == "content" and looks_temporal(message)
        ):
            try:
                if looks_temporal(message) or extract_path_candidates(message):
                    gt = GraphTools()
                    graph_tools_bundle = gt.gather_for_question(
                        message,
                        local_repo=local,
                        repo_name=repo_name,
                        repos=repos_for_tools,
                    )
            except Exception as e:
                graph_tools_bundle = {"error": str(e)}
        # Still attach lightweight graph health for content answers
        if graph_tools_bundle is None:
            try:
                graph_tools_bundle = {
                    "temporal": False,
                    "paths": extract_path_candidates(message),
                    "tools": [],
                    "graph_health": GraphTools().graph_health(),
                    "intent": intent,
                }
            except Exception:
                graph_tools_bundle = {"temporal": False, "tools": [], "intent": intent}
        else:
            graph_tools_bundle["intent"] = intent

        # ── Deterministic TEMPORAL only (git facts). Content/hypothetical always use the model. ──
        # Compose/dossier is evidence for the LLM — not a hard-coded final answer.
        if intent in ("temporal", "hybrid"):
            direct = _direct_answer_from_graph_tools(message, graph_tools_bundle)
            if direct and intent == "temporal":
                return {
                    "answer": direct["answer"],
                    "scope": {"type": scope_type, "id": scope_id, "label": scope_label},
                    "citations": direct.get("citations") or [],
                    "context_count": len(hits),
                    "provider": "graph_tools",
                    "model": "gremlin+templates",
                    "graph": gctx,
                    "graph_tools": graph_tools_bundle,
                    "mode": "deterministic_graph",
                    "intent": intent,
                }
            # hybrid: inject card later; model writes the narrative

        # Optional structured compose summary as *evidence*, not the user-facing answer
        compose_evidence = None
        if content_dossier and content_dossier.get("ok") and COMPOSE_TOPIC.search(message):
            try:
                compose_evidence = format_compose_deepwiki_answer(content_dossier)
            except Exception:
                compose_evidence = None

        # Web search for theoretical / design / explicit online questions
        web_bundle = None
        try:
            from web_search import needs_web, search_web, format_web_for_prompt

            raw_settings = self.settings.get_all_raw()
            web_enabled = (raw_settings.get("enable_web_search") or "1") not in (
                "0",
                "false",
                "no",
            )
            if web_enabled and needs_web(message, intent=intent):
                # Prefer a search query that combines user Q + project anchor
                wq = message
                if len(wq) > 180:
                    wq = wq[:180]
                # Add project name for context without drowning the query
                try:
                    if scope_type == "project":
                        wq = f"{proj['name'] if scope_type=='project' else ''} {wq}".strip()
                except Exception:
                    pass
                web_bundle = search_web(wq, max_results=6)
        except Exception as e:
            log.warning("web search path failed: %s", e)
            web_bundle = {"ok": False, "results": [], "error": str(e)}

        citations = []
        ctx_blocks = []
        for i, h in enumerate(hits, 1):
            cite = {
                "n": i,
                "title": h.get("title"),
                "repo_name": h.get("repo_name"),
                "path": h.get("path"),
                "sha": h.get("sha"),
                "doc_type": h.get("doc_type"),
                "repo_id": h.get("repo_id"),
            }
            citations.append(cite)
            piece = f"[{i}] {h.get('title')}\n"
            if h.get("path"):
                piece += f"path: {h['path']}\n"
            if h.get("sha"):
                piece += f"commit: {h['sha']}\n"
            piece += f"repo: {h.get('repo_name')}\n"
            piece += f"{h.get('snippet') or ''}\n"
            ctx_blocks.append(piece)

        # Inject full file dossier (primary evidence for content/hybrid/hypothetical)
        golden_cards_txt = []
        n0 = len(citations)
        if content_dossier and content_dossier.get("ok"):
            blocks, dcites = dossier_as_prompt_blocks(content_dossier, start_n=n0 + 1)
            for b, c in zip(blocks, dcites):
                citations.append(c)
                ctx_blocks.append(b)
            n0 = len(citations)

        if compose_evidence and compose_evidence.get("answer"):
            n0 += 1
            ctx_blocks.append(
                f"[{n0}] STRUCTURED EXTRACT (auto-parsed from compose files — verify against FILE DOSSIER)\n"
                f"{compose_evidence['answer'][:3500]}\n"
            )
            citations.append(
                {
                    "n": n0,
                    "title": "structured compose extract",
                    "path": "(derived)",
                    "repo_name": "workspace",
                    "doc_type": "derived",
                }
            )

        web_prompt = "(web search not used for this question)\n"
        if web_bundle is not None:
            from web_search import format_web_for_prompt

            web_prompt, web_cites = format_web_for_prompt(
                web_bundle, start_n=len(citations) + 1
            )
            for c in web_cites:
                citations.append(c)
                ctx_blocks.append(
                    f"[{c['n']}] WEB {c.get('title')}\nurl: {c.get('url')}\n{c.get('snippet')}\n"
                )

        if intent in ("temporal", "hybrid") and graph_tools_bundle:
            for tool in graph_tools_bundle.get("tools") or []:
                if not tool.get("ok"):
                    continue
                if tool.get("tool") == "commits_touching_path":
                    path_t = tool.get("path") or ""
                    repo_t = tool.get("repo_name") or "?"
                    for c in (tool.get("commits") or [])[:5]:
                        n = len(citations) + 1
                        short = c.get("short_sha") or (c.get("sha") or "")[:7]
                        citations.append(
                            {
                                "n": n,
                                "title": f"{short} — {c.get('message') or ''}",
                                "path": path_t,
                                "sha": short,
                                "doc_type": "graph_commit",
                                "repo_name": repo_t,
                            }
                        )
                        ctx_blocks.append(
                            f"[{n}] GRAPH commit path={path_t} repo={repo_t}\n"
                            f"short_sha={short} author={c.get('author_name')} "
                            f"when={c.get('authored_at')} msg={c.get('message')}\n"
                            f"what_changed={'; '.join(c.get('change_summary') or [])}\n"
                        )
                    if tool.get("commits"):
                        golden_cards_txt.append(
                            _format_commit_card(
                                tool["commits"][0],
                                path=path_t,
                                repo_name=repo_t,
                                graph_source=tool.get("source"),
                            )
                        )

        graph_txt = json.dumps(gctx, indent=2)[:2000] if gctx else "{}"
        tools_txt = json.dumps(
            {
                "intent": intent,
                "paths": (graph_tools_bundle or {}).get("paths"),
                "tools": [
                    {
                        "tool": t.get("tool"),
                        "ok": t.get("ok"),
                        "path": t.get("path"),
                        "repo_name": t.get("repo_name"),
                        "count": t.get("count"),
                        "commits": [
                            {
                                "short_sha": c.get("short_sha")
                                or (c.get("sha") or "")[:7],
                                "message": c.get("message"),
                                "author_name": c.get("author_name"),
                                "authored_at": c.get("authored_at"),
                                "change_summary": (c.get("change_summary") or [])[:10],
                            }
                            for c in (t.get("commits") or [])[:3]
                        ],
                    }
                    for t in (graph_tools_bundle or {}).get("tools") or []
                ],
            },
            indent=2,
        )[:5000]
        cards_blob = "\n\n".join(golden_cards_txt) if golden_cards_txt else "(none)"
        context_blob = (
            "\n---\n".join(ctx_blocks) if ctx_blocks else "(no indexed documents)"
        )

        # Model-first answering: reason over evidence (repo + optional web). No hard-coded essay.
        system = (
            "You are the product assistant for a multi-repo git knowledge workspace. "
            f"Scope: {scope_label}.\n\n"
            "You are given private evidence blocks. They are NOT part of the answer.\n"
            "HARD RULES:\n"
            "- NEVER paste or restate internal labels from the prompt such as "
            "'Graph membership', 'Graph tools', 'Optional commit cards', 'Workspace evidence', "
            "'Web search', 'User question', 'Intent', 'Scope', 'workspace_path_inventory', "
            "'workspace_files_and_search', 'web_optional', 'commit_cards_internal', "
            "'graph_stats_internal', or 'graph_tools_internal'.\n"
            "- NEVER list project_stats, repo commit counts, or tool metadata as the answer.\n"
            "- When describing Compose, use real **service names** (e.g. pd, store, server, pd0) "
            "from FILE evidence — not chart project `name:` fields as if they were services.\n"
            "- Prefer **workspace FILE / search** evidence over web for what *this* project does.\n"
            "- Web is only for general methods/tools (e.g. how Helm conversion works in industry). "
            "If you mention a third-party tool from the web, say it is optional and still map "
            "the project's real Compose services from FILE evidence.\n"
            "- Treat typos: 'hidden chart' almost always means **Helm chart**.\n"
            "- For Compose→Helm: (1) list real compose files/services from evidence, "
            "(2) note any existing charts (e.g. Chart.yaml under hugegraph-ai), "
            "(3) propose concrete conversion steps (values, Deployments, Services, probes from healthchecks, "
            "PVCs from volumes, multi-stack = multi-chart or subcharts), "
            "(4) only then mention converters like Kompose/Katenary as optional accelerators.\n"
            "- Cite [n]. Do not invent ports/env/paths not in evidence.\n"
            "- Answer in clear markdown for a human engineer. Be concise."
        )
        extra = (self.settings.get("system_prompt_extra") or "").strip()
        if extra:
            system += "\n" + extra

        # Explicit path list so the model cannot "forget" repo files in favor of a web tool
        path_inventory = []
        if content_dossier and content_dossier.get("ok"):
            for d in (content_dossier.get("documents") or [])[:20]:
                path_inventory.append(f"- {d.get('repo_name')}/{d.get('path')}")
        inv_txt = "\n".join(path_inventory) if path_inventory else "(no dossier paths)"

        user_prompt = (
            f"{system}\n\n"
            f"<evidence scope=\"{scope_type}:{scope_id}\" intent=\"{intent}\">\n"
            f"### files_in_this_project\n{inv_txt}\n\n"
            f"### file_excerpts_and_search\n{context_blob}\n\n"
            f"### optional_web_snippets\n{web_prompt}\n\n"
            f"### internal_graph_stats\n{graph_txt}\n\n"
            f"### internal_graph_tools\n{tools_txt}\n\n"
            f"### internal_commit_cards\n{cards_blob}\n"
            f"</evidence>\n\n"
            f"<user_question>\n{message}\n</user_question>\n\n"
            "Write only the engineer-facing answer.\n"
            "Start from the project files listed above; web tools are optional helpers only.\n"
            "Do not echo XML tags or evidence section names."
        )

        resolved = self._resolve_provider(
            prefer=(self.settings.get("ai_provider") or "auto"),
            probe=True,
        )
        if not resolved.get("ok"):
            # Fallback only if model is down: structured extract / graph card
            if intent == "temporal":
                direct = _direct_answer_from_graph_tools(message, graph_tools_bundle)
                if direct:
                    return {
                        "answer": direct["answer"],
                        "scope": {
                            "type": scope_type,
                            "id": scope_id,
                            "label": scope_label,
                        },
                        "citations": direct.get("citations") or [],
                        "mode": "deterministic_graph",
                        "intent": intent,
                        "ai_fallback": resolved.get("detail"),
                    }
            if compose_evidence and compose_evidence.get("answer"):
                return {
                    "answer": compose_evidence["answer"]
                    + "\n\n_(AI offline — showing structured file extract only.)_",
                    "scope": {
                        "type": scope_type,
                        "id": scope_id,
                        "label": scope_label,
                    },
                    "citations": compose_evidence.get("citations") or [],
                    "mode": "deterministic_content",
                    "intent": intent,
                    "ai_fallback": resolved.get("detail"),
                }
            raise AIError(
                resolved.get("detail") or "AI not configured",
                503,
                "not_configured",
            )

        answer = self._generate(resolved, user_prompt, max_tokens=2048)
        answer = _sanitize_model_answer(answer)
        out = {
            "answer": answer,
            "scope": {"type": scope_type, "id": scope_id, "label": scope_label},
            "citations": citations,
            "context_count": len(hits),
            "provider": resolved.get("provider"),
            "model": resolved.get("model"),
            "graph": gctx,
            "graph_tools": graph_tools_bundle,
            "mode": "llm",
            "intent": intent,
            "web_search": {
                "used": bool(web_bundle and web_bundle.get("ok")),
                "query": (web_bundle or {}).get("query"),
                "count": len((web_bundle or {}).get("results") or []),
                "error": (web_bundle or {}).get("error"),
            }
            if web_bundle is not None
            else {"used": False},
        }
        if content_dossier and content_dossier.get("ok"):
            out["content_dossier"] = {
                "file_count": content_dossier.get("file_count"),
                "topic": content_dossier.get("topic"),
                "paths": [
                    f"{d['repo_name']}/{d['path']}"
                    for d in (content_dossier.get("documents") or [])
                ],
            }
        return out

    def _generate(self, resolved: dict, prompt: str, *, max_tokens: int = 1024) -> str:
        mode = resolved.get("mode")
        if mode == "parvaana_prompt":
            base = resolved["base_url"].rstrip("/")
            last_err = ""
            with httpx.Client(timeout=120.0) as client:
                for attempt in range(2):
                    r = client.post(
                        f"{base}/prompt",
                        json={
                            "prompt": prompt,
                            "stream": False,
                            "max_tokens": max_tokens,
                            "temperature": 0.3,
                        },
                    )
                    if r.status_code < 400:
                        data = r.json()
                        return (data.get("response") or "").strip() or "(empty model response)"
                    last_err = f"{r.status_code} {r.text[:300]}"
                    if r.status_code >= 500 and attempt == 0:
                        log.warning("parvaana /prompt %s; retrying", r.status_code)
                        continue
                    break
            raise AIError(f"Parvaana AI error: {last_err}", 502)

        if mode == "openai_chat":
            base = resolved["base_url"].rstrip("/")
            key = resolved["api_key"]
            model = resolved["model"]
            url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
            with httpx.Client(timeout=120.0) as client:
                r = client.post(
                    url,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": [
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": 0.3,
                        "max_tokens": max_tokens,
                    },
                )
                if r.status_code >= 400:
                    raise AIError(f"OpenAI-compatible error: {r.status_code} {r.text[:300]}", 502)
                data = r.json()
                try:
                    return data["choices"][0]["message"]["content"].strip()
                except Exception as e:
                    raise AIError(f"bad completion payload: {e}", 502) from e

        raise AIError(f"unknown provider mode: {mode}", 500)

    def test_connection(self) -> dict[str, Any]:
        resolved = self._resolve_provider(
            prefer=(self.settings.get("ai_provider") or "auto"),
            probe=True,
        )
        if not resolved.get("ok"):
            return {
                "ok": False,
                **{k: resolved.get(k) for k in ("provider", "detail", "setup_hint", "base_url_public")},
            }
        try:
            ans = self._generate(
                resolved,
                "Reply with exactly the single word PONG and nothing else.",
            )
            ok = "PONG" in (ans or "").upper()
            return {
                "ok": ok or bool(ans),
                "provider": resolved.get("provider"),
                "model": resolved.get("model"),
                "sample": (ans or "")[:80],
                "detail": "connection ok" if ans else "empty response",
            }
        except Exception as e:
            return {"ok": False, "detail": str(e), "provider": resolved.get("provider")}


_LEAK_LINE = re.compile(
    r"(?im)^[ \t]*("
    r"graph membership|graph tools(?:\s*\(temporal\))?|optional commit cards|"
    r"workspace_path_inventory|workspace_files_and_search|web_optional|"
    r"files_in_this_project|file_excerpts_and_search|optional_web_snippets|"
    r"graph_stats_internal|graph_tools_internal|commit_cards_internal|"
    r"internal_graph_stats|internal_graph_tools|internal_commit_cards|"
    r"user question|intent:|project_stats:"
    r").*$"
)


def _sanitize_model_answer(text: str) -> str:
    """Strip prompt scaffolding the model sometimes echoes."""
    if not text:
        return text
    lines = []
    for ln in str(text).splitlines():
        if _LEAK_LINE.search(ln):
            continue
        if re.match(r"(?i)^evidence\s*$", ln.strip()):
            continue
        lines.append(ln)
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    # mid-sentence leaks (backticked or plain)
    for phrase, repl in (
        (r"(?i)\bin\s+`?workspace_path_inventory`?", "in the project files"),
        (r"(?i)\bfrom\s+(the\s+)?`?workspace_path_inventory`?,?\s*", "from the project files, "),
        (r"(?i)\b(at|using|via|see)\s+(the\s+)?`?workspace_path_inventory`?", r"\1 the project files"),
        (r"(?i)`?workspace_path_inventory`?", "project files"),
        (r"(?i)`?files_in_this_project`?", "project files"),
        (r"(?i)</?evidence[^>]*>", ""),
        (r"(?i)</?user_question>", ""),
    ):
        out = re.sub(phrase, repl, out)
    return out.strip()


def _direct_answer_from_graph_tools(message: str, bundle: Optional[dict]) -> Optional[dict]:
    """Golden commit card: WHO / WHEN / short id+subject / files / what changed."""
    if not bundle or not bundle.get("tools"):
        return None

    # Prefer path→commits golden card (score by path relevance to question)
    best = None
    qlow = (message or "").lower()
    for t in bundle["tools"]:
        if t.get("tool") not in ("commits_touching_path", "search_commits_by_message"):
            continue
        if not t.get("ok"):
            continue
        commits = t.get("commits") or []
        if not commits:
            continue
        path = t.get("path") or ""
        score = 1
        if t.get("tool") == "commits_touching_path":
            score = 2 if path.startswith("docker/") else 1
            if path == "docker/docker-compose.yml":
                score += 2
            if "3pd" in path or "3store" in path:
                score += 8 if re.search(r"3|cluster|3x3|distributed", qlow) else 3
            if path.endswith("README.md") and "supervis" in qlow:
                score += 6
        elif t.get("tool") == "search_commits_by_message":
            score = 4
            # boost if subject matches supervision/cluster keywords
            msg0 = (commits[0].get("message") or "").lower()
            if "supervis" in qlow and "supervis" in msg0:
                score += 10
            if re.search(r"3|cluster", qlow) and re.search(
                r"cluster|3pd|compose|docker", msg0
            ):
                score += 6
            # synthesize path from enriched files if present
            if not path and commits[0].get("files"):
                path = commits[0]["files"][0]
                t = dict(t)
                t["path"] = path
        if best is None or score > best[0]:
            best = (score, t)
    if best:
        t = best[1]
        commits = t["commits"]
        path = t.get("path") or (commits[0].get("files") or [""])[0] or "(topic match)"
        repo = t.get("repo_name") or commits[0].get("repo_name") or "?"
        c0 = commits[0]
        # Prefer compose-family file from change list when path is generic
        if path in ("", "(topic match)") or path == c0.get("matched_grep"):
            for f in c0.get("files") or []:
                if "compose" in f.lower() or f.startswith("docker/"):
                    path = f
                    break
        card = _format_commit_card(c0, path=path, repo_name=repo, graph_source=t.get("source"))
        cites = [
            {
                "n": 1,
                "title": f"{(c0.get('short_sha') or (c0.get('sha') or '')[:7])} — {c0.get('message') or ''}",
                "path": path,
                "sha": (c0.get("short_sha") or (c0.get("sha") or "")[:7]),
                "doc_type": "graph_commit",
                "repo_name": repo,
            }
        ]
        if len(commits) > 1:
            card += "\n\nEarlier commits on this path:\n"
            for c in commits[1:6]:
                short = c.get("short_sha") or (c.get("sha") or "")[:7]
                when = _human_when(c.get("authored_at"))
                who = c.get("author_name") or "unknown"
                card += f"• {short} — {c.get('message') or '?'} ({who}, {when})\n"
        return {"answer": card, "citations": cites}

    # Blast radius / SHA questions
    for t in bundle["tools"]:
        if t.get("tool") != "blast_radius" or not t.get("ok"):
            continue
        card = _format_blast_card(t, local_hint=message)
        if card:
            short = (t.get("sha") or "")[:7]
            return {
                "answer": card,
                "citations": [
                    {
                        "n": 1,
                        "title": f"{short} — {t.get('message') or 'blast radius'}",
                        "path": (t.get("files_modified") or [None])[0],
                        "sha": short,
                        "doc_type": "graph_commit",
                        "repo_name": t.get("repo_name"),
                    }
                ],
            }
    return None


def _format_blast_card(t: dict, *, local_hint: str = "") -> Optional[str]:
    """Golden-ish card for blast radius (commit → files + related)."""
    sha = t.get("sha") or ""
    if not sha:
        return None
    short = t.get("short_sha") or sha[:7]
    msg = t.get("message") or "(no subject)"
    files = t.get("files_modified") or []
    ordered = []
    for f in files:
        if "compose" in f.lower() or f.startswith("docker/"):
            ordered.append(f)
    for f in files:
        if f not in ordered:
            ordered.append(f)
    # Cap monorepo dumps
    show = ordered[:20]
    files_lines = "\n".join(f"  • {f}" for f in show) or "  • (no MODIFIED files in graph for this SHA)"
    if len(ordered) > 20:
        files_lines += f"\n  • … +{len(ordered)-20} more"

    related = t.get("related_commits") or []
    rel_lines = ""
    if related:
        rel_lines = "\n\nRelated commits (same files):\n"
        for r in related[:8]:
            rs = r.get("short_sha") or (r.get("sha") or "")[:7]
            rel_lines += f"  • {rs} — {r.get('message') or '?'} (via {r.get('via_file') or '?'})\n"

    who = t.get("author_name") or "unknown"
    email = t.get("author_email") or ""
    who_line = f"{who} <{email}>" if email else who
    when = _human_when(t.get("authored_at"))
    summary = t.get("change_summary") or []
    what = "\n".join(f"  • {s}" for s in summary[:15]) if summary else "  • (see files list; path-scoped summary unavailable)"

    return (
        f"Blast radius for {short} — {msg}\n"
        f"\n"
        f"• Commit:  {short} — {msg}\n"
        f"• Author:  {who_line}\n"
        f"• When:    {when}\n"
        f"• Files modified (File ← MODIFIED ← Commit):\n"
        f"{files_lines}\n"
        f"\nWhat they changed:\n{what}\n"
        f"{rel_lines}"
        f"\n(Evidence: GitAtlas/HugeGraph + git metadata; source={t.get('source') or 'gremlin'}; "
        f"detail sha {sha[:12]}…)"
    )


def _human_when(iso: Optional[str]) -> str:
    if not iso:
        return "unknown time"
    try:
        from datetime import datetime, timezone

        s = str(iso).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%d %b %Y, %H:%M %Z").strip() or dt.strftime("%d %b %Y, %H:%M UTC")
    except Exception:
        return str(iso)


def _format_commit_card(
    c: dict,
    *,
    path: str,
    repo_name: str,
    graph_source: Optional[str] = None,
) -> str:
    short = c.get("short_sha") or (c.get("sha") or "")[:7]
    full = c.get("sha") or ""
    msg = c.get("message") or "(no subject)"
    who = c.get("author_name") or "unknown"
    email = c.get("author_email") or ""
    who_line = f"{who} <{email}>" if email else who
    when = _human_when(c.get("authored_at") or c.get("committed_at"))
    # Committer if different from author (e.g. GitHub merge bot)
    cn, ce = c.get("committer_name") or "", c.get("committer_email") or ""
    committer_line = ""
    different_person = bool(cn and cn != who)
    different_email = bool(ce and email and ce != email)
    if different_person or different_email:
        committer_line = f"\n• Committer: {cn or 'unknown'}" + (f" <{ce}>" if ce else "")

    files = c.get("files") or [path]
    ordered = []
    if path in files:
        ordered.append(path)
    for f in files:
        if f not in ordered:
            ordered.append(f)
    # Cap file list — path family only (no monorepo dump)
    show = ordered[:12]
    files_lines = "\n".join(f"             {repo_name} → {f}" for f in show)
    if len(ordered) > 12:
        files_lines += f"\n             … +{len(ordered)-12} more (path-family focused)"

    summary = c.get("change_summary") or []
    if summary:
        what = "\n".join(f"  • {s}" for s in summary[:20])
    elif c.get("diff_stat"):
        what = "  • " + "\n  • ".join(
            ln.strip() for ln in str(c["diff_stat"]).splitlines()[:15] if ln.strip()
        )
    else:
        what = (
            f"  • Commit subject: {msg}\n"
            "  • (Detailed change summary unavailable — need local git clone or reindex)"
        )

    src = graph_source or "gremlin"
    return (
        f"Last change to {path} (repo: {repo_name})\n"
        f"\n"
        f"• Commit:  {short} — {msg}\n"
        f"• Author:  {who_line}"
        f"{committer_line}\n"
        f"• When:    {when}\n"
        f"• Files:   (File ← modified by ← Commit)\n{files_lines}\n"
        f"\n"
        f"What they changed:\n{what}\n"
        f"\n"
        f"(Evidence: GitAtlas/HugeGraph graph + git metadata; source={src}"
        + (f"; detail sha {full[:12]}…" if len(full) > 12 else "")
        + ")"
    )
