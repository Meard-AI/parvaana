"""Content-brain tools: discover + read workspace files for DeepWiki-style Ask.

Complements graph_tools (GitAtlas temporal). Used when the question is about
*how something works / is used*, not only who last changed a path.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("product.content_tools")

# Intent: content vs temporal vs hybrid
CONTENT_HINTS = re.compile(
    r"\b(how|what|where|which\s+files?|explain|overview|used|usage|document|"
    r"structure|purpose|describe|list|summary|breakdown|across|"
    r"architecture|deploy|deployment|setup|configure|configuration)\b",
    re.I,
)
# Strong commit/history signals — must win over generic "how compose works"
TEMPORAL_STRONG = re.compile(
    r"\b("
    r"who\s+(authored|touched|modified|changed|last|wrote|committed)|"
    r"when\s+(was|did|is|were)|"
    r"which\s+commit|last\s+commit|the\s+commit|"
    r"last\s+(touched|changed|modified|edited|updated|upgraded)|"
    r"(touched|changed|modified|edited|updated|upgraded)\s+(when|last)|"
    r"commit\s+where|was\s+edited|was\s+updated|was\s+upgraded|"
    r"blast\s*radius|what\s+changed|history\s+of|git\s+log|"
    r"authored|committer|sha\b|"
    r"broke|break|regression|introduced|caused\s+the\s+fail"
    r")\b",
    re.I,
)
# Soft temporal without "when/who" — still commit-shaped
TEMPORAL_SOFT = re.compile(
    r"\b(commit|commits|changelog|diff|blame|PR\s*#?\d+|pull\s+request)\b",
    re.I,
)
COMPOSE_TOPIC = re.compile(r"\b(docker[\s_-]*compose|compose\.ya?ml|\bcompose\b)\b", re.I)
HELM_K8S_TOPIC = re.compile(
    r"\b(helm|helmfile|chart\.ya?ml|values\.ya?ml|kubernetes|k8s|"
    r"deployment\.ya?ml|statefulset|ingress|kubectl)\b",
    re.I,
)
HYPOTHETICAL_HINTS = re.compile(
    r"\b("
    r"how\s+would|what\s+if|could\s+we|should\s+we|theoretically|theoretical|"
    r"hypothetic|imagine|propose|design\s+a|map\s+(this|it|them)\s+to|"
    r"in\s+theory|what\s+would|how\s+might|how\s+can\s+we|"
    r"would\s+(a|the|it|this)|if\s+we\s+(added|used|had|built)"
    r")\b",
    re.I,
)


def looks_hypothetical(question: str) -> bool:
    return bool(HYPOTHETICAL_HINTS.search(question or ""))


def classify_intent(question: str) -> str:
    """Return 'temporal' | 'content' | 'hybrid' | 'hypothetical'."""
    q = question or ""
    # Hypothetical / design questions first (still grounded in repo evidence)
    if looks_hypothetical(q) or (
        HELM_K8S_TOPIC.search(q)
        and re.search(r"\b(how|would|could|work|design|map|theor)\b", q, re.I)
        and not TEMPORAL_STRONG.search(q)
    ):
        # pure "what helm charts exist" is content; "how would helm work" is hypothetical
        if looks_hypothetical(q) or re.search(r"\bhow\s+would\b", q, re.I):
            return "hypothetical"

    temporal = bool(TEMPORAL_STRONG.search(q))
    # "check the last commit where …" / "commit … edited" without soft miss
    if not temporal and re.search(
        r"\b(last\s+commit|commit\s+where|where\s+.+\s+(edited|updated|changed))\b",
        q,
        re.I,
    ):
        temporal = True
    if not temporal and TEMPORAL_SOFT.search(q) and re.search(
        r"\b(when|who|last|edit|change|update|upgrad|touch|modif)\b", q, re.I
    ):
        temporal = True

    # How/what/explain — independent of temporal (compound Qs need both)
    how_content = bool(
        re.search(
            r"\b(how\s+(does|do|is|are|can)|what\s+is|what\s+are|explain|overview|"
            r"how\s+.+\s+work)\b",
            q,
            re.I,
        )
    )
    content = bool(CONTENT_HINTS.search(q)) or how_content or bool(
        COMPOSE_TOPIC.search(q)
    )

    # Multi-clause: "How …? When …?" → hybrid (must not drop the how-half)
    clauses = [c.strip() for c in re.split(r"[?\n;]|/(?=\s)", q) if c.strip()]
    if len(clauses) >= 2:
        clause_intents = []
        for c in clauses:
            c_temp = bool(TEMPORAL_STRONG.search(c)) or bool(
                re.search(r"\b(when|who|last\s+commit|upgraded|edited)\b", c, re.I)
            )
            c_how = bool(
                re.search(
                    r"\b(how|what\s+is|explain|overview|work)\b", c, re.I
                )
            )
            if c_temp and c_how:
                clause_intents.append("hybrid")
            elif c_temp:
                clause_intents.append("temporal")
            elif c_how or COMPOSE_TOPIC.search(c):
                clause_intents.append("content")
        if "content" in clause_intents and "temporal" in clause_intents:
            return "hybrid"
        if "hybrid" in clause_intents:
            return "hybrid"

    # "when did X break" → hybrid
    if re.search(r"\b(broke|break|fail|failure|regression|why\s+did)\b", q, re.I) and (
        temporal or COMPOSE_TOPIC.search(q)
    ):
        return "hybrid"
    # Same message has both how-it-works and when/who history
    if temporal and (how_content or (content and COMPOSE_TOPIC.search(q) and how_content)):
        return "hybrid"
    if temporal and how_content:
        return "hybrid"
    if temporal:
        return "temporal"
    if how_content or content or COMPOSE_TOPIC.search(q):
        return "content"
    return "content"


def looks_content(question: str) -> bool:
    return classify_intent(question) in ("content", "hybrid")


def discover_files(
    repos: list[dict[str, Any]],
    *,
    patterns: Optional[list[str]] = None,
    topic: str = "compose",
    limit: int = 40,
) -> list[dict[str, Any]]:
    """Find files under each repo local_path matching topic patterns."""
    if patterns is None:
        if topic in ("compose",) or COMPOSE_TOPIC.search(topic or ""):
            patterns = [
                "**/docker-compose*.yml",
                "**/docker-compose*.yaml",
                "**/compose.yml",
                "**/compose.yaml",
                "**/docker/**/README.md",
            ]
        elif topic in ("helm", "k8s", "deploy", "hypothetical"):
            # Real charts + compose (evidence to map theoretical Helm onto)
            patterns = [
                "**/Chart.yaml",
                "**/values.yaml",
                "**/values*.yaml",
                "**/charts/**/templates/*.yaml",
                "**/charts/**/templates/*.yml",
                "**/docker-compose*.yml",
                "**/docker-compose*.yaml",
                "**/docker/**/README.md",
                "**/Dockerfile*",
                "**/k8s/**/*.yaml",
                "**/kubernetes/**/*.yaml",
                "**/deploy/**/*.yaml",
            ]
        else:
            patterns = [f"**/*{topic}*"]

    found: list[dict[str, Any]] = []
    seen = set()
    for r in repos:
        name = r.get("name") or "?"
        root = r.get("local_path")
        if not root:
            continue
        root_p = Path(root)
        if not root_p.is_dir():
            continue
        for pat in patterns:
            try:
                matches = sorted(root_p.glob(pat))
            except Exception:
                continue
            for fp in matches:
                if not fp.is_file():
                    continue
                # skip noise
                rel = str(fp.relative_to(root_p)).replace("\\", "/")
                if any(
                    x in rel
                    for x in (
                        "node_modules/",
                        ".git/",
                        "vendor/",
                        "target/",
                        "__pycache__/",
                    )
                ):
                    continue
                key = (name, rel)
                if key in seen:
                    continue
                seen.add(key)
                found.append(
                    {
                        "repo_name": name,
                        "path": rel,
                        "abs_path": str(fp),
                        "size": fp.stat().st_size,
                    }
                )
                if len(found) >= limit:
                    return _rank_discovered_files(found, topic=topic)
    return _rank_discovered_files(found, topic=topic)


def _rank_discovered_files(
    files: list[dict[str, Any]], *, topic: str = "compose"
) -> list[dict[str, Any]]:
    if topic in ("helm", "k8s", "deploy", "hypothetical"):
        return _rank_deploy_files(files)
    return _rank_compose_files(files)


def _rank_deploy_files(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prefer Helm charts + primary compose for deploy/hypothetical questions."""

    def score(f: dict) -> tuple:
        p = (f.get("path") or "").lower()
        repo = (f.get("repo_name") or "").lower()
        s = 0
        if p.endswith("chart.yaml"):
            s += 120
        if p.endswith("values.yaml") or "/values." in p:
            s += 100
        if "/templates/" in p and p.endswith((".yml", ".yaml")):
            s += 80
        if "docker-compose" in p and "example" not in p:
            s += 55
        if p == "docker/readme.md":
            s += 45
        if "dockerfile" in p.split("/")[-1].lower():
            s += 25
        if "example" in p or "test" in p:
            s -= 20
        if repo == "hugegraph":
            s += 10
        if "ai" in repo:
            s += 15  # charts live under hugegraph-ai today
        return (-s, repo, p)

    return sorted(files, key=score)


def _rank_compose_files(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prefer primary hugegraph docker compose + README over examples."""

    def score(f: dict) -> tuple:
        p = f.get("path") or ""
        repo = (f.get("repo_name") or "").lower()
        s = 0
        if repo == "hugegraph":
            s += 50
        elif "ai" in repo:
            s += 30
        elif "toolchain" in repo:
            s += 20
        elif "computer" in repo:
            s += 10
        if p.startswith("docker/") and "example" not in p:
            s += 20
        if p.endswith("docker-compose.yml") and "example" not in p:
            s += 15
        if "docker-compose.dev.yml" in p:
            s += 12
        if "3pd-3store" in p:
            s += 11
        if "hbase" in p:
            s += 8
        if p == "docker/README.md":
            s += 40  # primary deploy docs (DeepWiki cites this)
        elif p.endswith("README.md") and p.startswith("docker/"):
            s += 15
        elif p.endswith("README.md") and "docker" in p:
            s += 8
        if "example" in p or "/test" in p or p.endswith("test.yml"):
            s -= 15
        if "gitatlas" in repo:
            s -= 40  # not hugegraph family product surface
        return (-s, repo, p)

    return sorted(files, key=score)


def read_file_excerpt(
    abs_path: str, *, max_chars: int = 6000, prefer_services: bool = True
) -> dict[str, Any]:
    """Read file text; for compose YAML prefer services-relevant body."""
    p = Path(abs_path)
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"ok": False, "error": str(e), "body": ""}
    full_len = len(text)
    body = text
    if prefer_services and (p.suffix in (".yml", ".yaml") or "compose" in p.name.lower()):
        # Drop long license headers for denser context
        lines = text.splitlines()
        start = 0
        for i, ln in enumerate(lines):
            if re.match(r"^(name:|services:|networks:|volumes:|version:|x-)", ln):
                start = i
                break
            if i > 40:
                break
        body = "\n".join(lines[start:])
    if len(body) > max_chars:
        body = body[:max_chars] + f"\n… [truncated, {full_len} chars total]"
    return {
        "ok": True,
        "body": body,
        "full_len": full_len,
        "line_count": text.count("\n") + 1,
    }


def summarize_compose_yaml(body: str, path: str) -> dict[str, Any]:
    """Heuristic summary of a compose file for deterministic answers."""
    services: list[str] = []
    images: list[str] = []
    builds: list[str] = []
    ports: list[str] = []
    env_keys: list[str] = []
    project_name = None
    m = re.search(r"(?m)^name:\s*['\"]?([^\s'\"]+)", body)
    if m:
        project_name = m.group(1)

    in_services = False
    current = None
    for line in body.splitlines():
        if re.match(r"^services:\s*$", line):
            in_services = True
            current = None
            continue
        if in_services:
            if line and not line[0].isspace() and not line.strip().startswith("#"):
                in_services = False
                current = None
                continue
            ms = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", line)
            if ms:
                current = ms.group(1)
                services.append(current)
                continue
            if current:
                mi = re.search(r"image:\s*['\"]?([^\s'\"]+)", line)
                if mi:
                    images.append(f"{current} → {mi.group(1)}")
                if re.search(r"^\s+build:", line) or re.search(r"dockerfile:", line):
                    builds.append(current)
                mp = re.match(r"""^\s+-\s*["']?(\d+:\d+)["']?""", line)
                if mp:
                    ports.append(f"{current}: {mp.group(1)}")
                me = re.match(r"^\s+([A-Z][A-Z0-9_]+):\s*", line)
                if me and me.group(1) not in ("CMD",):
                    env_keys.append(me.group(1))
                me2 = re.match(r"""^\s+-\s*([A-Z][A-Z0-9_]+)=""", line)
                if me2:
                    env_keys.append(me2.group(1))

    purpose = _infer_purpose(path, services, images, builds, project_name)
    return {
        "project_name": project_name,
        "services": services,
        "images": images[:12],
        "builds": list(dict.fromkeys(builds)),
        "ports": ports[:16],
        "env_keys": list(dict.fromkeys(env_keys))[:24],
        "purpose": purpose,
    }


def _infer_purpose(
    path: str,
    services: list[str],
    images: list[str],
    builds: list[str],
    project_name: Optional[str],
) -> str:
    pl = path.lower()
    if "hbase" in pl:
        return "Standalone HBase backend for local development/testing"
    if "3pd" in pl or "3store" in pl:
        return "3-node distributed cluster (PD + Store + Server)"
    if "dev.yml" in pl or "dev.yaml" in pl:
        return "Single-node development stack built from local Dockerfiles"
    if "llm" in pl:
        return "HugeGraph LLM / RAG service"
    if "network" in pl:
        return "Server + LLM/RAG on a shared Docker network"
    if "loader" in pl:
        return "Example loader + server + hubble data-import stack"
    if "cassandra" in pl:
        return "Example Cassandra-backed stack"
    if "trace" in pl:
        return "Example distributed tracing stack"
    if builds and not images:
        return "Build-from-source compose stack"
    if images and any("hugegraph/pd" in i or "hugegraph/server" in i for i in images):
        return "Single-node quickstart using pre-built Docker Hub images"
    if services:
        return f"Compose stack with services: {', '.join(services[:8])}"
    return "Docker Compose configuration"


def infer_dossier_topic(question: str) -> str:
    q = question or ""
    if looks_hypothetical(q) or HELM_K8S_TOPIC.search(q):
        return "hypothetical"
    if COMPOSE_TOPIC.search(q):
        return "compose"
    return "general"


def build_content_dossier(
    repos: list[dict[str, Any]],
    question: str,
    *,
    max_files: int = 12,
    max_chars_each: int = 4500,
) -> dict[str, Any]:
    """Discover + read + summarize files relevant to the question."""
    topic = infer_dossier_topic(question)
    patterns = None
    if topic == "general":
        # generic: still try path-like tokens
        patterns = None
        for m in re.finditer(
            r"[\w./-]+\.(yml|yaml|md|py|java|go|ts|tsx|json|toml)",
            question or "",
            re.I,
        ):
            patterns = patterns or []
            patterns.append(f"**/{m.group(0).lstrip('./')}")
        # also broad keywords
        if re.search(r"\bhelm\b", question or "", re.I):
            topic = "helm"
            patterns = None

    files = discover_files(repos, patterns=patterns, topic=topic, limit=max_files + 8)
    # Prefer Chart.yaml / values / compose higher for deploy questions
    if topic in ("hypothetical", "helm", "k8s", "deploy"):
        def _deploy_rank(f: dict) -> tuple:
            p = (f.get("path") or "").lower()
            s = 0
            if p.endswith("chart.yaml"):
                s += 100
            if p.endswith("values.yaml"):
                s += 90
            if "/templates/" in p:
                s += 70
            if "docker-compose" in p and "example" not in p:
                s += 50
            if p.endswith("dockerfile") or "dockerfile" in p:
                s += 30
            if "readme" in p and "docker" in p:
                s += 40
            return (-s, p)

        files = sorted(files, key=_deploy_rank)
    # Prefer compose yml + docker README
    selected = []
    for f in files:
        if len(selected) >= max_files:
            break
        selected.append(f)

    documents = []
    for f in selected:
        ex = read_file_excerpt(f["abs_path"], max_chars=max_chars_each)
        if not ex.get("ok"):
            continue
        body = ex["body"]
        summary = None
        if f["path"].endswith((".yml", ".yaml")) or "compose" in f["path"].lower():
            summary = summarize_compose_yaml(body, f["path"])
        documents.append(
            {
                "repo_name": f["repo_name"],
                "path": f["path"],
                "abs_path": f["abs_path"],
                "body": body,
                "line_count": ex.get("line_count"),
                "summary": summary,
            }
        )

    return {
        "tool": "content_dossier",
        "topic": topic,
        "question": question,
        "file_count": len(documents),
        "documents": documents,
        "ok": len(documents) > 0,
    }


def _is_example_compose(path: str) -> bool:
    pl = (path or "").lower()
    return "example" in pl or "test" in pl or "trace" in pl


def _compose_file_blurb(d: dict, cite_fn) -> str:
    """One short markdown block per compose file — no YAML dumps."""
    s = d.get("summary") or {}
    purpose = s.get("purpose") or "Compose stack"
    lines = [f"\n### `{d['path']}` — {purpose} {cite_fn(d)}\n"]
    bits = []
    if s.get("project_name"):
        bits.append(f"**project** `{s['project_name']}`")
    if s.get("services"):
        svcs = s["services"]
        shown = ", ".join(f"`{x}`" for x in svcs[:8])
        if len(svcs) > 8:
            shown += f" (+{len(svcs) - 8})"
        bits.append(f"**services** {shown}")
    if s.get("builds") and not s.get("images"):
        bits.append("**builds from source**")
    elif s.get("images"):
        # one-line images, not a bullet wall
        imgs = [i.split(" → ")[-1] if " → " in i else i for i in s["images"][:4]]
        bits.append("**images** " + ", ".join(f"`{i}`" for i in imgs))
    if s.get("ports"):
        bits.append("**ports** " + ", ".join(s["ports"][:6]))
    if s.get("env_keys"):
        bits.append(
            "**config via env** "
            + ", ".join(f"`{k}`" for k in s["env_keys"][:5])
            + ("…" if len(s["env_keys"]) > 5 else "")
        )
    if bits:
        lines.append("- " + " · ".join(bits) + "\n")
    return "".join(lines)


def format_compose_deepwiki_answer(dossier: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Short multi-repo compose overview — summary first, no YAML walls."""
    docs = dossier.get("documents") or []
    if not docs:
        return None

    by_repo: dict[str, list] = {}
    for d in docs:
        by_repo.setdefault(d["repo_name"], []).append(d)

    repo_order = sorted(
        by_repo.keys(),
        key=lambda n: (
            0
            if n == "hugegraph"
            else 1
            if "ai" in n
            else 2
            if "toolchain" in n
            else 3
            if "computer" in n
            else 9
        ),
    )

    cite_n = 0
    cite_map: list[dict] = []

    def cite(doc: dict, note: str = "") -> str:
        nonlocal cite_n
        cite_n += 1
        body = doc.get("body") or ""
        path = doc.get("path") or ""
        lang = "yaml" if path.endswith((".yml", ".yaml")) else (
            "markdown" if path.endswith(".md") else "text"
        )
        cite_map.append(
            {
                "n": cite_n,
                "title": f"{doc['repo_name']}/{path}",
                "path": path,
                "repo_name": doc["repo_name"],
                "doc_type": "content_file",
                "language": lang,
                # Short line for chips; full excerpt for Sources dropdown
                "snippet": body[:160],
                "body": body[:8000],
            }
        )
        return f"[{cite_n}]"

    # Split primary vs example stacks
    primary_rows: list[tuple[str, str, dict]] = []
    example_rows: list[tuple[str, str, dict]] = []

    for repo in repo_order:
        for d in by_repo[repo]:
            if not (
                "compose" in d["path"].lower()
                and d["path"].endswith((".yml", ".yaml"))
            ):
                continue
            s = d.get("summary") or {}
            purpose = s.get("purpose") or "Compose file"
            row = (f"{repo}/{d['path']}", purpose, d)
            if _is_example_compose(d["path"]):
                example_rows.append(row)
            else:
                primary_rows.append(row)

    parts = [
        "Docker Compose wires **deployment stacks** across this project: "
        "single-node quickstart, local builds, multi-node cluster, optional HBase, "
        "and AI/loader helpers. Below is a **summary only** (no full YAML — open the cited path for source).\n"
    ]

    # Summary table first (what the user scans)
    if primary_rows:
        parts.append("\n## Summary\n\n| File | Purpose |\n|---|---|\n")
        for path, purpose, _d in primary_rows:
            parts.append(f"| `{path}` | {purpose} |\n")

    # Brief detail by area (no code fences)
    if any(d["repo_name"] == "hugegraph" for _p, _pur, d in primary_rows):
        parts.append("\n## Core (`hugegraph/docker/`)\n")
        readmes = [
            d
            for d in by_repo.get("hugegraph", [])
            if d["path"] == "docker/README.md"
        ]
        if readmes:
            parts.append(
                f"Deploy docs: `hugegraph/docker/README.md` {cite(readmes[0])}. "
                "Stacks use **env vars** (not mounted `application-*.yml`) and healthchecks for startup order.\n"
            )
        for _path, _purpose, d in primary_rows:
            if d["repo_name"] != "hugegraph":
                continue
            parts.append(_compose_file_blurb(d, cite))

    for repo in repo_order:
        if repo == "hugegraph":
            continue
        rows = [r for r in primary_rows if r[2]["repo_name"] == repo]
        if not rows:
            continue
        title = (
            f"HugeGraph-AI (`{repo}/docker/`)"
            if "ai" in repo
            else f"Toolchain (`{repo}`)"
            if "toolchain" in repo
            else f"`{repo}`"
        )
        parts.append(f"\n## {title}\n")
        for _path, _purpose, d in rows:
            parts.append(_compose_file_blurb(d, cite))

    if example_rows:
        parts.append("\n## Examples (optional)\n")
        for path, purpose, d in example_rows[:6]:
            parts.append(f"- `{path}` — {purpose} {cite(d)}\n")

    parts.append(
        "\n*Cited paths are real files in the project clones "
        "(see **Sources** panel). Ask who/when a file changed for commit history.*\n"
    )

    return {
        "answer": "".join(parts),
        "citations": cite_map,
    }


def _services_snippet(body: str, max_lines: int = 30) -> str:
    lines = body.splitlines()
    # find services: or first service-ish
    start = 0
    for i, ln in enumerate(lines):
        if re.match(r"^(services:|name:|networks:|x-)", ln):
            start = i
            break
    chunk = lines[start : start + max_lines]
    return "\n".join(chunk).rstrip()


def dossier_as_prompt_blocks(dossier: dict[str, Any], start_n: int = 1) -> tuple[list[str], list[dict]]:
    """Serialize dossier into LLM context blocks + citations."""
    blocks = []
    cites = []
    n = start_n
    for d in dossier.get("documents") or []:
        cites.append(
            {
                "n": n,
                "title": f"{d['repo_name']}/{d['path']}",
                "path": d["path"],
                "repo_name": d["repo_name"],
                "doc_type": "content_file",
            }
        )
        summary = d.get("summary")
        head = f"[{n}] FILE {d['repo_name']} → {d['path']}\n"
        if summary:
            head += (
                f"purpose: {summary.get('purpose')}\n"
                f"services: {', '.join(summary.get('services') or [])}\n"
                f"images: {'; '.join(summary.get('images') or [])}\n"
            )
        head += "--- file content ---\n"
        head += d.get("body") or ""
        blocks.append(head)
        n += 1
    return blocks, cites
