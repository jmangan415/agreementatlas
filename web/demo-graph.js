/* The workbench's knowledge-graph canvas and evidence inspector, carried onto
   the demo page. Ported from app.js rather than shared with it: the workbench
   binds thirty other elements this page does not have, and a refactor of the
   tool the operator uses daily is not worth saving four hundred duplicated
   lines on a demo. Loads after demo.js and shares its `$`, `api`, `element`
   and `state` globals; everything graph-owned lives on `gstate`. */

"use strict";

const gstate = {
  graph: { nodes: [], relationships: [] },
  positions: new Map(),
  adjacency: new Map(),
  selectedId: null,
  hoveredId: null,
  view: "overview",
  filters: new Set(),
  transform: { x: 0, y: 0, k: 1 },
  drag: null,
  answerIds: new Set(),
  diagramOnly: false,
  paintable: new Set(),
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

async function loadGraph() {
  if (!state.status?.graph_ready) {
    gstate.graph = { nodes: [], relationships: [] };
    gstate.positions.clear();
    renderGraphState();
    renderInspector();
    return;
  }
  gstate.graph = await api(`/api/graph?view=${gstate.view}`);
  gstate.positions.clear();
  gstate.selectedId = null;
  gstate.answerIds = new Set();
  gstate.filters.clear();
  buildAdjacency();
  initialisePositions();
  computePaintable();
  createFilters();
  fitGraph();
  renderGraphState();
  renderInspector();
}

function buildAdjacency() {
  gstate.adjacency.clear();
  for (const node of gstate.graph.nodes) gstate.adjacency.set(node.id, new Set());
  for (const edge of gstate.graph.relationships) {
    gstate.adjacency.get(edge.source)?.add(edge.target);
    gstate.adjacency.get(edge.target)?.add(edge.source);
  }
}

function hashId(value) {
  let result = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    result ^= value.charCodeAt(index);
    result = Math.imul(result, 16777619);
  }
  return result >>> 0;
}

function initialisePositions() {
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
  for (const node of gstate.graph.nodes) counts[node.type] = (counts[node.type] || 0) + 1;
  for (const node of gstate.graph.nodes) {
    if (gstate.positions.has(node.id)) continue;
    const style = nodeStyle[node.type] || nodeStyle.rule;
    const index = indexes[node.type] || 0;
    indexes[node.type] = index + 1;
    const total = counts[node.type];
    const jitter = (hashId(node.id) % 1000) / 1000;
    const angle = ((index + jitter) / Math.max(1, total)) * Math.PI * 2;
    const radius = rings[node.type] || 380;
    gstate.positions.set(node.id, {
      x: Math.cos(angle) * radius,
      y: Math.sin(angle) * radius * 0.68,
      vx: 0,
      vy: 0,
      radius: style.radius,
    });
  }
  for (let iteration = 0; iteration < 75; iteration += 1) layoutStep();
}

// Beyond ~550 units apart, two nodes are on opposite sides of the view and
// pushing them further apart only inflates the whole layout.
const REPULSION_RANGE2 = 550 * 550;

function layoutStep() {
  const nodes = gstate.graph.nodes;
  for (let i = 0; i < nodes.length; i += 1) {
    const a = gstate.positions.get(nodes[i].id);
    for (let j = i + 1; j < nodes.length; j += 1) {
      const b = gstate.positions.get(nodes[j].id);
      let dx = b.x - a.x;
      let dy = b.y - a.y;
      const distance2 = Math.max(100, dx * dx + dy * dy);
      if (distance2 > REPULSION_RANGE2) continue;
      const force = Math.min(0.55, 680 / distance2);
      dx *= force;
      dy *= force;
      a.vx -= dx; a.vy -= dy;
      b.vx += dx; b.vy += dy;
    }
  }
  for (const edge of gstate.graph.relationships) {
    const a = gstate.positions.get(edge.source);
    const b = gstate.positions.get(edge.target);
    if (!a || !b) continue;
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const distance = Math.max(1, Math.hypot(dx, dy));
    const target = edge.type === "GOVERNS" ? 150 : 95;
    const force = (distance - target) * 0.0025;
    a.vx += dx / distance * force; a.vy += dy / distance * force;
    b.vx -= dx / distance * force; b.vy -= dy / distance * force;
  }
  for (const node of nodes) {
    const point = gstate.positions.get(node.id);
    point.vx += -point.x * 0.0007;
    point.vy += -point.y * 0.0007;
    point.vx *= 0.76;
    point.vy *= 0.76;
    point.x += point.vx;
    point.y += point.vy;
  }
}

function createFilters() {
  const container = $("#typeFilters");
  const present = [...new Set(gstate.graph.nodes.map((node) => node.type))];
  if (!gstate.filters.size) present.forEach((type) => gstate.filters.add(type));
  container.replaceChildren();
  // A pseudo-type: only the nodes whose sentence diagram paints completely.
  if (gstate.paintable.size) {
    const chip = element("label", "filter-chip diagram-chip");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = gstate.diagramOnly;
    box.addEventListener("change", () => {
      gstate.diagramOnly = box.checked;
      drawGraph();
    });
    const dot = document.createElement("i");
    dot.className = "diagram-dot";
    chip.append(box, dot, document.createTextNode(`Diagrammed sentences (${gstate.paintable.size})`));
    container.append(chip);
  }
  for (const type of present) {
    const style = nodeStyle[type] || nodeStyle.rule;
    const label = element("label", "filter-chip");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = gstate.filters.has(type);
    input.addEventListener("change", () => {
      if (input.checked) gstate.filters.add(type);
      else gstate.filters.delete(type);
      drawGraph();
    });
    // Charting-legend convention: click the chip to see only that type,
    // click it again to bring everything back. Unticking six boxes to look
    // at one thing was the annoying version of this.
    label.addEventListener("click", (event) => {
      if (event.target === input) return;
      event.preventDefault();
      const soloed = gstate.filters.size === 1 && gstate.filters.has(type);
      gstate.filters = new Set(soloed ? present : [type]);
      createFilters();
      drawGraph();
    });
    const dot = document.createElement("i");
    dot.style.background = style.color;
    label.append(input, dot, document.createTextNode(style.label));
    container.append(label);
  }
}

function renderGraphState() {
  $("#graphEmpty").classList.toggle("hidden", Boolean(gstate.graph.nodes.length));
  drawGraph();
}

function visibleNodes() {
  return gstate.graph.nodes.filter(
    (node) =>
      gstate.filters.has(node.type) &&
      (!gstate.diagramOnly || gstate.paintable.has(node.id))
  );
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
    x: point.x * gstate.transform.k + gstate.transform.x + rect.width / 2,
    y: point.y * gstate.transform.k + gstate.transform.y + rect.height / 2,
  };
}

function drawGraph() {
  const { canvas, rect, ratio, context } = canvasMetrics();
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, rect.width, rect.height);
  if (!gstate.graph.nodes.length) return;

  const visible = new Set(visibleNodes().map((node) => node.id));
  const connected = gstate.selectedId ? gstate.adjacency.get(gstate.selectedId) || new Set() : new Set();
  context.lineWidth = 1;
  for (const edge of gstate.graph.relationships) {
    if (!visible.has(edge.source) || !visible.has(edge.target)) continue;
    const source = screenPoint(gstate.positions.get(edge.source), rect);
    const target = screenPoint(gstate.positions.get(edge.target), rect);
    const inAnswer = gstate.answerIds.size
      && gstate.answerIds.has(edge.source) && gstate.answerIds.has(edge.target);
    const highlighted = gstate.selectedId && (
      edge.source === gstate.selectedId || edge.target === gstate.selectedId
    );
    context.strokeStyle = inAnswer
      ? "rgba(47,125,110,.75)"
      : highlighted ? "rgba(27,130,149,.68)" : "rgba(92,112,130,.16)";
    context.lineWidth = inAnswer ? 2 : highlighted ? 1.8 : 0.75;
    context.beginPath();
    context.moveTo(source.x, source.y);
    context.lineTo(target.x, target.y);
    context.stroke();
  }

  for (const node of visibleNodes()) {
    const point = screenPoint(gstate.positions.get(node.id), rect);
    const style = nodeStyle[node.type] || nodeStyle.rule;
    const matches = true;
    const related = !gstate.selectedId || node.id === gstate.selectedId || connected.has(node.id);
    const cited = gstate.answerIds.has(node.id);
    const inAnswerView = !gstate.answerIds.size || cited;
    context.globalAlpha = matches && related && inAnswerView ? 1 : cited ? 1 : 0.10;
    context.fillStyle = style.color;
    context.beginPath();
    context.arc(point.x, point.y, style.radius * Math.sqrt(gstate.transform.k), 0, Math.PI * 2);
    context.fill();
    if (cited && node.id !== gstate.selectedId) {
      context.strokeStyle = "#2F7D6E";
      context.lineWidth = 2.4;
      context.stroke();
    }
    if (node.id === gstate.selectedId || node.id === gstate.hoveredId) {
      context.strokeStyle = node.id === gstate.selectedId ? "#0d1d33" : "#218899";
      context.lineWidth = 2;
      context.stroke();
    }
    const showLabel = cited || node.type === "document" || node.id === gstate.selectedId ||
      node.id === gstate.hoveredId || (node.type === "contract_scope" && gstate.transform.k > 0.72);
    if (showLabel) {
      context.globalAlpha = matches ? 1 : 0.2;
      context.fillStyle = "#203047";
      context.font = `${node.type === "document" ? "600 " : ""}10px -apple-system, sans-serif`;
      context.textAlign = "center";
      context.fillText(truncateLabel(node.label, 32), point.x, point.y + style.radius + 13);
    }
  }
  context.globalAlpha = 1;
  canvas.classList.toggle("dragging", Boolean(gstate.drag));
}

function truncateLabel(value, maximum) {
  const text = String(value || "");
  return text.length > maximum ? `${text.slice(0, maximum - 1)}…` : text;
}

function fitGraph() {
  const nodes = visibleNodes();
  const rect = $("#graphCanvas").getBoundingClientRect();
  if (!nodes.length || !rect.width || !rect.height) return;
  const xs = nodes.map((node) => gstate.positions.get(node.id).x);
  const ys = nodes.map((node) => gstate.positions.get(node.id).y);
  const width = Math.max(180, Math.max(...xs) - Math.min(...xs) + 100);
  const height = Math.max(180, Math.max(...ys) - Math.min(...ys) + 100);
  gstate.transform.k = Math.min(1.25, Math.max(0.03, Math.min(rect.width / width, rect.height / height)));
  gstate.transform.x = -(Math.max(...xs) + Math.min(...xs)) / 2 * gstate.transform.k;
  gstate.transform.y = -(Math.max(...ys) + Math.min(...ys)) / 2 * gstate.transform.k;
  drawGraph();
}

function fitToNodes(ids) {
  const points = [...ids].map((id) => gstate.positions.get(id)).filter(Boolean);
  const rect = $("#graphCanvas").getBoundingClientRect();
  if (points.length < 2 || !rect.width || !rect.height) return;
  const xs = points.map((point) => point.x);
  const ys = points.map((point) => point.y);
  const width = Math.max(180, Math.max(...xs) - Math.min(...xs) + 160);
  const height = Math.max(180, Math.max(...ys) - Math.min(...ys) + 160);
  gstate.transform.k = Math.min(1.6, Math.max(0.03, Math.min(rect.width / width, rect.height / height)));
  gstate.transform.x = -(Math.max(...xs) + Math.min(...xs)) / 2 * gstate.transform.k;
  gstate.transform.y = -(Math.max(...ys) + Math.min(...ys)) / 2 * gstate.transform.k;
}

function worldFromEvent(event) {
  const rect = $("#graphCanvas").getBoundingClientRect();
  return {
    screenX: event.clientX - rect.left,
    screenY: event.clientY - rect.top,
    x: (event.clientX - rect.left - rect.width / 2 - gstate.transform.x) / gstate.transform.k,
    y: (event.clientY - rect.top - rect.height / 2 - gstate.transform.y) / gstate.transform.k,
  };
}

function nodeAt(event) {
  const point = worldFromEvent(event);
  let best = null;
  let distance = Infinity;
  for (const node of visibleNodes()) {
    const position = gstate.positions.get(node.id);
    const current = Math.hypot(position.x - point.x, position.y - point.y);
    const radius = (nodeStyle[node.type] || nodeStyle.rule).radius / Math.sqrt(gstate.transform.k) + 5;
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
  gstate.drag = {
    nodeId: node?.id || null,
    startX: event.clientX,
    startY: event.clientY,
    screenX: point.screenX,
    screenY: point.screenY,
    originX: gstate.transform.x,
    originY: gstate.transform.y,
    moved: false,
  };
  $("#graphCanvas").setPointerCapture(event.pointerId);
  drawGraph();
});

$("#graphCanvas").addEventListener("pointermove", (event) => {
  if (!gstate.drag) {
    const hovered = nodeAt(event)?.id || null;
    if (hovered !== gstate.hoveredId) {
      gstate.hoveredId = hovered;
      drawGraph();
    }
    return;
  }
  const dx = event.clientX - gstate.drag.startX;
  const dy = event.clientY - gstate.drag.startY;
  if (Math.hypot(dx, dy) > 3) gstate.drag.moved = true;
  if (gstate.drag.nodeId) {
    const world = worldFromEvent(event);
    const position = gstate.positions.get(gstate.drag.nodeId);
    position.x = world.x;
    position.y = world.y;
    position.vx = 0; position.vy = 0;
  } else {
    gstate.transform.x = gstate.drag.originX + dx;
    gstate.transform.y = gstate.drag.originY + dy;
  }
  drawGraph();
});

function endPointer(event) {
  if (!gstate.drag) return;
  const selected = gstate.drag.nodeId;
  const moved = gstate.drag.moved;
  gstate.drag = null;
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
  const old = gstate.transform.k;
  const next = Math.min(3.5, Math.max(0.18, old * Math.exp(-event.deltaY * 0.0012)));
  gstate.transform.x = mouseX - (mouseX - gstate.transform.x) * next / old;
  gstate.transform.y = mouseY - (mouseY - gstate.transform.y) * next / old;
  gstate.transform.k = next;
  drawGraph();
}, { passive: false });

function zoomGraph(factor) {
  gstate.transform.k = Math.min(3.5, Math.max(0.18, gstate.transform.k * factor));
  drawGraph();
}
$("#zoomIn").addEventListener("click", () => zoomGraph(1.25));
$("#zoomOut").addEventListener("click", () => zoomGraph(0.8));
$("#resetView").addEventListener("click", fitGraph);
new ResizeObserver(drawGraph).observe($("#graphStage"));

/* The canvas shows the same picture whatever was asked, until an answer
   arrives: an answer names the records it used, so light exactly those and
   dim the rest. */
function highlightAnswerEvidence(evidence) {
  const wanted = new Set();
  for (const item of evidence || []) {
    const direct = gstate.graph.nodes.find((node) => node.id === item.id);
    const viaClause = direct || gstate.graph.nodes.find((node) => node.clause_id === item.id);
    if (viaClause) wanted.add(viaClause.id);
  }
  gstate.answerIds = wanted;
  if (wanted.size) fitToNodes(wanted);
  drawGraph();
}

function resolveGraphNode(id) {
  const nodes = gstate.graph.nodes;
  const direct = nodes.find((node) => node.id === id);
  if (direct) return direct;
  const viaClause = nodes.find((node) => node.clause_id === id);
  if (viaClause) return viaClause;
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
    gstate.selectedId = node.id;
    renderInspector();
    drawGraph();
    return;
  }
  gstate.selectedId = null;
  showEvidenceOnlyInspector(item);
  drawGraph();
}

function setInspectorOpen(open) {
  const panel = $("#inspectorPanel");
  if (panel) panel.hidden = !open;
}
$("#inspectorClose").addEventListener("click", () => {
  setInspectorOpen(false);
  gstate.selectedId = null;
  drawGraph();
});

function showEvidenceOnlyInspector(item) {
  const inspector = $("#nodeInspector");
  inspector.replaceChildren();
  setInspectorOpen(true);
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
    `This provision is cited by the answer but is not drawn in the ${gstate.view === "overview" ? "overview" : "rule detail"} view.`));
  inspector.append(wrap);
}

function renderInspector() {
  const inspector = $("#nodeInspector");
  inspector.replaceChildren();
  const node = gstate.graph.nodes.find((item) => item.id === gstate.selectedId);
  if (!node) {
    setInspectorOpen(false);
    return;
  }
  setInspectorOpen(true);
  const heading = element("div", "node-title");
  const title = element("div");
  title.append(
    element("span", "eyebrow", node.source || "GRAPH ENTITY"),
    element("h3", "", node.label)
  );
  heading.append(title, element("span", "node-type", (nodeStyle[node.type] || nodeStyle.rule).label));
  inspector.append(heading);
  const grid = element("div", "detail-grid");
  // The field values wear the same marks as the quote below them, so the
  // grid doubles as the key: actor blue, action underlined, object dotted,
  // conditions amber, the deontic verb in its effect's colour.
  const effectClass =
    { PERMISSION: "permission", PROHIBITION: "prohibition", OBLIGATION: "obligation" }[
      String(node.effect || "")
    ] || "";
  const details = [
    ["Section", node.section, ""],
    ["Scope", node.scope, ""],
    ["Effect", node.effect, effectClass ? `an ${effectClass}` : ""],
    ["Modality", node.modality, effectClass ? `an deontic ${effectClass}` : ""],
    ["Polarity", node.polarity, ""],
    ["Actor", node.actor, "an actor"],
    ["Action", node.action, "an action"],
    ["Object", node.object, "an object"],
    ["Conditions", Array.isArray(node.conditions) ? node.conditions.join("; ") : node.conditions, "an condition"],
    ["Carve-outs", Array.isArray(node.carve_outs) ? node.carve_outs.join("; ") : node.carve_outs, "an condition"],
    ["Status", node.status, ""],
    ["Source", node.source, ""],
    ["Model", node.model, ""],
  ].filter(([, value]) => value);
  for (const [label, value, cls] of details) {
    const item = element("div", "detail-item");
    item.append(element("b", "", label), element("span", cls, String(value)));
    item.querySelector("span").title = String(value);
    grid.append(item);
  }
  inspector.append(grid);
  const segments = node.evidence_segments || [];
  if (segments.length) {
    const source = element("section", "clause-source");
    source.append(element("span", "eyebrow", "SOURCE TEXT"));
    const reading = typeof presentableRule === "function" ? presentableRule(node) : null;
    for (const segment of segments) {
      const segmentNode = element("div", `anatomy-evidence ${segment.purpose || "clause"}`);
      segmentNode.append(
        element("b", "", segment.purpose === "chapeau" ? "Governing chapeau" : segment.purpose === "list_item" ? "Operative list item" : "Exact evidence")
      );
      // The colours are the point of this panel: paint the segment with the
      // node's own reading, under the same rules the chat cards use.
      if (reading && typeof annotatedQuote === "function") {
        const painted = annotatedQuote(segment.text, reading);
        painted.classList.add("segment-quote");
        segmentNode.append(painted);
      } else {
        segmentNode.append(element("q", "", segment.text));
      }
      source.append(segmentNode);
    }
    if (typeof propositionCount === "function") {
      const total = segments.reduce(
        (sum, segment) => sum + propositionCount(segment.text),
        0
      );
      if (total > 1) {
        source.append(
          element(
            "p",
            "proposition-note",
            `This passage states ${total} deontic propositions; the reading above describes the highlighted one.`
          )
        );
      }
    }
    inspector.append(source);
  } else {
    const evidence = node.evidence || node.description;
    if (evidence) {
      // Rule nodes carry their own fields; paint them onto the quote with the
      // same honesty gate the chat's evidence cards use.
      if (typeof annotatedQuote === "function" && typeof presentableRule === "function") {
        const quote = annotatedQuote(evidence, presentableRule(node));
        quote.classList.add("evidence-quote");
        inspector.append(quote);
      } else {
        inspector.append(element("blockquote", "evidence-quote", evidence));
      }
    }
  }
}

/* Light a cited record's node for context without rewriting the inspector:
   the evidence card that was clicked owns the inspector's content. */
function revealGraphNode(id) {
  const node = resolveGraphNode(id);
  gstate.selectedId = node ? node.id : null;
  drawGraph();
}


/* The nodes whose reading paints completely -- actor, deontic, action and
   more all anchored verbatim in the clause. One node per distinct sentence:
   the deep build sometimes stutters near-identical rules for one clause, and
   a filter that shows seven copies of one sentence is showing the stutter. */
function computePaintable() {
  gstate.paintable = new Set();
  const seen = new Set();
  for (const node of gstate.graph.nodes) {
    if (node.type !== "rule" && node.type !== "llm_rule") continue;
    const reading = typeof presentableRule === "function" ? presentableRule(node) : null;
    if (!reading || !node.evidence) continue;
    const quote = annotatedQuote(node.evidence, reading);
    if (quote.querySelectorAll(".an").length < 3) continue;
    const sentence = String(node.evidence).toLowerCase().replace(/\s+/g, " ").slice(0, 110);
    if (seen.has(sentence)) continue;
    seen.add(sentence);
    gstate.paintable.add(node.id);
  }
}

window.AtlasGraph = {
  load: () => loadGraph().catch(() => {}),
  highlight: highlightAnswerEvidence,
  select: selectGraphNode,
  reveal: revealGraphNode,
};
