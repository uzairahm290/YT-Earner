// Production Desk: one small vanilla-JS app, no build step.

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const VISUALS = {
  archive_photo: "Archive photo",
  broll: "Stock footage",
  document: "Document",
  chart: "Chart",
  map: "Map",
  title_card: "Title card",
};
const VISUAL_HINT = {
  archive_photo: "Wikimedia search, e.g. Stephen Elop 2011",
  broll: "Generic and filmable, e.g. empty office at night",
  document: "Headline, filing or memo to show",
  chart: "The data, using fact-sheet numbers only",
  map: "Place",
  title_card: "Text on screen",
};

const ICON = {
  up: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M4 10l4-4 4 4"/></svg>',
  down: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M4 6l4 4 4-4"/></svg>',
  del: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 8.5h5.8l.6-8.5"/></svg>',
};

const state = {
  status: null,
  episodes: [],
  jobs: [],
  topics: null,
  ep: null,        // full episode payload from the API
  draft: null,     // editable copy of ep.script
  dirty: false,
  tab: "facts",
  angleChoice: null,
};

// ───────────────────────── API ─────────────────────────

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) throw new Error(data?.detail || `Request failed (${res.status})`);
  return data;
}

function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("bad", bad);
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), bad ? 6000 : 2600);
}

// ───────────────────────── status helpers ─────────────────────────

const STATUS_TEXT = {
  new: "Researching",
  researched: "Facts to check",
  verified: "Ready for a script",
  scripted: "Script to review",
  approved: "Approved",
};

function statusOrder() {
  return ["new", ...state.status.stages.map((s) => s.status)];
}
const rank = (status) => Math.max(0, statusOrder().indexOf(status));

function epLine(e) {
  if (e.running) return e.status === "new" ? "Researching…" : "Working…";
  if (e.status === "researched" && e.verify_total) return `${e.verify_done} of ${e.verify_total} facts checked`;
  if (e.status === "scripted" || e.status === "approved") return `${STATUS_TEXT[e.status]}${e.est_minutes ? ` · ${e.est_minutes} min` : ""}`;
  return STATUS_TEXT[e.status] || e.status;
}

function wpm() { return state.status?.script_cfg?.words_per_minute || 140; }
function words(text) { return (text.trim().match(/\S+/g) || []).length; }
function beatSeconds(b) { return (words(b.narration) / wpm()) * 60 + (Number(b.pause_after) || 0); }
function clock(sec) {
  sec = Math.round(sec);
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
}

// ───────────────────────── rail ─────────────────────────

function renderRail() {
  const list = $("#epList");
  const slug = state.ep?.slug;
  if (!state.episodes.length) {
    list.innerHTML = `<li class="ep-empty">No stories yet.</li>`;
  } else {
    list.innerHTML = state.episodes.map((e) => `
      <li><a href="#/e/${e.slug}" class="${e.slug === slug && location.hash.startsWith("#/e/") ? "active" : ""}">
        <span class="ep-name">${esc(e.topic)}</span>
        <span class="ep-state">${esc(epLine(e))}</span>
      </a></li>`).join("");
  }

  const route = location.hash.split("/")[1] || "";
  $$(".rail-nav a").forEach((a) => a.classList.toggle("active",
    (a.dataset.nav === "desk" && route === "") || (a.dataset.nav === "topics" && route === "topics")));

  const s = state.status;
  const note = $("#setupNote");
  if (s?.demo) {
    note.hidden = false;
    note.innerHTML = `<b>Demo mode.</b> Everything runs on sample data. Restart with <code>python app.py</code> to use real sources.`;
  } else if (s && (!s.key_present || !s.wiki_agent_set)) {
    note.hidden = false;
    note.innerHTML = [
      !s.key_present && `Add <code>${esc(s.key_env)}</code> to <code>.env</code> so research and scripts can run.`,
      !s.wiki_agent_set && `Add <code>WIKI_USER_AGENT</code> to <code>.env</code> with your email, or Wikipedia will refuse requests.`,
    ].filter(Boolean).join("<br><br>");
  } else {
    note.hidden = true;
  }
}

function renderJobs() {
  const box = $("#jobs");
  box.innerHTML = state.jobs.slice(-4).map((j) => {
    const secs = ((j.finished || Date.now() / 1000) - j.started);
    const label = j.status === "error" ? `${j.label} failed` : j.label;
    return `<div class="job ${j.status}" title="${esc(j.error || "")}">
      <span class="reel" aria-hidden="true"></span>
      <span class="label">${esc(label)}</span>
      <span class="t">${j.status === "running" ? clock(secs) : j.status === "done" ? "done" : ""}</span>
    </div>`;
  }).join("");
}

// ───────────────────────── polling ─────────────────────────

let lastJobs = new Map();
// Mark a job we just started as running, so a fast finish still triggers a refresh.
const track = (job) => lastJobs.set(job.id, "running");

async function poll() {
  try {
    const jobs = await api("/api/jobs");
    const finished = jobs.filter((j) => j.status !== "running" && lastJobs.get(j.id) === "running");
    state.jobs = jobs;
    lastJobs = new Map(jobs.map((j) => [j.id, j.status]));
    renderJobs();

    if (finished.length) {
      state.episodes = await api("/api/episodes");
      renderRail();
      for (const j of finished) {
        if (j.status === "error") toast(`${j.label} failed: ${j.error}`, true);
        else toast(j.kind === "topics" ? "Topic ideas updated" : `${j.label}: done`);
      }
      const route = parseRoute();
      if (route.view === "topics" && finished.some((j) => j.kind === "topics")) {
        state.topics = await api("/api/topics");
        render();
      } else if (route.view === "episode" && finished.some((j) => j.slug === route.slug)) {
        if (state.dirty) toast("New version ready. Save or discard your edits to see it.");
        else await loadEpisode(route.slug, true);
      } else if (route.view === "desk") {
        render();
      }
    } else if (jobs.some((j) => j.status === "running")) {
      // keep "Researching…" labels fresh
      const before = JSON.stringify(state.episodes.map((e) => e.running));
      state.episodes = await api("/api/episodes");
      if (JSON.stringify(state.episodes.map((e) => e.running)) !== before) { renderRail(); if (parseRoute().view !== "episode") render(); }
    }
  } catch { /* server restarting; try again next tick */ }
}

// ───────────────────────── routing ─────────────────────────

function parseRoute() {
  const [, a, b, c] = location.hash.split("/");
  if (a === "topics") return { view: "topics" };
  if (a === "e" && b) return { view: "episode", slug: decodeURIComponent(b), tab: c || null };
  return { view: "desk" };
}

let currentHash = location.hash;
window.addEventListener("hashchange", async () => {
  const leavingEpisode = currentHash.split("/")[2] !== location.hash.split("/")[2];
  if (state.dirty && leavingEpisode && !confirm("You have unsaved script edits. Leave without saving?")) {
    history.replaceState(null, "", currentHash);
    return;
  }
  if (leavingEpisode) state.dirty = false;
  currentHash = location.hash;
  $("#rail").classList.remove("open");
  await route();
});

async function route() {
  const r = parseRoute();
  if (r.view === "topics" && !state.topics) state.topics = await api("/api/topics");
  if (r.view === "episode") {
    if (state.ep?.slug !== r.slug) await loadEpisode(r.slug);
    else { if (r.tab) state.tab = r.tab; render(); }
    return;
  }
  render();
  $("#main").focus({ preventScroll: true });
  window.scrollTo(0, 0);
}

async function loadEpisode(slug, keepTab = false) {
  try {
    state.ep = await api(`/api/episodes/${encodeURIComponent(slug)}`);
  } catch (e) {
    state.ep = null;
    $("#main").innerHTML = `<div class="empty"><h2>That story isn't here</h2><p>${esc(e.message)}</p><a class="btn" href="#/">Back to the desk</a></div>`;
    renderRail();
    return;
  }
  state.draft = state.ep.script ? structuredClone(state.ep.script) : null;
  state.dirty = false;
  state.angleChoice = state.ep.meta?.angle || null;
  const r = parseRoute();
  if (r.tab) state.tab = r.tab;
  else if (!keepTab) state.tab = defaultTab(state.ep);
  render();
}

function defaultTab(ep) {
  return ["scripted", "approved"].includes(ep.status) ? "script" : "facts";
}

function render() {
  renderRail();
  const r = parseRoute();
  if (r.view === "topics") renderTopics();
  else if (r.view === "episode" && state.ep) renderEpisode();
  else renderDesk();
}

// ───────────────────────── desk ─────────────────────────

function renderDesk() {
  const eps = state.episodes;
  const cols = [
    { title: "Researching", what: "Pulling sources and building the fact sheet.", match: (e) => e.status === "new" },
    { title: "Check the facts", what: "Tick off each flagged claim before it goes in a script.", match: (e) => e.status === "researched" },
    { title: "Write the script", what: "Facts are solid. Pick an angle and write it.", match: (e) => e.status === "verified" },
    { title: "Review and approve", what: "Read it out loud, fix what sounds off, approve.", match: (e) => e.status === "scripted" },
    { title: "Approved", what: "Waiting for the voice stage, which comes next.", match: (e) => e.status === "approved" },
  ];
  const needYou = eps.filter((e) => ["researched", "verified", "scripted"].includes(e.status) && !e.running).length;

  if (!eps.length) {
    $("#main").innerHTML = `
      <div class="empty">
        <h1>Start your first story</h1>
        <p class="lede">Pick a company, product or disaster with a real story behind it. Research builds a sourced fact sheet; you check it; then the script gets written from those facts only.</p>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:18px">
          <button class="btn btn-primary" data-act="new">New story</button>
          <a class="btn" href="#/topics">Browse topic ideas</a>
        </div>
      </div>`;
    return;
  }

  $("#main").innerHTML = `
    <header class="desk-head">
      <h1>${needYou ? `${needYou} ${needYou === 1 ? "story needs" : "stories need"} you` : "Nothing needs you right now"}</h1>
      <p class="lede">${needYou ? "Work left to right. Nothing moves forward without your sign-off." : "Start a new story or grab one from the topic ideas."}</p>
    </header>
    <section class="queue">
      ${cols.map((c) => {
        const items = eps.filter(c.match);
        return `<div class="queue-col">
          <h3>${c.title} <span class="n">${items.length}</span></h3>
          <p class="what">${c.what}</p>
          ${items.length ? items.map((e) => `
            <a class="queue-item" href="#/e/${e.slug}">
              <div class="qi-name">${esc(e.topic)}</div>
              <div class="qi-meta">${esc(epLine(e))}</div>
            </a>`).join("") : `<div class="none">Nothing here.</div>`}
        </div>`;
      }).join("")}
    </section>`;
}

// ───────────────────────── topics ─────────────────────────

function renderTopics() {
  const t = state.topics || { items: [] };
  const running = state.jobs.some((j) => j.kind === "topics" && j.status === "running");
  $("#main").innerHTML = `
    <header class="page-head">
      <div>
        <h1>Topic ideas</h1>
        <p class="lede">From US tech outlets and Wikipedia articles with a traffic spike${t.fetched ? `, fetched ${esc(t.fetched)}` : ""}. A headline is only a hook; look for the bigger story behind it.</p>
      </div>
      <button class="btn" data-act="refresh-topics" ${running ? "disabled" : ""}>${running ? "Finding ideas…" : "Refresh ideas"}</button>
    </header>
    ${t.items.length ? `<ul class="topic-list">
      ${t.items.map((x, i) => `
        <li class="topic">
          <div class="t-title">${esc(x.title)}${x.kind === "wiki_trend" ? `<span class="trend-pill">${(x.views || 0).toLocaleString()} views yesterday</span>` : ""}</div>
          <div class="t-src">${esc(x.kind === "wiki_trend" ? "Wikipedia trending" : x.source)}${x.url ? ` · <a href="${esc(x.url)}" target="_blank" rel="noopener">source</a>` : ""}</div>
          <div class="t-sum">${esc(x.summary)}</div>
          <button class="btn" data-act="research-topic" data-i="${i}">Research this</button>
        </li>`).join("")}
    </ul>` : `<div class="empty"><h2>No ideas fetched yet</h2><p>Refresh to pull today's headlines and Wikipedia's traffic spikes.</p></div>`}`;
}

// ───────────────────────── episode ─────────────────────────

function renderEpisode() {
  const ep = state.ep;
  const s = state.status;
  const running = state.jobs.find((j) => j.slug === ep.slug && j.status === "running");
  const r = rank(ep.status);

  const stages = s.stages.map((st, i) => {
    const sr = rank(st.status);
    // "Verify facts" only counts as done when every flagged fact is ticked, even if the story moved on.
    const done = st.key === "verify" ? ep.verify_done >= ep.verify_total && sr <= r : sr <= r;
    const cls = !st.built ? "upcoming" : done ? "done" : sr === r + 1 || (st.key === "verify" && sr <= r) ? "current" : "";
    return `<li class="stage ${cls}"><span class="s-n">${i + 1}</span><span class="s-l">${esc(st.label)}</span></li>`;
  }).join("");

  const flags = state.draft ? beatFlags() : new Map();
  const lintCount = (ep.lint?.issues || []).filter((x) => !x.startsWith("Length")).length;
  const tabs = [
    ["facts", "Fact sheet", ep.verify_total ? `${ep.verify_done}/${ep.verify_total}` : ""],
    ["script", "Script", lintCount ? `${lintCount} to fix` : ""],
    ["shorts", "Shorts", state.draft ? String(state.draft.shorts.length) : ""],
    ["package", "Title and thumbnail", ""],
  ];

  $("#main").innerHTML = `
    <header class="ep-head">
      <div class="ep-title-row">
        <h1>${esc(ep.topic)}</h1>
        <button class="btn btn-quiet btn-danger" data-act="delete-ep">Delete story</button>
      </div>
      ${ep.meta?.angle ? `<p class="ep-angle">Angle: ${esc(ep.meta.angle)}</p>` : ""}
      <ol class="stages" aria-label="Production stages">${stages}</ol>
      ${nextStep(ep, running, flags)}
    </header>
    <div class="tabs" role="tablist">
      ${tabs.map(([k, label, badge]) => `
        <button class="tab" role="tab" aria-selected="${state.tab === k}" data-tab="${k}">
          ${label}${badge ? `<span class="badge ${k === "script" && lintCount ? "warn" : ""}">${badge}</span>` : ""}
        </button>`).join("")}
    </div>
    <section id="tabBody"></section>`;

  renderTab();
}

function nextStep(ep, running, flags) {
  if (running) return box(`<strong>${esc(running.label)}…</strong> Free-tier models usually take under a minute. You can leave this page.`);
  const verifyLeft = ep.verify_total - ep.verify_done;
  switch (ep.status) {
    case "new":
      return box(`<strong>Research didn't finish.</strong> Check the error in the sidebar, then try again.`,
        `<button class="btn btn-primary" data-act="rerun-research">Run research again</button>`);
    case "researched":
      return box(`<strong>Check ${verifyLeft} flagged ${verifyLeft === 1 ? "fact" : "facts"}.</strong> Confirm each one against the sources, then tick it off.`,
        state.tab !== "facts" ? `<button class="btn btn-primary" data-tab="facts">Open the fact sheet</button>` : "");
    case "verified":
      return box(`<strong>Facts are checked.</strong> Pick a story angle and write the script.`,
        state.tab !== "script" ? `<button class="btn btn-primary" data-tab="script">Write the script</button>` : "");
    case "scripted": {
      const ok = ep.lint?.ok && !state.dirty;
      const msg = state.dirty ? "<strong>You have unsaved edits.</strong> Save them to re-check the script."
        : ep.lint?.ok ? "<strong>Read it out loud.</strong> Fix anything that sounds off, then approve it for voice and visuals."
        : `<strong>${flags.size} ${flags.size === 1 ? "beat has" : "beats have"} problems marked in red.</strong> Fix them and save to approve.`;
      return box(msg, `<button class="btn btn-approve" data-act="approve" ${ok ? "" : "disabled"}>Approve script</button>`);
    }
    case "approved":
      return box(`<strong>Approved.</strong> Voice is the next stage to be built; this story will be first in line for it.`,
        `<button class="btn" data-act="unapprove">Reopen for edits</button>`);
    default:
      return "";
  }
  function box(text, actions = "") { return `<div class="next-step"><p>${text}</p>${actions}</div>`; }
}

function renderTab() {
  const body = $("#tabBody");
  if (state.tab === "facts") body.innerHTML = factsTab();
  else if (state.tab === "script") body.innerHTML = scriptTab();
  else if (state.tab === "shorts") body.innerHTML = shortsTab();
  else body.innerHTML = packageTab();
  $$("textarea.autosize", body).forEach(autosize);
}

// ───── fact sheet ─────

function factsTab() {
  const fs = state.ep.factsheet;
  if (!fs) {
    const running = state.jobs.some((j) => j.slug === state.ep.slug && j.status === "running");
    return `<div class="empty"><h2>${running ? "Researching…" : "No fact sheet yet"}</h2><p>${running ? "The fact sheet shows up here when research finishes." : "Run research to build one."}</p></div>`;
  }
  const checked = new Set(state.ep.meta?.verify_checked || []);
  const verify = fs.needs_verification || [];
  const src = (tag) => tag ? `<span class="src" title="${esc(fs.sources_index?.[tag] || "")}">${esc(tag)}</span>` : "";

  return `
  <div class="fs-grid">
    <div>
      <p class="fs-summary">${esc(fs.summary)}</p>

      ${fs.story_angles?.length ? `<div class="fs-block">
        <h2>Story angle</h2>
        <p class="muted" style="margin:-6px 0 12px">The script follows the one you pick.</p>
        ${angleChooser(fs.story_angles)}
      </div>` : ""}

      ${fs.timeline?.length ? `<div class="fs-block"><h2>Timeline</h2>
        <ol class="timeline">${fs.timeline.map((e) => `<li><span class="d">${esc(e.date)}</span><span>${esc(e.event)}</span>${src(e.source)}</li>`).join("")}</ol>
      </div>` : ""}

      ${fs.people?.length ? `<div class="fs-block"><h2>People</h2>
        <ul class="people">${fs.people.map((p) => `<li><b>${esc(p.name)}</b> <span class="muted">${esc(p.role)}</span></li>`).join("")}</ul>
      </div>` : ""}

      ${fs.key_numbers?.length ? `<div class="fs-block"><h2>Numbers</h2>
        <ul class="numbers">${fs.key_numbers.map((n) => `<li><span class="fig">${esc(n.figure)}</span>${esc(n.context)} ${src(n.source)}</li>`).join("")}</ul>
      </div>` : ""}

      ${fs.turning_points?.length ? `<div class="fs-block"><h2>Turning points</h2>
        <ul class="people">${fs.turning_points.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>
      </div>` : ""}

      <details class="fs-block json-edit">
        <summary class="btn btn-quiet" style="display:inline-flex">Correct the fact sheet</summary>
        <p class="hint" style="margin:12px 0 8px">Fix a wrong date or number here. The script writer only ever sees this fact sheet, so this is where corrections belong.</p>
        <textarea id="fsJson" spellcheck="false">${esc(JSON.stringify(stripFs(fs), null, 2))}</textarea>
        <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
          <button class="btn btn-primary" data-act="save-fs">Save corrections</button>
        </div>
        <div style="margin-top:26px">
          <label class="field"><span>Research again with more articles <em>(one per line)</em></span>
            <textarea id="rerunExtra" rows="2" placeholder="Stephen Elop&#10;Symbian"></textarea></label>
          <button class="btn" data-act="rerun-research">Research again</button>
        </div>
      </details>
    </div>

    <aside class="verify">
      <div class="verify-card">
        <h2>Check before scripting</h2>
        <p class="progress">${verify.length ? `${checked.size} of ${verify.length} confirmed` : "Nothing was flagged. Still skim the timeline."}</p>
        ${verify.length ? `<ul class="verify-list">${verify.map((v, i) => `
          <li><label><input type="checkbox" data-verify="${i}" ${checked.has(i) ? "checked" : ""}><span class="claim">${esc(v)}</span></label></li>`).join("")}
        </ul>` : ""}
        <details class="sources">
          <summary>Sources (${Object.keys(fs.sources_index || {}).length + (fs.external_links?.length || 0)})</summary>
          <ul>
            ${Object.entries(fs.sources_index || {}).map(([k, u]) => `<li>${esc(k)}: <a href="${esc(u)}" target="_blank" rel="noopener">${esc(u)}</a></li>`).join("")}
            ${(fs.external_links || []).map((u) => `<li><a href="${esc(u)}" target="_blank" rel="noopener">${esc(u)}</a></li>`).join("")}
          </ul>
        </details>
      </div>
    </aside>
  </div>`;
}

function stripFs(fs) {
  const { sources_index, external_links, ...rest } = fs;
  return rest;
}

function angleChooser(angles) {
  const custom = state.angleChoice && !angles.includes(state.angleChoice) ? state.angleChoice : "";
  return `<div class="angles">
    ${angles.map((a) => `<label class="angle"><input type="radio" name="angle" value="${esc(a)}" ${state.angleChoice === a ? "checked" : ""}><span>${esc(a)}</span></label>`).join("")}
    <label class="angle"><input type="radio" name="angle" value="__custom" ${custom ? "checked" : ""}>
      <input type="text" id="customAngle" placeholder="Or write your own angle" value="${esc(custom)}" style="padding:4px 8px"></label>
  </div>`;
}

// ───── script ─────

function beatFlags() {
  // beat id → list of problems. Banned phrases are checked live; number checks come from the last save.
  const flags = new Map();
  const banned = state.status.banned_phrases || [];
  const add = (id, msg) => { if (!flags.has(id)) flags.set(id, []); flags.get(id).push(msg); };
  state.draft.chapters.forEach((ch, ci) => ch.beats.forEach((b, bi) => {
    const id = beatId(ci, bi);
    const low = b.narration.toLowerCase();
    banned.forEach((p) => { if (low.includes(p)) add(id, `sounds like AI: “${p}”`); });
  }));
  const numIssue = (state.ep.lint?.issues || []).find((x) => x.startsWith("Numbers not found"));
  if (numIssue && !state.dirty) {
    for (const m of numIssue.matchAll(/(c\d+b\d+) '([^']+)'/g)) add(m[1], `not in fact sheet: ${m[2]}`);
  }
  return flags;
}
const beatId = (ci, bi) => `c${ci}b${String(bi + 1).padStart(2, "0")}`;

function scriptStats() {
  let w = 0, sec = 0, n = 0;
  state.draft.chapters.forEach((ch) => ch.beats.forEach((b) => { w += words(b.narration); sec += beatSeconds(b); n++; }));
  return { w, sec, n };
}

function scriptBar() {
  const { w, sec, n } = scriptStats();
  const target = state.status.script_cfg.long_minutes;
  return `<div class="script-bar">
    <div class="stats">
      <span class="stat"><b id="stRun">${clock(sec)}</b> runtime <span class="muted">(target ${target}:00)</span></span>
      <span class="stat"><b id="stWords">${w.toLocaleString()}</b> words</span>
      <span class="stat"><b>${n}</b> beats</span>
      <span class="dirty" id="dirtyFlag" ${state.dirty ? "" : "hidden"}>Unsaved edits</span>
    </div>
    <button class="btn" data-act="discard" ${state.dirty ? "" : "hidden"}>Discard</button>
    <button class="btn btn-primary" data-act="save-script" id="saveBtn" ${state.dirty ? "" : "disabled"}>Save changes</button>
  </div>`;
}

function scriptTab() {
  const ep = state.ep;
  const running = state.jobs.some((j) => j.slug === ep.slug && j.status === "running");
  if (running && !state.draft) return `<div class="empty"><h2>Writing…</h2><p>The script appears here when it's done.</p></div>`;
  if (!ep.factsheet) return `<div class="empty"><h2>Research comes first</h2><p>The script is written only from the fact sheet, so that has to exist first.</p></div>`;
  if (!state.draft || state.showGen) return genForm();

  const flags = beatFlags();
  const issues = (ep.lint?.issues || []).filter((x) => !x.startsWith("Length"));
  const lengthNote = (ep.lint?.issues || []).find((x) => x.startsWith("Length"));
  let t = 0;

  return `
    ${scriptBar()}
    ${issues.length ? `<ul class="lint-list">${issues.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`
      : `<div class="lint-ok">No AI phrases, and every number traces back to the fact sheet.</div>`}
    ${lengthNote ? `<p class="lint-note">${esc(lengthNote)}. Not a blocker, but aim close to the target.</p>` : ""}
    <div class="paper" id="paper">
      <div class="paper-cols"><div>Time</div><div>Picture</div><div>Narration</div><div></div></div>
      ${state.draft.chapters.map((ch, ci) => `
        <div class="chapter-head">
          <span class="cn">${ci === 0 ? "Open" : `Ch ${ci}`}</span>
          <input type="text" value="${esc(ch.title)}" data-ch="${ci}" data-field="title" aria-label="Chapter title">
          <span class="ch-tools">${state.draft.chapters.length > 2 ? `<button class="icon-btn del" data-act="del-chapter" data-ch="${ci}" title="Delete chapter" aria-label="Delete chapter">${ICON.del}</button>` : ""}</span>
        </div>
        ${ch.beats.map((b, bi) => {
          const id = beatId(ci, bi);
          const start = t; t += beatSeconds(b);
          const f = flags.get(id) || [];
          return `<div class="beat ${f.length ? "flagged" : ""}" data-ci="${ci}" data-bi="${bi}">
            <div class="tc"><span class="start">${clock(start)}</span><span class="id">${id}</span></div>
            <div class="pic">
              <select data-field="vtype" style="--vcolor: var(--v-${b.visual.type})" aria-label="Shot type">
                ${Object.entries(VISUALS).map(([k, v]) => `<option value="${k}" ${k === b.visual.type ? "selected" : ""}>${v}</option>`).join("")}
              </select>
              <input type="text" data-field="vquery" value="${esc(b.visual.query)}" placeholder="${esc(VISUAL_HINT[b.visual.type])}" aria-label="What to show">
              <input type="text" class="l3" data-field="l3" value="${esc(b.on_screen_text || "")}" placeholder="Lower third (optional)" aria-label="Lower third">
            </div>
            <div class="vo">
              <textarea class="autosize" data-field="narration" rows="2" aria-label="Narration">${esc(b.narration)}</textarea>
              ${f.length ? `<div class="marks">${f.map((m) => `<span class="mark">${esc(m)}</span>`).join("")}</div>` : ""}
              <div class="meta"><span class="dur">${Math.round(beatSeconds(b))}s</span>
                <label>pause <input type="number" min="0" max="3" step="0.1" data-field="pause" value="${b.pause_after}"> s</label></div>
            </div>
            <div class="tools">
              <button class="icon-btn" data-act="up" title="Move up" aria-label="Move beat up">${ICON.up}</button>
              <button class="icon-btn" data-act="down" title="Move down" aria-label="Move beat down">${ICON.down}</button>
              <button class="icon-btn del" data-act="del-beat" title="Delete beat" aria-label="Delete beat">${ICON.del}</button>
            </div>
          </div>`;
        }).join("")}
        <div class="add-row"><button class="btn btn-quiet" data-act="add-beat" data-ch="${ci}">Add beat</button></div>
      `).join("")}
      <div class="add-chapter"><button class="btn btn-quiet" data-act="add-chapter">Add chapter</button></div>
    </div>
    <p style="margin-top:28px"><button class="btn" data-act="show-gen">Rewrite from scratch</button></p>`;
}

function genForm() {
  const fs = state.ep.factsheet;
  const hasDraft = !!state.draft;
  const unverified = (state.ep.verify_total - state.ep.verify_done);
  return `<div class="gen-form">
    <h2 style="margin-bottom:8px">${hasDraft ? "Rewrite the script" : "Write the script"}</h2>
    <p class="lede" style="margin-bottom:22px">${hasDraft ? "This replaces the current script, including your edits." : "Written from the fact sheet only. Your angle and notes shape it; your notes are also what makes it yours."}</p>
    ${unverified > 0 ? `<p class="lint-list" style="list-style:none;padding-left:14px">${unverified} flagged ${unverified === 1 ? "fact is" : "facts are"} still unchecked. You can write now, but check them before approving.</p>` : ""}
    <div class="field"><span>Story angle</span>${angleChooser(fs.story_angles || [])}</div>
    <label class="field"><span>Your take <em>(optional, strongly recommended)</em></span>
      <textarea id="genNotes" rows="4" placeholder="What do you think the real lesson is? A detail you find wild? Say it here and it gets worked into the narration.">${esc(state.ep.meta?.notes || "")}</textarea></label>
    <div style="display:flex;gap:8px">
      <button class="btn btn-primary" data-act="generate">${hasDraft ? "Rewrite script" : "Write script"}</button>
      ${hasDraft ? `<button class="btn" data-act="hide-gen">Keep current script</button>` : ""}
    </div>
  </div>`;
}

// ───── Shorts ─────

function shortsTab() {
  if (!state.draft) return `<div class="empty"><h2>No Shorts yet</h2><p>The script marks the moments that work as Shorts. Write the script first.</p></div>`;
  const d = state.draft;
  return `${scriptBar()}
    <p class="lede" style="margin:0 0 24px">Each Short reuses a stretch of the long video, opened with a new hook line. They'll be cut vertical once the render stage exists.</p>
    ${d.shorts.length ? `<div class="shorts">${d.shorts.map((s, i) => {
      const ch = d.chapters[s.chapter];
      const beats = ch ? ch.beats.slice(s.beats[0] - 1, s.beats[1]) : [];
      const sec = beats.reduce((a, b) => a + beatSeconds(b), 0) + 3;
      return `<div class="short">
        <div class="phone"><span class="len">~${Math.round(sec)}s</span>
          <div class="hook">${esc(s.hook)}</div>
          <div class="body">${beats.map((b) => esc(b.narration)).join(" ")}</div>
        </div>
        <textarea class="autosize" rows="2" data-short="${i}" aria-label="Hook line">${esc(s.hook)}</textarea>
        <p class="why">${ch ? `${esc(ch.title)}, beats ${s.beats[0]}–${s.beats[1]}. ` : ""}${esc(s.why || "")}</p>
      </div>`;
    }).join("")}</div>` : `<div class="empty"><p>The script didn't flag any Shorts.</p></div>`}`;
}

// ───── title + thumbnail ─────

function packageTab() {
  if (!state.draft) return `<div class="empty"><h2>Nothing to package yet</h2><p>Titles, thumbnail text and the description come with the script.</p></div>`;
  const d = state.draft;
  const title = d.chosen_title || d.title_options[0] || "";
  const thumb = d.chosen_thumbnail || d.thumbnail_text_options[0] || "";
  const choice = (name, opts, cur) => opts.map((o) => `
    <label class="angle"><input type="radio" name="${name}" value="${esc(o)}" ${o === cur ? "checked" : ""}><span>${esc(o)}</span></label>`).join("");
  return `${scriptBar()}
  <div class="pack">
    <div>
      <h2 style="margin-bottom:12px">Title</h2>
      <div class="pick-list">${choice("pickTitle", d.title_options, title)}
        <input type="text" id="ownTitle" placeholder="Or write your own" value="${esc(d.title_options.includes(title) ? "" : title)}" maxlength="100">
      </div>
      <h2 style="margin-bottom:12px">Description</h2>
      <textarea class="autosize" id="descBox" rows="5">${esc(d.description)}</textarea>
      <h2 style="margin:26px 0 8px">Tags</h2>
      <input type="text" id="tagBox" value="${esc(d.tags.join(", "))}" placeholder="Comma separated">
      <div class="tags">${d.tags.map((t) => `<span class="tag">${esc(t)}</span>`).join("")}</div>
    </div>
    <div>
      <h2 style="margin-bottom:12px">Thumbnail text</h2>
      <div class="thumb-preview" aria-hidden="true"><span id="thumbPreview">${esc(thumb)}</span></div>
      <p class="hint" style="margin-bottom:14px">Rough preview of the text only. The real thumbnail gets the strongest archive photo behind it.</p>
      <div class="pick-list">${choice("pickThumb", d.thumbnail_text_options, thumb)}
        <input type="text" id="ownThumb" placeholder="Or write your own (2–4 words)" value="${esc(d.thumbnail_text_options.includes(thumb) ? "" : thumb)}" maxlength="40">
      </div>
    </div>
  </div>`;
}

// ───────────────────────── editing ─────────────────────────

function markDirty() {
  if (!state.dirty) {
    state.dirty = true;
    const f = $("#dirtyFlag"); if (f) f.hidden = false;
    const b = $("#saveBtn"); if (b) b.disabled = false;
    $$('[data-act="discard"]').forEach((x) => (x.hidden = false));
    const appr = $('[data-act="approve"]'); if (appr) appr.disabled = true;
  }
}

function refreshTimings() {
  let t = 0;
  $$("#paper .beat").forEach((row) => {
    const b = state.draft.chapters[+row.dataset.ci].beats[+row.dataset.bi];
    $(".start", row).textContent = clock(t);
    const s = beatSeconds(b); t += s;
    $(".dur", row).textContent = `${Math.round(s)}s`;
  });
  const st = scriptStats();
  if ($("#stRun")) $("#stRun").textContent = clock(st.sec);
  if ($("#stWords")) $("#stWords").textContent = st.w.toLocaleString();
}

function refreshBeatMarks(row) {
  const ci = +row.dataset.ci, bi = +row.dataset.bi;
  const f = beatFlags().get(beatId(ci, bi)) || [];
  row.classList.toggle("flagged", f.length > 0);
  let marks = $(".marks", row);
  if (!f.length) { marks?.remove(); return; }
  if (!marks) { marks = document.createElement("div"); marks.className = "marks"; $(".meta", row).before(marks); }
  marks.innerHTML = f.map((m) => `<span class="mark">${esc(m)}</span>`).join("");
}

function autosize(el) { el.style.height = "auto"; el.style.height = el.scrollHeight + 2 + "px"; }

document.addEventListener("input", (e) => {
  const el = e.target;
  if (el.classList.contains("autosize")) autosize(el);
  if (!state.draft) return;

  const row = el.closest(".beat");
  if (row && el.dataset.field) {
    const b = state.draft.chapters[+row.dataset.ci].beats[+row.dataset.bi];
    const f = el.dataset.field;
    if (f === "narration") { b.narration = el.value; refreshTimings(); refreshBeatMarks(row); }
    else if (f === "vquery") b.visual.query = el.value;
    else if (f === "l3") b.on_screen_text = el.value.trim() || null;
    else if (f === "pause") { b.pause_after = Math.min(3, Math.max(0, parseFloat(el.value) || 0)); refreshTimings(); }
    else if (f === "vtype") { b.visual.type = el.value; el.style.setProperty("--vcolor", `var(--v-${el.value})`); $('[data-field="vquery"]', row).placeholder = VISUAL_HINT[el.value]; }
    markDirty();
    return;
  }
  if (el.dataset.ch !== undefined && el.dataset.field === "title") { state.draft.chapters[+el.dataset.ch].title = el.value; markDirty(); return; }
  if (el.dataset.short !== undefined) {
    const i = +el.dataset.short;
    state.draft.shorts[i].hook = el.value;
    el.closest(".short").querySelector(".hook").textContent = el.value;
    markDirty(); return;
  }
  if (el.id === "descBox") { state.draft.description = el.value; markDirty(); }
  if (el.id === "tagBox") { state.draft.tags = el.value.split(",").map((t) => t.trim()).filter(Boolean); markDirty(); }
  if (el.id === "ownTitle" && el.value.trim()) { state.draft.chosen_title = el.value.trim(); $$('[name="pickTitle"]').forEach((r) => (r.checked = false)); markDirty(); }
  if (el.id === "ownThumb" && el.value.trim()) { state.draft.chosen_thumbnail = el.value.trim(); $$('[name="pickThumb"]').forEach((r) => (r.checked = false)); $("#thumbPreview").textContent = el.value; markDirty(); }
  if (el.id === "customAngle") { state.angleChoice = el.value.trim() || null; const r = $('input[name="angle"][value="__custom"]'); if (r) r.checked = true; }
});

document.addEventListener("change", async (e) => {
  const el = e.target;
  if (el.name === "angle") state.angleChoice = el.value === "__custom" ? ($("#customAngle")?.value.trim() || null) : el.value;
  if (el.name === "pickTitle") { state.draft.chosen_title = el.value; $("#ownTitle").value = ""; markDirty(); }
  if (el.name === "pickThumb") { state.draft.chosen_thumbnail = el.value; $("#ownThumb").value = ""; $("#thumbPreview").textContent = el.value; markDirty(); }
  if (el.dataset.verify !== undefined) {
    const checked = $$("[data-verify]").filter((c) => c.checked).map((c) => +c.dataset.verify);
    try {
      const summary = await api(`/api/episodes/${state.ep.slug}/verify`, { method: "PUT", body: { checked } });
      Object.assign(state.ep, summary);
      state.ep.meta.verify_checked = checked;
      state.episodes = await api("/api/episodes");
      if (summary.status === "verified" && checked.length === summary.verify_total) toast("All facts checked");
      renderEpisode();
    } catch (err) { toast(err.message, true); }
  }
});

function newBeat() {
  return { id: "", narration: "", visual: { type: "broll", query: "", note: null }, on_screen_text: null, pause_after: 0.3 };
}

// Structural edits keep Shorts pointing at the same beats where possible.
function shiftShorts(ci, fromBi, delta) {
  state.draft.shorts = state.draft.shorts.filter((s) => {
    if (s.chapter !== ci) return true;
    let [a, b] = s.beats;
    if (delta < 0 && fromBi + 1 >= a && fromBi + 1 <= b) { b -= 1; if (b < a) return false; }
    else if (fromBi + 1 < a) { a += delta; b += delta; }
    else if (delta > 0 && fromBi + 1 <= b) { b += delta; }
    s.beats = [a, b];
    return a >= 1;
  });
}

document.addEventListener("click", async (e) => {
  const tabBtn = e.target.closest("[data-tab]");
  if (tabBtn) {
    state.tab = tabBtn.dataset.tab;
    history.replaceState(null, "", `#/e/${state.ep.slug}/${state.tab}`);
    currentHash = location.hash;
    renderEpisode();
    return;
  }
  const btn = e.target.closest("[data-act]");
  if (!btn) return;
  const act = btn.dataset.act;
  const slug = state.ep?.slug;
  const row = btn.closest(".beat");

  try {
    switch (act) {
      case "new": openNew(); break;

      case "refresh-topics":
        track(await api("/api/topics/refresh", { method: "POST" }));
        await poll(); renderTopics(); break;

      case "research-topic": openNew(state.topics.items[+btn.dataset.i].title); break;

      case "rerun-research": {
        const extra = ($("#rerunExtra")?.value || "").split("\n");
        track(await api(`/api/episodes/${slug}/research`, { method: "POST", body: { topic: state.ep.topic, extra } }));
        await poll(); renderEpisode(); break;
      }

      case "save-fs": {
        let fs;
        try { fs = JSON.parse($("#fsJson").value); } catch (err) { toast(`That isn't valid JSON: ${err.message}`, true); return; }
        state.ep = await api(`/api/episodes/${slug}/factsheet`, { method: "PUT", body: { ...fs, sources_index: state.ep.factsheet.sources_index, external_links: state.ep.factsheet.external_links } });
        toast("Corrections saved"); renderEpisode(); break;
      }

      case "show-gen":
        if (state.dirty && !confirm("Rewriting throws away your unsaved edits. Continue?")) return;
        state.showGen = true; renderTab(); break;
      case "hide-gen": state.showGen = false; renderTab(); break;

      case "generate": {
        const notes = $("#genNotes").value.trim() || null;
        track(await api(`/api/episodes/${slug}/script`, { method: "POST", body: { angle: state.angleChoice, notes } }));
        state.showGen = false; state.dirty = false;
        state.draft = null;
        state.ep.meta.angle = state.angleChoice; state.ep.meta.notes = notes;
        await poll(); renderEpisode(); break;
      }

      case "save-script": {
        btn.disabled = true;
        state.draft.chapters.forEach((ch) => ch.beats.forEach((b) => { if (!b.visual.query.trim()) b.visual.query = "—"; }));
        state.ep = await api(`/api/episodes/${slug}/script`, { method: "PUT", body: state.draft });
        state.draft = structuredClone(state.ep.script);
        state.dirty = false;
        state.episodes = await api("/api/episodes");
        toast(state.ep.lint.ok ? "Saved. Script is clean." : "Saved. Some problems still marked.");
        render(); break;
      }

      case "discard":
        if (!confirm("Throw away your unsaved edits?")) return;
        state.draft = structuredClone(state.ep.script); state.dirty = false; renderEpisode(); break;

      case "approve":
        state.ep = await api(`/api/episodes/${slug}/approve`, { method: "POST" });
        state.episodes = await api("/api/episodes");
        toast("Script approved"); render(); break;

      case "unapprove":
        state.ep = await api(`/api/episodes/${slug}/unapprove`, { method: "POST" });
        state.episodes = await api("/api/episodes"); render(); break;

      case "delete-ep":
        if (!confirm(`Delete "${state.ep.topic}" and everything in it? This can't be undone.`)) return;
        await api(`/api/episodes/${slug}`, { method: "DELETE" });
        state.ep = null; state.dirty = false;
        state.episodes = await api("/api/episodes");
        location.hash = "#/"; toast("Story deleted"); break;

      case "up": case "down": {
        const ci = +row.dataset.ci, bi = +row.dataset.bi;
        const beats = state.draft.chapters[ci].beats;
        const to = act === "up" ? bi - 1 : bi + 1;
        if (to < 0 || to >= beats.length) return;
        [beats[bi], beats[to]] = [beats[to], beats[bi]];
        markDirty(); renderTab(); break;
      }
      case "del-beat": {
        const ci = +row.dataset.ci, bi = +row.dataset.bi;
        const beats = state.draft.chapters[ci].beats;
        if (beats.length === 1) { toast("A chapter needs at least one beat. Delete the chapter instead.", true); return; }
        beats.splice(bi, 1); shiftShorts(ci, bi, -1);
        markDirty(); renderTab(); break;
      }
      case "add-beat": {
        const ci = +btn.dataset.ch;
        state.draft.chapters[ci].beats.push(newBeat());
        markDirty(); renderTab();
        const rows = $$(`#paper .beat[data-ci="${ci}"]`);
        $('[data-field="narration"]', rows[rows.length - 1])?.focus();
        break;
      }
      case "add-chapter":
        state.draft.chapters.push({ title: "New chapter", beats: [newBeat()] });
        markDirty(); renderTab(); break;
      case "del-chapter": {
        const ci = +btn.dataset.ch;
        if (!confirm(`Delete chapter "${state.draft.chapters[ci].title}" and its beats?`)) return;
        state.draft.chapters.splice(ci, 1);
        state.draft.shorts = state.draft.shorts.filter((s) => s.chapter !== ci).map((s) => ({ ...s, chapter: s.chapter > ci ? s.chapter - 1 : s.chapter }));
        markDirty(); renderTab(); break;
      }
    }
  } catch (err) {
    toast(err.message, true);
    if (act === "save-script") btn.disabled = false;
  }
});

// ───────────────────────── new story dialog ─────────────────────────

function openNew(topic = "") {
  $("#newTopic").value = topic;
  $("#newExtra").value = "";
  $("#newDialog").showModal();
  $("#newTopic").focus();
}

$("#newStoryBtn").addEventListener("click", () => openNew());
$("#newCancel").addEventListener("click", () => $("#newDialog").close());
$("#newForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const topic = $("#newTopic").value.trim();
  if (!topic) return;
  try {
    const res = await api("/api/episodes", { method: "POST", body: { topic, extra: $("#newExtra").value.split("\n") } });
    $("#newDialog").close();
    track(res.job);
    state.episodes = await api("/api/episodes");
    await poll();
    state.ep = null;
    location.hash = `#/e/${res.slug}/facts`;
  } catch (err) { toast(err.message, true); }
});

$("#railToggle").addEventListener("click", () => {
  const open = $("#rail").classList.toggle("open");
  $("#railToggle").setAttribute("aria-expanded", String(open));
});

window.addEventListener("beforeunload", (e) => { if (state.dirty) { e.preventDefault(); e.returnValue = ""; } });

// ───────────────────────── boot ─────────────────────────

(async function boot() {
  try {
    [state.status, state.episodes, state.jobs] = await Promise.all([api("/api/status"), api("/api/episodes"), api("/api/jobs")]);
  } catch {
    $("#main").innerHTML = `<div class="empty"><h2>Can't reach the server</h2><p>Start it with <code>python app.py</code> and reload.</p></div>`;
    return;
  }
  lastJobs = new Map(state.jobs.map((j) => [j.id, j.status]));
  renderJobs();
  await route();
  setInterval(poll, 1500);
  setInterval(() => { if (state.jobs.some((j) => j.status === "running")) renderJobs(); }, 1000);
})();
