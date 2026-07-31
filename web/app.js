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

// Everything on screen belongs to the family that was showing, so switching or
// creating one has to clear all of it together. Kept in one place because a
// caller that forgets the positions Map leaves the new family's nodes laid out
// on the old family's coordinates.
function resetWorkspaceView() {
  state.graph = { nodes: [], relationships: [] };
  state.positions.clear();
  state.selectedId = null;
  renderGraphState();
  renderInspector();
  $("#chat").replaceChildren(createChatEmpty());
}

async function selectFamily(id) {
  if (id === state.familyId) return;
  setFamilyId(id);
  resetWorkspaceView();
  await refreshStatus({ reloadGraph: true });
}

// A native prompt() blocks the page, cannot be styled, and gave no hint what a
// family is for. The inline form lives in the panel it acts on, so the name is
// typed next to the list it will join.
function toggleFamilyForm(open) {
  const form = $("#newFamilyForm");
  if (!form) return;
  form.hidden = !open;
  $("#newFamilyButton").hidden = open;
  if (open) {
    $("#newFamilyName").value = "";
    $("#newFamilyName").focus();
  }
}

async function createFamily(name) {
  const trimmed = String(name || "").trim();
  if (!trimmed) {
    setStatus($("#uploadStatus"), "Give the agreement family a name.", true);
    return;
  }
  try {
    const family = await api("/api/families", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: trimmed }),
    });
    toggleFamilyForm(false);
    setFamilyId(family.id);
    resetWorkspaceView();
    await refreshStatus({ reloadGraph: true });
    setStatus($("#uploadStatus"), `${family.name} created. Add its agreements to build the graph.`);
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
// until it has actually been loaded.
// Questions checked against the sample family rather than written from
// intuition: each one was asked, and the answer read, before it was offered.
// They are shown only when that corpus is loaded -- against a visitor's own
// upload they would name products that are not there.
const SAMPLE_QUESTIONS = [
  "If the Support Schedule and the General Terms and Conditions disagree, which one controls?",
  "How many free Joule messages do we get per year?",
  "Can SAP use our data for product development, even though the DPA limits processing to running our service?",
  "Which country's law and courts apply to a dispute about the EU Standard Contractual Clauses?",
  "How much notice must Customer give to stop auto-renewal, and how much must SAP give?",
];

const GENERIC_QUESTIONS = [
  "What licence rights are granted, and under what conditions?",
  "Which document takes precedence if terms conflict?",
  "What security and data-processing duties apply?",
];

// One builder for both lists. The sample questions used to be appended without
// a click handler, so the five questions chosen to show the product off were
// the only ones in the interface that did nothing when clicked.
function suggestionButton(question) {
  const button = element("button", "suggestion", question);
  button.type = "button";
  // One click asks. A screener will not type; the chip is the demo doing
  // itself. The question lands in the composer too, so what was asked stays
  // visible and editable.
  button.addEventListener("click", () => {
    $("#question").value = question;
    ask(question);
  });
  return button;
}

function renderSuggestions() {
  const host = $("#suggestions");
  if (!host) return;
  // Server-reported, not inferred from a filename -- and per sample, not per
  // "a sample". The hardcoded list belonged to the SAP bundle, so with the
  // OpenText sample loaded the chips offered Joule questions against a corpus
  // that has never heard of Joule, and a one-click chip asks immediately now.
  // The catalogue each bundle ships is the source of truth; the hardcoded
  // list survives only as the fallback for old workspaces without one.
  const active = String(state.status.sample_name || "");
  const catalogued = (state.status.samples || []).find(
    (item) => item.name === active
  );
  const mode = state.status.is_sample ? `sample:${active}` : "generic";
  if (host.dataset.mode === mode) return;
  host.dataset.mode = mode;
  const questions = !state.status.is_sample
    ? GENERIC_QUESTIONS
    : (catalogued && catalogued.questions) || SAMPLE_QUESTIONS;
  host.replaceChildren(...questions.map(suggestionButton));
}

function renderSampleOffer() {
  const offer = $("#sampleOffer");
  if (!offer) return;
  const name = state.status.sample_family;
  const available = Boolean(name);
  // The sample now gets its own family rather than filling whichever one is
  // selected, so "is the workspace on screen empty" is the wrong question in
  // local mode: with six families loaded it is never empty and the offer never
  // appeared. What matters is whether the sample has been loaded before.
  const loaded = state.status.persistent
    ? (state.status.families || []).some((item) => item.name === name && item.document_count)
    : Boolean(state.status.documents.length);
  offer.hidden = !(available && !loaded);
  // The in-graph offer sits inside #graphEmpty, which is hidden the moment any
  // graph draws -- so in local mode, where a family is almost always loaded, it
  // could never be reached. The library card carries the same action next to
  // "New family", which is where the two ways into the product belong together.
  const shortcut = $("#loadSampleLibrary");
  if (shortcut) shortcut.hidden = !(available && !loaded && state.status.persistent);
  const note = offer.querySelector(".sample-note");
  if (available && note) {
    note.dataset.family = state.status.sample_family;
  }
}

// The sample family gets its own workspace, always. `/api/demo` replaces the
// legal, sources, input and output directories wholesale, so loading it into
// whichever family happened to be selected would silently destroy that family's
// documents -- and in local mode the request carried no family at all, so
// `require_family` answered 404 and the button simply did not work.
async function loadSampleFamily(button) {
  button.disabled = true;
  const original = button.textContent;
  button.textContent = "Loading sample…";
  try {
    if (state.status.persistent) await selectSampleWorkspace();
    const result = await api(withFamily("/api/demo"), { method: "POST" });
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

// Reuse the sample family if it already exists rather than accumulating a new
// one on every click; `/api/demo` is idempotent, so reloading into the same
// workspace is the intended repeat behaviour.
async function selectSampleWorkspace() {
  const name = state.status.sample_family || "Sample family";
  const existing = (state.status.families || []).find((item) => item.name === name);
  const family = existing || await api("/api/families", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  setFamilyId(family.id);
  resetWorkspaceView();
}

$("#loadSample")?.addEventListener("click", () => loadSampleFamily($("#loadSample")));
$("#loadSampleLibrary")?.addEventListener("click", () =>
  loadSampleFamily($("#loadSampleLibrary"))
);

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
  const host = element("div", "suggestions");
  host.id = "suggestions";
  const sample = state.status && state.status.is_sample;
  host.dataset.mode = sample ? "sample" : "generic";
  host.append(...(sample ? SAMPLE_QUESTIONS : GENERIC_QUESTIONS).map(suggestionButton));
  container.append(host);
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

/* ---------------- the console, transplanted from the public demo ----------
   The workbench used to render answers as one settled bubble: no streaming,
   no visible thinking, evidence as bare buttons. The demo console earned its
   keep with visitors -- exchange blocks, a live token stream, the model's
   working shown in a fold, cited clauses as annotated quotes -- so the
   workbench adopts it wholesale, keeping its own graph integration
   (selectGraphNode / highlightAnswerEvidence) and family-scoped API. */

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

/* Citations arrive alone ("[6]") and grouped ("[10, 11, 12]") -- both forms
   must link, and both count as cited. */
const CITATION_GROUP = /\[(\d{1,2}(?:\s*,\s*\d{1,2})*)\]/g;

function citedIndices(text) {
  const found = new Set();
  for (const hit of String(text).matchAll(CITATION_GROUP)) {
    hit[1].split(",").forEach((part) => found.add(Number(part.trim())));
  }
  return found;
}

function renderAnswerText(text, turnId) {
  let safe = escapeHtml(text);
  safe = safe.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  safe = safe.replace(
    /[“"]([^”"\n]{6,240})[”"]/g,
    (match, quoted) => `<q class="agq">${quoted}</q>`
  );
  safe = safe.replace(CITATION_GROUP, (match, group) =>
    group
      .split(",")
      .map((part) => {
        const index = part.trim();
        return `<a class="cite-ref" href="#${turnId}-ev-${index}">[${index}]</a>`;
      })
      .join(" ")
  );
  return safe;
}

const MODALITY_FORMS = {
  MAY: /\b(may not|may only|may)\b/gi,
  MUST: /\b(must not|must)\b/gi,
  SHALL: /\b(shall not|shall)\b/gi,
  WILL: /\b(will not|will)\b/gi,
  CAN: /\b(cannot|can not|can)\b/gi,
};
const ANY_MODAL = /\b(may not|may only|may|must not|must|shall not|shall|will not|will|cannot|can)\b/gi;
const EFFECT_CLASS = {
  PERMISSION: "permission",
  PROHIBITION: "prohibition",
  OBLIGATION: "obligation",
};

function propositionCount(text) {
  const found = String(text || "").match(ANY_MODAL);
  return found ? found.length : 0;
}

/* A reading is painted only when it reads as a rule: a named actor and a
   short verb phrase. No reading, no paint. */
function presentableRule(rule) {
  if (!rule) return null;
  const actor = String(rule.actor || "").trim();
  const action = String(rule.action || "").trim();
  if (!actor || actor.split(/\s+/).length > 6) return null;
  if (!action || action.split(/\s+/).length > 8) return null;
  return rule;
}

/* Paint a rule's fields inside its own quote -- but only fields whose text
   locates exactly once in the quote. Anything ambiguous stays plain. */
function annotatedQuote(text, rule) {
  const quote = element("blockquote");
  const shown = String(text || "").slice(0, 1200);
  if (!rule) {
    quote.textContent = shown;
    return quote;
  }
  const lower = shown.toLowerCase();
  const spans = [];
  const claim = (needle, cls, maxChars) => {
    const wanted = String(needle || "").trim().toLowerCase();
    if (wanted.length < 3 || wanted.length > (maxChars || 90)) return;
    const first = lower.indexOf(wanted);
    if (first < 0 || lower.indexOf(wanted, first + 1) >= 0) return;
    spans.push({ start: first, end: first + wanted.length, cls });
  };
  claim(rule.actor, "actor", 60);
  claim(rule.action, "action", 60);
  claim(rule.object, "object", 90);
  (rule.conditions || []).forEach((value) => claim(value, "condition", 140));
  (rule.carve_outs || []).forEach((value) => claim(value, "condition", 140));
  const effectClass = EFFECT_CLASS[String(rule.effect || "")];
  const forms = MODALITY_FORMS[String(rule.modality || "").toUpperCase()];
  if (forms && effectClass) {
    const actionSpan = spans.find((span) => span.cls === "action");
    const anchor = actionSpan ? actionSpan.start : 0;
    let chosen = null;
    forms.lastIndex = 0;
    for (let hit = forms.exec(shown); hit; hit = forms.exec(shown)) {
      const distance = Math.abs(hit.index - anchor);
      if (!chosen || distance < chosen.distance) {
        chosen = { start: hit.index, end: hit.index + hit[0].length, distance };
      }
    }
    if (chosen) {
      spans.push({ start: chosen.start, end: chosen.end, cls: `deontic ${effectClass}` });
    }
  }
  spans.sort((a, b) => a.start - b.start);
  let cursor = 0;
  spans.forEach((span) => {
    if (span.start < cursor) return;
    quote.append(document.createTextNode(shown.slice(cursor, span.start)));
    quote.append(
      element("span", `an ${span.cls}`, shown.slice(span.start, span.end))
    );
    cursor = span.end;
  });
  quote.append(document.createTextNode(shown.slice(cursor)));
  return quote;
}

function evidenceReading(item, rule) {
  const host = element("div", "ev-reading");
  if (item.term) {
    host.append(element("span", "chip", "DEFINES"));
    host.append(element("span", "chip", String(item.term).slice(0, 48)));
    return host;
  }
  if (!rule) {
    host.append(element("span", "chip chip-none", "no validated reading"));
    return host;
  }
  const effect = String(rule.effect || "");
  if (effect) {
    const chip = element("span", "chip", effect);
    if (effect === "PERMISSION") chip.classList.add("effect-permission");
    if (effect === "PROHIBITION") chip.classList.add("effect-prohibition");
    host.append(chip);
  }
  ["modality", "actor", "action"].forEach((key) => {
    const value = String(rule[key] || "").trim();
    if (value) host.append(element("span", "chip", value.slice(0, 48)));
  });
  const propositions = propositionCount(item.text);
  if (propositions > 1) {
    host.append(
      element("span", "chip chip-none", `1 of ${propositions} propositions here`)
    );
  }
  return host;
}

function documentTitle(source) {
  const documents = (state.status || {}).documents || [];
  const match = documents.find((doc) => doc.name === source);
  return (match && match.title) || String(source || "document");
}

function evidenceCard(item, index, turnId) {
  const details = element("details", "evidence-item");
  details.id = `${turnId}-ev-${index}`;
  const summary = element("summary");
  summary.append(element("span", "ev-index", `[${index}]`));
  summary.append(element("span", "ev-doc", documentTitle(item.source)));
  summary.append(element("span", "", item.citation || `§${item.section || "—"}`));
  if (item.term) summary.append(element("span", "", `“${item.term}”`));
  const hint = item.term
    ? ""
    : String((presentableRule(item.rule) || {}).action || item.text || "").slice(0, 60);
  if (hint) summary.append(element("span", "ev-hint", hint));
  // Opening the card shows the annotated quote; the click also locates the
  // record on the workbench canvas and inspector -- the integration the demo
  // never had.
  summary.addEventListener("click", () => selectGraphNode(item.id, item));
  details.append(summary);
  const reading = presentableRule(item.rule);
  details.append(annotatedQuote(item.text, reading));
  details.append(evidenceReading(item, reading));
  return details;
}

let exchangeCount = 0;
let turnCounter = 0;

function openExchange(question) {
  const empty = $("#chatEmpty");
  if (empty) empty.remove();
  exchangeCount += 1;
  const block = element("div", "exchange");
  block.append(element("span", "turn-index", `Q${exchangeCount}`));
  block.append(element("div", "turn-q", question));
  $("#chat").append(block);
  return block;
}

function appendTurn(node, block) {
  const chat = $("#chat");
  (block || chat.lastElementChild || chat).append(node);
  chat.scrollTop = chat.scrollHeight;
}

function showAskError(error, block) {
  const note = element("div", "turn-a error", error.message || "Something failed.");
  appendTurn(note, block);
}

function renderResultTurn(result, block) {
  turnCounter += 1;
  const turnId = `turn${turnCounter}`;
  const turn = element("div", "turn-a");
  turn.id = turnId;

  if (result.understood_as) {
    turn.append(
      element("p", "understood", `Understood as: ${result.understood_as}`)
    );
  }
  const body = element("div", "answer-body");
  const answerText = String(result.answer || "");
  body.innerHTML = renderAnswerText(answerText, turnId);
  turn.append(body);

  const evidence = result.evidence || [];
  if (evidence.length) {
    const cited = citedIndices(answerText);
    const list = element("div", "evidence-list");
    const citedCards = [];
    const rest = [];
    evidence.forEach((item, position) => {
      const index = position + 1;
      const card = evidenceCard(item, index, turnId);
      card.dataset.cited = cited.has(index) ? "1" : "0";
      (cited.has(index) ? citedCards : rest).push(card);
    });
    if (citedCards.length) {
      const fold = element("details", "more-evidence cited-evidence");
      fold.append(element("summary", "", `Cited clauses (${citedCards.length})`));
      citedCards.forEach((card) => fold.append(card));
      list.append(fold);
    }
    if (rest.length) {
      const more = element("details", "more-evidence");
      more.append(
        element("summary", "", `Also retrieved but not cited (${rest.length})`)
      );
      rest.forEach((card) => more.append(card));
      list.append(more);
    }
    turn.append(list);
    highlightAnswerEvidence(evidence);
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
    turn.append(trace);
  }

  const offered = result.offered || [];
  if (offered.length > 1) {
    const drill = element("div", "variant-drill");
    drill.append(element("span", "drill-label", "Drill into:"));
    offered.forEach((name) => {
      const chip = element("button", "", String(name));
      chip.type = "button";
      chip.addEventListener("click", () => ask(String(name)));
      drill.append(chip);
    });
    turn.append(drill);
  }
  appendTurn(turn, block);
  // Show the top of the answer, not the bottom of everything after it.
  const chat = $("#chat");
  chat.scrollTop = Math.max(0, turn.offsetTop - chat.offsetTop - 8);
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

/* Streaming ask, from the demo console: SSE frames named thinking / token /
   result / error. The family scoping, model picker and graph highlighting are
   the workbench's own. */
function ask(question) {
  const trimmed = String(question || "").trim();
  if (trimmed.length < 2 || state.asking) return;
  state.asking = true;
  $("#askButton").disabled = true;
  clearAnswerHighlight();

  const block = openExchange(trimmed);
  const live = element("div", "turn-a streaming");
  const body = element("div", "answer-body");
  live.append(body);
  const cursor = element("span", "caret");
  body.append(cursor);
  appendTurn(live, block);

  let text = "";
  let thinkingText = "";
  let thinkingBox = null;
  const thinkingBody = element("pre", "thinking-body");
  const ensureThinking = () => {
    if (thinkingBox) return;
    thinkingBox = element("details", "thinking");
    thinkingBox.open = true;
    thinkingBox.append(element("summary", "", "The model's working"));
    thinkingBox.append(thinkingBody);
    live.before(thinkingBox);
  };
  let finished = false;

  const settle = () => {
    state.asking = false;
    $("#askButton").disabled = false;
    $("#question").value = "";
    renderRuntime();
    $("#question").focus();
  };

  fetch(withFamily("/api/query"), {
    method: "POST",
    headers: {
      "X-AgreementAtlas-Request": "1",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      question: trimmed,
      model: $("#modelSelect").value,
      stream: true,
      reasoning: Boolean($("#thinkToggle")?.checked),
    }),
  })
    .then(async (response) => {
      if (!response.ok || !response.body) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.error || "The request failed.");
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        // SSE frames are separated by a blank line; keep the tail for the
        // next chunk rather than parsing a half-written frame.
        const frames = buffer.split("\n\n");
        buffer = frames.pop() || "";
        for (const frame of frames) {
          const nameLine = frame.match(/^event: (.+)$/m);
          const dataLine = frame.match(/^data: ([\s\S]*)$/m);
          if (!nameLine || !dataLine) continue;
          let payload = {};
          try {
            payload = JSON.parse(dataLine[1]);
          } catch {
            continue;
          }
          const chat = $("#chat");
          if (nameLine[1] === "thinking") {
            thinkingText += payload.text || "";
            ensureThinking();
            const stick =
              thinkingBody.scrollHeight -
                thinkingBody.scrollTop -
                thinkingBody.clientHeight <
              48;
            thinkingBody.textContent = thinkingText;
            if (stick) thinkingBody.scrollTop = thinkingBody.scrollHeight;
            if (chat.scrollHeight - chat.scrollTop - chat.clientHeight < 160) {
              chat.scrollTop = chat.scrollHeight;
            }
          } else if (nameLine[1] === "token") {
            text += payload.text || "";
            body.replaceChildren();
            body.innerHTML = renderAnswerText(text, "live");
            body.append(cursor);
            chat.scrollTop = chat.scrollHeight;
          } else if (nameLine[1] === "result") {
            finished = true;
            live.remove();
            if (thinkingBox) thinkingBox.open = false;
            renderResultTurn(payload, block);
          } else if (nameLine[1] === "error") {
            finished = true;
            live.remove();
            showAskError(new Error(payload.error || "The model call failed."), block);
          }
        }
      }
      if (!finished) {
        live.classList.remove("streaming");
        cursor.remove();
      }
    })
    .catch((error) => {
      live.remove();
      showAskError(error, block);
    })
    .finally(settle);
}

$("#chat").addEventListener("click", (event) => {
  const link = event.target.closest("a.cite-ref");
  if (!link) return;
  event.preventDefault();
  const target = document.getElementById(link.getAttribute("href").slice(1));
  if (!target) return;
  for (let node = target; node; node = node.parentElement) {
    if (node.tagName === "DETAILS") node.open = true;
  }
  target.scrollIntoView({ block: "nearest" });
});

$("#askForm").addEventListener("submit", (event) => {
  event.preventDefault();
  ask($("#question").value);
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
$("#newFamilyButton").addEventListener("click", () => toggleFamilyForm(true));
$("#newFamilyCancel").addEventListener("click", () => toggleFamilyForm(false));
$("#newFamilyForm").addEventListener("submit", (event) => {
  event.preventDefault();
  createFamily($("#newFamilyName").value);
});
$("#newFamilyName").addEventListener("keydown", (event) => {
  if (event.key === "Escape") toggleFamilyForm(false);
});
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


// The clause anatomy panel used to render a sentence diagram here: every
// field of the extracted rule underlined onto the words that produced it,
// captioned "Evidence-backed sentence diagram". It was removed rather than
// repaired. Only 41-62% of rules (varying by family) have all three of
// actor, action and object appearing verbatim in their own evidence, so
// roughly half the time the underlines could not honestly anchor and the
// caption claimed more than the panel delivered. The same six fields are
// still shown above as plain labelled text, where they promise nothing
// about which words produced them, and the quoted source follows unmarked.

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
  const segments = node.evidence_segments || [];
  if (segments.length) {
    const source = element("section", "clause-source");
    const sourceHeading = element("div", "anatomy-heading");
    sourceHeading.append(
      element("span", "eyebrow", "SOURCE TEXT"),
      element("small", "", "Quoted exactly from the agreement")
    );
    source.append(sourceHeading);
    for (const segment of segments) {
      const segmentNode = element("div", `anatomy-evidence ${segment.purpose || "clause"}`);
      segmentNode.append(
        element("b", "", segment.purpose === "chapeau" ? "Governing chapeau" : segment.purpose === "list_item" ? "Operative list item" : "Exact evidence"),
        element("q", "", segment.text)
      );
      source.append(segmentNode);
    }
    inspector.append(source);
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
