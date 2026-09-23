/* Survey Studio - the app window. Plain JavaScript, no framework, no network: runs
   offline on survey laptops. Every file operation goes through the local server. */
"use strict";

// ---------------------------------------------------------------- helpers
const $ = (sel, root = document) => root.querySelector(sel);
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: { "X-Studio": "1" } };
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  const r = await fetch("/api/" + path, init);
  let data = {};
  try { data = await r.json(); } catch (e) { /* empty */ }
  if (!r.ok) {
    const err = new Error(data.error || `Something went wrong (${r.status})`);
    err.status = r.status;
    throw err;
  }
  return data;
}
function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "on" + (bad ? " bad" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.className = ""), bad ? 6000 : 2600);
}
const pad = (n) => String(n).padStart(2, "0");
function hms(sec) {
  sec = Math.max(0, Math.floor(sec));
  return `${Math.floor(sec / 3600)}:${pad(Math.floor(sec / 60) % 60)}:${pad(sec % 60)}`;
}
function clockToSec(t) {
  const m = /^(\d{1,2}):(\d{2})(?::(\d{2}))?$/.exec(t || "");
  return m ? (+m[1]) * 3600 + (+m[2]) * 60 + (+(m[3] || 0)) : null;
}
function secToClock(s) {
  s = ((Math.floor(s) % 86400) + 86400) % 86400;
  return `${pad(Math.floor(s / 3600))}:${pad(Math.floor(s / 60) % 60)}:${pad(s % 60)}`;
}
function startSec(iso) {                 // "2026-06-14T21:05:00" -> seconds since midnight
  const m = /T(\d{2}):(\d{2}):(\d{2})/.exec(iso || "");
  return m ? (+m[1]) * 3600 + (+m[2]) * 60 + (+m[3]) : 0;
}
function niceDate(iso) {
  if (!iso) return "time unknown";
  const d = new Date(iso);
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }) +
    " " + iso.slice(11, 16);
}
function dur(s) {
  if (!s && s !== 0) return "";
  s = Math.round(s);
  return s >= 3600 ? `${Math.floor(s / 3600)} h ${pad(Math.floor(s / 60) % 60)} min` : `${Math.floor(s / 60)} min ${pad(s % 60)} s`;
}
const EV_LABEL = { emergence: "Emergence", "re-entry": "Re-entry", pass: "Pass", foraging: "Foraging", other: "Other" };

// ---------------------------------------------------------------- state
const S = {
  state: null,          // /api/state
  survey: null,         // current survey summary
  rid: null,            // selected recording id
  mode: "review",       // review | qa
  tab: "log",
  rows: [],             // current log rows
  dets: null,           // detections or null
  detIdx: -1,
  pendingCount: "",
  species: "",
  direction: "",
  history: [],          // for undo
  readOnly: false,
  lockTimer: null,
  saveTimer: null,
  ws: "video",
  frameTime: null,      // media time of the frame on screen
  openRow: null,        // log row showing its details
  lockHeld: false,
};

// ---------------------------------------------------------------- boot
async function boot() {
  try {
    S.state = await api("state");
  } catch (e) {
    document.body.textContent = "Survey Studio couldn't start: " + e.message;
    return;
  }
  S.species = S.state.settings.default_species || "";
  renderUser();
  document.querySelectorAll(".ws").forEach((b) => b.addEventListener("click", () => switchWs(b.dataset.ws)));
  $("#btn-settings").addEventListener("click", settingsModal);
  $("#btn-help").addEventListener("click", keysModal);
  document.addEventListener("keydown", onKey);
  setInterval(() => api("ping", { method: "POST", body: {} }).catch(() => {}), 20000);
  if (!S.state.settings.observer) settingsModal(true);
  home();
}
function renderUser() {
  const s = S.state.settings;
  $("#btn-settings").textContent = s.observer || "Set up";
}
function switchWs(ws) {
  S.ws = ws;
  document.querySelectorAll(".ws").forEach((b) => b.classList.toggle("active", b.dataset.ws === ws));
  if (ws === "acoustic") return acoustic();
  if (S.survey) return openSurvey(S.survey.key, true);
  home();
}
function crumbs(...items) {
  const c = $("#crumbs");
  c.replaceChildren();
  items.forEach((it, i) => {
    if (i) c.append(h("span", { class: "sep" }, "›"));
    c.append(it.onclick ? h("a", { onclick: it.onclick }, it.label) : h("span", { class: "cur" }, it.label));
  });
}
function mount(...nodes) {
  releaseLock();
  const app = $("#app");
  app.replaceChildren(...nodes);
}

// ---------------------------------------------------------------- home
function home() {
  S.survey = null; S.rid = null;
  crumbs({ label: "Home" });
  const st = S.state;
  const tools = st.tools;
  const recent = st.recent || [];
  const notices = [];
  if (!tools.ffmpeg || !tools.ffprobe)
    notices.push(h("div", { class: "notice bad" },
      h("b", {}, "ffmpeg isn't installed on this computer. "),
      "Survey Studio needs it to read camera cards and make review copies. Ask IT to deploy it, or run ",
      h("kbd", {}, "winget install Gyan.FFmpeg"), " and restart Survey Studio."));
  else if (!tools.exiftool)
    notices.push(h("div", { class: "notice" },
      "exiftool isn't installed, so some recording times will come from file dates and be flagged for checking."));
  mount(h("div", { class: "home" },
    h("h1", {}, "Bat emergence video review"),
    h("p", { class: "lead" }, "Import a night's camera cards, review each recording with the real clock time on screen, and turn your counts into report tables."),
    h("div", { class: "cards" },
      h("button", { class: "card", onclick: importModal },
        h("div", { class: "ico" }, "⇩"), h("h2", {}, "Import camera cards"),
        h("p", {}, "Copies the footage safely to this computer, joins split clips and makes fast review copies. Originals are never changed.")),
      h("button", { class: "card", onclick: pickSurvey },
        h("div", { class: "ico" }, "▤"), h("h2", {}, "Open a survey"),
        h("p", {}, "Carry on with a survey you or a colleague already imported."))),
    ...notices,
    h("div", { class: "section-title" }, "Recent surveys"),
    recent.length ? h("div", { class: "recent" }, recent.map((r) =>
      h("div", { class: "recent-item", onclick: () => r.exists ? openPath(r.path) : forget(r) },
        h("div", { class: "grow" },
          h("div", { class: "nm" }, r.name),
          h("div", { class: "pth" }, r.path)),
        r.exists ? h("span", { class: "faint" }, "Opened " + (r.opened || "").replace("T", " ")) :
          h("span", { class: "chip warn" }, "Folder moved or deleted")))) :
      h("p", { class: "muted" }, "Nothing yet. Import your first camera cards to get started."),
    h("p", { class: "faint" }, `Survey Studio ${st.version}`)));
}
async function forget(r) {
  if (!confirm(`"${r.name}" isn't at ${r.path} any more. Remove it from the list?`)) return;
  const res = await api("surveys/forget", { method: "POST", body: { path: r.path } });
  S.state.recent = res.recent;
  home();
}
async function pickSurvey() {
  try {
    const { path } = await api("pick-folder", { method: "POST", body: { title: "Choose a survey folder" } });
    if (path) openPath(path);
  } catch (e) {
    const p = prompt("Paste the survey folder's path:");
    if (p) openPath(p);
  }
}
async function openPath(path) {
  try {
    const sv = await api("surveys/open", { method: "POST", body: { path } });
    S.state.recent = (await api("state")).recent;
    S.survey = sv;
    openSurvey(sv.key, true);
  } catch (e) { toast(e.message, true); }
}

// ---------------------------------------------------------------- import
function importModal() {
  const cards = [];
  const name = h("input", { type: "text", placeholder: "e.g. Barn A dusk 14 June 2026" });
  const list = h("div", { class: "cardlist" });
  const dest = h("input", { type: "text", value: "" });
  const qsv = h("input", { type: "checkbox", checked: S.state.settings.quick_sync });
  const drawCards = () => {
    list.replaceChildren(...(cards.length ? cards.map((c, i) => h("div", { class: "item" },
      h("span", {}, "💾"), h("span", { class: "grow" }, c),
      h("button", { class: "x", title: "Remove", onclick: () => { cards.splice(i, 1); drawCards(); } }, "×"))) :
      [h("div", { class: "faint" }, "No cards added yet.")]));
  };
  const addCard = async () => {
    try {
      const { path } = await api("pick-folder", { method: "POST", body: { title: "Choose a camera card (its drive) or a copied card folder" } });
      if (path && !cards.includes(path)) cards.push(path);
    } catch (e) {
      const p = prompt("Paste the card's drive letter or folder path (e.g. E:\\):");
      if (p) cards.push(p.trim());
    }
    drawCards();
  };
  const pickDest = async () => {
    try {
      const { path } = await api("pick-folder", { method: "POST", body: { title: "Where should surveys be kept?" } });
      if (path) dest.value = path;
    } catch (e) { /* type it */ }
  };
  drawCards();
  modal("Import camera cards", [
    h("label", { class: "field" }, h("span", {}, "Survey name"), name),
    h("div", { class: "field" }, h("span", {}, "Camera cards"),
      h("div", { class: "muted" }, "Add every card from the night: one per camera. You can add a card in a card reader or a folder already copied from one."),
      list, h("div", {}, h("button", { class: "btn small", onclick: addCard }, "+ Add a card"))),
    h("label", { class: "field" }, h("span", {}, "Keep the survey in"),
      h("div", { class: "row" }, dest, h("button", { class: "btn small", onclick: pickDest }, "Change…")),
      h("span", { class: "faint" }, "Leave empty for Documents\\Survey reviews. Use a local drive: review copies play badly over the network.")),
    h("label", { class: "check" }, qsv, h("span", {}, h("b", {}, "Use Intel Quick Sync"), " – much faster on most Intel laptops. Untick if the import fails.")),
  ], [
    h("button", { class: "btn", onclick: closeModal }, "Cancel"),
    h("button", { class: "btn primary", onclick: async () => {
      if (!name.value.trim()) return toast("Give the survey a name first", true);
      if (!cards.length) return toast("Add at least one camera card", true);
      try {
        const r = await api("import", { method: "POST", body: { name: name.value, cards, dest: dest.value.trim() || null, quick_sync: qsv.checked } });
        closeModal();
        const res = await follow(r.task, "Importing " + name.value, "This can take a while for a full night of footage. You can leave it running.");
        if (res) { S.state.recent = (await api("state")).recent; openSurvey(res.key, true); }
      } catch (e) { toast(e.message, true); }
    } }, "Start import"),
  ]);
  setTimeout(() => name.focus(), 50);
}

// progress modal for a background task; resolves with the result (or null on failure)
function follow(tid, title, note) {
  return new Promise((resolve) => {
    const bar = h("div", {});
    const prog = h("div", { class: "progress indet" }, bar);
    const last = h("div", { class: "muted" }, "Starting…");
    const lines = h("div", { class: "log-lines hidden" });
    const more = h("button", { class: "btn small", onclick: () => lines.classList.toggle("hidden") }, "Details");
    modal(title, [note ? h("p", { class: "muted" }, note) : null, prog, last, lines], [more], { noClose: true });
    const tick = async () => {
      let t;
      try { t = await api("tasks/" + tid); } catch (e) { setTimeout(tick, 1500); return; }
      if (t.percent > 0) { prog.classList.remove("indet"); bar.style.width = t.percent + "%"; }
      last.textContent = t.stage || t.last || "Working…";
      lines.textContent = t.lines.join("\n");
      if (t.state === "running") return setTimeout(tick, 800);
      closeModal();
      if (t.state === "done") { toast(`${title}: done`); resolve(t.result); }
      else {
        modal("That didn't work", [h("p", {}, t.error || "Unknown error"),
          h("div", { class: "log-lines" }, t.lines.join("\n"))], [h("button", { class: "btn primary", onclick: closeModal }, "Close")]);
        resolve(null);
      }
    };
    tick();
  });
}

// ---------------------------------------------------------------- survey
async function openSurvey(key, fresh = false) {
  try {
    if (fresh || !S.survey || S.survey.key !== key) S.survey = await api("s/" + key);
  } catch (e) { toast(e.message, true); return home(); }
  S.ws = "video";
  document.querySelectorAll(".ws").forEach((b) => b.classList.toggle("active", b.dataset.ws === "video"));
  if (S.rid && S.survey.recordings.some((r) => r.id === S.rid)) return openRecording(S.rid);
  S.rid = null;
  renderSurvey();
}
async function refreshSurvey() {
  S.survey = await api("s/" + S.survey.key);
}
function recStatus(r) {
  if (r.qa === "approved") return h("span", { class: "chip ok" }, "QA approved");
  if (r.qa === "rejected") return h("span", { class: "chip bad" }, "QA rejected");
  if (r.signed_off_by && r.changed_after_signoff) return h("span", { class: "chip warn" }, "Changed after sign-off");
  if (r.signed_off_by) return h("span", { class: "chip info" }, "Signed off");
  if (r.log_rows) return h("span", { class: "chip" }, "In review");
  return h("span", { class: "chip" }, "Not started");
}
function sidebar() {
  const sv = S.survey;
  return h("aside", { class: "sidebar" },
    h("div", { class: "sidebar-head" },
      h("h2", { title: sv.folder }, sv.name),
      h("div", { class: "sidebar-actions" },
        h("button", { class: "btn small", disabled: !!sv.read_only, onclick: runDetect, title: "Find moments with movement, as a checklist to review" }, "Find movement"),
        h("button", { class: "btn small", disabled: !!sv.read_only, onclick: reportModal }, "Make report"))),
    h("div", { class: "rec-list" }, sv.recordings.map((r, i) =>
      h("div", { class: "rec" + (r.id === S.rid ? " active" : ""), onclick: () => openRecording(r.id) },
        h("div", { class: "t" }, h("span", {}, `${i + 1}. ${niceDate(r.start)}`), recStatus(r)),
        h("div", { class: "s" },
          h("span", {}, dur(r.duration_s)),
          r.camera ? h("span", {}, "· " + r.camera) : null,
          r.detections !== null ? h("span", {}, `· ${r.detections} to check`) : null,
          r.log_rows ? h("span", {}, `· ${r.log_rows} logged`) : null)))),
    h("div", { class: "sidebar-foot" },
      checkoutBox(sv),
      h("div", { class: "row" },
        sv.audit.ok ? h("span", { class: "chip ok", title: `${sv.audit.entries} audit entries, chain intact` }, "Audit trail intact") :
          h("span", { class: "chip bad", title: sv.audit.problems.join("\n") }, "Audit problem"),
        sv.hub ? h("span", { class: "chip info", title: sv.hub.url }, "Team hub") : null),
      h("a", { class: "faint", href: "#", onclick: (e) => { e.preventDefault(); api(`s/${sv.key}/open-folder`, { method: "POST", body: {} }); } }, "Open survey folder")));
}
function checkoutBox(sv) {
  const co = sv.checkout || { state: "free" };
  const since = (t) => (t || "").slice(0, 16).replace("T", " ");
  if (co.state === "local-copy")
    return h("div", { class: "cobox" },
      h("div", {}, h("span", { class: "chip info" }, "Checked out to this laptop"), " since ", since(co.since)),
      h("div", { class: "faint pth", title: co.share }, "From ", co.share),
      h("button", { class: "btn small primary", onclick: checkin, title: "Copy your work back to the share and release it" }, "Check in to the share"));
  if (co.state === "checked-out")
    return h("div", { class: "cobox" },
      h("div", {}, h("span", { class: "chip warn" }, `Checked out by ${co.user || "someone"}`)),
      h("div", { class: "faint" }, `${co.host ? "on " + co.host + " " : ""}since ${since(co.since)}. Read-only here.`),
      h("a", { class: "faint", href: "#", onclick: (e) => { e.preventDefault(); releaseCheckout(); } }, "Release it (if it's stuck)…"));
  return h("div", { class: "cobox" },
    h("button", { class: "btn small", onclick: checkout, title: "Copy to this laptop for fast review; colleagues see it read-only until you check it in" }, "Check out to this laptop"));
}
async function checkout() {
  if (!confirm("Check this survey out to this laptop?\n\nReview copies and logs are copied here for fast review. Colleagues will see it as checked out to you (read-only) until you check it back in.")) return;
  try {
    const r = await api(`s/${S.survey.key}/checkout`, { method: "POST", body: {} });
    const res = await follow(r.task, "Checking out", "Copying the review copies and logs to this laptop.");
    if (res) { S.rid = null; openSurvey(res.key, true); }
  } catch (e) { toast(e.message, true); }
}
async function checkin() {
  try {
    const r = await api(`s/${S.survey.key}/checkin`, { method: "POST", body: {} });
    const res = await follow(r.task, "Checking in", "Copying your logs, sign-offs and reports back to the share, then checking them.");
    if (res) { toast(`Checked in: ${res.files} file(s) copied back`); S.rid = null; openSurvey(res.key, true); }
  } catch (e) { toast(e.message, true); }
}
async function releaseCheckout() {
  const reason = prompt("Why release this check-out? (e.g. laptop lost; the person has left). Any work on their laptop won't come back automatically. This is recorded.");
  if (!reason) return;
  try {
    await api(`s/${S.survey.key}/checkout/release`, { method: "POST", body: { reason } });
    toast("Released"); openSurvey(S.survey.key, true);
  } catch (e) { toast(e.message, true); }
}
function renderSurvey() {
  const sv = S.survey;
  crumbs({ label: "Home", onclick: home }, { label: sv.name });
  const recs = sv.recordings;
  const n = (f) => recs.filter(f).length;
  const signed = n((r) => r.signed_off_by && !r.changed_after_signoff);
  const qa = n((r) => r.qa === "approved");
  const total = recs.reduce((a, r) => a + (r.duration_s || 0), 0);
  const stepsDone = [true, sv.detect_done, recs.length > 0 && signed === recs.length, sv.report];
  mount(h("div", { class: "survey" }, sidebar(),
    h("section", { class: "overview" },
      h("h1", {}, sv.name),
      h("div", { class: "muted" }, `${recs.length} recording${recs.length === 1 ? "" : "s"} · ${dur(total)} of footage`),
      h("div", { class: "stats" },
        stat(recs.length, "recordings"), stat(`${signed}/${recs.length}`, "signed off"),
        stat(`${qa}/${recs.length}`, "QA approved"),
        stat(recs.reduce((a, r) => a + r.log_rows, 0), "events logged")),
      h("div", { class: "steps" },
        ["Imported", "Movement found", "All reviewed", "Report made"].map((k, i) =>
          h("div", { class: "step" + (stepsDone[i] ? " done" : "") },
            h("div", { class: "k" }, `Step ${i + 1}`), h("div", { class: "v" }, (stepsDone[i] ? "✓ " : "") + k)))),
      h("table", { class: "grid" },
        h("thead", {}, h("tr", {}, ["#", "Starts", "Length", "Camera", "Movement", "Logged", "Status", "Reviewer"].map((t) => h("th", {}, t)))),
        h("tbody", {}, recs.map((r, i) => h("tr", { class: "click", onclick: () => openRecording(r.id) },
          h("td", {}, i + 1), h("td", { class: "nowrap" }, niceDate(r.start),
            r.start_source === "file-mtime" ? h("span", { class: "chip warn", title: "Time taken from the file date: check the camera clock" }, "check time") : null),
          h("td", {}, dur(r.duration_s)), h("td", {}, r.camera || "–"),
          h("td", {}, r.detections === null ? "–" : r.detections), h("td", {}, r.log_rows),
          h("td", {}, recStatus(r)), h("td", { class: "muted" }, r.signed_off_by || "–"))))),
      sv.report ? h("p", {}, h("button", { class: "btn", onclick: () => window.open(`/files/${sv.key}/report.html`) }, "Open the last report")) : null)));
}
function stat(n, l) { return h("div", { class: "stat" }, h("div", { class: "n" }, n), h("div", { class: "l" }, l)); }

async function runDetect() {
  if (!confirm("Find movement in every recording? It takes roughly a quarter of the footage length, and you can keep reviewing while it runs.")) return;
  try {
    const r = await api(`s/${S.survey.key}/detect`, { method: "POST", body: {} });
    const res = await follow(r.task, "Finding movement", "Looking for small moving objects in every review copy.");
    if (res) toast(`${res.motion} moments to check`);
    await refreshSurvey();
    S.rid ? openRecording(S.rid) : renderSurvey();
  } catch (e) { toast(e.message, true); }
}

// ---------------------------------------------------------------- recording review
function rec() { return S.survey.recordings.find((r) => r.id === S.rid); }

async function openRecording(rid) {
  releaseLock();
  S.rid = rid; S.detIdx = -1; S.pendingCount = ""; S.history = []; S.openRow = null;
  const r = rec();
  crumbs({ label: "Home", onclick: home }, { label: S.survey.name, onclick: () => { S.rid = null; renderSurvey(); } },
         { label: niceDate(r.start) });
  // QA mode only makes sense once signed off
  if (S.mode === "qa" && !r.signed_off_by) S.mode = "review";
  let rows = [], blocked = null;
  try { rows = (await api(`s/${S.survey.key}/r/${rid}/log?kind=${S.mode}`)).rows; }
  catch (e) { blocked = e.message; S.mode = "review"; rows = (await api(`s/${S.survey.key}/r/${rid}/log?kind=review`)).rows; }
  S.rows = rows;
  try { S.dets = (await api(`s/${S.survey.key}/r/${rid}/detections`)).rows; } catch (e) { S.dets = null; }
  renderRecording();
  if (blocked) toast(blocked, true);
  takeLock();
}

function renderRecording() {
  const r = rec();
  const video = h("video", { id: "vid", preload: "auto", playsinline: true });
  if (r.has_video) video.src = `/media/${S.survey.key}/${encodeURIComponent(r.id)}`;
  const flash = h("div", { class: "flash", id: "flash" });
  const stage = h("div", { class: "stage" },
    r.has_video ? video : h("div", { class: "nov" }, "No review copy for this recording. Re-import the survey to make one."),
    flash, h("div", { class: "lockbar hidden", id: "lockbar" }));
  const tl = h("canvas", { class: "timeline", id: "tl", height: 34 });
  const speed = h("select", { class: "speed", title: "Playback speed ([ and ])", onchange: () => (video.playbackRate = +speed.value) },
    [0.25, 0.5, 1, 1.5, 2, 3, 4].map((s) => h("option", { value: s, selected: s === 1 }, s + "×")));
  speed.id = "speed";
  const skip = S.state.settings.skip_seconds || 5;
  const tb = (label, title, fn, cls = "tbtn") => h("button", { class: cls, title, onclick: fn }, label);
  const transport = h("div", { class: "transport" }, tl,
    h("div", { class: "controls" },
      tb(`−${skip}s`, `Back ${skip} seconds (Shift+←)`, () => seek(-skip)),
      tb("−1s", "Back 1 second (←)", () => seek(-1)),
      tb("◀|", "Back one frame (,)", () => frame(-1)),
      tb("Play", "Play / pause (Space)", togglePlay, "tbtn play"),
      tb("|▶", "Forward one frame (.)", () => frame(1)),
      tb("+1s", "Forward 1 second (→)", () => seek(1)),
      tb(`+${skip}s`, `Forward ${skip} seconds (Shift+→)`, () => seek(skip)),
      speed,
      h("div", { class: "times mono", id: "times" })));
  const player = h("section", { class: "player" }, stage, transport);

  mount(h("div", { class: "survey reviewing" }, sidebar(), player, rightPanel()));
  wireVideo(video);
}

function rightPanel() {
  const r = rec();
  const canQA = !!r.signed_off_by;
  const panel = h("aside", { class: "panel", id: "panel" },
    h("div", { class: "mode" },
      h("button", { class: S.mode === "review" ? "active" : "", onclick: () => setMode("review") }, "Review"),
      h("button", { class: S.mode === "qa" ? "active" : "", disabled: !canQA, title: canQA ? "Independent second review" : "QA starts after the reviewer signs off", onclick: () => setMode("qa") }, "QA check")),
    h("div", { class: "tabs" },
      h("button", { class: "tab" + (S.tab === "log" ? " active" : ""), onclick: () => { S.tab = "log"; redrawPanel(); } },
        S.mode === "qa" ? "QA log" : "Log", ` (${S.rows.length})`),
      h("button", { class: "tab" + (S.tab === "det" ? " active" : ""), onclick: () => { S.tab = "det"; redrawPanel(); } },
        "Movement", S.dets ? ` (${S.dets.filter((d) => d.kind === "motion").length})` : "")),
    h("div", { class: "panel-body", id: "panel-body" }, S.tab === "log" ? logView() : detView()),
    panelFoot());
  return panel;
}
function redrawPanel() {
  const old = $("#panel");
  if (old) old.replaceWith(rightPanel());
  drawTimeline();
}
async function setMode(m) {
  if (m === S.mode) return;
  const r = rec();
  try {
    const res = await api(`s/${S.survey.key}/r/${r.id}/log?kind=${m}`);
    releaseLock();
    S.mode = m; S.rows = res.rows; S.history = [];
    redrawPanel();
    takeLock();
  } catch (e) { toast(e.message, true); }
}

// --- log
function logView() {
  const counts = {};
  S.rows.forEach((x) => (counts[x.event] = (counts[x.event] || 0) + (+x.count || 1)));
  const sp = h("select", { title: "Species or group for the next event", onchange: () => (S.species = sp.value) },
    h("option", { value: "" }, "Species / group…"),
    S.state.species.map((s) => h("option", { value: s, selected: s === S.species }, s)));
  const cnt = h("input", { type: "number", min: 1, value: S.pendingCount || 1, title: "How many bats (or type a number key before the event key)", oninput: () => (S.pendingCount = cnt.value) });
  cnt.id = "count-input";
  const dir = h("select", { title: "Direction", onchange: () => (S.direction = dir.value) },
    S.state.directions.map((d) => h("option", { value: d, selected: d === S.direction }, d || "Direction")));
  const ro = S.readOnly;
  const entry = h("div", { class: "entry" },
    h("div", { class: "r2" }, sp, cnt, dir),
    h("div", { class: "evbtns" }, S.state.events.map(([k, ev]) =>
      h("button", { class: "evbtn", "data-ev": ev, disabled: ro, onclick: () => logEvent(ev), title: `Log ${EV_LABEL[ev].toLowerCase()} at the current time (${k.toUpperCase()})` },
        EV_LABEL[ev], h("kbd", {}, k.toUpperCase())))),
    h("div", { class: "pending-count", id: "pending" }, S.pendingCount && S.pendingCount !== "1" ? `Next event: ${S.pendingCount} bats` : ""));
  const totals = h("div", { class: "totals" }, Object.keys(counts).length ?
    Object.entries(counts).map(([ev, n]) => h("span", { class: "chip" }, h("span", { class: "dot", "data-ev": ev }), `${EV_LABEL[ev] || ev}: ${n}`)) :
    h("span", { class: "faint" }, S.mode === "qa" ? "Your independent QA log. The reviewer's log is hidden until you decide." : "Press a key or button to log what you see at the current time."));
  totals.querySelectorAll(".dot").forEach((d) => (d.style.background = `var(--ev-${d.dataset.ev})`));
  const tbody = h("tbody", {});
  const cur = currentClockSec();
  S.rows.forEach((row, i) => {
    const t = clockToSec(row.clock_time);
    const near = cur !== null && t !== null && Math.abs(t - cur) <= 1;
    const dot = h("span", { class: "dot" }); dot.style.background = `var(--ev-${row.event})`;
    const open = S.openRow === i;
    tbody.append(h("tr", { class: (near ? "near " : "") + (open ? "sel" : "") },
      h("td", { class: "t mono", title: "Go to this moment", onclick: () => seekToClock(row.clock_time) }, row.clock_time),
      h("td", { class: "ev nowrap", title: "Show direction and note", onclick: () => { S.openRow = open ? null : i; redrawPanel(); } },
        dot, EV_LABEL[row.event] || row.event, row.notes || row.direction ? h("span", { class: "faint" }, " ✎") : null),
      h("td", { class: "cnt" }, h("input", { type: "number", min: 1, value: row.count, disabled: ro, title: "Number of bats", onchange: (e) => editRow(i, { count: e.target.value }) })),
      h("td", {}, h("input", { type: "text", value: row.species_group, placeholder: "species", disabled: ro, list: "species-list", onchange: (e) => editRow(i, { species_group: e.target.value }) })),
      h("td", { class: "del" }, ro ? null : h("button", { class: "x", title: "Delete", onclick: () => deleteRow(i) }, "×"))));
    if (open) tbody.append(h("tr", { class: "sel detail" }, h("td", {}),
      h("td", { colspan: 4 }, h("div", { class: "row" },
        h("select", { disabled: ro, onchange: (e) => editRow(i, { direction: e.target.value }) },
          S.state.directions.map((d) => h("option", { value: d, selected: d === row.direction }, d || "Direction"))),
        h("input", { type: "text", class: "grow", value: row.notes, placeholder: "Note (e.g. behaviour, uncertain ID)", disabled: ro, onchange: (e) => editRow(i, { notes: e.target.value }) }),
        h("span", { class: "faint" }, row.observer)))));
  });
  const dl = h("datalist", { id: "species-list" }, S.state.species.map((s) => h("option", { value: s })));
  return h("div", {}, entry, totals, dl,
    S.rows.length ? h("table", { class: "logtable" }, tbody) : h("div", { class: "empty" }, "Nothing logged yet."));
}
// The time of the frame actually on screen (what the burned-in clock shows). The
// video's currentTime can sit a few milliseconds past the frame being displayed, which
// would put the logged time a second ahead of the clock the reviewer is reading.
function shownTime(v) {
  if (S.frameTime !== null && Math.abs(S.frameTime - v.currentTime) < 0.25) return S.frameTime;
  return v.currentTime || 0;
}
function currentClockSec() {
  const v = $("#vid");
  if (!v || !rec()) return null;
  return (startSec(rec().start) + Math.floor(shownTime(v) + 0.001)) % 86400;
}
function logEvent(ev) {
  if (S.readOnly) return toast("Someone else has this recording open for review.", true);
  const t = currentClockSec();
  if (t === null) return;
  const count = Math.max(1, parseInt(S.pendingCount || "1", 10) || 1);
  const row = { clock_time: secToClock(t), event: ev, species_group: S.species, count: String(count),
                direction: S.direction, observer: S.state.settings.observer || "", notes: "" };
  S.history.push(JSON.stringify(S.rows));
  S.rows = [...S.rows, row].sort((a, b) => ordered(a) - ordered(b));
  S.pendingCount = "";
  flashMsg(`${EV_LABEL[ev]}${count > 1 ? " ×" + count : ""} · ${row.clock_time}`);
  saveSoon();
  if (S.tab !== "log") S.tab = "log";
  redrawPanel();
}
function ordered(row) {                  // chronological, across midnight
  const t = clockToSec(row.clock_time) ?? 0;
  const s = startSec(rec().start);
  return ((t - s) + 86400) % 86400;
}
function editRow(i, patch) {
  S.history.push(JSON.stringify(S.rows));
  S.rows = S.rows.map((r, j) => (j === i ? { ...r, ...patch } : r));
  saveSoon();
}
function deleteRow(i) {
  S.history.push(JSON.stringify(S.rows));
  S.rows = S.rows.filter((_, j) => j !== i);
  saveSoon(); redrawPanel();
}
function undo() {
  if (!S.history.length) return toast("Nothing to undo");
  S.rows = JSON.parse(S.history.pop());
  saveSoon(); redrawPanel(); toast("Undone");
}
function saveSoon() {
  const s = $("#savestate");
  s.className = "savestate"; s.textContent = "Saving…";
  clearTimeout(S.saveTimer);
  S.saveTimer = setTimeout(save, 350);
}
async function save() {
  const s = $("#savestate");
  try {
    const r = await api(`s/${S.survey.key}/r/${S.rid}/log?kind=${S.mode}`, { method: "PUT", body: { rows: S.rows } });
    S.rows = r.rows;
    s.className = "savestate ok"; s.textContent = "Saved " + r.saved_at;
    const rr = rec();
    if (S.mode === "review") {
      rr.log_rows = S.rows.length;
      if (rr.signed_off_by) rr.changed_after_signoff = true;
    } else rr.qa_rows = S.rows.length;
    const sb = $(".sidebar");
    if (sb) sb.replaceWith(sidebar());
  } catch (e) {
    s.className = "savestate err"; s.textContent = "Not saved";
    toast(e.message, true);
  }
}

// --- detections
function seenKey() { return `seen:${S.survey.key}:${S.rid}`; }
function seenSet() { try { return new Set(JSON.parse(localStorage.getItem(seenKey()) || "[]")); } catch (e) { return new Set(); } }
function markSeen(i) {
  const s = seenSet(); s.add(i);
  try { localStorage.setItem(seenKey(), JSON.stringify([...s])); } catch (e) { /* ignore */ }
}
function detView() {
  if (!S.dets) return h("div", { class: "empty" },
    h("p", {}, "Movement hasn't been searched for yet."),
    h("button", { class: "btn", onclick: runDetect }, "Find movement"),
    h("p", { class: "faint" }, "It lists moments with small moving objects. You still watch and decide: it's a checklist, not a count."));
  const motion = S.dets.map((d, i) => ({ ...d, i })).filter((d) => d.kind === "motion");
  if (!motion.length) return h("div", { class: "empty" }, "No movement found in this recording.");
  const seen = seenSet();
  return h("div", {},
    h("div", { class: "totals" }, h("span", { class: "faint" }, `${seen.size} of ${motion.length} checked · N next, B previous`)),
    motion.map((d) => h("div", { class: "det" + (d.i === S.detIdx ? " cur" : "") + (seen.has(d.i) ? " seen" : ""), onclick: () => gotoDet(d.i) },
      h("span", {}, seen.has(d.i) ? "✓" : "○"),
      h("span", { class: "tm mono" }, d.clock_time),
      h("span", { class: "muted" }, `${d.duration_s}s${d.direction ? " · " + d.direction : ""}`),
      h("span", { class: "faint" }, `size ${d.peak_pixels}`))));
}
function detOffset(d) {
  const p = (d.video_offset || "0:00:00").split(":").map(Number);
  return p[0] * 3600 + p[1] * 60 + p[2];
}
function gotoDet(i) {
  const v = $("#vid");
  if (!v) return;
  S.detIdx = i; markSeen(i);
  v.currentTime = Math.max(0, detOffset(S.dets[i]) - 1);
  v.pause();
  if (S.tab === "det") redrawPanel(); else drawTimeline();
}
function nextDet(dir) {
  if (!S.dets) return toast("Run 'Find movement' first");
  const motion = S.dets.map((d, i) => ({ d, i })).filter((x) => x.d.kind === "motion");
  if (!motion.length) return;
  const v = $("#vid");
  const now = v ? v.currentTime + (dir > 0 ? 1.5 : -0.5) : 0;
  const list = dir > 0 ? motion : [...motion].reverse();
  const hit = list.find((x) => (dir > 0 ? detOffset(x.d) > now : detOffset(x.d) < now - 1));
  if (hit) gotoDet(hit.i); else toast(dir > 0 ? "That was the last one" : "That was the first one");
}

// --- sign-off / QA footer
function panelFoot() {
  const r = rec();
  const foot = h("div", { class: "panel-foot" });
  if (S.mode === "review") {
    if (r.signed_off_by && !r.changed_after_signoff)
      foot.append(h("div", { class: "banner ok" }, `Signed off by ${r.signed_off_by}. `, r.qa === "approved" ? `QA approved by ${r.qa_by}.` : r.qa === "rejected" ? `QA rejected by ${r.qa_by}: check and sign off again.` : "Waiting for QA."));
    else if (r.changed_after_signoff)
      foot.append(h("div", { class: "banner warn" }, "The log has changed since it was signed off. Sign off again when you've finished."));
    if (!r.signed_off_by || r.changed_after_signoff || r.qa === "rejected")
      foot.append(h("button", { class: "btn primary", disabled: S.readOnly, onclick: signoffModal }, "I've finished: sign off this recording"));
  } else {
    const decided = r.qa === "approved" || r.qa === "rejected";
    if (decided)
      foot.append(h("div", { class: "banner " + (r.qa === "approved" ? "ok" : "bad") }, `QA ${r.qa} by ${r.qa_by}` + (r.qa_agreement != null ? ` · ${Math.round(r.qa_agreement * 100)}% agreement` : "")));
    else
      foot.append(h("button", { class: "btn primary", disabled: S.readOnly, onclick: qaModal }, "Compare with the review and decide"));
  }
  return foot;
}
function signoffModal() {
  const r = rec();
  const note = h("textarea", { placeholder: "Anything the QA reviewer or report writer should know (optional)" });
  modal("Sign off this recording?", [
    h("p", {}, `You logged ${S.rows.length} event${S.rows.length === 1 ? "" : "s"}. Signing off records that your review is complete. The log's fingerprint is kept, so any later change shows up.`),
    S.rows.length ? null : h("div", { class: "banner warn" }, "Nothing is logged. Only sign off if you watched the whole recording and saw no bats."),
    h("label", { class: "field" }, h("span", {}, "Note"), note),
  ], [
    h("button", { class: "btn", onclick: closeModal }, "Not yet"),
    h("button", { class: "btn primary", onclick: async () => {
      try {
        clearTimeout(S.saveTimer); await save();
        const res = await api(`s/${S.survey.key}/r/${r.id}/signoff`, { method: "POST", body: { note: note.value } });
        closeModal(); toast(`Signed off by ${res.by}`);
        await refreshSurvey(); S.lockHeld = false; openRecording(r.id);
      } catch (e) { toast(e.message, true); }
    } }, "Sign off"),
  ]);
}
function qaModal() {
  const r = rec();
  const note = h("textarea", { placeholder: "Reason (required if you reject)" });
  const decide = async (decision) => {
    if (decision === "reject" && !note.value.trim()) return toast("Say why you're rejecting it", true);
    try {
      clearTimeout(S.saveTimer); await save();
      const res = await api(`s/${S.survey.key}/r/${r.id}/qa`, { method: "POST", body: { decision, note: note.value } });
      closeModal();
      const c = res.comparison;
      modal(decision === "approve" ? "QA approved" : "QA rejected", [
        h("div", { class: "cmp" }, stat(`${Math.round(c.agreement * 100)}%`, "agreement"), stat(c.matched, "events matched"), stat(`${c.only_primary} / ${c.only_qa}`, "only in review / only in QA")),
        h("table", { class: "grid" }, h("thead", {}, h("tr", {}, ["Event", "Review", "QA", "Difference"].map((t) => h("th", {}, t)))),
          h("tbody", {}, Object.entries(c.totals).map(([ev, t]) => h("tr", {}, h("td", {}, EV_LABEL[ev] || ev), h("td", {}, t.primary), h("td", {}, t.qa), h("td", {}, t.difference > 0 ? "+" + t.difference : t.difference))))),
      ], [h("button", { class: "btn primary", onclick: closeModal }, "Done")]);
      await refreshSurvey(); openRecording(r.id);
    } catch (e) { toast(e.message, true); }
  };
  modal("QA decision", [
    h("p", {}, `Your QA log has ${S.rows.length} event${S.rows.length === 1 ? "" : "s"}. Deciding compares it with the reviewer's log (events of the same type within 2 seconds count as matching) and records your decision with the figures.`),
    h("label", { class: "field" }, h("span", {}, "Note"), note),
  ], [
    h("button", { class: "btn", onclick: closeModal }, "Cancel"),
    h("button", { class: "btn danger", onclick: () => decide("reject") }, "Reject: needs another look"),
    h("button", { class: "btn primary", onclick: () => decide("approve") }, "Approve"),
  ]);
}

// --- hub lock (only when the survey is linked to a team hub)
async function takeLock() {
  S.readOnly = !!S.survey.read_only;
  if (S.readOnly) {
    const bar = $("#lockbar");
    if (bar) { bar.textContent = "Read only: " + S.survey.read_only; bar.classList.remove("hidden"); }
    redrawPanel();
    return;
  }
  if (!S.survey.hub || !S.rid) return;
  const kind = S.mode;
  const bar = $("#lockbar");
  try {
    const r = await api(`s/${S.survey.key}/r/${S.rid}/lock`, { method: "POST", body: { kind } });
    if (r.locked === false && !r.offline) {
      S.readOnly = true;
      if (bar) { bar.textContent = "Read only: " + r.message; bar.classList.remove("hidden"); }
      redrawPanel();
    } else if (r.offline) {
      toast("Team hub not reachable: you can review, but sign-off needs the hub", true);
    } else {
      S.lockHeld = true;
      S.lockTimer = setInterval(() => api(`s/${S.survey.key}/r/${S.rid}/lock`, { method: "POST", body: { kind } }).catch(() => {}), 10 * 60 * 1000);
    }
  } catch (e) { /* ignore */ }
}
function releaseLock() {
  clearInterval(S.lockTimer); S.lockTimer = null;
  if (S.lockHeld && S.survey && S.rid) {
    api(`s/${S.survey.key}/r/${S.rid}/lock`, { method: "POST", body: { kind: S.mode, release: true } }).catch(() => {});
  }
  S.lockHeld = false;
}

// ---------------------------------------------------------------- video
function wireVideo(v) {
  const r = rec();
  const upd = () => {
    const c = currentClockSec();
    const t = $("#times");
    if (t) t.replaceChildren(h("b", {}, secToClock(c ?? 0)), `   ${hms(shownTime(v))} / ${hms(v.duration || r.duration_s || 0)}`);
    const play = $(".tbtn.play");
    if (play) play.textContent = v.paused ? "Play" : "Pause";
    drawTimeline();
  };
  ["timeupdate", "seeked", "play", "pause", "loadedmetadata"].forEach((e) => v.addEventListener(e, upd));
  S.frameTime = null;
  if (v.requestVideoFrameCallback) {
    const onFrame = (_now, meta) => {
      S.frameTime = meta.mediaTime; upd();
      if (document.body.contains(v)) v.requestVideoFrameCallback(onFrame);
    };
    v.requestVideoFrameCallback(onFrame);
  }
  v.addEventListener("error", () => toast("This video can't be played here. Try re-importing the survey.", true));
  const tl = $("#tl");
  tl.addEventListener("click", (e) => {
    const rect = tl.getBoundingClientRect();
    const d = v.duration || r.duration_s || 0;
    if (d) v.currentTime = ((e.clientX - rect.left) / rect.width) * d;
  });
  new ResizeObserver(drawTimeline).observe(tl);
  const loop = () => { if (!v.paused) drawTimeline(); if (document.body.contains(v)) requestAnimationFrame(loop); };
  requestAnimationFrame(loop);
  upd();
}
function drawTimeline() {
  const c = $("#tl"), v = $("#vid");
  if (!c) return;
  const r = rec();
  const w = c.clientWidth, hh = 34, dpr = window.devicePixelRatio || 1;
  if (c.width !== w * dpr) { c.width = w * dpr; c.height = hh * dpr; }
  const g = c.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.fillStyle = "#262b29"; g.fillRect(0, 0, w, hh);
  const d = (v && v.duration) || r.duration_s || 0;
  if (!d) return;
  const x = (s) => (s / d) * w;
  // quarter-hour gridlines on the camera clock
  const s0 = startSec(r.start);
  g.fillStyle = "#3a403d"; g.font = "10px Segoe UI, sans-serif";
  for (let q = Math.ceil(s0 / 900) * 900; q < s0 + d; q += 900) {
    const px = x(q - s0); g.fillRect(px, 0, 1, hh);
    g.fillStyle = "#7d8782"; g.fillText(secToClock(q).slice(0, 5), px + 3, 10); g.fillStyle = "#3a403d";
  }
  if (S.dets) {
    const seen = seenSet();
    S.dets.forEach((det, i) => {
      if (det.kind !== "motion") return;
      g.fillStyle = seen.has(i) ? "#8a6a2a" : "#f0a020";
      g.fillRect(x(detOffset(det)), 14, Math.max(2, x(+det.duration_s || 0.5)), 8);
    });
  }
  const colours = { emergence: "#2e9d6a", "re-entry": "#3b76c4", pass: "#8b6bc2", foraging: "#c07a1a", other: "#9aa39f" };
  S.rows.forEach((row) => {
    const t = clockToSec(row.clock_time);
    if (t === null) return;
    const off = ((t - s0) + 86400) % 86400;
    g.fillStyle = colours[row.event] || "#fff";
    g.fillRect(x(off) - 1, 24, 3, 10);
  });
  if (v) { g.fillStyle = "#ffffff"; g.fillRect(x(v.currentTime) - 1, 0, 2, hh); }
}
function togglePlay() { const v = $("#vid"); if (v) v.paused ? v.play() : v.pause(); }
function seek(s) { const v = $("#vid"); if (v) v.currentTime = Math.max(0, Math.min((v.duration || 1e9), v.currentTime + s)); }
function frame(n) { const v = $("#vid"); if (!v) return; v.pause(); v.currentTime = Math.max(0, v.currentTime + n / 25); }
function seekToClock(t) {
  const v = $("#vid"), s = clockToSec(t);
  if (!v || s === null) return;
  v.currentTime = Math.max(0, (((s - startSec(rec().start)) + 86400) % 86400) - 1);
  v.pause();
}
function changeSpeed(dir) {
  const sel = $("#speed"); if (!sel) return;
  const i = Math.max(0, Math.min(sel.options.length - 1, sel.selectedIndex + dir));
  sel.selectedIndex = i; $("#vid").playbackRate = +sel.value;
  flashMsg(`Speed ${sel.value}×`);
}
function flashMsg(m) {
  const f = $("#flash"); if (!f) return;
  f.textContent = m; f.classList.add("on");
  clearTimeout(flashMsg._t); flashMsg._t = setTimeout(() => f.classList.remove("on"), 1100);
}

// ---------------------------------------------------------------- keyboard
function onKey(e) {
  if ($("#modal-root").childElementCount) { if (e.key === "Escape" && !closeModal.locked) closeModal(); return; }
  const tag = (e.target.tagName || "").toLowerCase();
  if (["input", "select", "textarea"].includes(tag)) { if (e.key === "Escape") e.target.blur(); return; }
  if (e.key === "?") { e.preventDefault(); return keysModal(); }
  if (S.ws === "acoustic" && AC.view && $("#ac-panel")) return acKey(e);
  if (!$("#vid")) return;
  const k = e.key.toLowerCase();
  const skip = S.state.settings.skip_seconds || 5;
  if ((e.ctrlKey || e.metaKey) && k === "z") { e.preventDefault(); return undo(); }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const evmap = Object.fromEntries(S.state.events);
  if (k === " ") { e.preventDefault(); togglePlay(); }
  else if (k === "arrowleft") { e.preventDefault(); seek(e.shiftKey ? -skip : -1); }
  else if (k === "arrowright") { e.preventDefault(); seek(e.shiftKey ? skip : 1); }
  else if (k === ",") frame(-1);
  else if (k === ".") frame(1);
  else if (k === "[") changeSpeed(-1);
  else if (k === "]") changeSpeed(1);
  else if (k === "n") nextDet(1);
  else if (k === "b") nextDet(-1);
  else if (/^[0-9]$/.test(k)) {
    S.pendingCount = ((S.pendingCount === "1" ? "" : S.pendingCount) + k).replace(/^0+/, "").slice(0, 3);
    const p = $("#pending"); if (p) p.textContent = S.pendingCount ? `Next event: ${S.pendingCount} bats` : "";
    const ci = $("#count-input"); if (ci) ci.value = S.pendingCount || 1;
  }
  else if (k === "escape") { S.pendingCount = ""; const p = $("#pending"); if (p) p.textContent = ""; }
  else if (evmap[k]) { e.preventDefault(); logEvent(evmap[k]); }
}

// ---------------------------------------------------------------- report
function reportModal() {
  const sv = S.survey;
  const signed = sv.recordings.filter((r) => r.signed_off_by && !r.changed_after_signoff).length;
  const site = h("input", { type: "text", value: sv.name, placeholder: "Site name as it should appear in the report" });
  const lat = h("input", { type: "text", placeholder: "e.g. 51.4545", inputmode: "decimal" });
  const lon = h("input", { type: "text", placeholder: "e.g. -2.5879 (west is negative)", inputmode: "decimal" });
  const off = h("select", {}, h("option", { value: "" }, "From the camera files (recommended)"),
    h("option", { value: "1" }, "BST (UTC+1)"), h("option", { value: "0" }, "GMT (UTC+0)"));
  modal("Make the report", [
    signed < sv.recordings.length ? h("div", { class: "banner warn" }, `${sv.recordings.length - signed} of ${sv.recordings.length} recordings aren't signed off yet. The report will list them under "Check before use".`) : null,
    h("label", { class: "field" }, h("span", {}, "Site name"), site),
    h("div", { class: "row" },
      h("label", { class: "field grow" }, h("span", {}, "Latitude"), lat),
      h("label", { class: "field grow" }, h("span", {}, "Longitude"), lon)),
    h("p", { class: "faint" }, "The location is used only to work out sunset and sunrise. It's never saved in the report or any other file, so reports can be shared without revealing roost locations."),
    h("label", { class: "field" }, h("span", {}, "Camera clock"), off),
  ], [
    h("button", { class: "btn", onclick: closeModal }, "Cancel"),
    h("button", { class: "btn primary", onclick: async () => {
      try {
        const r = await api(`s/${sv.key}/report`, { method: "POST", body: { site: site.value, lat: lat.value.trim(), lon: lon.value.trim(), utc_offset: off.value } });
        closeModal();
        const res = await follow(r.task, "Making the report");
        if (res) {
          await refreshSurvey(); if (!S.rid) renderSurvey();
          modal("Report ready", [
            h("p", {}, "The report, summary tables (CSV for Excel) and event list are in the survey folder."),
            res.issues.length ? h("div", { class: "banner warn" }, `${res.issues.length} thing(s) to check, listed at the top of the report.`) : h("div", { class: "banner ok" }, "No problems found."),
          ], [
            h("button", { class: "btn", onclick: () => api(`s/${sv.key}/open-folder`, { method: "POST", body: {} }) }, "Open folder"),
            h("button", { class: "btn primary", onclick: () => { window.open(res.url); closeModal(); } }, "Open report"),
          ]);
        }
      } catch (e) { toast(e.message, true); }
    } }, "Make report"),
  ]);
  setTimeout(() => lat.focus(), 50);
}

// ---------------------------------------------------------------- settings, help
function settingsModal(first = false) {
  const s = S.state.settings;
  const obs = h("input", { type: "text", value: s.observer, maxlength: 6, placeholder: "e.g. LB" });
  const sp = h("select", {}, h("option", { value: "" }, "None"), S.state.species.map((x) => h("option", { value: x, selected: x === s.default_species }, x)));
  const skip = h("select", {}, [2, 5, 10, 15, 30].map((n) => h("option", { value: n, selected: n === s.skip_seconds }, `${n} seconds`)));
  const qsv = h("input", { type: "checkbox", checked: s.quick_sync });
  modal(first ? "Welcome to Survey Studio" : "Your details", [
    first ? h("p", {}, "Your initials go into every event you log, so reports show who recorded what.") : null,
    h("label", { class: "field" }, h("span", {}, "Your initials"), obs),
    h("div", { class: "muted" }, "Signed in as ", h("b", {}, S.state.user.user), ". Sign-offs and QA are recorded under this account."),
    h("label", { class: "field" }, h("span", {}, "Usual species or group (pre-selected when logging)"), sp),
    h("label", { class: "field" }, h("span", {}, "Shift + arrow jumps"), skip),
    h("label", { class: "check" }, qsv, h("span", {}, "Use Intel Quick Sync when importing (faster on most Intel laptops)")),
  ], [
    first ? null : h("button", { class: "btn", onclick: closeModal }, "Cancel"),
    h("button", { class: "btn primary", onclick: async () => {
      if (!obs.value.trim()) return toast("Enter your initials", true);
      S.state.settings = await api("settings", { method: "POST", body: { observer: obs.value, default_species: sp.value, skip_seconds: +skip.value, quick_sync: qsv.checked } });
      if (!S.species) S.species = S.state.settings.default_species;
      renderUser(); closeModal();
    } }, "Save"),
  ], { noClose: first });
  setTimeout(() => obs.focus(), 50);
}
function keysModal() {
  const k = (keys, what) => [h("span", {}, ...keys.map((x) => h("kbd", {}, x))), h("span", {}, what)];
  modal("Keyboard shortcuts", [h("div", { class: "keys" },
    k(["Space"], "Play / pause"), k(["←", "→"], "Back / forward 1 second"),
    k(["Shift", "←"], `Back ${S.state.settings.skip_seconds || 5} seconds (Shift+→ forward)`),
    k([",", "."], "One frame back / forward"), k(["[", "]"], "Slower / faster"),
    k(["E"], "Log an emergence at the current time"), k(["R"], "Log a re-entry"), k(["P"], "Log a pass"),
    k(["F"], "Log foraging"), k(["O"], "Log other"),
    k(["3", "E"], "Type a number first to log several bats at once"),
    k(["N", "B"], "Next / previous movement to check"), k(["Ctrl", "Z"], "Undo"), k(["?"], "This list"))],
    [h("button", { class: "btn primary", onclick: closeModal }, "Close")]);
}

// ---------------------------------------------------------------- acoustic
const AC = { view: null, i: 0, calls: null, zoom: null, fmax: 125000, busy: false };

async function acoustic() {
  S.survey = null; S.rid = null;
  if (AC.view) return acousticFolder();
  crumbs({ label: "Acoustic analysis" });
  let recent = [];
  try { recent = (await api("acoustic/recent")).recent; } catch (e) { /* none */ }
  mount(h("div", { class: "home" },
    h("h1", {}, "Bat detector recordings"),
    h("p", { class: "lead" }, "Open a folder of full-spectrum WAV files from any detector. See each call as a spectrogram with measurements, listen slowed down, and record your identification."),
    h("div", { class: "cards" },
      h("button", { class: "card", onclick: pickAcoustic },
        h("div", { class: "ico" }, "〰"), h("h2", {}, "Open a folder of recordings"),
        h("p", {}, "WAV files are only read, never changed. Your identifications go in a labels file in the same folder."))),
    h("div", { class: "section-title" }, "Recent folders"),
    recent.length ? h("div", { class: "recent" }, recent.map((r) =>
      h("div", { class: "recent-item", onclick: () => r.exists ? openAcoustic(r.path) : toast("That folder has moved or been deleted", true) },
        h("div", { class: "grow" }, h("div", { class: "nm" }, r.name), h("div", { class: "pth" }, r.path)),
        h("span", { class: "faint" }, "Opened " + (r.opened || "").replace("T", " "))))) :
      h("p", { class: "muted" }, "No recordings opened yet.")));
}
async function pickAcoustic() {
  try {
    const { path } = await api("pick-folder", { method: "POST", body: { title: "Choose a folder of bat detector recordings" } });
    if (path) openAcoustic(path);
  } catch (e) {
    const p = prompt("Paste the folder's path:");
    if (p) openAcoustic(p);
  }
}
async function openAcoustic(path) {
  try {
    AC.view = await api("acoustic/open", { method: "POST", body: { path } });
    AC.i = 0; AC.zoom = null; AC.calls = null;
    acousticFolder();
  } catch (e) { toast(e.message, true); }
}
function acFile() { return AC.view.files[AC.i]; }
function acTime(ts) { return ts ? ts.replace("T", " ").slice(0, 19) : "time unknown"; }

async function acousticFolder() {
  const v = AC.view;
  crumbs({ label: "Acoustic analysis", onclick: () => { AC.view = null; acoustic(); } }, { label: v.name });
  const f = acFile();
  AC.calls = null;
  mount(h("div", { class: "survey reviewing ac" }, acSidebar(), acStage(), acPanel()));
  const el = $(`.rec[data-i="${AC.i}"]`); if (el) el.scrollIntoView({ block: "nearest" });
  try {
    const r = await api(`a/${v.key}/f/${f.i}/calls`);
    if (acFile() !== f) return;
    AC.calls = r.calls;
    const old = $("#ac-panel"); if (old) old.replaceWith(acPanel());
    drawCallStrip();
  } catch (e) { toast(e.message, true); }
}
function acSidebar() {
  const v = AC.view;
  const done = v.files.filter((x) => x.manual_id).length;
  return h("aside", { class: "sidebar" },
    h("div", { class: "sidebar-head" },
      h("h2", { title: v.folder }, v.name),
      h("div", { class: "muted" }, `${v.files.length} recordings · ${done} identified`),
      h("div", { class: "sidebar-actions" },
        h("button", { class: "btn small", onclick: pickAcoustic }, "Open folder…"),
        h("button", { class: "btn small", onclick: () => api(`a/${v.key}/open-folder`, { method: "POST", body: {} }) }, "Show files"))),
    h("div", { class: "rec-list" }, v.files.map((x) =>
      h("div", { class: "rec" + (x.i === AC.i ? " active" : ""), "data-i": x.i, onclick: () => { AC.i = x.i; AC.zoom = null; acousticFolder(); } },
        h("div", { class: "t" }, h("span", { class: "mono" }, acTime(x.timestamp).slice(11) || x.name),
          x.error ? h("span", { class: "chip bad" }, "can't read") :
            x.manual_id ? h("span", { class: "chip ok" }, x.manual_id) : h("span", { class: "chip" }, "to check")),
        h("div", { class: "s" }, h("span", {}, x.name),
          x.auto_id ? h("span", {}, "· auto: " + x.auto_id) : null)))),
    h("div", { class: "sidebar-foot" }, h("span", {}, "↑ ↓ previous / next recording")));
}
function acStage() {
  const v = AC.view, f = acFile();
  const q = new URLSearchParams({ fmax: AC.fmax });
  if (AC.zoom) { q.set("start", AC.zoom.start); q.set("dur", AC.zoom.dur); }
  const img = h("img", { class: "spec", alt: "Spectrogram", src: `/acoustic/${v.key}/${f.i}/spec.png?${q}` });
  const axis = h("div", { class: "faxis" });
  const step = AC.fmax > 150000 ? 50 : 20;
  for (let k = step; k * 1000 < AC.fmax; k += step) {
    const line = h("div", { class: "fline" }, h("span", {}, `${k} kHz`));
    line.style.bottom = `${(k * 1000 / AC.fmax) * 100}%`;
    axis.append(line);
  }
  const audio = h("audio", { id: "ac-audio", preload: "none", src: `/acoustic/${v.key}/${f.i}/te.wav?x=10` });
  const fsel = h("select", { class: "speed wide", title: "Frequency range", onchange: () => { AC.fmax = +fsel.value; acousticFolder(); } },
    [[125000, "0–125 kHz"], [150000, "0–150 kHz"], [250000, "0–250 kHz"]].map(([val, l]) => h("option", { value: val, selected: val === AC.fmax }, l)));
  const tb = (label, title, fn, cls = "tbtn") => h("button", { class: cls, title, onclick: fn }, label);
  return h("section", { class: "player" },
    h("div", { class: "stage spec-stage" }, h("div", { class: "spec-wrap" }, img, axis),
      h("div", { class: "spec-cap" }, AC.zoom ? `Zoomed: ${AC.zoom.start.toFixed(2)}–${(AC.zoom.start + AC.zoom.dur).toFixed(2)} s` : `Whole recording · ${f.duration_s ?? "?"} s`)),
    h("div", { class: "transport" },
      h("canvas", { class: "timeline", id: "callstrip", height: 34, title: "Calls found. Click one to zoom in." }),
      h("div", { class: "controls" },
        tb("▲ Prev", "Previous recording (↑)", () => acMove(-1)),
        tb("Play ×10", "Play slowed down 10 times so calls are audible (Space)", () => { const a = $("#ac-audio"); a.paused ? a.play() : a.pause(); }, "tbtn play"),
        tb("Whole file", "Zoom out (Z)", () => { AC.zoom = null; acousticFolder(); }),
        fsel,
        tb("Next ▼", "Next recording (↓)", () => acMove(1)),
        h("div", { class: "times mono" }, `${f.sample_rate ? f.sample_rate / 1000 + " kHz" : ""} · ${f.bits || "?"}-bit${f.detector ? " · " + f.detector : ""}`)),
      audio));
}
function drawCallStrip() {
  const c = $("#callstrip"), f = acFile();
  if (!c || !f) return;
  const w = c.clientWidth, hh = 34, dpr = window.devicePixelRatio || 1;
  c.width = w * dpr; c.height = hh * dpr;
  const g = c.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.fillStyle = "#262b29"; g.fillRect(0, 0, w, hh);
  const d = f.duration_s || 1;
  (AC.calls || []).forEach((call) => {
    g.fillStyle = "#6fd3a0"; g.fillRect((call.start_s / d) * w, 8, Math.max(2, (call.duration_ms / 1000 / d) * w), 18);
  });
  if (AC.zoom) { g.strokeStyle = "#fff"; g.strokeRect((AC.zoom.start / d) * w, 2, (AC.zoom.dur / d) * w, 30); }
  c.onclick = (e) => {
    const t = ((e.clientX - c.getBoundingClientRect().left) / w) * d;
    AC.zoom = { start: Math.max(0, t - 0.1), dur: 0.25 }; acousticFolder();
  };
}
function acPanel() {
  const f = acFile(), v = AC.view;
  const calls = AC.calls;
  const opts = ["", ...S.state.species, "No bat (noise)", "Social call", "Unsure"];
  const sp = h("select", {}, opts.map((o) => h("option", { value: o, selected: o === f.manual_id }, o || "Your identification…")));
  const notes = h("input", { type: "text", value: f.notes, placeholder: "Note (optional)" });
  sp.id = "ac-species";
  const save = async () => {
    try {
      const r = await api(`a/${v.key}/f/${f.i}/label`, { method: "PUT", body: { manual_id: sp.value, notes: notes.value, calls: calls ? calls.length : "" } });
      f.manual_id = sp.value; f.notes = notes.value;
      const st = $("#savestate"); st.className = "savestate ok"; st.textContent = "Saved " + r.saved_at;
      acMove(1);
    } catch (e) { toast(e.message, true); }
  };
  const med = (a) => { if (!a.length) return null; const s = [...a].sort((x, y) => x - y); return s[Math.floor(s.length / 2)]; };
  let summary = h("div", { class: "empty" }, "Measuring calls…");
  if (calls) summary = calls.length ? h("div", { class: "totals" },
      h("span", { class: "chip info" }, `${calls.length} calls`),
      h("span", { class: "chip" }, `end freq ~${med(calls.map((c) => c.f_end_khz))} kHz`),
      h("span", { class: "chip" }, `peak ~${med(calls.map((c) => c.f_peak_khz))} kHz`),
      h("span", { class: "chip" }, `~${med(calls.map((c) => c.duration_ms))} ms`)) :
    h("div", { class: "totals" }, h("span", { class: "chip warn" }, "No calls found: noise or very faint"));
  return h("aside", { class: "panel", id: "ac-panel" },
    h("div", { class: "entry" },
      h("div", { class: "muted" }, acTime(f.timestamp), f.has_location ? " · location in file (not shown)" : ""),
      f.auto_id ? h("div", {}, "Detector's auto ID: ", h("b", {}, f.auto_id)) : h("div", { class: "faint" }, "No auto ID in the file"),
      h("label", { class: "field" }, h("span", {}, "Your identification"), sp),
      notes,
      h("button", { class: "btn primary", onclick: save, title: "Save and go to the next recording (Enter)" }, "Save and next ↵")),
    summary,
    h("div", { class: "panel-body" }, calls && calls.length ? h("table", { class: "logtable" },
      h("tbody", {}, h("tr", {}, ["Start", "ms", "kHz", "Peak"].map((t) => h("td", { class: "faint" }, t))),
        calls.slice(0, 300).map((c) => h("tr", {},
          h("td", { class: "t mono", title: "Zoom to this call", onclick: () => { AC.zoom = { start: Math.max(0, c.start_s - 0.05), dur: 0.2 }; acousticFolder(); } }, c.start_s.toFixed(3) + "s"),
          h("td", {}, c.duration_ms), h("td", { class: "nowrap" }, `${c.f_start_khz}→${c.f_end_khz}`), h("td", {}, c.f_peak_khz))))) : null),
    h("div", { class: "panel-foot" }, h("div", { class: "faint" }, "Measurements are a guide. The identification is yours, and it's saved with your initials in survey_studio_labels.csv in the recordings folder.")));
}
function acMove(d) {
  const n = AC.view.files.length;
  const next = Math.max(0, Math.min(n - 1, AC.i + d));
  if (next === AC.i) return toast(d > 0 ? "That was the last recording" : "That was the first recording");
  AC.i = next; AC.zoom = null; acousticFolder();
}
function acKey(e) {
  const k = e.key;
  if (k === "ArrowDown") { e.preventDefault(); acMove(1); }
  else if (k === "ArrowUp") { e.preventDefault(); acMove(-1); }
  else if (k === " ") { e.preventDefault(); const a = $("#ac-audio"); if (a) a.paused ? a.play() : a.pause(); }
  else if (k.toLowerCase() === "z") { AC.zoom = null; acousticFolder(); }
  else if (k === "Enter") { const b = $("#ac-panel .btn.primary"); if (b) b.click(); }
}

// ---------------------------------------------------------------- modal
function modal(title, body, actions, opts = {}) {
  const root = $("#modal-root");
  closeModal.locked = !!opts.noClose;
  const m = h("div", { class: "backdrop", onclick: (e) => { if (e.target === e.currentTarget && !opts.noClose) closeModal(); } },
    h("div", { class: "modal" + (opts.wide ? " wide" : ""), role: "dialog", "aria-modal": "true", "aria-label": title },
      h("header", {}, h("h2", {}, title)),
      h("div", { class: "body" }, body),
      h("footer", {}, actions)));
  root.replaceChildren(m);
}
function closeModal() { $("#modal-root").replaceChildren(); closeModal.locked = false; }

window.addEventListener("beforeunload", releaseLock);
boot();
