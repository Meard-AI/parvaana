# Handoff — Product Ask / Graph / UI (2026-07-16)

**Status when stopped:** product API **stopped** on purpose. Parvaana / HugeGraph / other docker left **running** (not killed).  
**Pick up:** restart product, hard-refresh UI, re-run acceptance questions below.

---

## 1. What this product is

**product-v1** = face for multi-repo knowledge on library:

| Layer | Role | Port / path |
|-------|------|-------------|
| Product UI + API | Project/repo scope Ask + Search | `http://127.0.0.1:3847/` · code `/home/library/product-v1/` |
| Parvaana | Content brain (searcher + optional AI) | web 3002, Caddy 8080, AI often ~3003 |
| GitAtlas → HugeGraph | Temporal graph (Commit–MODIFIED–File, Author) | Gremlin **18080**, Hubble 18088 |

**IA (locked):** Workspaces = projects only. No install-wide search. Project multi-repo; repo single-repo.

**Default project:** HugeGraph `504faa65b22a440d`  
**Repos (family):** hugegraph, hugegraph-ai, hugegraph-computer, hugegraph-toolchain, hugegraph-doc  

Clones under: `/home/library/product-v1/repos/504faa65b22a440d/`  
Parvaana FS sources often under `/home/library/code/hg-family/` (linked).

---

## 2. How to start / stop

### Start product API
```bash
cd /home/library/product-v1/backend
# use project venv
/home/library/product-v1/.venv/bin/python3 -m uvicorn app:app --host 0.0.0.0 --port 3847
# or background:
nohup /home/library/product-v1/.venv/bin/python3 -m uvicorn app:app --host 0.0.0.0 --port 3847 \
  > /tmp/product-v1.log 2>&1 &
```

### Stop product API only
```bash
ps -eo pid,cmd | awk '/product-v1\/.venv\/bin\/python3 -m uvicorn app:app/ && !/awk/ {print $1}' \
  | while read p; do kill "$p"; done
```
Do **not** use naive `pkill -f uvicorn app:app` from a shell whose command line contains that string (can kill the wrapper).

### Health
```bash
curl -sS http://127.0.0.1:3847/health | python3 -m json.tool
```

### Tests
```bash
cd /home/library/product-v1
.venv/bin/python -m pytest tests/ -q
# Last green count: ~50 passed
```

### UI
Open `http://127.0.0.1:3847/` — hard-refresh after static changes (`frontend/assets/*`).

---

## 3. Architecture of Ask (important)

```
User question
    │
    ├─ classify_intent → temporal | content | hybrid | hypothetical
    │
    ├─ Parvaana search (scoped) + local file dossier (content_tools)
    ├─ GitAtlas Gremlin tools (graph_tools) for who/when/path history
    ├─ Optional web search (web_search / ddgs) for design / “how would”
    │
    ├─ temporal only → deterministic golden commit card (no LLM)
    │
    └─ else → LLM (Parvaana /prompt or OpenAI-compatible) with evidence
              + sanitize (strip prompt leaks)
```

**Design principle (final):**  
- Retrieval + graph + optional web = **evidence**.  
- **Model reasons** on that evidence (not hard-coded essays for content).  
- Deterministic only for **commit cards** (author/when/short sha/files) and offline fallbacks.

---

## 4. Backend modules

| File | Purpose |
|------|---------|
| `backend/app.py` | FastAPI routes: catalog, search, jobs, `/ai/*`, settings, static UI |
| `backend/ai_service.py` | Chat orchestration, prompts, sanitize, provider resolve |
| `backend/content_tools.py` | Intent, discover files, compose summary extract, dossier |
| `backend/graph_tools.py` | Gremlin path→commits, blast, message grep, enrich WHO/WHEN |
| `backend/gremlin_client.py` | HugeGraph Gremlin HTTP client |
| `backend/parvaana_client.py` | Searcher client + source map |
| `backend/catalog.py` | SQLite catalog projects/repos/FTS |
| `backend/jobs.py` | Clone / index / ingest |
| `backend/settings_store.py` | AI settings (keys masked) |
| `backend/providers_catalog.py` | Hermes-style provider presets + base URLs + models |
| `backend/web_search.py` | DuckDuckGo (`ddgs`) for theoretical questions |

### Key API endpoints
- `POST /ai/chat` — `{ message, scope: {type,id}, model? }`
- `GET /ai/status` — active provider/model chip data + `active_display`
- `GET /ai/providers` — catalog
- `GET /ai/models?fetch=true|false` — models for current provider (+ remote `/models`)
- `GET|PATCH /settings` — provider, base URL, key, model, `enable_web_search`
- `POST /search` — project/repo scoped search

### Settings keys (SQLite `settings` table)
- `ai_provider`: auto | parvaana | openai_compatible | disabled  
- `api_provider_id`: auto | parvaana | openai | minimax | xai | openrouter | … | custom  
- `openai_base_url`, `openai_api_key`, `openai_model`  
- `enable_web_search`: `"1"` (default) / `"0"` to disable web  
- `parvaana_base_url` optional override  

**When stopped, catalog settings roughly:**  
`api_provider_id` / model may still say MiniMax defaults in DB while **active** runtime with Auto was **Parvaana Llama** (no MiniMax key). Chip shows **resolved** backend; saved defaults can still be MiniMax until user Saves a provider+key.

---

## 5. Frontend

| Path | Notes |
|------|--------|
| `frontend/index.html` | Shell, settings, Ask composers, model selects |
| `frontend/assets/app.js` | Tree, chat, markdown (marked+DOMPurify), sources dropdown, providers UI, shortcuts |
| `frontend/assets/app.css` | Dark ink + copper; responsive rail; split msg + cite rail |

**UI features shipped:**
- Ask primary; markdown rendering  
- Sources rail (wider) with **expandable code** per citation (`body` on cites)  
- Settings: provider catalog fills base URL; API key save; model list; refresh models  
- Composer model dropdown syncs to **available models for active provider**  
- AI chip: `AI · {provider} · {model}` from **resolved** backend  
- Shortcuts: ⌘/Ctrl+Enter send, ⌘/Ctrl+, settings, ⌘/Ctrl+K focus Ask, Esc, ?  

---

## 6. Golden answer shapes (acceptance)

### A) Content — “How does Docker Compose work?”
- LLM over dossier + search (not hard-coded essay as primary path)  
- Mentions real services (pd/store/server) and real paths  
- Prefer short bullets; YAML in **Sources** dropdown, not full dump in body  
- No prompt leaks (`Graph membership`, `workspace_path_inventory`, etc.)

### B) Temporal — “Who last touched docker/docker-compose.yml?”
- Mode `deterministic_graph`  
- Short sha (e.g. `ef5d4e0`), Author, When, files (repo → path), what changed  
- Not 40-char hex as hero  

### C) Hypothetical — “Compose → Helm?” / “How would Helm work?”
- Intent `hypothetical`  
- Dossier: Chart.yaml if any + compose stacks  
- Optional web (`web_search.used`) for industry tools  
- Answer: what exists → map to chart → optional Kompose/Katenary last  
- Prefer workspace services over pure web tool ads  

### D) Hybrid — how + when in one question
- Intent `hybrid`; model + injected commit evidence (or appendix)  

---

## 7. GitAtlas / graph notes

- Schema Commit props: `message` + (after fix) `authored_at`, `author_name`, `author_email`  
- Transformer writes temporal props; live schema was **appended** for those keys  
- Graph is still **sparse** (~tens of commits on demo index) — many compose files share root-ish commit `ef5d4e0`  
- Enrichment from **local git** fills WHO/WHEN/diff for cards  
- **Deeper reindex** needed for real multi-commit upgrade timelines  

Gremlin probe:
```text
g.V().hasLabel('File').hasId('file:docker/docker-compose.yml').in('MODIFIED').valueMap(true)
```

---

## 8. Dependencies

Product venv: `/home/library/product-v1/.venv/`  
Notable: `httpx`, `fastapi`/`uvicorn`, `playwright` (tests), `ddgs` (web search).

```bash
/home/library/product-v1/.venv/bin/pip install ddgs  # if web search empty
```

---

## 9. What was fixed this session (summary)

1. Golden commit cards (WHO/WHEN/short id/files/what-changed)  
2. Content DeepWiki-style multi-repo compose (then shortened; YAML → sources)  
3. Intent: content / temporal / hybrid / hypothetical  
4. Hybrid compound questions (“how + when”)  
5. UI redesign: markdown, sources dropdown, providers, model bar, responsive  
6. Model chip vs Ask bar inconsistency (resolved display)  
7. Hermes-style provider catalog + key save + model list  
8. Model-first answers + optional web search (less hard-coded essays)  
9. Anti-leak sanitizer for prompt scaffolding  
10. Self-test loops until acceptance PASS on core Qs  

---

## 10. Known gaps / next work

| Gap | Notes |
|-----|--------|
| Sparse GitAtlas history | Full reindex for multi-commit timelines |
| LLM quality | Llama 3.1 8B weak on long design; use MiniMax/GPT/Grok via Settings + key |
| Junk UI projects | `UIAddRepo_*` in catalog; purge or hide |
| Personal vs HugeGraph scope | Ensure UI default is HugeGraph; don’t answer family repos under wrong project |
| Agent tool loop | Still fixed tools + one LLM call, not multi-step agent |
| Web search reliability | DDG can rate-limit; `enable_web_search=0` to disable |
| Gitatlas EnsureSchema | append temporal props on Commit; re-run index after schema |

---

## 11. Smoke checklist (when you resume)

```bash
# start API
cd /home/library/product-v1/backend
nohup /home/library/product-v1/.venv/bin/python3 -m uvicorn app:app --host 0.0.0.0 --port 3847 > /tmp/product-v1.log 2>&1 &

curl -sS http://127.0.0.1:3847/health | python3 -m json.tool
cd /home/library/product-v1 && .venv/bin/python -m pytest tests/ -q

# API smokes (HugeGraph project id)
PID=504faa65b22a440d
curl -sS -X POST http://127.0.0.1:3847/ai/chat -H 'Content-Type: application/json' \
  -d "{\"message\":\"Who last touched docker/docker-compose.yml?\",\"scope\":{\"type\":\"project\",\"id\":\"$PID\"}}"
# expect: deterministic_graph, ef5d4e0, Author, When

curl -sS -X POST http://127.0.0.1:3847/ai/chat -H 'Content-Type: application/json' \
  -d "{\"message\":\"How does Docker Compose work in HugeGraph?\",\"scope\":{\"type\":\"project\",\"id\":\"$PID\"}}"
# expect: llm, pd/store, docker-compose paths, no Graph membership leak

curl -sS -X POST http://127.0.0.1:3847/ai/chat -H 'Content-Type: application/json' \
  -d "{\"message\":\"Convert Docker Compose to a Helm chart?\",\"scope\":{\"type\":\"project\",\"id\":\"$PID\"}}"
# expect: llm + hypothetical, real services + optional web, no prompt dump
```

UI: open HugeGraph → Ask each of the three; Sources rail expand for code; Settings → pick provider + key → Save → chip matches.

---

## 12. Related docs

- `TRUE-PRODUCT-PLAN.md`, `DETAILED-TODO.md`, `GRAPHRAG-RESEARCH.md`  
- `SESSION_CONTINUE_DONE.md`  
- `/home/library/docs/PRODUCT-V1-PLAN.md`, `PRODUCT-V1-OPS.md`  
- Dogfood: `/home/library/product-v1/dogfood-output/` (screenshots + report)  

---

## 13. Process state at handoff

| Process | State |
|---------|--------|
| product-v1 uvicorn `:3847` | **STOPPED** |
| Parvaana / HugeGraph docker | Left running (not stopped) |
| Scheduled self-test jobs | None created (no interval was set for `/loop`) |

**Date:** 2026-07-16  
**Owner next:** resume from §11 smoke checklist.
