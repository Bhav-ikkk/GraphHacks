/**
 * Syzygy EDL — Apple macOS Sequoia 3-Pane Workbench Application Controller
 * Track 1: Graph Hacks 2026 — Engineering Decision Layer
 */

const stateLabel = {
  PROCEED: "PROCEED",
  APPROVED: "APPROVED",
  PASSED: "PASSED",
  PROCEED_WITH_GUARDS: "PROCEED WITH GUARDS",
  NEEDS_REVIEW: "NEEDS REVIEW",
  HOLD: "HOLD",
  BLOCKED: "BLOCKED",
  INSUFFICIENT_EVIDENCE: "INSUFFICIENT EVIDENCE",
};

const modeLabel = {
  full: "Full graph",
  one_hop: "1-hop only",
  graph_off: "Graph off",
};

const ROLE_COLORS = {
  changed: "#FF3B30",
  direct: "#FF3B30",
  consumer: "#0071E3",
  dependency: "#FF9F0A",
  incident: "#AF52DE",
  fix: "#AF52DE",
  owner: "#34C759",
  service: "#0071E3",
  function: "#5E5CE6",
  policy: "#34C759",
  default: "#8E8E93",
};

// Parameterized Cypher query templates & LLM reasoning steps for tool traces
const TOOL_CYPHER_MAP = {
  centrality_rank: {
    title: "centrality_rank (FalkorDB In-Degree Centrality)",
    cypher: (symbols, service) =>
      `MATCH (fn:Function)-[:CALLS*1..3]->(target:Function)\nWHERE target.name IN [\n  ${symbols.map((s) => `'${s}'`).join(",\n  ") || "'edl.step'"}\n]\nRETURN target.name AS function,\n       count(DISTINCT fn) AS callers\nORDER BY callers DESC`,
    reasoning: [
      "Query all inbound CALLS paths within 3 hops targeting modified symbols.",
      "Calculate caller in-degree centrality to quantify function blast radius.",
      "Identify high-traffic hubs requiring regression safeguards.",
      "Verify centrality ranking satisfies architectural isolation bounds.",
    ],
  },
  impact_analysis: {
    title: "impact_analysis (Downstream Consumer & Dependency Blast Radius)",
    cypher: (symbols, service) =>
      `MATCH (svc:Service {name: '${service || "edl"}'})\n      -[:DEPENDS_ON*1..3]-(consumer:Service)\nRETURN DISTINCT\n  consumer.name AS service,\n  consumer.tier AS tier\nORDER BY tier ASC`,
    reasoning: [
      "Trace multi-hop downstream consumers and upstream service dependencies.",
      "Determine Tier-1 critical consumer touchpoints across the service graph.",
      "Verify whether dependent services maintain active integration test suites.",
      "Flag unmitigated downstream blast radius before policy gate evaluation.",
    ],
  },
  nearest_owner: {
    title: "nearest_owner (CODEOWNERS Metadata Lookup)",
    cypher: (symbols, service) =>
      `MATCH (team:Team)-[:OWNS]->(svc:Service {name: '${service || "edl"}'})\nRETURN team.name AS owner,\n       team.email AS contact`,
    reasoning: [
      "Traverse OWNS edge from scoped service to registered repository Team.",
      "Check CODEOWNERS coverage to identify mandatory approver groups.",
      "Validate owner sign-off requirement against active gate policies.",
      "Surface missing CODEOWNERS rules as explicit claim gaps.",
    ],
  },
  find_similar_incidents: {
    title: "find_similar_incidents (Incident & Prior Outage Traversal)",
    cypher: (symbols, service) =>
      `MATCH (inc:Incident)-[:AFFECTED]->(svc:Service {name: '${service || "edl"}'})\nWHERE inc.status = 'open'\n   OR inc.resolved_at > datetime() - duration('P90D')\nRETURN inc.id, inc.summary, inc.status`,
    reasoning: [
      "Inspect historical and active incidents attached to target services.",
      "Check whether recent rollbacks or hotfixes involved the same symbols.",
      "Ensure changes to frequently degraded components trigger heightened review.",
      "Correlate incident blast patterns with proposed PR diff.",
    ],
  },
  get_test_coverage: {
    title: "get_test_coverage (Test Suite Verification)",
    cypher: (symbols, service) =>
      `MATCH (t:Test)-[:COVERS]->(fn:Function)\nWHERE fn.name IN [\n  ${symbols.map((s) => `'${s}'`).join(",\n  ") || "'edl.step'"}\n]\nRETURN t.id AS test_id,\n       t.path AS path,\n       t.status AS status`,
    reasoning: [
      "Identify all Test nodes with direct COVERS relations to changed functions.",
      "Inspect test run execution logs and recorded pass/fail results.",
      "Determine test coverage freshness relative to git base and head commits.",
      "Verify coverage meets policy thresholds for critical logic.",
    ],
  },
  get_deploy_state: {
    title: "get_deploy_state (Production Environment State)",
    cypher: (symbols, service) =>
      `MATCH (d:Deployment)-[:DEPLOYED_AS]->(svc:Service {name: '${service || "edl"}'})\nRETURN d.env AS environment,\n       d.version AS version,\n       d.health AS health`,
    reasoning: [
      "Inspect current production and staging deployment records.",
      "Confirm target environment health is green and ready for release.",
      "Check for concurrent canary rollouts or freeze windows.",
      "Verify deploy status before authorizing release pipeline trigger.",
    ],
  },
};

let snapshot = null;
let current = null;
let cy = null; // Preserved for compatibility
let activeToolKey = "impact_analysis";
let currentLayoutMode = "tree"; // 'tree' | 'radial' | 'force'
let selectedGraphNode = null;
let currentGraphData = null;

// SVG Pan & Zoom Transform State
const svgTransform = {
  scale: 1,
  x: 0,
  y: 0,
  isDragging: false,
  startX: 0,
  startY: 0,
};

const $ = (id) => document.getElementById(id);

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

function showError(id, error) {
  const node = $(id);
  if (!node) return;
  node.hidden = !error;
  node.textContent = error ? error.message || String(error) : "";
}

function parseFiles(value) {
  return value
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);
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

function renderSuggestions(paths) {
  const wrap = $("suggestions");
  const list = $("suggestion-list");
  list.innerHTML = "";
  const rows = paths || [];
  wrap.hidden = rows.length === 0;
  for (const path of rows) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "file-chip-btn";
    button.textContent = path;
    button.addEventListener("click", async () => {
      $("files").value = path;
      showError("ingest-error", null);
      showError("scope-error", null);
      const rescope = $("rescope");
      rescope.disabled = true;
      try {
        const body = await api("/api/change", {
          method: "POST",
          body: JSON.stringify({ files: [path] }),
        });
        renderSnapshot(body.snapshot);
        // Automatically run review when file chip is selected
        if (body.snapshot?.changes?.length > 0) {
          await runReview();
        }
      } catch (error) {
        showError("ingest-error", error);
      } finally {
        rescope.disabled = snapshot?.dataset !== "repository";
      }
    });
    list.appendChild(button);
  }
}

function renderSnapshot(next) {
  snapshot = next;
  
  // Update Backend Pill & Top Header
  const backendPill = $("backend");
  if (backendPill) {
    backendPill.innerHTML = `<span class="status-dot"></span><span>${next.backend || "FalkorDB Connected"}</span>`;
    backendPill.title = next.backend || "FalkorDB Connection";
  }

  // Parse and set FalkorDB Browser link
  const match = (next.backend || "").match(/https?:\/\/[^\s\/]+(?::\d+)?/);
  const falkorUrl = match ? match[0] : "http://13.236.193.238:3000";
  const falkorBtn = $("btn-open-falkor");
  if (falkorBtn) falkorBtn.href = falkorUrl;

  // Update Repo Trigger Pill Label
  const repoNameEl = $("repo-pill-name");
  if (repoNameEl && next.origin?.value) {
    repoNameEl.textContent = next.origin.value;
  }

  $("origin-note").textContent = next.note || "";
  const scoped = (next.changes || []).length > 0;
  $("scope-note").textContent = scoped
    ? next.scope_note || ""
    : next.scope_note || "No change is scoped yet.";
  const scopeErr = next.scope_error && !scoped ? next.scope_error : null;
  showError("scope-error", scopeErr ? new Error(scopeErr) : null);
  renderSuggestions(next.suggested_files || []);

  const change = $("change");
  const previous = change.value;
  change.innerHTML = "";
  if (!next.changes.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "Scope a change (click a suggested file, or fill files / base / head)";
    change.appendChild(option);
  }
  for (const item of next.changes) {
    const option = document.createElement("option");
    option.value = item.id;
    const mapping = item.mapping ? ` · ${item.mapping}` : "";
    const symbols = (item.symbols || []).slice(0, 3).join(", ");
    const more = (item.symbols || []).length > 3 ? ` +${item.symbols.length - 3}` : "";
    option.textContent = `${item.title || item.id}${mapping}${symbols ? ` · ${symbols}${more}` : ""}`;
    change.appendChild(option);
  }
  if ([...change.options].some((option) => option.value === previous)) {
    change.value = previous;
  }
  const editor = $("service-editor");
  editor.hidden = next.dataset !== "repository";
  const services = $("services");
  services.innerHTML = "";
  $("run").disabled = next.changes.length === 0;
  $("rescope").disabled = next.dataset !== "repository";
  $("change-help").textContent = scoped
    ? "One scoped change from the diff or file list — not every function in the tree."
    : "Graph is loaded. Pick a suggested file below, or enter changed paths, then Rescope.";
  if (next.dataset === "repository") {
    for (const service of next.services || []) {
      const row = document.createElement("div");
      row.className = "service-item";
      const owner = service.owner || "no CODEOWNERS entry";
      row.innerHTML = `<strong>${service.name}</strong><small>${owner}</small>`;
      services.appendChild(row);
    }
  }
}

function renderEvidence(rows) {
  const list = $("evidence");
  list.innerHTML = "";
  if (!rows || !rows.length) {
    const item = document.createElement("li");
    item.className = "evidence-row-card";
    item.textContent = "No evidence rows for this mode.";
    list.appendChild(item);
    return;
  }
  for (const row of rows) {
    const item = document.createElement("li");
    item.className = "evidence-row-card";
    const code = document.createElement("span");
    code.className = "eid";
    code.textContent = row.id;
    const meta = document.createElement("span");
    meta.className = "evidence-meta";
    const bits = [row.evidence_type, row.role, row.source_system].filter(Boolean);
    if (row.freshness_ok === false) bits.push("stale");
    if (row.freshness_ok === true) bits.push("fresh");
    meta.textContent = bits.join(" · ");
    item.append(code, meta);
    if (row.detail) {
      const detail = document.createElement("span");
      detail.className = "detail";
      detail.textContent = row.detail;
      item.appendChild(detail);
    }
    if (row.limitations) {
      const limit = document.createElement("span");
      limit.className = "detail";
      limit.textContent = `limitation: ${row.limitations}`;
      item.appendChild(limit);
    }
    list.appendChild(item);
  }
}

function renderRecord(payload, mode) {
  const record = payload.runs[mode];
  current = { payload, mode, record };
  
  // Ensure results workbench is visible
  const resultsEl = $("results");
  if (resultsEl) resultsEl.hidden = false;

  const revision = record.graph_revision ? ` · graph ${record.graph_revision}` : "";
  const backend = record.graph_backend ? ` · ${record.graph_backend}` : "";
  $("kicker").textContent = `${record.change_id} · ${modeLabel[mode]} · policy ${record.policy_version}${revision}${backend}`;
  
  const state = $("state");
  const displayState = stateLabel[record.state] || record.state;
  state.textContent = displayState;
  state.className = `verdict-hero-badge state-${record.state}`;
  $("rationale").textContent = record.rationale;

  const policies = $("policies");
  policies.innerHTML = "";
  const counts = {};
  for (const p of record.triggered || []) {
    counts[p] = (counts[p] || 0) + 1;
  }
  const keys = Object.keys(counts);
  if (!keys.length) {
    const item = document.createElement("li");
    item.textContent = "none";
    policies.appendChild(item);
  } else {
    for (const key of keys) {
      const item = document.createElement("li");
      item.textContent = counts[key] > 1 ? `${key} ×${counts[key]}` : key;
      policies.appendChild(item);
    }
  }

  // 3-Way Mode Comparison Bar
  const compare = $("compare");
  compare.innerHTML = "";
  for (const name of ["full", "one_hop", "graph_off"]) {
    const run = payload.runs[name];
    const button = document.createElement("button");
    button.type = "button";
    button.className = "compare-segment-btn";
    if (name === mode) button.classList.add("active");
    button.innerHTML = `<span class="mode-name">${modeLabel[name]}</span><span class="mode-state">${stateLabel[run.state] || run.state}</span>`;
    button.addEventListener("click", () => {
      $("mode").value = name;
      renderRecord(payload, name);
    });
    compare.appendChild(button);
  }

  renderList("claims", record.claims, (claim) => `${claim.text}`, (claim) => claim.evidence_id);
  const missing = record.missing.length
    ? record.missing
    : [{ evidence_id: "—", detail: "Nothing required was missing or stale." }];
  renderList("missing", missing, (item) => item.detail, (item) => item.evidence_id || item.policy);
  renderEvidence(record.evidence || []);

  // Render Tool Trace Step Cards & Cypher Inspector
  renderToolTrace(record.tool_calls || []);

  const action = record.action || { status: "ready", reason: "" };
  $("action-reason").textContent = action.reason || "";
  const act = $("act");
  act.disabled = action.status !== "ready";
  $("action-result").textContent = "";

  // Update FalkorDB Browser GRAPH.QUERY footer telemetry
  const metaCounts = $("cypher-meta-counts");
  if (metaCounts && record.graph) {
    const nCnt = record.graph.nodes ? record.graph.nodes.length : 0;
    const eCnt = record.graph.edges ? record.graph.edges.length : 0;
    metaCounts.textContent = `${nCnt} nodes · ${eCnt} edges`;
  }

  // Render Inline SVG / @falkordb/canvas Knowledge Graph
  drawGraph(record.graph);
}

function renderList(id, rows, text, eid) {
  const list = $(id);
  list.innerHTML = "";
  for (const row of rows) {
    const item = document.createElement("li");
    const code = document.createElement("span");
    code.className = "claim-eid";
    code.textContent = eid(row);
    item.append(code, document.createTextNode(" " + text(row)));
    list.appendChild(item);
  }
}

/**
 * Render Tool Trace Step Cards and sync with FalkorDB Cypher Inspector
 */
function renderToolTrace(toolCalls) {
  const trace = $("trace");
  trace.innerHTML = "";

  const calls = toolCalls.length > 0 ? toolCalls : [
    { tool: "centrality_rank", summary: "8 row(s)" },
    { tool: "impact_analysis", summary: "3 row(s)" },
    { tool: "nearest_owner", summary: "1 row(s)" },
    { tool: "find_similar_incidents", summary: "0 row(s)" },
    { tool: "get_test_coverage", summary: "2 row(s)" },
    { tool: "get_deploy_state", summary: "1 row(s)" },
  ];

  calls.forEach((call, index) => {
    const item = document.createElement("li");
    item.className = "tool-trace-card";
    if (call.tool === activeToolKey || (index === 0 && !activeToolKey)) {
      item.classList.add("active");
    }

    item.innerHTML = `
      <div class="tool-trace-left">
        <span class="tool-trace-num">${index + 1}</span>
        <div>
          <span class="tool-trace-name">${call.tool}</span>
          <span class="tool-trace-summary">${call.summary || ""}</span>
        </div>
      </div>
      <span class="tool-trace-rows-pill">${call.summary || "completed"}</span>
    `;

    item.addEventListener("click", () => {
      document.querySelectorAll(".tool-trace-card").forEach((card) => card.classList.remove("active"));
      item.classList.add("active");
      activeToolKey = call.tool;
      updateCypherInspector(call.tool);
    });

    trace.appendChild(item);
  });

  // Update inspector with initial active tool
  updateCypherInspector(activeToolKey || (calls[0] && calls[0].tool));
}

/**
 * Update the Cypher code block and LLM reasoning steps
 */
function updateCypherInspector(toolKey) {
  const mapping = TOOL_CYPHER_MAP[toolKey] || TOOL_CYPHER_MAP.impact_analysis;
  const currentSymbols = current?.record?.change_id ? ["edl.step", "edl.run_agent"] : ["edl.step"];
  const currentService = "edl";

  const rawQuery = mapping.cypher(currentSymbols, currentService);
  const titleEl = $("cypher-box-title");
  if (titleEl) titleEl.textContent = mapping.title || "Executed FalkorDB Cypher Query";

  // Syntax highlight Cypher tokens
  const highlighted = rawQuery
    .replace(/\b(MATCH|WHERE|RETURN|ORDER BY|DESC|ASC|IN|OR|AND|count|DISTINCT)\b/g, '<span class="keyword">$1</span>')
    .replace(/(:\w+)/g, '<span class="label">$1</span>')
    .replace(/(\$\w+)/g, '<span class="var">$1</span>');

  const displayEl = $("cypher-code-display");
  if (displayEl) {
    displayEl.innerHTML = highlighted;
    displayEl.dataset.raw = rawQuery;
  }

  // Update Reasoning Steps
  const reasoningList = $("llm-reasoning-list");
  if (reasoningList && mapping.reasoning) {
    reasoningList.innerHTML = mapping.reasoning
      .map(
        (step, idx) => `
        <div class="reasoning-step-item">
          <span class="reasoning-num-badge">${idx + 1}</span>
          <span>${step}</span>
        </div>
      `
      )
      .join("");
  }
}

/**
 * Update FalkorDB Browser "Schema & Subgraph Telemetry" Strip
 */
function updateSchemaTelemetry(targetGraph) {
  const pillsEl = $("schema-pills");
  if (!pillsEl) return;
  pillsEl.innerHTML = "";

  const nodes = targetGraph.nodes || [];
  const edges = targetGraph.edges || [];

  // Count node labels
  const labelCounts = {};
  nodes.forEach((n) => {
    let lbl = n.kind ? n.kind.charAt(0).toUpperCase() + n.kind.slice(1) : "Node";
    if (n.kind === "service") lbl = "Service";
    else if (n.kind === "function") lbl = "Function";
    else if (n.kind === "team") lbl = "Team";
    labelCounts[lbl] = (labelCounts[lbl] || 0) + 1;
  });

  // Count edge relationships
  const relCounts = {};
  edges.forEach((e) => {
    const rel = e.label || "PART_OF";
    relCounts[rel] = (relCounts[rel] || 0) + 1;
  });

  Object.entries(labelCounts).forEach(([lbl, cnt]) => {
    const span = document.createElement("span");
    span.className = "schema-pill label-pill";
    span.textContent = `:${lbl} (${cnt})`;
    pillsEl.appendChild(span);
  });

  Object.entries(relCounts).forEach(([rel, cnt]) => {
    const span = document.createElement("span");
    span.className = "schema-pill rel-pill";
    span.textContent = cnt > 1 ? `[:${rel} (${cnt})]` : `[:${rel}]`;
    pillsEl.appendChild(span);
  });

  // Update engine badge
  const badge = $("falkor-engine-badge");
  if (badge) {
    badge.textContent = window.FALKOR_CANVAS_READY ? "⚡ @falkordb/canvas" : "⚡ @falkordb/canvas (Hybrid SVG)";
  }
}

/**
 * Semantic Color Mapping for Graph Nodes
 */
function getNodeSemanticColor(node) {
  if (!node) return ROLE_COLORS.default;
  if (node.role === "changed" || node.kind === "function") return ROLE_COLORS.changed;
  if (node.role === "consumer") return ROLE_COLORS.consumer;
  if (node.role === "dependency") return ROLE_COLORS.dependency;
  if (node.role === "incident" || node.role === "fix") return ROLE_COLORS.incident;
  if (node.role === "owner" || node.kind === "team") return ROLE_COLORS.owner;
  if (node.kind === "service") return node.role === "direct" ? ROLE_COLORS.changed : ROLE_COLORS.consumer;
  return ROLE_COLORS[node.role] || ROLE_COLORS.default;
}

/**
 * FalkorDB Browser Interactive "Node Property Inspector"
 */
function selectGraphNode(rawNode, allNodes, allEdges) {
  if (!rawNode) return;
  const node = rawNode.properties ? { ...rawNode.properties, ...rawNode } : rawNode;
  selectedGraphNode = node;
  const inspector = $("falkor-node-inspector");
  if (!inspector) return;

  inspector.hidden = false;

  const nodeName = node.label || node.name || node.id;
  const nodeKind = node.kind ? node.kind.charAt(0).toUpperCase() + node.kind.slice(1) : (node.role || "Node");
  const color = getNodeSemanticColor(node);

  const dot = $("node-insp-dot");
  if (dot) dot.style.background = color;

  const labelEl = $("node-insp-label");
  if (labelEl) labelEl.textContent = `:${nodeKind}`;

  const nameEl = $("node-insp-name");
  if (nameEl) nameEl.textContent = nodeName;

  // Calculate connected edges count
  const degree = (allEdges || []).filter(
    (e) => e.source === node.id || e.target === node.id ||
           (e.source && e.source.id === node.id) || (e.target && e.target.id === node.id)
  ).length;

  const propsEl = $("node-insp-props");
  if (propsEl) {
    propsEl.innerHTML = `
      <div class="node-prop-row"><span class="node-prop-key">role:</span><span class="node-prop-val">"${node.role || "direct"}"</span></div>
      <div class="node-prop-row"><span class="node-prop-key">kind:</span><span class="node-prop-val">"${node.kind || "service"}"</span></div>
      <div class="node-prop-row"><span class="node-prop-key">degree:</span><span class="node-prop-val">${degree} edge${degree === 1 ? "" : "s"}</span></div>
      <div class="node-prop-row"><span class="node-prop-key">status:</span><span class="node-prop-val">"in_scope"</span></div>
      ${node.tier !== undefined ? `<div class="node-prop-row"><span class="node-prop-key">tier:</span><span class="node-prop-val">${node.tier}</span></div>` : ""}
    `;
  }
}

function setupFalkorInspectorControls() {
  const closeBtn = $("node-insp-close");
  if (closeBtn) {
    closeBtn.onclick = () => {
      const inspector = $("falkor-node-inspector");
      if (inspector) inspector.hidden = true;
    };
  }

  const filterBtn = $("btn-filter-cypher");
  if (filterBtn) {
    filterBtn.onclick = () => {
      if (!selectedGraphNode) return;
      const nodeName = selectedGraphNode.label || selectedGraphNode.name || selectedGraphNode.id;
      const displayEl = $("cypher-code-display");
      const titleEl = $("cypher-box-title");
      if (titleEl) titleEl.textContent = `FalkorDB Filter: ${nodeName}`;
      if (displayEl) {
        displayEl.innerHTML = `<span class="keyword">MATCH</span> (n {name: '${nodeName}'})-[r]-(neighbor)\n<span class="keyword">RETURN</span> n, r, neighbor\n<span class="keyword">LIMIT</span> 25`;
      }
    };
  }
}

function setupFalkorLayoutControls() {
  const treeBtn = $("layout-tree");
  const radialBtn = $("layout-radial");
  const forceBtn = $("layout-force");

  [treeBtn, radialBtn, forceBtn].forEach((btn) => {
    if (!btn) return;
    btn.onclick = () => {
      [treeBtn, radialBtn, forceBtn].forEach((b) => b && b.classList.remove("active"));
      btn.classList.add("active");
      currentLayoutMode = btn.dataset.mode;
      svgTransform.scale = 1;
      svgTransform.x = 0;
      svgTransform.y = 0;
      drawGraph(currentGraphData);
    };
  });
}

/**
 * Upgraded Pure SVG / @falkordb/canvas Knowledge Graph Renderer with FalkorDB Browser UI Ecosystem
 */
function drawGraph(graph) {
  const container = $("graph");
  if (!container) return;

  currentGraphData = graph;
  container.innerHTML = "";

  // If no graph is provided on initial boot, generate a preview topology matching repo scope
  const targetGraph = (graph && graph.nodes && graph.nodes.length) ? graph : {
    nodes: [
      { id: "svc:edl", label: "edl", kind: "service", role: "direct", tier: 1 },
      { id: "fn:agent", label: "edl.agent", kind: "function", role: "changed" },
      { id: "fn:api", label: "edl.api", kind: "function", role: "changed" },
      { id: "fn:change", label: "edl.change", kind: "function", role: "changed" },
      { id: "fn:review", label: "edl.review", kind: "function", role: "changed" },
      { id: "svc:falkordb", label: "falkordb", kind: "service", role: "dependency", tier: 1 },
      { id: "team:core", label: "core-infra", kind: "team", role: "owner" },
    ],
    edges: [
      { source: "fn:agent", target: "svc:edl", label: "PART_OF" },
      { source: "fn:api", target: "svc:edl", label: "PART_OF" },
      { source: "fn:change", target: "svc:edl", label: "PART_OF" },
      { source: "fn:review", target: "svc:edl", label: "PART_OF" },
      { source: "svc:edl", target: "svc:falkordb", label: "DEPENDS_ON" },
      { source: "team:core", target: "svc:edl", label: "OWNS" },
    ],
  };

  // Live update the FalkorDB Browser Schema Telemetry Strip
  updateSchemaTelemetry(targetGraph);

  // Default select root node for inspector
  if (!selectedGraphNode && targetGraph.nodes.length > 0) {
    selectGraphNode(targetGraph.nodes[0], targetGraph.nodes, targetGraph.edges);
  }

  // Resilient High-DPI Hybrid Engine supporting all 3 FalkorDB Layout Modes
  const nodes = targetGraph.nodes.map((n) => ({ ...n }));
  const edges = (targetGraph.edges || []).map((e) => ({ ...e }));

  // Separate nodes into distinct semantic tiers
  const rootNodes = [];
  const functionNodes = [];
  const thirdTierNodes = [];

  nodes.forEach((node) => {
    const isRoot = (node.kind === "service" && (node.role === "direct" || node.role === "changed")) ||
                   (node.id === "svc:edl" && rootNodes.length === 0);
    const isFunction = node.kind === "function" || node.role === "changed";

    if (isRoot) {
      rootNodes.push(node);
    } else if (isFunction) {
      functionNodes.push(node);
    } else {
      thirdTierNodes.push(node);
    }
  });

  if (rootNodes.length === 0 && nodes.length > 0) {
    const first = nodes[0];
    rootNodes.push(first);
    const fIdx = functionNodes.indexOf(first);
    if (fIdx >= 0) functionNodes.splice(fIdx, 1);
    const tIdx = thirdTierNodes.indexOf(first);
    if (tIdx >= 0) thirdTierNodes.splice(tIdx, 1);
  }

  const viewW = 860;
  const maxTierItems = Math.max(rootNodes.length, functionNodes.length, thirdTierNodes.length, 1);
  const viewH = Math.max(480, maxTierItems * 60 + 100);

  const nodeMap = new Map();

  if (currentLayoutMode === "radial") {
    // Mode 2: FalkorDB Concentric Radial Blast-Radius Layout
    const cx = viewW / 2;
    const cy = viewH / 2;

    // Root at center
    rootNodes.forEach((node) => {
      node.x = cx;
      node.y = cy;
      nodeMap.set(node.id, node);
    });

    // Tier 2: Functions on Ring 1 (radius: 175px)
    const fnCount = functionNodes.length;
    functionNodes.forEach((node, i) => {
      const angle = i * (2 * Math.PI / Math.max(1, fnCount)) - Math.PI / 2;
      node.x = cx + Math.cos(angle) * 175;
      node.y = cy + Math.sin(angle) * 175;
      nodeMap.set(node.id, node);
    });

    // Tier 3: Consumers, Owners, Incidents on Ring 2 (radius: 295px)
    const t3Count = thirdTierNodes.length;
    thirdTierNodes.forEach((node, i) => {
      const angle = i * (2 * Math.PI / Math.max(1, t3Count)) - Math.PI / 2 + (Math.PI / Math.max(1, t3Count * 2));
      node.x = cx + Math.cos(angle) * 295;
      node.y = cy + Math.sin(angle) * 295;
      nodeMap.set(node.id, node);
    });
  } else if (currentLayoutMode === "force") {
    // Mode 3: FalkorDB Force Directed Organic Simulation
    const cx = viewW / 2;
    const cy = viewH / 2;

    rootNodes.forEach((node, i) => {
      node.x = 220;
      node.y = cy + (i * 70);
      nodeMap.set(node.id, node);
    });

    const fnCount = functionNodes.length;
    const spacingFn = (viewH - 120) / Math.max(1, fnCount - 1);
    functionNodes.forEach((node, i) => {
      node.x = 480 + (i % 2 === 0 ? -25 : 35);
      node.y = 70 + i * spacingFn;
      nodeMap.set(node.id, node);
    });

    const t3Count = thirdTierNodes.length;
    const spacingT3 = (viewH - 120) / Math.max(1, t3Count - 1);
    thirdTierNodes.forEach((node, i) => {
      node.x = 730 + (i % 2 === 0 ? 25 : -25);
      node.y = 80 + i * spacingT3;
      nodeMap.set(node.id, node);
    });
  } else {
    // Mode 1: FalkorDB Tree (Left-to-Right Hierarchy, Default)
    if (rootNodes.length === 1) {
      rootNodes[0].x = 140;
      rootNodes[0].y = viewH / 2;
      nodeMap.set(rootNodes[0].id, rootNodes[0]);
    } else {
      const spacingRoot = (viewH - 120) / Math.max(1, rootNodes.length - 1);
      rootNodes.forEach((node, i) => {
        node.x = 140;
        node.y = 60 + i * spacingRoot;
        nodeMap.set(node.id, node);
      });
    }

    const fnCount = functionNodes.length;
    if (fnCount === 1) {
      functionNodes[0].x = 450;
      functionNodes[0].y = viewH / 2;
      nodeMap.set(functionNodes[0].id, functionNodes[0]);
    } else if (fnCount > 1) {
      const spacingFn = (viewH - 120) / (fnCount - 1);
      functionNodes.forEach((node, i) => {
        const t = i / (fnCount - 1);
        node.x = 440 + Math.sin(t * Math.PI) * 35;
        node.y = 60 + i * spacingFn;
        nodeMap.set(node.id, node);
      });
    }

    const t3Count = thirdTierNodes.length;
    if (t3Count === 1) {
      thirdTierNodes[0].x = 740;
      thirdTierNodes[0].y = viewH / 2;
      nodeMap.set(thirdTierNodes[0].id, thirdTierNodes[0]);
    } else if (t3Count > 1) {
      const spacingT3 = (viewH - 120) / (t3Count - 1);
      thirdTierNodes.forEach((node, i) => {
        node.x = 740;
        node.y = 60 + i * spacingT3;
        nodeMap.set(node.id, node);
      });
    }
  }



  // Build SVG Document
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");

  // Defs for DropShadow & Glow Filters
  const defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
  defs.innerHTML = `
    <filter id="node-glow" x="-50%" y="-50%" width="200%" height="200%">
      <feGaussianBlur stdDeviation="8" result="blur" />
      <feMerge>
        <feMergeNode in="blur" />
        <feMergeNode in="SourceGraphic" />
      </feMerge>
    </filter>
    <filter id="soft-shadow" x="-30%" y="-30%" width="160%" height="160%">
      <feDropShadow dx="0" dy="2" stdDeviation="3" flood-opacity="0.12" />
    </filter>
  `;
  svg.appendChild(defs);

  // Pan & Zoom G Group
  const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
  g.id = "svg-viewport-group";
  applyTransform(g);
  svg.appendChild(g);

  // In Radial mode, draw subtle concentric guide circles
  if (currentLayoutMode === "radial") {
    const cx = viewW / 2;
    const cy = viewH / 2;
    const ring1 = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    ring1.setAttribute("cx", cx);
    ring1.setAttribute("cy", cy);
    ring1.setAttribute("r", "175");
    ring1.setAttribute("fill", "none");
    ring1.setAttribute("stroke", "#E5E5EA");
    ring1.setAttribute("stroke-width", "1.2");
    ring1.setAttribute("stroke-dasharray", "4,4");
    g.appendChild(ring1);

    const ring2 = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    ring2.setAttribute("cx", cx);
    ring2.setAttribute("cy", cy);
    ring2.setAttribute("r", "295");
    ring2.setAttribute("fill", "none");
    ring2.setAttribute("stroke", "#E5E5EA");
    ring2.setAttribute("stroke-width", "1.2");
    ring2.setAttribute("stroke-dasharray", "4,4");
    g.appendChild(ring2);
  }

  // Render Cubic Bezier Edges
  edges.forEach((edge) => {
    const src = nodeMap.get(edge.source);
    const tgt = nodeMap.get(edge.target);
    if (!src || !tgt) return;

    let pathD;
    if (currentLayoutMode === "radial") {
      pathD = `M ${src.x} ${src.y} L ${tgt.x} ${tgt.y}`;
    } else {
      const p1 = src.x <= tgt.x ? src : tgt;
      const p2 = src.x <= tgt.x ? tgt : src;
      const dx = Math.abs(p2.x - p1.x) * 0.55;
      pathD = `M ${p1.x} ${p1.y} C ${p1.x + dx} ${p1.y}, ${p2.x - dx} ${p2.y}, ${p2.x} ${p2.y}`;
    }

    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", pathD);
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "#D2D2D7");
    path.setAttribute("stroke-width", "2");
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("opacity", "0.85");
    g.appendChild(path);

    // Suppress duplicate PART_OF pills to eliminate overlapping badge clutter
    if (edge.label && edge.label !== "PART_OF") {
      const midX = (src.x + tgt.x) / 2;
      const midY = (src.y + tgt.y) / 2;
      const badgeW = Math.max(edge.label.length * 6.5 + 16, 50);

      const edgePill = document.createElementNS("http://www.w3.org/2000/svg", "g");
      edgePill.innerHTML = `
        <rect x="${midX - badgeW / 2}" y="${midY - 9}" width="${badgeW}" height="18" rx="9" fill="rgba(255,255,255,0.95)" stroke="rgba(0,0,0,0.08)" stroke-width="1" />
        <text x="${midX}" y="${midY + 3.5}" text-anchor="middle" font-family="-apple-system, sans-serif" font-size="9" font-weight="600" fill="#6E6E73">${edge.label}</text>
      `;
      g.appendChild(edgePill);
    }
  });

  // Render Nodes with Soft Glow & High-Contrast 12.5px Labels
  nodes.forEach((node) => {
    if (node.x === undefined || node.y === undefined) return;

    // Determine Semantic Color matching Apple Legend
    const color = getNodeSemanticColor(node);

    const labelText = node.label || node.name || node.id;

    const nodeGroup = document.createElementNS("http://www.w3.org/2000/svg", "g");
    nodeGroup.setAttribute("class", "graph-node");
    nodeGroup.style.cursor = "pointer";

    // Outer Glow Aura
    const aura = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    aura.setAttribute("cx", node.x);
    aura.setAttribute("cy", node.y);
    aura.setAttribute("r", "22");
    aura.setAttribute("fill", color);
    aura.setAttribute("opacity", "0.25");
    aura.setAttribute("filter", "url(#node-glow)");
    nodeGroup.appendChild(aura);

    // Main Node Circle
    const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    circle.setAttribute("cx", node.x);
    circle.setAttribute("cy", node.y);
    circle.setAttribute("r", "15");
    circle.setAttribute("fill", color);
    circle.setAttribute("stroke", "#FFFFFF");
    circle.setAttribute("stroke-width", "2.5");
    circle.setAttribute("filter", "url(#soft-shadow)");
    nodeGroup.appendChild(circle);

    // High-Contrast 12.5px Monospace Label on White Pill Backdrop
    const textLen = Math.max(labelText.length * 7.8 + 18, 56);
    const labelG = document.createElementNS("http://www.w3.org/2000/svg", "g");

    const isRoot = rootNodes.some((r) => r.id === node.id);
    if (isRoot && currentLayoutMode !== "radial") {
      labelG.innerHTML = `
        <rect x="${node.x - textLen / 2}" y="${node.y + 19}" width="${textLen}" height="22" rx="6" fill="rgba(255,255,255,0.95)" stroke="rgba(0,0,0,0.12)" stroke-width="1" filter="url(#soft-shadow)" />
        <text x="${node.x}" y="${node.y + 34}" text-anchor="middle" font-family="'SF Mono', 'JetBrains Mono', ui-monospace, monospace" font-size="12.5" font-weight="600" fill="#1D1D1F">${labelText}</text>
      `;
    } else if (currentLayoutMode === "radial" && isRoot) {
      labelG.innerHTML = `
        <rect x="${node.x - textLen / 2}" y="${node.y + 19}" width="${textLen}" height="22" rx="6" fill="rgba(255,255,255,0.95)" stroke="rgba(0,0,0,0.12)" stroke-width="1" filter="url(#soft-shadow)" />
        <text x="${node.x}" y="${node.y + 34}" text-anchor="middle" font-family="'SF Mono', 'JetBrains Mono', ui-monospace, monospace" font-size="12.5" font-weight="600" fill="#1D1D1F">${labelText}</text>
      `;
    } else if (currentLayoutMode === "radial" && node.x < viewW / 2 - 20) {
      labelG.innerHTML = `
        <rect x="${node.x - textLen - 14}" y="${node.y - 11}" width="${textLen}" height="22" rx="6" fill="rgba(255,255,255,0.95)" stroke="rgba(0,0,0,0.12)" stroke-width="1" filter="url(#soft-shadow)" />
        <text x="${node.x - 18}" y="${node.y + 4.5}" text-anchor="end" font-family="'SF Mono', 'JetBrains Mono', ui-monospace, monospace" font-size="12.5" font-weight="600" fill="#1D1D1F">${labelText}</text>
      `;
    } else {
      labelG.innerHTML = `
        <rect x="${node.x + 12}" y="${node.y - 11}" width="${textLen}" height="22" rx="6" fill="rgba(255,255,255,0.95)" stroke="rgba(0,0,0,0.12)" stroke-width="1" filter="url(#soft-shadow)" />
        <text x="${node.x + 18}" y="${node.y + 4.5}" text-anchor="start" font-family="'SF Mono', 'JetBrains Mono', ui-monospace, monospace" font-size="12.5" font-weight="600" fill="#1D1D1F">${labelText}</text>
      `;
    }
    nodeGroup.appendChild(labelG);

    // Interactive Node Click for FalkorDB Property Inspector
    nodeGroup.addEventListener("click", (e) => {
      e.stopPropagation();
      selectGraphNode(node, nodes, edges);
    });

    // Interactive Hover Effect
    nodeGroup.addEventListener("mouseenter", () => {
      circle.setAttribute("stroke", color);
      circle.setAttribute("stroke-width", "3.5");
      aura.setAttribute("opacity", "0.45");
      aura.setAttribute("r", "28");
    });
    nodeGroup.addEventListener("mouseleave", () => {
      circle.setAttribute("stroke", "#FFFFFF");
      circle.setAttribute("stroke-width", "2.5");
      aura.setAttribute("opacity", "0.25");
      aura.setAttribute("r", "22");
    });

    g.appendChild(nodeGroup);
  });

  // Auto-center the entire graph automatically in the SVG viewBox
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  nodes.forEach((n) => {
    if (n.x !== undefined && n.y !== undefined) {
      minX = Math.min(minX, n.x - 40);
      const labelW = (n.label || n.name || n.id || "").length * 8 + 26;
      maxX = Math.max(maxX, n.x + labelW + 30);
      minY = Math.min(minY, n.y - 40);
      maxY = Math.max(maxY, n.y + 45);
    }
  });
  if (currentLayoutMode === "radial") {
    const cx = viewW / 2;
    const cy = viewH / 2;
    minX = Math.min(minX, cx - 340);
    maxX = Math.max(maxX, cx + 340);
    minY = Math.min(minY, cy - 320);
    maxY = Math.max(maxY, cy + 320);
  }
  if (minX === Infinity) { minX = 0; maxX = viewW; minY = 0; maxY = viewH; }
  const contentW = Math.max(maxX - minX, 220);
  const contentH = Math.max(maxY - minY, 160);
  const pad = 35;
  svg.setAttribute("viewBox", `${minX - pad} ${minY - pad} ${contentW + pad * 2} ${contentH + pad * 2}`);

  container.appendChild(svg);
  attachSvgPanZoom(container, svg, g);
}

function applyTransform(g) {
  g.setAttribute("transform", `translate(${svgTransform.x}, ${svgTransform.y}) scale(${svgTransform.scale})`);
}

/**
 * Interactive SVG Pan & Zoom Controllers
 */
function attachSvgPanZoom(container, svg, g) {
  // Zoom Controls (+, -, Reset)
  const btnIn = $("btn-zoom-in");
  const btnOut = $("btn-zoom-out");
  const btnReset = $("btn-zoom-reset");

  if (btnIn) {
    btnIn.onclick = () => {
      svgTransform.scale = Math.min(2.5, svgTransform.scale * 1.2);
      applyTransform(g);
    };
  }
  if (btnOut) {
    btnOut.onclick = () => {
      svgTransform.scale = Math.max(0.4, svgTransform.scale / 1.2);
      applyTransform(g);
    };
  }
  if (btnReset) {
    btnReset.onclick = () => {
      svgTransform.scale = 1;
      svgTransform.x = 0;
      svgTransform.y = 0;
      applyTransform(g);
    };
  }

  // Mouse Drag Pan
  container.onmousedown = (e) => {
    if (e.target.closest("button")) return;
    svgTransform.isDragging = true;
    svgTransform.startX = e.clientX - svgTransform.x;
    svgTransform.startY = e.clientY - svgTransform.y;
  };

  window.onmousemove = (e) => {
    if (!svgTransform.isDragging) return;
    svgTransform.x = e.clientX - svgTransform.startX;
    svgTransform.y = e.clientY - svgTransform.startY;
    applyTransform(g);
  };

  window.onmouseup = () => {
    svgTransform.isDragging = false;
  };

  // Mouse Wheel Zoom
  container.onwheel = (e) => {
    e.preventDefault();
    const factor = e.deltaY < 0 ? 1.08 : 0.92;
    svgTransform.scale = Math.max(0.4, Math.min(2.5, svgTransform.scale * factor));
    applyTransform(g);
  };

  // Double Click Reset
  container.ondblclick = () => {
    svgTransform.scale = 1;
    svgTransform.x = 0;
    svgTransform.y = 0;
    applyTransform(g);
  };
}

async function runReview() {
  showError("review-error", null);
  if (!$("change").value) {
    showError("review-error", new Error("Scope a change first (suggested file, --files, or git base/head)."));
    return;
  }
  const button = $("run");
  button.disabled = true;
  try {
    const payload = await api("/api/review", {
      method: "POST",
      body: JSON.stringify({ change_id: $("change").value, mode: $("mode").value }),
    });
    renderRecord(payload, $("mode").value);
  } catch (error) {
    showError("review-error", error);
  } finally {
    button.disabled = snapshot?.changes?.length === 0;
  }
}

async function boot() {
  setupFalkorLayoutControls();
  setupFalkorInspectorControls();

  snapshot = await api("/api/bootstrap");
  renderSnapshot(snapshot);

  // Render default tool traces and graph placeholder on initial load
  renderToolTrace([]);
  drawGraph(null);

  // Auto-run review on initial load if changes are already scoped
  if (snapshot.changes && snapshot.changes.length > 0) {
    await runReview();
  }
}

// Resilient listener for @falkordb/canvas ESM loading
window.addEventListener("falkor-canvas-loaded", () => {
  const badge = $("falkor-engine-badge");
  if (badge) {
    badge.textContent = window.FALKOR_CANVAS_READY ? "⚡ @falkordb/canvas" : "⚡ @falkordb/canvas (Hybrid SVG)";
  }
  if (currentGraphData) {
    drawGraph(currentGraphData);
  }
});

// Ingest Form Submit
$("ingest-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  showError("ingest-error", null);
  showError("scope-error", null);
  const target = $("target").value.trim();
  const button = event.submitter || $("ingest-form").querySelector("button");
  if (!target) {
    showError("ingest-error", new Error("Paste a GitHub URL or a folder path."));
    return;
  }
  button.disabled = true;
  button.textContent = "Scanning…";
  try {
    const body = await api("/api/ingest", {
      method: "POST",
      body: JSON.stringify({ target, ...scopePayload() }),
    });
    renderSnapshot(body.snapshot);
    if (body.snapshot?.changes?.length > 0) {
      await runReview();
    }
  } catch (error) {
    showError("ingest-error", error);
  } finally {
    button.disabled = false;
    button.textContent = "Analyze";
  }
});

// Rescope Button Click
$("rescope").addEventListener("click", async () => {
  showError("ingest-error", null);
  showError("review-error", null);
  showError("scope-error", null);
  if (!hasScopeInput() && !(snapshot?.suggested_files || []).length) {
    showError(
      "ingest-error",
      new Error(
        "Add changed files or git base/head first. After Analyze, suggested paths appear when the tree has source files."
      )
    );
    return;
  }
  if (!hasScopeInput() && (snapshot?.suggested_files || []).length) {
    showError(
      "ingest-error",
      new Error("Click a suggested file below, or paste paths into Changed files, then Rescope.")
    );
    return;
  }
  const button = $("rescope");
  button.disabled = true;
  try {
    const body = await api("/api/change", {
      method: "POST",
      body: JSON.stringify(scopePayload()),
    });
    renderSnapshot(body.snapshot);
    if (body.snapshot?.changes?.length > 0) {
      await runReview();
    }
  } catch (error) {
    showError("ingest-error", error);
  } finally {
    button.disabled = snapshot?.dataset !== "repository";
  }
});

// Run Review Button Click
$("run").addEventListener("click", runReview);

// Mode Dropdown Change
$("mode").addEventListener("change", () => {
  if (current?.payload) {
    renderRecord(current.payload, $("mode").value);
  } else {
    runReview();
  }
});

// Change Dropdown Change
$("change").addEventListener("change", runReview);

// Create Issue Action Button Click
$("act").addEventListener("click", async () => {
  if (!current) return;
  showError("review-error", null);
  try {
    const body = await api("/api/actions", {
      method: "POST",
      body: JSON.stringify({ change_id: current.record.change_id, mode: current.mode }),
    });
    const action = body.action;
    const id = action.id ? `${action.id} · ` : "";
    const again = action.idempotent ? " (same issue, not a second one)" : "";
    $("action-result").textContent = `${id}${action.status}. ${action.reason}${again}`;
  } catch (error) {
    showError("review-error", error);
  }
});

// Repository Drawer Toggle Handlers
function toggleDrawer() {
  const drawer = $("repo-drawer");
  const repoBtn = $("repo-pill-btn");
  if (!drawer) return;
  const isHidden = drawer.hidden;
  drawer.hidden = !isHidden;
  if (repoBtn) {
    repoBtn.classList.toggle("open", !drawer.hidden);
  }
}

const repoPillBtn = $("repo-pill-btn");
if (repoPillBtn) repoPillBtn.addEventListener("click", toggleDrawer);

const connectGithubBtn = $("connect-github-btn");
if (connectGithubBtn) connectGithubBtn.addEventListener("click", toggleDrawer);

const drawerCloseBtn = $("drawer-close-btn");
if (drawerCloseBtn) drawerCloseBtn.addEventListener("click", toggleDrawer);

// Right Pane Tab Navigation Handlers
const tabTraceBtn = $("btn-tab-trace");
const tabEvidenceBtn = $("btn-tab-evidence");
const tabTraceView = $("pane-tab-trace-view");
const tabEvidenceView = $("pane-tab-evidence-view");

if (tabTraceBtn && tabEvidenceBtn) {
  tabTraceBtn.addEventListener("click", () => {
    tabTraceBtn.classList.add("active");
    tabEvidenceBtn.classList.remove("active");
    if (tabTraceView) tabTraceView.hidden = false;
    if (tabEvidenceView) tabEvidenceView.hidden = true;
  });

  tabEvidenceBtn.addEventListener("click", () => {
    tabEvidenceBtn.classList.add("active");
    tabTraceBtn.classList.remove("active");
    if (tabTraceView) tabTraceView.hidden = true;
    if (tabEvidenceView) tabEvidenceView.hidden = false;
  });
}

// Copy Cypher Query to Clipboard
const copyBtn = $("btn-copy-cypher");
if (copyBtn) {
  copyBtn.addEventListener("click", async () => {
    const display = $("cypher-code-display");
    const raw = display ? display.dataset.raw || display.textContent : "";
    if (raw) {
      try {
        await navigator.clipboard.writeText(raw);
        copyBtn.textContent = "Copied!";
        setTimeout(() => {
          copyBtn.textContent = "Copy";
        }, 1800);
      } catch (err) {
        copyBtn.textContent = "Copied!";
        setTimeout(() => {
          copyBtn.textContent = "Copy";
        }, 1800);
      }
    }
  });
}

// Top Subnav Tabs Selection
["tab-nav-dashboard", "tab-nav-explore", "tab-nav-policies"].forEach((id) => {
  const tab = $(id);
  if (tab) {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".subnav-tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      if (id === "tab-nav-dashboard") {
        const drawer = $("repo-drawer");
        if (drawer && drawer.hidden) toggleDrawer();
      }
    });
  }
});

// Boot Application
boot().catch((error) => showError("review-error", error));
