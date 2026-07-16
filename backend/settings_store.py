"""Product settings (AI provider config). Secrets stay local; API never echoes full keys."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional


DEFAULTS = {
    "ai_provider": "auto",  # auto | parvaana | openai_compatible | disabled
    # Catalog id: auto | parvaana | openai | minimax | xai | openrouter | custom | …
    "api_provider_id": "auto",
    "parvaana_base_url": "",  # empty = auto-detect docker IP
    # Default OpenAI-compatible path (filled when user picks a catalog provider)
    "openai_base_url": "https://api.minimax.chat/v1",
    "openai_api_key": "",
    "openai_model": "MiniMax-M3",
    "system_prompt_extra": "",
    # "1" = allow DuckDuckGo web search for theoretical / design questions
    "enable_web_search": "1",
}


class SettingsStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c

    def _init(self) -> None:
        with self._connect() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            c.commit()

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._connect() as c:
            row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default if default is not None else DEFAULTS.get(key)
        return row["value"]

    def set(self, key: str, value: str) -> None:
        with self._connect() as c:
            c.execute(
                """
                INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, time.time()),
            )
            c.commit()

    def get_all_raw(self) -> dict[str, str]:
        out = dict(DEFAULTS)
        with self._connect() as c:
            for row in c.execute("SELECT key, value FROM settings"):
                out[row["key"]] = row["value"]
        return out

    def public_view(self) -> dict[str, Any]:
        """Safe for API responses — mask secrets."""
        raw = self.get_all_raw()
        key = raw.get("openai_api_key") or ""
        masked = ""
        if key:
            masked = ("*" * max(0, len(key) - 4)) + key[-4:] if len(key) >= 4 else "****"
        return {
            "ai_provider": raw.get("ai_provider") or "auto",
            "api_provider_id": raw.get("api_provider_id") or "auto",
            "parvaana_base_url": raw.get("parvaana_base_url") or "",
            "openai_base_url": raw.get("openai_base_url") or "",
            "openai_api_key_set": bool(key),
            "openai_api_key_masked": masked,
            "openai_model": raw.get("openai_model") or "",
            "system_prompt_extra": raw.get("system_prompt_extra") or "",
            "enable_web_search": (raw.get("enable_web_search") or "1") not in (
                "0",
                "false",
                "no",
            ),
        }

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "ai_provider",
            "api_provider_id",
            "parvaana_base_url",
            "openai_base_url",
            "openai_api_key",
            "openai_model",
            "system_prompt_extra",
            "enable_web_search",
        }
        patch = dict(patch or {})
        # Catalog provider pick fills base URL + default model (unless user overrode in same request)
        pid = patch.get("api_provider_id")
        if pid and str(pid).strip():
            try:
                from providers_catalog import apply_provider_defaults

                defaults = apply_provider_defaults(str(pid).strip())
                user_url = bool((patch.get("openai_base_url") or "").strip())
                user_model = bool((patch.get("openai_model") or "").strip())
                for dk, dv in defaults.items():
                    if dk == "openai_base_url" and user_url:
                        continue
                    if dk == "openai_model" and user_model:
                        continue
                    patch[dk] = dv
            except Exception:
                pass

        for k, v in patch.items():
            if k not in allowed:
                continue
            if k == "openai_api_key" and (v is None or v == "" or str(v).startswith("*")):
                # ignore empty/masked — keep existing
                continue
            self.set(k, "" if v is None else str(v))
        return self.public_view()
