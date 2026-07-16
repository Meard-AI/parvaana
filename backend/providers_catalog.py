"""OpenAI-compatible provider catalog (Hermes-style presets).

Pick a provider → base URL fills in → paste API key → choose model.
Parvaana remains the local default when Auto / Parvaana is selected.
"""
from __future__ import annotations

from typing import Any, Optional

# Curated list — transport is openai_chat unless noted (product uses OpenAI-compatible /chat/completions).
PROVIDERS: list[dict[str, Any]] = [
    {
        "id": "auto",
        "name": "Auto",
        "description": "Parvaana on this host if healthy, else the configured API key provider.",
        "kind": "auto",
        "base_url": "",
        "models": [],
        "needs_key": False,
    },
    {
        "id": "parvaana",
        "name": "Parvaana AI (this host)",
        "description": "Local Parvaana /prompt (NVIDIA NIM). No external API key.",
        "kind": "parvaana",
        "base_url": "",  # auto-discovered
        "models": [
            "meta/llama-3.1-8b-instruct",
            "meta/llama-3.1-70b-instruct",
        ],
        "needs_key": False,
    },
    {
        "id": "openai",
        "name": "OpenAI",
        "description": "api.openai.com",
        "kind": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "o4-mini"],
        "needs_key": True,
    },
    {
        "id": "minimax",
        "name": "MiniMax",
        "description": "Long-context MiniMax (OpenAI-compatible)",
        "kind": "openai_compatible",
        "base_url": "https://api.minimax.chat/v1",
        "models": ["MiniMax-M3", "MiniMax-M2", "MiniMax-Text-01"],
        "needs_key": True,
    },
    {
        "id": "xai",
        "name": "xAI (Grok)",
        "description": "api.x.ai",
        "kind": "openai_compatible",
        "base_url": "https://api.x.ai/v1",
        "models": ["grok-4", "grok-4.5", "grok-3", "grok-3-mini", "grok-2"],
        "needs_key": True,
    },
    {
        "id": "openrouter",
        "name": "OpenRouter",
        "description": "Aggregator — many models via one key",
        "kind": "openai_compatible",
        "base_url": "https://openrouter.ai/api/v1",
        "models": [
            "openai/gpt-4o-mini",
            "anthropic/claude-sonnet-4",
            "google/gemini-2.5-pro",
            "meta-llama/llama-3.3-70b-instruct",
            "x-ai/grok-3",
        ],
        "needs_key": True,
    },
    {
        "id": "anthropic",
        "name": "Anthropic (OpenAI-compat proxy)",
        "description": "Only if you use a gateway that speaks OpenAI chat.completions",
        "kind": "openai_compatible",
        "base_url": "https://api.anthropic.com/v1",
        "models": ["claude-sonnet-4-20250514", "claude-opus-4-20250514", "claude-3-5-haiku-latest"],
        "needs_key": True,
    },
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "description": "api.deepseek.com",
        "kind": "openai_compatible",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "needs_key": True,
    },
    {
        "id": "groq",
        "name": "Groq",
        "description": "Fast inference",
        "kind": "openai_compatible",
        "base_url": "https://api.groq.com/openai/v1",
        "models": [
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "mixtral-8x7b-32768",
            "gemma2-9b-it",
        ],
        "needs_key": True,
    },
    {
        "id": "together",
        "name": "Together AI",
        "description": "together.xyz",
        "kind": "openai_compatible",
        "base_url": "https://api.together.xyz/v1",
        "models": [
            "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
            "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
            "Qwen/Qwen2.5-72B-Instruct-Turbo",
        ],
        "needs_key": True,
    },
    {
        "id": "fireworks",
        "name": "Fireworks",
        "description": "fireworks.ai",
        "kind": "openai_compatible",
        "base_url": "https://api.fireworks.ai/inference/v1",
        "models": [
            "accounts/fireworks/models/llama-v3p1-70b-instruct",
            "accounts/fireworks/models/llama-v3p3-70b-instruct",
        ],
        "needs_key": True,
    },
    {
        "id": "mistral",
        "name": "Mistral",
        "description": "api.mistral.ai",
        "kind": "openai_compatible",
        "base_url": "https://api.mistral.ai/v1",
        "models": ["mistral-large-latest", "mistral-small-latest", "codestral-latest"],
        "needs_key": True,
    },
    {
        "id": "google",
        "name": "Google AI (OpenAI-compat)",
        "description": "generativelanguage.googleapis.com OpenAI endpoint",
        "kind": "openai_compatible",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "models": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"],
        "needs_key": True,
    },
    {
        "id": "nvidia",
        "name": "NVIDIA NIM",
        "description": "integrate.api.nvidia.com",
        "kind": "openai_compatible",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "models": [
            "meta/llama-3.1-8b-instruct",
            "meta/llama-3.1-70b-instruct",
            "nvidia/llama-3.1-nemotron-70b-instruct",
        ],
        "needs_key": True,
    },
    {
        "id": "huggingface",
        "name": "Hugging Face",
        "description": "router.huggingface.co",
        "kind": "openai_compatible",
        "base_url": "https://router.huggingface.co/v1",
        "models": [
            "meta-llama/Llama-3.3-70B-Instruct",
            "Qwen/Qwen2.5-72B-Instruct",
        ],
        "needs_key": True,
    },
    {
        "id": "ollama",
        "name": "Ollama (local)",
        "description": "Local Ollama OpenAI-compatible server",
        "kind": "openai_compatible",
        "base_url": "http://127.0.0.1:11434/v1",
        "models": ["llama3.1", "llama3.2", "qwen2.5", "mistral"],
        "needs_key": False,  # often no key; send dummy if required
    },
    {
        "id": "lmstudio",
        "name": "LM Studio (local)",
        "description": "Local LM Studio server",
        "kind": "openai_compatible",
        "base_url": "http://127.0.0.1:1234/v1",
        "models": ["local-model"],
        "needs_key": False,
    },
    {
        "id": "custom",
        "name": "Custom OpenAI-compatible",
        "description": "Any /v1 base URL (LiteLLM, vLLM, proxy, …)",
        "kind": "openai_compatible",
        "base_url": "",
        "models": [],
        "needs_key": True,
    },
]

_BY_ID = {p["id"]: p for p in PROVIDERS}


def list_providers() -> list[dict[str, Any]]:
    """Public catalog (no secrets)."""
    out = []
    for p in PROVIDERS:
        out.append(
            {
                "id": p["id"],
                "name": p["name"],
                "description": p.get("description") or "",
                "kind": p["kind"],
                "base_url": p.get("base_url") or "",
                "models": list(p.get("models") or []),
                "needs_key": bool(p.get("needs_key")),
            }
        )
    return out


def get_provider(provider_id: str) -> Optional[dict[str, Any]]:
    return _BY_ID.get((provider_id or "").strip().lower())


def models_for_provider(provider_id: str) -> list[str]:
    p = get_provider(provider_id)
    if not p:
        return []
    return list(p.get("models") or [])


def apply_provider_defaults(provider_id: str) -> dict[str, str]:
    """Return settings patch fields when user picks a catalog provider."""
    p = get_provider(provider_id)
    if not p:
        return {}
    pid = p["id"]
    if pid == "auto":
        return {"ai_provider": "auto", "api_provider_id": "auto"}
    if pid == "parvaana":
        return {
            "ai_provider": "parvaana",
            "api_provider_id": "parvaana",
        }
    models = p.get("models") or []
    patch = {
        "ai_provider": "openai_compatible",
        "api_provider_id": pid,
        "openai_base_url": p.get("base_url") or "",
    }
    if models:
        patch["openai_model"] = models[0]
    return patch
