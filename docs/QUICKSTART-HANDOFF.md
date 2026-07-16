# Product v1 Ask/Graph handoff (library)

Canonical long-form handoff:

**`/home/library/obsidian/20_Knowledge/GitAtlas-Parvaana/HANDOFF-2026-07-16-ASK-GRAPH.md`**

### Quick resume
```bash
cd /home/library/product-v1/backend
nohup /home/library/product-v1/.venv/bin/python3 -m uvicorn app:app --host 0.0.0.0 --port 3847 > /tmp/product-v1.log 2>&1 &
curl -sS http://127.0.0.1:3847/health
cd /home/library/product-v1 && .venv/bin/python -m pytest tests/ -q
```

UI: `http://127.0.0.1:3847/` (hard-refresh).

**Stopped on handoff:** product API only. Parvaana/HugeGraph left as-is.
