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
  sdraft: null,    // editable copy of ep.shorts (production view)
  sdirty: false,
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
  if (res.status === 401 && path !== "/api/login") { location.href = "/login"; throw new Error("Signed out"); }
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
  voiced: "Pick the pictures",
  visuals_ready: "Ready to render",
  rendered: "Video ready",
};

const RENDER_LINE = { queued: "Waiting to render", running: "Rendering…", error: "Render failed" };

const VOICE_LINE = { queued: "Waiting for the narrator", running: "Narrating…", error: "Narration failed" };

function statusOrder() {
  return ["new", ...state.status.stages.map((s) => s.status)];
}
const rank = (status) => Math.max(0, statusOrder().indexOf(status));

function epLine(e) {
  if (e.running) return e.status === "new" ? "Researching…" : "Working…";
  if (e.status === "researched" && e.verify_total) return `${e.verify_done} of ${e.verify_total} facts checked`;
  if (e.status === "approved" && VOICE_LINE[e.voice_state]) return VOICE_LINE[e.voice_state];
  if (e.status === "visuals_ready" && RENDER_LINE[e.render_state]) return RENDER_LINE[e.render_state];
  if (["scripted", "approved", "voiced"].includes(e.status)) return `${STATUS_TEXT[e.status]}${e.est_minutes ? ` · ${e.est_minutes} min` : ""}`;
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
  $("#signOut").hidden = !s?.auth;
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
    // Finished since last look: was running before, or is new and already done (fast jobs
    // can start and finish between two checks).
    const finished = jobs.filter((j) => j.status !== "running" &&
      (lastJobs.has(j.id) ? lastJobs.get(j.id) === "running" : true));
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
    await watchVoice();
  } catch { /* server restarting; try again next tick */ }
}

// Narration and renders run on the GitHub worker, not in this server's job list,
// so their state is read from the episode list (and the open story, for render progress).
let lastWorkerCheck = 0;
const workerState = (e) => `${e.voice_state}|${e.render_state}|${e.illustrate_state}|${e.shorts_state}|${e.shorts_made}|${e.youtube_state}|${e.uploaded}`;
async function watchVoice() {
  const busy = (e) => [e.voice_state, e.render_state, e.illustrate_state, e.shorts_state, e.youtube_state].some((s) => ["queued", "running", "waiting"].includes(s));
  if (!state.episodes.some(busy) || Date.now() - lastWorkerCheck < 8000) return;
  lastWorkerCheck = Date.now();
  const before = new Map(state.episodes.map((e) => [e.slug, workerState(e)]));
  state.episodes = await api("/api/episodes");
  const changed = state.episodes.filter((e) => before.get(e.slug) !== workerState(e));
  const route = parseRoute();
  const openRendering = route.view === "episode" && [state.ep?.meta?.render?.state, state.ep?.meta?.shorts?.state, state.ep?.meta?.youtube?.state].includes("running");
  if (!changed.length) {
    // keep the progress bar moving on the story you're looking at
    if (openRendering && !state.dirty && !state.sdirty) await loadEpisode(route.slug, true);
    return;
  }
  renderRail();
  for (const e of changed) {
    const was = (before.get(e.slug) || "").split("|");
    if (e.voice_state === "done" && was[0] !== "done") toast(`Narration ready: ${e.topic}`);
    if (e.voice_state === "error" && was[0] !== "error") toast(`Narration failed: ${e.topic}`, true);
    if (e.render_state === "done" && was[1] !== "done") toast(`Video rendered: ${e.topic}`);
    if (e.render_state === "error" && was[1] !== "error") toast(`Render failed: ${e.topic}`, true);
    if (e.illustrate_state === "done" && was[2] !== "done") toast(`Illustrations ready: ${e.topic}`);
    if (e.illustrate_state === "error" && was[2] !== "error") toast(`Illustrations failed: ${e.topic}`, true);
    if (e.shorts_state === "done" && (was[3] !== "done" || was[4] !== String(e.shorts_made))) toast(`Shorts ready: ${e.topic}`);
    if (e.shorts_state === "error" && was[3] !== "error") toast(`Shorts failed: ${e.topic}`, true);
    if (e.youtube_state === "done" && (was[5] !== "done" || was[6] !== String(e.uploaded))) toast(`Uploaded to YouTube: ${e.topic}`);
    if (e.youtube_state === "error" && was[5] !== "error") toast(`YouTube upload failed: ${e.topic}`, true);
  }
  if (route.view === "episode" && changed.some((e) => e.slug === route.slug) && !state.dirty && !state.sdirty) await loadEpisode(route.slug, true);
  else if (route.view === "desk") render();
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
  if (leavingEpisode) { state.dirty = false; stopAudio(); }
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
  state.sdraft = state.ep.shorts ? structuredClone(state.ep.shorts) : null;
  state.sdirty = false;
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
    { title: "Research", what: "Sources pulled into a fact sheet. Tick off each flagged claim.", match: (e) => ["new", "researched"].includes(e.status) },
    { title: "Write the script", what: "Facts are solid. Pick an angle and write it.", match: (e) => e.status === "verified" },
    { title: "Review and approve", what: "Read it out loud, fix what sounds off, approve.", match: (e) => e.status === "scripted" },
    { title: "Narration", what: "Approved scripts get recorded by the narrator.", match: (e) => e.status === "approved" },
    { title: "Pick pictures", what: "Narration's done. Check the picture for every beat.", match: (e) => e.status === "voiced" },
    { title: "Render", what: "Narration and pictures cut into the video.", match: (e) => e.status === "visuals_ready" },
    { title: "Video ready", what: "Watch it, download it. Upload is the next stage.", match: (e) => e.status === "rendered" },
  ];
  const needYou = eps.filter((e) => !e.running && (["researched", "verified", "scripted", "voiced"].includes(e.status)
    || (e.status === "approved" && ["error", "cancelled", undefined, null].includes(e.voice_state))
    || (e.status === "visuals_ready" && !["queued", "running"].includes(e.render_state)))).length;

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
    let done = st.key === "verify" ? ep.verify_done >= ep.verify_total && sr <= r : sr <= r;
    let current = sr === r + 1 || (st.key === "verify" && sr <= r);
    if (st.key === "shorts") {  // done once any is made, open as soon as pictures are approved
      done = ep.shorts_made > 0;
      current = !done && ["visuals_ready", "rendered"].includes(ep.status);
    } else if (st.key === "upload") {
      done = ep.uploaded > 0;
      current = !done && (ep.status === "rendered" || ep.shorts_made > 0);
    }
    const cls = !st.built ? "upcoming" : done ? "done" : current ? "current" : "";
    return `<li class="stage ${cls}"><span class="s-n">${i + 1}</span><span class="s-l">${esc(st.label)}</span></li>`;
  }).join("");

  const flags = state.draft ? beatFlags() : new Map();
  const lintCount = (ep.lint?.issues || []).filter((x) => !x.startsWith("Length")).length;
  const tabs = [
    ["facts", "Fact sheet", ep.verify_total ? `${ep.verify_done}/${ep.verify_total}` : ""],
    ["script", "Script", lintCount ? `${lintCount} to fix` : ""],
    ["shorts", "Shorts", ep.shorts_made ? `${ep.shorts_made} made` : state.draft ? String(state.draft.shorts.length) : ""],
    ["visuals", "Pictures", ep.visuals ? (ep.visuals.summary.needs_you ? `${ep.visuals.summary.needs_you} need you` : "✓") : ""],
    ["package", "Title and thumbnail", ""],
    ...(ep.meta?.render?.state === "done" ? [["video", "Video", "✓"]] : []),
    ...(ep.youtube ? [["youtube", "YouTube", ep.uploaded ? `${ep.uploaded} up` : ""]] : []),
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
          ${label}${badge ? `<span class="badge ${(k === "script" && lintCount) || (k === "visuals" && ep.visuals?.summary.needs_you) ? "warn" : ""}">${badge}</span>` : ""}
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
      return box(msg, `${voicePick()}<button class="btn btn-approve" data-act="approve" ${ok ? "" : "disabled"}>Approve and narrate</button>`);
    }
    case "approved": {
      const job = ep.meta?.voice || {};
      if (job.state === "queued") {
        const when = state.status.worker === "every 10 minutes"
          ? "The narrator checks for work every 10 minutes, so it starts within about 15."
          : "The narrator is starting now.";
        return box(`<strong>Queued for narration with ${esc(voiceName(job.voice))}.</strong> ${when} You can close this page.`,
          `${ep.visuals ? "" : `<button class="btn" data-act="find-visuals">Find pictures meanwhile</button>`}
           <button class="btn" data-act="cancel-voice">Cancel</button>`);
      }
      if (job.state === "running") {
        return box(`<strong>Recording the narration with ${esc(voiceName(job.voice))}…</strong> About 8 minutes for a 14-minute script. This page updates when it's done.`,
          ep.visuals ? "" : `<button class="btn" data-act="find-visuals">Find pictures meanwhile</button>`);
      }
      if (job.state === "error") {
        return box(`<strong>Narration failed.</strong> ${esc(job.error || "")}`,
          `${voicePick()}<button class="btn btn-primary" data-act="record-voice">Try again</button>`);
      }
      return box(`<strong>Approved.</strong> Record the narration when you're ready.`,
        `${voicePick()}<button class="btn btn-primary" data-act="record-voice">Record narration</button>
         <button class="btn" data-act="unapprove">Reopen for edits</button>`);
    }
    case "voiced": {
      const v = ep.voice || {};
      const said = `<strong>Narration recorded:</strong> ${clock(v.duration || 0)} with ${esc(voiceName(v.voice))}.`;
      if (!ep.visuals) {
        return box(`${said} Next, find a real picture for every beat.`,
          `<button class="btn btn-primary" data-act="find-visuals">Find pictures</button>
           ${voicePick()}<button class="btn" data-act="record-voice">Re-record</button>`);
      }
      const n = ep.visuals.summary.needs_you;
      return box(`${said} ${n ? `<strong>${n} ${n === 1 ? "beat needs" : "beats need"} you</strong> on the Pictures tab: a real screenshot, or a better search.` : "Every beat has a picture. Look through them, then approve."}`,
        `${state.tab !== "visuals" ? `<button class="btn" data-tab="visuals">Open pictures</button>` : ""}
         <button class="btn btn-approve" data-act="approve-visuals">Approve pictures</button>`);
    }
    case "visuals_ready": {
      const job = ep.meta?.render || {};
      if (job.state === "queued") {
        const when = state.status.worker === "every 10 minutes"
          ? "The worker checks every 10 minutes, so it starts within about 15." : "It's starting now.";
        return box(`<strong>Queued to render.</strong> ${when} A 15-minute video takes about 15–25 minutes on the free machine. You can close this page.`,
          `<button class="btn" data-act="cancel-render">Cancel</button>`);
      }
      if (job.state === "running") {
        const pct = job.progress || 0;
        return box(`<strong>Rendering…</strong> ${pct}% of the shots done. This page updates by itself.
          <span class="progress-bar" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"><span style="width:${pct}%"></span></span>`);
      }
      if (job.state === "error") {
        return box(`<strong>Render failed.</strong> ${esc(job.error || "")}`,
          `<button class="btn btn-primary" data-act="render">Try again</button>
           <button class="btn" data-act="reopen-visuals">Change pictures</button>`);
      }
      return box(`<strong>Narration and pictures approved.</strong> Cut them into the finished video: Ken Burns moves on the photos, lower thirds, one grade, captions.`,
        `<button class="btn btn-primary" data-act="render">Render the video</button>
         ${state.tab !== "shorts" ? `<button class="btn" data-tab="shorts">Make Shorts</button>` : ""}
         <button class="btn" data-act="reopen-visuals">Change pictures</button>`);
    }
    case "rendered": {
      const r = ep.meta?.render || {};
      return box(`<strong>Video rendered:</strong> ${clock(r.duration || 0)}, ${r.size_mb || "?"} MB, ${r.shots || "?"} shots${r.fallbacks ? `, ${r.fallbacks} used a card because their picture wouldn't load` : ""}. Upload it from the YouTube tab.`,
        `${state.tab !== "video" ? `<button class="btn btn-primary" data-tab="video">Watch the preview</button>` : ""}
         ${downloadLink(r, "btn")}
         ${state.tab !== "shorts" ? `<button class="btn" data-tab="shorts">${ep.shorts_made ? "Shorts" : "Make Shorts"}</button>` : ""}
         ${state.tab !== "youtube" ? `<button class="btn" data-tab="youtube">YouTube</button>` : ""}
         <button class="btn" data-act="reopen-visuals">Change pictures</button>`);
    }
    default:
      return "";
  }
  function box(text, actions = "") { return `<div class="next-step"><p>${text}</p>${actions}</div>`; }
}

function voiceName(id) { return (state.status.voices || {})[id] || id || "the default voice"; }

function voicePick() {
  const cur = state.ep?.meta?.voice?.voice || state.status.voice_default;
  return `<label class="voice-pick"><span class="sr-only">Narrator voice</span>
    <select id="voicePick" aria-label="Narrator voice">
      ${Object.entries(state.status.voices || {}).map(([k, v]) => `<option value="${k}" ${k === cur ? "selected" : ""}>${esc(v)}</option>`).join("")}
    </select></label>`;
}

function renderTab() {
  const body = $("#tabBody");
  if (state.tab === "facts") body.innerHTML = factsTab();
  else if (state.tab === "script") body.innerHTML = scriptTab();
  else if (state.tab === "shorts") body.innerHTML = shortsTab();
  else if (state.tab === "visuals") body.innerHTML = visualsTab();
  else if (state.tab === "video") { body.innerHTML = videoTab(); loadPreview(); }
  else if (state.tab === "youtube") body.innerHTML = youtubeTab();
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
    ${playerBar()}
  </div>`;
}

// ───── pictures ─────
// One row per beat: the chosen picture big, alternates beside it, search and upload right there.

const SOURCE_NAME = { commons: "Wikimedia Commons", pexels: "Pexels", pixabay: "Pixabay", upload: "Your upload", card: "Card", ai: "AI illustration" };

function candidateThumb(c, big = false) {
  const slug = encodeURIComponent(state.ep.slug);
  if (c.source === "card") return cardPreview(c, big);
  const src = ["upload", "ai"].includes(c.source)
    ? `/api/episodes/${slug}/${c.source === "ai" ? "ai" : "uploads"}/${c.file.split("/").pop()}` : c.thumb;
  if (big && c.kind === "video") {
    return `<video class="v-media" src="${esc(c.url)}" poster="${esc(c.thumb || "")}" preload="none" muted controls playsinline></video>`;
  }
  return `<img class="v-media" src="${esc(src)}" alt="${esc(c.title || "")}" loading="lazy" referrerpolicy="no-referrer">
    ${c.kind === "video" ? `<span class="v-badge">▶ ${c.duration ? `${c.duration}s` : "video"}</span>` : ""}
    ${c.source === "ai" ? `<span class="v-badge v-ai">AI</span>` : ""}`;
}

function cardPreview(c, big) {
  if (c.card === "stat") return `<div class="v-card v-card-stat"><b>${esc(c.big)}</b><span>${esc(c.text)}</span></div>`;
  if (c.card === "document") return `<div class="v-card v-card-doc"><span class="doc-tag">Document</span><b>${esc(c.text)}</b>${big ? `<span>${esc(c.sub || "")}</span>` : ""}</div>`;
  if (c.card === "place") return `<div class="v-card v-card-place"><b>${esc(c.text)}</b></div>`;
  return `<div class="v-card v-card-title"><b>${esc(c.text)}</b></div>`;
}

function visualsTab() {
  const ep = state.ep;
  const v = ep.visuals;
  const running = state.jobs.some((j) => j.slug === ep.slug && j.kind === "visuals" && j.status === "running");
  if (!["approved", "voiced", "visuals_ready"].includes(ep.status) && !v) {
    return `<div class="empty"><h2>Approve the script first</h2><p>Pictures are found for the approved beats, one per beat.</p></div>`;
  }
  if (!v) {
    return `<div class="empty"><h2>${running ? "Finding pictures…" : "No pictures yet"}</h2>
      <p>${running ? "Searching Wikimedia Commons and stock libraries for every beat. Usually a minute or two." : "Finds real photos and footage for every beat, plus clean cards for figures and documents. Nothing AI-generated."}</p>
      ${running ? "" : `<button class="btn btn-primary" data-act="find-visuals">Find pictures</button>`}</div>`;
  }

  const sum = v.summary;
  const timings = new Map((ep.voice?.beats || []).map((b) => [b.id, b]));
  const narration = new Map((ep.script?.chapters || []).flatMap((ch) => ch.beats.map((b) => [b.id, b.narration])));
  const stock = state.status.stock_video || [];
  const running_draw = ["running", "waiting"].includes(ep.meta?.illustrate?.state);
  const queuedBeats = new Set(ep.meta?.illustrate?.state === "queued" ? ep.meta.illustrate.beats || [] : []);
  const rows = v.beats.filter((b) => !state.onlyNeeds || needsYou(b));

  return `
    <div class="v-bar">
      <div class="stats">
        <span class="stat"><b>${sum.real}</b> real pictures</span>
        ${sum.illustrations ? `<span class="stat"><b>${sum.illustrations}</b> illustrations</span>` : ""}
        <span class="stat"><b>${sum.cards}</b> cards</span>
        <span class="stat ${sum.needs_you ? "warn" : ""}"><b>${sum.needs_you}</b> need you</span>
      </div>
      <label class="v-filter"><input type="checkbox" data-act="toggle-needs" ${state.onlyNeeds ? "checked" : ""}> Only beats that need you</label>
      ${illustrateButton(v)}
      <button class="btn" data-act="find-visuals" ${running ? "disabled" : ""}>${running ? "Searching…" : "Search again"}</button>
    </div>
    ${illustrateNote()}
    ${stock.length ? "" : `<p class="v-note">Stock video is off. Add a free <code>PEXELS_API_KEY</code> on Render to get real footage for the B-roll beats; until then they use Commons photos.</p>`}
    <ol class="v-list">
      ${rows.map((b) => {
        const c = b.candidates[b.chosen] || b.candidates[0];
        const t = timings.get(b.id);
        const flag = c.needs_screenshot ? "Add a screenshot of the real document (headline, filing, post) for this beat."
          : b.missing ? "Nothing real found. Search with other words, or upload a photo you have the rights to use." : "";
        return `<li class="v-row ${flag ? "v-needs" : ""}" data-beat="${b.id}">
          <div class="v-main">${candidateThumb(c, true)}</div>
          <div class="v-side">
            <div class="v-head"><span class="v-id">${b.id}${t ? ` · ${clock(t.start)}` : ""}</span><span class="v-type" style="--vcolor: var(--v-${b.type})">${esc(VISUALS[b.type] || b.type)}</span></div>
            <p class="v-say">${esc(narration.get(b.id) || "")}</p>
            ${flag ? `<p class="v-flag">${flag}</p>` : ""}
            <div class="v-alts">
              ${b.candidates.map((alt, i) => `<button class="v-alt ${i === b.chosen ? "on" : ""}" data-act="pick-visual" data-i="${i}" aria-label="Use option ${i + 1}: ${esc(SOURCE_NAME[alt.source])}" title="${esc(alt.title || SOURCE_NAME[alt.source])}">${candidateThumb(alt)}</button>`).join("")}
              <button class="v-alt v-add" data-act="upload-visual" aria-label="Upload your own picture" title="Upload your own (screenshot or photo)">＋</button>
              ${running_draw ? "" : `<button class="v-alt v-add v-draw ${queuedBeats.has(b.id) ? "on" : ""}" data-act="open-draw" aria-label="Draw an AI illustration for this beat" title="${queuedBeats.has(b.id) ? "Queued to draw" : "Draw an AI illustration for this beat"}">✦</button>`}
              <input type="file" accept="image/png,image/jpeg,image/webp" hidden>
            </div>
            ${state.drawOpen === b.id ? drawForm(b) : ""}
            <div class="v-search">
              <input type="text" class="v-query" value="${esc(b.query)}" aria-label="Search words for this beat">
              <button class="btn btn-quiet" data-act="search-visual">Search</button>
            </div>
            <p class="v-credit">${c.source === "card" ? "Generated card, no credit needed" : c.source === "ai" ? "AI illustration (SDXL), drawn for this beat in the channel's style" : `${esc(SOURCE_NAME[c.source])}${c.credit && c.source !== "upload" ? ` · ${esc(c.credit)}` : ""}${c.license && c.source !== "upload" ? ` · ${esc(c.license)}` : ""}${c.page ? ` · <a href="${esc(c.page)}" target="_blank" rel="noopener">source</a>` : ""}`}</p>
            ${b.errors?.length ? `<p class="v-err">Search problem: ${esc(b.errors[0])}</p>` : ""}
          </div>
        </li>`;
      }).join("")}
    </ol>
    ${state.onlyNeeds && !rows.length ? `<div class="lint-ok">Nothing needs you. Every beat has a real picture or a card you've accepted.</div>` : ""}
    <details class="v-credits">
      <summary>Credits for the description (${v.credits.length})</summary>
      <pre>${esc(v.credits.join("\n")) || "No credited sources used yet."}</pre>
      ${v.credits.length ? `<button class="btn" data-act="copy-credits">Copy credits</button>` : ""}
    </details>`;
}

function drawForm(b) {
  const abstract = (state.ep.visuals?.illustratable || []).includes(b.id) || b.candidates.some((c) => c.source === "ai");
  const suggestion = abstract ? b.query.replace(/\b(close up of|slow motion|stock footage|footage)\b/gi, "").replace(/\s+/g, " ").trim() : "";
  return `<form class="v-drawform" data-act="draw-beat">
    <label>What should the illustration show?</label>
    <input type="text" class="v-prompt" value="${esc(suggestion)}" maxlength="160" required
      placeholder="An idea, not a real thing: “a phone splitting in two”, “a tangle of supply lines”">
    <p class="v-hint">Drawn in the channel's ink style. No names, brands or years: AI never draws real people or products.</p>
    <div class="v-drawbtns"><button class="btn btn-primary" type="submit">✦ Draw this</button><button class="btn btn-quiet" type="button" data-act="close-draw">Cancel</button></div>
  </form>`;
}

function illustrateButton(v) {
  const job = state.ep.meta?.illustrate || {};
  if (["queued", "running", "waiting"].includes(job.state)) return `<button class="btn" disabled>${job.state === "queued" ? "Queued to draw" : "Drawing on Kaggle…"}</button>`;
  const n = (v.illustratable || []).length;
  return n ? `<button class="btn" data-act="illustrate" title="AI illustrations in the channel's ink style, for abstract beats only">✦ Illustrate ${n} abstract ${n === 1 ? "beat" : "beats"}</button>` : "";
}

function illustrateNote() {
  const job = state.ep.meta?.illustrate || {};
  if (job.state === "queued") return `<p class="v-note">Queued for Kaggle's GPU: ${job.beats?.length || 0} beats, ${state.status.worker === "every 10 minutes" ? "starts when the worker next checks in, then " : ""}about 10–15 minutes including Kaggle's queue. They'll appear as extra options on each beat.</p>`;
  const k = job.kaggle_status ? ` Kaggle says: <b>${esc(job.kaggle_status)}</b>.` : "";
  if (job.state === "running") return `<p class="v-note">Drawing on Kaggle's GPU…${k} Usually 20–30 minutes for a full batch. They'll appear as extra options marked AI.</p>`;
  if (job.state === "waiting") return `<p class="v-note">Kaggle's free GPU queue is slow, so this batch is still drawing there.${k} The worker checks back every 10 minutes while this page is open and adds them when they're done.</p>`;
  if (job.state === "error") return `<p class="v-note v-err">Illustrations failed: ${esc(job.error || "")}</p>`;
  return "";
}

function needsYou(b) {
  const c = b.candidates[b.chosen] || {};
  return !!(c.needs_screenshot || b.missing);
}

// Shrink uploads in the browser so a phone screenshot doesn't become a 6 MB row.
async function resizeImage(file, max = 1920) {
  const img = await new Promise((ok, bad) => {
    const i = new Image(); i.onload = () => ok(i); i.onerror = () => bad(new Error("That file isn't an image this browser can read."));
    i.src = URL.createObjectURL(file);
  });
  const scale = Math.min(1, max / img.width);
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(img.width * scale); canvas.height = Math.round(img.height * scale);
  canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
  URL.revokeObjectURL(img.src);
  return canvas.toDataURL("image/jpeg", 0.86);
}

document.addEventListener("change", async (e) => {
  const input = e.target;
  if (input.type !== "file" || !input.closest("[data-beat]") || !input.files[0]) return;
  const beat = input.closest("[data-beat]").dataset.beat;
  try {
    toast("Uploading…");
    const data_url = await resizeImage(input.files[0]);
    state.ep = await api(`/api/episodes/${state.ep.slug}/visuals/${beat}/upload`, { method: "POST", body: { data_url, name: input.files[0].name } });
    toast("Picture added");
    renderEpisode();
  } catch (err) { toast(err.message, true); }
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.classList?.contains("v-query")) {
    e.preventDefault();
    e.target.closest("[data-beat]").querySelector('[data-act="search-visual"]').click();
  }
});

// ───── finished video ─────

function downloadLink(r, cls = "") {
  if (r.run_id && r.repo) {
    return `<a class="${cls}" href="https://github.com/${esc(r.repo)}/actions/runs/${esc(r.run_id)}" target="_blank" rel="noopener">Download full video</a>`;
  }
  return r.file ? `<span class="muted">Rendered locally: <code>output/${esc(r.file)}</code></span>` : "";
}

function videoTab() {
  const r = state.ep.meta?.render || {};
  if (r.state !== "done") return `<div class="empty"><h2>No video yet</h2><p>Render it once the narration and pictures are approved.</p></div>`;
  const slug = encodeURIComponent(state.ep.slug);
  return `
    <div class="vid">
      <div class="vid-frame"><video id="previewPlayer" controls playsinline preload="metadata"></video>
        <p class="vid-note">Preview: first ${state.status.preview_seconds || 60} seconds at 480p. The full video is 1080p.</p></div>
      <dl class="vid-facts">
        <div><dt>Length</dt><dd>${clock(r.duration || 0)}</dd></div>
        <div><dt>File</dt><dd>${r.size_mb} MB, 1920×1080, 30 fps</dd></div>
        <div><dt>Shots</dt><dd>${r.shots}${r.fallbacks ? ` (${r.fallbacks} fell back to a card)` : ""}</dd></div>
        <div><dt>Render time</dt><dd>${Math.round((r.seconds_taken || 0) / 60)} min</dd></div>
      </dl>
      <div class="vid-actions">
        ${downloadLink(r, "btn btn-primary")}
        <a class="btn" href="/api/episodes/${slug}/captions.srt">Captions (.srt)</a>
        <button class="btn" data-act="render">Render again</button>
      </div>
      <p class="hint">The full video is kept on GitHub for 30 days, under <b>Artifacts</b> at the bottom of the run page (sign in to GitHub to download). Captions get uploaded to YouTube as a subtitle track, not burned in.</p>
    </div>`;
}

let previewKey = null, previewUrl = null;
async function loadPreview() {
  const el = $("#previewPlayer");
  if (!el) return;
  const key = `${state.ep.slug}:${state.ep.meta?.render?.finished_at}`;
  if (previewKey !== key) {
    const res = await fetch(`/api/episodes/${encodeURIComponent(state.ep.slug)}/preview`);
    if (!res.ok) { el.replaceWith(Object.assign(document.createElement("p"), { className: "muted", textContent: "Preview isn't available." })); return; }
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = URL.createObjectURL(await res.blob());
    previewKey = key;
  }
  el.src = previewUrl;
}

// ───── narration playback ─────
// One MP3 per story. Each beat's play button seeks into it and stops at the beat's end.

const ICON_PLAY = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4.5 3v10l8-5z" fill="currentColor"/></svg>';
const ICON_PAUSE = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4.5 3h2.5v10H4.5zM9 3h2.5v10H9z" fill="currentColor"/></svg>';
const audio = { el: null, key: null, stopAt: null, loading: null };

function voiceKey() { const v = state.ep?.voice; return v ? `${state.ep.slug}:${v.duration}:${v.voice}:${state.ep.meta?.voice?.finished_at}` : null; }
function beatTimings() {
  const v = state.ep?.voice;
  return v && !v.stale && !state.dirty ? new Map(v.beats.map((b) => [b.id, b])) : new Map();
}

function playerBar() {
  const v = state.ep?.voice;
  if (!v) return "";
  const playing = audio.el && !audio.el.paused && audio.key === voiceKey();
  return `<div class="player" id="player">
    <button class="play-all" data-act="play-all" aria-label="${playing ? "Pause" : "Play"} narration">${playing ? ICON_PAUSE : ICON_PLAY}</button>
    <span class="p-time"><b id="pNow">${clock(audio.key === voiceKey() && audio.el ? audio.el.currentTime : 0)}</b> / ${clock(v.duration)}</span>
    <span class="p-voice">${esc(voiceName(v.voice))}</span>
    ${v.stale || state.dirty ? `<span class="p-stale">Script changed since this recording. Approve again to re-record.</span>` : ""}
  </div>`;
}

async function ensureAudio() {
  const key = voiceKey();
  if (audio.el && audio.key === key) return audio.el;
  if (audio.loading?.key === key) return audio.loading.promise;
  const promise = (async () => {
    const res = await fetch(`/api/episodes/${encodeURIComponent(state.ep.slug)}/audio`);
    if (!res.ok) throw new Error("Couldn't load the narration.");
    const url = URL.createObjectURL(await res.blob());  // a blob seeks reliably, no range requests needed
    if (audio.el) { audio.el.pause(); URL.revokeObjectURL(audio.el.src); }
    const el = new Audio(url);
    el.addEventListener("timeupdate", onAudioTime);
    el.addEventListener("play", syncPlayButton);
    el.addEventListener("pause", () => { syncPlayButton(); markSpeaking(null); });
    Object.assign(audio, { el, key, stopAt: null, loading: null });
    return el;
  })();
  audio.loading = { key, promise };
  return promise;
}

function onAudioTime() {
  const el = audio.el;
  if (audio.stopAt != null && el.currentTime >= audio.stopAt) { el.pause(); audio.stopAt = null; return; }
  const now = $("#pNow"); if (now) now.textContent = clock(el.currentTime);
  const hit = (state.ep?.voice?.beats || []).find((b) => el.currentTime >= b.start && el.currentTime < b.end + 0.3);
  markSpeaking(hit && beatTimings().size ? hit.id : null);
}

function markSpeaking(id) {
  $$("#paper .beat.speaking").forEach((r) => { if (r.dataset.id !== id) r.classList.remove("speaking"); });
  if (id) $(`#paper .beat[data-id="${id}"]`)?.classList.add("speaking");
}

function syncPlayButton() {
  const btn = $('[data-act="play-all"]');
  if (!btn) return;
  const playing = audio.el && !audio.el.paused;
  btn.innerHTML = playing ? ICON_PAUSE : ICON_PLAY;
  btn.setAttribute("aria-label", `${playing ? "Pause" : "Play"} narration`);
}

function stopAudio() { if (audio.el) audio.el.pause(); }

function scriptTab() {
  const ep = state.ep;
  const running = state.jobs.some((j) => j.slug === ep.slug && j.status === "running");
  if (running && !state.draft) return `<div class="empty"><h2>Writing…</h2><p>The script appears here when it's done.</p></div>`;
  if (!ep.factsheet) return `<div class="empty"><h2>Research comes first</h2><p>The script is written only from the fact sheet, so that has to exist first.</p></div>`;
  if (!state.draft || state.showGen) return genForm();

  const flags = beatFlags();
  const issues = (ep.lint?.issues || []).filter((x) => !x.startsWith("Length"));
  const lengthNote = (ep.lint?.issues || []).find((x) => x.startsWith("Length"));
  const timings = beatTimings();
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
          const spoken = timings.get(id);
          return `<div class="beat ${f.length ? "flagged" : ""}" data-ci="${ci}" data-bi="${bi}" data-id="${id}">
            <div class="tc"><span class="start">${clock(spoken ? spoken.start : start)}</span><span class="id">${id}</span>
              ${spoken ? `<button class="beat-play" data-act="play-beat" data-id="${id}" aria-label="Play beat ${id}" title="Play this beat">${ICON_PLAY}</button>` : ""}</div>
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

// Production view, once pictures are approved: edit what each Short says, make them, watch them.
function shortsDesk() {
  const ep = state.ep;
  const d = state.sdraft;
  const job = ep.meta?.shorts || {};
  const made = job.made || {};
  const busy = ["queued", "running"].includes(job.state);
  const picks = new Map((ep.visuals?.beats || []).map((b) => [b.id, b.candidates[b.chosen] || b.candidates[0]]));
  const chosen = d.items.filter((it) => it.include);
  const est = (it) => {
    const w = [it.hook, ...it.lines.map((l) => l.text), it.outro].join(" ").trim().split(/\s+/).filter(Boolean).length;
    return Math.round(w / (2.55 * 1.05) + 0.3 * (it.lines.length + 2));
  };
  const max = 58;

  let status = "";
  if (job.state === "queued") status = `<p class="v-note">Queued: ${job.ids?.length || 0} Shorts. ${state.status.worker === "every 10 minutes" ? "The worker checks every 10 minutes." : "The worker is starting."} About a minute each.</p>`;
  else if (job.state === "running") status = `<p class="v-note">Making Shorts… ${job.progress || 0}% done. Voice, word timings, then the vertical cut.
      <span class="progress-bar" role="progressbar" aria-valuenow="${job.progress || 0}" aria-valuemin="0" aria-valuemax="100"><span style="width:${job.progress || 0}%"></span></span></p>`;
  else if (job.state === "error") status = `<p class="v-note v-err">Shorts failed: ${esc(job.error || "")}</p>`;
  const failed = Object.entries(job.failed || {});

  return `
    <div class="sd-bar">
      <label class="sd-label">Name at the top of every Short
        <input type="text" id="shortLabel" value="${esc(d.label)}" maxlength="60">
      </label>
      <div class="sd-actions">
        ${state.sdirty ? `<button class="btn" data-act="save-shorts">Save edits</button>` : ""}
        <button class="btn btn-quiet" data-act="reset-shorts" title="Go back to the script's flagged moments">Start over from the script</button>
        ${busy ? `<button class="btn" data-act="cancel-shorts" ${job.state === "running" ? "disabled" : ""}>${job.state === "running" ? "Making…" : "Cancel"}</button>`
          : `<button class="btn btn-primary" data-act="make-shorts" ${chosen.length ? "" : "disabled"}>${chosen.length && chosen.every((it) => made[it.id]) ? "Remake" : "Make"} ${chosen.length} ${chosen.length === 1 ? "Short" : "Shorts"}</button>`}
      </div>
    </div>
    ${status}
    ${failed.length ? `<p class="v-note v-err">${failed.map(([k, v]) => `${esc(k)}: ${esc(v)}`).join("<br>")}</p>` : ""}
    <p class="lede sd-lede">Each Short opens on its hook, plays the flagged lines from the long video over their pictures, and closes by pointing to the full video. Captions are burned in, with the spoken word highlighted.</p>
    <div class="sd-list">
      ${d.items.map((it, i) => {
        const m = made[it.id];
        const sec = est(it);
        return `<article class="sd ${it.include ? "" : "off"}" data-si="${i}">
          <div class="sd-phone">
            ${m ? `<video class="sd-video" src="/api/episodes/${encodeURIComponent(ep.slug)}/shorts/${esc(it.id)}/preview?v=${encodeURIComponent(m.made_at || "")}" controls playsinline preload="metadata"></video>`
              : `<div class="sd-mock"><span class="sd-mock-label">${esc(d.label)}</span>${picks.get(it.lines[0]?.beat) ? `<div class="sd-mock-pic">${candidateThumb(picks.get(it.lines[0].beat))}</div>` : ""}<span class="sd-mock-hook">${esc(it.hook)}</span></div>`}
          </div>
          <div class="sd-body">
            <div class="sd-head">
              <label class="sd-inc"><input type="checkbox" data-sf="include" ${it.include ? "checked" : ""}> <b>${esc(it.id.toUpperCase())}</b></label>
              <span class="sd-len ${sec > max ? "warn" : ""}">~${sec}s${sec > max ? ` · over ${max}s, trim a line` : ""}</span>
              ${m ? `<span class="sd-made">Made: ${m.duration}s · ${m.shots} shots · ${m.size_mb} MB${m.timed_by === "whisper" ? "" : " · timed by word length"}</span>` : ""}
            </div>
            <label class="sd-k">Hook <small>the first thing they hear: make it stop the scroll</small></label>
            <textarea class="autosize sd-hook" rows="1" data-sf="hook">${esc(it.hook)}</textarea>
            <label class="sd-k">Lines <small>from the long video, over their pictures</small></label>
            <ol class="sd-lines">
              ${it.lines.map((ln, li) => `<li data-li="${li}">
                <span class="sd-thumb" title="${esc(ln.beat)}">${picks.get(ln.beat) ? candidateThumb(picks.get(ln.beat)) : ""}</span>
                <textarea class="autosize" rows="1" data-sf="line" aria-label="Line ${li + 1}">${esc(ln.text)}</textarea>
                <button class="sd-x" data-act="drop-line" aria-label="Remove this line" title="Remove this line">×</button>
              </li>`).join("")}
            </ol>
            <label class="sd-k">Closing line</label>
            <textarea class="autosize" rows="1" data-sf="outro">${esc(it.outro)}</textarea>
            ${it.why ? `<p class="why">Why this works: ${esc(it.why)}</p>` : ""}
            ${m ? `<div class="sd-dl">${downloadLink({ run_id: m.run_id, repo: m.repo, file: m.file }, "btn btn-quiet").replace("Download full video", "Download this Short")}</div>` : ""}
          </div>
        </article>`;
      }).join("")}
    </div>`;
}

// ───── YouTube ─────

function youtubeTab() {
  const ep = state.ep;
  const pack = ep.youtube;
  const acct = state.status.youtube || {};
  const up = ep.meta?.upload || {};
  const job = ep.meta?.youtube || {};

  let account;
  if (!acct.configured) {
    account = `<div class="yt-acct"><p><strong>One-time setup for one-click uploads.</strong> Make a free Google Cloud app with the YouTube API, then add its <code>GOOGLE_CLIENT_ID</code> and <code>GOOGLE_CLIENT_SECRET</code> on Render and as GitHub secrets. Steps: DEPLOY.md, section 7.</p>
      <p class="hint">Until then, use the upload kit below: everything is ready to copy into YouTube Studio.</p></div>`;
  } else if (!acct.connected) {
    account = `<div class="yt-acct"><p><strong>Connect your channel</strong> to upload from here. You sign in with Google once; the desk only gets permission to upload and add captions.</p>
      <a class="btn btn-primary" href="/api/youtube/connect" data-act="yt-connect">Connect YouTube channel</a></div>`;
  } else {
    account = `<div class="yt-acct yt-on"><p>Uploading to <strong>${esc(acct.channel || "your channel")}</strong></p>
      <button class="btn btn-quiet" data-act="yt-disconnect">Disconnect</button></div>`;
  }

  const row = (what, info, label) => {
    const u = up[what] || {};
    const busy = ["queued", "running"].includes(u.state) && ["queued", "running"].includes(job.state);
    const src = what === "long" ? (ep.meta?.render || {}) : (ep.meta?.shorts?.made || {})[what] || {};
    const status = u.state === "done"
      ? `<p class="yt-done">On YouTube: <a href="${esc(u.url)}" target="_blank" rel="noopener">${esc(u.url)}</a> · ${esc(u.privacy)}${u.publish_at ? `, publishes ${esc(new Date(u.publish_at).toLocaleString())}` : ""}${u.captions === true ? " · captions added" : typeof u.captions === "string" ? ` · <span class="v-err">${esc(u.captions)}</span>` : ""}</p>`
      : busy ? `<p class="v-note">${job.state === "running" ? `Uploading… ${job.progress || 0}%` : "Queued to upload."}</p>`
      : u.state === "error" ? `<p class="v-note v-err">Upload failed: ${esc(u.error || job.error || "")}</p>` : "";
    return `<article class="yt-item" data-what="${esc(what)}">
      <header class="yt-h"><h3>${esc(label)}</h3>${downloadLink({ run_id: src.run_id, repo: src.repo, file: info.file }, "btn btn-quiet").replace("Download full video", what === "long" ? "Download video" : "Download Short")}
        ${what === "long" && ep.meta?.render?.state === "done" ? `<a class="btn btn-quiet" href="/api/episodes/${encodeURIComponent(ep.slug)}/captions.srt">Captions (.srt)</a>` : ""}</header>
      <div class="yt-field"><span class="yt-k">Title <small>${info.title.length}/100</small></span><div class="yt-copyrow"><input type="text" readonly value="${esc(info.title)}"><button class="btn btn-quiet" data-act="yt-copy" data-copy="title">Copy</button></div></div>
      <div class="yt-field"><span class="yt-k">Description</span><textarea readonly class="autosize" rows="4">${esc(info.description)}</textarea><button class="btn btn-quiet yt-copy-under" data-act="yt-copy" data-copy="description">Copy description</button></div>
      <div class="yt-field"><span class="yt-k">Tags</span><div class="yt-copyrow"><input type="text" readonly value="${esc(info.tags.join(", "))}"><button class="btn btn-quiet" data-act="yt-copy" data-copy="tags">Copy</button></div></div>
      ${acct.connected && u.state !== "done" ? `<div class="yt-send">
        <label>Visibility <select class="yt-privacy">
          <option value="private">Private</option><option value="unlisted">Unlisted</option><option value="public">Public</option></select></label>
        <label>Schedule <input type="datetime-local" class="yt-when" aria-label="Publish at (optional)"></label>
        <button class="btn btn-primary" data-act="yt-upload" ${busy ? "disabled" : ""}>Upload to YouTube</button>
      </div>` : ""}
      ${status}
    </article>`;
  };

  const shortsIds = Object.keys(pack.shorts || {});
  return `
    ${account}
    <p class="v-note yt-note">YouTube keeps videos uploaded through a new, unreviewed app <b>private</b> until the app passes Google's free review (<a href="https://support.google.com/youtube/contact/yt_api_form" target="_blank" rel="noopener">audit form</a>), and a locked video can't be switched to public later. Until then, upload anything that should go public with the kit: download, then paste the title, description and tags in YouTube Studio. Free quota: about 6 uploads a day.</p>
    ${pack.long.ready ? row("long", pack.long, "Long video") : `<p class="hint">Render the long video to get its upload kit.</p>`}
    ${shortsIds.length ? `<h2 class="yt-sh">Shorts</h2>${shortsIds.map((id) => row(id, pack.shorts[id], `Short ${id.toUpperCase()}`)).join("")}` : ""}`;
}

function shortsTab() {
  if (["visuals_ready", "rendered"].includes(state.ep.status) && state.sdraft) return shortsDesk();
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
  if (el.id === "shortLabel" && state.sdraft) { state.sdraft.label = el.value; markShortsDirty(); return; }
  if (el.dataset.sf && state.sdraft) {
    const it = state.sdraft.items[+el.closest("[data-si]").dataset.si];
    if (el.dataset.sf === "hook") it.hook = el.value;
    else if (el.dataset.sf === "outro") it.outro = el.value;
    else if (el.dataset.sf === "line") it.lines[+el.closest("[data-li]").dataset.li].text = el.value;
    else if (el.dataset.sf === "include") { it.include = el.checked; markShortsDirty(true); return; }
    markShortsDirty();
    return;
  }
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

// Keep the address bar in step with the tab, so reloads and refreshes land on the same tab.
function setTab(tab) {
  state.tab = tab;
  history.replaceState(null, "", `#/e/${state.ep.slug}/${tab}`);
  currentHash = location.hash;
}

document.addEventListener("click", async (e) => {
  const tabBtn = e.target.closest("[data-tab]");
  if (tabBtn) {
    setTab(tabBtn.dataset.tab);
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
        state.ep = await api(`/api/episodes/${slug}/approve`, { method: "POST", body: { voice: $("#voicePick")?.value } });
        state.episodes = await api("/api/episodes");
        await poll();
        lastWorkerCheck = 0;
        toast("Approved. Narration queued."); render(); break;

      case "record-voice":
        stopAudio();
        state.ep = await api(`/api/episodes/${slug}/voice`, { method: "POST", body: { voice: $("#voicePick")?.value } });
        state.episodes = await api("/api/episodes");
        await poll();
        lastWorkerCheck = 0;
        toast("Narration queued"); render(); break;

      case "find-visuals":
        track(await api(`/api/episodes/${slug}/visuals`, { method: "POST" }));
        setTab("visuals");
        await poll(); renderEpisode(); break;

      case "pick-visual": {
        const row = btn.closest("[data-beat]");
        state.ep = await api(`/api/episodes/${slug}/visuals/${row.dataset.beat}`, { method: "PUT", body: { choice: +btn.dataset.i } });
        renderEpisode(); break;
      }

      case "search-visual": {
        const row = btn.closest("[data-beat]");
        const q = $(".v-query", row).value.trim();
        if (!q) return;
        btn.disabled = true; btn.textContent = "Searching…";
        state.ep = await api(`/api/episodes/${slug}/visuals/${row.dataset.beat}`, { method: "PUT", body: { query: q } });
        renderEpisode(); break;
      }

      case "upload-visual":
        btn.closest("[data-beat]").querySelector("input[type=file]").click();
        break;

      case "approve-visuals": {
        const n = state.ep.visuals?.summary.needs_you || 0;
        if (n && !confirm(`${n} ${n === 1 ? "beat still uses a placeholder card" : "beats still use placeholder cards"} where a real screenshot or photo would be better. Approve anyway?`)) return;
        state.ep = await api(`/api/episodes/${slug}/visuals/approve`, { method: "POST" });
        state.episodes = await api("/api/episodes");
        toast("Pictures approved"); render(); break;
      }

      case "reopen-visuals":
        state.ep = await api(`/api/episodes/${slug}/visuals/${state.ep.visuals.beats[0].id}`, { method: "PUT", body: { choice: state.ep.visuals.beats[0].chosen } });
        state.episodes = await api("/api/episodes");
        setTab("visuals"); render(); break;

      case "copy-credits":
        await navigator.clipboard.writeText(state.ep.visuals.credits.join("\n"));
        toast("Credits copied"); break;

      case "yt-copy": {
        const item = btn.closest("[data-what]");
        const info = item.dataset.what === "long" ? state.ep.youtube.long : state.ep.youtube.shorts[item.dataset.what];
        const text = btn.dataset.copy === "tags" ? info.tags.join(", ") : info[btn.dataset.copy];
        await navigator.clipboard.writeText(text);
        toast(`${btn.dataset.copy[0].toUpperCase()}${btn.dataset.copy.slice(1)} copied`); break;
      }
      case "yt-connect": sessionStorageSet("ytReturn", location.hash); return;  // let the link navigate
      case "yt-disconnect": {
        if (!confirm("Disconnect this YouTube channel from the desk?")) break;
        state.status.youtube = await api("/api/youtube/disconnect", { method: "POST" });
        renderEpisode(); break;
      }
      case "yt-upload": {
        const item = btn.closest("[data-what]");
        const when = item.querySelector(".yt-when").value;
        const target = { what: item.dataset.what, privacy: item.querySelector(".yt-privacy").value,
          publish_at: when ? new Date(when).toISOString() : null };
        state.ep = await api(`/api/episodes/${slug}/youtube/upload`, { method: "POST", body: { targets: [target] } });
        state.episodes = await api("/api/episodes");
        await poll();
        lastWorkerCheck = 0;
        toast("Queued to upload"); renderEpisode(); break;
      }
      case "save-shorts": await saveShorts(); toast("Shorts saved"); renderEpisode(); break;
      case "reset-shorts": {
        if (!confirm("Throw away your edits to the Shorts and start again from the script?")) break;
        state.ep = await api(`/api/episodes/${slug}/shorts/reset`, { method: "POST" });
        state.sdraft = structuredClone(state.ep.shorts); state.sdirty = false; renderEpisode(); break;
      }
      case "drop-line": {
        const it = state.sdraft.items[+btn.closest("[data-si]").dataset.si];
        if (it.lines.length <= 1) { toast("A Short needs at least one line.", true); break; }
        it.lines.splice(+btn.closest("[data-li]").dataset.li, 1);
        markShortsDirty(true); break;
      }
      case "make-shorts": {
        if (state.sdirty) await saveShorts();
        state.ep = await api(`/api/episodes/${slug}/shorts/make`, { method: "POST", body: {} });
        state.sdraft = structuredClone(state.ep.shorts);
        state.episodes = await api("/api/episodes");
        await poll();
        lastWorkerCheck = 0;
        toast("Queued: making Shorts"); renderEpisode(); break;
      }
      case "cancel-shorts":
        state.ep = await api(`/api/episodes/${slug}/shorts/cancel`, { method: "POST" });
        state.episodes = await api("/api/episodes"); renderEpisode(); break;
      case "open-draw": {
        const id = btn.closest("[data-beat]").dataset.beat;
        state.drawOpen = state.drawOpen === id ? null : id;
        renderEpisode();
        document.querySelector(`[data-beat="${id}"] .v-prompt`)?.focus();
        break;
      }
      case "close-draw": state.drawOpen = null; renderEpisode(); break;
      case "illustrate": {
        const body = {};
        state.ep = await api(`/api/episodes/${slug}/visuals/illustrate`, { method: "POST", body });
        state.episodes = await api("/api/episodes");
        await poll();
        lastWorkerCheck = 0;  // look again on the next tick instead of waiting 8 s
        toast("Queued for illustration"); renderEpisode(); break;
      }

      case "toggle-needs":
        state.onlyNeeds = !state.onlyNeeds; renderTab(); break;

      case "render":
        state.ep = await api(`/api/episodes/${slug}/render`, { method: "POST" });
        state.episodes = await api("/api/episodes");
        await poll();
        lastWorkerCheck = 0;
        toast("Queued to render"); render(); break;

      case "cancel-render":
        state.ep = await api(`/api/episodes/${slug}/render/cancel`, { method: "POST" });
        state.episodes = await api("/api/episodes"); render(); break;

      case "cancel-voice":
        state.ep = await api(`/api/episodes/${slug}/voice/cancel`, { method: "POST" });
        state.episodes = await api("/api/episodes"); render(); break;

      case "play-all": {
        const el = await ensureAudio();
        audio.stopAt = null;
        if (el.paused) await el.play(); else el.pause();
        break;
      }

      case "play-beat": {
        const b = beatTimings().get(btn.dataset.id);
        if (!b) return;
        const el = await ensureAudio();
        el.currentTime = b.start;
        audio.stopAt = b.end + 0.15;
        await el.play();
        break;
      }

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

function markShortsDirty(rerender = false) {
  const first = !state.sdirty;
  state.sdirty = true;
  if (first || rerender) {  // show the Save button without stealing focus from the box being typed in
    const focus = document.activeElement;
    const sel = focus && focus.selectionStart;
    const key = focus?.closest?.("[data-si]") ? [focus.closest("[data-si]").dataset.si, focus.dataset.sf, focus.closest("[data-li]")?.dataset.li] : null;
    renderTab();
    if (key) {
      const box = document.querySelector(`[data-si="${key[0]}"] ${key[2] !== undefined ? `[data-li="${key[2]}"] ` : ""}[data-sf="${key[1]}"]`);
      if (box && box.tagName === "TEXTAREA") { box.focus(); box.setSelectionRange(sel, sel); }
    }
  }
}

async function saveShorts() {
  const slug = encodeURIComponent(state.ep.slug);
  state.ep = await api(`/api/episodes/${slug}/shorts`, { method: "POST", body: { label: state.sdraft.label, items: state.sdraft.items } });
  state.sdraft = structuredClone(state.ep.shorts);
  state.sdirty = false;
}

function sessionStorageGet(k) { try { return sessionStorage.getItem(k); } catch { return null; } }
function sessionStorageSet(k, v) { try { sessionStorage.setItem(k, v); } catch { /* private mode */ } }

// One picture at a time: the ✦ form on a beat in the Pictures tab.
document.addEventListener("submit", async (e) => {
  const form = e.target.closest("form.v-drawform");
  if (!form) return;
  e.preventDefault();
  const id = form.closest("[data-beat]").dataset.beat;
  const prompt = form.querySelector(".v-prompt").value.trim();
  const btn = form.querySelector("[type=submit]");
  btn.disabled = true;
  try {
    state.ep = await api(`/api/episodes/${encodeURIComponent(state.ep.slug)}/visuals/illustrate`,
      { method: "POST", body: { prompts: { [id]: prompt } } });
    state.drawOpen = null;
    state.episodes = await api("/api/episodes");
    await poll();
    lastWorkerCheck = 0;
    toast(`Queued ${id} to draw. It'll appear as a new option on this beat.`);
    renderEpisode();
  } catch (err) {
    btn.disabled = false;
    toast(err.message, true);
  }
});

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

$("#signOut").addEventListener("click", async () => {
  if (state.dirty && !confirm("You have unsaved script edits. Sign out anyway?")) return;
  state.dirty = false;
  await api("/api/logout", { method: "POST" });
  location.href = "/login";
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
  // Back from Google's sign-in page.
  const yt = new URLSearchParams(location.hash.split("?")[1] || "");
  if (yt.get("youtube")) {
    const ok = yt.get("youtube") === "connected";
    toast(ok ? `YouTube connected: ${state.status.youtube?.channel || "your channel"}` : `YouTube didn't connect: ${yt.get("why") || yt.get("youtube")}`, !ok);
    history.replaceState(null, "", sessionStorageGet("ytReturn") || "#/");
  }
  await route();
  setInterval(poll, 1500);
  setInterval(() => { if (state.jobs.some((j) => j.status === "running")) renderJobs(); }, 1000);
})();
