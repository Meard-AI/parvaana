# parvaana (product control plane)

**Project-scoped knowledge workspace** for multi-repo git: **Search + Ask** grounded in Parvaana content + GitAtlas temporal graph.

## Meard-AI stack

| Repo | Role |
|------|------|
| **[Meard-AI/parvaana](https://github.com/Meard-AI/parvaana)** (this) | Product UI + API (Ask, Sources, Settings) |
| **[Meard-AI/gitatlas](https://github.com/Meard-AI/gitatlas)** | Temporal Git → HugeGraph indexer/CLI |
| **[Meard-AI/parvaana-platform](https://github.com/Meard-AI/parvaana-platform)** | Workplace AI agent platform (searcher, connectors, agents) |

| Engine | Role in Ask |
|--------|-------------|
| **Parvaana platform** | Content brain (searcher + optional AI) |
| **GitAtlas → HugeGraph** | Temporal graph (commit ↔ file ↔ author) |

This repo is the **product control plane** (UI + API) formerly developed as `product-v1` / `parvaana-v1` on the library host.

## Layout

```
parvaana-v1/
├── backend/           # FastAPI app (Ask, search, graph tools, settings)
│   ├── app.py
│   ├── ai_service.py
│   ├── content_tools.py
│   ├── graph_tools.py
│   ├── gremlin_client.py
│   ├── parvaana_client.py
│   ├── providers_catalog.py
│   ├── web_search.py
│   ├── catalog.py
│   ├── jobs.py
│   └── settings_store.py
├── frontend/          # Static UI (Ask, Sources, Settings, providers)
│   ├── index.html
│   └── assets/{app.js,app.css}
├── tests/             # pytest suite
├── scripts/           # helpers
├── docs/              # handoff / ops notes
├── data/              # runtime only (gitignored DBs)
├── requirements.txt
└── .env.example
```

## Quick start (library-style)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cd backend
uvicorn app:app --host 0.0.0.0 --port 3847
```

Open `http://127.0.0.1:3847/`.

**Depends on local services** (optional but expected for full features):

- Parvaana searcher / AI (docker)
- HugeGraph Gremlin (`:18080`) for temporal graph tools

## Ask modes

| Intent | Behavior |
|--------|----------|
| **content** | Search + file dossier → LLM reasons over evidence |
| **temporal** | Golden commit card (WHO / WHEN / short sha / files) via Gremlin+git |
| **hybrid** | Content + history |
| **hypothetical** | Repo evidence + optional web search → design-style answer |

## Settings

Hermes-style provider catalog (OpenAI, MiniMax, xAI, OpenRouter, …): pick provider → base URL → API key → model.  
AI chip shows **resolved** backend (`AI · Parvaana · llama-…` vs API providers).

## Tests

```bash
pytest tests/ -q
```

## Docs

See `docs/HANDOFF.md` for full library-host handoff (ports, smoke checks, known gaps).

## License

Application code in this tree follows the same operational use as the library product install.  
Upstream Parvaana engine remains separate: https://github.com/bitflicker64/parvaana
