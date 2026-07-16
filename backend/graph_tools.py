"""Graph tools for product Ask — Gremlin first (GitAtlas schema), git fallback.

Agentic GraphRAG style: fixed tools agents/router call, not free-form SQL.

GitAtlas schema (vertices): Repository, Branch, Commit, Author, File
Edges: HAS_BRANCH, HEAD, AUTHORED, PARENT, MODIFIED (Commit -MODIFIED-> File)
"""
from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

from gremlin_client import GremlinClient, GremlinError

log = logging.getLogger("product.graph_tools")

TEMPORAL_HINTS = re.compile(
    r"\b(commit|commits|changed|change|history|when|whom|who\s+(modified|authored|touched|changed)|"
    r"last\s+touch|authored|author|"
    r"blast|radius|what\s+changed|touched|modified|diff|sha|recently)\b",
    re.I,
)


def looks_temporal(question: str) -> bool:
    return bool(TEMPORAL_HINTS.search(question or ""))


def extract_path_candidates(question: str) -> list[str]:
    """Pull path-like tokens from a natural language question."""
    q = question or ""
    found = []
    # explicit paths with slash or extension
    for m in re.finditer(
        r"(?:[\w.-]+/)+[\w.-]+\.[\w.-]+|[\w.-]+\.(?:yml|yaml|json|md|py|go|java|xml|toml|lock)",
        q,
        re.I,
    ):
        found.append(m.group(0).lstrip("./"))
    # docker-compose / bare "compose" (common user shorthand)
    if re.search(r"docker[\s_-]*compose|\bcompose\b", q, re.I):
        found.extend(
            [
                "docker/docker-compose.yml",
                "docker-compose.yml",
                "docker/docker-compose.dev.yml",
                "compose.yml",
            ]
        )
    # 3-node / 3×3 / three-cluster compose
    if re.search(
        r"\b(3[\s_-]*node|3[\s_-]*cluster|three[\s_-]*cluster|3x3|3pd|3[\s_-]*pd|"
        r"distributed\s+cluster|pd0|3store)\b",
        q,
        re.I,
    ):
        found.insert(0, "docker/docker-compose-3pd-3store-3server.yml")
    # process supervision / -d flag docs often touch README + compose
    if re.search(r"\b(supervision|supervisor|HEALTHCHECK|-d\s+flag|daemon)\b", q, re.I):
        found.extend(
            [
                "docker/README.md",
                "docker/docker-compose.yml",
                "docker/docker-compose.dev.yml",
            ]
        )
    if re.search(r"\bhbase\b", q, re.I) and re.search(r"compose|docker", q, re.I):
        found.insert(0, "docker/hbase/docker-compose.hbase.yml")
    # dedupe preserve order
    out = []
    seen = set()
    for p in found:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def file_vertex_id(path: str) -> str:
    p = path.lstrip("./")
    if p.startswith("file:"):
        return p
    return f"file:{p}"


def commit_vertex_id(sha: str) -> str:
    s = sha.strip()
    if s.startswith("commit:"):
        return s
    return f"commit:{s}"


class GraphTools:
    def __init__(self, gremlin: Optional[GremlinClient] = None):
        self.gremlin = gremlin or GremlinClient()

    def graph_health(self) -> dict[str, Any]:
        return self.gremlin.health()

    def commits_touching_path(
        self,
        path: str,
        *,
        limit: int = 20,
        local_repo: Optional[Path] = None,
        repo_name: Optional[str] = None,
    ) -> dict[str, Any]:
        """Return commits that MODIFIED a file path (Gremlin), enriched with WHO/WHEN/files/diff."""
        path = path.lstrip("./")
        fid = file_vertex_id(path)
        source = "gremlin"
        commits: list[dict[str, Any]] = []
        error = None
        try:
            # Commit -MODIFIED-> File  ⇒  File.in('MODIFIED') = commits
            q = (
                f"g.V().hasLabel('File').hasId('{_esc(fid)}')"
                f".in('MODIFIED').limit({int(limit)}).valueMap(true)"
            )
            rows = self.gremlin.execute(q)
            commits = [_commit_from_valuemap(r) for r in rows]
            # try alternate common paths if empty
            if not commits and "docker-compose" in path.replace("_", "-"):
                for alt in (
                    "docker/docker-compose.yml",
                    "docker-compose.yml",
                    "docker/docker-compose.dev.yml",
                ):
                    if alt == path:
                        continue
                    alt_id = file_vertex_id(alt)
                    q2 = (
                        f"g.V().hasLabel('File').hasId('{_esc(alt_id)}')"
                        f".in('MODIFIED').limit({int(limit)}).valueMap(true)"
                    )
                    rows2 = self.gremlin.execute(q2)
                    if rows2:
                        path = alt
                        fid = alt_id
                        commits = [_commit_from_valuemap(r) for r in rows2]
                        break
            # Enrich each commit: author edge, files out(MODIFIED), temporal + path-scoped diff
            for c in commits:
                self._enrich_commit_card(c, path=path, local_repo=local_repo, repo_name=repo_name)
        except GremlinError as e:
            error = str(e)
            log.warning("gremlin commits_touching_path failed: %s", e)

        if not commits and local_repo and Path(local_repo).exists():
            source = "git_fallback"
            commits = _git_commits_touching_path(Path(local_repo), path, limit=limit)
            for c in commits:
                self._enrich_commit_card(c, path=path, local_repo=local_repo, repo_name=repo_name)

        # Most recent first when temporal data available
        commits = _sort_commits_newest_first(commits)

        return {
            "tool": "commits_touching_path",
            "path": path,
            "file_id": fid,
            "repo_name": repo_name,
            "source": source,
            "count": len(commits),
            "commits": commits,
            "error": error,
            "ok": len(commits) > 0,
        }

    def _enrich_commit_card(
        self,
        c: dict[str, Any],
        *,
        path: str,
        local_repo: Optional[Path],
        repo_name: Optional[str],
    ) -> None:
        """Fill author, when, short_sha, files, change_summary — graph first, git fill gaps."""
        sha = (c.get("sha") or "").strip()
        if not sha:
            return
        c["short_sha"] = sha[:7]
        c["repo_name"] = repo_name or c.get("repo_name")
        c.setdefault("path", path)

        # Author from graph: Author -AUTHORED-> Commit
        if not c.get("author_name") or not c.get("author_email"):
            try:
                rows = self.gremlin.execute(
                    f"g.V().hasLabel('Commit').hasId('{_esc(commit_vertex_id(sha))}')"
                    f".in('AUTHORED').valueMap(true)"
                )
                if rows:
                    a = rows[0] if isinstance(rows[0], dict) else {}
                    names = a.get("name") or []
                    if isinstance(names, list) and names:
                        c["author_name"] = c.get("author_name") or names[0]
                    aid = str(a.get("id") or "")
                    if aid.startswith("author:"):
                        c["author_email"] = c.get("author_email") or aid[len("author:") :]
            except Exception as e:
                log.debug("author enrich: %s", e)

        # Files from graph (full list can be huge on sparse roots — filter to path family)
        graph_files: list[str] = []
        try:
            frows = self.gremlin.execute(
                f"g.V().hasLabel('Commit').hasId('{_esc(commit_vertex_id(sha))}')"
                f".out('MODIFIED').limit(200).id()"
            )
            graph_files = [_file_id_to_path(str(x)) for x in frows]
        except Exception:
            graph_files = []

        # Temporal + author + PATH-SCOPED diff from git (required for WHO/WHEN/what-changed)
        if local_repo and Path(local_repo).exists():
            meta = _git_commit_meta(Path(local_repo), sha, focus_path=path)
            if meta:
                c["author_name"] = c.get("author_name") or meta.get("author_name")
                c["author_email"] = c.get("author_email") or meta.get("author_email")
                # Prefer git authored_at when graph schema lacks temporal props
                c["authored_at"] = meta.get("authored_at") or c.get("authored_at")
                c["message"] = c.get("message") or meta.get("message")
                c["message_body"] = meta.get("message_body") or ""
                c["change_summary"] = meta.get("change_summary") or []
                c["diff_stat"] = meta.get("diff_stat") or ""
                c["files"] = meta.get("files") or _focus_files(graph_files, path) or [path]
                c["committer_name"] = meta.get("committer_name")
                c["committer_email"] = meta.get("committer_email")
                c["committed_at"] = meta.get("committed_at")
            else:
                c["files"] = _focus_files(graph_files, path) or [path]
        else:
            c["files"] = _focus_files(graph_files, path) or [path]
            if not c.get("change_summary"):
                c["change_summary"] = [
                    f"Modified {path} (graph link File ← MODIFIED ← Commit; no local git for line-level diff)"
                ]

        if not c.get("authored_at") and c.get("authored_at_prop"):
            c["authored_at"] = c["authored_at_prop"]

    def blast_radius(
        self,
        sha: str,
        *,
        limit_files: int = 50,
        limit_related: int = 30,
        local_repo: Optional[Path] = None,
    ) -> dict[str, Any]:
        sha = sha.strip()
        cid = commit_vertex_id(sha)
        source = "gremlin"
        files: list[str] = []
        message = None
        error = None
        related: list[dict[str, Any]] = []
        try:
            msg_rows = self.gremlin.execute(
                f"g.V().hasLabel('Commit').hasId('{_esc(cid)}').valueMap(true)"
            )
            if msg_rows:
                c0 = _commit_from_valuemap(msg_rows[0])
                message = c0.get("message")
                if c0.get("sha"):
                    sha = c0["sha"]
                    cid = commit_vertex_id(sha)

            file_rows = self.gremlin.execute(
                f"g.V().hasLabel('Commit').hasId('{_esc(cid)}')"
                f".out('MODIFIED').limit({int(limit_files)}).id()"
            )
            for fr in file_rows:
                files.append(_file_id_to_path(str(fr)))

            # related commits: other commits that touch same files
            if files:
                # sample first few files
                related_seen = set()
                for fpath in files[:15]:
                    fid = file_vertex_id(fpath)
                    rel = self.gremlin.execute(
                        f"g.V().hasLabel('File').hasId('{_esc(fid)}')"
                        f".in('MODIFIED').limit(8).valueMap(true)"
                    )
                    for r in rel:
                        c = _commit_from_valuemap(r)
                        if not c.get("sha") or c["sha"] == sha or c["sha"] in related_seen:
                            continue
                        related_seen.add(c["sha"])
                        c["via_file"] = fpath
                        related.append(c)
                        if len(related) >= limit_related:
                            break
                    if len(related) >= limit_related:
                        break
        except GremlinError as e:
            error = str(e)
            log.warning("gremlin blast_radius failed: %s", e)

        if not files and local_repo and Path(local_repo).exists():
            source = "git_fallback"
            files, message, related = _git_blast(Path(local_repo), sha)

        author_name = author_email = authored_at = None
        committer_name = committer_email = None
        change_summary: list[str] = []
        # Prefer compose-related focus for summary if present
        focus = None
        for f in files:
            if "compose" in f.lower():
                focus = f
                break
        if local_repo and Path(local_repo).exists() and sha:
            meta = _git_commit_meta(Path(local_repo), sha, focus_path=focus)
            if meta:
                author_name = meta.get("author_name")
                author_email = meta.get("author_email")
                authored_at = meta.get("authored_at")
                committer_name = meta.get("committer_name")
                committer_email = meta.get("committer_email")
                message = message or meta.get("message")
                change_summary = meta.get("change_summary") or []
                if meta.get("files") and not files:
                    files = meta["files"]

        return {
            "tool": "blast_radius",
            "sha": sha,
            "short_sha": sha[:7] if sha else "",
            "commit_id": cid,
            "message": message,
            "author_name": author_name,
            "author_email": author_email,
            "authored_at": authored_at,
            "committer_name": committer_name,
            "committer_email": committer_email,
            "change_summary": change_summary,
            "files_modified": files,
            "related_commits": related,
            "source": source,
            "error": error,
            "ok": bool(files or message),
            "source_label": "product-control-plane+gremlin"
            if source == "gremlin"
            else "product-control-plane+git",
        }

    def search_commits_by_message(
        self,
        query: str,
        *,
        local_repo: Optional[Path] = None,
        repo_name: Optional[str] = None,
        limit: int = 15,
    ) -> dict[str, Any]:
        """Find commits by message keywords (git log --grep + optional Gremlin message scan)."""
        q = (query or "").strip()
        # Extract grep-worthy tokens
        tokens = re.findall(
            r"[A-Za-z][A-Za-z0-9_.-]{2,}",
            q,
        )
        stop = {
            "the",
            "and",
            "was",
            "were",
            "when",
            "where",
            "what",
            "which",
            "this",
            "that",
            "with",
            "from",
            "check",
            "last",
            "commit",
            "edited",
            "updated",
            "change",
            "changed",
            "docker",
            "compose",
            "about",
            "did",
            "how",
            "does",
        }
        keys = [t for t in tokens if t.lower() not in stop][:6]
        # Prefer distinctive multi-word patterns
        greps = []
        if re.search(r"supervis", q, re.I):
            greps.append("supervis")
        if re.search(r"3pd|3x3|three.?cluster|3.?node|distributed", q, re.I):
            greps.append("cluster")
            greps.append("3pd")
            greps.append("docker-compose")
        if re.search(r"-d\s+flag|daemon", q, re.I):
            greps.append("-d")
            greps.append("daemon")
        greps.extend(keys[:4])
        greps = list(dict.fromkeys(greps))[:6]

        commits: list[dict] = []
        source = "none"
        if local_repo and Path(local_repo).exists() and greps:
            source = "git_grep"
            for g in greps:
                try:
                    out = subprocess.check_output(
                        [
                            "git",
                            "-C",
                            str(local_repo),
                            "log",
                            f"-n{limit}",
                            f"--grep={g}",
                            "--regexp-ignore-case",
                            "--format=%H%x1f%an%x1f%ae%x1f%aI%x1f%s",
                        ],
                        text=True,
                        timeout=30,
                    )
                except Exception:
                    continue
                for line in out.splitlines():
                    parts = line.split("\x1f")
                    if len(parts) < 5:
                        continue
                    sha, an, ae, when, subj = parts
                    if any(c.get("sha") == sha for c in commits):
                        continue
                    commits.append(
                        {
                            "sha": sha,
                            "short_sha": sha[:7],
                            "author_name": an,
                            "author_email": ae,
                            "authored_at": when,
                            "message": subj,
                            "matched_grep": g,
                        }
                    )
                if len(commits) >= limit:
                    break
            for c in commits:
                self._enrich_commit_card(
                    c,
                    path=c.get("matched_grep") or "",
                    local_repo=local_repo,
                    repo_name=repo_name,
                )

        return {
            "tool": "search_commits_by_message",
            "query": q,
            "greps": greps,
            "repo_name": repo_name,
            "source": source,
            "count": len(commits),
            "commits": commits[:limit],
            "ok": len(commits) > 0,
        }

    def gather_for_question(
        self,
        question: str,
        *,
        local_repo: Optional[Path] = None,
        repo_name: Optional[str] = None,
        repos: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """Tool bundle for Ask when question looks temporal/change-shaped.

        repos: optional multi-repo list [{name, local_path}] for project scope —
        try each until path hits (correct repo for compose, not wrong-repo subjects).
        """
        tools_run = []
        paths = extract_path_candidates(question)
        repo_list = list(repos or [])
        if not repo_list and (local_repo or repo_name):
            repo_list = [{"name": repo_name, "local_path": local_repo}]

        for p in paths[:6]:
            hit = None
            # Prefer explicit single local_repo first, then scan project members
            candidates = repo_list or [{"name": repo_name, "local_path": local_repo}]
            for rinfo in candidates:
                rpath = rinfo.get("local_path")
                rname = rinfo.get("name")
                lp = Path(rpath) if rpath else None
                if lp and not lp.exists():
                    lp = None
                res = self.commits_touching_path(
                    p, limit=15, local_repo=lp, repo_name=rname
                )
                if res.get("ok"):
                    hit = res
                    # If multi-repo, keep first real hit per path (don't mix wrong repos)
                    break
                if hit is None:
                    hit = res
            if hit is not None:
                tools_run.append(hit)

        # Message search when user asks about a topic (supervision, cluster upgrade, …)
        if looks_temporal(question) or re.search(
            r"\b(commit|edited|updated|upgraded|supervis|cluster|when)\b",
            question or "",
            re.I,
        ):
            candidates = repo_list or [{"name": repo_name, "local_path": local_repo}]
            for rinfo in candidates[:4]:
                lp = rinfo.get("local_path")
                if not lp or not Path(lp).exists():
                    continue
                msg_hit = self.search_commits_by_message(
                    question, local_repo=Path(lp), repo_name=rinfo.get("name"), limit=12
                )
                if msg_hit.get("ok"):
                    tools_run.append(msg_hit)
                    break

        # if user mentions a short sha
        sha_m = re.search(r"\b([0-9a-f]{7,40})\b", question or "", re.I)
        if sha_m:
            lp0 = None
            if local_repo and Path(local_repo).exists():
                lp0 = Path(local_repo)
            elif repo_list:
                for rinfo in repo_list:
                    if rinfo.get("local_path") and Path(rinfo["local_path"]).exists():
                        lp0 = Path(rinfo["local_path"])
                        break
            tools_run.append(self.blast_radius(sha_m.group(1), local_repo=lp0))
        return {
            "temporal": looks_temporal(question),
            "paths": paths,
            "tools": tools_run,
            "graph_health": self.graph_health(),
        }


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _commit_from_valuemap(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {"raw": row}
    vid = str(row.get("id") or "")
    sha = vid[7:] if vid.startswith("commit:") else vid
    msg = row.get("message")
    if isinstance(msg, list) and msg:
        msg = msg[0]
    def _first(key: str):
        v = row.get(key)
        if isinstance(v, list) and v:
            return v[0]
        return v

    return {
        "sha": sha,
        "short_sha": sha[:7] if sha else "",
        "message": msg,
        "id": vid,
        "author_name": _first("author_name"),
        "author_email": _first("author_email"),
        "authored_at": _first("authored_at"),
    }


def _file_id_to_path(fid: str) -> str:
    s = str(fid)
    if s.startswith("file:"):
        return s[5:]
    return s


def _git_commits_touching_path(repo: Path, path: str, limit: int = 20) -> list[dict]:
    try:
        out = subprocess.check_output(
            [
                "git",
                "-C",
                str(repo),
                "log",
                f"-n{limit}",
                "--format=%H%x1f%an%x1f%ae%x1f%aI%x1f%s",
                "--",
                path,
            ],
            text=True,
            timeout=60,
        )
    except Exception:
        return []
    commits = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) < 5:
            continue
        sha, an, ae, when, subj = parts[0], parts[1], parts[2], parts[3], parts[4]
        commits.append(
            {
                "sha": sha,
                "short_sha": sha[:7],
                "message": subj,
                "author_name": an,
                "author_email": ae,
                "authored_at": when,
            }
        )
    return commits


def _sort_commits_newest_first(commits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(c: dict) -> str:
        return str(c.get("authored_at") or c.get("committed_at") or "")

    # Only reorder when at least one has a timestamp
    if any(key(c) for c in commits):
        return sorted(commits, key=key, reverse=True)
    return commits


def _focus_files(files: list[str], path: str) -> list[str]:
    """Keep asked path + sibling compose/docker files; avoid monorepo dumps."""
    if not files:
        return [path] if path else []
    path = path.lstrip("./")
    focused = []
    for f in files:
        fl = f.lstrip("./")
        if fl == path:
            focused.append(fl)
        elif _same_path_family(fl, path):
            focused.append(fl)
    if not focused:
        # Still chain the asked file even if graph listed unrelated paths
        return [path]
    # de-dupe preserve order
    out, seen = [], set()
    for f in focused:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def _same_path_family(candidate: str, focus: str) -> bool:
    c, f = candidate.lower(), focus.lower()
    if c == f:
        return True
    # compose family
    compose_tokens = ("docker-compose", "compose.yml", "compose.yaml")
    if any(t in f for t in compose_tokens) or "docker/" in f:
        if any(t in c for t in compose_tokens):
            return True
        if c.startswith("docker/") and (
            c.endswith(".yml") or c.endswith(".yaml") or c.endswith("readme.md")
        ):
            return True
    # same directory
    if "/" in f and c.rsplit("/", 1)[0] == f.rsplit("/", 1)[0]:
        return True
    return False


def _git_commit_meta(
    repo: Path, sha: str, *, focus_path: Optional[str] = None
) -> Optional[dict[str, Any]]:
    """Author, when, path-scoped files, and human change summary for a commit."""
    try:
        meta = subprocess.check_output(
            [
                "git",
                "-C",
                str(repo),
                "log",
                "-1",
                "--format=%H%x1f%an%x1f%ae%x1f%aI%x1f%cn%x1f%ce%x1f%cI%x1f%s%x1f%b",
                sha,
            ],
            text=True,
            timeout=30,
        ).strip("\n")
        parts = meta.split("\x1f")
        if len(parts) < 8:
            return None
        full = parts[0]
        an, ae, a_when = parts[1], parts[2], parts[3]
        cn, ce, c_when = parts[4], parts[5], parts[6]
        subj = parts[7]
        body = parts[8] if len(parts) > 8 else ""

        focus = (focus_path or "").lstrip("./")
        # Path-scoped file list (what the user asked about), not whole-tree dump
        name_cmd = ["git", "-C", str(repo), "show", "--name-only", "--format=", full]
        if focus:
            name_cmd += ["--", focus]
            # also sibling compose files if focus is compose
            if "compose" in focus or focus.startswith("docker/"):
                name_cmd += [
                    "docker/docker-compose.yml",
                    "docker/docker-compose.dev.yml",
                    "docker-compose.yml",
                    "docker/README.md",
                    "compose.yml",
                ]
        files_out = subprocess.check_output(name_cmd, text=True, timeout=30)
        files = [f for f in files_out.splitlines() if f.strip()]
        files = _focus_files(files, focus) if focus else files[:40]
        if focus and focus not in files:
            # Ensure the asked path is chained even when only linked via graph
            files = [focus] + files

        # Path-scoped numstat
        num_cmd = ["git", "-C", str(repo), "show", "--numstat", "--format=", full]
        if focus:
            num_cmd += ["--"] + files[:20]
        numstat = subprocess.check_output(num_cmd, text=True, timeout=30)
        change_summary: list[str] = []

        # Prefer commit body bullets when present (human "what they did")
        for bl in body.splitlines():
            bl = bl.strip()
            if bl.startswith(("-", "*", "•")) and len(bl) > 3:
                change_summary.append(bl.lstrip("-*• ").strip())
            if len(change_summary) >= 8:
                break

        # Path-level substance (compose services / line stats)
        if focus:
            change_summary.extend(
                _path_substance_bullets(repo, full, focus, numstat=numstat)
            )
        else:
            for line in numstat.splitlines():
                cols = line.split("\t")
                if len(cols) >= 3:
                    a, d, pth = cols[0], cols[1], cols[2]
                    if a == "-" and d == "-":
                        change_summary.append(f"binary change: {pth}")
                    else:
                        change_summary.append(f"{pth}: +{a}/-{d} lines")
                if len(change_summary) >= 20:
                    break

        # de-dupe preserve order
        seen_s, uniq = set(), []
        for s in change_summary:
            if s and s not in seen_s:
                seen_s.add(s)
                uniq.append(s)
        change_summary = uniq[:20]

        stat = subprocess.check_output(
            ["git", "-C", str(repo), "show", "--stat", "--format=", full]
            + (["--"] + files[:15] if files else []),
            text=True,
            timeout=30,
        )

        return {
            "sha": full,
            "author_name": an,
            "author_email": ae,
            "authored_at": a_when,  # author date — true temporal "when they wrote it"
            "committer_name": cn,
            "committer_email": ce,
            "committed_at": c_when,
            "message": subj,
            "message_body": body.strip()[:2000],
            "files": files,
            "change_summary": change_summary,
            "diff_stat": stat.strip()[:2000],
        }
    except Exception as e:
        log.debug("git meta failed: %s", e)
        return None


def _path_substance_bullets(
    repo: Path, sha: str, path: str, *, numstat: str = ""
) -> list[str]:
    """Concrete 'what changed' for a path — compose services, ports, images, line stats."""
    bullets: list[str] = []
    # line stats for this path
    for line in (numstat or "").splitlines():
        cols = line.split("\t")
        if len(cols) >= 3 and cols[2].lstrip("./") == path.lstrip("./"):
            a, d = cols[0], cols[1]
            if a == "-" and d == "-":
                bullets.append(f"{path}: binary change")
            else:
                status = "added" if d == "0" and a != "0" else "modified"
                # detect pure add via git status letter
                try:
                    ns = subprocess.check_output(
                        [
                            "git",
                            "-C",
                            str(repo),
                            "show",
                            "--name-status",
                            "--format=",
                            sha,
                            "--",
                            path,
                        ],
                        text=True,
                        timeout=15,
                    ).strip()
                    if ns.startswith("A"):
                        status = "added (new file)"
                    elif ns.startswith("D"):
                        status = "deleted"
                    elif ns.startswith("M"):
                        status = "modified"
                except Exception:
                    pass
                bullets.append(f"{path}: {status}, +{a}/-{d} lines")
            break

    # Compose / YAML substance from file blob at this commit
    lower = path.lower()
    if any(x in lower for x in ("compose", ".yml", ".yaml")):
        try:
            blob = subprocess.check_output(
                ["git", "-C", str(repo), "show", f"{sha}:{path}"],
                text=True,
                timeout=15,
                stderr=subprocess.DEVNULL,
            )
            bullets.extend(_compose_yaml_bullets(blob, path))
        except Exception:
            # new file might need different handling — try still
            pass
    return bullets


def _compose_yaml_bullets(content: str, path: str) -> list[str]:
    """Extract services / images / ports / volumes from a compose file for human cards."""
    if not content:
        return []
    bullets: list[str] = []
    # project name
    for m in re.finditer(r"(?m)^name:\s*['\"]?([^\s'\"]+)", content):
        bullets.append(f"compose project name: {m.group(1)}")
        break
    # services block — naive YAML indent parse
    services: list[str] = []
    images: list[str] = []
    ports: list[str] = []
    volumes: list[str] = []
    in_services = False
    current_svc = None
    for line in content.splitlines():
        if re.match(r"^services:\s*$", line):
            in_services = True
            current_svc = None
            continue
        if in_services:
            if line and not line.startswith((" ", "\t")) and not line.strip().startswith("#"):
                # top-level key left services
                in_services = False
                current_svc = None
                continue
            m_svc = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", line)
            if m_svc:
                current_svc = m_svc.group(1)
                services.append(current_svc)
                continue
            if current_svc:
                mi = re.search(r"image:\s*['\"]?([^\s'\"]+)", line)
                if mi:
                    images.append(f"{current_svc} → {mi.group(1)}")
                mp2 = re.match(r"""^\s+-\s*["']?(\d+:\d+)["']?""", line)
                if mp2:
                    ports.append(f"{current_svc}: {mp2.group(1)}")
                mv = re.match(r"""^\s+-\s*([A-Za-z0-9_./-]+:/[^\s#]+)""", line)
                if mv:
                    volumes.append(f"{current_svc}: {mv.group(1)}")

    if services:
        extra = f" (+{len(services)-12} more)" if len(services) > 12 else ""
        bullets.append(f"services defined: {', '.join(services[:12])}{extra}")
    for img in images[:8]:
        bullets.append(f"image: {img}")
    if ports:
        bullets.append("ports: " + ", ".join(ports[:10]))
    if volumes:
        uniq_vols = list(dict.fromkeys(volumes))[:8]
        bullets.append("volumes: " + ", ".join(uniq_vols))
    if re.search(r"(?m)^networks:\s*$", content):
        bullets.append("defines custom Docker networks")
    return bullets[:12]


def _git_blast(repo: Path, sha: str) -> tuple[list[str], Optional[str], list[dict]]:
    try:
        full = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", sha], text=True, timeout=30
        ).strip()
        msg = subprocess.check_output(
            ["git", "-C", str(repo), "log", "-1", "--format=%s", full],
            text=True,
            timeout=30,
        ).strip()
        files_out = subprocess.check_output(
            ["git", "-C", str(repo), "show", "--name-only", "--format=", full],
            text=True,
            timeout=30,
        )
        files = [f for f in files_out.splitlines() if f.strip()]
        related = []
        seen = set()
        for f in files[:20]:
            log_out = subprocess.check_output(
                ["git", "-C", str(repo), "log", "-n", "5", "--format=%H%x1f%s", "--", f],
                text=True,
                timeout=30,
            )
            for line in log_out.splitlines():
                if "\x1f" not in line:
                    continue
                csha, subj = line.split("\x1f", 1)
                if csha == full or csha in seen:
                    continue
                seen.add(csha)
                related.append({"sha": csha, "message": subj, "via_file": f})
        return files, msg, related[:30]
    except Exception:
        return [], None, []
