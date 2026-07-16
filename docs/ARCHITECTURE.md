# Architecture

```
Browser → product UI (:3847)
              │
              ▼
         FastAPI (backend/app.py)
         ├── Search → Parvaana searcher (scoped) / FTS fallback
         ├── Ask → ai_service
         │         ├── intent: content | temporal | hybrid | hypothetical
         │         ├── content_tools: discover/read compose, helm, files
         │         ├── graph_tools: Gremlin File←MODIFIED←Commit + git enrich
         │         ├── web_search: optional DDG for design questions
         │         └── LLM: Parvaana /prompt or OpenAI-compatible
         └── Settings → providers_catalog + settings_store (SQLite)
```

Temporal “who changed X” uses **deterministic** commit cards.  
Content/design uses **LLM on retrieved evidence** (not hard-coded essays).
