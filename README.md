# ai-interviewer

A generic engine for timed, observed, adversarial practice sessions with an
AI interviewer — plus **scenario packs** that are pure data and prompts.
Swapping the kind of interview means writing a new pack, never touching
engine code.

Five packs ship:

| pack | what it drills | shape |
|---|---|---|
| `python-idiom-fluency` | writing idiomatic Python under a clock | 45 min, one problem, silent observer, 4-interjection budget |
| `python-rehab` | de-rusting via volume | 30 min of 2–4 minute rewrite micro-drills, auto-advance on a clean run |
| `leetcode-drill` | classic algorithm rounds | 40 min, seeded stubs, quiet until your first passing run — then complexity probing |
| `system-design-doc` | design interviews, no whiteboard needed | you write `design.md` under observation; staged probing by clock marks |
| `verbal-drill` | leading with your conclusion | no workspace at all; relentless probing; one-line rubric |

Grading is deliberately **not** the engine's job. The live model only needs
short, in-persona phrasing (a small local model is plenty); the append-only
transcript is the real product — `regrade.py` rebuilds the report offline,
`python -m harness analyze` ships it to a strong model for deep review.

## Quick start

Requires Python **3.11+** (uses `tomllib`).

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# local model (default config; no API key, ever):
ollama pull qwen3:4b-instruct-2507-q4_K_M

# the browser front end — recommended:
python -m harness web            # then open http://127.0.0.1:8765
# or in docker:
docker compose up --build

# the terminal front end works too:
python -m harness run --pack packs/python-idiom-fluency

# no model running? sessions still work, phrased from pack fallback lines:
python -m harness run --pack verbal-drill --provider canned
```

### The web pane

`python -m harness web` serves a session picker plus, per session: the
task, the chat, a live clock, and (for workspace packs) a built-in
editor with autosave into the observed workspace, a run button, and
typing-cadence capture (edit pulses — timing only, never content).
Reconnects are invisible: the WebSocket streams transcript rows and a
returning client replays from the last row it saw, which is exactly
what makes it work well from an iPad (see `docs/DEPLOY.md` for the
Tailscale setup, and the eventual friends-with-auth setup via
Cloudflare Access). A **voice** toggle speaks interviewer lines aloud
(browser-native TTS) and a mic button dictates where the browser
supports it; the iPad keyboard's own dictation also just works.

The terminal pane (`run`) observes any external editor instead, and
`tools/irun` reports runs from a second terminal.

### After a session

```sh
python -m harness check sessions/<id>     # run the pack's hidden checks
                                          # against what you actually shipped
python regrade.py sessions/<id>           # rebuild the report offline, byte-identical
python -m harness analyze sessions/<id>   # deep review via the [analyze] model
```

`check` also runs automatically at session end for packs that opt in.
`analyze` uses the pack's own rubric prompt and a **separate** model
config from the live interviewer — small-and-local for the session,
strongest-you-have for the review. With no key it writes the complete
request to `analysis-request.md` for manual pasting instead of failing.

Watch-list trips accumulate in `sessions/recurrence-python-idiom-fluency.tsv`
across sessions; the fluency and rehab packs order their tasks by it
(`order = "recurrence"`), so your repeat offenders keep coming back
until they stop tripping.

## Models

Default: **`qwen3:4b-instruct-2507-q4_K_M`** via Ollama (Apache 2.0,
~2.5 GB, CPU-friendly) — best persona-adherence per gigabyte at this
size, and the interviewer only ever needs 1–2 terse sentences. Works
with anything OpenAI-compatible (Ollama, LM Studio, llama.cpp, vLLM) or
hosted (OpenRouter, Groq — around a penny per session; Anthropic via
the thin `anthropic` adapter). Presets in `config.toml` and
`docs/DEPLOY.md`. Swapping is a config edit; no key is ever required
for the local path.

```toml
[model]    # the live interviewer: small and fast is correct
provider = "openai-compat"
base_url = "http://127.0.0.1:11434/v1"
name = "qwen3:4b-instruct-2507-q4_K_M"

[analyze]  # the post-session deep read: strongest model you have
provider = "anthropic"
name = "claude-sonnet-5"
```

## The seam

**Engine owns** (`harness/`): session lifecycle + clock; workspace
observation (debounced file watch, run capture, seeding/pad-write
mechanics); a fixed event taxonomy; the append-only JSONL transcript;
the chat panes (terminal and web); the model adapters; the
deterministic interjection gate; report generation from pack templates.

**Pack owns** (data + prompts only): persona and affect rules; tasks
(statements, planted ambiguities, hidden checks, seed files, probe
material); wake rules, thresholds, silence budget; hint ladder;
visibility; report rubric, watch-lists, and analyze prompt.

Enforced, not aspirational: `tests/test_seam.py` greps every engine
source file for `python`, `idiom`, `code`, `leetcode` as
case-insensitive substrings (the engine says `exit_status`, uses
`proc.poll()` and `bytes(s, "utf-8")` to stay clean). Packs may say
anything — they're data.

## How the interjection gate works

**Rules decide whether to act; the model only phrases.** On every wake
the gate — deterministic engine code — evaluates the pack's declarative
rules against a closed fact set. Every evaluation, including silent
ones, is logged with the rule that fired (or none), each rule's
blocking reason, and the facts. Wakes are events only, never interval
sampling: debounced saves, runs with output, idle-deadline expiry,
chat messages, edit pulses (activity only), pack clock marks, task
presentation, session start.

```toml
[[rules]]
id = "stuck-too-long"
on = ["idle"]                          # which wakes it hears
when = "task_open and idle_s >= 150"   # tiny safe expression language
action = "speak"                       # or "advance", or "write"
hint = "escalate"                      # ladder position; never skips
cooldown_s = 240
max_fires = 0
counts_toward_budget = true            # draws down interjection_budget
priority = 50
prompt = "Guidance injected into this one call."
```

Three actions:
- **speak** — persona re-injected on every call, hard sentence cap
  (default 2), fenced-block/`<think>` stripping (including truncated
  ones), pack banned-phrase filter, deterministic pack fallback lines
  when nothing survives. A weak or absent model cannot produce a
  chatty interviewer.
- **advance** — next task (also `/next`).
- **write** — the interviewer writes into the workspace, the way real
  pad interviews go: pack-sourced templates (`{task.probe}` pastes a
  task's probe test) or model-phrased comment lines, every line
  prefixed with the pack's `pad_marker`, append-only — candidate text
  is never modified, watch-lists exclude marker lines, and reports
  attribute every write. Task `seed` files (stubs, doc skeletons) use
  the same machinery at presentation.

Facts available in `when`: clock (`elapsed_s`, `remaining_min`, …),
activity (`idle_s`, `saves_total`, `runs_total`, `failed_run_streak`,
`last_run_ok`, `pulses_total`, `since_last_*_s`, …), conversation
(`user_messages_total`, `unprompted_speaks`, `budget_left`), task state
(`task_open`, `task_elapsed_s`, `task_user_messages`, `hint_level`,
`tasks_left`), `pad_writes_total`, wake info (`kind`, `mark`),
`has_workspace`. Unknown names fail at pack load, not mid-session.

## Transcript format

`sessions/<id>/transcript.jsonl`, append-only, one JSON object per
line, each with `t` (seconds offset) and `ts` (wall clock). The closed
taxonomy:

| kind | payload |
|---|---|
| `session_start` | pack, minutes, task order |
| `pack_snapshot` | the complete raw pack (makes regrade self-contained) |
| `task_presented` | task id, title, statement |
| `file_saved` | path, full content, sha256 (debounced, deduped) |
| `run_executed` | cmd, out, err, exit_status, duration_ms, source (+ backend on container runs) |
| `edit_pulse` | path, delta — debounced typing cadence, no content |
| `pad_write` | path, text, mode, source, rule — interviewer-authored |
| `user_message` / `interviewer_message` | text (+ rule, hint_level, source, counted) |
| `gate_decision` | wake, fired rule or null, per-rule reasons, facts |
| `idle` / `clock_mark` | idle_s / mark id |
| `note` | engine notices (e.g. model endpoint failures) |
| `session_end` | reason |

The web pane streams these same rows as its wire format — there is no
second schema, which is also what makes reconnect/replay and future
consumers (dashboards, spectators) cheap.

## Writing a pack

A pack is a directory: `pack.toml` + `tasks/` (`.toml` or `.json`;
fields: `id`, `title`, `statement`, interviewer-only `notes`,
report-only `appendix`, `tags`, `focus` watch-ids, runnable `check`,
`seed` files, plus any extra string fields your write rules template
in). Start from `packs/verbal-drill` (minimal) and go up. Report
section types: `header`, `timeline` (milestone expressions),
`watchlist` (regex + suggestion + `exclude_lines`, optional recurrence
log), `gaps`, `spoken_vs_typed`, `openers`, `messages`, `appendix`.
A pack with `workspace = false` runs purely on transcript + clock.

## Deployment

`docs/DEPLOY.md` covers the staged path: laptop → home machine +
Tailscale (iPad from anywhere, zero auth code) → always-on VPS →
friends via Cloudflare Tunnel + Access, with the `[run]`
`backend = "container"` switch that sandboxes every run in
no-network docker before anyone else can reach the pane.

## Tests

```sh
python -m pytest tests/
```

73 tests: the seam grep, expression-language safety, gate determinism,
pack validation, watch-list matching + attribution exclusion, report
determinism, runner backends, self-checks, recurrence ordering,
analyze compaction, pad-write/seeding echo suppression, and two
end-to-end compressed sessions (terminal and web — real scheduler,
watcher, spool, WebSocket resume; canned adapter; no network) whose
offline regrade must reproduce the live report byte-for-byte.

## Known gaps

- **Typing is invisible between saves in the terminal flow.** The web
  editor closes this with edit pulses; with an external editor,
  autosave narrows it.
- **The analyze/check loop stops at files.** `analysis.md` and
  `checks.md` land in the session dir; nothing pushes them anywhere.
- **Web pane trusts its network.** No auth by design — bind to
  localhost/tailnet, or front with Cloudflare Access (docs). Sessions
  aren't per-user namespaced yet.
- **Container runs can orphan on hard timeout** (docker client is
  killed; a wedged container may need `docker container prune`).
- **Dictation support varies by browser**; the voice toggle (TTS) is
  broadly supported, `SpeechRecognition` less so — the iPad keyboard
  mic is the reliable path there.
- **Watch-lists are regex heuristics**, evidence not judgment; deep
  judgment belongs to `analyze`.
- **Windows untested** (process groups, inotify); Linux/macOS are the
  supported paths. On platforms with no native file-event API,
  watchdog silently falls back to polling.
- **No pause/resume of the clock** — a session is one continuous take.
