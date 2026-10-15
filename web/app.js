const stateLabel = {
  PROCEED: "Proceed",
  PROCEED_WITH_GUARDS: "Proceed with guards",
  NEEDS_REVIEW: "Needs review",
  HOLD: "Hold",
  INSUFFICIENT_EVIDENCE: "Insufficient evidence",
};

const modeLabel = {
  full: "Full graph",
  one_hop: "1-hop only",
  graph_off: "Graph off",
};

let snapshot = null;
let current = null;
let cy = null;

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
  node.hidden = !error;
  node.textContent = error ? error.message || String(error) : "";
}

function renderSnapshot(next) {
  snapshot = next;
  $("backend").textContent = next.backend;
  $("origin-note").textContent = next.note || "";
  const change = $("change");
  const previous = change.value;
  change.innerHTML = "";
  for (const item of next.changes) {
    const option = document.createElement("option");
    option.value = item.id;
    const expected = item.expected ? ` · ${stateLabel[item.expected] || item.expected}` : "";
    const note = item.note ? ` · ${item.note}` : "";
    option.textContent = `${item.id}  ${item.title}${expected}${note}`;
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
  if (next.dataset === "repository") {
    for (const service of next.services) {
      const row = document.createElement("div");
      row.className = "service";
      const copy = document.createElement("div");
      const owner = service.owner || "no CODEOWNERS entry";
      copy.innerHTML = `<strong>${service.name}</strong><small>${owner}</small>`;
      row.append(copy);
      services.appendChild(row);
    }
  }
}

function renderRecord(payload, mode) {
  const record = payload.runs[mode];
  current = { payload, mode, record };
  $("results").hidden = false;
  $("kicker").textContent = `${record.change_id} · ${modeLabel[mode]} · policy ${record.policy_version}`;
  const state = $("state");
  state.textContent = stateLabel[record.state] || record.state;
  state.className = `state-${record.state}`;
  $("rationale").textContent = record.rationale;

  const policies = $("policies");
  policies.innerHTML = "";
  const triggered = record.triggered.length ? record.triggered : ["none"];
  for (const policy of triggered) {
    const item = document.createElement("li");
    item.textContent = policy;
    policies.appendChild(item);
  }

  const compare = $("compare");
  compare.innerHTML = "";
  for (const name of ["full", "one_hop", "graph_off"]) {
    const run = payload.runs[name];
    const button = document.createElement("button");
    button.type = "button";
    if (name === mode) button.classList.add("active");
    button.innerHTML = `<span class="mode-name">${modeLabel[name]}</span><span class="mode-state">${stateLabel[run.state]}</span>`;
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

  const trace = $("trace");
  trace.innerHTML = "";
  for (const call of record.tool_calls) {
    const item = document.createElement("li");
    item.innerHTML = `${call.tool}<span class="summary">${call.summary}</span>`;
    trace.appendChild(item);
  }

  const action = record.action || { status: "ready", reason: "" };
  $("action-reason").textContent = action.reason || "";
  const act = $("act");
  act.disabled = action.status !== "ready";
  $("action-result").textContent = "";
  drawGraph(record.graph);
}

function renderList(id, rows, text, eid) {
  const list = $(id);
  list.innerHTML = "";
  for (const row of rows) {
    const item = document.createElement("li");
    const code = document.createElement("span");
    code.className = "eid";
    code.textContent = eid(row);
    item.append(code, document.createTextNode(text(row)));
    list.appendChild(item);
  }
}

function drawGraph(graph) {
  const container = $("graph");
  if (cy) {
    cy.destroy();
    cy = null;
  }
  if (!window.cytoscape || !graph || !graph.nodes || !graph.nodes.length) {
    container.textContent = "No evidence nodes for this mode.";
    return;
  }
  container.textContent = "";
  const color = {
    changed: "#e24a1b",
    direct: "#e24a1b",
    consumer: "#8eb6ff",
    dependency: "#f0b429",
    incident: "#ff7ad1",
    fix: "#ff7ad1",
    owner: "#d6f25c",
  };
  cy = window.cytoscape({
    container,
    elements: [
      ...graph.nodes.map((node) => ({ data: node })),
      ...graph.edges.map((edge, index) => ({
        data: { id: `e${index}`, source: edge.source, target: edge.target, label: edge.label },
      })),
    ],
    style: [
      {
        selector: "node",
        style: {
          label: "data(label)",
          color: "#f3eee4",
          "background-color": (element) => color[element.data("role")] || "#d9d0c3",
          "font-size": 11,
          "text-wrap": "wrap",
          "text-max-width": 90,
          "text-valign": "bottom",
          "text-margin-y": 6,
          width: 28,
          height: 28,
        },
      },
      {
        selector: "edge",
        style: {
          width: 1.5,
          "line-color": "#8a8175",
          "target-arrow-color": "#8a8175",
          "target-arrow-shape": "triangle",
          "curve-style": "bezier",
          label: "data(label)",
          color: "#b3aa9c",
          "font-size": 9,
          "text-background-color": "#100e0b",
          "text-background-opacity": 0.8,
          "text-background-padding": 2,
        },
      },
    ],
    layout: { name: "breadthfirst", directed: true, padding: 24, spacingFactor: 1.15 },
  });
}

async function runReview() {
  showError("review-error", null);
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
    button.disabled = false;
  }
}

async function boot() {
  snapshot = await api("/api/bootstrap");
  renderSnapshot(snapshot);
}

$("ingest-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  showError("ingest-error", null);
  const target = $("target").value.trim();
  if (!target) {
    showError("ingest-error", new Error("Paste a GitHub URL or a folder path."));
    return;
  }
  try {
    const body = await api("/api/ingest", { method: "POST", body: JSON.stringify({ target }) });
    renderSnapshot(body.snapshot);
    $("results").hidden = true;
  } catch (error) {
    showError("ingest-error", error);
  }
});

$("run").addEventListener("click", runReview);

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

boot().catch((error) => showError("review-error", error));
