/* AgreementAtlas public demo console.
   Talks to the same session-scoped API as the workbench; renders a
   question-led view of it. No framework, no external requests. */

"use strict";

const $ = (selector) => document.querySelector(selector);

const state = {
  status: null,
  model: "",
  busy: false,
};

function api(path, options = {}) {
  const headers = Object.assign(
    { "X-AgreementAtlas-Request": "1" },
    options.body ? { "Content-Type": "application/json" } : {},
    options.headers || {}
  );
  return fetch(path, Object.assign({}, options, { headers })).then(
    async (response) => {
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        const error = new Error(payload.error || "The request failed.");
        error.code = payload.code || "";
        error.status = response.status;
        throw error;
      }
      return payload;
    }
  );
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/* ---------------- status + samples ---------------- */

function renderLmState(status) {
  const lm = status.lmstudio || {};
  const dot = $("#lmDot");
  const label = $("#lmLabel");
  if (lm.available) {
    dot.className = "dot ok";
    const model = String(status.lmstudio.selected_model || "local model");
    label.textContent = `${model.split("/").pop()} · ready`;
    state.model = model;
  } else {
    dot.className = "dot down";
    label.textContent = "Local model offline — answers paused";
    state.model = "";
  }
  $("#askButton").disabled = !lm.available || state.busy;
}

function renderSamples(status) {
  const host = $("#sampleSelect");
  host.textContent = "";
  const active = String(status.sample_name || "");
  const documents = (status.documents || []).length;
  (status.samples || []).forEach((sample) => {
    const seg = element("button", "seg", sample.short_name || sample.name);
    seg.type = "button";
    seg.setAttribute("aria-pressed", String(sample.name === active));
    seg.addEventListener("click", () => loadSample(sample));
    host.append(seg);
  });
  // Uploading is a different kind of act from picking a sample, so it is not
  // a third segment: the toggle below opens it.
  const toggle = $("#uploadToggle");
  const own = !status.is_sample && documents > 0;
  toggle.textContent = own
    ? "Your uploaded agreements are loaded — upload a different set"
    : "Upload your own agreements";
  toggle.classList.toggle("active", own);
}

$("#uploadToggle").addEventListener("click", () => {
  const strip = $("#uploadStrip");
  strip.hidden = !strip.hidden;
});

$("#uploadPick").addEventListener("click", () => {
  const note = $("#uploadNote");
  if (!$("#uploadConsent").checked) {
    note.textContent = "Tick the authorisation box first.";
    note.classList.add("error");
    return;
  }
  note.classList.remove("error");
  $("#uploadInput").click();
});
$("#uploadConsent").addEventListener("change", () => {
  const note = $("#uploadNote");
  if ($("#uploadConsent").checked && note.classList.contains("error")) {
    note.textContent = "";
    note.classList.remove("error");
  }
});

$("#uploadInput").addEventListener("change", (event) => {
  if (event.target.files.length) {
    uploadFiles(event.target.files, $("#uploadNote"), $("#uploadPick"));
  }
});



/* ---------------- enrichment of uploaded families ----------------
   Slow is accepted: that is what local models are. Answers work from the
   deterministic parse immediately; readings and embeddings sharpen them as
   they land. Progress is the server's durable job state, so a reloaded page
   resumes the same story. */

let enrichTimer = null;

function setEnrichNote(text, error) {
  const note = $("#enrichNote");
  if (!note) return;
  note.hidden = !text;
  note.textContent = text || "";
  note.classList.toggle("error", Boolean(error));
}

function startEnrichment() {
  if (!state.model) {
    setEnrichNote(
      "The local model is offline, so enrichment is paused. Answers use the deterministic parse.",
      true
    );
    return;
  }
  api("/api/enrich", { method: "POST", body: JSON.stringify({ model: state.model }) })
    .then(() => pollEnrichment())
    .catch((error) => {
      setEnrichNote(
        `Enrichment could not start (${error.message}) — answers still work from the deterministic parse.`,
        true
      );
    });
}

function pollEnrichment() {
  window.clearTimeout(enrichTimer);
  api("/api/enrich/status")
    .then((job) => {
      if (job.state === "running") {
        const done = job.completed || 0;
        const total = job.total || 0;
        setEnrichNote(
          `Reading clauses into rules on the local model — ${done}${total ? ` of ${total}` : ""}. ` +
            "Answers work now and sharpen as readings land; questions queue behind this while it runs."
        );
        enrichTimer = window.setTimeout(pollEnrichment, 4000);
        return;
      }
      if (job.state === "complete") {
        setEnrichNote("Enrichment finished: model readings and embeddings are live for this family.");
        window.setTimeout(() => setEnrichNote(""), 12000);
        refresh().then(() => window.AtlasGraph && window.AtlasGraph.load());
        return;
      }
      if (job.state === "error") {
        setEnrichNote(
          `Enrichment stopped: ${job.error || "the model call failed"}. Deterministic answers remain available.`,
          true
        );
        return;
      }
      setEnrichNote("");
    })
    .catch(() => {
      enrichTimer = window.setTimeout(pollEnrichment, 8000);
    });
}

/* Uploading replaces whatever the session holds: parsed deterministically in
   seconds, then enrichment runs in the background. A fresh session first --
   uploading into a workspace still holding a sample would merge two unrelated
   families into one graph. */
function uploadFiles(files, note, pick) {
  if (state.busy) return;
  state.busy = true;
  pick.disabled = true;
  note.classList.remove("error");
  note.textContent = `Parsing ${files.length} file(s)…`;
  const body = new FormData();
  [...files].forEach((file) => body.append("files", file));
  api("/api/session", { method: "DELETE" })
    .then(() =>
      fetch("/api/upload", {
        method: "POST",
        headers: { "X-AgreementAtlas-Request": "1" },
        body,
      })
    )
    .then(async (response) => {
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || "The upload failed.");
      note.textContent = "Parsed. Ask away.";
      clearThread();
      return refresh().then(() => startEnrichment());
    })
    .catch((error) => {
      note.textContent = error.message;
      note.classList.add("error");
    })
    .finally(() => {
      state.busy = false;
      pick.disabled = false;
    });
}

function renderDocuments(status) {
  const host = $("#familyDocs");
  host.textContent = "";
  const summary = $("#docsSummary");
  if (summary) {
    const active = (status.samples || []).find(
      (item) => item.name === String(status.sample_name || "")
    );
    summary.textContent = `Documents in this family (${(status.documents || []).length})`;
    if (active) {
      summary.append(
        document.createTextNode(
          ` · ${active.clauses} clauses · ${active.definitions} defined terms · `
        )
      );
      const link = element("a", "source-link", "the vendor's published originals →");
      link.href = active.source_url || "#";
      link.addEventListener("click", (event) => {
        event.preventDefault();
        window.open(active.source_url, "_blank", "noopener");
      });
      summary.append(link);
    }
  }
  (status.documents || []).forEach((doc) => {
    const item = element("li");
    item.append(element("span", "", doc.title || doc.name));
    item.append(
      element("span", "doc-type", doc.instrument_type || doc.document_type || "")
    );
    host.append(item);
  });
}

function renderQuestions(status) {
  const host = $("#questionChips");
  host.textContent = "";
  const active = String(status.sample_name || "");
  const sample = (status.samples || []).find((item) => item.name === active);
  const questions = (sample && sample.questions) || [];
  questions.forEach((question) => {
    const chip = element("button", "", question);
    chip.type = "button";
    chip.addEventListener("click", () => {
      $("#questionInput").value = question;
      ask(question);
    });
    host.append(chip);
  });
}

function refresh() {
  return api("/api/status").then((status) => {
    const before = state.status?.build?.built_at;
    state.status = status;
    renderLmState(status);
    renderSamples(status);
    renderDocuments(status);
    renderQuestions(status);
    // The graph belongs to whatever family the workspace now holds; reload it
    // when the workspace changed (or on the first status of the page).
    if (window.AtlasGraph && status.build?.built_at !== before) {
      window.AtlasGraph.load();
    }
  });
}

function loadSample(sample) {
  if (state.busy) return;
  const current = String((state.status || {}).sample_name || "");
  if (sample.name === current) return;
  api("/api/demo", { method: "POST", body: JSON.stringify({ bundle: sample.slug }) })
    .then(() => {
      clearThread();
      return refresh();
    })
    .catch(showError);
}

/* ---------------- the thread ---------------- */

function clearThread() {
  exchangeCount = 0;
  const thread = $("#thread");
  thread.textContent = "";
  const empty = element("div", "thread-empty");
  empty.id = "threadEmpty";
  empty.append(
    element("p", "", "Pick a question above, or ask your own. The hard ones are the point.")
  );
  thread.append(empty);
}

function showError(error, block) {
  const note = element("div", "turn-a error", error.message || "Something failed.");
  appendTurn(note, block);
}

let exchangeCount = 0;

/* One question and its answer share a block. After three or four turns an
   undivided thread is a wall of quoted contract text; a rule and a turn
   number make it navigable without adding another colour to a page whose
   colours already mean something. */
function openExchange(question) {
  const empty = $("#threadEmpty");
  if (empty) empty.remove();
  exchangeCount += 1;
  const block = element("div", "exchange");
  block.append(element("span", "turn-index", `Q${exchangeCount}`));
  block.append(element("div", "turn-q", question));
  $("#thread").append(block);
  return block;
}

function appendTurn(node, block) {
  const empty = $("#threadEmpty");
  if (empty) empty.remove();
  const thread = $("#thread");
  (block || thread.lastElementChild || thread).append(node);
  thread.scrollTop = thread.scrollHeight;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

/* Minimal, safe subset of the model's markdown habits: bold and citations.
   Citations arrive alone ("[6]") and grouped ("[10, 11, 12]") -- both forms
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
  // The words of the agreement are the point of every answer; give them the
  // highlighter treatment so a quoted phrase reads as evidence, not prose.
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

function evidenceReading(item, rule) {
  const host = element("div", "ev-reading");
  if (item.term) {
    host.append(element("span", "chip", "DEFINES"));
    host.append(element("span", "chip", String(item.term).slice(0, 48)));
    return host;
  }
  if (!rule) {
    // Absence is a state worth naming: this passage carries no reading that
    // survived validation, and saying so beats a card that is quietly bare.
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
      element(
        "span",
        "chip chip-none",
        `1 of ${propositions} propositions here`
      )
    );
  }
  return host;
}

/* The short title of the document an evidence item quotes, from the same
   status payload the sidebar shows -- one vocabulary everywhere. */
function documentTitle(source) {
  const documents = (state.status || {}).documents || [];
  const match = documents.find((doc) => doc.name === source);
  return (match && match.title) || String(source || "document");
}

/* The modal that belongs to THIS reading, not the first one in the clause.
   A licence-model section is several propositions in one blob -- "(i)
   Licensee must purchase ... (ii) The Software may only be used with MFP" --
   so painting the first modal put MUST under a reading whose modality is MAY.
   Search for the rule's own modal, and among its occurrences take the one
   nearest the action: that is the proposition the reading came from. */
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

/* A reading is painted only when it reads as a rule: a named actor and a
   short verb phrase. The deterministic parse of an inheritance sentence
   ("...shall be identical to those...") stores no actor, "identical to" as
   the action and a paragraph-long carve-out -- displaying that as anatomy is
   how a correct quote ends up wearing a wrong label. No reading, no paint. */
function presentableRule(rule) {
  if (!rule) return null;
  const actor = String(rule.actor || "").trim();
  const action = String(rule.action || "").trim();
  if (!actor || actor.split(/\s+/).length > 6) return null;
  if (!action || action.split(/\s+/).length > 8) return null;
  return rule;
}

/* Paint a rule's fields inside its own quote -- but only fields whose text
   locates exactly once in the quote. A wrong highlight is worse than none:
   anything ambiguous stays plain. */
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
    // A span longer than a clause-sized phrase paints a paragraph, which
    // stops communicating structure -- leave it plain.
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
    // Anchor on the action if it was located; otherwise the first occurrence
    // of the right modal is the best available guess.
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
    if (span.start < cursor) return; // overlap: first claim wins
    quote.append(document.createTextNode(shown.slice(cursor, span.start)));
    quote.append(
      element("span", `an ${span.cls}`, shown.slice(span.start, span.end))
    );
    cursor = span.end;
  });
  quote.append(document.createTextNode(shown.slice(cursor)));
  return quote;
}


/* How many distinct deontic propositions the passage contains. A licence
   model section routinely carries several; a reading describes one of them,
   and saying so is more honest than implying it describes the paragraph. */
function propositionCount(text) {
  const found = String(text || "").match(ANY_MODAL);
  return found ? found.length : 0;
}

function evidenceCard(item, index, turnId) {
  const details = element("details", "evidence-item");
  details.id = `${turnId}-ev-${index}`;
  const summary = element("summary");
  summary.append(element("span", "ev-index", `[${index}]`));
  summary.append(element("span", "ev-doc", documentTitle(item.source)));
  summary.append(element("span", "", `§${item.section || "—"}`));
  if (item.term) summary.append(element("span", "", `“${item.term}”`));
  // The document and section name a place; the hint says which sentence
  // lives there, so [6] and [7] in the same section stop looking identical.
  const hint = item.term
    ? ""
    : String((presentableRule(item.rule) || {}).action || item.text || "").slice(0, 60);
  if (hint) summary.append(element("span", "ev-hint", hint));
  // The expanded card already shows the annotated quote, fields and chips;
  // opening an inspector with the same content was duplication. The click
  // lights the record's node on the canvas and nothing more.
  summary.addEventListener("click", () => {
    if (window.AtlasGraph) window.AtlasGraph.reveal(item.id);
  });
  details.append(summary);
  const reading = presentableRule(item.rule);
  details.append(annotatedQuote(item.text, reading));
  details.append(evidenceReading(item, reading));
  return details;
}


let turnCounter = 0;

function renderAnswer(result, block) {
  turnCounter += 1;
  const turnId = `turn${turnCounter}`;
  const turn = element("div", "turn-a");
  turn.id = turnId;

  // A one-word reply to "which applies?" is rewritten server-side into the
  // question it answers. Invisible, that rewrite reads as the system ignoring
  // the reply -- say what was understood.
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
    // Only the passages the answer cites appear as cards. The rest of the
    // retrieval was scrolling, not reading -- it moves behind one line.
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
    // Both groups fold. The cards arrive all at once after the streamed
    // prose, and a stack of open cards made the thread jump; two closed
    // lines add almost no height, and a citation click opens its card.
    if (citedCards.length) {
      const fold = element("details", "more-evidence cited-evidence");
      fold.append(element("summary", "", `Cited clauses (${citedCards.length})`));
      citedCards.forEach((card) => fold.append(card));
      list.append(fold);
    }
    if (rest.length) {
      const more = element("details", "more-evidence");
      more.append(
        element(
          "summary",
          "",
          `Also retrieved but not cited (${rest.length})`
        )
      );
      rest.forEach((card) => more.append(card));
      list.append(more);
    }
    turn.append(list);
  }
  // An answer that ends "which applies?" should not make the reader type the
  // name back. One click sends the variant; the server resolves it against
  // what this answer offered.
  if (window.AtlasGraph) window.AtlasGraph.highlight(evidence);
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
}

function ask(question) {
  const trimmed = String(question || "").trim();
  if (trimmed.length < 2 || state.busy || !state.model) return;
  state.busy = true;
  $("#askButton").disabled = true;

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
    state.busy = false;
    $("#askButton").disabled = !state.model;
    $("#questionInput").value = "";
  };

  fetch("/api/query", {
    method: "POST",
    headers: {
      "X-AgreementAtlas-Request": "1",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      question: trimmed,
      model: state.model,
      stream: true,
      reasoning: $("#thinkToggle").checked,
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
          if (nameLine[1] === "thinking") {
            thinkingText += payload.text || "";
            ensureThinking();
            // The working box scrolls internally once it fills, so following
            // the stream means pinning *its* bottom -- unless the reader has
            // scrolled up inside it, in which case leave them where they are.
            const stick =
              thinkingBody.scrollHeight -
                thinkingBody.scrollTop -
                thinkingBody.clientHeight <
              48;
            thinkingBody.textContent = thinkingText;
            if (stick) thinkingBody.scrollTop = thinkingBody.scrollHeight;
            const thread = $("#thread");
            if (
              thread.scrollHeight - thread.scrollTop - thread.clientHeight <
              160
            ) {
              thread.scrollTop = thread.scrollHeight;
            }
          } else if (nameLine[1] === "token") {
            text += payload.text || "";
            body.replaceChildren();
            body.innerHTML = renderAnswerText(text, "live");
            body.append(cursor);
            const thread = $("#thread");
            thread.scrollTop = thread.scrollHeight;
          } else if (nameLine[1] === "result") {
            finished = true;
            live.remove();
            if (thinkingBox) thinkingBox.open = false;
            renderAnswer(payload, block);
          } else if (nameLine[1] === "error") {
            finished = true;
            live.remove();
            showError(new Error(payload.error || "The model call failed."), block);
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
      showError(error);
    })
    .finally(settle);
}

/* ---------------- wiring ---------------- */

$("#thread").addEventListener("click", (event) => {
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
  ask($("#questionInput").value);
});

$("#questionInput").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    ask($("#questionInput").value);
  }
});

$("#resetChat").addEventListener("click", () => {
  // Clearing the chat also clears the workspace, so the next status call
  // reloads the default sample -- the same state a first visitor gets.
  api("/api/session", { method: "DELETE" })
    .then(() => {
      clearThread();
      setEnrichNote("");
      $("#uploadStrip").hidden = true;
      return refresh();
    })
    .then(() => window.AtlasGraph && window.AtlasGraph.load())
    .catch(showError);
});

$("#deleteSession").addEventListener("click", () => {
  api("/api/session", { method: "DELETE" })
    .then(() => {
      clearThread();
      return refresh();
    })
    .catch(showError);
});

refresh()
  .then(() => {
    if (state.status?.enrichment?.state === "running") pollEnrichment();
  })
  .catch(() => {
    $("#lmLabel").textContent = "The server did not answer. Refresh to retry.";
  });
/* The model can come back (or fall asleep) while the page sits open. */
setInterval(() => {
  if (!state.busy) refresh().catch(() => {});
}, 45000);
