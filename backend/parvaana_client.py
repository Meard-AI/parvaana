"""Parvaana searcher client — content brain for product Search/Ask."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("product.parvaana")

DEFAULT_SEARCHER = os.environ.get("PARVAANA_SEARCHER_URL", "")


def discover_searcher_url() -> Optional[str]:
    if DEFAULT_SEARCHER:
        return DEFAULT_SEARCHER.rstrip("/")
    try:
        out = subprocess.check_output(
            [
                "docker",
                "inspect",
                "parvaana-searcher",
                "--format",
                "{{range $k,$v := .NetworkSettings.Networks}}{{$k}}={{$v.IPAddress}} {{end}}",
            ],
            text=True,
            timeout=5,
        ).strip()
    except Exception as e:
        log.debug("searcher discover failed: %s", e)
        return None
    for part in out.split():
        if "=" not in part:
            continue
        net, ip = part.split("=", 1)
        if not ip:
            continue
        if "parvaana" in net:
            return f"http://{ip}:3001"
    # any ip
    for part in out.split():
        if "=" in part:
            ip = part.split("=", 1)[1]
            if ip:
                return f"http://{ip}:3001"
    return None


def discover_connector_manager_url() -> Optional[str]:
    env = os.environ.get("PARVAANA_CONNECTOR_MANAGER_URL")
    if env:
        return env.rstrip("/")
    try:
        out = subprocess.check_output(
            [
                "docker",
                "inspect",
                "parvaana-connector-manager",
                "--format",
                "{{range $k,$v := .NetworkSettings.Networks}}{{$k}}={{$v.IPAddress}} {{end}}",
            ],
            text=True,
            timeout=5,
        ).strip()
    except Exception:
        return None
    for part in out.split():
        if "parvaana" in part and "=" in part:
            ip = part.split("=", 1)[1]
            if ip:
                return f"http://{ip}:3004"
    for part in out.split():
        if "=" in part and part.split("=", 1)[1]:
            return f"http://{part.split('=',1)[1]}:3004"
    return None


class ParvaanaClient:
    def __init__(
        self,
        searcher_url: Optional[str] = None,
        connector_url: Optional[str] = None,
        timeout: float = 60.0,
    ):
        self.searcher_url = (searcher_url or discover_searcher_url() or "").rstrip("/")
        self.connector_url = (connector_url or discover_connector_manager_url() or "").rstrip("/")
        self.timeout = timeout
        self.source_map_path = Path(
            os.environ.get(
                "PRODUCT_PARVAANA_SOURCE_MAP",
                "/home/library/product-v1/data/parvaana-source-map.json",
            )
        )

    def load_source_map(self) -> dict[str, str]:
        if not self.source_map_path.exists():
            return {}
        try:
            return json.loads(self.source_map_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def health(self) -> dict[str, Any]:
        if not self.searcher_url:
            return {"ok": False, "error": "searcher URL not discovered"}
        try:
            req = urllib.request.Request(f"{self.searcher_url}/health", method="GET")
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read().decode())
            return {"ok": data.get("status") == "healthy", "url": self.searcher_url, "raw": data}
        except Exception as e:
            return {"ok": False, "error": str(e), "url": self.searcher_url}

    def trigger_sync(self, source_id: str) -> dict[str, Any]:
        if not self.connector_url:
            return {"ok": False, "error": "connector-manager not discovered"}
        url = f"{self.connector_url}/sync/{source_id}"
        req = urllib.request.Request(
            url, data=b"{}", method="POST", headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return {"ok": True, **json.loads(r.read().decode() or "{}")}
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:300]
            # 409 = already running
            return {"ok": e.code in (200, 409), "status": e.code, "body": body}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def search(
        self,
        query: str,
        *,
        source_ids: Optional[list[str]] = None,
        limit: int = 20,
        mode: str = "fulltext",
    ) -> dict[str, Any]:
        """Call Parvaana searcher. Filter results client-side by source_id if provided.

        Note: searcher scopes by user permissions; without auth it often returns all
        active org/user sources the service can see. We filter by source_ids for
        project/repo isolation.
        """
        if not self.searcher_url:
            raise RuntimeError("Parvaana searcher not available")
        body = {
            "query": query,
            "limit": min(max(int(limit), 1), 100),
            "mode": mode,
            "include_facets": False,
        }
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"{self.searcher_url}/search",
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"searcher HTTP {e.code}: {e.read()[:200]}") from e

        results = raw.get("results") or []
        allowed = set(source_ids or [])
        hits = []
        for item in results:
            doc = item.get("document") or item
            sid = doc.get("source_id") or ""
            if allowed and sid not in allowed:
                continue
            meta = doc.get("metadata") or {}
            path = meta.get("path") or doc.get("external_id") or ""
            title = doc.get("title") or meta.get("title") or path
            snippet = (
                item.get("snippet")
                or _highlights_text(item.get("highlights"))
                or (doc.get("content") or "")[:800]
            )
            if not snippet and isinstance(item.get("content"), str):
                snippet = item["content"][:800]
            # Parvaana may index metadata without body; read host file under /repos → /home/library/code
            if not snippet and path:
                snippet = _read_repos_file_snippet(path, max_chars=1200) or ""
            hits.append(
                {
                    "id": doc.get("id"),
                    "source_id": sid,
                    "title": title,
                    "path": path,
                    "external_id": doc.get("external_id"),
                    "snippet": snippet,
                    "content_type": doc.get("content_type"),
                    "score": item.get("score"),
                    "doc_type": "parvaana_document",
                }
            )
        return {
            "query": query,
            "count": len(hits),
            "results": hits,
            "backend": "parvaana_searcher",
            "source_filter": list(allowed) if allowed else None,
            "raw_result_count": len(results),
        }


def _highlights_text(highlights: Any) -> str:
    if not highlights:
        return ""
    if isinstance(highlights, list):
        parts = []
        for h in highlights:
            if isinstance(h, str):
                parts.append(h)
            elif isinstance(h, dict):
                parts.append(str(h.get("text") or h.get("snippet") or ""))
        return " … ".join(p for p in parts if p)[:800]
    return str(highlights)[:800]


def _read_repos_file_snippet(path: str, max_chars: int = 1200) -> Optional[str]:
    """Map /repos/... (container) → /home/library/code/... (host bind mount)."""
    p = (path or "").strip()
    if not p:
        return None
    candidates = []
    if p.startswith("/repos/"):
        candidates.append("/home/library/code/" + p[len("/repos/") :])
    candidates.append(p)
    for host_path in candidates:
        try:
            fp = Path(host_path)
            if fp.is_file() and fp.stat().st_size < 2_000_000:
                text = fp.read_text(encoding="utf-8", errors="replace")
                return text[:max_chars]
        except Exception:
            continue
    return None
