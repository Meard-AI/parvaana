/**
 * Atlas product UI — project|repo scope, Ask primary, markdown answers, model picker, shortcuts.
 */
const LS_PROJECT = "product.v1.selectedProjectId";
const LS_REPO = "product.v1.selectedRepoId";
const LS_MODEL = "product.v1.chatModel";

const state = {
  projects: [],
  defaultProjectId: null,
  selectedProjectId: null,
  selectedRepoId: null,
  overview: null,
  expanded: new Set(),
  view: "empty",
  aiStatus: null,
  projectTab: "ask",
  repoTab: "ask",
  chatModel: localStorage.getItem(LS_MODEL) || "",
  providers: [],
  availableModels: [],
  activeModel: null,
  activeProviderLabel: null,
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  const text = await res.text();
  let data;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { raw: text };
  }
  if (!res.ok) {
    const msg = (data && (data.error || data.detail)) || res.statusText;
    const err = new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    err.status = res.status;
    err.code = data?.code;
    err.data = data;
    throw err;
  }
  return data;
}

function fmtNum(n) {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1).replace(/\.0$/, "") + "M";
  if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, "") + "k";
  return String(n ?? 0);
}
function formatStats(s) {
  return `${s?.repos ?? 0} repos · ${fmtNum(s?.commits ?? 0)} commits · ${fmtNum(s?.files ?? 0)} files`;
}
function fillStatRibbon(s) {
  if ($("#st-repos")) $("#st-repos").textContent = String(s?.repos ?? 0);
  if ($("#st-commits")) $("#st-commits").textContent = fmtNum(s?.commits ?? 0);
  if ($("#st-files")) $("#st-files").textContent = fmtNum(s?.files ?? 0);
}
function shortSha(s) {
  if (!s) return "none";
  return s.length > 7 ? s.slice(0, 7) : s;
}
function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function isMod(e) {
  return e.metaKey || e.ctrlKey;
}

/* ── Markdown (GitHub-like preview) ─────────────────────── */

function renderMarkdown(src) {
  const raw = String(src ?? "");
  try {
    if (typeof marked !== "undefined" && typeof DOMPurify !== "undefined") {
      marked.setOptions({
        gfm: true,
        breaks: true,
        headerIds: false,
        mangle: false,
      });
      const html = marked.parse(raw);
      return DOMPurify.sanitize(html, {
        USE_PROFILES: { html: true },
        ADD_ATTR: ["target", "rel"],
      });
    }
  } catch (e) {
    console.warn("markdown render failed", e);
  }
  return `<pre class="md-fallback">${escapeHtml(raw)}</pre>`;
}

function showView(name) {
  state.view = name;
  ["empty", "project", "repo", "jobs", "settings"].forEach((v) => {
    $(`#view-${v}`)?.classList.toggle("hidden", v !== name);
  });
  const titles = {
    empty: "Workspaces",
    project: $("#project-name")?.textContent || "Project",
    repo: $("#repo-name")?.textContent || "Repository",
    jobs: "Jobs",
    settings: "Settings",
  };
  if ($("#topbar-title")) $("#topbar-title").textContent = titles[name] || "Atlas";
  closeRail();
}

function persistSelection() {
  if (state.selectedProjectId) localStorage.setItem(LS_PROJECT, state.selectedProjectId);
  else localStorage.removeItem(LS_PROJECT);
  if (state.selectedRepoId) localStorage.setItem(LS_REPO, state.selectedRepoId);
  else localStorage.removeItem(LS_REPO);
}

function isJunkName(name) {
  return /^Skeptic/i.test(name || "") || /Skeptic/i.test(name || "");
}

function openRail() {
  $("#rail")?.classList.add("open");
  const bd = $("#rail-backdrop");
  if (bd) bd.hidden = false;
}
function closeRail() {
  $("#rail")?.classList.remove("open");
  const bd = $("#rail-backdrop");
  if (bd) bd.hidden = true;
}

function fillSelectOptions(sel, models, { activeModel, selected, placeholder } = {}) {
  if (!sel) return;
  const prev = selected != null ? selected : sel.value;
  sel.innerHTML = "";
  const ph = document.createElement("option");
  ph.value = "";
  ph.textContent = placeholder || (activeModel ? `Active · ${activeModel}` : "Active model");
  sel.appendChild(ph);
  const seen = new Set([""]);
  for (const m of models || []) {
    if (!m || seen.has(m)) continue;
    seen.add(m);
    const opt = document.createElement("option");
    opt.value = m;
    opt.textContent = m;
    sel.appendChild(opt);
  }
  if (prev && seen.has(prev)) sel.value = prev;
  else if (activeModel && seen.has(activeModel) && !prev) sel.value = "";
  else sel.value = prev && seen.has(prev) ? prev : "";
}

function syncModelSelects() {
  const models = state.availableModels || [];
  const active = state.activeModel || state.aiStatus?.model || "";
  // Drop stale local override if it isn't on this provider's list
  if (state.chatModel && models.length && !models.includes(state.chatModel)) {
    state.chatModel = "";
    localStorage.removeItem(LS_MODEL);
  }
  fillSelectOptions($("#project-model"), models, {
    activeModel: active,
    selected: state.chatModel,
    placeholder: active ? `Active · ${shortModel(active)}` : "Active model",
  });
  fillSelectOptions($("#repo-model"), models, {
    activeModel: active,
    selected: state.chatModel,
    placeholder: active ? `Active · ${shortModel(active)}` : "Active model",
  });
  // Settings model select
  const setModel = $("#set-openai-model");
  if (setModel && setModel.tagName === "SELECT") {
    const saved = state.aiStatus?.settings?.openai_model || active || "";
    fillSelectOptions(setModel, models, {
      activeModel: saved,
      selected: saved,
      placeholder: saved || "Choose model",
    });
    if (saved && [...setModel.options].some((o) => o.value === saved)) {
      setModel.value = saved;
    }
  }
}

function shortModel(m) {
  if (!m) return "";
  const s = String(m).includes("/") ? String(m).split("/").pop() : String(m);
  return s.length > 22 ? s.slice(0, 20) + "…" : s;
}

async function loadProvidersCatalog() {
  try {
    const data = await api("/ai/providers");
    state.providers = data.providers || [];
  } catch {
    state.providers = [];
  }
  const sel = $("#set-api-provider");
  if (!sel) return;
  const cur = sel.value || state.aiStatus?.api_provider_id || "auto";
  sel.innerHTML = "";
  for (const p of state.providers) {
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = p.name;
    opt.dataset.baseUrl = p.base_url || "";
    opt.dataset.kind = p.kind || "";
    opt.dataset.needsKey = p.needs_key ? "1" : "0";
    opt.dataset.desc = p.description || "";
    sel.appendChild(opt);
  }
  if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
  else sel.value = "auto";
  updateProviderDesc();
}

function updateProviderDesc() {
  const sel = $("#set-api-provider");
  const desc = $("#set-provider-desc");
  if (!sel || !desc) return;
  const opt = sel.selectedOptions[0];
  if (!opt) return;
  const p = state.providers.find((x) => x.id === sel.value);
  const needs = p?.needs_key ? "Requires API key." : "No API key required.";
  desc.textContent = `${p?.description || opt.dataset.desc || ""} ${needs}`.trim();
  // Fill base URL from catalog unless custom and already typed
  const urlInput = $("#set-openai-url");
  if (urlInput && p && p.id !== "custom" && p.base_url) {
    urlInput.value = p.base_url;
    urlInput.readOnly = p.id !== "custom" && p.id !== "auto" && p.id !== "parvaana";
  }
  if (urlInput && (p?.id === "custom" || p?.id === "auto" || p?.id === "parvaana")) {
    urlInput.readOnly = false;
    if (p?.id === "parvaana" || p?.id === "auto") {
      // leave URL for openai path optional
    }
  }
  // Fill models from catalog immediately
  if (p?.models?.length) {
    state.availableModels = [...p.models];
    const setModel = $("#set-openai-model");
    if (setModel) {
      fillSelectOptions(setModel, p.models, {
        activeModel: p.models[0],
        selected: p.models[0],
        placeholder: "Choose model",
      });
      setModel.value = p.models[0];
    }
  }
}

async function refreshModels({ fetchRemote = true } = {}) {
  try {
    const q = fetchRemote ? "/ai/models?fetch=true" : "/ai/models?fetch=false";
    const data = await api(q);
    state.availableModels = data.models || [];
    state.activeModel = data.active_model || state.aiStatus?.model || null;
    syncModelSelects();
    return data;
  } catch (e) {
    console.warn("refresh models", e);
    return null;
  }
}

/* ── AI status ──────────────────────────────────────────── */

async function refreshAiStatus() {
  try {
    state.aiStatus = await api("/ai/status");
  } catch {
    state.aiStatus = { configured: false, detail: "AI status unavailable" };
  }
  state.activeModel = state.aiStatus.model || null;
  state.activeProviderLabel =
    state.aiStatus.provider_label || state.aiStatus.provider || null;
  if (Array.isArray(state.aiStatus.models) && state.aiStatus.models.length) {
    state.availableModels = state.aiStatus.models;
  }
  const chip = $("#ai-chip");
  if (!chip) return state.aiStatus;
  if (state.aiStatus.configured) {
    // Always show *resolved* backend (Parvaana Llama vs MiniMax API), not the stale dropdown
    chip.textContent =
      state.aiStatus.active_display ||
      `AI · ${state.activeProviderLabel || "ok"} · ${shortModel(state.activeModel || "")}`;
    chip.title = [
      state.aiStatus.provider_label || state.aiStatus.provider,
      state.aiStatus.model,
      state.aiStatus.base_url,
      state.aiStatus.detail,
    ]
      .filter(Boolean)
      .join(" · ");
    chip.className = "ai-pill ok";
  } else {
    chip.textContent = "AI not configured";
    chip.className = "ai-pill bad";
    chip.title = state.aiStatus.setup_hint || state.aiStatus.detail || "";
  }
  syncModelSelects();
  updateAiBanners();
  return state.aiStatus;
}

function updateAiBanners() {
  const st = state.aiStatus;
  const html = !st?.configured
    ? `<strong>AI not configured.</strong> ${escapeHtml(st?.setup_hint || st?.detail || "")} ` +
      `<a data-go-settings>Open Settings</a> to set API key / model.`
    : "";
  for (const id of ["#project-ai-banner", "#repo-ai-banner"]) {
    const el = $(id);
    if (!el) continue;
    if (html) {
      el.innerHTML = html;
      el.classList.remove("hidden");
      el.querySelector("[data-go-settings]")?.addEventListener("click", () => openSettings());
    } else {
      el.classList.add("hidden");
      el.innerHTML = "";
    }
  }
}

/* ── Tree / selection ───────────────────────────────────── */

async function loadProjects() {
  const data = await api("/projects");
  state.projects = (data.projects || []).filter((p) => !isJunkName(p.name));
  state.defaultProjectId = data.default_project_id;
  renderTree();
}

function renderTree() {
  const root = $("#project-tree");
  if (!root) return;
  root.innerHTML = "";
  if (!state.projects.length) {
    root.innerHTML = `<p class="muted" style="padding:0.5rem">No projects yet.</p>`;
    return;
  }
  for (const p of state.projects) {
    const open = state.expanded.has(p.id);
    const el = document.createElement("div");
    el.className = `tree-project${open ? " open" : ""}`;
    el.dataset.projectId = p.id;
    el.innerHTML = `
      <div class="tree-project-head ${state.selectedProjectId === p.id && !state.selectedRepoId ? "active" : ""}">
        <span class="caret" data-action="toggle">${open ? "▾" : "▸"}</span>
        <span class="name" data-action="select"></span>
        <span class="badge" data-action="select">${p.repo_count ?? 0}</span>
      </div>
      <div class="tree-repos" data-repos-for="${p.id}"></div>
    `;
    el.querySelector(".name").textContent = p.name;
    el.querySelector(".tree-project-head").addEventListener("click", async (e) => {
      const action =
        e.target?.dataset?.action || (e.target?.classList?.contains("caret") ? "toggle" : "select");
      if (action === "toggle") {
        if (state.expanded.has(p.id)) state.expanded.delete(p.id);
        else state.expanded.add(p.id);
        renderTree();
        return;
      }
      state.expanded.add(p.id);
      await selectProject(p.id);
    });
    root.appendChild(el);
    if (open) loadTreeRepos(p.id);
  }
}

async function loadTreeRepos(projectId) {
  const box = document.querySelector(`[data-repos-for="${projectId}"]`);
  if (!box) return;
  try {
    const data = await api(`/projects/${projectId}/repos`);
    box.innerHTML = "";
    for (const r of data.repos || []) {
      const item = document.createElement("div");
      item.className = `tree-repo${state.selectedRepoId === r.id ? " active" : ""}`;
      item.textContent = r.name;
      item.addEventListener("click", (e) => {
        e.stopPropagation();
        selectRepo(projectId, r.id);
      });
      box.appendChild(item);
    }
  } catch (err) {
    box.innerHTML = `<div class="muted">${escapeHtml(err.message)}</div>`;
  }
}

function setProjectTab(name) {
  state.projectTab = name;
  $$('#view-project .tab[data-for="project"]').forEach((t) =>
    t.classList.toggle("active", t.dataset.tab === name)
  );
  ["ask", "search", "repos"].forEach((t) => {
    $(`#project-tab-${t}`)?.classList.toggle("hidden", t !== name);
  });
}

function setRepoTab(name) {
  state.repoTab = name;
  $$('#view-repo .tab[data-for="repo"]').forEach((t) =>
    t.classList.toggle("active", t.dataset.tab === name)
  );
  ["ask", "search", "graph"].forEach((t) => {
    $(`#repo-tab-${t}`)?.classList.toggle("hidden", t !== name);
  });
}

async function selectProject(projectId) {
  state.selectedProjectId = projectId;
  state.selectedRepoId = null;
  persistSelection();
  showView("project");
  setProjectTab(state.projectTab || "ask");
  const overview = await api(`/projects/${projectId}/overview`);
  state.overview = overview;
  $("#project-name").textContent = overview.project.name;
  fillStatRibbon(overview.stats);
  renderRepoList(overview.repos || []);
  populateScopeSelect(overview.repos || []);
  renderTree();
  ensureChatWelcome(
    "#project-chat-log",
    `Ask about **${overview.project.name}** — answers use indexed commits & files **in this project only**.`
  );
  if ($("#project-scope-tag")) {
    $("#project-scope-tag").textContent = `scope · project · ${overview.project.name}`;
  }
  updateAiBanners();
  syncModelSelects();
}

function populateScopeSelect(repos) {
  const sel = $("#search-scope");
  if (!sel) return;
  sel.innerHTML = `<option value="project">This project</option>`;
  for (const r of repos) {
    const opt = document.createElement("option");
    opt.value = `repo:${r.id}`;
    opt.textContent = `Repo: ${r.name}`;
    sel.appendChild(opt);
  }
}

function renderRepoList(repos) {
  const list = $("#repo-list");
  if (!list) return;
  list.innerHTML = "";
  if (!repos.length) {
    list.innerHTML = `<p class="muted">No repositories. Use <strong>Add repository</strong>.</p>`;
    return;
  }
  for (const r of repos) {
    const row = document.createElement("div");
    row.className = "repo-row";
    const failed =
      r.graph_state === "failed" ||
      r.search_state === "failed" ||
      (r.last_error && String(r.last_error).length > 0);
    const errLine = failed
      ? `<div class="meta" style="color:var(--bad)">Index issue: ${escapeHtml(
          String(r.last_error || r.graph_state || r.search_state).slice(0, 120)
        )}</div>`
      : "";
    row.innerHTML = `
      <div class="name">${escapeHtml(r.name)}</div>
      <div class="meta">${r.commit_count || 0} commits · ${r.file_count || 0} files · graph:${escapeHtml(
      r.graph_state || "—"
    )} · search:${escapeHtml(r.search_state || "—")}</div>
      ${errLine}
    `;
    row.addEventListener("click", () => selectRepo(state.selectedProjectId, r.id));
    list.appendChild(row);
  }
}

async function selectRepo(projectId, repoId) {
  state.selectedProjectId = projectId;
  state.selectedRepoId = repoId;
  state.expanded.add(projectId);
  persistSelection();
  showView("repo");
  setRepoTab(state.repoTab || "ask");
  const repo = await api(`/repos/${repoId}`);
  $("#repo-name").textContent = repo.name;
  $("#repo-stats").textContent = `${repo.commit_count || 0} commits · ${
    repo.file_count || 0
  } files · HEAD ${shortSha(repo.sync_to_sha)}`;
  $("#repo-back").onclick = (e) => {
    e.preventDefault();
    selectProject(projectId);
  };
  renderTree();
  ensureChatWelcome(
    "#repo-chat-log",
    `Ask about **${repo.name}** — answers stay inside this repository.`
  );
  try {
    const wc = await api(`/repos/${repoId}/what-changed?since=HEAD~8`);
    const lines = (wc.commits || [])
      .slice(0, 8)
      .map((c) => `${shortSha(c.sha)}  ${c.message || c.subject || ""}`)
      .join("\n");
    $("#what-changed").textContent = lines || "(no recent commits)";
  } catch (err) {
    $("#what-changed").textContent = err.message;
  }
  updateAiBanners();
  syncModelSelects();
}

/* ── Chat ───────────────────────────────────────────────── */

function ensureChatWelcome(logSel, mdText) {
  const log = $(logSel);
  if (!log) return;
  if (!log.dataset.ready) {
    log.innerHTML = "";
    appendMsg(log, "system", mdText);
    log.dataset.ready = "1";
  }
}

function sourceLang(c) {
  if (c.language) return String(c.language);
  const p = String(c.path || c.title || "").toLowerCase();
  if (p.endsWith(".yml") || p.endsWith(".yaml")) return "yaml";
  if (p.endsWith(".md")) return "markdown";
  if (p.endsWith(".json")) return "json";
  if (p.endsWith(".py")) return "python";
  if (p.endsWith(".go")) return "go";
  if (p.endsWith(".java")) return "java";
  return "text";
}

function renderSourceItem(c) {
  const path = escapeHtml(String(c.path || "").replace(/^\/repos\/[^/]+\//, ""));
  const repo = escapeHtml(c.repo_name || "");
  const n = c.n ?? "";
  const sha = c.sha ? String(c.sha).slice(0, 7) : "";
  const code = c.body || c.snippet || c.content || "";
  const hasCode = Boolean(String(code).trim());
  const lang = escapeHtml(sourceLang(c));
  const head = [`[${n}]`, repo, path, sha].filter(Boolean).join(" · ");
  if (!hasCode) {
    return `<li class="cite-item cite-item-plain mono">${escapeHtml(head)}</li>`;
  }
  // Expandable: tap header to show file excerpt
  return (
    `<li class="cite-item">` +
    `<details class="cite-details">` +
    `<summary class="cite-summary">` +
    `<span class="cite-summary-main mono">${escapeHtml(head)}</span>` +
    `<span class="cite-chevron" aria-hidden="true">▾</span>` +
    `</summary>` +
    `<div class="cite-code-wrap">` +
    `<div class="cite-code-meta mono">${lang} · expand to read</div>` +
    `<pre class="cite-code"><code class="language-${lang}">${escapeHtml(code)}</code></pre>` +
    `</div>` +
    `</details>` +
    `</li>`
  );
}

function appendMsg(log, role, body, cites) {
  const div = document.createElement("div");
  div.className = `msg ${role === "user" ? "user" : role === "system" ? "system" : "ai"}`;
  const roleLabel = role === "user" ? "You" : role === "system" ? "Tip" : "Assistant";
  let citesHtml = "";
  if (cites?.length && role === "ai") {
    citesHtml =
      `<aside class="cite-rail" aria-label="Sources">` +
      `<div class="cite-rail-title">Sources</div>` +
      `<p class="cite-rail-hint">Tap a file to show code</p>` +
      `<ul class="cites">` +
      cites.map(renderSourceItem).join("") +
      `</ul></aside>`;
  }
  const bodyHtml =
    role === "user" ? escapeHtml(body).replace(/\n/g, "<br>") : renderMarkdown(body);
  // AI: explanation column (left) + wider sources rail (right on desktop)
  if (role === "ai" && cites?.length) {
    div.classList.add("msg-split");
    div.innerHTML =
      `<div class="role">${roleLabel}</div>` +
      `<div class="msg-grid">` +
      `<div class="body md">${bodyHtml}</div>` +
      citesHtml +
      `</div>`;
  } else {
    div.innerHTML = `<div class="role">${roleLabel}</div><div class="body md">${bodyHtml}</div>`;
  }
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return div;
}

function currentModel() {
  const a = $("#project-model")?.value;
  const b = $("#repo-model")?.value;
  return (state.view === "repo" ? b : a) || state.chatModel || "";
}

async function sendChat({ logSel, inputSel, scopeType, scopeId }) {
  const log = $(logSel);
  const input = $(inputSel);
  if (!log || !input) return;
  const message = input.value.trim();
  if (!message) return;
  if (!scopeId) return;
  input.value = "";
  appendMsg(log, "user", message);
  const pending = appendMsg(log, "ai", "_Thinking…_");
  const model = currentModel();
  if (model) {
    state.chatModel = model;
    localStorage.setItem(LS_MODEL, model);
    syncModelSelects();
  }
  try {
    const payload = {
      message,
      scope: { type: scopeType, id: scopeId },
    };
    if (model) payload.model = model;
    const data = await api("/ai/chat", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    pending.remove();
    appendMsg(log, "ai", data.answer || "(no answer)", data.citations);
  } catch (err) {
    pending.remove();
    if (err.code === "not_configured" || err.status === 503) {
      appendMsg(
        log,
        "ai",
        `**AI not configured.** ${err.message}\n\nOpen **Settings** to use Parvaana AI or add an API key + model.`
      );
      await refreshAiStatus();
    } else {
      appendMsg(log, "ai", `**Error:** ${err.message}`);
    }
  }
}

/* ── Search / graph ─────────────────────────────────────── */

async function runSearch(q, scopeType, scopeId, resultsSel) {
  const box = $(resultsSel);
  if (!box) return;
  box.innerHTML = `<p class="muted">Searching…</p>`;
  try {
    const data = await api("/search", {
      method: "POST",
      body: JSON.stringify({ q, scope: { type: scopeType, id: scopeId }, limit: 25 }),
    });
    if (!data.results?.length) {
      box.innerHTML = `<p class="muted">No results for “${escapeHtml(data.q || q)}” in ${
        data.scope?.type || scopeType
      } scope.</p>`;
      return;
    }
    box.innerHTML = `<p class="muted">${data.count} hits · scope ${escapeHtml(
      data.scope?.type || scopeType
    )}</p>`;
    for (const r of data.results) {
      const card = document.createElement("div");
      card.className = "result-card";
      const path = (r.path || r.title || "").replace(/^\/repos\/[^/]+\//, "");
      card.innerHTML = `
        <div class="path">${escapeHtml(r.repo_name || "")} · ${escapeHtml(path)}</div>
        <div class="snip">${escapeHtml((r.snippet || "").slice(0, 280))}</div>
      `;
      box.appendChild(card);
    }
  } catch (err) {
    box.innerHTML = `<p class="muted" style="color:var(--bad)">${escapeHtml(err.message)}</p>`;
  }
}

function renderBlast(data) {
  const el = $("#blast-radius");
  if (!el) return;
  const files = (data.files_modified || data.files || []).slice(0, 40).join("\n");
  el.textContent = `${shortSha(data.sha)}  ${data.message || ""}\n\n${files || "(no files)"}`;
}

/* ── Jobs / Settings ────────────────────────────────────── */

async function loadJobs() {
  showView("jobs");
  const data = await api("/jobs?limit=40");
  const list = $("#jobs-list");
  list.innerHTML = "";
  for (const j of data.jobs || []) {
    const row = document.createElement("div");
    row.className = "job-row";
    row.innerHTML = `
      <div><strong>${escapeHtml(j.kind)}</strong> · ${escapeHtml(j.status)} · stage ${escapeHtml(
      j.stage
    )}</div>
      <div class="muted mono">${j.id}${j.error ? " · " + escapeHtml(j.error) : ""}</div>
      <div class="job-stages">${(j.stages || [])
        .map((s) => `<span class="stage-chip ${s.status}">${s.name}: ${s.status}</span>`)
        .join("")}</div>
    `;
    list.appendChild(row);
  }
}

async function openSettings() {
  showView("settings");
  if (!state.providers.length) await loadProvidersCatalog();
  const st = await refreshAiStatus();
  const pub = st.settings || (await api("/settings"));
  const pid = pub.api_provider_id || "auto";
  if ($("#set-api-provider")) {
    if (![...$("#set-api-provider").options].some((o) => o.value === pid)) {
      await loadProvidersCatalog();
    }
    $("#set-api-provider").value = pid;
    updateProviderDesc();
  }
  if ($("#set-provider")) $("#set-provider").value = pub.ai_provider || "auto";
  if ($("#set-parvaana-url")) $("#set-parvaana-url").value = pub.parvaana_base_url || "";
  if ($("#set-openai-url")) {
    $("#set-openai-url").value = pub.openai_base_url || "";
  }
  if ($("#set-openai-key")) $("#set-openai-key").value = "";
  if ($("#key-hint")) {
    $("#key-hint").textContent = pub.openai_api_key_set
      ? `(saved ${pub.openai_api_key_masked || "****"})`
      : "(not set)";
  }
  await refreshModels({ fetchRemote: true });
  const setModel = $("#set-openai-model");
  if (setModel && pub.openai_model) {
    if (![...setModel.options].some((o) => o.value === pub.openai_model)) {
      const opt = document.createElement("option");
      opt.value = pub.openai_model;
      opt.textContent = pub.openai_model;
      setModel.appendChild(opt);
    }
    setModel.value = pub.openai_model;
  }
  if ($("#set-openai-model-custom")) $("#set-openai-model-custom").value = "";

  const box = $("#settings-status");
  if (st.configured) {
    box.innerHTML =
      `<strong>Active now:</strong> <code class="mono">${escapeHtml(
        st.provider_label || st.provider || ""
      )}</code> · model <code class="mono">${escapeHtml(st.model || "")}</code>` +
      (st.base_url
        ? `<br><span class="muted mono">${escapeHtml(st.base_url)}</span>`
        : "") +
      `<br><span class="muted">${escapeHtml(st.detail || "")}</span>` +
      `<br><span class="muted">Settings catalog: ${escapeHtml(pid)} · saved model ${escapeHtml(
        pub.openai_model || "—"
      )}</span>`;
  } else {
    box.innerHTML = `<strong>AI not configured.</strong> ${escapeHtml(
      st.setup_hint || st.detail || ""
    )}`;
  }
}

function focusAsk() {
  if (state.view === "repo") {
    setRepoTab("ask");
    $("#repo-chat-input")?.focus();
  } else if (state.view === "project") {
    setProjectTab("ask");
    $("#project-chat-input")?.focus();
  } else if (state.selectedProjectId) {
    selectProject(state.selectedProjectId).then(() => $("#project-chat-input")?.focus());
  }
}

/* ── Wire ───────────────────────────────────────────────── */

function wireUi() {
  $("#btn-menu")?.addEventListener("click", openRail);
  $("#btn-rail-close")?.addEventListener("click", closeRail);
  $("#rail-backdrop")?.addEventListener("click", closeRail);

  $("#btn-new-project")?.addEventListener("click", () => $("#dlg-project").showModal());
  $("#btn-empty-new")?.addEventListener("click", () => $("#dlg-project").showModal());
  $("#dlg-project")?.addEventListener("close", async () => {
    if ($("#dlg-project").returnValue !== "ok") return;
    const name = new FormData($("#form-project")).get("name");
    if (!name) return;
    try {
      const p = await api("/projects", {
        method: "POST",
        body: JSON.stringify({ name: String(name) }),
      });
      await loadProjects();
      await selectProject(p.id);
    } catch (err) {
      alert(err.message);
    } finally {
      $("#form-project").reset();
    }
  });

  $("#btn-add-repo")?.addEventListener("click", () => {
    if (!state.selectedProjectId) return;
    $("#dlg-repo").showModal();
  });
  $("#dlg-repo")?.addEventListener("close", async () => {
    if ($("#dlg-repo").returnValue !== "ok") return;
    const url = new FormData($("#form-repo")).get("url");
    if (!url || !state.selectedProjectId) return;
    try {
      await api(`/projects/${state.selectedProjectId}/repos`, {
        method: "POST",
        body: JSON.stringify({ url: String(url), index: true }),
      });
      for (let i = 0; i < 40; i++) {
        await selectProject(state.selectedProjectId);
        const pending = (state.overview?.repos || []).some((r) => {
          const graphOk = r.graph_state === "ready" || r.graph_state === "ready_local";
          return !(graphOk && r.search_state === "ready");
        });
        if (!pending) break;
        await sleep(1500);
      }
      setProjectTab("repos");
    } catch (err) {
      alert(err.message);
    } finally {
      $("#form-repo").reset();
    }
  });

  $$('#view-project .tab[data-for="project"]').forEach((t) =>
    t.addEventListener("click", () => setProjectTab(t.dataset.tab))
  );
  $$('#view-repo .tab[data-for="repo"]').forEach((t) =>
    t.addEventListener("click", () => setRepoTab(t.dataset.tab))
  );

  $("#project-chat-form")?.addEventListener("submit", (e) => {
    e.preventDefault();
    sendChat({
      logSel: "#project-chat-log",
      inputSel: "#project-chat-input",
      scopeType: "project",
      scopeId: state.selectedProjectId,
    });
  });
  $("#repo-chat-form")?.addEventListener("submit", (e) => {
    e.preventDefault();
    sendChat({
      logSel: "#repo-chat-log",
      inputSel: "#repo-chat-input",
      scopeType: "repo",
      scopeId: state.selectedRepoId,
    });
  });

  for (const id of ["#project-model", "#repo-model"]) {
    $(id)?.addEventListener("change", (e) => {
      state.chatModel = e.target.value || "";
      localStorage.setItem(LS_MODEL, state.chatModel);
      syncModelSelects();
    });
  }

  // Ctrl/Cmd+Enter in textareas
  for (const id of ["#project-chat-input", "#repo-chat-input"]) {
    $(id)?.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && isMod(e)) {
        e.preventDefault();
        $(id)?.form?.requestSubmit();
      }
    });
  }

  $("#search-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const q = $("#search-q").value.trim();
    if (!q || !state.selectedProjectId) return;
    const scopeVal = $("#search-scope").value;
    if (scopeVal === "project")
      await runSearch(q, "project", state.selectedProjectId, "#search-results");
    else if (scopeVal.startsWith("repo:"))
      await runSearch(q, "repo", scopeVal.slice(5), "#search-results");
  });
  $("#repo-search-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const q = $("#repo-search-q").value.trim();
    if (!q || !state.selectedRepoId) return;
    await runSearch(q, "repo", state.selectedRepoId, "#repo-search-results");
  });

  $("#blast-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const sha = $("#blast-sha").value.trim();
    if (!sha || !state.selectedRepoId) return;
    renderBlast(
      await api(`/repos/${state.selectedRepoId}/commits/${encodeURIComponent(sha)}/blast-radius`)
    );
  });
  $("#btn-sync")?.addEventListener("click", async () => {
    if (!state.selectedRepoId) return;
    await api(`/repos/${state.selectedRepoId}/sync`, { method: "POST" });
    alert("Sync job queued");
  });
  $("#btn-reindex")?.addEventListener("click", async () => {
    if (!state.selectedRepoId) return;
    await api(`/repos/${state.selectedRepoId}/reindex`, { method: "POST" });
    alert("Reindex job queued");
  });

  $("#btn-jobs")?.addEventListener("click", () => loadJobs());
  $("#btn-settings")?.addEventListener("click", () => openSettings());
  $("#btn-settings-top")?.addEventListener("click", () => openSettings());
  $("#ai-chip")?.addEventListener("click", () => openSettings());

  $("#set-api-provider")?.addEventListener("change", () => {
    updateProviderDesc();
  });

  $("#settings-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const custom = ($("#set-openai-model-custom")?.value || "").trim();
    const model = custom || fd.get("openai_model") || "";
    const body = {
      api_provider_id: fd.get("api_provider_id") || $("#set-api-provider")?.value,
      openai_base_url: fd.get("openai_base_url"),
      openai_model: model,
      parvaana_base_url: fd.get("parvaana_base_url") || "",
    };
    const key = fd.get("openai_api_key");
    if (key) body.openai_api_key = key;
    await api("/settings", { method: "PATCH", body: JSON.stringify(body) });
    // Clear per-message override so chip + bar match saved active provider
    state.chatModel = "";
    localStorage.removeItem(LS_MODEL);
    await refreshAiStatus();
    await refreshModels({ fetchRemote: true });
    await openSettings();
    $("#settings-test-out").textContent = "Saved. Active provider/model updated.";
  });
  $("#btn-test-ai")?.addEventListener("click", async () => {
    $("#settings-test-out").textContent = "Testing…";
    try {
      const r = await api("/ai/test", { method: "POST" });
      $("#settings-test-out").textContent = JSON.stringify(r, null, 2);
      await refreshAiStatus();
      await refreshModels({ fetchRemote: true });
    } catch (err) {
      $("#settings-test-out").textContent = err.message;
    }
  });
  $("#btn-refresh-models")?.addEventListener("click", async () => {
    $("#settings-test-out").textContent = "Fetching models…";
    const data = await refreshModels({ fetchRemote: true });
    $("#settings-test-out").textContent = data
      ? `Models (${data.source}): ${(data.models || []).slice(0, 12).join(", ")}${(data.models || []).length > 12 ? "…" : ""}`
      : "Failed to load models";
  });

  // Global shortcuts
  document.addEventListener("keydown", (e) => {
    const tag = (e.target && e.target.tagName) || "";
    const typing =
      tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || e.target?.isContentEditable;

    if (e.key === "Escape") {
      closeRail();
      $$("dialog[open]").forEach((d) => d.close());
      return;
    }
    if (e.key === "?" && !typing && !isMod(e)) {
      e.preventDefault();
      $("#dlg-help")?.showModal();
      return;
    }
    if (isMod(e) && e.key === ",") {
      e.preventDefault();
      openSettings();
      return;
    }
    if (isMod(e) && (e.key === "k" || e.key === "K")) {
      e.preventDefault();
      focusAsk();
      return;
    }
  });
}

async function boot() {
  wireUi();
  await loadProvidersCatalog();
  await refreshAiStatus();
  await refreshModels({ fetchRemote: false });
  try {
    await api("/admin/purge-test-projects", { method: "POST" });
  } catch {
    /* ignore */
  }
  await loadProjects();

  const saved = localStorage.getItem(LS_PROJECT);
  const savedOk = saved && state.projects.some((p) => p.id === saved);
  const pick =
    (savedOk && saved) ||
    state.defaultProjectId ||
    state.projects.find((p) => p.name === "HugeGraph")?.id ||
    state.projects.find((p) => (p.repo_count || 0) > 0)?.id ||
    state.projects[0]?.id;

  if (pick) {
    state.expanded.add(pick);
    await selectProject(pick);
    // Prefer HugeGraph over Personal if saved was junk-adjacent
    const proj = state.projects.find((p) => p.id === pick);
    if (proj && /personal/i.test(proj.name) && (proj.repo_count || 0) < 2) {
      const hg = state.projects.find((p) => p.name === "HugeGraph");
      if (hg) {
        state.expanded.add(hg.id);
        await selectProject(hg.id);
      }
    }
  } else {
    showView("empty");
  }
}

boot().catch((err) => {
  console.error(err);
  if ($("#ai-chip")) {
    $("#ai-chip").textContent = "boot error";
    $("#ai-chip").className = "ai-pill bad";
  }
});
