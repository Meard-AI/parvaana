"""HugeGraph Gremlin client — how GitAtlas graph is queried for product tools."""
from __future__ import annotations

import gzip
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Optional

log = logging.getLogger("product.gremlin")

DEFAULT_URL = os.environ.get(
    "GITATLAS_HUGEGRAPH_URL",
    "http://127.0.0.1:18080/graphs/hugegraph",
)


class GremlinError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def parse_server_and_graph(url: str) -> tuple[str, str]:
    """http://host:18080/graphs/hugegraph → (http://host:18080, hugegraph)."""
    u = (url or "").rstrip("/")
    if "/graphs/" in u:
        base, graph = u.split("/graphs/", 1)
        return base, graph or "hugegraph"
    return u, "hugegraph"


class GremlinClient:
    def __init__(self, hugegraph_url: Optional[str] = None, timeout: float = 30.0):
        self.hugegraph_url = hugegraph_url or DEFAULT_URL
        self.server, self.graph = parse_server_and_graph(self.hugegraph_url)
        self.timeout = timeout
        self.gremlin_url = f"{self.server}/gremlin"

    def execute(self, query: str, bindings: Optional[dict[str, Any]] = None) -> list[Any]:
        payload = {
            "gremlin": query,
            "language": "gremlin-groovy",
            "bindings": bindings or {},
            "aliases": {
                "graph": self.graph,
                "g": f"__g_{self.graph}",
            },
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.gremlin_url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:400]
            raise GremlinError(f"HTTP {e.code}: {body}", e.code) from e
        except Exception as e:
            raise GremlinError(f"gremlin unreachable: {e}") from e

        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise GremlinError(f"bad gremlin JSON: {e}") from e

        status = (parsed.get("status") or {}).get("code")
        if status not in (None, 200, 0):
            msg = (parsed.get("status") or {}).get("message") or parsed
            raise GremlinError(f"gremlin status {status}: {msg}")

        result = parsed.get("result") or {}
        data_out = result.get("data")
        if data_out is None:
            return []
        if not isinstance(data_out, list):
            return [data_out]
        return data_out

    def health(self) -> dict[str, Any]:
        try:
            counts = self.execute("g.V().label().groupCount()")
            labels = counts[0] if counts and isinstance(counts[0], dict) else {}
            return {
                "ok": True,
                "server": self.server,
                "graph": self.graph,
                "vertex_labels": labels,
            }
        except Exception as e:
            return {"ok": False, "error": str(e), "server": self.server, "graph": self.graph}
