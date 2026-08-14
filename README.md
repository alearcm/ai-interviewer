# ai-interviewer

A generic engine for timed, observed, adversarial practice sessions with an
AI interviewer — plus **scenario packs** that are pure data and prompts.
Swapping the kind of interview means writing a new pack, never touching
engine code.

Two packs ship as proof the seam is real:

- **`packs/python-idiom-fluency`** — 45-minute Python fluency drills under a
  neutral, terse observer. 12 easy-to-medium problems skewed to counting /
  grouping / parsing / dict-and-set work, each with planted ambiguities the
  interviewer resolves only if asked, hidden checks, and a target idiom.
- **`packs/verbal-drill`** — no workspace at all: transcript + clock only.
  A relentless prober with an effectively unlimited interjection budget.
  Its whole report rubric is one question: did you lead with your
  conclusion, or bury it?

Grading is deliberately **not** the engine's job. The live model only needs
short, in-persona phrasing (it runs fine on a small local model); the
append-only transcript is designed to be shipped elsewhere for deep
analysis, and `regrade.py` reproduces the report offline at any time.

## Quick start

Requires Python **3.11+** (uses `tomllib`).

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# local model (recommended path — no API key, ever):
ollama pull qwen3:4b-instruct-2507-q4_K_M

# the real thing: 45 min of Python fluency under observation
python -m harness run --pack packs/python-idiom-fluency

# the seam proof: verbal drill, no workspace
python -m harness run --pack packs/verbal-drill

# no model running? the session still works, phrased from the pack's
# fallback lines:
python -m harness run --pack packs/python-idiom-fluency --provider canned
```

During a session the harness prints the workspace path. Open that directory
in your editor; every save is observed (debounced, snapshotted into the
transcript). Talk to the interviewer by typing in the chat pane.

Chat commands: `/run CMD` (run in the workspace, output captured),
`/next` (next task), `/time`, `/end`, `/help`. Anything else is a message
to the interviewer.

To run your program from a **separate terminal** instead of `/run`:

```sh
cd <workspace-dir>
/path/to/repo/tools/irun python3 solution.py
```

`irun` captures the command, output and exit status and reports them to the
live session through a spool directory picked up by the file watcher
(event-driven, nothing polls). Note: `irun` shows output after the command
finishes.

Useful flags: `--minutes 20` (shorter session), `--task word-tally`
(specific problem), `--workspace DIR` (observe an existing directory),
`--provider canned` (no model), `--config FILE`.

### Regrade — offline, always

```sh
python regrade.py sessions/<id>            # writes report.regraded.md
python regrade.py sessions/<id> --stdout
```

Zero live calls. The transcript embeds the full pack (rules, rubric,
regexes) at session start, so the regrade is self-contained even if the
pack has changed since — identical transcript, identical report, byte for
byte.

## Recommended local model

**`qwen3:4b-instruct-2507-q4_K_M`** (Apache 2.0, ~2.5 GB, runs on CPU) —
a purpose-built non-thinking instruct model with the best persona-adherence
per gigabyte at this size, and the interviewer only ever needs 1–2 terse
sentences. Fallback: `llama3.2:3b-instruct-q4_K_M`. (The engine also strips
`<think>…</think>` defensively, so thinking-style models work too.)

Works with anything OpenAI-compatible — Ollama, LM Studio, llama.cpp,
vLLM — via `config.toml`:

```toml
[model]
provider = "openai-compat"     # or "anthropic", or "canned"
base_url = "http://127.0.0.1:11434/v1"
name = "qwen3:4b-instruct-2507-q4_K_M"
api_key_env = ""               # only if your endpoint wants one
```

Swapping model or provider is a config edit. No key is ever required for
the default path; the `anthropic` provider is a thin adapter alongside
(reads `ANTHROPIC_API_KEY`).

## The seam

**Engine owns** (in `harness/`): session lifecycle + clock; workspace
observation (debounced file watch, run capture); a fixed event taxonomy;
the append-only JSONL transcript; the chat pane; the model adapter; the
deterministic interjection gate; report generation from the pack's
template.

**Pack owns** (data + prompts only): persona and affect rules; tasks; wake
rules, thresholds, silence budget; hint ladder; what is "visible" to the
interviewer; report rubric and watch-lists.

Enforced, not aspirational: `tests/test_seam.py` greps every engine file
for `python`, `idiom`, `code`, `leetcode` (case-insensitive substrings —
so even `exit_code` or `.encode()` would fail the build). The engine
speaks only in generic vocabulary: tasks, runs, saves, marks, rules.

## How the interjection gate works

**Rules decide whether to speak; the model only phrases it.** On every wake
the gate — deterministic engine code — evaluates the pack's declarative
rules against a closed set of facts. Only after a rule fires is the model
called, and it is told which hint level to produce. Every evaluation,
including the silent ones, is logged to the transcript with the rule that
fired (or none), each rule's blocking reason, and the facts.

Wakes are **events only** (never interval sampling): a debounced file
save, a run with its output, an idle-threshold expiry (a deadline timer,
re-armed by activity), a chat message, pack-defined clock marks, task
presentation, session start.

A rule looks like:

```toml
[[rules]]
id = "stuck-too-long"
on = ["idle"]                      # which wakes it listens to
when = "task_open and idle_s >= 150"   # tiny safe expression language
action = "speak"                   # or "advance" (next task)
hint = "escalate"                  # "none" | "escalate" | a level number
cooldown_s = 240
max_fires = 0                      # 0 = unlimited
counts_toward_budget = true        # draws down interjection_budget
priority = 50
prompt = "Extra guidance injected into this call only."
```

Facts available in `when`: clock (`elapsed_s`, `remaining_min`, …),
activity (`idle_s`, `saves_total`, `runs_total`, `failed_run_streak`,
`last_run_ok`, `since_last_*_s`, …), conversation (`user_messages_total`,
`speaks_total`, `unprompted_speaks`, `budget_left`), task state
(`task_open`, `task_elapsed_s`, `task_user_messages`, `hint_level`,
`tasks_left`), wake info (`kind`, `mark`), `has_workspace`. Unknown names
fail at pack load, not mid-session.

Hint levels can never skip: whatever a rule requests is clamped to at most
one step above the highest level already used on the current task.

**Persona drift is designed against**: the system prompt (persona + hint
instruction + brevity cap) is rebuilt from pack data on *every* call —
nothing conversational accumulates outside the transcript — and replies
are clamped after the fact: fenced blocks stripped, hard sentence cap
(default 2), pack-listed banned phrases dropped sentence-wise. If nothing
survives, a deterministic fallback line from the pack is used, so a weak,
drifting, or absent model can never produce a chatty interviewer.

## Transcript format

`sessions/<id>/transcript.jsonl`, append-only, one JSON object per line,
each with `t` (seconds offset) and `ts` (wall clock). The closed event
taxonomy:

| kind | payload |
|---|---|
| `session_start` | pack, minutes, task order |
| `pack_snapshot` | the complete raw pack (makes regrade self-contained) |
| `task_presented` | task id, title, statement |
| `file_saved` | path, full content, sha256 (debounced, deduped) |
| `run_executed` | cmd, out, err, exit_status, duration_ms, source |
| `user_message` / `interviewer_message` | text (+ rule, hint_level, source, counted) |
| `gate_decision` | wake, fired rule or null, per-rule reasons, facts |
| `idle` / `clock_mark` | idle_s / mark id |
| `note` | engine notices (e.g. model endpoint failures) |
| `session_end` | reason |

## Writing a pack

A pack is a directory: `pack.toml` + a `tasks/` directory of `.toml` or
`.json` files (`id`, `title`, `statement`, plus free-form `notes` =
interviewer-only knowledge, `appendix` = report-only material, `tags`).
Start from `packs/verbal-drill` (minimal) or `packs/python-idiom-fluency`
(everything). Sections available to the report template: `header`,
`timeline` (milestone expressions), `watchlist` (regex + suggestion,
deduped, optionally appended to a cross-session recurrence log), `gaps`
(stalls with what was on screen), `spoken_vs_typed` (narration vs diffs),
`openers` (first sentence of each answer, flag patterns), `messages`,
`appendix`. A pack with `workspace = false` runs purely on transcript +
clock — that is Pack B, and it required zero engine special-casing.

The cross-session watch-list log for Pack A accumulates at
`sessions/recurrence-python-idiom-fluency.tsv` (one line per tripped
pattern per session), so repeat offenses stay visible.

## Tests

```sh
python -m pytest tests/
```

44 tests: the seam grep, expression-language safety, gate determinism
(budget/cooldown/priority/no-skip escalation), pack validation, watch-list
matching, report determinism, and an end-to-end compressed session (real
scheduler, real watcher, real transcript, canned adapter — no network)
whose regrade must reproduce the live report byte-for-byte.

## Known gaps

- **Typing is invisible between saves.** Activity = saves, runs, messages.
  Type for three minutes without saving and the engine reads it as idle
  (the interviewer may prompt you). Deliberate scope, and honestly
  interview-realistic — but know it's there. Editor autosave narrows it.
- **The chat pane is line-oriented**, not a TUI. Without `prompt_toolkit`,
  interviewer lines can interleave with your half-typed input.
- **`tools/irun` prints output only after the command finishes** (POSIX-sh
  simplicity); `/run` in the pane behaves the same. Fine for the
  sub-second runs these drills produce.
- **Watch-list patterns are line-regex heuristics**, not static analysis:
  raw evidence with occasional false positives, by design. Deep judgment
  belongs to whatever you feed the transcript to.
- **Timed runs on Windows are untested**; the run captor uses process
  groups (POSIX). Linux and macOS are the supported paths.
- **On platforms with no native file-event API**, watchdog silently
  falls back to its polling observer; the engine doesn't assert the
  resolved observer class. On Linux (inotify) and macOS (FSEvents) it
  is genuinely event-driven.
- **No streaming model output** — replies land whole. They're at most two
  sentences, so latency is the model's generation time.
- The Anthropic adapter is intentionally thin (no retries/backoff).
