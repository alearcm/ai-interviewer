/* Web pane client. The wire format is the transcript: the server
 * streams {"i": index, "row": {...}} rows over a WebSocket; on
 * reconnect we ask it to replay from the last index we saw, so a
 * dropped connection (hello, iPad Safari) is invisible. */
"use strict";

const $ = (id) => document.getElementById(id);

/* ---------------- landing ---------------- */

let packs = [];

async function initLanding() {
  const res = await fetch("/api/packs");
  if (res.status === 401) { location.href = "/login"; return; }
  packs = await res.json();
  const sel = $("pack-select");
  sel.innerHTML = "";
  for (const p of packs) {
    const o = document.createElement("option");
    o.value = p.name;
    o.textContent = `${p.title} (${p.minutes} min${p.workspace ? "" : ", no workspace"})`;
    sel.appendChild(o);
  }
  sel.onchange = fillTasks;
  fillTasks();
  renderSessions();
}

function fillTasks() {
  const p = packs.find((x) => x.name === $("pack-select").value);
  const sel = $("task-select");
  sel.innerHTML = '<option value="">random / pack order</option>';
  for (const t of p ? p.tasks : []) {
    const o = document.createElement("option");
    o.value = t.id;
    o.textContent = t.title;
    sel.appendChild(o);
  }
}

async function renderSessions() {
  const list = await (await fetch("/api/sessions")).json();
  const host = $("session-list");
  host.innerHTML = list.length ? "" : "<div class='dim'>none yet</div>";
  for (const s of list) {
    const row = document.createElement("div");
    row.className = "row";
    if (s.past) {
      row.innerHTML = `<span class="mono dim">${s.id}</span><span class="spacer"></span>
        <a href="/api/sessions/${s.id}/report" target="_blank">report</a>`;
    } else {
      row.innerHTML = `<span class="mono">${s.id}</span><span class="dim">${s.over ? "over" : "live"}</span>
        <span class="spacer"></span><a href="#s=${s.id}">${s.over ? "view" : "rejoin"}</a>`;
    }
    host.appendChild(row);
  }
}

$("start").onclick = async () => {
  $("start-error").textContent = "";
  const body = {
    pack: $("pack-select").value,
    minutes: $("minutes").value || null,
    task: $("task-select").value || null,
    provider: $("provider").value || null,
  };
  const res = await fetch("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) { $("start-error").textContent = data.error || "failed"; return; }
  location.hash = "s=" + data.id;
};

/* ---------------- session view ---------------- */

let ws = null, meta = null, seen = 0, clockBase = null, clockTimer = null;
let editor = null, dirty = false, saveTimer = null, lastPulse = 0, suppressChange = false;

function fmt(t) {
  t = Math.max(0, Math.floor(t));
  return String(Math.floor(t / 60)).padStart(2, "0") + ":" + String(t % 60).padStart(2, "0");
}

function addMsg(cls, html) {
  const el = document.createElement("div");
  el.className = "msg " + cls;
  el.innerHTML = html;
  const log = $("log");
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el;
}

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function renderRow(row) {
  const t = `<span class="t">${fmt(row.t)}</span>`;
  switch (row.kind) {
    case "interviewer_message":
      addMsg("int", t + esc(row.text));
      window.speakLine && window.speakLine(row.text);
      break;
    case "user_message":
      addMsg("you", t + esc(row.text));
      break;
    case "task_presented": {
      // the current task lives in the panel; every task also lands in
      // the chat stream as a card, so past drills stay in scrollback
      $("task-panel").innerHTML = `<h3>${esc(row.title)}</h3>${esc(row.statement)}`;
      const panel = $("task-panel");
      panel.classList.remove("pulse");
      void panel.offsetWidth; // restart the animation
      panel.classList.add("pulse");
      for (const open of document.querySelectorAll("details.task-card[open]")) {
        open.removeAttribute("open");
      }
      const pos = row.seq && row.of ? ` · drill ${row.seq}/${row.of}` : "";
      if (row.seq && row.of) $("drill-pos").textContent = `drill ${row.seq}/${row.of}`;
      const card = document.createElement("details");
      card.className = "task-card";
      card.setAttribute("open", "");
      card.innerHTML = `<summary><span class="t">${fmt(row.t)}</span>▸ ${esc(row.title)}${esc(pos)}</summary><div>${esc(row.statement)}</div>`;
      const log = $("log");
      log.appendChild(card);
      log.scrollTop = log.scrollHeight;
      break;
    }
    case "run_executed": {
      const ok = row.exit_status === 0;
      const out = [row.out, row.err].filter(Boolean).join("\n").trim();
      const el = addMsg("run", `${t}$ ${esc(row.cmd)} <span class="${ok ? "status-ok" : "status-bad"}">(${ok ? "passed" : "exit " + row.exit_status})</span>\n${esc(out)}`);
      if (ok) el.classList.add("ok");
      break;
    }
    case "file_saved":
      $("save-state").textContent = "observed ✓ " + fmt(row.t);
      break;
    case "pad_write":
      if (editor && row.path === meta.primary_file) {
        suppressChange = true;
        if (row.mode === "append" && row.rule !== "seed") {
          const end = { line: editor.lineCount(), ch: 0 };
          editor.replaceRange("\n" + row.text + "\n", end, end);
        } else {
          editor.setValue(row.text);
        }
        suppressChange = false;
      }
      if (row.rule !== "seed") addMsg("sys", `<span class="t">${fmt(row.t)}</span>interviewer wrote into ${esc(row.path)}`);
      break;
    case "session_end":
      onSessionEnd(row);
      break;
    default:
      break; // gate decisions, pulses, idle, marks: invisible in the UI
  }
}

async function selfCheck() {
  const el = $("check-result");
  el.textContent = "running…";
  const res = await fetch(`/api/sessions/${meta.id}/checks`, { method: "POST" });
  const results = await res.json();
  if (!results.length) { el.textContent = "this pack has no hidden checks"; return; }
  const ok = results.filter((r) => r.status === "ok").length;
  el.textContent = `${ok}/${results.length} pass — ` +
    results.map((r) => `${r.title}: ${r.status}`).join(", ");
}
window.selfCheck = selfCheck;

function onSessionEnd(row) {
  $("banner").classList.remove("hidden");
  $("banner").innerHTML = `session over (${esc(row.reason)}) — <a target="_blank" href="/api/sessions/${meta.id}/report">open the report</a>
    · <a href="#" onclick="selfCheck(); return false;">run self-check</a><div id="check-result" class="dim"></div>`;
  $("btn-report").href = `/api/sessions/${meta.id}/report`;
  $("btn-report").classList.remove("hidden");
  if (clockTimer) clearInterval(clockTimer);
  $("clock").textContent = "done";
}

function startClock() {
  if (clockTimer) clearInterval(clockTimer);
  clockTimer = setInterval(() => {
    if (!clockBase) return;
    const elapsed = clockBase.t + (performance.now() - clockBase.at) / 1000;
    const left = meta.minutes * 60 - elapsed;
    const el = $("clock");
    el.textContent = fmt(left);
    el.classList.toggle("low", left < 300);
  }, 500);
}

function connect(sid) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/api/sessions/${sid}/ws?resume=${seen}`);
  ws.onmessage = (ev) => {
    const data = JSON.parse(ev.data);
    if (data.hello) {
      meta = data.hello;
      $("s-title").textContent = meta.title;
      clockBase = { t: meta.now_t, at: performance.now() };
      startClock();
      if (meta.has_workspace && meta.primary_file) setupEditor();
      else $("right").classList.add("hidden");
      if (meta.over) $("btn-report").href = `/api/sessions/${meta.id}/report`;
      return;
    }
    if (data.notice !== undefined) { addMsg("sys", esc(data.notice)); return; }
    if (data.i === undefined) return;
    if (data.i < seen) return; // duplicate across replay/live seam
    seen = data.i + 1;
    if (!data.row.stub) renderRow(data.row);
    if (data.row.t !== undefined) clockBase = { t: data.row.t, at: performance.now() };
  };
  ws.onclose = () => {
    if (meta && !document.hidden) setTimeout(() => connect(sid), 1500);
    else if (meta) {
      const onVis = () => { document.removeEventListener("visibilitychange", onVis); connect(sid); };
      document.addEventListener("visibilitychange", onVis);
    }
  };
}

/* ---------------- editor ---------------- */

async function setupEditor() {
  $("right").classList.remove("hidden");
  $("filename").textContent = meta.primary_file;
  // pack default first; a command the user typed themselves wins
  $("run-cmd").value = localStorage.getItem("run-cmd:" + meta.pack) || meta.run_cmd || "";
  if (editor) return;
  const res = await fetch(`/api/sessions/${meta.id}/file?path=${encodeURIComponent(meta.primary_file)}`);
  const data = await res.json();
  editor = CodeMirror($("editor-host"), {
    value: data.content || "",
    mode: "python",
    lineNumbers: true,
    indentUnit: 4,
    viewportMargin: Infinity,
    extraKeys: { Tab: (cm) => cm.replaceSelection("    ", "end") },
  });
  editor.on("change", () => {
    if (suppressChange) return;
    dirty = true;
    $("save-state").textContent = "typing…";
    // autosave: quiet gap -> save (the engine's watcher debounces again)
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(saveFile, 1200);
    // edit pulse: typing cadence signal, throttled, no content
    const now = Date.now();
    if (now - lastPulse > 4000 && ws && ws.readyState === 1) {
      lastPulse = now;
      ws.send(JSON.stringify({ pulse: { path: meta.primary_file, delta: 1 } }));
    }
  });
}

function saveFile() {
  if (!dirty || !ws || ws.readyState !== 1) return;
  dirty = false;
  $("save-state").textContent = "saving…";
  ws.send(JSON.stringify({ save: { path: meta.primary_file, content: editor.getValue() } }));
}

/* ---------------- controls ---------------- */

$("say-form").onsubmit = (e) => {
  e.preventDefault();
  const text = $("say").value.trim();
  if (text && ws && ws.readyState === 1) {
    if (text.startsWith("/")) ws.send(JSON.stringify({ command: text }));
    else ws.send(JSON.stringify({ say: text }));
    $("say").value = "";
  }
};
$("btn-next").onclick = () => ws && ws.send(JSON.stringify({ command: "/next" }));
$("btn-end").onclick = () => {
  if (confirm("End the session?")) ws.send(JSON.stringify({ command: "/end" }));
};
$("btn-run").onclick = () => {
  const cmd = $("run-cmd").value.trim() || (meta.run_cmd || "").trim();
  if (!cmd) { $("save-state").textContent = "enter a run command first"; return; }
  if (cmd !== (meta.run_cmd || "").trim()) localStorage.setItem("run-cmd:" + meta.pack, cmd);
  saveFile();
  ws.send(JSON.stringify({ run: cmd }));
};

/* ---------------- voice (browser-native, optional) ---------------- */

let voiceOn = localStorage.getItem("voice") === "on";

function renderVoiceButton() {
  const b = $("btn-voice");
  b.textContent = "voice: " + (voiceOn ? "on" : "off");
  b.classList.toggle("on", voiceOn);
}
$("btn-voice").onclick = () => {
  voiceOn = !voiceOn;
  localStorage.setItem("voice", voiceOn ? "on" : "off");
  if (!voiceOn && window.speechSynthesis) speechSynthesis.cancel();
  renderVoiceButton();
};
renderVoiceButton();

window.speakLine = (text) => {
  if (!voiceOn || !window.speechSynthesis) return;
  const u = new SpeechSynthesisUtterance(text);
  u.rate = 1.05;
  speechSynthesis.speak(u);
};

// Dictation via the Web Speech API where the browser has it (Chrome,
// recent Safari). Elsewhere the button hides — on iPad the keyboard's
// own mic key works fine in the input field.
const Rec = window.SpeechRecognition || window.webkitSpeechRecognition;
let rec = null;
if (Rec) {
  $("btn-mic").classList.remove("hidden");
  $("btn-mic").onclick = () => {
    if (rec) { rec.stop(); return; }
    rec = new Rec();
    rec.interimResults = true;
    rec.onresult = (e) => {
      const text = Array.from(e.results).map((r) => r[0].transcript).join("");
      $("say").value = text;
      if (e.results[e.results.length - 1].isFinal) {
        rec.stop();
        $("say-form").requestSubmit();
      }
    };
    rec.onend = () => { rec = null; $("btn-mic").classList.remove("on"); };
    rec.onerror = () => { rec = null; $("btn-mic").classList.remove("on"); };
    $("btn-mic").classList.add("on");
    rec.start();
  };
}

/* ---------------- boot ---------------- */

function boot() {
  const m = location.hash.match(/s=([\w.-]+)/);
  if (m) {
    $("landing").classList.add("hidden");
    $("session").classList.remove("hidden");
    seen = 0;
    connect(m[1]);
  } else {
    $("session").classList.add("hidden");
    $("landing").classList.remove("hidden");
    initLanding();
  }
}
window.addEventListener("hashchange", () => location.reload());
boot();
