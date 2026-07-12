/* salient-tutor — streaming Coach workspace + Library.
   Talks to the tutor over /ws/tutor (live thinking / text / tool-calls / done),
   with a skill-map + KB-search + lesson-plans + context rail, Mermaid
   step-through + auto-repair, memory-palace cards, read-aloud, and export. */

(function () {
  "use strict";

  // ── sentinels (contract with prompts/tutor.md) ──
  const EXPORT_TOKEN = "__EXPORT_LESSON__";
  const FIX_TOKEN = "__FIX_DIAGRAM__";
  const DRILL_TOKEN = "__DRILL__";
  const STUDY_TOKEN = "__STUDY__";

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
  const esc = (t) => { const d = document.createElement("div"); d.textContent = t == null ? "" : t; return d.innerHTML; };

  const messagesEl = $("messages"), inputEl = $("prompt-input"), chatScroll = $("chat-scroll");
  const DEFAULT_PLACEHOLDER = inputEl.getAttribute("placeholder") || "";
  let ws = null, wsReady = false, busy = false, welcomeShown = false;
  let pendingSend = null;  // a turn submitted before the socket opened; flushed on onopen
  let pending = null;            // { bubble, contentEl, raw } for the streaming reply
  let captureMode = "normal";    // "normal" | "exporting"
  let currentAgent = "tutor";    // which tutor variant we're talking to
  let lastQuestion = null;       // last real operator question (for second opinion)
  let panelReady = false;        // ≥2 tutors running → consensus panel available
  let strictness = loadStrictness(); // judge pedagogy-filter level (persisted)
  let diagramEngine = loadDiagramEngine(); // which fence dialect the tutor emits
  let imageModel = loadImageModel(); // "off" or a diffusion model (persisted)
  let imageAvailable = false;    // server can render illustrations (box + package)
  let imageDefault = null;       // model used when the dial is off but user forces one
  let forceImageModel = null;    // one-shot override for a manual "🎨 illustrate" turn

  function loadStrictness() {
    const v = (() => { try { return localStorage.getItem("tutor.strictness"); } catch (_) { return null; } })();
    return ["explain", "socratic", "bare"].includes(v) ? v : "socratic";
  }

  function loadDiagramEngine() {
    const v = (() => { try { return localStorage.getItem("tutor.diagramEngine"); } catch (_) { return null; } })();
    return ["auto", "mermaid", "dot", "d2", "plantuml"].includes(v) ? v : "auto";
  }

  // Diffusion illustrations default OFF (normal diagramming). Validated against
  // the server's advertised models in initImageModel().
  function loadImageModel() {
    try { return localStorage.getItem("tutor.imageModel") || "off"; } catch (_) { return "off"; }
  }

  // htmlLabels:false renders labels as SVG <text> (not foreignObject HTML) so
  // diagrams rasterize cleanly to canvas for the WebM walkthrough recorder.
  if (window.mermaid) mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: "dark", flowchart: { htmlLabels: false } });
  const reduceMotion = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ══════════════════════════════════════════════════════════════════
  //  Markdown
  // ══════════════════════════════════════════════════════════════════
  function renderMd(text) {
    // Capture group 2 is the fence info-string tail (e.g. "labeled" in
    // ```image labeled) — used by the image channel to pick a mode. Empty for
    // every existing dialect, so their handling is unchanged.
    const blocks = []; const fenceRe = /```([\w+-]*)([^\n]*)\n([\s\S]*?)```/g;
    let m, li = 0, html = "";
    while ((m = fenceRe.exec(text)) !== null) {
      if (m.index > li) html += inline(text.slice(li, m.index));
      const lang = (m[1] || "").toLowerCase(), info = (m[2] || "").trim().toLowerCase(), code = m[3].replace(/\n$/, "");
      // mermaid renders client-side; dot/d2/plantuml render server-side via
      // /api/diagram. Both flow through renderMermaid(), which dispatches on
      // `engine` — so every existing call site keeps working unchanged.
      if (lang === "mermaid" || lang === "dot" || lang === "d2" || lang === "plantuml") {
        const id = "mmd-" + Math.random().toString(36).slice(2, 9);
        html += `<div class="mermaid" id="${id}"></div>`;
        blocks.push({ id, code, engine: lang });
      }
      // Diffusion illustration: a placeholder card that renderImage() fills via
      // POST /api/image. Rides the same blocks[] + renderMermaid() dispatch path
      // as diagrams (engine:"image"), so every existing call site works unchanged.
      else if (lang === "image") {
        const id = "img-" + Math.random().toString(36).slice(2, 9);
        const mode = ["mnemonic", "loci", "labeled"].includes(info.split(/\s+/)[0]) ? info.split(/\s+/)[0] : "mnemonic";
        html += `<div class="image-card" id="${id}"></div>`;
        blocks.push({ id, code, engine: "image", mode });
      }
      else if (lang === "loci") html += lociCard(code);
      // Memory palace: a JSON walk of linked loci. Placeholder filled by
      // renderPalace() (parses JSON → recall-ladder cards; each locus reuses
      // renderImage() with mode "loci", grades POST /api/review).
      else if (lang === "palace") {
        const id = "palace-" + Math.random().toString(36).slice(2, 9);
        html += `<div class="palace" id="${id}"></div>`;
        blocks.push({ id, code, engine: "palace" });
      }
      else html += `<pre><code>${esc(code)}</code></pre>`;
      li = fenceRe.lastIndex;
    }
    if (li < text.length) html += inline(text.slice(li));
    return { html, mermaidBlocks: blocks };
  }

  function inline(text) {
    if (!text) return "";
    // Tables first (line-based), then the rest.
    let h = esc(text);
    h = h.replace(/^### (.+)$/gm, "<h3>$1</h3>")
         .replace(/^## (.+)$/gm, "<h2>$1</h2>")
         .replace(/^# (.+)$/gm, "<h1>$1</h1>")
         .replace(/^---$/gm, "<hr>")
         .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
         .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
         .replace(/`([^`]+)`/g, "<code>$1</code>")
         .replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
         .replace(/^&gt; (.+)$/gm, "<blockquote>$1</blockquote>")
         .replace(/^\s*\d+\. (.+)$/gm, "<oli>$1</oli>")
         .replace(/^\s*[-*] (.+)$/gm, "<li>$1</li>");
    h = h.replace(/(<oli>[\s\S]*?<\/oli>)(?=\n\n|\n[#<]|$)/g, (b) => "<ol>" + b.replace(/<\/?oli>/g, (t) => t[1] === "/" ? "</li>" : "<li>") + "</ol>");
    h = h.replace(/(<li>[\s\S]*?<\/li>)(?=\n\n|\n[#<]|$)/g, "<ul>$1</ul>");
    h = renderTables(h);
    h = h.replace(/\n\n/g, "</p><p>").replace(/\n/g, "<br>");
    h = "<p>" + h + "</p>";
    h = h.replace(/<p>(<(?:h[1-3]|ul|ol|blockquote|hr|table))/g, "$1")
         .replace(/(<\/(?:h[1-3]|ul|ol|blockquote|table)>|<hr>)<\/p>/g, "$1")
         .replace(/<p><\/p>/g, "");
    return h;
  }

  function renderTables(h) {
    return h.replace(/((?:^\|.*\|[ \t]*\n?)+)/gm, (block) => {
      const rows = block.trim().split("\n").map(r => r.trim());
      if (rows.length < 2 || !/^\|[\s:|-]+\|$/.test(rows[1])) return block;
      const cells = (r) => r.replace(/^\||\|$/g, "").split("|").map(c => c.trim());
      const head = cells(rows[0]).map(c => `<th>${c}</th>`).join("");
      const body = rows.slice(2).map(r => "<tr>" + cells(r).map(c => `<td>${c}</td>`).join("") + "</tr>").join("");
      return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
    });
  }

  function lociCard(code) {
    const lines = code.split("\n").filter(l => l.trim());
    if (!lines.length) return "";
    const hook = lines[0];
    const body = lines.slice(1).filter(l => !/^recall:/i.test(l));
    const recall = lines.slice(1).find(l => /^recall:/i.test(l));
    let html = `<details class="loci-card"><summary>${esc(hook)}</summary><div class="loci-body">`;
    if (body.length) html += "<ul>" + body.map(l => `<li>${esc(l)}</li>`).join("") + "</ul>";
    if (recall) html += `<div class="loci-recall">${esc(recall.replace(/^recall:\s*/i, ""))}</div>`;
    return html + "</div></details>";
  }

  // ══════════════════════════════════════════════════════════════════
  //  Mermaid render + step-through + auto-repair
  // ══════════════════════════════════════════════════════════════════
  // Renders every diagram block collected by renderMd. Dispatches on engine:
  // mermaid renders in-browser (Mermaid.js), while dot/d2/plantuml POST their
  // source to /api/diagram and get back sanitized SVG from the local engine
  // binary. Both failure paths show the same error card + repair button.
  async function renderMermaid(blocks) {
    if (!blocks.length) return;
    for (const b of blocks) {
      const host = $(b.id); if (!host) continue;
      const engine = b.engine || "mermaid";
      if (engine === "image") { await renderImage(host, b.code, b.mode); continue; }
      if (engine === "palace") { renderPalace(host, b.code); continue; }
      if (engine === "mermaid") {
        if (!window.mermaid) continue;
        try {
          const { svg } = await mermaid.render(b.id + "-svg", b.code);
          host.innerHTML = svg;
          addStepper(host, b.code);
        } catch (e) { diagramError(host, "mermaid", b.code); }
      } else {
        try {
          const r = await fetch("/api/diagram", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ engine, source: b.code }),
          });
          const data = await r.json();
          if (data.svg) host.innerHTML = data.svg;
          else diagramError(host, engine, b.code, data.error);
        } catch (e) { diagramError(host, engine, b.code, String(e)); }
      }
    }
  }

  // Shared parse-error card: message + a button that asks the tutor to repair
  // the (engine-tagged) diagram via the __FIX_DIAGRAM__ machine turn.
  function diagramError(host, engine, code, detail) {
    host.className = "mermaid-error";
    host.textContent = "Diagram error" + (detail ? ` (${String(detail).slice(0, 140)}). ` : ". ");
    const fix = el("button", "btn small ghost", "🔧 Ask tutor to fix");
    fix.onclick = () => { if (!busy) send(FIX_TOKEN + "\n```" + engine + "\n" + code + "\n```"); };
    host.appendChild(fix);
  }

  // ── Diffusion illustration card ──
  // Pull the caption out of the fence body (a `caption:` line, else first line).
  function imageCaption(code) {
    const lines = code.split("\n").map(l => l.trim()).filter(Boolean);
    const cap = lines.find(l => /^caption:/i.test(l));
    return (cap ? cap.replace(/^caption:\s*/i, "") : (lines[0] || "Illustration")).slice(0, 160);
  }

  // Stable localStorage key for a fence's rendered image. Lets a reload / new
  // session paint the EXACT previous PNG straight from disk (its remembered URL,
  // served by GET /api/image/<hash>.png) instead of re-POSTing to /api/image —
  // which round-trips, flashes a shimmer, and regenerates outright if the
  // content-address ever drifts. The PNG itself already persists under
  // work/images across restarts; this just skips the redundant round-trip.
  function imgKey(code, mode) {
    const s = (mode || "mnemonic") + "|" + code;
    let h = 0;
    for (let i = 0; i < s.length; i++) h = (Math.imul(h, 31) + s.charCodeAt(i)) | 0;
    return "tutor.img." + (h >>> 0).toString(36);
  }

  // Paint a finished image card from a known URL (+ Regenerate + self-heal).
  function paintImage(host, url, caption, code, mode) {
    host.className = "image-card";
    host.innerHTML =
      `<figure><img src="${url}" alt="${esc(caption)}" loading="lazy">` +
      `<figcaption>${esc(caption)}</figcaption></figure>`;
    const img = host.querySelector("img");
    if (img) img.onerror = () => {  // cached file evicted → forget the memo + regenerate
      img.onerror = null;
      try { localStorage.removeItem(imgKey(code, mode)); } catch (_) {}
      renderImage(host, code, mode);
    };
    const re = el("button", "btn small ghost", "↻ Regenerate");
    re.onclick = () => renderImage(host, code, mode, Math.random().toString(36).slice(2, 8));
    host.appendChild(re);
  }

  // Fills a placeholder card by POSTing the fence to /api/image and swapping in
  // the returned PNG. Async + slow (seconds–minutes) and serialized server-side;
  // the caption shows immediately so the card is meaningful before pixels arrive.
  // When illustrations are toggled off, renders caption-only (no GPU call).
  async function renderImage(host, code, mode, variant) {
    mode = mode || "mnemonic";
    const caption = imageCaption(code);
    // Already rendered this fence before? Paint it straight from disk (no POST,
    // no GPU/cloud, no shimmer). Skipped for an explicit regenerate (variant).
    if (!variant) {
      let saved = null;
      try { saved = localStorage.getItem(imgKey(code, mode)); } catch (_) {}
      if (saved) { paintImage(host, saved, caption, code, mode); return; }
    }
    // Effective model: the dial's pick, else the default (used by a manual
    // "🎨 illustrate" request while the dial is on "No art"). If the server can't
    // render at all, degrade to a caption-only card. (The server pins `labeled`
    // to qwen regardless; the model sent is only the preference for other modes.)
    const model = imageModel !== "off" ? imageModel : imageDefault;
    if (!imageAvailable || !model) {
      host.className = "image-card image-off";
      host.innerHTML = `<div class="image-cap">🖼 ${esc(caption)}</div>`;
      return;
    }
    host.className = "image-card image-loading";
    host.innerHTML = `<div class="image-shimmer"></div><div class="image-cap">🖼 ${esc(caption)} <span class="image-status">generating…</span></div>`;
    // A variant nonce changes the spec hash → a fresh seed on the server (specs
    // are otherwise deterministic and would re-serve the same cached image).
    const source = variant ? code + "\n# variant " + variant : code;
    try {
      const r = await fetch("/api/image", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source, model, mode }),
      });
      const data = await r.json();
      if (data.url) {
        // Remember the URL under the BASE fence key (even for a regenerate), so a
        // later reload paints the latest render straight from disk.
        try { localStorage.setItem(imgKey(code, mode), data.url); } catch (_) {}
        paintImage(host, data.url, caption, code, mode);
      } else {
        imageError(host, code, mode, caption, data.error);
      }
    } catch (e) { imageError(host, code, mode, caption, String(e)); }
  }

  function imageError(host, code, mode, caption, detail) {
    host.className = "image-card image-err";
    host.innerHTML = `<div class="image-cap">🖼 ${esc(caption)}</div>` +
      `<div class="image-detail">Illustration unavailable${detail ? " (" + esc(String(detail).slice(0, 120)) + ")" : ""}.</div>`;
    const re = el("button", "btn small ghost", "↻ Retry");
    re.onclick = () => renderImage(host, code, mode, Math.random().toString(36).slice(2, 8));
    host.appendChild(re);
  }

  // ── Memory palace (method-of-loci recall ladder) ──
  // Renders a ```palace JSON fence into rooms of per-locus cards. Each locus is
  // an active-recall ladder: prompt (locusPhrase) → hint (metaphorAnchor) →
  // reveal (loci image + caption mapping + fact) → grade. Grades POST to
  // /api/review under topic `loci:<palaceId>/<locusId>`, so palace mastery rides
  // the SAME SM-2 gradebook as quizzes (no parallel SRS). callbackTo scrolls to
  // and flashes the referenced locus card.
  function renderPalace(host, code) {
    let palace;
    try { palace = JSON.parse(code); } catch (_) {
      host.className = "palace palace-err";
      host.textContent = "Memory palace: could not parse JSON.";
      return;
    }
    const pid = palace.palaceId || "palace";
    host.className = "palace";
    host.innerHTML = "";
    const head = el("div", "palace-head");
    head.appendChild(el("div", "palace-theme", "🏛 " + (palace.palaceTheme || "Memory palace")));
    if (palace.topic) head.appendChild(el("div", "palace-topic", palace.topic));
    host.appendChild(head);

    const cardById = {};  // locusId → card element (for callback links)
    (palace.rooms || []).forEach(room => {
      const rm = el("div", "palace-room");
      rm.appendChild(el("h4", "palace-room-name", room.roomName || ""));
      if (room.conceptTaught) rm.appendChild(el("div", "palace-concept", room.conceptTaught));
      (room.loci || []).forEach(locus => {
        const card = buildLocus(pid, locus, cardById);
        cardById[locus.locusId] = card;
        rm.appendChild(card);
      });
      host.appendChild(rm);
    });
  }

  function buildLocus(palaceId, locus, cardById) {
    const card = el("div", "palace-locus");
    card.dataset.id = locus.locusId;
    const phrase = el("div", "palace-phrase");
    phrase.innerHTML = `<span class="palace-loc">at</span> ${esc(locus.locusPhrase || "")}`;
    card.appendChild(phrase);
    const ladder = el("div", "palace-ladder");
    card.appendChild(ladder);

    const controls = el("div", "palace-controls");
    const hintBtn = el("button", "btn small ghost", "💡 Hint");
    hintBtn.onclick = () => {
      if (!ladder.querySelector(".palace-hint")) {
        ladder.insertBefore(el("div", "palace-hint", "💡 " + (locus.metaphorAnchor || "")), controls);
      }
      hintBtn.disabled = true;
    };
    const revealBtn = el("button", "btn small g-good", "Reveal answer");
    revealBtn.onclick = () => reveal(palaceId, locus, card, ladder, cardById);
    controls.appendChild(hintBtn);
    controls.appendChild(revealBtn);
    ladder.appendChild(controls);
    return card;
  }

  function reveal(palaceId, locus, card, ladder, cardById) {
    ladder.innerHTML = "";
    const img = el("div", "image-card");
    ladder.appendChild(img);
    renderImage(img, "scene: " + (locus.scene || ""), "loci");
    if (locus.caption) ladder.appendChild(el("div", "palace-caption", locus.caption));
    if (locus.technicalFact) ladder.appendChild(el("div", "palace-fact", "✓ " + locus.technicalFact));
    if (locus.callbackTo && cardById[locus.callbackTo]) {
      const cb = el("span", "palace-cb", "↩ callback");
      cb.onclick = () => {
        const t = cardById[locus.callbackTo];
        t.scrollIntoView({ behavior: "smooth", block: "center" });
        t.classList.add("palace-flash");
        setTimeout(() => t.classList.remove("palace-flash"), 1200);
      };
      ladder.appendChild(cb);
    }
    const grades = el("div", "palace-grades");
    [["again", "Again"], ["hard", "Hard"], ["good", "Good"], ["easy", "Easy"]].forEach(([g, label]) => {
      const cls = g === "again" ? "g-again" : (g === "good" || g === "easy") ? "g-good" : "ghost";
      const b = el("button", "btn small " + cls, label);
      b.onclick = () => gradeLocus(palaceId, locus.locusId, g, grades);
      grades.appendChild(b);
    });
    ladder.appendChild(grades);
  }

  async function gradeLocus(palaceId, locusId, g, grades) {
    grades.querySelectorAll("button").forEach(b => (b.disabled = true));
    const topic = "loci:" + palaceId + "/" + locusId;
    try {
      const r = await fetch("/api/review", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ topic, grade: g }),
      });
      const data = await r.json();
      const badge = el("div", "palace-sched");
      if (data && data.interval_days != null) {
        const d = data.interval_days;
        badge.textContent = "⏱ next review in " + (d >= 1 ? Math.round(d) + " day" + (Math.round(d) === 1 ? "" : "s") : "under a day");
      } else {
        badge.textContent = data && data.error ? "⚠ " + data.error : "✓ recorded";
      }
      grades.replaceWith(badge);
    } catch (e) {
      grades.querySelectorAll("button").forEach(b => (b.disabled = false));
    }
  }

  // Reveal process-shaped diagrams (flow/sequence/state) one node at a time.
  function addStepper(host, code) {
    if (reduceMotion) return;
    const kind = (code.trim().split(/\s+/)[0] || "").toLowerCase();
    if (!/^(flowchart|graph|sequencediagram|statediagram)/.test(kind)) return;
    const svg = host.querySelector("svg"); if (!svg) return;
    const nodes = [...svg.querySelectorAll('.node, g[class*="node"], .messageText, .actor')];
    if (nodes.length < 3) return;

    const optin = el("button", "btn small ghost mmd-optin", "▶ Step through");
    host.appendChild(optin);
    optin.onclick = () => {
      optin.remove();
      let i = 0, timer = null;
      nodes.forEach(n => { n.classList.add("tut-fade", "tut-hidden"); });
      const bar = el("div", "mmd-controls");
      const prog = el("span", "mmd-progress");
      const prev = el("button", "btn small ghost", "‹");
      const next = el("button", "btn small ghost", "›");
      const play = el("button", "btn small ghost", "▶");
      const all = el("button", "btn small ghost", "show all");
      bar.append(prog, prev, next, play, all);
      host.appendChild(bar);
      const paint = () => {
        nodes.forEach((n, k) => { n.classList.toggle("tut-hidden", k > i); n.classList.toggle("tut-step-active", k === i); });
        prog.textContent = `step ${Math.min(i + 1, nodes.length)} / ${nodes.length}`;
      };
      const step = (d) => { i = Math.max(0, Math.min(nodes.length - 1, i + d)); paint(); };
      prev.onclick = () => { stop(); step(-1); };
      next.onclick = () => { stop(); step(1); };
      const stop = () => { if (timer) { clearInterval(timer); timer = null; play.textContent = "▶"; } };
      play.onclick = () => { if (timer) return stop(); play.textContent = "⏸"; timer = setInterval(() => { if (i >= nodes.length - 1) return stop(); step(1); }, 1200); };
      all.onclick = () => { stop(); nodes.forEach(n => n.classList.remove("tut-hidden", "tut-step-active")); bar.remove(); };
      if (webmSupported()) {
        const rec = el("button", "btn small ghost", "⤓ webm");
        rec.title = "Record this walkthrough as a WebM video (labels use text mode)";
        rec.onclick = () => { stop(); recordWalkthrough(svg, nodes, rec); };
        bar.appendChild(rec);
      }
      paint();
    };
  }

  // ── WebM walkthrough recording ──────────────────────────────────────
  // Rasterize the diagram frame-by-frame onto a canvas (progressive reveal via
  // inline opacity so the standalone SVG carries it), capture to WebM.
  function webmMime() {
    if (typeof MediaRecorder === "undefined") return null;
    for (const m of ["video/webm;codecs=vp9", "video/webm;codecs=vp8", "video/webm"]) {
      try { if (MediaRecorder.isTypeSupported(m)) return m; } catch (_) {}
    }
    return null;
  }
  function webmSupported() { const c = document.createElement("canvas"); return !!(c.captureStream && webmMime()); }

  async function recordWalkthrough(svg, nodes, btn) {
    const mime = webmMime();
    const box = (svg.viewBox && svg.viewBox.baseVal && svg.viewBox.baseVal.width)
      ? { w: svg.viewBox.baseVal.width, h: svg.viewBox.baseVal.height } : svg.getBoundingClientRect();
    const scale = 2, W = Math.max(240, Math.round(box.w * scale)), H = Math.max(160, Math.round(box.h * scale));
    const canvas = el("canvas"); canvas.width = W; canvas.height = H;
    const ctx = canvas.getContext("2d");
    if (!ctx || !canvas.captureStream || !mime) { btn.textContent = "⚠ unsupported"; return; }
    const stream = canvas.captureStream(0);
    const track = stream.getVideoTracks()[0];
    let rec; const chunks = [];
    try { rec = new MediaRecorder(stream, { mimeType: mime }); } catch (_) { btn.textContent = "⚠ rec failed"; return; }
    rec.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
    const stopped = new Promise(res => { rec.onstop = res; });
    btn.textContent = "● recording"; btn.disabled = true;
    const bg = (getComputedStyle(document.body).getPropertyValue("--bg-pane") || "#0a0e14").trim();
    const saved = nodes.map(n => n.style.opacity);
    rec.start();
    const frame = async (revealTo) => {
      nodes.forEach((n, k) => { n.style.opacity = k <= revealTo ? "1" : "0.06"; });
      const xml = new XMLSerializer().serializeToString(svg);
      const url = "data:image/svg+xml;base64," + btoa(unescape(encodeURIComponent(xml)));
      await new Promise((res) => {
        const img = new Image();
        img.onload = () => { ctx.fillStyle = bg; ctx.fillRect(0, 0, W, H); ctx.drawImage(img, 0, 0, W, H); res(); };
        img.onerror = () => res();
        img.src = url;
      });
      if (track && track.requestFrame) track.requestFrame(); else if (stream.requestFrame) stream.requestFrame();
      await new Promise(r => setTimeout(r, 650));
    };
    for (let k = 0; k < nodes.length; k++) await frame(k);
    await frame(nodes.length - 1);
    rec.stop(); await stopped;
    nodes.forEach((n, k) => { n.style.opacity = saved[k]; });
    downloadBlob(new Blob(chunks, { type: mime }), "walkthrough.webm");
    btn.textContent = "⤓ webm"; btn.disabled = false;
  }
  function downloadBlob(blob, name) {
    const url = URL.createObjectURL(blob); const a = el("a"); a.href = url; a.download = name; a.click(); URL.revokeObjectURL(url);
  }

  // ══════════════════════════════════════════════════════════════════
  //  Chat rendering
  // ══════════════════════════════════════════════════════════════════
  function scrollDown() { requestAnimationFrame(() => { chatScroll.scrollTop = chatScroll.scrollHeight; }); }
  function clearWelcome() { if (welcomeShown) { messagesEl.innerHTML = ""; welcomeShown = false; } revealQuickReplies(); }
  function revealQuickReplies() { const b = $("quickreply"); if (b) b.classList.remove("hidden"); }

  function addBubble(role, text) {
    clearWelcome();
    const msg = el("div", `message ${role}`);
    msg.appendChild(el("div", "who", role === "operator" ? "you" : "tutor"));
    const bubble = el("div", "bubble");
    const { html, mermaidBlocks } = renderMd(text);
    bubble.innerHTML = html; msg.appendChild(bubble);
    messagesEl.appendChild(msg); scrollDown();
    if (mermaidBlocks.length) renderMermaid(mermaidBlocks);
    return { msg, bubble };
  }

  let typingEl = null;
  function showTyping() {
    clearWelcome();
    typingEl = el("div", "message tutor");
    typingEl.appendChild(el("div", "who", "tutor"));
    typingEl.appendChild(el("div", "bubble", '<div class="typing"><span></span><span></span><span></span></div>'));
    messagesEl.appendChild(typingEl); scrollDown();
  }
  function removeTyping() { if (typingEl) { typingEl.remove(); typingEl = null; } }
  function insertSys(cls, text) {
    clearWelcome();
    // Chip form, matching the tool-call pills: leading glyph (from the text if
    // it starts with one, else per kind) + the message.
    const kind = (cls || "").trim();
    let glyph = kind === "err" ? "⚠" : kind === "tool" ? "🔧" : "";
    let body = String(text == null ? "" : text);
    const m = body.match(/^([^\w\s"'([{]+)\s*([\s\S]*)$/u);
    if (m && m[2]) { glyph = m[1]; body = m[2]; }
    const variant = kind === "err" ? "err" : kind === "tool" ? "" : "sys";
    const line = el("div", "sys-chip-line");
    line.innerHTML = `<span class="sys-chip ${variant}">${glyph ? `<span class="glyph">${esc(glyph)}</span>` : ""}<span>${esc(body)}</span></span>`;
    if (typingEl) messagesEl.insertBefore(line, typingEl); else messagesEl.appendChild(line);
    scrollDown();
  }

  // Tool chip: plain-language label of what the tool did + mono name badge
  // (design-system SysLine — cryptic call syntax stays out of the transcript).
  const TOOL_LABELS = {
    kg_query: "Searched the knowledge base",
    kg_semantic_query: "Searched the knowledge base",
    kg_neighbors: "Walked the prerequisite graph",
    kg_stats: "Checked the knowledge base",
    record_review: "Logged your drill result",
    read_evidence: "Read the source evidence",
    ask_agent: "Asked a partner agent",
    ask_partner: "Asked a partner agent",
    ask_consensus: "Convened the panel",
    ask_operator: "Asked for your take",
    prior_actions: "Recalled prior work",
    prior_techniques: "Recalled prior techniques",
    propose_lesson: "Saved a reusable lesson",
    propose_skill: "Saved a reusable skill",
    study_extract: "Read your study document",
    Read: "Read a file",
  };
  function insertToolChip(name) {
    clearWelcome();
    const bare = name.split(".").pop();
    const label = TOOL_LABELS[bare] || (bare.startsWith("context_") ? "Updated shared notes" : "Called a tool");
    const line = el("div", "sys-chip-line");
    line.innerHTML = `<span class="sys-chip"><span class="glyph">🔧</span><span>${esc(label)}</span><span class="tool-name">${esc(name)}</span></span>`;
    if (typingEl) messagesEl.insertBefore(line, typingEl); else messagesEl.appendChild(line);
    scrollDown();
  }

  function startPending() {
    removeTyping();
    const { msg, bubble } = addBubble("tutor", "");
    pending = { msg, bubble, raw: "" };
  }
  function appendPending(chunk) {
    if (!pending) startPending();
    pending.raw += (pending.raw ? "\n\n" : "") + chunk;
    const { html } = renderMd(pending.raw);
    pending.bubble.innerHTML = html;
    scrollDown();
  }
  function finalizePending(fallback, hinted, awaiting) {
    removeTyping();
    if (!pending && fallback) startPending();
    if (!pending) return;
    if (!pending.raw && fallback) pending.raw = fallback;
    // Export capture: the reply IS the lesson file — download instead of showing.
    if (captureMode === "exporting") {
      const p = pending; pending = null;
      p.msg.remove();
      downloadText(p.raw || fallback || "", "lesson.md", "text/markdown");
      addExportCard("Lesson exported → lesson.md");
      setCapture("normal");
      return;
    }
    const { html, mermaidBlocks } = renderMd(pending.raw);
    pending.bubble.innerHTML = html;
    if (mermaidBlocks.length) renderMermaid(mermaidBlocks);
    attachTts(pending.bubble, pending.raw);
    attachSecondOpinion(pending.msg, lastQuestion);
    if (awaiting === "attempt") {
      pending.msg.appendChild(el("span", "attempt-chip", "↩ your turn — attempt first"));
      inputEl.placeholder = "your best attempt…";
    } else if (hinted) {
      pending.msg.appendChild(el("span", "hinted-chip", "↩ hinted (judge)"));
    }
    pending = null;
    scrollDown();
  }
  function addExportCard(text) {
    const card = el("div", "export-card", "⤓ " + esc(text));
    messagesEl.appendChild(card); scrollDown();
  }

  // ══════════════════════════════════════════════════════════════════
  //  WebSocket streaming
  // ══════════════════════════════════════════════════════════════════
  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws/tutor`);
    ws.onopen = () => {
      wsReady = true;
      // Flush a turn the operator submitted while the socket was still opening
      // (e.g. hitting send right after page load) instead of having dropped it.
      if (pendingSend != null && !busy) { const m = pendingSend; pendingSend = null; send(m); }
      else if (!busy) setStatus("idle");
    };
    ws.onmessage = (m) => { try { onEvent(JSON.parse(m.data)); } catch (_) {} };
    ws.onclose = () => { wsReady = false; setStatus("offline"); setTimeout(connect, 2000); };
    ws.onerror = () => { try { ws.close(); } catch (_) {} };
  }

  function onEvent(evt) {
    const kind = evt.kind;
    if (kind === "thinking") { setStatus("thinking"); return; }
    if (kind === "phase") { setStatus(evt.text || "working"); return; }
    if (kind === "tool-call") {
      const name = (evt.meta && evt.meta.tool_call && evt.meta.tool_call.name) || String(evt.text || "").split("(")[0].trim().split(/\s+/)[0];
      if (name) insertToolChip(name); else insertSys("tool", "🔧 " + (evt.text || "tool"));
      setStatus("working"); return;
    }
    if (kind === "tool-error") { insertSys("err", "⚠ " + (evt.text || "tool error")); return; }
    if (kind === "tool-result" || kind === "user_message" || kind === "operator_answer") return;
    if (kind === "text") { setStatus("busy"); appendPending(evt.text || ""); return; }
    if (kind === "system") { if (evt.text) insertSys("", evt.text); return; }
    if (kind === "error" || kind === "refusal") { removeTyping(); insertSys("err", evt.text || "error"); finishTurn("error"); return; }
    if (kind === "done") { finalizePending(evt.text || "", evt.hinted, evt.awaiting); finishTurn("idle"); return; }
  }

  function finishTurn(status) {
    busy = false;
    setStatus(status === "error" ? "error" : "idle");
    setSendEnabled(true);
    inputEl.focus();
    if (status !== "error") { loadSkillmap(); loadContext(); loadRetention(); }
  }

  function send(text) {
    const msg = (text != null ? text : inputEl.value).trim();
    if (!msg || busy) return;
    if (!wsReady) {
      // Socket still handshaking (send right after load) or mid-reconnect: queue
      // the turn and flush it on onopen, instead of dropping it with an error.
      pendingSend = msg;
      if (text == null) { inputEl.value = ""; inputEl.style.height = "auto"; }
      setStatus("connecting");
      return;
    }
    // Show the operator's turn unless it's a machine sentinel.
    const isSentinel = msg.startsWith(EXPORT_TOKEN) || msg.startsWith(FIX_TOKEN) || msg.startsWith(STUDY_TOKEN) || msg.startsWith(DRILL_TOKEN);
    if (!isSentinel) { addBubble("operator", msg); lastQuestion = msg; }
    inputEl.placeholder = DEFAULT_PLACEHOLDER;  // clear any "your best attempt…" prompt
    if (text == null) { inputEl.value = ""; inputEl.style.height = "auto"; }
    busy = true; setSendEnabled(false); setStatus("thinking"); showTyping();
    // A manual "🎨 illustrate" turn forces a model for this turn only, even if the
    // dial is on "No art"; otherwise honor the dial.
    const turnImageModel = forceImageModel || (imageModel === "off" ? null : imageModel);
    forceImageModel = null;  // one-shot
    ws.send(JSON.stringify({ cmd: "prompt", message: msg, agent: currentAgent, strictness, diagram_engine: diagramEngine, image_model: turnImageModel }));
  }

  function setStatus(s) { const e = $("status"); e.className = "status-pill " + s; e.querySelector(".status-text").textContent = s; }
  function setSendEnabled(on) {
    $("send-btn").disabled = !on; $("drill-btn").disabled = !on; $("export-btn").disabled = !on;
    document.querySelectorAll(".quickreply-btn").forEach(b => { b.disabled = !on; });
  }

  // ── Export via blocking capture (keeps the file out of the transcript) ──
  function setCapture(mode) {
    captureMode = mode;
    const banner = $("capture-banner");
    if (mode === "normal") banner.classList.add("hidden");
    else { banner.classList.remove("hidden"); $("capture-label").textContent = "Waiting for lesson export…"; }
  }
  $("capture-cancel").addEventListener("click", () => setCapture("normal"));

  function downloadText(text, name, type) {
    const url = URL.createObjectURL(new Blob([text], { type }));
    const a = el("a"); a.href = url; a.download = name; a.click(); URL.revokeObjectURL(url);
  }

  // ── compose wiring ──
  $("send-btn").addEventListener("click", () => send());
  inputEl.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
  inputEl.addEventListener("input", () => { inputEl.style.height = "auto"; inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + "px"; });
  $("export-btn").addEventListener("click", () => { if (busy) return; setCapture("exporting"); send(EXPORT_TOKEN); });
  $("drill-btn").addEventListener("click", () => { if (busy) return; send(DRILL_TOKEN); });
  // Manual illustration request: forces a diffusion image of the current concept.
  // Works even when the dial is on "No art" by using the default model this turn.
  $("illustrate-btn").addEventListener("click", () => {
    if (busy) return;
    const model = imageModel !== "off" ? imageModel : imageDefault;
    if (!model) { insertSys("err", "No illustration model available on this server."); return; }
    forceImageModel = model;
    send("Illustrate the current concept with a single mnemonic ```image (memory-palace scene or visual metaphor) — no diagram.");
  });
  // data-p quick replies (skip the illustrate button, which has its own handler).
  document.querySelectorAll(".quickreply-btn[data-p]").forEach(b =>
    b.addEventListener("click", () => { if (busy) return; send(b.dataset.p); }));

  // ── strictness dial (shown only when a judge is configured) ──
  function paintStrictness() {
    document.querySelectorAll("#strictness .strictness-btn")
      .forEach(b => b.classList.toggle("active", b.dataset.level === strictness));
  }
  async function initStrictness() {
    let cfg = {};
    try { cfg = await (await fetch("/api/config")).json(); } catch (_) {}
    initDiagramEngine(cfg.diagram_engines || {});
    initImageModel(cfg.images || {});
    if (!cfg.judge) return;  // no judge → live streaming, dial stays hidden
    const wrap = $("strictness");
    wrap.classList.remove("hidden");
    wrap.querySelectorAll(".strictness-btn").forEach(b => b.addEventListener("click", () => {
      strictness = b.dataset.level;
      try { localStorage.setItem("tutor.strictness", strictness); } catch (_) {}
      paintStrictness();
    }));
    paintStrictness();
  }

  // ── diagram-engine dial ──
  // Auto (tutor picks per semantics) + one button per engine the server can
  // render. An unavailable engine (binary missing / plantuml disabled) is shown
  // disabled rather than hidden, so the capability is discoverable. If a
  // previously-persisted engine is no longer available, fall back to Auto.
  function paintDiagramEngine() {
    document.querySelectorAll("#diagram-engine .diagram-btn")
      .forEach(b => b.classList.toggle("active", b.dataset.engine === diagramEngine));
  }
  function initDiagramEngine(available) {
    const wrap = $("diagram-engine");
    if (!wrap) return;
    if (diagramEngine !== "auto" && diagramEngine !== "mermaid" && available[diagramEngine] === false) {
      diagramEngine = "auto";
      try { localStorage.setItem("tutor.diagramEngine", "auto"); } catch (_) {}
    }
    wrap.querySelectorAll(".diagram-btn").forEach(b => {
      const eng = b.dataset.engine;
      const usable = eng === "auto" || available[eng] === true;
      b.disabled = !usable;
      if (!usable) b.title = `${eng} not available on this server`;
      b.addEventListener("click", () => {
        if (b.disabled) return;
        diagramEngine = eng;
        try { localStorage.setItem("tutor.diagramEngine", diagramEngine); } catch (_) {}
        paintDiagramEngine();
      });
    });
    wrap.classList.remove("hidden");
    paintDiagramEngine();
  }

  // ── illustration model dial (shown only when the server can render images) ──
  // Off (default → normal diagramming) + one button per model the box offers.
  // Buttons are built from the server's advertised model list so the UI can't
  // offer a model that isn't installed.
  function paintImageModel() {
    document.querySelectorAll("#image-model .image-btn")
      .forEach(b => b.classList.toggle("active", b.dataset.model === imageModel));
  }
  function initImageModel(images) {
    const wrap = $("image-model");
    if (!wrap) return;
    if (!images.available || !(images.models || []).length) return;  // stays hidden
    const models = images.models;
    if (imageModel !== "off" && !models.includes(imageModel)) imageModel = "off";
    // Enable the manual "🎨 illustrate" quick reply now that the box can render.
    imageAvailable = true;
    imageDefault = images.default && models.includes(images.default) ? images.default : models[0];
    const illus = $("illustrate-btn");
    if (illus) illus.classList.remove("hidden");
    const labels = {
      "flux-schnell": "Flux fast", "flux-dev": "Flux HQ", "qwen": "Qwen",
      "minimax-image": "MiniMax", "glm-image": "GLM",
    };
    // The server tells us which models are cloud; the dial marks those with a
    // small cloud glyph (CSS ::after). Absence of the glyph = local (on the box).
    const cloud = new Set(images.cloud || []);
    const tierOf = m => (cloud.has(m) ? "cloud" : "local");
    wrap.innerHTML = `<button class="strictness-btn image-btn" data-model="off" data-tier="local" title="No illustrations — diagrams only">◇ No art</button>` +
      models.map(m => `<button class="strictness-btn image-btn" data-model="${m}" data-tier="${tierOf(m)}" title="${m} — ${tierOf(m)}">${labels[m] || m}</button>`).join("");
    wrap.querySelectorAll(".image-btn").forEach(b => b.addEventListener("click", () => {
      imageModel = b.dataset.model;
      try { localStorage.setItem("tutor.imageModel", imageModel); } catch (_) {}
      paintImageModel();
    }));
    wrap.classList.remove("hidden");
    paintImageModel();
  }

  // ══════════════════════════════════════════════════════════════════
  //  TTS (read-aloud) — only when the backend has a MiniMax key
  // ══════════════════════════════════════════════════════════════════
  let ttsAvailable = false, ttsVoice = null, ttsModels = [];
  const player = new Audio();
  let playingBtn = null;
  const TTS_FORMATS = ["mp3", "wav", "pcm", "flac"];
  const TTS_RATES = [8000, 16000, 22050, 24000, 32000, 44100];
  const TTS_BITRATES = [32000, 64000, 128000, 256000];
  const ttsPrefs = loadTtsPrefs();

  function loadTtsPrefs() {
    let p = {};
    try { p = JSON.parse(localStorage.getItem("tutor.tts") || "{}"); } catch (_) {}
    return Object.assign({ model: "", pitch: 0, vol: 1, format: "mp3", sample_rate: 32000, bitrate: 128000, raw: false }, p);
  }
  function saveTtsPrefs() { try { localStorage.setItem("tutor.tts", JSON.stringify(ttsPrefs)); } catch (_) {} }

  async function initTts() {
    try {
      const data = await (await fetch("/api/tts/voices")).json();
      if (!data.available) return;
      ttsAvailable = true;
      ttsVoice = data.defaults.voice;
      ttsModels = data.models || [];
      if (!ttsPrefs.model) ttsPrefs.model = data.defaults.model;
      const sel = $("tts-voice");
      sel.innerHTML = data.voices.map(v => `<option value="${v.id}">${esc(v.label)}</option>`).join("");
      sel.value = ttsVoice;
      sel.addEventListener("change", () => { ttsVoice = sel.value; });
      buildTtsPopover();
      $("tts-audio-controls").classList.remove("hidden");
      $("tts-bar").classList.remove("hidden");
      reattachTtsAll();
    } catch (_) {}
  }

  function buildTtsPopover() {
    const pop = $("tts-popover");
    const opt = (arr, cur) => arr.map(v => `<option value="${v}" ${String(v) === String(cur) ? "selected" : ""}>${v}</option>`).join("");
    pop.innerHTML = `
      <div class="pop-row"><span>model</span><select data-k="model" class="tts-select">${opt(ttsModels, ttsPrefs.model)}</select></div>
      <div class="pop-row"><span>pitch</span><input data-k="pitch" type="range" min="-12" max="12" step="1" value="${ttsPrefs.pitch}"><b class="pop-val">${ttsPrefs.pitch}</b></div>
      <div class="pop-row"><span>volume</span><input data-k="vol" type="range" min="0.1" max="2" step="0.1" value="${ttsPrefs.vol}"><b class="pop-val">${ttsPrefs.vol}</b></div>
      <div class="pop-row"><span>format</span><select data-k="format" class="tts-select">${opt(TTS_FORMATS, ttsPrefs.format)}</select></div>
      <div class="pop-row"><span>sample rate</span><select data-k="sample_rate" class="tts-select">${opt(TTS_RATES, ttsPrefs.sample_rate)}</select></div>
      <div class="pop-row"><span>bitrate</span><select data-k="bitrate" class="tts-select">${opt(TTS_BITRATES, ttsPrefs.bitrate)}</select></div>
      <label class="pop-row pop-check"><input data-k="raw" type="checkbox" ${ttsPrefs.raw ? "checked" : ""}> read raw (keep markdown)</label>
      <div class="pop-foot"><button class="btn small ghost" data-reset>reset defaults</button></div>`;
    pop.querySelectorAll("[data-k]").forEach(inp => {
      const k = inp.dataset.k;
      inp.addEventListener("input", () => {
        let v = inp.type === "checkbox" ? inp.checked : (inp.type === "range" ? parseFloat(inp.value) : inp.value);
        if (inp.tagName === "SELECT" && /rate|bitrate/.test(k)) v = parseInt(inp.value);
        ttsPrefs[k] = v; saveTtsPrefs();
        const b = inp.parentElement.querySelector(".pop-val"); if (b) b.textContent = v;
      });
    });
    pop.querySelector("[data-reset]").onclick = () => { localStorage.removeItem("tutor.tts"); Object.assign(ttsPrefs, loadTtsPrefs()); if (!ttsPrefs.model) ttsPrefs.model = ttsModels[0] || ""; buildTtsPopover(); pop.classList.remove("hidden"); };
    $("tts-settings").onclick = (e) => { e.stopPropagation(); pop.classList.toggle("hidden"); };
    document.addEventListener("click", (e) => { if (!pop.contains(e.target) && e.target !== $("tts-settings")) pop.classList.add("hidden"); });
  }

  function attachTts(bubble, rawText) {
    bubble._ttsRaw = rawText;
    if (!ttsAvailable || !rawText.trim()) return;
    if (bubble.parentElement.querySelector(".tts-btn")) return;
    const btn = el("button", "tts-btn", "🔊 read aloud");
    btn.onclick = async () => {
      if (playingBtn === btn) { player.pause(); return; }
      if (playingBtn) { player.pause(); playingBtn.classList.remove("playing"); playingBtn.textContent = "🔊 read aloud"; }
      btn.textContent = "… loading"; btn.classList.add("playing"); playingBtn = btn;
      const text = ttsPrefs.raw ? rawText : rawText.replace(/```[\s\S]*?```/g, " ").replace(/[#*`>_]/g, "");
      try {
        const resp = await fetch("/api/tts", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: text.trim().slice(0, 4000), voice: ttsVoice, speed: parseFloat($("tts-speed").value) || 1,
            model: ttsPrefs.model, pitch: ttsPrefs.pitch, vol: ttsPrefs.vol, format: ttsPrefs.format, sample_rate: ttsPrefs.sample_rate, bitrate: ttsPrefs.bitrate }) });
        if (!resp.ok) throw new Error("tts " + resp.status);
        player.src = URL.createObjectURL(await resp.blob());
        await player.play();
        btn.textContent = "⏸ playing";
      } catch (e) { btn.textContent = "⚠ tts failed"; btn.classList.remove("playing"); playingBtn = null; }
    };
    bubble.parentElement.appendChild(btn);
  }
  player.onended = player.onpause = () => { if (playingBtn) { playingBtn.classList.remove("playing"); playingBtn.textContent = "🔊 read aloud"; playingBtn = null; } };

  function reattachTtsAll() {
    if (!ttsAvailable) return;
    messagesEl.querySelectorAll(".message.tutor .bubble").forEach((b) => {
      if (b._ttsRaw && !b.parentElement.querySelector(".tts-btn")) attachTts(b, b._ttsRaw);
    });
  }

  // ── second opinion (consensus panel: tutor vs tutor_alt via ask_consensus) ──
  function attachSecondOpinion(msg, question) {
    if (!panelReady || !question || captureMode !== "normal") return;
    const btn = el("button", "secopinion-btn", "⚖ second opinion");
    btn.onclick = async () => {
      btn.disabled = true; btn.textContent = "… consulting panel";
      try {
        const resp = await fetch("/api/second_opinion", { method: "POST",
          headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question }) });
        const p = await resp.json();
        if (!p.ok && p.error) throw new Error(p.error);
        renderConsensusCard(msg, p);
        btn.remove();
      } catch (e) {
        insertSys("err", "second opinion failed: " + (e.message || e));
        btn.disabled = false; btn.textContent = "⚖ second opinion";
      }
    };
    msg.appendChild(btn);
  }

  function renderConsensusCard(afterMsg, p) {
    const card = el("div", "consensus-card");
    const chips = [`<span class="score-chip">agreement ${Math.round((p.agreement_score || 0) * 100)}%</span>`];
    if (p.semantic_score != null) chips.push(`<span class="score-chip">semantic ${Math.round(p.semantic_score * 100)}%</span>`);
    card.appendChild(el("div", "consensus-head",
      "⚖ Panel: " + esc((p.panel || []).join(" vs ")) + " " + chips.join(" ")));

    const cols = el("div", "panel-cols");
    const mermaidAll = [];
    (p.per_agent || []).forEach((r) => {
      const col = el("div", "panel-col");
      col.appendChild(el("div", "panel-col-name", esc(r.name)));
      const body = el("div", "panel-col-body");
      if (r.ok) {
        const { html, mermaidBlocks } = renderMd(r.result || "");
        body.innerHTML = html; mermaidAll.push(...mermaidBlocks);
      } else {
        body.innerHTML = "⚠ " + esc(r.error || "no reply");
      }
      col.appendChild(body); cols.appendChild(col);
    });
    card.appendChild(cols);

    const atomList = (obj, flat) => Object.entries(obj || {})
      .flatMap(([kind, v]) => (flat ? v : Object.keys(v)).map((a) => `${kind}: ${a}`));
    const agreed = atomList(p.corroborated, true);
    const single = atomList(p.divergent, false);
    if (agreed.length) card.appendChild(el("div", "consensus-atoms", "✓ both agree — " + esc(agreed.join(", "))));
    if (single.length) card.appendChild(el("div", "consensus-atoms divergent", "△ single-source — " + esc(single.join(", "))));

    if (p.judge) {
      const jb = el("div", "judge-block");
      jb.appendChild(el("div", "judge-head", "⚖ Judge verdict"));
      const jbody = el("div", "judge-body");
      const { html, mermaidBlocks } = renderMd(p.judge);
      jbody.innerHTML = html; mermaidAll.push(...mermaidBlocks);
      jb.appendChild(jbody); card.appendChild(jb);
    }
    if (p.warnings && p.warnings.length) card.appendChild(el("div", "consensus-warn", esc(p.warnings.join(" · "))));

    afterMsg.insertAdjacentElement("afterend", card);
    if (mermaidAll.length) renderMermaid(mermaidAll);
    scrollDown();
  }

  // ── tutor variant picker (shows only when a TUTOR_VARIANT_MODEL shadow exists) ──
  async function initTutors() {
    try {
      const data = await (await fetch("/api/tutors")).json();
      const list = data.tutors || [];
      panelReady = list.filter((t) => t.running).length >= 2;
      if (list.length < 2) return;
      const sel = $("tts-variant");
      sel.innerHTML = list.map(t => `<option value="${t.name}">${esc(t.label)}</option>`).join("");
      sel.value = currentAgent;
      sel.addEventListener("change", () => {
        currentAgent = sel.value;
        if (wsReady) ws.send(JSON.stringify({ cmd: "select", agent: currentAgent }));
        insertSys("", "↔ switched to " + sel.options[sel.selectedIndex].text);
      });
      $("tts-variant-wrap").classList.remove("hidden");
      $("tts-bar").classList.remove("hidden");
    } catch (_) {}
  }

  // ══════════════════════════════════════════════════════════════════
  //  Rail — skill-map
  // ══════════════════════════════════════════════════════════════════
  const now = () => Date.now() / 1000;
  function recallOdds(e) {
    if (!e.review_due || !e.ts) return null;
    const interval = e.review_due - e.ts;
    if (interval <= 0) return null;
    const r = Math.pow(0.9, (now() - e.ts) / interval);
    return Math.max(0, Math.min(1, r));
  }
  function fmtDue(due) {
    if (!due) return "";
    const d = Math.round((due - now()) / 86400);
    if (d < 0) return { t: `${-d}d ago`, c: "overdue" };
    if (d === 0) return { t: "today", c: "today" };
    return { t: `in ${d}d`, c: "" };
  }

  async function loadSkillmap() {
    const mount = $("skillmap");
    try {
      const p = await (await fetch("/api/learner/profile")).json();
      if (p.error) { mount.innerHTML = `<div class="rail-empty">${esc(p.error)}</div>`; return; }
      const all = [...(p.strong || []), ...(p.weak || [])];
      const total = (p.counts && (p.counts.strong + p.counts.weak + p.counts.misconceptions)) || all.length + (p.misconceptions || []).length;
      if (!total) { mount.innerHTML = '<div class="rail-empty">No drill data yet. Start a lesson to build your mastery profile.</div>'; return; }
      let overdue = 0, today = 0, soon = 0;
      all.forEach(e => { if (!e.review_due) return; const d = (e.review_due - now()) / 86400; if (d < 0) overdue++; else if (d < 1) today++; else if (d < 7) soon++; });
      let html = `<div class="sm-forecast">
        <div class="sm-chip overdue"><span class="n">${overdue}</span><span class="l">overdue</span></div>
        <div class="sm-chip today"><span class="n">${today}</span><span class="l">today</span></div>
        <div class="sm-chip soon"><span class="n">${soon}</span><span class="l">7 days</span></div></div>`;

      html += section("Due for review", p.due || [], true, "", true);
      html += section("Strong", (p.strong || []).slice(0, 8), false, "strong");
      html += section("Needs work", (p.weak || []).slice(0, 8), true, "weak");
      if ((p.misconceptions || []).length) {
        html += `<div class="sm-section"><div class="sm-section-hd">Misconceptions</div>` +
          p.misconceptions.slice(0, 6).map(m => `<div class="sm-mis">✗ ${esc(m.topic)}</div>`).join("") + `</div>`;
      }
      if (!(p.due || []).length && !(p.weak || []).length) html += `<div class="sm-caught-up">✓ All caught up</div>`;
      mount.innerHTML = html;
      mount.querySelectorAll(".sm-row.drillable").forEach(row => row.addEventListener("click", () => {
        document.querySelector('[data-tab="coach"]').click();
        inputEl.value = "Drill me on: " + row.dataset.topic; inputEl.focus();
      }));
      mount.querySelectorAll(".rail-quiz-btn").forEach(btn => btn.addEventListener("click", (ev) => {
        ev.stopPropagation();  // don't also trigger the row's drill-prefill
        startQuiz(btn.dataset.quiz);
      }));
    } catch (e) { mount.innerHTML = `<div class="rail-empty">Error: ${esc(e.message)}</div>`; }
  }

  function section(title, items, drillable, fillClass, quizzable) {
    if (!items.length) return "";
    const rows = items.map(e => {
      const r = recallOdds(e), due = fmtDue(e.review_due);
      const pct = Math.round((e.mastery || 0) * 100);
      const rBadge = r != null ? `<span class="sm-r">R${Math.round(r * 100)}</span>` : "";
      const dueBadge = due ? `<span class="sm-r sm-due ${due.c}">${due.t}</span>` : "";
      const quizBtn = quizzable ? `<button class="rail-quiz-btn" data-quiz="${esc(e.topic)}" title="Retrieval quiz">quiz</button>` : "";
      return `<div class="sm-row ${drillable ? "drillable" : ""}" data-topic="${esc(e.topic)}">
        <span class="sm-topic" title="${esc(e.topic)}">${esc(e.topic)}</span>
        <span class="sm-bar"><span class="sm-fill ${fillClass || ""}" style="width:${pct}%"></span></span>
        ${dueBadge || rBadge}${quizBtn}</div>`;
    }).join("");
    return `<div class="sm-section"><div class="sm-section-hd">${title}</div>${rows}</div>`;
  }
  $("progress-refresh").addEventListener("click", loadSkillmap);

  // ── Retrieval micro-quiz (structured card on a due tile) ──
  async function startQuiz(topic) {
    document.querySelector('[data-tab="coach"]').click();
    clearWelcome();
    const card = el("div", "quiz-card");
    card.innerHTML = `<div class="quiz-hd">⚡ Retrieval quiz — ${esc(topic)}</div>` +
      `<div class="quiz-body"><div class="rail-empty">· generating question…</div></div>`;
    messagesEl.appendChild(card); scrollDown();
    let data;
    try {
      data = await (await fetch("/api/quiz", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ topic }) })).json();
    } catch (e) { data = { error: e.message }; }
    if (!data || data.error || !data.question) {
      card.querySelector(".quiz-body").innerHTML = `<div class="sys-line err">⚠ ${esc((data && data.error) || "quiz failed")}</div>`;
      return;
    }
    renderQuizQuestion(card, topic, data.question, data.answer || "");
  }

  function renderQuizQuestion(card, topic, question, answer) {
    const body = card.querySelector(".quiz-body");
    const { html, mermaidBlocks } = renderMd(question);
    body.innerHTML = `<div class="quiz-q">${html}</div>` +
      `<textarea class="quiz-answer" rows="3" placeholder="your answer, from memory…"></textarea>` +
      `<div class="quiz-actions"><button class="btn small primary quiz-submit">submit answer</button></div>`;
    if (mermaidBlocks.length) renderMermaid(mermaidBlocks);
    const ta = body.querySelector(".quiz-answer"); ta.focus();
    body.querySelector(".quiz-submit").addEventListener("click",
      () => submitQuiz(card, topic, question, answer, ta.value));
    scrollDown();
  }

  async function submitQuiz(card, topic, question, answer, learnerAnswer) {
    const body = card.querySelector(".quiz-body");
    body.innerHTML = `<div class="quiz-your"><span class="quiz-lbl">your answer</span>${esc(learnerAnswer || "(no answer)")}</div>` +
      `<div class="rail-empty">· grading…</div>`;
    scrollDown();
    let res;
    try {
      res = await (await fetch("/api/quiz/grade", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ topic, question, answer, learner_answer: learnerAnswer }) })).json();
    } catch (e) { res = { error: e.message }; }
    renderQuizResult(card, answer, learnerAnswer, res);
  }

  function renderQuizResult(card, answer, learnerAnswer, res) {
    const body = card.querySelector(".quiz-body");
    if (!res || res.error) {
      body.insertAdjacentHTML("beforeend", `<div class="sys-line err">⚠ ${esc((res && res.error) || "grading failed")}</div>`);
      return;
    }
    const grade = res.grade;
    const badge = grade ? `<span class="grade-badge grade-${grade}">${esc(grade)}</span>`
                        : `<span class="grade-badge">ungraded</span>`;
    const fb = renderMd(res.feedback || "");
    const next = res.interval_days != null
      ? `<div class="quiz-next">↻ next review in ~${Math.round(res.interval_days)}d</div>` : "";
    body.innerHTML = `<div class="quiz-your"><span class="quiz-lbl">your answer</span>${esc(learnerAnswer || "(no answer)")}</div>` +
      `<div class="quiz-grade">${badge}<div class="quiz-fb">${fb.html}</div></div>${next}` +
      `<details class="quiz-ref"><summary>reference answer</summary><div class="quiz-ref-body"></div></details>`;
    const ra = renderMd(answer || "");
    body.querySelector(".quiz-ref-body").innerHTML = ra.html;
    if (fb.mermaidBlocks.length) renderMermaid(fb.mermaidBlocks);
    if (ra.mermaidBlocks.length) renderMermaid(ra.mermaidBlocks);
    loadSkillmap();  // review recorded → rail re-buckets the topic
    loadRetention(); // …and the retention telemetry updates
    scrollDown();
  }

  // ── Prerequisite-DAG skill map (Mermaid card) ──
  const STATUS_STYLE = {
    mastered:      { fill: "#1f6f3d", stroke: "#3fb950", color: "#fff" },
    learning:      { fill: "#7a5a04", stroke: "#db9a04", color: "#fff" },
    due:           { fill: "#1f4b73", stroke: "#58a6ff", color: "#fff" },
    misconception: { fill: "#6e2222", stroke: "#f85149", color: "#fff" },
    available:     { fill: "#21262d", stroke: "#8b949e", color: "#c9d1d9" },
    locked:        { fill: "#161b22", stroke: "#30363d", color: "#6e7681" },
  };

  function buildSkillMermaid(nodes, edges) {
    const idOf = new Map();
    nodes.forEach((n, i) => idOf.set(n.id, "n" + i));
    const safe = (s) => String(s).replace(/["`\[\]\n]/g, " ").trim() || "?";
    const lines = ["graph TD"];
    nodes.forEach(n => lines.push(`  ${idOf.get(n.id)}["${safe(n.label)}"]`));
    (edges || []).forEach(([a, b]) => {
      if (idOf.has(a) && idOf.has(b)) lines.push(`  ${idOf.get(a)} --> ${idOf.get(b)}`);
    });
    Object.entries(STATUS_STYLE).forEach(([st, s]) =>
      lines.push(`  classDef ${st} fill:${s.fill},stroke:${s.stroke},color:${s.color};`));
    const byStatus = {};
    nodes.forEach(n => { (byStatus[n.status] = byStatus[n.status] || []).push(idOf.get(n.id)); });
    Object.entries(byStatus).forEach(([st, ids]) => {
      if (STATUS_STYLE[st]) lines.push(`  class ${ids.join(",")} ${st}`);
    });
    return lines.join("\n");
  }

  function skillLegend(counts) {
    counts = counts || {};
    const items = Object.entries(STATUS_STYLE)
      .filter(([st]) => counts[st])
      .map(([st, s]) =>
        `<span class="legend-item"><span class="legend-swatch" style="background:${s.fill};border-color:${s.stroke}"></span>${st} ${counts[st]}</span>`)
      .join("");
    return items ? `<div class="skillmap-legend">${items}</div>` : "";
  }

  async function openSkillGraph(rebuild) {
    document.querySelector('[data-tab="coach"]').click();
    clearWelcome();
    const card = el("div", "skillmap-card");
    card.innerHTML = `<div class="skillmap-hd">🗺 Prerequisite map</div>` +
      `<div class="skillmap-body"><div class="rail-empty">· building prerequisite map…</div></div>`;
    messagesEl.appendChild(card); scrollDown();
    const body = card.querySelector(".skillmap-body");
    let data;
    try {
      data = await (await fetch(rebuild ? "/api/skillmap/graph/rebuild" : "/api/skillmap/graph",
        rebuild ? { method: "POST" } : {})).json();
    } catch (e) { data = { error: e.message }; }
    if (!data || data.error) {
      body.innerHTML = `<div class="sys-line err">⚠ ${esc((data && data.error) || "map failed")}</div>`;
      return;
    }
    if (!data.nodes || !data.nodes.length) {
      body.innerHTML = `<div class="rail-empty">${esc(data.note || "no topics yet")}</div>`;
      return;
    }
    const code = buildSkillMermaid(data.nodes, data.edges);
    const id = "smm-" + Math.random().toString(36).slice(2, 9);
    body.innerHTML = `<div class="mermaid" id="${id}"></div>` + skillLegend(data.counts) +
      `<button class="btn small ghost skillmap-rebuild">↻ rebuild</button>`;
    renderMermaid([{ id, code }]);
    body.querySelector(".skillmap-rebuild").addEventListener("click", () => openSkillGraph(true));
    scrollDown();
  }
  $("skillmap-graph-btn").addEventListener("click", () => openSkillGraph(false));

  // ── Rail — knowledge base search ──
  async function kbSearch() {
    const q = $("kb-search").value.trim(); const box = $("kb-results");
    if (!q) { box.innerHTML = '<div class="rail-empty">Search the knowledge base.</div>'; return; }
    box.innerHTML = '<div class="rail-empty">searching…</div>';
    try {
      const data = await (await fetch("/api/kg/search?q=" + encodeURIComponent(q))).json();
      const rows = data.results || [];
      if (!rows.length) { box.innerHTML = '<div class="rail-empty">No matching facts.</div>'; return; }
      box.innerHTML = "";
      rows.slice(0, 60).forEach(r => {
        const row = el("button", "kb-row",
          `<span class="kb-subject">${esc(r.subject)}</span><span class="kb-predicate">${esc(r.predicate)}</span><span class="kb-object">${esc(r.object)}</span>`);
        row.onclick = () => { document.querySelector('[data-tab="coach"]').click(); inputEl.value = `Teach me about "${r.subject} ${r.predicate} ${r.object}" from our knowledge base.`; inputEl.focus(); };
        box.appendChild(row);
      });
    } catch (e) { box.innerHTML = `<div class="rail-empty">Error: ${esc(e.message)}</div>`; }
  }
  $("kb-go").addEventListener("click", kbSearch);
  $("kb-search").addEventListener("keydown", e => { if (e.key === "Enter") kbSearch(); });

  // ── Rail — context bar ──
  // Renders the SDK's rich context-usage breakdown: a STACKED segment bar where
  // each segment is a category (system prompt / tools / messages / …) in the
  // SDK's own color, capped at the autocompact threshold, with a legend + model.
  // Falls back to a flat bar when categories are unavailable, and to a dash when
  // no usage has been reported yet.
  const CAT_COLORS = {  // fallback colors when the SDK omits one
    "system prompt": "#a371f7", tools: "#f0883e", messages: "#58a6ff",
    memory: "#3fb950", "mcp servers": "#d2a8ff", agents: "#f778ba",
  };
  function fmtTok(n) { n = +n || 0; return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n); }
  async function loadContext() {
    const mount = $("ctx");
    try {
      const u = await (await fetch("/api/context/usage")).json();
      if (u.error || (!u.totalTokens && !u.used && !u.used_tokens)) {
        mount.innerHTML = '<div class="rail-empty">—</div>'; return;
      }
      // The SDK reports camelCase fields; tolerate older shapes too.
      const total = u.totalTokens ?? u.used ?? u.used_tokens ?? 0;
      const max = u.maxTokens ?? u.max ?? u.context_window ?? u.window ?? u.limit;
      let pct = u.percentage != null ? u.percentage / 100 : (max ? total / max : null);
      const cats = Array.isArray(u.categories) ? u.categories.filter(c => c && c.tokens > 0) : [];
      const model = u.model || "";
      const autoCompact = u.isAutoCompactEnabled ? u.autoCompactThreshold : null;

      if (pct == null) { mount.innerHTML = '<div class="rail-empty">—</div>'; return; }
      pct = Math.max(0, Math.min(1, pct || 0));
      const cls = pct > 0.85 ? "hot" : pct > 0.6 ? "warn" : "";

      // Stacked segment bar: one colored block per category, proportional to the
      // category's share of the context window. Free space stays empty.
      let bar;
      if (cats.length && max) {
        const segs = cats.map(c => {
          const w = Math.max(0.5, (c.tokens / max) * 100);  // min 0.5% so tiny cats are visible
          const col = c.color || CAT_COLORS[(c.name || "").toLowerCase()] || "#8b949e";
          return `<div class="ctx-seg" style="width:${w}%;background:${col}" title="${esc(c.name)}: ${fmtTok(c.tokens)} tok"></div>`;
        }).join("");
        // Autocompact threshold marker (a tick where compaction will trigger).
        const tick = (autoCompact && autoCompact < max)
          ? `<div class="ctx-tick" style="left:${(autoCompact / max) * 100}%" title="autocompact at ${fmtTok(autoCompact)} tok"></div>` : "";
        bar = `<div class="ctx-bar ctx-stacked">${segs}${tick}</div>`;
      } else {
        bar = `<div class="ctx-bar"><div class="ctx-fill ${cls}" style="width:${Math.round(pct * 100)}%"></div></div>`;
      }

      // Legend: category chips with token counts (only when we have categories).
      const legend = cats.length ? `<div class="ctx-legend">${
        cats.slice(0, 6).map(c => {
          const col = c.color || CAT_COLORS[(c.name || "").toLowerCase()] || "#8b949e";
          return `<span class="ctx-leg" title="${esc(c.name)}: ${fmtTok(c.tokens)} tok"><span class="ctx-dot" style="background:${col}"></span>${esc(c.name)} ${fmtTok(c.tokens)}</span>`;
        }).join("")
      }</div>` : "";

      const meta = `<div class="ctx-meta">${Math.round(pct * 100)}% · ${fmtTok(total)} / ${max ? fmtTok(max) : "?"} tok${
        model ? ` · <span class="ctx-model">${esc(model)}</span>` : ""}</div>`;
      mount.innerHTML = `${bar}${legend}${meta}`;
    } catch (e) { mount.innerHTML = '<div class="rail-empty">—</div>'; }
  }
  $("ctx-refresh").addEventListener("click", loadContext);

  // ── Retention (Phase-0 scheduling telemetry) ──
  // Recall-at-due rate = of reviews that came back on a spacing gap, how many
  // were recalled (grade != again). Low = gaps too long; high with long
  // intervals = healthy. Read-only view of the append-only review log.
  const GRADE_COLORS = { again: "#ef4444", hard: "#fbbf24", good: "#10b981", easy: "#22d3ee" };
  async function loadRetention() {
    const mount = $("retention");
    try {
      const r = await (await fetch("/api/review-log")).json();
      if (r.error || !r.total) {
        mount.innerHTML = '<div class="rail-empty">No reviews yet.</div>'; return;
      }
      const rate = r.recall_rate;
      const ratePct = rate == null ? null : Math.round(rate * 100);
      const cls = ratePct == null ? "" : ratePct >= 85 ? "" : ratePct >= 65 ? "warn" : "hot";
      const g = r.grades || {};
      const totalG = (g.again || 0) + (g.hard || 0) + (g.good || 0) + (g.easy || 0);
      // Stacked grade-distribution bar.
      const segs = ["again", "hard", "good", "easy"].map(k => {
        const n = g[k] || 0; if (!n || !totalG) return "";
        const w = (n / totalG) * 100;
        return `<div class="ret-seg" style="width:${w}%;background:${GRADE_COLORS[k]}" title="${k}: ${n}"></div>`;
      }).join("");
      const headline = ratePct == null
        ? `<div class="ret-headline">— <span class="ret-sub">recall at due (no due reviews yet)</span></div>`
        : `<div class="ret-headline ${cls}">${ratePct}% <span class="ret-sub">recall at due · ${r.reviews_at_due} review${r.reviews_at_due === 1 ? "" : "s"}</span></div>`;
      const meta = `<div class="ret-meta">${r.total} total · ${r.topics} topic${r.topics === 1 ? "" : "s"}</div>`;
      mount.innerHTML = `${headline}<div class="ret-bar">${segs}</div>${meta}`;
    } catch (e) { mount.innerHTML = '<div class="rail-empty">—</div>'; }
  }
  $("retention-refresh").addEventListener("click", loadRetention);

  // ══════════════════════════════════════════════════════════════════
  //  Lesson-plan / curriculum picker (client-side)
  // ══════════════════════════════════════════════════════════════════
  const CURRICULUM = {
    "Foundations": [
      { kind: "path", title: "Cyber kill chain", prompt: "Walk me through the cyber kill chain, one stage at a time, with a diagram." },
      { kind: "guide", title: "ATT&CK tactics tour", prompt: "Give me a tour of the MITRE ATT&CK tactics and how they chain together." },
      { kind: "guide", title: "The unicorn rule", prompt: "Explain the unicorn rule and why rare + high-impact findings matter." },
    ],
    "Web / AppSec": [
      { kind: "path", title: "How SQL injection works", prompt: "Teach me how SQL injection works, then drill me on detecting it." },
      { kind: "path", title: "SSRF end to end", prompt: "What is SSRF, how do I exploit it, and how do I detect it?" },
      { kind: "drill", title: "XSS variants", prompt: "Drill me on the differences between reflected, stored, and DOM XSS." },
    ],
    "AD / Windows": [
      { kind: "path", title: "Kerberoasting", prompt: "Teach me about kerberoasting with a memory palace for the steps." },
      { kind: "guide", title: "Pass-the-hash vs pass-the-ticket", prompt: "Compare pass-the-hash and pass-the-ticket, then quiz me." },
      { kind: "drill", title: "AD privilege paths", prompt: "Drill me on common Active Directory privilege-escalation paths." },
    ],
    "Detection / Blue": [
      { kind: "guide", title: "Reading Sysmon", prompt: "Teach me to read Sysmon event logs for lateral movement." },
      { kind: "path", title: "Kill-chain to detections", prompt: "Map each kill-chain stage to the detections that catch it." },
      { kind: "drill", title: "Spot the false positive", prompt: "Drill me on distinguishing true positives from false positives in alerts." },
    ],
  };

  function openCurriculum() {
    const areas = Object.keys(CURRICULUM);
    const back = el("div", "modal-backdrop");
    const modal = el("div", "modal");
    modal.innerHTML = `<div class="modal-head"><div class="modal-title">Lesson plans <span class="sub">· pick a starting point</span></div><button class="modal-close">✕</button></div>
      <div class="modal-body"><div class="cur-tabs"></div><div class="cur-list"></div></div>`;
    back.appendChild(modal); document.body.appendChild(back);
    const close = () => back.remove();
    back.addEventListener("click", e => { if (e.target === back) close(); });
    modal.querySelector(".modal-close").onclick = close;
    document.addEventListener("keydown", function esc2(e) { if (e.key === "Escape") { close(); document.removeEventListener("keydown", esc2); } });

    const tabs = modal.querySelector(".cur-tabs"), list = modal.querySelector(".cur-list");
    const paint = (area) => {
      [...tabs.children].forEach(t => t.classList.toggle("active", t.textContent === area));
      list.innerHTML = "";
      CURRICULUM[area].forEach(c => {
        const card = el("button", "cur-card",
          `<div class="cur-card-head"><span class="cur-card-title">${esc(c.title)}</span><span class="cur-kind ${c.kind}">${c.kind}</span></div><div class="cur-card-prompt">${esc(c.prompt)}</div>`);
        card.onclick = () => { document.querySelector('[data-tab="coach"]').click(); inputEl.value = c.prompt; inputEl.focus(); close(); };
        list.appendChild(card);
      });
    };
    areas.forEach(a => { const t = el("button", "cur-tab", a); t.onclick = () => paint(a); tabs.appendChild(t); });
    paint(areas[0]);
  }
  $("plans-btn").addEventListener("click", openCurriculum);

  // ══════════════════════════════════════════════════════════════════
  //  Library tab (study projects)
  // ══════════════════════════════════════════════════════════════════
  async function loadStudyList() {
    const c = $("study-list-container");
    try {
      const data = await (await fetch("/api/study/list")).json();
      const projects = data.projects || [];
      if (!projects.length) { c.innerHTML = '<p class="empty-state">No study projects yet. Create one and upload a document to teach from it.</p>'; return; }
      c.innerHTML = projects.map(p => {
        const docs = p.docs || [];
        const docRows = docs.map(d => {
          const cls = d.status === "extracted" ? "ok" : (d.status === "failed" ? "err" : "busy");
          return `<div class="doc-row">
            <span class="doc-name" title="${esc(d.filename)}">${esc(d.filename)}</span>
            <span class="status-chip ${cls}">${esc(d.status)}</span>
            <button class="doc-del" title="Delete this document" onclick="window._deleteDoc('${p.project_id}','${d.sha}')">🗑</button>
          </div>`;
        }).join("") || '<div class="doc-row empty">No documents yet.</div>';
        const facts = (p.facts != null) ? p.facts : "—";
        return `
        <div class="study-card">
          <div class="study-card-title">${esc(p.title || p.project_id)}</div>
          <div class="study-card-meta">ID: ${esc(p.project_id)} · ${esc(p.subject || "cyber")} · ${docs.length} doc(s) · ${facts} facts</div>
          <div class="doc-list">${docRows}</div>
          <div class="study-card-actions">
            <button class="btn small primary" onclick="window._teach('${p.project_id}')">Teach from this</button>
            <button class="btn small ghost" onclick="window._upload('${p.project_id}')">Upload doc</button>
            <button class="btn small danger" onclick="window._deleteProject('${p.project_id}')">Delete</button>
          </div>
        </div>`;
      }).join("");
    } catch (e) { c.innerHTML = `<p class="empty-state">Error loading projects: ${esc(e.message)}</p>`; }
  }
  $("study-new").addEventListener("click", () => {
    // A small modal to capture title + subject together (better than nested
    // prompts). Non-cyber subjects suggest Fable for the tutor.
    const back = el("div", "modal-backdrop");
    const modal = el("div", "modal embed-modal");
    modal.innerHTML = `<div class="modal-head"><div class="modal-title">New study project</div><button class="modal-close">✕</button></div>
      <div class="modal-body">
        <label class="embed-field"><span>Title</span><input id="np-title" type="text" placeholder="e.g. Cell biology fundamentals" autofocus></label>
        <label class="embed-field"><span>Subject</span>
          <select id="np-subject">
            <option value="cyber">cyber (Opus tutor)</option>
            <option value="biology">biology (Fable tutor)</option>
            <option value="other">other (Fable tutor)</option>
          </select>
        </label>
        <p class="embed-help" id="np-hint">Cyber keeps the sharper, technical model (Opus). Biology/other suggest Fable's gentler narrative persona.</p>
        <div class="modal-actions">
          <button class="btn ghost" id="np-cancel">Cancel</button>
          <button class="btn primary" id="np-create">Create</button>
        </div>
      </div>`;
    back.appendChild(modal); document.body.appendChild(back);
    const close = () => back.remove();
    back.addEventListener("click", e => { if (e.target === back) close(); });
    modal.querySelector(".modal-close").onclick = close;
    modal.querySelector("#np-cancel").onclick = close;
    const subj = modal.querySelector("#np-subject");
    const hint = modal.querySelector("#np-hint");
    subj.onchange = () => {
      hint.innerHTML = subj.value === "cyber"
        ? "Cyber keeps the sharper, technical model (Opus)."
        : `${esc(subj.value)} suggests Fable's gentler narrative persona. (Advisory — your 🤖 Agents config always wins.)`;
    };
    const create = modal.querySelector("#np-create");
    const go = async () => {
      const title = modal.querySelector("#np-title").value.trim();
      if (!title) return;
      create.disabled = true;
      await postJson("/api/study/create", { title, subject: subj.value });
      close();
      loadStudyList();
    };
    create.onclick = go;
    modal.querySelector("#np-title").onkeydown = (e) => { if (e.key === "Enter") go(); };
  });
  window._teach = (pid) => { document.querySelector('[data-tab="coach"]').click(); send(`${STUDY_TOKEN} id=${pid}`); };
  function studyNote(text, cls) {
    const n = $("study-note");
    n.className = "study-note" + (cls ? " " + cls : "");
    n.textContent = text;
  }
  // Run extraction over SSE so the Library panel shows the librarian's live
  // activity (reading / tool-calls / writing) instead of a minute of silence.
  // Resolves with the same terminal result dict as the blocking POST. Closes on
  // the `done` event BEFORE the server ends the stream, so EventSource never
  // auto-reconnects (which would kick off a second extraction).
  function streamExtract(pid, sha, filename) {
    return new Promise((resolve) => {
      let settled = false;
      const done = (result) => { if (!settled) { settled = true; resolve(result); } };
      let es;
      try {
        es = new EventSource(`/api/study/${pid}/extract/stream?doc_sha=${encodeURIComponent(sha)}`);
      } catch (_) {
        // No EventSource → fall back to the blocking POST.
        postJson(`/api/study/${pid}/extract`, { doc_sha: sha }).then(done, () => done({ error: "extraction failed" }));
        return;
      }
      es.onmessage = (m) => {
        let evt; try { evt = JSON.parse(m.data); } catch (_) { return; }
        if (evt.kind === "progress") studyNote(`📖 ${filename}: ${evt.text}`);
        else if (evt.kind === "done") { es.close(); done(evt.result || {}); }
      };
      es.onerror = () => { es.close(); done({ error: "extraction stream interrupted" }); };
    });
  }
  // Confirm-style modal mirroring openCurriculum's .modal-backdrop/.modal shape.
  function confirmModal(title, bodyHtml, onYes, { danger = false, yesLabel = "Confirm" } = {}) {
    const back = el("div", "modal-backdrop");
    const modal = el("div", "modal");
    modal.innerHTML = `<div class="modal-head"><div class="modal-title">${esc(title)}</div><button class="modal-close">✕</button></div>
      <div class="modal-body">${bodyHtml}<div class="modal-actions"></div></div>`;
    back.appendChild(modal); document.body.appendChild(back);
    const close = () => back.remove();
    back.addEventListener("click", e => { if (e.target === back) close(); });
    modal.querySelector(".modal-close").onclick = close;
    document.addEventListener("keydown", function esc3(e) { if (e.key === "Escape") { close(); document.removeEventListener("keydown", esc3); } });
    const cancel = el("button", "btn ghost", "Cancel"); cancel.onclick = close;
    const yes = el("button", "btn " + (danger ? "danger" : "primary"), yesLabel);
    yes.onclick = () => { close(); onYes(); };
    const actions = modal.querySelector(".modal-actions");
    actions.append(cancel, yes);
  }
  window._deleteProject = (pid) => {
    confirmModal(
      "Delete project",
      `<p>This permanently removes the project, all its documents, and every KG fact under <code>study:${esc(pid)}:</code>.</p>`,
      async () => {
        const resp = await fetch(`/api/study/${pid}`, { method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirm: true }) });
        const r = await resp.json().catch(() => ({}));
        if (r.error) studyNote(`⚠ ${r.error}`, "err");
        else studyNote("✓ project deleted", "ok");
        loadStudyList();
      },
      { danger: true, yesLabel: "Delete project" },
    );
  };
  window._deleteDoc = (pid, sha) => {
    confirmModal(
      "Delete document",
      `<p>Removes this document and its passage facts from the knowledge graph. The project's structured sections stay.</p>`,
      async () => {
        const resp = await fetch(`/api/study/${pid}/doc/${sha}`, { method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirm: true }) });
        const r = await resp.json().catch(() => ({}));
        if (r.error) studyNote(`⚠ ${r.error}`, "err");
        else studyNote(`✓ document deleted (${r.purged || 0} facts purged)`, "ok");
        loadStudyList();
      },
      { danger: true, yesLabel: "Delete document" },
    );
  };
  function b64encode(buf) {
    // Chunked: spreading a whole Uint8Array into fromCharCode blows the call
    // stack for files past a few hundred KB.
    const bytes = new Uint8Array(buf), chunk = 0x8000;
    let bin = "";
    for (let i = 0; i < bytes.length; i += chunk) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    return btoa(bin);
  }
  async function postJson(url, body) {
    const resp = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!resp.ok) throw new Error(`server error (${resp.status})`);
    return resp.json();
  }
  window._upload = (pid) => {
    const input = el("input"); input.type = "file"; input.accept = ".pdf,.txt,.md,.markdown";
    input.onchange = () => {
      const file = input.files[0]; if (!file) return;
      const reader = new FileReader();
      reader.onload = async () => {
        try {
          studyNote(`⤴ uploading ${file.name}…`);
          const up = await postJson(`/api/study/${pid}/upload`, { filename: file.name, data: b64encode(reader.result) });
          if (up.error) { studyNote(`⚠ upload failed — ${up.error}`, "err"); return; }
          loadStudyList();
          studyNote(`📖 librarian reading ${file.name}…`);
          const ex = await streamExtract(pid, up.doc.sha.slice(0, 8), file.name);
          if (ex.status === "extracted") studyNote(`✓ extracted ${ex.sections} section(s) — ready to teach`, "ok");
          else studyNote(`⚠ extraction failed — ${ex.error || "unknown"}`, "err");
        } catch (e) {
          studyNote(`⚠ upload failed — ${e.message}`, "err");
        }
        loadStudyList();
      };
      reader.readAsArrayBuffer(file);
    };
    input.click();
  };

  // ══════════════════════════════════════════════════════════════════
  //  Settings (embeddings + librarian/parser) — gear modal
  // ══════════════════════════════════════════════════════════════════
  // Animate the KG coverage gauge while a model switch re-embeds every fact.
  // The backfill loop re-embeds ~200 facts/pass every 30s; pending climbs to 0.
  async function animateReembed(statusEl, model) {
    let embedded = 0, total = 0, pending = 1, ticks = 0;
    while (pending > 0 && ticks < 40) {  // cap at ~2min
      try {
        const cfg = await (await fetch("/api/embed/config")).json();
        const cov = cfg.coverage || {};
        embedded = cov.embedded || 0; total = cov.total || 0; pending = cov.pending || 0;
        statusEl.innerHTML = `<span class="chip busy">re-embedding under ${esc(model)}</span> ${embedded}/${total} · ${pending} pending`;
      } catch (_) { break; }
      if (pending <= 0) break;
      await new Promise(r => setTimeout(r, 3000));
      ticks++;
    }
    statusEl.innerHTML = `<span class="chip ok">re-embedded</span> ${embedded}/${total} under <code>${esc(model)}</code>`;
  }

  async function openEmbedSettings() {
    const back = el("div", "modal-backdrop");
    const modal = el("div", "modal embed-modal");
    modal.innerHTML = `<div class="modal-head"><div class="modal-title">Settings</div><button class="modal-close">✕</button></div>
      <div class="modal-body">
        <div class="set-tabs">
          <button class="set-tab active" data-set="embed">🜂 Embeddings</button>
          <button class="set-tab" data-set="agents">🤖 Agents</button>
        </div>

        <!-- ── Embeddings section ── -->
        <div class="set-pane active" data-set="embed">
          <p class="embed-help">Point salient-tutor at an OpenAI-compatible embeddings server (e.g. <code>http://ai.home:1234</code>). The model is <strong>selected from what the server has loaded</strong>. Switching the model re-embeds every fact (≈60–90s).</p>
          <label class="embed-field"><span>Base URL</span><input id="embed-url" type="text" placeholder="http://ai.home:1234"></label>
          <label class="embed-field"><span>API key <em>(optional)</em></span><input id="embed-key" type="password" placeholder="sk-…"></label>
          <div class="embed-row">
            <label class="embed-field grow"><span>Model</span>
              <select id="embed-model"><option value="">— refresh to load models —</option></select>
            </label>
            <button class="btn small ghost" id="embed-refresh" title="Query the server for models">↻ Refresh</button>
            <button class="btn small ghost hidden" id="embed-loadtoggle" title="Load / unload the selected model in LM Studio"></button>
          </div>
          <div id="embed-status" class="embed-status"></div>
          <div class="modal-actions">
            <button class="btn danger" id="embed-clear">Clear (use env)</button>
            <button class="btn ghost" id="embed-cancel">Cancel</button>
            <button class="btn primary" id="embed-save">Save</button>
          </div>
        </div>

        <!-- ── Agents section (per-agent provider/model/effort) ── -->
        <div class="set-pane" data-set="agents">
          <p class="embed-help">Route each agent at its own provider — the tutor on Opus, the librarian on a local model, the judge on DeepSeek or Codex, etc. Endpoint providers (DeepSeek/MiniMax/local) need a base URL + model. OpenAI Codex needs no endpoint — install the <code>codex</code> extra (<code>pip install -e ".[codex]"</code>) and authenticate via <code>codex login</code> or OPENAI_API_KEY; model defaults by tier. Effort sets the reasoning depth (low = fast/cheap, high = deep).</p>
          <div id="agents-list" class="agents-list"></div>
          <datalist id="ag-anthropic-models">
            <option value="claude-opus-4-8[1m]">Opus 4.8 (1M)</option>
            <option value="claude-sonnet-4-6[1m]">Sonnet 4.6 (1M)</option>
            <option value="claude-fable-5[1m]">Fable 5 (1M)</option>
            <option value="claude-haiku-4-5">Haiku 4.5</option>
          </datalist>
          <datalist id="ag-minimax-models">
            <option value="MiniMax-M3">M3 — flagship (agentic/coding/long-context)</option>
            <option value="MiniMax-M2.7">M2.7</option>
            <option value="MiniMax-M2.7-highspeed">M2.7 highspeed</option>
            <option value="MiniMax-M2.5">M2.5</option>
            <option value="MiniMax-M2.5-highspeed">M2.5 highspeed</option>
            <option value="MiniMax-M2.1">M2.1</option>
            <option value="MiniMax-M2">M2</option>
            <option value="MiniMax-Text-01">Text-01</option>
          </datalist>
          <datalist id="ag-codex-models">
            <option value="gpt-5.5">gpt-5.5 — flagship (≈ Opus tier)</option>
            <option value="gpt-5.4">gpt-5.4 — balanced (≈ Sonnet tier)</option>
            <option value="gpt-5.3-codex-spark">gpt-5.3-codex-spark — fast/cheap (≈ Haiku tier)</option>
          </datalist>
          <datalist id="ag-deepseek-models">
            <option value="deepseek-v4-pro">V4 Pro — reasoning/coding</option>
            <option value="deepseek-v4-flash">V4 Flash — fast/cheap</option>
            <option value="deepseek-chat">chat (legacy → v4-flash, retires 2026-07-24)</option>
            <option value="deepseek-reasoner">reasoner (legacy → v4-flash thinking, retires 2026-07-24)</option>
          </datalist>
          <div id="agents-status" class="embed-status"></div>
        </div>
      </div>`;
    back.appendChild(modal); document.body.appendChild(back);
    const close = () => back.remove();
    back.addEventListener("click", e => { if (e.target === back) close(); });
    modal.querySelector(".modal-close").onclick = () => close();
    document.addEventListener("keydown", function esc4(e) { if (e.key === "Escape") { close(); document.removeEventListener("keydown", esc4); } });

    // ── tab switching ──
    modal.querySelectorAll(".set-tab").forEach(t => t.onclick = () => {
      modal.querySelectorAll(".set-tab").forEach(x => x.classList.toggle("active", x === t));
      modal.querySelectorAll(".set-pane").forEach(p => p.classList.toggle("active", p.dataset.set === t.dataset.set));
    });

    // Recommended defaults — preselected in the model dropdowns when the server
    // offers them and nothing is already saved. Pre-filled into the Base URL
    // field when empty, so a fresh setup points at the operator's LM Studio by
    // default. Editable: these are just the highlighted starting choice.
    const DEFAULT_BASE_URL = "http://ai.home:1234";
    const DEFAULT_EMBED_MODEL = "text-embedding-qwen3-embedding-0.6b";
    // Pick the model id to highlight in a dropdown: a saved config wins, else
    // the recommended default if the server has it, else the first model.
    const pickModel = (ids, saved, dflt) =>
      (saved && ids.includes(saved) ? saved : null) ||
      ids.find(m => m === dflt) ||
      ids[0] ||
      "";
    // Render a <select> of LM Studio models with a loaded-state marker on each
    // option. `models` is the rich list [{id, state, type, ...}] from
    // /api/lms/models. Replaces `selEl` (in case a prior failure swapped in
    // free-text) and returns the fresh select.
    function renderModelSelect(selEl, models) {
      const real = el("select"); real.id = selEl.id;
      const ids = models.map(m => m.id);
      real.innerHTML = models.length
        ? models.map(m => {
            const mark = m.state === "loaded" ? " ●" : " ○";
            const ty = m.type ? ` [${esc(m.type)}]` : "";
            return `<option value="${esc(m.id)}">${esc(m.id)}${ty}${mark}</option>`;
          }).join("")
        : `<option value="">— no models found —</option>`;
      selEl.replaceWith(real);
      return real;
    }
    // Update a load/unload toggle button to match the selected model's state.
    // hidden when no model selected or the server can't report state.
    function syncLoadToggle(btn, models, selectedId) {
      const m = models.find(x => x.id === selectedId);
      if (!m || !m.state) { btn.classList.add("hidden"); btn.textContent = ""; return; }
      btn.classList.remove("hidden");
      if (m.state === "loaded") { btn.textContent = "⏏ Unload"; btn.title = "Unload from LM Studio memory"; }
      else { btn.textContent = "⚡ Load"; btn.title = "Load into LM Studio memory"; }
    }
    // Drive a load/unload against /api/lms/{load,unload}, then re-refresh.
    // statusEl shows progress + LM Studio's error (with the loaded list on
    // failure, so the operator can see what to unload for 'no room').
    async function lmsToggle({ action, model, url, key, statusEl, after }) {
      statusEl.innerHTML = `<span class="chip busy">${action === "load" ? "loading" : "unloading"} ${esc(model)}…</span>`;
      try {
        const r = await postJson(`/api/lms/${action}`, { base_url: url, api_key: key, model });
        if (r.ok) statusEl.innerHTML = `<span class="chip ok">${action === "load" ? "loaded" : "unloaded"} ${esc(model)}</span>`;
        else {
          const loaded = (r.loaded || []).join(", ") || "none";
          statusEl.innerHTML = `<span class="chip err">⚠ ${esc(r.error || "failed")}</span> currently loaded: ${esc(loaded)}`;
        }
        await after();
      } catch (e) {
        statusEl.innerHTML = `<span class="chip err">⚠ ${esc(e.message)}</span>`;
      }
    }

    // ══ Embeddings section ══
    const urlI = modal.querySelector("#embed-url"), keyI = modal.querySelector("#embed-key"),
          sel = modal.querySelector("#embed-model"), status = modal.querySelector("#embed-status");
    let savedEmbedModel = "";
    try {
      const cfg = await (await fetch("/api/embed/config")).json();
      urlI.value = cfg.base_url || DEFAULT_BASE_URL;
      if (cfg.api_key) keyI.placeholder = "•••• (set — leave blank to keep)";
      savedEmbedModel = cfg.model || "";
      if (cfg.enabled) status.innerHTML = `<span class="chip ok">configured</span> model <code>${esc(cfg.model)}</code> · coverage ${cfg.coverage.embedded}/${cfg.coverage.total}`;
      else status.innerHTML = `<span class="chip">not configured</span> — falling back to env / inert`;
    } catch (_) { /* ignore — fields just stay empty */ }

    let embedModels = [];
    const embedToggle = modal.querySelector("#embed-loadtoggle");
    async function refreshModels() {
      const url = urlI.value.trim();
      if (!url) { status.innerHTML = `<span class="chip err">enter a base URL first</span>`; return; }
      status.innerHTML = `<span class="chip busy">checking ${esc(url)}…</span>`;
      sel.innerHTML = `<option value="">loading…</option>`;
      try {
        const r = await (await fetch(`/api/lms/models?base_url=${encodeURIComponent(url)}&api_key=${encodeURIComponent(keyI.value)}`)).json();
        if (!r.reachable) {
          // FAIL-SAFE: server down/unreachable — keep the modal usable.
          status.innerHTML = `<span class="chip err">⚠ server unreachable</span> ${esc(r.error || "")}. You can still type a model id and Save; the backfill will retry when the server is up.`;
          sel.innerHTML = `<option value="">— server unreachable, type a model id —</option>`;
          const free = el("input");
          free.type = "text"; free.id = "embed-model"; free.placeholder = "model id";
          free.className = "embed-fallback"; free.value = "";
          sel.replaceWith(free);
          embedToggle.classList.add("hidden");
          return;
        }
        embedModels = r.models;
        const loadedN = (r.loaded || []).length;
        status.innerHTML = `<span class="chip ok">reachable</span> — ${r.models.length} model(s), ${loadedN} in memory`;
        const real = renderModelSelect(sel, r.models);
        if (r.models.length) real.value = pickModel(r.models.map(m => m.id), savedEmbedModel, DEFAULT_EMBED_MODEL);
        syncLoadToggle(embedToggle, r.models, real.value);
        real.onchange = () => syncLoadToggle(embedToggle, embedModels, real.value);
      } catch (e) {
        status.innerHTML = `<span class="chip err">⚠ ${esc(e.message)}</span>`;
      }
    }
    modal.querySelector("#embed-refresh").onclick = refreshModels;
    embedToggle.onclick = async () => {
      const mEl = modal.querySelector("#embed-model");
      const model = mEl ? (mEl.value || "").trim() : "";
      if (!model) return;
      const m = embedModels.find(x => x.id === model);
      const action = m && m.state === "loaded" ? "unload" : "load";
      await lmsToggle({ action, model, url: urlI.value.trim(), key: keyI.value, statusEl: status, after: refreshModels });
    };
    modal.querySelector("#embed-cancel").onclick = () => close();
    modal.querySelector("#embed-clear").onclick = async () => {
      await postJson("/api/embed/config", { base_url: "", model: "", api_key: "" });
      status.innerHTML = `<span class="chip">cleared — using env / inert</span>`;
      urlI.value = ""; keyI.value = ""; savedEmbedModel = "";
      close();
    };
    modal.querySelector("#embed-save").onclick = async () => {
      const mEl = modal.querySelector("#embed-model");
      const model = mEl ? (mEl.value || "").trim() : "";
      const body = { base_url: urlI.value.trim(), model, api_key: keyI.value };
      if (!body.api_key) delete body.api_key;  // blank = keep existing
      const r = await postJson("/api/embed/config", body);
      if (r.error) { status.innerHTML = `<span class="chip err">${esc(r.error)}</span>`; return; }
      savedEmbedModel = r.model || "";
      const changed = savedEmbedModel && (r.coverage && r.coverage.pending > 0);
      status.innerHTML = `<span class="chip ok">saved</span> model <code>${esc(r.model || "")}</code>`;
      // Animate coverage when a model switch kicked off a re-embed.
      if (changed) await animateReembed(status, r.model);
      setTimeout(close, 600);
    };

    // ══ Agents section (per-agent provider/model/effort) ══
    // (The legacy "Librarian / parser" section was retired — it could only
    // express claude|local and mislabeled a MiniMax/DeepSeek librarian as
    // "Local (LM Studio)", clobbering it to local on save. Route the librarian
    // (and every agent) from the Agents tab below, which speaks all providers.)
    const agentsList = modal.querySelector("#agents-list"),
          agentsStatus = modal.querySelector("#agents-status");
    let AGENT_PROVIDERS = {}, AGENT_EFFORTS = ["low", "med", "high"], agentsData = {};
    // Per-provider model autocomplete: which datalist feeds the Model field and
    // what id to default to when you first switch to that provider. Endpoint
    // providers (minimax/deepseek) can't be probed like LM Studio, so we ship a
    // curated list of the current native model ids instead.
    const PROVIDER_MODELS = {
      anthropic: { list: "ag-anthropic-models", default: "" },
      minimax:   { list: "ag-minimax-models",  default: "MiniMax-M3" },
      deepseek:  { list: "ag-deepseek-models", default: "deepseek-v4-pro" },
      local:     { list: "", default: "" },
      codex:     { list: "ag-codex-models", default: "" },
    };
    // Model placeholder per provider shape: endpoint providers need an id,
    // codex defaults by Claude-tier mapping, anthropic falls to the roster.
    const modelPlaceholder = (spec) =>
      spec.needs_endpoint ? "model id"
      : spec.kind === "backend" ? "default by tier (e.g. gpt-5.5)"
      : "default (e.g. claude-opus-4-8[1m])";
    // Backend providers (codex) fail at the NEXT TURN if the runtime is
    // missing/unauthenticated — probe up front so the row hints before the
    // operator routes an agent there. One fetch per provider per modal open;
    // the server adds its own TTL + single-flight (each cold probe spawns a
    // codex CLI handshake).
    const probeCache = {};
    const probeProvider = (name) => {
      if (!probeCache[name])
        probeCache[name] = fetch(`/api/providers/probe?name=${encodeURIComponent(name)}`)
          .then(r => r.json())
          .catch(e => ({ available: false, detail: e.message }));
      return probeCache[name];
    };
    function updateProbeHint(row, prov) {
      const el = row.querySelector(".ag-probe");
      if (!el) return;
      if ((AGENT_PROVIDERS[prov] || {}).kind !== "backend") { el.innerHTML = ""; return; }
      el.innerHTML = `<span class="chip busy">checking ${esc(prov)}…</span>`;
      probeProvider(prov).then(p => {
        if (row.querySelector(".ag-prov").value !== prov) return; // switched away meanwhile
        el.innerHTML = p.available
          ? `<span class="chip ok">${esc(prov)} ready</span>`
          : `<span class="chip err" title="${esc(p.detail || "")}">${esc(prov)} unavailable — ${esc((p.detail || "not reachable").slice(0, 80))}</span>`;
      });
    }
    async function loadAgents() {
      agentsList.innerHTML = `<p class="empty-state">loading…</p>`;
      try {
        const r = await (await fetch("/api/agents/config")).json();
        if (r.error) { agentsList.innerHTML = `<p class="empty-state">${esc(r.error)}</p>`; return; }
        AGENT_PROVIDERS = r.providers || {};
        AGENT_EFFORTS = r.efforts || ["low", "med", "high"];
        agentsData = r.agents || {};
        // Optional agents (judge/tutor_alt) not live yet get an addable row,
        // defaulting to anthropic — give them a model + Save to bring them up.
        (r.optional || []).forEach(name => {
          if (!(name in agentsData)) agentsData[name] = { provider: "anthropic", model: "", _optional: true };
        });
        renderAgentRows();
      } catch (e) { agentsList.innerHTML = `<p class="empty-state">${esc(e.message)}</p>`; }
    }
    function renderAgentRows() {
      const provOpts = Object.entries(AGENT_PROVIDERS).map(([k, v]) =>
        `<option value="${k}">${esc(v.label)}</option>`).join("");
      agentsList.innerHTML = Object.entries(agentsData).map(([name, cfg]) => {
        const prov = cfg.provider || "anthropic";
        const spec = AGENT_PROVIDERS[prov] || {};
        const needs = spec.needs_endpoint;
        const hints = { tutor: " orchestrator", librarian: " parser", judge: " gate", tutor_alt: " shadow tutor" };
        const hint = (hints[name] || "") + (cfg._optional ? " · not set — add a model to enable" : "");
        return `<div class="agent-row" data-agent="${esc(name)}">
          <div class="agent-name">${esc(name)}<span class="agent-hint">${hint}</span></div>
          <div class="agent-fields">
            <label class="embed-field"><span>Provider</span>
              <select class="ag-prov">${provOpts}</select></label>
            <label class="embed-field ${needs ? "" : "hidden"} ag-endpoint"><span>Base URL</span>
              <input class="ag-url" type="text" value="${esc(cfg.base_url || spec.default_base_url || "")}" placeholder="${esc(spec.default_base_url || "http://…")}"></label>
            <label class="embed-field ${needs ? "" : "hidden"} ag-endpoint"><span>API key</span>
              <input class="ag-key" type="password" placeholder="${cfg.api_key ? "•••• (set)" : "optional"}"></label>
            <label class="embed-field"><span>Model</span>
              <input class="ag-model" type="text" list="${(PROVIDER_MODELS[prov] || {}).list || ""}" value="${esc(cfg.model || "")}" placeholder="${modelPlaceholder(spec)}"></label>
            <label class="embed-field"><span>Effort</span>
              <select class="ag-effort">${AGENT_EFFORTS.map(e => `<option value="${e}">${e}</option>`).join("")}</select></label>
          </div>
          <div class="agent-actions">
            <span class="ag-probe"></span>
            <button class="btn small primary ag-save">Save</button>
          </div>
        </div>`;
      }).join("");
      // Set current values + wire interactions.
      agentsList.querySelectorAll(".agent-row").forEach(row => {
        const name = row.dataset.agent, cfg = agentsData[name] || {};
        row.querySelector(".ag-prov").value = cfg.provider || "anthropic";
        row.querySelector(".ag-effort").value = cfg.effort || "med";
        updateProbeHint(row, cfg.provider || "anthropic");
        row.querySelector(".ag-prov").onchange = (e) => {
          const prov = e.target.value, spec = AGENT_PROVIDERS[prov] || {}, needs = spec.needs_endpoint;
          updateProbeHint(row, prov);
          row.querySelectorAll(".ag-endpoint").forEach(f => f.classList.toggle("hidden", !needs));
          // Model is provider-specific too: repopulate from the saved config
          // when returning to the saved provider, else seed this provider's
          // default (M3 / v4-pro) — so a stale endpoint model can't carry into
          // an Anthropic save (and vice-versa). The autocomplete list swaps too.
          const model = row.querySelector(".ag-model"), saved = prov === cfg.provider;
          const pm = PROVIDER_MODELS[prov] || {};
          model.setAttribute("list", pm.list || "");
          model.value = saved ? (cfg.model || "") : (pm.default || "");
          model.placeholder = modelPlaceholder(spec);
          if (!needs) return;
          // Endpoint fields are provider-specific: repopulate from the saved
          // config when returning to the saved provider, else this provider's
          // defaults — so a stale LM Studio URL can't carry over into a
          // MiniMax/DeepSeek save.
          const url = row.querySelector(".ag-url"), key = row.querySelector(".ag-key");
          url.value = saved ? (cfg.base_url || spec.default_base_url || "") : (spec.default_base_url || "");
          url.placeholder = spec.default_base_url || "http://…";
          key.value = "";
          key.placeholder = (saved && cfg.api_key) ? "•••• (set)" : "optional";
        };
        row.querySelector(".ag-save").onclick = async () => {
          const provider = row.querySelector(".ag-prov").value;
          const body = { agent: name, provider, effort: row.querySelector(".ag-effort").value };
          // Model applies to every provider (endpoint model, or a per-agent
          // Anthropic model override like opus/sonnet/fable); base_url/key are
          // endpoint-only.
          body.model = row.querySelector(".ag-model").value.trim();
          if ((AGENT_PROVIDERS[provider] || {}).needs_endpoint) {
            body.base_url = row.querySelector(".ag-url").value.trim();
            body.api_key = row.querySelector(".ag-key").value;
          }
          agentsStatus.innerHTML = `<span class="chip busy">saving ${esc(name)}…</span>`;
          const r = await postJson("/api/agents/config", body);
          if (r.error) { agentsStatus.innerHTML = `<span class="chip err">${esc(r.error)}</span>`; return; }
          agentsData[name] = r;
          agentsStatus.innerHTML = `<span class="chip ok">saved ${esc(name)}</span> → ${esc(r.provider)}${r.model ? " / " + esc(r.model) : ""}`;
          renderAgentRows();
        };
      });
    }
    loadAgents();
  }
  const gear = $("embed-gear");
  if (gear) gear.addEventListener("click", openEmbedSettings);

  // ══════════════════════════════════════════════════════════════════
  //  Tabs, zoom, welcome, boot
  // ══════════════════════════════════════════════════════════════════
  document.querySelectorAll(".tab").forEach(tab => tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    document.querySelectorAll(".tab-pane").forEach(p => p.classList.remove("active"));
    tab.classList.add("active");
    $("pane-" + tab.dataset.tab).classList.add("active");
    $("rail-flip").classList.toggle("hidden", tab.dataset.tab !== "coach");
    if (tab.dataset.tab === "library") loadStudyList();
  }));

  // ⇄ workspace-rail side toggle (persisted)
  function initRailSide() {
    const apply = (side) => $("pane-coach").classList.toggle("rail-left", side === "left");
    let side = (() => { try { return localStorage.getItem("tutor.railSide"); } catch (_) { return null; } })() === "left" ? "left" : "right";
    apply(side);
    $("rail-flip").addEventListener("click", () => {
      side = side === "left" ? "right" : "left";
      apply(side);
      try { localStorage.setItem("tutor.railSide", side); } catch (_) {}
    });
  }

  const zoom = (d) => { const fs = parseInt(getComputedStyle(document.documentElement).getPropertyValue("--font-size")) || 15; document.documentElement.style.setProperty("--font-size", Math.max(12, Math.min(20, fs + d)) + "px"); };
  $("zoom-out").addEventListener("click", () => zoom(-1));
  $("zoom-in").addEventListener("click", () => zoom(1));

  function showWelcome() {
    welcomeShown = true;
    messagesEl.innerHTML = `<div class="welcome"><h2>Welcome to salient-tutor</h2>
      <p>A Socratic teaching coach with spaced repetition. Ask about a technique, a concept, or a defensive signal — or pick a lesson plan from the rail.</p>
      <div class="suggestions">
        <div class="suggestion" data-p="Teach me about kerberoasting">Kerberoasting</div>
        <div class="suggestion" data-p="What is SSRF and how do I detect it?">SSRF</div>
        <div class="suggestion" data-p="Walk me through the cyber kill chain">Kill chain</div>
        <div class="suggestion" data-p="How does SQL injection work?">SQL injection</div>
      </div></div>`;
    messagesEl.querySelectorAll(".suggestion").forEach(s => s.addEventListener("click", () => send(s.dataset.p)));
  }

  // ── history replay: rehydrate recent turns on (re)load ──
  async function bootTranscript() {
    try {
      const data = await (await fetch("/api/history?limit=24")).json();
      const turns = data.turns || [];
      if (!turns.length) { showWelcome(); return; }
      messagesEl.innerHTML = ""; welcomeShown = false;
      messagesEl.appendChild(el("div", "hist-divider", "▲ earlier session"));
      turns.forEach(t => renderHistoryTurn(t.role === "operator" ? "operator" : "tutor", t.text));
      messagesEl.appendChild(el("div", "hist-divider", "▼ continue below"));
      revealQuickReplies();
      scrollDown();
    } catch (_) { showWelcome(); }
  }
  function renderHistoryTurn(role, text) {
    const msg = el("div", `message ${role} hist`);
    msg.appendChild(el("div", "who", role === "operator" ? "you" : "tutor"));
    const bubble = el("div", "bubble");
    const { html, mermaidBlocks } = renderMd(text);
    bubble.innerHTML = html; msg.appendChild(bubble);
    messagesEl.appendChild(msg);
    if (mermaidBlocks.length) renderMermaid(mermaidBlocks);
    if (role === "tutor") attachTts(bubble, text);
  }

  connect();
  initTts();
  initTutors();
  // /api/config (which sets imageAvailable/imageDefault) MUST resolve before we
  // replay history — otherwise history image/palace fences render while images
  // still look unavailable and degrade to caption-only cards (they "disappear"
  // on reload). Chain config → transcript; replay even if config errors.
  initStrictness().then(bootTranscript, bootTranscript);
  initRailSide();
  loadSkillmap();
  loadContext();
  inputEl.focus();
})();
