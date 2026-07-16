"""Lightweight web search for product Ask — general knowledge alongside repo evidence.

Uses duckduckgo-search when available; degrades gracefully if offline/blocked.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

log = logging.getLogger("product.web_search")


def needs_web(question: str, *, intent: str = "") -> bool:
    """Heuristic: theoretical, design, comparison, or explicit online ask."""
    q = question or ""
    if re.search(
        r"\b(online|web\s+search|look\s+up|according\s+to\s+(docs?|documentation)|"
        r"best\s+practice|industry|standard|official)\b",
        q,
        re.I,
    ):
        return True
    if intent in ("hypothetical",):
        return True
    if re.search(
        r"\b(how\s+would|what\s+if|theoretically|compare|vs\.?|versus|"
        r"should\s+we|design\s+a|migrate|helm|kubernetes|terraform)\b",
        q,
        re.I,
    ):
        return True
    return False


def search_web(query: str, *, max_results: int = 6) -> dict[str, Any]:
    """Return {ok, results:[{title,url,snippet}], source, error?}."""
    q = (query or "").strip()
    if not q:
        return {"ok": False, "results": [], "source": "none", "error": "empty query"}

    DDGS = None
    try:
        from ddgs import DDGS as _DDGS

        DDGS = _DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS as _DDGS

            DDGS = _DDGS
        except ImportError:
            return {
                "ok": False,
                "results": [],
                "source": "none",
                "error": "ddgs / duckduckgo-search not installed",
            }

    results: list[dict[str, str]] = []
    try:
        with DDGS() as ddgs:
            for item in ddgs.text(q, max_results=max_results):
                if not isinstance(item, dict):
                    continue
                results.append(
                    {
                        "title": str(item.get("title") or "")[:200],
                        "url": str(item.get("href") or item.get("link") or "")[:500],
                        "snippet": str(item.get("body") or item.get("snippet") or "")[
                            :600
                        ],
                    }
                )
    except Exception as e:
        log.warning("web search failed: %s", e)
        return {"ok": False, "results": [], "source": "ddg", "error": str(e)}

    return {
        "ok": len(results) > 0,
        "results": results,
        "source": "duckduckgo",
        "query": q,
    }


def format_web_for_prompt(bundle: dict[str, Any], *, start_n: int = 1) -> tuple[str, list[dict]]:
    """Serialize web hits for LLM + citation list (doc_type=web)."""
    if not bundle or not bundle.get("results"):
        err = (bundle or {}).get("error") or "no web results"
        return f"(web search unavailable: {err})\n", []
    lines = [f"Web search query: {bundle.get('query') or '?'}\n"]
    cites = []
    n = start_n
    for r in bundle["results"]:
        lines.append(
            f"[{n}] WEB {r.get('title')}\n"
            f"url: {r.get('url')}\n"
            f"{r.get('snippet')}\n"
        )
        cites.append(
            {
                "n": n,
                "title": r.get("title") or "web",
                "path": r.get("url") or "",
                "repo_name": "web",
                "doc_type": "web",
                "url": r.get("url"),
                "snippet": r.get("snippet") or "",
            }
        )
        n += 1
    return "\n".join(lines), cites
