import "./vendor/falkordb-canvas.js?v=46";

const stateLabel = {
  PROCEED: "Proceed",
  PROCEED_WITH_GUARDS: "Proceed with guards",
  NEEDS_REVIEW: "Needs review",
  HOLD: "Hold",
  INSUFFICIENT_EVIDENCE: "Insufficient evidence",
};

const modeLabel = {
  full: "Full graph",
  one_hop: "1-hop",
  graph_off: "Graph off",
};

const pathHelp = {
  review: "Deterministic policy. Best for Full vs Graph-off lift.",
  agent: "Scripted agent picks named graph tools, then decide + gate.",
  llm: "LLM chooses tools and narrates. Policy still owns the state.",
};

const pathRunLabel = {
  review: "Run review",
  agent: "Run agent",
  llm: "Run agent + LLM",
};

const ROLE_COLOR = {
  changed: "#e879c8",
  direct: "#e879c8",
  consumer: "#3b82f6",
  dependency: "#c17a3a",
  incident: "#c084fc",
  fix: "#ec6bb8",
  owner: "#34d399",
};

const EDGE_COLOR = {
  CALLS: "#7eb6e8",
  PART_OF: "#d4b562",
  DEPENDS_ON: "#5ba3d9",
  OWNS: "#3ecf8e",
  AFFECTED: "#c084fc",
  COVERS: "#c4d35a",
};

const JOBS = {
  analyze: { label: "Analyzing", steps: ["Scan repo", "Publish to FalkorDB", "Auto-scope"] },
  scope: { label: "Scoping", steps: ["Map files to symbols"] },
  review: { label: "Reviewing", steps: ["Read FalkorDB", "Apply policy", "Mode lift"] },
  agent: { label: "Agent", steps: ["Named tools", "decide_change", "Compare modes"] },
  llm: { label: "Agent + LLM", steps: ["LLM picks tools", "Graph reads", "Policy decides"] },
  notify: { label: "Notify", steps: ["Draft message"] },
  act: { label: "Issue", steps: ["Action gate"] },
  pr: { label: "PR", steps: ["Post comment"] },
  openpr: { label: "GitHub", steps: ["Open issue", "Branch + PR"] },
};

let snapshot = null;
let current = null;
let decidePath = "review";
let statusTimer = null;
let statusStepIndex = 0;
let statusSteps = [];
let previewGraph = null;
let animating = true;
let fitCount = 0;
let githubStatus = { connected: false, oauth_configured: false };
let repoCache = [];
let repoSearchTimer = null;
let layoutMode = "force";
let lastGraph = null;
let selectedId = null;
let canvasReady = false;
let reheatTimer = null;

const $ = (id) => document.getElementById(id);

function canvasEl() {
  return $("falkor-canvas");
}

async function api(path, options) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await response.text();
  const body = text ? JSON.parse(text) : {};
  if (!response.ok) {
    const detail = body.detail || response.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return body;
}

function shortError(error) {
  const raw = error?.message || String(error || "");
  const cut = raw.split(" Suggested files")[0].split(" Pass explicit")[0];
  return cut.length > 160 ? `${cut.slice(0, 157)}…` : cut;
}

function showError(id, error) {
  const node = $(id);
  if (!node) return;
  node.hidden = !error;
  node.textContent = error ? shortError(error) : "";
}

function setBusy(on, jobKey, detail) {
  const el = $("toast");
  if (!el) return;
  if (statusTimer) {
    clearInterval(statusTimer);
    statusTimer = null;
  }
  if (!on) {
    el.hidden = true;
    return;
  }
  const job = JOBS[jobKey] || { label: "Working", steps: [detail || "In progress…"] };
  statusSteps = job.steps.slice();
  statusStepIndex = 0;
  el.hidden = false;
  $("toast-label").textContent = job.label;
  $("toast-detail").textContent = detail || statusSteps[0] || "Working…";
  if (statusSteps.length > 1) {
    statusTimer = setInterval(() => {
      if (statusStepIndex < statusSteps.length - 1) {
        statusStepIndex += 1;
        $("toast-detail").textContent = statusSteps[statusStepIndex];
      }
    }, 2200);
  }
}

function parseFiles(value) {
  return value.split(",").map((p) => p.trim()).filter(Boolean);
}

function scopePayload() {
  const files = parseFiles($("files").value);
  const base = $("base").value.trim() || null;
  const head = $("head").value.trim() || null;
  const body = {};
  if (files.length) body.files = files;
  if (base) body.base = base;
  if (head) body.head = head;
  return body;
}

function hasScopeInput() {
  const body = scopePayload();
  return Boolean((body.files && body.files.length) || body.base || body.head);
}

function setDecidePath(path) {
  decidePath = path;
  for (const btn of document.querySelectorAll(".seg-btn")) {
    btn.classList.toggle("active", btn.dataset.path === path);
  }
  $("path-help").textContent = pathHelp[path] || pathHelp.review;
  $("run").textContent = pathRunLabel[path] || "Run";
  if (path === "llm" && snapshot && snapshot.llm_ready === false) {
    showError("review-error", new Error("No LLM key in .env — local planner will run instead."));
  } else {
    showError("review-error", null);
  }
}

function setWorkflowStep(step) {
  for (const btn of document.querySelectorAll(".step")) {
    const n = Number(btn.dataset.step);
    btn.classList.toggle("active", n === step);
    btn.classList.toggle("done", n < step);
  }
}

function backendChip(next) {
  const chip = $("backend");
  if (next.graph_ready) {
    chip.textContent = `Falkor · ${next.nodes || 0}n / ${next.edges || 0}e`;
    chip.className = "chip ok";
  } else {
    chip.textContent = "Falkor offline";
    chip.className = "chip bad";
  }
}

function llmChip(next) {
  const chip = $("llm-chip");
  if (!chip) return;
  if (next.llm_ready) {
    chip.textContent = next.llm_model || "LLM ready";
    chip.className = "chip ok";
    return;
  }
  if (next.llm_ready === false) {
    chip.textContent = "LLM off";
    chip.className = "chip";
  }
}

function renderGithub(status) {
  githubStatus = status || { connected: false, oauth_configured: false };
  const chip = $("gh-chip");
  const login = $("gh-login");
  const logout = $("gh-logout");
  const help = $("gh-pr-help");
  const prInput = $("pr-number");
  const prBtn = $("pr-comment");
  const prOpen = $("pr-open");
  const picker = $("repo-picker");
  if (!chip) return;

  if (githubStatus.connected) {
    chip.textContent = `GH · ${githubStatus.login || "connected"}`;
    chip.className = "chip ok";
    if (login) {
      login.hidden = true;
      login.classList.add("is-hidden");
    }
    if (logout) {
      logout.hidden = false;
      logout.classList.remove("is-hidden");
    }
    if (help) {
      help.textContent =
        "Pick an open PR and Post, or Open issue + PR if this repo has none.";
    }
    if (prInput) prInput.disabled = false;
    if (prBtn) prBtn.disabled = !current;
    if (prOpen) prOpen.disabled = !current;
    if ($("pr-select")) $("pr-select").disabled = false;
    if (picker) picker.hidden = false;
    loadGithubRepos($("repo-search")?.value || "");
    loadGithubPulls();
  } else if (githubStatus.oauth_configured) {
    chip.textContent = "GitHub off";
    chip.className = "chip";
    if (login) {
      login.hidden = false;
      login.classList.remove("is-hidden");
      login.href = "/auth/github/login";
      login.textContent = "Connect GitHub";
    }
    if (logout) {
      logout.hidden = true;
      logout.classList.add("is-hidden");
    }
    if (help) help.textContent = "Connect GitHub above to post the decision onto a pull request.";
    if (prInput) prInput.disabled = true;
    if (prBtn) prBtn.disabled = true;
    if (prOpen) prOpen.disabled = true;
    if ($("pr-select")) {
      $("pr-select").disabled = true;
      $("pr-select").innerHTML = '<option value="">Connect GitHub to list PRs</option>';
    }
    if (picker) picker.hidden = true;
    repoCache = [];
  } else {
    chip.textContent = "GitHub unset";
    chip.className = "chip bad";
    if (login) {
      login.hidden = false;
      login.classList.remove("is-hidden");
      login.href = "#";
      login.textContent = "OAuth not configured";
      login.onclick = (event) => {
        event.preventDefault();
        showError("review-error", new Error("Set GITHUB_OAUTH_CLIENT_ID/SECRET in .env"));
      };
    }
    if (logout) {
      logout.hidden = true;
      logout.classList.add("is-hidden");
    }
    if (picker) picker.hidden = true;
  }
}

function fillRepoSelect(repos) {
  const select = $("repo-select");
  const hint = $("repo-hint");
  if (!select) return;
  select.innerHTML = "";
  if (!repos.length) {
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = "No repositories found";
    select.appendChild(empty);
    if (hint) hint.textContent = "No matches. Try another search, or paste a URL below.";
    return;
  }
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = `Select a repository (${repos.length})`;
  select.appendChild(placeholder);
  for (const repo of repos) {
    const option = document.createElement("option");
    option.value = repo.html_url || `https://github.com/${repo.full_name}`;
    const lock = repo.private ? "private" : "public";
    const lang = repo.language ? ` · ${repo.language}` : "";
    option.textContent = `${repo.full_name} (${lock}${lang})`;
    select.appendChild(option);
  }
  if (hint) hint.textContent = "Pick a repo, then Analyze — or paste any URL / local path below.";
}

async function loadGithubRepos(query) {
  const select = $("repo-select");
  const hint = $("repo-hint");
  if (!githubStatus.connected || !select) return;
  select.innerHTML = "";
  const loading = document.createElement("option");
  loading.value = "";
  loading.textContent = "Loading repositories…";
  select.appendChild(loading);
  if (hint) hint.textContent = "Fetching from GitHub…";
  try {
    const q = encodeURIComponent(query || "");
    const body = await api(`/api/github/repos?limit=40&q=${q}`);
    repoCache = body.repos || [];
    fillRepoSelect(repoCache);
  } catch (error) {
    select.innerHTML = "";
    const fail = document.createElement("option");
    fail.value = "";
    fail.textContent = "Could not load repos";
    select.appendChild(fail);
    if (hint) hint.textContent = shortError(error);
  }
}

async function loadGithubPulls() {
  const select = $("pr-select");
  const help = $("gh-pr-help");
  if (!select) return;
  if (!githubStatus.connected) {
    select.innerHTML = '<option value="">Connect GitHub to list PRs</option>';
    select.disabled = true;
    return;
  }
  select.disabled = false;
  select.innerHTML = '<option value="">Loading pull requests…</option>';
  try {
    const body = await api("/api/github/pulls");
    const pulls = body.pulls || [];
    select.innerHTML = "";
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = pulls.length
      ? `Open pull requests (${pulls.length})`
      : (body.detail || "No open pull requests");
    select.appendChild(placeholder);
    for (const pull of pulls) {
      const option = document.createElement("option");
      option.value = String(pull.number);
      option.textContent = `#${pull.number} ${pull.title}`;
      select.appendChild(option);
    }
    if (help && githubStatus.connected) {
      help.textContent = pulls.length
        ? "Pick an open PR, then Post — or Open issue + PR for a fresh review thread."
        : (body.detail || "No open PRs. Click Open issue + PR to create both.");
    }
  } catch (error) {
    select.innerHTML = "";
    const fail = document.createElement("option");
    fail.value = "";
    fail.textContent = shortError(error);
    select.appendChild(fail);
  }
}

function shortCaption(label, kind) {
  const raw = String(label || "");
  if (kind === "function") {
    const bit = raw.split(".").pop() || raw;
    return bit.length > 14 ? `${bit.slice(0, 12)}…` : bit;
  }
  return raw.length > 16 ? `${raw.slice(0, 14)}…` : raw;
}

function nodeSize(role, kind, count) {
  const dense = count > 18;
  if (kind === "function") return dense ? 5 : 6;
  if (kind === "test") return dense ? 4.5 : 5.5;
  if (role === "changed" || role === "direct") return dense ? 7 : 8;
  if (kind === "service" || role === "dependency") return dense ? 8 : 9;
  if (kind === "team" || role === "owner") return dense ? 6 : 7;
  return dense ? 5.5 : 6.5;
}

function toCanvasData(graph) {
  const nodes = [];
  const links = [];
  const seen = new Set();
  const source = graph?.nodes || [];
  const dense = source.length > 24;
  for (const node of source) {
    if (!node?.id || seen.has(node.id)) continue;
    seen.add(node.id);
    const role = node.role || "dependency";
    const kind = node.kind || "";
    const label = String(node.label || node.id);
    nodes.push({
      id: node.id,
      labels: [kind || role, "node"].filter(Boolean),
      color: ROLE_COLOR[role] || "#94a3b8",
      visible: true,
      size: nodeSize(role, kind, source.length),
      caption: "name",
      data: {
        name: shortCaption(label, kind),
        fullName: label,
        role,
        kind,
        tier: node.tier ?? "",
        hops: node.hops ?? "",
        id: node.id,
      },
    });
  }
  const idSet = new Set(nodes.map((n) => n.id));
  let i = 0;
  for (const edge of graph?.edges || []) {
    if (!edge?.source || !edge?.target) continue;
    if (!idSet.has(edge.source) || !idSet.has(edge.target)) continue;
    i += 1;
    const rel = edge.label || "RELATED";
    links.push({
      id: i,
      relationship: "",
      color: EDGE_COLOR[rel] || "#4b5563",
      source: edge.source,
      target: edge.target,
      visible: true,
      data: { relationship: rel },
    });
  }
  return { nodes, links };
}

function sizeCanvas() {
  const shell = document.querySelector(".canvas-shell");
  const host = $("graph");
  const el = canvasEl();
  if (!host || !el || typeof el.setWidth !== "function") return;
  // Measure the shell, never the canvas element — FalkorDB Canvas can expand
  // its host and create a resize feedback loop.
  const box = shell?.getBoundingClientRect?.() || host.getBoundingClientRect();
  const legend = document.querySelector(".legend");
  const legendH = Math.max(legend?.getBoundingClientRect?.().height || 0, 34);
  const width = Math.max(Math.floor(box.width || host.clientWidth || 640), 320);
  const height = Math.max(Math.floor((box.height || 480) - legendH), 320);
  if (el.config?.width === width && el.config?.height === height) return;
  el.setWidth(width);
  el.setHeight(height);
}

function reheat() {
  const el = canvasEl();
  if (!el || !animating || layoutMode !== "force") return;
  try {
    const fg = typeof el.getGraph === "function" ? el.getGraph() : null;
    fg?.d3ReheatSimulation?.();
  } catch (_) {
    /* ignore */
  }
}

function fitGraph() {
  const el = canvasEl();
  if (!el) return;
  try {
    const fg = typeof el.getGraph === "function" ? el.getGraph() : null;
    const nodes = (el.getGraphData?.()?.nodes || []).filter(
      (n) => Number.isFinite(n.x) && Number.isFinite(n.y)
    );
    if (!fg || !nodes.length) {
      el.zoomToFit?.(0.3);
      return;
    }
    let minX = Infinity;
    let maxX = -Infinity;
    let minY = Infinity;
    let maxY = -Infinity;
    for (const n of nodes) {
      const r = (n.size || 22) + 8;
      minX = Math.min(minX, n.x - r);
      maxX = Math.max(maxX, n.x + r);
      minY = Math.min(minY, n.y - r);
      maxY = Math.max(maxY, n.y + r);
    }
    const spanX = Math.max(maxX - minX, 80);
    const spanY = Math.max(maxY - minY, 80);
    const host = $("graph");
    const dense = nodes.length > 18;
    const pad = 0.82;
    const zx = ((host?.clientWidth || 800) * pad) / spanX;
    const zy = ((host?.clientHeight || 560) * pad) / spanY;
    const target = Math.max(0.35, Math.min(dense ? 1.05 : 1.35, Math.min(zx, zy)));
    fg.centerAt((minX + maxX) / 2, (minY + maxY) / 2, 280);
    fg.zoom(target, 280);
    fitCount += 1;
  } catch (_) {
    /* ignore */
  }
}

function startReheatLoop() {
  if (reheatTimer) clearInterval(reheatTimer);
  reheatTimer = setInterval(reheat, 6500);
}

function configureCanvas() {
  const el = canvasEl();
  if (!el || typeof el.setConfig !== "function") return;
  sizeCanvas();
  el.setConfig({
    backgroundColor: "#0f1115",
    foregroundColor: "#d7dce3",
    layoutMode,
    animation: animating,
    captionsKeys: [],
    pinOnDragEnd: false,
    simulation: {
      centerStrength: 0.04,
      chargeStrength: -420,
      linkDistance: 46,
      velocityDecay: 0.28,
      alphaMin: 0.001,
      warmupTicks: 120,
      chargeDistanceMax: "auto",
    },
    interaction: {
      zoomToFitPadding: 0.22,
      zoomToFitMaxZoom: 1.2,
      zoomToFitDelay: 180,
      tooltipFontSize: 12,
    },
    nodeStyle: {
      fontFamily: "IBM Plex Mono, monospace",
      textFillRatio: 0,
      fontSize: 9,
      glowColor: [193, 122, 58],
      glowMaxOpacity: 0.18,
      glowSpread: 6,
    },
    linkStyle: {
      fontFamily: "IBM Plex Mono, monospace",
      width: 1,
    },
    largeGraph: {
      enabled: true,
      lowZoomThreshold: 1.8,
      skipLabelsAtLowZoom: true,
      skipArrowsAtLowZoom: true,
      skipLinkLabelsAtLowZoom: true,
    },
    eventHandlers: {
      onNodeClick: (node) => showInspector(node),
      onBackgroundClick: () => hideInspector(),
      onEngineStop: () => {
        fitGraph();
        if (animating && layoutMode === "force") setTimeout(reheat, 900);
      },
    },
  });
  if (typeof el.setAnimation === "function") el.setAnimation(animating);
  startReheatLoop();
  canvasReady = true;
}

function drawGraph(graph) {
  lastGraph = graph;
  const empty = $("graph-empty");
  const el = canvasEl();
  const nodes = graph?.nodes || [];
  if (!nodes.length) {
    if (empty) empty.hidden = false;
    if (el && typeof el.setData === "function") {
      el.setData({ nodes: [], links: [] });
    }
    hideInspector();
    return;
  }
  if (empty) empty.hidden = true;
  if (!canvasReady) configureCanvas();
  sizeCanvas();
  const data = toCanvasData(graph);
  fitCount = 0;
  animating = true;
  const play = $("g-play");
  if (play) {
    play.setAttribute("aria-pressed", "true");
    play.textContent = "❚❚";
  }
  el.setData(data);
  if (typeof el.setAnimation === "function") el.setAnimation(true);
  if (typeof el.setLayout === "function") el.setLayout(layoutMode);
  requestAnimationFrame(() => {
    try {
      const fg = typeof el.getGraph === "function" ? el.getGraph() : null;
      if (fg && typeof fg.d3ReheatSimulation === "function") fg.d3ReheatSimulation();
    } catch (_) {
      /* ignore */
    }
    setTimeout(() => fitGraph(), 500);
    setTimeout(() => fitGraph(), 1400);
  });
}

function showInspector(node) {
  const panel = $("inspector");
  if (!panel || !node) return;
  selectedId = node.id;
  panel.hidden = false;
  const data = node.data || {};
  $("insp-title").textContent = data.fullName || data.name || node.id || "Node";
  const meta = $("insp-meta");
  meta.innerHTML = "";
  const rows = [
    ["Role", data.role || (node.labels && node.labels[0]) || "—"],
    ["Kind", data.kind || "—"],
    ["Id", data.id || node.id || "—"],
  ];
  if (data.tier !== "" && data.tier != null) rows.push(["Tier", String(data.tier)]);
  if (data.hops !== "" && data.hops != null) rows.push(["Hops", String(data.hops)]);
  for (const [k, v] of rows) {
    const wrap = document.createElement("div");
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    dt.textContent = k;
    dd.textContent = v;
    wrap.append(dt, dd);
    meta.appendChild(wrap);
  }
}

function hideInspector() {
  selectedId = null;
  const panel = $("inspector");
  if (panel) panel.hidden = true;
}

function renderSuggestions(paths, activePath) {
  const wrap = $("suggestions");
  const list = $("suggestion-list");
  list.innerHTML = "";
  const rows = paths || [];
  wrap.hidden = rows.length === 0;
  for (const path of rows) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = path;
    if (activePath && (activePath === path || (Array.isArray(activePath) && activePath.includes(path)))) {
      button.classList.add("active");
    }
    button.addEventListener("click", () => scopeFile(path));
    list.appendChild(button);
  }
}

async function scopeFile(path) {
  $("files").value = path;
  showError("scope-error", null);
  setBusy(true, "scope", path);
  setWorkflowStep(2);
  try {
    const body = await api("/api/change", {
      method: "POST",
      body: JSON.stringify({ files: [path] }),
    });
    renderSnapshot(body.snapshot || body);
    clearDecision();
    setWorkflowStep(3);
  } catch (error) {
    showError("scope-error", error);
  } finally {
    setBusy(false);
  }
}

function clearDecision() {
  current = null;
  $("decision").hidden = true;
  $("evidence-strip").hidden = true;
  $("compare").innerHTML = "";
  if (previewGraph) {
    $("graph-title").textContent = "Repository graph";
    $("graph-caption").textContent = "Live FalkorDB service map. Run a decision to see blast radius.";
    drawGraph(previewGraph);
  }
}

function renderSnapshot(next) {
  snapshot = next;
  backendChip(next);
  llmChip(next);
  if (next.github) renderGithub(next.github);
  previewGraph = next.graph_preview || null;

  $("origin-note").textContent = next.note || "";
  const scoped = (next.changes || []).length > 0;
  const files = scoped ? next.changes[0].files || [] : [];
  if (files.length) $("files").value = files.slice(0, 3).join(", ");

  $("scope-note").textContent = scoped ? next.scope_note || "Scoped." : "";
  showError("scope-error", !scoped && next.scope_error ? new Error(next.scope_error) : null);
  renderSuggestions(next.suggested_files || [], files);

  const change = $("change");
  change.innerHTML = "";
  if (!next.changes.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "None";
    change.appendChild(option);
  }
  for (const item of next.changes) {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item.title || item.id;
    change.appendChild(option);
  }

  $("run").disabled = next.changes.length === 0;
  $("rescope").disabled = next.dataset !== "repository";
  $("change-help").textContent = scoped
    ? `Ready · ${(files[0] || "scoped file")}`
    : next.dataset === "repository"
      ? "Click a file to scope"
      : "Analyze a repo first";

  if (next.dataset === "repository") setWorkflowStep(scoped ? 3 : 2);
  else setWorkflowStep(1);

  if (previewGraph && !current) {
    $("graph-title").textContent = "Repository graph";
    $("graph-caption").textContent = `${next.nodes || 0} nodes · ${next.edges || 0} edges on FalkorDB · drag to explore`;
    drawGraph(previewGraph);
  } else if (!previewGraph && !current) {
    drawGraph(null);
  }
}

function renderList(id, rows, text, eid) {
  const list = $(id);
  if (!list) return;
  list.innerHTML = "";
  for (const row of rows || []) {
    const item = document.createElement("li");
    if (eid) {
      const code = document.createElement("span");
      code.className = "eid";
      code.textContent = eid(row);
      item.append(code, document.createTextNode(" " + text(row)));
    } else {
      item.textContent = text(row);
    }
    list.appendChild(item);
  }
}

function renderEvidence(rows) {
  const list = $("evidence");
  list.innerHTML = "";
  if (!rows?.length) {
    list.innerHTML = "<li>None for this mode.</li>";
    return;
  }
  for (const row of rows.slice(0, 12)) {
    const item = document.createElement("li");
    const code = document.createElement("span");
    code.className = "eid";
    code.textContent = row.id;
    item.append(code, document.createTextNode(` ${row.detail || row.role || ""}`));
    list.appendChild(item);
  }
}

function renderImpact(record) {
  const list = $("impact-list");
  list.innerHTML = "";
  const nodes = record.impact?.nodes || record.graph?.nodes || [];
  if (!nodes.length) {
    list.innerHTML = `<li>${
      record.mode === "graph_off" ? "Empty on purpose (graph off)." : "No impact nodes."
    }</li>`;
    return;
  }
  for (const node of nodes.slice(0, 14)) {
    const item = document.createElement("li");
    item.textContent = `${node.label || node.id} · ${node.role || ""}`;
    list.appendChild(item);
  }
}

function renderNextSteps(steps) {
  const list = $("next-steps");
  list.innerHTML = "";
  for (const step of steps?.length ? steps : ["Done."]) {
    const item = document.createElement("li");
    item.textContent = String(step).replace(/^\d+\.\s*/, "").replace(/^-\s*/, "");
    list.appendChild(item);
  }
}

function renderTrace(calls, agentMeta) {
  const trace = $("trace");
  trace.innerHTML = "";
  $("agent-note").textContent = agentMeta
    ? `${agentMeta.planner || "tools"} · ${agentMeta.backend || "?"}`
    : "Named tools only";
  for (const call of calls || []) {
    const item = document.createElement("li");
    item.textContent = call.tool || call.name || "tool";
    trace.appendChild(item);
  }
  if (!calls?.length) trace.innerHTML = "<li>None</li>";
}

function renderLift(payload) {
  const box = $("lift");
  const full = payload?.runs?.full;
  const off = payload?.runs?.graph_off;
  if (!full || !off || full.state === off.state) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  $("lift-text").textContent =
    `Graph off → ${stateLabel[off.state] || off.state}. Full → ${stateLabel[full.state] || full.state}.`;
}

function renderRecord(payload, mode, agentMeta) {
  const record = payload.runs?.[mode];
  if (!record) {
    showError("review-error", new Error(`No run for ${mode}`));
    return;
  }
  current = { payload, mode, record, agentMeta: agentMeta || null };
  $("mode").value = mode;
  $("decision").hidden = false;
  $("evidence-strip").hidden = false;
  setWorkflowStep(3);
  renderLift(payload);

  $("graph-title").textContent = `${modeLabel[mode]} · impact`;
  $("graph-caption").textContent =
    mode === "graph_off"
      ? "Graph off — relationship view stays empty on purpose."
      : "Live blast-radius graph for this mode. Flip modes to see lift.";

  const revision = record.graph_revision ? String(record.graph_revision).slice(0, 8) : "";
  $("kicker").textContent = `${record.graph_backend || "graph"}${revision ? ` · ${revision}` : ""}`;
  const state = $("state");
  state.textContent = stateLabel[record.state] || record.state;
  state.className = `state-${record.state}`;
  $("rationale").textContent = record.rationale || "";

  const policies = $("policies");
  policies.innerHTML = "";
  for (const policy of record.triggered?.length ? record.triggered : ["none"]) {
    const item = document.createElement("li");
    item.textContent = policy;
    policies.appendChild(item);
  }

  const fullState = payload.runs.full?.state;
  const compare = $("compare");
  compare.innerHTML = "";
  for (const name of ["full", "one_hop", "graph_off"]) {
    const run = payload.runs[name];
    if (!run) continue;
    const button = document.createElement("button");
    button.type = "button";
    if (name === mode) button.classList.add("active");
    if (fullState && run.state !== fullState) button.classList.add("lift-diff");
    button.innerHTML =
      `<span class="mode-name">${modeLabel[name]}</span>` +
      `<span class="mode-state">${stateLabel[run.state] || run.state}</span>`;
    button.addEventListener("click", () => renderRecord(payload, name, current.agentMeta));
    compare.appendChild(button);
  }

  renderList("claims", record.claims || [], (c) => c.text, (c) => c.evidence_id);
  const missing = record.missing?.length
    ? record.missing
    : [{ evidence_id: "—", detail: "Nothing missing." }];
  renderList("missing", missing, (i) => i.detail, (i) => i.evidence_id || "—");
  renderEvidence(record.evidence || []);
  renderImpact(record);
  renderNextSteps(record.next_steps || []);
  renderTrace(
    agentMeta?.tool_trace_summary || record.tool_calls || [],
    agentMeta
  );

  if (agentMeta?.narration) {
    $("narration-box").hidden = false;
    $("narration").textContent = agentMeta.narration;
  } else {
    $("narration-box").hidden = true;
    $("narration").textContent = "";
  }

  const action = record.action || {};
  $("action-reason").textContent = action.reason || "";
  $("act").disabled = action.status !== "ready";
  if (!agentMeta?.narration) $("action-result").textContent = "";
  if ($("pr-comment")) $("pr-comment").disabled = !githubStatus.connected;
  if ($("pr-open")) $("pr-open").disabled = !githubStatus.connected;
  drawGraph(record.graph);
  requestAnimationFrame(() => {
    sizeCanvas();
    setTimeout(sizeCanvas, 80);
  });
  if (githubStatus.connected) loadGithubPulls();
}

async function runDecide() {
  showError("review-error", null);
  if (!$("change").value) {
    showError("review-error", new Error("Scope a file in step 2 first."));
    return;
  }
  const button = $("run");
  button.disabled = true;
  const useAgent = decidePath === "agent" || decidePath === "llm";
  const useLlm = decidePath === "llm";
  try {
    if (useAgent) {
      setBusy(true, useLlm ? "llm" : "agent");
      const result = await api("/api/agent", {
        method: "POST",
        body: JSON.stringify({
          change_id: $("change").value || undefined,
          mode: $("mode").value,
          llm: useLlm,
          hops: 3,
          execute: false,
        }),
      });
      if (!result.ok && result.error) throw new Error(result.error);
      setBusy(true, "review", "Comparing modes");
      const payload = await api("/api/review", {
        method: "POST",
        body: JSON.stringify({
          change_id: result.change_id || $("change").value,
          mode: $("mode").value,
        }),
      });
      renderRecord(payload, $("mode").value, {
        planner: result.planner || (useLlm ? "llm" : "scripted"),
        state: result.state,
        backend: result.graph_backend,
        narration: result.narration,
        tool_trace_summary: (result.tool_selection || []).map((name) => ({
          tool: name,
          summary: "",
        })),
      });
    } else {
      setBusy(true, "review");
      const payload = await api("/api/review", {
        method: "POST",
        body: JSON.stringify({ change_id: $("change").value, mode: $("mode").value }),
      });
      renderRecord(payload, $("mode").value, null);
    }
  } catch (error) {
    showError("review-error", error);
  } finally {
    setBusy(false);
    button.disabled = !(snapshot?.changes?.length);
    button.textContent = pathRunLabel[decidePath] || "Run";
  }
}

function wireGraphControls() {
  const play = $("g-play");
  play.addEventListener("click", () => {
    animating = !animating;
    play.setAttribute("aria-pressed", animating ? "true" : "false");
    play.textContent = animating ? "❚❚" : "▶";
    const el = canvasEl();
    if (el && typeof el.setAnimation === "function") el.setAnimation(animating);
    if (animating) {
      try {
        const fg = el.getGraph?.();
        fg?.d3ReheatSimulation?.();
      } catch (_) {
        /* ignore */
      }
    }
  });

  $("g-fit").addEventListener("click", () => fitGraph());

  $("g-zoom-in").addEventListener("click", () => {
    const el = canvasEl();
    if (!el?.getZoom || !el?.zoom) return;
    el.zoom(Math.min((el.getZoom() || 1) * 1.25, 8));
  });

  $("g-zoom-out").addEventListener("click", () => {
    const el = canvasEl();
    if (!el?.getZoom || !el?.zoom) return;
    el.zoom(Math.max((el.getZoom() || 1) / 1.25, 0.15));
  });

  for (const btn of document.querySelectorAll(".layout-btn")) {
    btn.addEventListener("click", () => {
      layoutMode = btn.dataset.layout || "force";
      for (const other of document.querySelectorAll(".layout-btn")) {
        other.classList.toggle("active", other === btn);
      }
      const el = canvasEl();
      if (el?.setLayout) el.setLayout(layoutMode);
      if (animating && el?.setAnimation) el.setAnimation(true);
    });
  }

  $("insp-close").addEventListener("click", hideInspector);

  const shell = document.querySelector(".canvas-shell");
  if (shell && typeof ResizeObserver !== "undefined") {
    let t = null;
    let last = "";
    const ro = new ResizeObserver((entries) => {
      const entry = entries[0];
      const w = Math.floor(entry?.contentRect?.width || 0);
      const h = Math.floor(entry?.contentRect?.height || 0);
      const key = `${w}x${h}`;
      if (key === last || w < 40 || h < 40) return;
      last = key;
      clearTimeout(t);
      t = setTimeout(() => {
        sizeCanvas();
        if (lastGraph?.nodes?.length) fitGraph();
      }, 100);
    });
    ro.observe(shell);
  }
}

async function boot() {
  await customElements.whenDefined("falkordb-canvas");
  configureCanvas();
  wireGraphControls();
  snapshot = await api("/api/bootstrap");
  renderSnapshot(snapshot);
  setDecidePath("review");
  if (!snapshot.graph_preview) drawGraph(null);

  const params = new URLSearchParams(window.location.search);
  if (params.get("github") === "connected") {
    history.replaceState({}, "", "/");
    $("action-result").textContent = `GitHub connected as ${githubStatus.login || "user"}.`;
  } else if (params.get("github_error")) {
    const err = params.get("github_error");
    history.replaceState({}, "", "/");
    showError("review-error", new Error(`GitHub OAuth: ${err}`));
  }

  window.addEventListener("resize", () => {
    sizeCanvas();
    if (current?.record?.graph) drawGraph(current.record.graph);
    else if (previewGraph) drawGraph(previewGraph);
  });
}

$("ingest-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  showError("ingest-error", null);
  showError("scope-error", null);
  const target = $("target").value.trim();
  if (!target) {
    showError("ingest-error", new Error("Paste a GitHub URL or folder path."));
    return;
  }
  $("files").value = "";
  $("base").value = "";
  $("head").value = "";
  const button = $("analyze-btn");
  button.disabled = true;
  setBusy(true, "analyze");
  setWorkflowStep(1);
  try {
    const body = await api("/api/ingest", {
      method: "POST",
      body: JSON.stringify({ target }),
    });
    renderSnapshot(body.snapshot);
    clearDecision();
    if (body.snapshot?.graph_preview) drawGraph(body.snapshot.graph_preview);
    setWorkflowStep((body.snapshot?.changes || []).length ? 3 : 2);
  } catch (error) {
    showError("ingest-error", error);
  } finally {
    setBusy(false);
    button.disabled = false;
  }
});

$("rescope").addEventListener("click", async () => {
  showError("scope-error", null);
  if (!hasScopeInput()) {
    showError("scope-error", new Error("Pick a file or type a path."));
    return;
  }
  setBusy(true, "scope");
  setWorkflowStep(2);
  try {
    const body = await api("/api/change", {
      method: "POST",
      body: JSON.stringify(scopePayload()),
    });
    renderSnapshot(body.snapshot || body);
    clearDecision();
    setWorkflowStep(3);
  } catch (error) {
    showError("scope-error", error);
  } finally {
    setBusy(false);
    $("rescope").disabled = snapshot?.dataset !== "repository";
  }
});

$("mode").addEventListener("change", () => {
  const mode = $("mode").value;
  if (current?.payload?.runs?.[mode]) renderRecord(current.payload, mode, current.agentMeta);
});

for (const btn of document.querySelectorAll(".seg-btn")) {
  btn.addEventListener("click", () => setDecidePath(btn.dataset.path));
}

for (const btn of document.querySelectorAll(".step")) {
  btn.addEventListener("click", () => {
    const n = Number(btn.dataset.step);
    const block = $(n === 1 ? "block-analyze" : n === 2 ? "block-scope" : "block-decide");
    block?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    setWorkflowStep(n);
  });
}

$("run").addEventListener("click", runDecide);

$("act").addEventListener("click", async () => {
  if (!current) return;
  setBusy(true, "act");
  try {
    const body = await api("/api/actions", {
      method: "POST",
      body: JSON.stringify({ change_id: current.record.change_id, mode: current.mode }),
    });
    $("action-result").textContent = `${body.action?.status || ""}. ${body.action?.reason || ""}`;
  } catch (error) {
    showError("review-error", error);
  } finally {
    setBusy(false);
  }
});

$("notify").addEventListener("click", async () => {
  if (!current) return;
  setBusy(true, "notify");
  try {
    const body = await api("/api/notify", {
      method: "POST",
      body: JSON.stringify({
        change_id: current.record.change_id,
        mode: current.mode,
        send: false,
      }),
    });
    const note = body.notification || {};
    $("action-result").textContent =
      `${note.status}. ${note.detail || ""}` + (note.draft_path ? ` · ${note.draft_path}` : "");
  } catch (error) {
    showError("review-error", error);
  } finally {
    setBusy(false);
  }
});

$("gh-logout")?.addEventListener("click", async () => {
  try {
    const body = await api("/auth/github/logout", { method: "POST", body: "{}" });
    renderGithub(body.github || { connected: false, oauth_configured: true });
    $("action-result").textContent = "GitHub disconnected.";
  } catch (error) {
    showError("review-error", error);
  }
});

$("repo-select")?.addEventListener("change", () => {
  const url = $("repo-select").value;
  if (url) $("target").value = url;
});

$("repo-select")?.addEventListener("dblclick", () => {
  const url = $("repo-select").value;
  if (!url) return;
  $("target").value = url;
  $("ingest-form").requestSubmit();
});

$("repo-search")?.addEventListener("input", () => {
  clearTimeout(repoSearchTimer);
  repoSearchTimer = setTimeout(() => {
    loadGithubRepos($("repo-search").value.trim());
  }, 280);
});

$("pr-select")?.addEventListener("change", () => {
  const value = $("pr-select").value;
  if (value && $("pr-number")) $("pr-number").value = value;
});

$("pr-comment")?.addEventListener("click", async () => {
  if (!current) return;
  if (!githubStatus.connected) {
    showError("review-error", new Error("Connect GitHub first."));
    return;
  }
  const prValue = Number($("pr-number").value || $("pr-select")?.value || 0);
  if (!prValue) {
    showError(
      "review-error",
      new Error("No open PR selected. Use Open issue + PR, or enter a PR number.")
    );
    return;
  }
  setBusy(true, "pr");
  showError("review-error", null);
  try {
    const body = await api("/api/github/pr-comment", {
      method: "POST",
      body: JSON.stringify({
        change_id: current.record.change_id,
        mode: current.mode,
        pr: prValue,
        check: Boolean($("pr-check")?.checked),
        dry_run: false,
      }),
    });
    const gh = body.github || {};
    $("action-result").textContent =
      `${gh.status}. ${gh.detail || ""}` + (gh.comment_url ? ` ${gh.comment_url}` : "");
  } catch (error) {
    showError("review-error", error);
  } finally {
    setBusy(false);
  }
});

$("pr-open")?.addEventListener("click", async () => {
  if (!current) return;
  if (!githubStatus.connected) {
    showError("review-error", new Error("Connect GitHub first."));
    return;
  }
  setBusy(true, "openpr");
  showError("review-error", null);
  try {
    const body = await api("/api/github/open-decision", {
      method: "POST",
      body: JSON.stringify({
        change_id: current.record.change_id,
        mode: current.mode,
        dry_run: false,
      }),
    });
    const gh = body.github || {};
    const bits = [gh.status, gh.detail].filter(Boolean);
    if (gh.issue_url) bits.push(gh.issue_url);
    if (gh.pr_url) bits.push(gh.pr_url);
    $("action-result").textContent = bits.join(" · ");
    if (gh.pr_number && $("pr-number")) $("pr-number").value = String(gh.pr_number);
    await loadGithubPulls();
    if (gh.pr_number && $("pr-select")) $("pr-select").value = String(gh.pr_number);
  } catch (error) {
    showError("review-error", error);
  } finally {
    setBusy(false);
  }
});

boot().catch((error) => showError("review-error", error));
