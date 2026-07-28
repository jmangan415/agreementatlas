const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const state = {
  status: null,
  graph: { nodes: [], relationships: [] },
  positions: new Map(),
  adjacency: new Map(),
  selectedId: null,
  hoveredId: null,
  view: "overview",
  filters: new Set(),
  transform: { x: 0, y: 0, k: 1 },
  drag: null,
  poll: null,
  answerIds: new Set(),
  familyId: window.localStorage.getItem("agreementatlas.family") || "",
};

const nodeStyle = {
  document: { color: "#18385d", radius: 9, label: "Agreement" },
  agreement_family: { color: "#0d1d33", radius: 11, label: "Agreement family" },
  rule: { color: "#c77f27", radius: 5, label: "Deterministic rule" },
  llm_rule: { color: "#a45c9d", radius: 5, label: "AI-enriched rule" },
  precedence_rule: { color: "#a83d46", radius: 7, label: "Precedence" },
  definition: { color: "#5a6eb4", radius: 6, label: "Defined term" },
  amendment: { color: "#8a5a3b", radius: 7, label: "Amendment" },
  party_or_role: { color: "#788a9d", radius: 6, label: "Party / role" },
  clause: { color: "#94a1ad", radius: 4, label: "Source clause" },
  contract_scope: { color: "#218899", radius: 7, label: "Contract scope" },
  party_or_subject: { color: "#788a9d", radius: 6, label: "Party / subject" },
};

// Every workspace-scoped call names the family it addresses. Local mode has no
// session cookie: the library is a list the user navigates, not a hidden identity.
function withFamily(path) {
  if (!state.familyId) return path;
  return path + (path.includes("?") ? "&" : "?") + "family=" + encodeURIComponent(state.familyId);
}

function setFamilyId(value) {
  state.familyId = value || "";
  if (state.familyId) {
    window.localStorage.setItem("agreementatlas.family", state.familyId);
  } else {
    window.localStorage.removeItem("agreementatlas.family");
  }
}

async function api(path, options = {}) {
  const request = { ...options };
  request.headers = new Headers(options.headers || {});
  if (request.method && request.method !== "GET") {
    request.headers.set("X-AgreementAtlas-Request", "1");
  }
  const response = await fetch(path, request);
  let body;
  try {
    body = await response.json();
  } catch {
    body = { error: "The server returned an unreadable response." };
  }
  if (!response.ok) {
    const error = new Error(body.error || "The request failed.");
    error.code = body.code;
    throw error;
  }
  return body;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function bytes(value) {
  if (value < 1024 * 1024) return `${Math.max(1, Math.ceil(value / 1024))} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function setStatus(target, message, error = false) {
  target.textContent = message;
  target.classList.toggle("error", error);
}

function renderDocuments() {
  const list = $("#documentList");
  list.replaceChildren();
  for (const document of state.status.documents) {
    const row = element("div", "document");
    const icon = element("span", "document-icon", shortType(document.document_type));
    const copy = element("div");
    const title = element("strong", "", document.title || document.name);
    title.title = document.name;
    const metadata = [document.document_type, document.version && `v${document.version}`]
      .filter(Boolean).join(" · ");
    copy.append(title, element("span", "", metadata));
    row.append(icon, copy, element("small", "", bytes(document.size)));
    list.append(row);
  }
  const session = state.status.session;
  $("#fileCount").textContent = `${session.file_count} / ${session.file_limit}`;
  $("#usageText").textContent = `${bytes(session.total_bytes)} / ${bytes(session.byte_limit)}`;
  $("#usageBar").style.width = `${Math.min(100, session.total_bytes / session.byte_limit * 100)}%`;
}

function shortType(value) {
  const labels = {
    DPA: "DPA",
    SLA: "SLA",
    LICENSE_MODEL_ANNEX: "LMA",
    ORDER_SCHEDULE: "ORD",
    ORDER_FORM: "ORD",
    MSA: "MSA",
    GTC: "GTC",
    AMENDMENT: "AMD",
    EULA: "EULA",
    AGREEMENT: "AGR",
  };
  return labels[value] || "DOC";
}

function renderRuntime() {
  const localModel = state.status.lmstudio;
  $("#lmDot").classList.toggle("ok", localModel.available);
  $("#lmSummary").textContent = localModel.available
    ? `${localModel.models.length} local model${localModel.models.length === 1 ? "" : "s"} available`
    : localModel.message;
  $("#modePill").textContent = state.status.privacy.mode === "public-demo"
    ? "Temporary public demo"
    : "Local processing";
  $("#publicConfirm").hidden = state.status.privacy.mode !== "public-demo";

  const select = $("#modelSelect");
  const selected = select.value || localModel.selected_model;
  select.replaceChildren();
  if (!localModel.models.length) {
    select.append(new Option("No local model available", ""));
  } else {
    for (const model of localModel.models) {
      select.append(new Option(model.id, model.id));
    }
    select.value = localModel.models.some((model) => model.id === selected)
      ? selected
      : localModel.selected_model;
  }
  const canAsk = Boolean(
    state.status.documents.length && localModel.available && select.value
  );
  $("#askButton").disabled = !canAsk;
  $("#questionHint").textContent = canAsk
    ? `Using ${select.value}`
    : "Requires uploaded agreements and a loaded LM Studio model";
  renderEnrichment();
}

function renderEnrichment() {
  const job = state.status.enrichment;
  const running = job.state === "running";
  const canEnrich = Boolean(
    state.status.documents.length &&
    state.status.lmstudio.available &&
    $("#modelSelect").value &&
    !running
  );
  $("#enrichButton").disabled = !canEnrich;
  $("#enrichButton").textContent = state.status.enriched
    ? "Re-run AI enrichment"
    : "Enrich legal rules";
  const labels = {
    idle: "Not started",
    running: "Running",
    complete: "Complete",
    error: "Needs attention",
    cancelled: "Cancelled",
  };
  // The job dictionary lives in memory and is empty after a restart, but the
  // enrichment it produced is on disk. Reporting "Not started" for a family that
  // took hours to extract is the worst of the available lies, so durable state
  // wins whenever no job is actually in flight.
  const coverage = state.status.enrichment_coverage || { state: "none" };
  const settled = job.state === "idle" || job.state === "complete";
  let label = labels[job.state] || job.state;
  if (settled && coverage.state === "complete") label = "AI-enriched";
  else if (settled && coverage.state === "partial") label = "Partly enriched";
  const pill = $("#enrichState");
  pill.textContent = label;
  pill.classList.toggle("is-enriched", coverage.state === "complete" && settled);
  pill.classList.toggle("is-partial", coverage.state === "partial" && settled);
  const percent = running && job.total_batches
    ? job.completed_batches / job.total_batches * 100
    : coverage.total_clauses
    ? (coverage.completed_clauses / coverage.total_clauses) * 100
    : 0;
  $("#enrichProgress").style.width = `${percent}%`;
  let message = "Deterministic rules appear immediately; AI enrichment is optional.";
  if (running) {
    message = job.total_batches
      ? `Extracting structured rules: batch ${job.completed_batches} of ${job.total_batches}.`
      : "Preparing material clauses for local extraction…";
  } else if (job.state === "complete") {
    message = `${job.summary.rules} validated AI rules · ${job.summary.fallback_rules || 0} deterministic fallbacks · ${job.summary.clauses_considered} clauses checked.`;
  } else if (coverage.state !== "none") {
    const model = coverage.model ? ` · ${coverage.model}` : "";
    const done = coverage.state === "complete";
    message =
      `${coverage.completed_clauses} of ${coverage.total_clauses} clauses enriched · ` +
      `${coverage.rules} AI rules${model}` +
      (done ? "." : ". Run again to continue where it stopped — completed clauses are not re-extracted.");
  } else if (job.state === "error") {
    message = job.error;
  }
  setStatus($("#enrichMessage"), message, job.state === "error");
}

function stamp(seconds) {
  if (!seconds) return "";
  const value = new Date(seconds * 1000);
  const today = new Date().toDateString() === value.toDateString();
  return today
    ? value.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : value.toLocaleDateString([], { day: "numeric", month: "short" });
}

function renderExpiry() {
  if (state.status.persistent) {
    const family = state.status.family;
    $("#retentionTitle").textContent = "Stored on this machine";
    $("#expiryText").textContent = family ? `Updated ${stamp(family.updated_at)}` : "";
    $("#retentionNote").textContent =
      "Your agreements and AI enrichment are kept until you delete them. Nothing is sent to a cloud model.";
    $("#deleteButton").textContent = "Delete this family";
    return;
  }
  const expires = new Date(state.status.session.expires_at * 1000);
  $("#expiryText").textContent = `Auto-delete ${expires.toLocaleTimeString([], {
    hour: "2-digit", minute: "2-digit"
  })}`;
}

function renderProvenance() {
  // A graph built by an older parser looks identical to a current one. Say when
  // it was built and against which schema, so a stale workspace is visible.
  const build = state.status.build || {};
  const target = $("#buildProvenance");
  if (!build.built_at) {
    target.textContent = "";
    return;
  }
  const mode = build.mode === "deep" ? "AI-enriched" : "deterministic";
  target.textContent = `Graph built ${stamp(build.built_at)} · schema ${build.schema_version} · ${mode}`;
}

function renderLibrary() {
  const card = $("#libraryCard");
  if (!state.status.persistent) {
    card.hidden = true;
    return;
  }
  card.hidden = false;
  const families = state.status.families || [];
  const list = $("#familyList");
  list.replaceChildren();
  if (!families.length) {
    list.appendChild(element("p", "inline-status", "No agreement families yet."));
    return;
  }
  for (const family of families) {
    const row = element("button", "family-row");
    if (family.id === state.familyId) row.classList.add("is-active");
    row.type = "button";
    row.appendChild(element("span", "family-name", family.name));
    const count = `${family.document_count} document${family.document_count === 1 ? "" : "s"}`;
    row.appendChild(element("span", "family-meta", family.enriched ? `${count} · enriched` : count));
    row.addEventListener("click", () => selectFamily(family.id));
    list.appendChild(row);
  }
  const heading = state.status.family ? state.status.family.name : "Source documents";
  $("#familyHeading").textContent = heading;
}

async function selectFamily(id) {
  if (id === state.familyId) return;
  setFamilyId(id);
  state.graph = { nodes: [], relationships: [] };
  state.positions.clear();
  state.selectedId = null;
  renderGraphState();
  renderInspector();
  $("#chat").replaceChildren(createChatEmpty());
  await refreshStatus({ reloadGraph: true });
}

async function createFamily() {
  const name = window.prompt("Name this agreement family", "");
  if (name === null) return;
  try {
    const family = await api("/api/families", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim() }),
    });
    setFamilyId(family.id);
    state.graph = { nodes: [], relationships: [] };
    state.positions.clear();
    state.selectedId = null;
    renderGraphState();
    await refreshStatus({ reloadGraph: true });
    setStatus($("#uploadStatus"), "Family created. Add its agreements to build the graph.");
  } catch (error) {
    setStatus($("#uploadStatus"), error.message, true);
  }
}

async function refreshStatus({ reloadGraph = false } = {}) {
  state.status = await api(withFamily("/api/status"));
  // The stored family may be gone, or this may be a browser that has never
  // chosen one. Fall back to the most recently updated family rather than
  // showing an empty workspace beside a library that clearly has contents.
  if (!state.status.family && (state.status.families || []).length) {
    setFamilyId(state.status.families[0].id);
    state.status = await api(withFamily("/api/status"));
    reloadGraph = true;
  }
  renderLibrary();
  renderDocuments();
  renderRuntime();
  renderExpiry();
  renderProvenance();
  renderSampleOffer();
  renderSuggestions();
  if (reloadGraph || (!state.graph.nodes.length && state.status.graph_ready)) {
    await loadGraph();
  }
  if (state.status.enrichment.state === "running" && !state.poll) {
    state.poll = window.setInterval(pollEnrichment, 2500);
  }
}

// A visitor who has not uploaded anything can see nothing the product does, and
// the terms correctly tell them not to upload a real agreement. The sample
// family is the only path from landing to understanding, so it is offered
// wherever the workspace is empty and a bundle exists to load.
// Questions checked against the sample family rather than written from
// intuition: each one was asked, and the answer read, before it was offered.
// They are shown only when that corpus is loaded -- against a visitor's own
// upload they would name products that are not there.
const SAMPLE_QUESTIONS = [
  "Do I need a licence for someone who is authorised to use the software but has never logged in?",
  "How many days per year can an Occasional Named User access InsightHub?",
  "Can I assign my licence to an affiliate?",
  "Does NDS warrant that usage data produced by the Software will be accurate?",
  "What happens if we are found to be under-licensed in an audit?",
];

function renderSuggestions() {
  const host = $("#suggestions");
  if (!host) return;
  const sampleLoaded = (state.status.documents || []).some((item) =>
    String(item.source || item.name || "").startsWith("01-BASE_EULA_UK-Ireland")
  );
  if (!sampleLoaded || host.dataset.mode === "sample") return;
  host.dataset.mode = "sample";
  host.replaceChildren();
  for (const question of SAMPLE_QUESTIONS) {
    host.append(element("button", "suggestion", question));
  }
}

function renderSampleOffer() {
  const offer = $("#sampleOffer");
  if (!offer) return;
  const available = Boolean(state.status.sample_family);
  const empty = !state.status.documents.length;
  offer.hidden = !(available && empty);
  const note = offer.querySelector(".sample-note");
  if (available && note) {
    note.dataset.family = state.status.sample_family;
  }
}

async function loadSampleFamily() {
  const button = $("#loadSample");
  button.disabled = true;
  const original = button.textContent;
  button.textContent = "Loading sample…";
  try {
    const result = await api("/api/demo", { method: "POST" });
    setStatus(
      $("#uploadStatus"),
      `${result.name} loaded — ${result.clauses} clauses, ${result.definitions} definitions.`
    );
    await refreshStatus({ reloadGraph: true });
  } catch (error) {
    button.textContent = original;
    button.disabled = false;
    setStatus($("#uploadStatus"), error.message, true);
  }
}

$("#loadSample")?.addEventListener("click", loadSampleFamily);

async function pollEnrichment() {
  try {
    const job = await api(withFamily("/api/enrich/status"));
    state.status.enrichment = job;
    renderEnrichment();
    if (job.state !== "running") {
      clearInterval(state.poll);
      state.poll = null;
      await refreshStatus({ reloadGraph: job.state === "complete" });
    }
  } catch {
    clearInterval(state.poll);
    state.poll = null;
  }
}

async function upload(files) {
  const chosen = [...files];
  if (!chosen.length) return;
  if (!$("#publicConfirm").hidden && !$("#uploadConsent").checked) {
    setStatus(
      $("#uploadStatus"),
      "Confirm that you are authorised to use the temporary public demo before uploading.",
      true
    );
    $("#fileInput").value = "";
    return;
  }
  setStatus($("#uploadStatus"), `Reading ${chosen.length} file${chosen.length === 1 ? "" : "s"}…`);
  const form = new FormData();
  for (const file of chosen) form.append("files", file);
  try {
    await api(withFamily("/api/upload"), { method: "POST", body: form });
    setStatus($("#uploadStatus"), "Agreement family updated. Deterministic graph ready.");
    state.selectedId = null;
    await refreshStatus({ reloadGraph: true });
  } catch (error) {
    setStatus($("#uploadStatus"), error.message, true);
  } finally {
    $("#fileInput").value = "";
  }
}

$("#fileInput").addEventListener("change", (event) => upload(event.target.files));
for (const name of ["dragenter", "dragover"]) {
  $("#dropZone").addEventListener(name, (event) => {
    event.preventDefault();
    $("#dropZone").classList.add("drag");
  });
}
for (const name of ["dragleave", "drop"]) {
  $("#dropZone").addEventListener(name, (event) => {
    event.preventDefault();
    $("#dropZone").classList.remove("drag");
  });
}
$("#dropZone").addEventListener("drop", (event) => upload(event.dataTransfer.files));

$("#modelSelect").addEventListener("change", () => {
  renderRuntime();
});

$("#enrichButton").addEventListener("click", async () => {
  $("#enrichButton").disabled = true;
  try {
    await api(withFamily("/api/enrich"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: $("#modelSelect").value }),
    });
    await refreshStatus();
  } catch (error) {
    setStatus($("#enrichMessage"), error.message, true);
    renderEnrichment();
  }
});

$("#deleteButton").addEventListener("click", async () => {
  const persistent = state.status && state.status.persistent;
  const name = persistent && state.status.family ? state.status.family.name : "this session";
  if (!window.confirm(`Permanently delete ${name}, including every uploaded document, its graph and any AI enrichment?`)) {
    return;
  }
  try {
    await api(withFamily(persistent ? "/api/families" : "/api/session"), { method: "DELETE" });
    if (persistent) setFamilyId("");
    state.graph = { nodes: [], relationships: [] };
    state.positions.clear();
    state.selectedId = null;
    renderGraphState();
    renderInspector();
    $("#chat").replaceChildren(createChatEmpty());
    await refreshStatus();
    setStatus($("#uploadStatus"), "Your documents and generated session data were deleted.");
  } catch (error) {
    setStatus($("#uploadStatus"), error.message, true);
  }
});

function createChatEmpty() {
  const container = element("div", "chat-empty");
  container.id = "chatEmpty";
  container.append(element("strong", "", "Try a focused question"));
  for (const question of [
    "What licence rights are granted, and under what conditions?",
    "Which document takes precedence if terms conflict?",
    "What security and data-processing duties apply?",
  ]) {
    const button = element("button", "suggestion", question);
    button.addEventListener("click", () => {
      $("#question").value = question;
      $("#question").focus();
    });
    container.append(button);
  }
  return container;
}

function evidenceHeading(item, index) {
  const graphReason = item.relationship
    ? ` · graph: ${item.relationship.replaceAll("_", " ").toLowerCase()}`
    : "";
  // Prefer the citation the server built. Most passages cannot be cited by
  // number, and a synthetic one a reader cannot find in the PDF is worse than
  // the heading or defined term that actually locates it.
  const where = item.citation || item.section || "Unnumbered";
  return `[${index + 1}] ${item.source} · ${where}${graphReason}`;
}

function addMessage(role, text, evidence = []) {
  const empty = $("#chatEmpty");
  if (empty) empty.remove();
  const message = element("div", `message ${role}`);
  message.append(
    element("div", "role", role === "user" ? "YOU" : "AGREEMENTATLAS"),
  );
  message.append(element("div", "bubble", text));
  if (evidence.length) {
    const list = element("div", "evidence-list");
    evidence.forEach((item, index) => {
      const card = element("button", "evidence-card");
      card.type = "button";
      card.append(
        element("b", "", evidenceHeading(item, index)),
        element("span", "", item.text)
      );
      card.addEventListener("click", () => selectGraphNode(item.id, item));
      list.append(card);
    });
    message.append(list);
  }
  $("#chat").append(message);
  const chat = $("#chat");
  chat.scrollTop = Math.max(0, message.offsetTop - chat.offsetTop - 8);
  return message.querySelector(".bubble");
}

$("#chat").addEventListener("click", (event) => {
  if (event.target.classList.contains("suggestion")) {
    $("#question").value = event.target.textContent;
    $("#question").focus();
  }
});

$("#question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("#askForm").requestSubmit();
  }
});

$("#askForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = $("#question").value.trim();
  if (!question) return;
  addMessage("user", question);
  $("#question").value = "";
  clearAnswerHighlight();
  const pending = addMessage("assistant", "Retrieving exact clauses and asking the local model…");
  $("#askButton").disabled = true;
  try {
    const result = await api(withFamily("/api/query"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, model: $("#modelSelect").value }),
    });
    pending.textContent = `${result.answer}\n\n${result.disclaimer}`;
    const parent = pending.parentElement;
    // A one-word reply is rewritten into the question it answers. Say so, or
    // the reader cannot tell which variant was actually addressed.
    if (result.selected_variant) {
      parent.insertBefore(
        element("div", "understood-as", `Understood as: ${result.understood_as}`),
        pending.nextSibling
      );
    }
    if (result.evidence?.length) {
      // Fourteen expanded cards pushed the answer off screen, and the scroll
      // below then landed past all of them, so the reader had to scroll back up
      // to read what was said. Collapsed by default; the count is the summary.
      const details = element("details", "evidence-details");
      const summary = element("summary", "", `${result.evidence.length} source${result.evidence.length === 1 ? "" : "s"} · click any to locate it in the graph`);
      details.append(summary);
      const list = element("div", "evidence-list");
      result.evidence.forEach((item, index) => {
        const card = element("button", "evidence-card");
        card.type = "button";
        card.append(
          element("b", "", evidenceHeading(item, index)),
          element("span", "", item.text)
        );
        card.addEventListener("click", () => selectGraphNode(item.id, item));
        list.append(card);
      });
      details.append(list);
      parent.append(details);
      highlightAnswerEvidence(result.evidence);
    }
    if (result.resolution_trace) {
      const trace = element("div", `resolution-trace ${result.resolution_trace.status.toLowerCase()}`);
      trace.append(
        element("b", "", `Legal resolution: ${result.resolution_trace.status}`),
        element("span", "", `${result.resolution_trace.steps.length} candidate rule${result.resolution_trace.steps.length === 1 ? "" : "s"} · ${result.graph_build_mode} graph`)
      );
      if (result.resolution_trace.unresolved_warnings?.length) {
        trace.append(element("small", "", result.resolution_trace.unresolved_warnings.join(" · ")));
      }
      parent.append(trace);
    }
  } catch (error) {
    pending.textContent = `AgreementAtlas could not answer yet: ${error.message}`;
  } finally {
    renderRuntime();
    // Show the top of the answer, not the bottom of everything after it.
    const bubble = pending.parentElement;
    const chat = $("#chat");
    chat.scrollTop = Math.max(0, bubble.offsetTop - chat.offsetTop - 8);
    $("#question").focus();
  }
});

async function loadGraph() {
  if (!state.status?.graph_ready) {
    state.graph = { nodes: [], relationships: [] };
    renderGraphState();
    return;
  }
  state.graph = await api(withFamily(`/api/graph?view=${state.view}`));
  buildAdjacency();
  initialisePositions();
  createFilters();
  fitGraph();
  renderGraphState();
  renderInspector();
}

function buildAdjacency() {
  state.adjacency.clear();
  for (const node of state.graph.nodes) state.adjacency.set(node.id, new Set());
  for (const edge of state.graph.relationships) {
    state.adjacency.get(edge.source)?.add(edge.target);
    state.adjacency.get(edge.target)?.add(edge.source);
  }
}

function hash(value) {
  let result = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    result ^= value.charCodeAt(index);
    result = Math.imul(result, 16777619);
  }
  return result >>> 0;
}

function initialisePositions() {
  const fresh = [];
  const rings = {
    agreement_family: 20,
    document: 65,
    precedence_rule: 150,
    amendment: 185,
    definition: 240,
    party_or_role: 315,
    contract_scope: 210,
    party_or_subject: 330,
    rule: 430,
    llm_rule: 470,
  };
  const counts = {};
  const indexes = {};
  for (const node of state.graph.nodes) counts[node.type] = (counts[node.type] || 0) + 1;
  for (const node of state.graph.nodes) {
    if (state.positions.has(node.id)) continue;
    const style = nodeStyle[node.type] || nodeStyle.rule;
    const index = indexes[node.type] || 0;
    indexes[node.type] = index + 1;
    const total = counts[node.type];
    const jitter = (hash(node.id) % 1000) / 1000;
    const angle = ((index + jitter) / Math.max(1, total)) * Math.PI * 2;
    const radius = rings[node.type] || 380;
    state.positions.set(node.id, {
      x: Math.cos(angle) * radius,
      y: Math.sin(angle) * radius * .68,
      vx: 0,
      vy: 0,
      radius: style.radius,
    });
    fresh.push(node.id);
  }
  for (let iteration = 0; iteration < 75; iteration += 1) {
    layoutStep();
  }
}

// Beyond ~550 units apart, two nodes are on opposite sides of the view and
// pushing them further apart only inflates the whole layout.
const REPULSION_RANGE2 = 550 * 550;

function layoutStep() {
  const nodes = state.graph.nodes;
  for (let i = 0; i < nodes.length; i += 1) {
    const a = state.positions.get(nodes[i].id);
    for (let j = i + 1; j < nodes.length; j += 1) {
      const b = state.positions.get(nodes[j].id);
      let dx = b.x - a.x;
      let dy = b.y - a.y;
      const distance2 = Math.max(100, dx * dx + dy * dy);
      // Repulsion with no range limit means every node pushes every other one
      // however far apart they are, so the cloud has to grow until the inverse
      // square drops far enough -- and it grows with the node count. Enrichment
      // took this graph from about sixty nodes to a hundred and fifty and the
      // layout inflated to ten thousand units across an eight hundred pixel
      // canvas, which is why it "looked great until it was enriched". Past this
      // range two nodes are already unrelated on screen.
      if (distance2 > REPULSION_RANGE2) continue;
      const force = Math.min(.55, 680 / distance2);
      dx *= force;
      dy *= force;
      a.vx -= dx; a.vy -= dy;
      b.vx += dx; b.vy += dy;
    }
  }
  for (const edge of state.graph.relationships) {
    const a = state.positions.get(edge.source);
    const b = state.positions.get(edge.target);
    if (!a || !b) continue;
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const distance = Math.max(1, Math.hypot(dx, dy));
    const target = edge.type === "GOVERNS" ? 150 : 95;
    const force = (distance - target) * .0025;
    a.vx += dx / distance * force; a.vy += dy / distance * force;
    b.vx -= dx / distance * force; b.vy -= dy / distance * force;
  }
  for (const node of nodes) {
    const point = state.positions.get(node.id);
    point.vx += -point.x * .0007;
    point.vy += -point.y * .0007;
    point.vx *= .76;
    point.vy *= .76;
    point.x += point.vx;
    point.y += point.vy;
  }
}

function createFilters() {
  const container = $("#typeFilters");
  const present = [...new Set(state.graph.nodes.map((node) => node.type))];
  if (!state.filters.size) present.forEach((type) => state.filters.add(type));
  container.replaceChildren();
  for (const type of present) {
    const style = nodeStyle[type] || nodeStyle.rule;
    const label = element("label", "filter-chip");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = state.filters.has(type);
    input.addEventListener("change", () => {
      if (input.checked) state.filters.add(type);
      else state.filters.delete(type);
      drawGraph();
    });
    const dot = document.createElement("i");
    dot.style.background = style.color;
    label.append(input, dot, document.createTextNode(style.label));
    container.append(label);
  }
}

function renderGraphState() {
  $("#graphEmpty").classList.toggle("hidden", Boolean(state.graph.nodes.length));
  drawGraph();
}

function visibleNodes() {
  return state.graph.nodes.filter((node) => state.filters.has(node.type));
}

function canvasMetrics() {
  const canvas = $("#graphCanvas");
  const rect = canvas.getBoundingClientRect();
  const ratio = Math.min(2, window.devicePixelRatio || 1);
  if (canvas.width !== Math.round(rect.width * ratio) || canvas.height !== Math.round(rect.height * ratio)) {
    canvas.width = Math.round(rect.width * ratio);
    canvas.height = Math.round(rect.height * ratio);
  }
  return { canvas, rect, ratio, context: canvas.getContext("2d") };
}

function screenPoint(point, rect) {
  return {
    x: point.x * state.transform.k + state.transform.x + rect.width / 2,
    y: point.y * state.transform.k + state.transform.y + rect.height / 2,
  };
}

function drawGraph() {
  const { canvas, rect, ratio, context } = canvasMetrics();
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, rect.width, rect.height);
  if (!state.graph.nodes.length) return;

  const visible = new Set(visibleNodes().map((node) => node.id));
  const connected = state.selectedId ? state.adjacency.get(state.selectedId) || new Set() : new Set();
  context.lineWidth = 1;
  for (const edge of state.graph.relationships) {
    if (!visible.has(edge.source) || !visible.has(edge.target)) continue;
    const source = screenPoint(state.positions.get(edge.source), rect);
    const target = screenPoint(state.positions.get(edge.target), rect);
    const inAnswer = state.answerIds.size
      && state.answerIds.has(edge.source) && state.answerIds.has(edge.target);
    const highlighted = state.selectedId && (
      edge.source === state.selectedId || edge.target === state.selectedId
    );
    context.strokeStyle = inAnswer
      ? "rgba(47,125,110,.75)"
      : highlighted ? "rgba(27,130,149,.68)" : "rgba(92,112,130,.16)";
    context.lineWidth = inAnswer ? 2 : highlighted ? 1.8 : .75;
    context.beginPath();
    context.moveTo(source.x, source.y);
    context.lineTo(target.x, target.y);
    context.stroke();
  }

  const query = $("#graphSearch").value.trim().toLowerCase();
  for (const node of visibleNodes()) {
    const point = screenPoint(state.positions.get(node.id), rect);
    const style = nodeStyle[node.type] || nodeStyle.rule;
    const matches = !query || [
      node.label, node.description, node.source, node.scope, node.section
    ].some((value) => String(value || "").toLowerCase().includes(query));
    const related = !state.selectedId || node.id === state.selectedId || connected.has(node.id);
    const cited = state.answerIds.has(node.id);
    const inAnswerView = !state.answerIds.size || cited;
    context.globalAlpha = matches && related && inAnswerView ? 1 : cited ? 1 : .10;
    context.fillStyle = style.color;
    context.beginPath();
    context.arc(point.x, point.y, style.radius * Math.sqrt(state.transform.k), 0, Math.PI * 2);
    context.fill();
    if (cited && node.id !== state.selectedId) {
      context.strokeStyle = "#2F7D6E";
      context.lineWidth = 2.4;
      context.stroke();
    }
    if (node.id === state.selectedId || node.id === state.hoveredId) {
      context.strokeStyle = node.id === state.selectedId ? "#0d1d33" : "#218899";
      context.lineWidth = 2;
      context.stroke();
    }
    const showLabel = cited || node.type === "document" || node.id === state.selectedId ||
      node.id === state.hoveredId || (node.type === "contract_scope" && state.transform.k > .72);
    if (showLabel) {
      context.globalAlpha = matches ? 1 : .2;
      context.fillStyle = "#203047";
      context.font = `${node.type === "document" ? "600 " : ""}10px Inter, sans-serif`;
      context.textAlign = "center";
      context.fillText(truncate(node.label, 32), point.x, point.y + style.radius + 13);
    }
  }
  context.globalAlpha = 1;
  canvas.classList.toggle("dragging", Boolean(state.drag));
}

function truncate(value, maximum) {
  const text = String(value || "");
  return text.length > maximum ? `${text.slice(0, maximum - 1)}…` : text;
}

function fitGraph() {
  const nodes = visibleNodes();
  const rect = $("#graphCanvas").getBoundingClientRect();
  if (!nodes.length || !rect.width || !rect.height) return;
  const xs = nodes.map((node) => state.positions.get(node.id).x);
  const ys = nodes.map((node) => state.positions.get(node.id).y);
  const width = Math.max(180, Math.max(...xs) - Math.min(...xs) + 100);
  const height = Math.max(180, Math.max(...ys) - Math.min(...ys) + 100);
  state.transform.k = Math.min(1.25, Math.max(.03, Math.min(rect.width / width, rect.height / height)));
  state.transform.x = -(Math.max(...xs) + Math.min(...xs)) / 2 * state.transform.k;
  state.transform.y = -(Math.max(...ys) + Math.min(...ys)) / 2 * state.transform.k;
  drawGraph();
}

function worldFromEvent(event) {
  const rect = $("#graphCanvas").getBoundingClientRect();
  return {
    screenX: event.clientX - rect.left,
    screenY: event.clientY - rect.top,
    x: (event.clientX - rect.left - rect.width / 2 - state.transform.x) / state.transform.k,
    y: (event.clientY - rect.top - rect.height / 2 - state.transform.y) / state.transform.k,
  };
}

function nodeAt(event) {
  const point = worldFromEvent(event);
  let best = null;
  let distance = Infinity;
  for (const node of visibleNodes()) {
    const position = state.positions.get(node.id);
    const current = Math.hypot(position.x - point.x, position.y - point.y);
    const radius = (nodeStyle[node.type] || nodeStyle.rule).radius / Math.sqrt(state.transform.k) + 5;
    if (current < radius && current < distance) {
      best = node;
      distance = current;
    }
  }
  return best;
}

$("#graphCanvas").addEventListener("pointerdown", (event) => {
  const node = nodeAt(event);
  const point = worldFromEvent(event);
  state.drag = {
    nodeId: node?.id || null,
    startX: event.clientX,
    startY: event.clientY,
    screenX: point.screenX,
    screenY: point.screenY,
    originX: state.transform.x,
    originY: state.transform.y,
    moved: false,
  };
  $("#graphCanvas").setPointerCapture(event.pointerId);
  drawGraph();
});

$("#graphCanvas").addEventListener("pointermove", (event) => {
  if (!state.drag) {
    const hovered = nodeAt(event)?.id || null;
    if (hovered !== state.hoveredId) {
      state.hoveredId = hovered;
      drawGraph();
    }
    return;
  }
  const dx = event.clientX - state.drag.startX;
  const dy = event.clientY - state.drag.startY;
  if (Math.hypot(dx, dy) > 3) state.drag.moved = true;
  if (state.drag.nodeId) {
    const world = worldFromEvent(event);
    const position = state.positions.get(state.drag.nodeId);
    position.x = world.x;
    position.y = world.y;
    position.vx = 0; position.vy = 0;
  } else {
    state.transform.x = state.drag.originX + dx;
    state.transform.y = state.drag.originY + dy;
  }
  drawGraph();
});

function endPointer(event) {
  if (!state.drag) return;
  const selected = state.drag.nodeId;
  const moved = state.drag.moved;
  state.drag = null;
  if (selected && !moved) selectGraphNode(selected);
  drawGraph();
  try { $("#graphCanvas").releasePointerCapture(event.pointerId); } catch {}
}
$("#graphCanvas").addEventListener("pointerup", endPointer);
$("#graphCanvas").addEventListener("pointercancel", endPointer);

$("#graphCanvas").addEventListener("wheel", (event) => {
  event.preventDefault();
  const rect = $("#graphCanvas").getBoundingClientRect();
  const mouseX = event.clientX - rect.left - rect.width / 2;
  const mouseY = event.clientY - rect.top - rect.height / 2;
  const old = state.transform.k;
  const next = Math.min(3.5, Math.max(.18, old * Math.exp(-event.deltaY * .0012)));
  state.transform.x = mouseX - (mouseX - state.transform.x) * next / old;
  state.transform.y = mouseY - (mouseY - state.transform.y) * next / old;
  state.transform.k = next;
  drawGraph();
}, { passive: false });

function zoom(factor) {
  state.transform.k = Math.min(3.5, Math.max(.18, state.transform.k * factor));
  drawGraph();
}
$("#zoomIn").addEventListener("click", () => zoom(1.25));
$("#zoomOut").addEventListener("click", () => zoom(.8));
$("#resetView").addEventListener("click", fitGraph);
$("#newFamilyButton").addEventListener("click", createFamily);
$("#graphSearch").addEventListener("input", drawGraph);
new ResizeObserver(drawGraph).observe($("#graphStage"));

for (const button of $$(".view-switch button")) {
  button.addEventListener("click", async () => {
    state.view = button.dataset.view;
    $$(".view-switch button").forEach((item) => item.classList.toggle("active", item === button));
    await loadGraph();
  });
}

// The canvas is the largest thing on screen and, until an evidence card was
// clicked, it showed the same picture whatever was asked. An answer already
// names the records it used, so light exactly those and dim the rest: the graph
// becomes the reason for the answer rather than decoration beside it.
function highlightAnswerEvidence(evidence) {
  const wanted = new Set();
  for (const item of evidence || []) {
    const direct = state.graph.nodes.find((node) => node.id === item.id);
    const viaClause = direct || state.graph.nodes.find((node) => node.clause_id === item.id);
    if (viaClause) wanted.add(viaClause.id);
  }
  state.answerIds = wanted;
  if (wanted.size) fitToNodes(wanted);
  drawGraph();
}

function clearAnswerHighlight() {
  if (!state.answerIds.size) return;
  state.answerIds = new Set();
  drawGraph();
}

// Frame the cited records rather than the whole family, so a two-clause answer
// is not shown as two dots in an otherwise empty field. Positions are in the
// same pixel space `fitGraph` works in -- treating them as normalised put the
// origin hundreds of pixels off-canvas and drew an empty graph.
function fitToNodes(ids) {
  const points = [...ids].map((id) => state.positions.get(id)).filter(Boolean);
  const rect = $("#graphCanvas").getBoundingClientRect();
  if (points.length < 2 || !rect.width || !rect.height) return;
  const xs = points.map((point) => point.x);
  const ys = points.map((point) => point.y);
  const width = Math.max(180, Math.max(...xs) - Math.min(...xs) + 160);
  const height = Math.max(180, Math.max(...ys) - Math.min(...ys) + 160);
  state.transform.k = Math.min(1.6, Math.max(.03, Math.min(rect.width / width, rect.height / height)));
  state.transform.x = -(Math.max(...xs) + Math.min(...xs)) / 2 * state.transform.k;
  state.transform.y = -(Math.max(...ys) + Math.min(...ys)) / 2 * state.transform.k;
}

// An evidence record and a graph node are not the same population. The graph
// caps how many rules it draws, offerings are not drawn at all, and a clause
// only appears through the rule that cites it. Clicking a card that fell into
// any of those gaps did nothing whatsoever -- no selection, no message -- so
// the panel looked broken at random.
function resolveGraphNode(id) {
  const nodes = state.graph.nodes;
  const direct = nodes.find((node) => node.id === id);
  if (direct) return direct;
  const viaClause = nodes.find((node) => node.clause_id === id);
  if (viaClause) return viaClause;
  // Records derived from the same clause share a hash and differ only by
  // prefix: offering:abc and definition:abc are the same provision, and only
  // one of them is ever drawn.
  const hash = String(id).split(":")[1];
  if (hash) {
    const twin = nodes.find((node) => String(node.id).split(":")[1] === hash);
    if (twin) return twin;
  }
  return null;
}

function selectGraphNode(id, item) {
  const node = resolveGraphNode(id);
  if (node) {
    state.selectedId = node.id;
    renderInspector();
    drawGraph();
    return;
  }
  // Nothing to select, so show the evidence itself rather than ignoring the
  // click. Saying why is the point: the record is real, it is simply not drawn
  // in this view.
  state.selectedId = null;
  showEvidenceOnlyInspector(item);
  drawGraph();
}

function showEvidenceOnlyInspector(item) {
  const inspector = $("#nodeInspector");
  inspector.replaceChildren();
  const wrap = element("div", "inspector-placeholder");
  wrap.append(element("span", "eyebrow", "EVIDENCE INSPECTOR"));
  if (item) {
    wrap.append(
      element("h3", "", item.citation || item.section || "Cited provision"),
      element("p", "", item.source || ""),
      element("blockquote", "evidence-quote", item.text || "")
    );
  }
  wrap.append(element("p", "",
    `This provision is cited by the answer but is not drawn in the ${state.view === "overview" ? "overview" : "rule detail"} view.`));
  inspector.append(wrap);
}


// --- Sentence diagramming -----------------------------------------------------
// Show which words in the clause produced which part of the extracted rule. Only
// text that is literally present is highlighted: the action and object are drawn
// from closed vocabularies and often do not appear verbatim, and inventing a
// highlight for them would misrepresent where the reading came from.

const MODAL_PATTERN = /\b(shall not|must not|may not|will not|cannot|can not|shall|must|may|will|can)\b/gi;
const LIMIT_PATTERN = /\bonly\b/gi;

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function collectMatches(text, pattern, role) {
  const found = [];
  pattern.lastIndex = 0;
  let match;
  while ((match = pattern.exec(text)) !== null) {
    if (!match[0]) break;
    found.push({ start: match.index, end: match.index + match[0].length, role });
  }
  return found;
}

function literalMatches(text, value, role) {
  if (!value || String(value).length < 3) return [];
  // Whole words only. Matching "allocate" inside "allocated" highlighted the
  // stem and left the suffix bare -- "allocate|d" -- which reads as a parser
  // error rather than a mapping. \b alone is not enough: it stops at the start
  // of "allocated" but not at the end, so require a non-letter after too.
  const pattern = new RegExp(`\\b${escapeRegExp(String(value))}(?![A-Za-z])`, "gi");
  return collectMatches(text, pattern, role);
}

function diagramSentence(text, node, usedRoles) {
  const fragment = document.createDocumentFragment();
  if (!text) return fragment;

  // Two bands, and the distinction is the whole diagram. Actor, modality,
  // action, object and limit are constituents: a few words each, naming a part
  // of the sentence. A condition or carve-out is a clause, and is regularly the
  // entire sentence -- "Unless prohibited under the applicable License Document,
  // the Licensee may allocate Software Licenses to its Affiliates, provided".
  //
  // Resolving purely by length let that clause win and swallow every
  // constituent inside it, so the sentence rendered as one flat carve-out band
  // with none of the parts marked. Constituents are placed first and the coarse
  // spans fill in around them.
  const constituents = [
    ...collectMatches(text, MODAL_PATTERN, "modality"),
    ...collectMatches(text, LIMIT_PATTERN, "limit"),
    ...literalMatches(text, node.actor, "actor"),
    ...literalMatches(text, node.object, "object"),
    ...literalMatches(text, node.action, "action"),
  ];
  const clauses = [];
  for (const value of node.conditions || []) clauses.push(...literalMatches(text, value, "condition"));
  for (const value of node.carve_outs || []) clauses.push(...literalMatches(text, value, "carveout"));

  // Within a band, longest first, then drop overlaps so no word has two roles.
  const place = (candidates, taken) => {
    candidates.sort((a, b) => b.end - b.start - (a.end - a.start) || a.start - b.start);
    for (const mark of candidates) {
      if (taken.some((other) => mark.start < other.end && other.start < mark.end)) continue;
      taken.push(mark);
    }
    return taken;
  };
  const taken = place(constituents, []);

  // A clause keeps the words the constituents did not claim, as one or more
  // fragments, so "Unless prohibited ..." still reads as the condition while
  // "the Licensee" inside it still reads as the actor.
  const fragments = [];
  for (const mark of clauses) {
    let cursor = mark.start;
    const inside = taken
      .filter((other) => other.start < mark.end && mark.start < other.end)
      .sort((a, b) => a.start - b.start);
    for (const other of inside) {
      if (other.start > cursor) fragments.push({ start: cursor, end: other.start, role: mark.role });
      cursor = Math.max(cursor, other.end);
    }
    if (cursor < mark.end) fragments.push({ start: cursor, end: mark.end, role: mark.role });
  }
  // Ignore slivers left between adjacent constituents; a two-character
  // highlight reads as a rendering fault rather than a role.
  place(fragments.filter((item) => item.end - item.start >= 3), taken);
  taken.sort((a, b) => a.start - b.start);

  let cursor = 0;
  for (const mark of taken) {
    if (usedRoles) usedRoles.add(mark.role);
    if (mark.start > cursor) fragment.append(document.createTextNode(text.slice(cursor, mark.start)));
    const piece = element("mark", `hl hl-${mark.role}`, text.slice(mark.start, mark.end));
    piece.title = mark.role;
    fragment.append(piece);
    cursor = mark.end;
  }
  if (cursor < text.length) fragment.append(document.createTextNode(text.slice(cursor)));
  return fragment;
}

const ROLE_LABELS = {
  actor: "Actor",
  modality: "Modality",
  object: "Object",
  action: "Action",
  limit: "Limit",
  condition: "Condition",
  carveout: "Carve-out",
};

function diagramLegend(usedRoles) {
  // Only name the colours actually on screen. A legend entry with no matching
  // highlight reads as a highlight the reader has failed to spot.
  const roles = Object.entries(ROLE_LABELS).filter(([role]) => usedRoles.has(role));
  if (!roles.length) return document.createDocumentFragment();
  const legend = element("div", "diagram-legend");
  for (const [role, label] of roles) {
    const item = element("span", "");
    item.append(element("i", `hl-${role}`), document.createTextNode(label));
    legend.append(item);
  }
  return legend;
}

function renderInspector() {
  const inspector = $("#nodeInspector");
  inspector.replaceChildren();
  const node = state.graph.nodes.find((item) => item.id === state.selectedId);
  if (!node) {
    const placeholder = element("div", "inspector-placeholder");
    placeholder.append(
      element("span", "eyebrow", "EVIDENCE INSPECTOR"),
      element("p", "", "Select a node to inspect its scope, conditions and source clause.")
    );
    inspector.append(placeholder);
    return;
  }
  const heading = element("div", "node-title");
  const title = element("div");
  title.append(
    element("span", "eyebrow", node.source || "GRAPH ENTITY"),
    element("h3", "", node.label)
  );
  heading.append(title, element("span", "node-type", (nodeStyle[node.type] || nodeStyle.rule).label));
  inspector.append(heading);
  const grid = element("div", "detail-grid");
  const details = [
    ["Section", node.section],
    ["Scope", node.scope],
    ["Effect", node.effect],
    ["Modality", node.modality],
    ["Polarity", node.polarity],
    ["Actor", node.actor],
    ["Action", node.action],
    ["Object", node.object],
    ["Conditions", Array.isArray(node.conditions) ? node.conditions.join("; ") : node.conditions],
    ["Carve-outs", Array.isArray(node.carve_outs) ? node.carve_outs.join("; ") : node.carve_outs],
    ["Status", node.status],
    ["Source", node.source],
    ["Model", node.model],
  ].filter(([, value]) => value);
  for (const [label, value] of details) {
    const item = element("div", "detail-item");
    item.append(element("b", "", label), element("span", "", String(value)));
    item.querySelector("span").title = String(value);
    grid.append(item);
  }
  inspector.append(grid);
  const anatomyValues = [
    ["effect", node.effect],
    ["modality", node.modality],
    ["polarity", node.polarity],
    ["actor", node.actor],
    ["action", node.action],
    ["object", node.object],
  ].filter(([, value]) => value);
  if (anatomyValues.length || node.evidence_segments?.length) {
    const anatomy = element("section", "clause-anatomy");
    const anatomyHeading = element("div", "anatomy-heading");
    anatomyHeading.append(
      element("span", "eyebrow", "CLAUSE ANATOMY"),
      element("small", "", "Evidence-backed sentence diagram")
    );
    anatomy.append(anatomyHeading);
    if (anatomyValues.length) {
      const chips = element("div", "anatomy-chips");
      for (const [label, value] of anatomyValues) {
        // Effect and polarity are keyed to their value: a lawyer scans for
        // "is this an obligation or a prohibition", so that distinction has to
        // survive being glanced at, not read.
        const keyed = label === "effect" || label === "polarity"
          ? `v-${String(value).toLowerCase().replace(/[^a-z]/g, "")}`
          : "";
        const chip = element("span", `anatomy-chip ${label} ${keyed}`.trim());
        chip.append(element("b", "", label), document.createTextNode(String(value)));
        chips.append(chip);
      }
      anatomy.append(chips);
    }
    const usedRoles = new Set();
    for (const segment of node.evidence_segments || []) {
      const segmentNode = element("div", `anatomy-evidence ${segment.purpose || "clause"}`);
      const quote = element("q", "diagrammed");
      quote.append(diagramSentence(segment.text, node, usedRoles));
      segmentNode.append(
        element("b", "", segment.purpose === "chapeau" ? "Governing chapeau" : segment.purpose === "list_item" ? "Operative list item" : "Exact evidence"),
        quote
      );
      anatomy.append(segmentNode);
    }
    anatomy.append(diagramLegend(usedRoles));
    inspector.append(anatomy);
  } else {
    const evidence = node.evidence || node.description;
    if (evidence) inspector.append(element("blockquote", "evidence-quote", evidence));
  }
}

refreshStatus({ reloadGraph: true }).catch((error) => {
  setStatus(
    $("#uploadStatus"),
    `AgreementAtlas could not start: ${error.message}`,
    true,
  );
});
// Status tells us whether the models are loaded, which cannot change while
// nobody is looking at the page. Polling a hidden tab every fifteen seconds
// kept the inference server busy answering questions no one would read.
window.setInterval(() => {
  if (document.hidden) return;
  refreshStatus().catch(() => {});
}, 15000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshStatus().catch(() => {});
});
