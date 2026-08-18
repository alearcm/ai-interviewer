"""Pack loading and validation.

A pack is a directory containing pack.toml plus a tasks directory of
.toml/.json files. Everything in it is data and prompt text: persona,
wake rules, thresholds, hint ladder, visibility, report template.

The full raw pack is embedded into the transcript at session start
(the pack_snapshot event), and `Pack.from_snapshot` rebuilds an
identical compiled pack from that row alone — this is what makes the
offline regrade self-contained.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import tomllib
from typing import Any, Dict, List, Optional

from . import events
from .exprs import Expr, ExprError
from .facts import EVENT_FACT_NAMES, RULE_FACT_NAMES


class PackError(ValueError):
    pass


SECTION_TYPES = {
    "header",
    "timeline",
    "watchlist",
    "gaps",
    "spoken_vs_typed",
    "openers",
    "messages",
    "appendix",
}

_RULE_ACTIONS = {"speak", "advance"}


def _table(data: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise PackError("[%s] must be a table" % key)
    return dict(value)


class Rule:
    def __init__(self, raw: Dict[str, Any], index: int, ladder_len: int) -> None:
        self.raw = raw
        self.index = index
        self.id = str(raw.get("id") or "")
        if not self.id:
            raise PackError("rule #%d has no id" % index)
        on = raw.get("on")
        if not isinstance(on, list) or not on:
            raise PackError("rule %r: 'on' must be a non-empty list" % self.id)
        for kind in on:
            if kind != "any" and kind not in events.WAKE_KINDS:
                raise PackError("rule %r: unknown wake kind %r" % (self.id, kind))
        self.on = list(on)
        try:
            self.when = Expr(str(raw.get("when", "true")), RULE_FACT_NAMES)
        except ExprError as exc:
            raise PackError("rule %r: %s" % (self.id, exc)) from None
        self.action = str(raw.get("action", "speak"))
        if self.action not in _RULE_ACTIONS:
            raise PackError("rule %r: unknown action %r" % (self.id, self.action))
        hint = raw.get("hint", "none")
        if isinstance(hint, bool) or (
            hint not in ("none", "escalate") and not isinstance(hint, int)
        ):
            raise PackError("rule %r: hint must be 'none', 'escalate' or a level" % self.id)
        if isinstance(hint, int) and hint < 1:
            raise PackError("rule %r: hint level must be >= 1 (use 'none' for no hint)" % self.id)
        if hint != "none" and self.action == "speak" and ladder_len == 0 and hint != 0:
            raise PackError("rule %r: hint %r but the pack has no hint ladder" % (self.id, hint))
        self.hint = hint
        self.priority = int(raw.get("priority", 0))
        self.cooldown_s = float(raw.get("cooldown_s", 0))
        self.max_fires = int(raw.get("max_fires", 0))
        default_counts = "user_message" not in self.on
        self.counts_toward_budget = bool(raw.get("counts_toward_budget", default_counts))
        self.prompt = str(raw.get("prompt", ""))


class Pattern:
    """One watch-list entry: a regular expression plus the suggested
    replacement shown next to any line it matches."""

    def __init__(self, raw: Dict[str, Any], section_i: int) -> None:
        self.id = str(raw.get("id") or "")
        if not self.id:
            raise PackError("watchlist section #%d: pattern with no id" % section_i)
        text = raw.get("pattern")
        if not text:
            raise PackError("watchlist pattern %r: missing 'pattern'" % self.id)
        flags = re.MULTILINE
        if raw.get("dotall"):
            flags |= re.DOTALL
        try:
            self.rx = re.compile(str(text), flags)
        except re.error as exc:
            raise PackError("watchlist pattern %r: %s" % (self.id, exc)) from None
        self.suggest = str(raw.get("suggest", ""))
        self.files = str(raw.get("files", "*"))

    def matches_file(self, path: str) -> bool:
        return fnmatch.fnmatch(path, self.files) or fnmatch.fnmatch(
            os.path.basename(path), self.files
        )


class Pack:
    def __init__(self, raw: Dict[str, Any]) -> None:
        if "pack" not in raw:
            raise PackError("pack data has no [pack] table")
        self.raw = raw
        data = raw["pack"]

        meta = _table(data, "pack")
        self.name = str(meta.get("name") or "")
        if not self.name:
            raise PackError("[pack] name is required")
        self.title = str(meta.get("title", self.name))

        session = _table(data, "session")
        self.minutes = float(session.get("minutes", 45.0))
        self.workspace = bool(session.get("workspace", True))
        self.idle_threshold_s = float(session.get("idle_threshold_s", 120.0))
        self.debounce_ms = int(session.get("debounce_ms", 1200))
        self.snapshot_max_bytes = int(session.get("snapshot_max_bytes", 200_000))
        # The file a front-end editor should open by default ("" = none).
        self.primary_file = str(session.get("primary_file", ""))
        self.ignore_globs = list(session.get("ignore", ["*.pyc", "*.swp", "*~", "*.tmp"]))
        self.clock_marks: List[Dict[str, Any]] = []
        for i, mark in enumerate(session.get("clock_marks", [])):
            if "id" not in mark or "at_min" not in mark:
                raise PackError("clock_marks[%d] needs 'id' and 'at_min'" % i)
            self.clock_marks.append({"id": str(mark["id"]), "at_min": float(mark["at_min"])})

        interviewer = _table(data, "interviewer")
        self.persona = str(interviewer.get("persona", "")).strip()
        if not self.persona:
            raise PackError("[interviewer] persona is required")
        self.max_sentences = int(interviewer.get("max_sentences", 2))
        self.max_chars = int(interviewer.get("max_chars", 320))
        self.banned_phrases = [str(p) for p in interviewer.get("banned_phrases", [])]
        self.fallback_lines = [str(p) for p in interviewer.get("fallback_lines", [])] or ["Okay."]
        self.interjection_budget = int(interviewer.get("interjection_budget", 4))
        self.opening_line = str(interviewer.get("opening_line", ""))
        self.strip_fenced_blocks = bool(interviewer.get("strip_fenced_blocks", True))

        visibility = _table(data, "visibility")
        self.show_latest_snapshot = bool(visibility.get("show_latest_snapshot", self.workspace))
        self.recent_files = int(visibility.get("recent_files", 2))
        self.snapshot_max_chars = int(visibility.get("snapshot_max_chars", 6000))
        self.recent_runs = int(visibility.get("recent_runs", 2))
        self.run_output_max_chars = int(visibility.get("run_output_max_chars", 1200))
        self.recent_messages = int(visibility.get("recent_messages", 10))
        self.include_task_notes = bool(visibility.get("include_task_notes", True))
        self.include_clock = bool(visibility.get("include_clock", True))

        hints = _table(data, "hints")
        ladder_raw = hints.get("ladder", [])
        self.ladder: List[str] = []
        for i, step in enumerate(ladder_raw):
            if isinstance(step, str):
                self.ladder.append(step)
            elif isinstance(step, dict) and "instruction" in step:
                self.ladder.append(str(step["instruction"]))
            else:
                raise PackError("hints.ladder[%d] must be a string or have 'instruction'" % i)

        rules_raw = data.get("rules", [])
        self.rules = [Rule(r, i, len(self.ladder)) for i, r in enumerate(rules_raw)]
        seen = set()
        for rule in self.rules:
            if rule.id in seen:
                raise PackError("duplicate rule id %r" % rule.id)
            seen.add(rule.id)

        tasks_cfg = _table(data, "tasks")
        self.tasks_order = str(tasks_cfg.get("order", "sequential"))
        if self.tasks_order not in ("sequential", "shuffle", "recurrence"):
            raise PackError("[tasks] order must be 'sequential', 'shuffle' or 'recurrence'")
        self.tasks_dir = str(tasks_cfg.get("dir", "tasks"))

        self.tasks: List[Dict[str, Any]] = []
        for i, task in enumerate(raw.get("tasks", [])):
            for field in ("id", "title", "statement"):
                if not task.get(field):
                    raise PackError("task #%d is missing %r" % (i, field))
            self.tasks.append(
                {
                    "id": str(task["id"]),
                    "title": str(task["title"]),
                    "statement": str(task["statement"]).strip(),
                    "notes": str(task.get("notes", "")).strip(),
                    "appendix": str(task.get("appendix", "")).strip(),
                    "tags": [str(t) for t in task.get("tags", [])],
                    # watch-list ids this task exercises; drives
                    # order = "recurrence" (repeat offenders come back)
                    "focus": [str(f) for f in task.get("focus", [])],
                    # opaque runnable text for the post-session
                    # self-check; "" = nothing to run for this task
                    "check": str(task.get("check", "")),
                }
            )

        checks = _table(data, "checks")
        self.checks_file = str(checks.get("file", ""))
        self.checks_cmd = str(checks.get("cmd", ""))
        self.checks_auto = bool(checks.get("auto", False))
        if self.checks_cmd and "{file}" not in self.checks_cmd:
            raise PackError("[checks] cmd must contain the {file} placeholder")

        report = _table(data, "report")
        self.report_title = str(report.get("title", self.title))
        self.recurrence_log = str(report.get("recurrence_log", ""))
        self.analyze_prompt = str(report.get("analyze_prompt", "")).strip()
        self.sections: List[Dict[str, Any]] = []
        for i, section in enumerate(report.get("sections", [])):
            self.sections.append(self._compile_section(section, i))

    def _compile_section(self, raw: Dict[str, Any], i: int) -> Dict[str, Any]:
        kind = raw.get("type")
        if kind not in SECTION_TYPES:
            raise PackError("report section #%d: unknown type %r" % (i, kind))
        out: Dict[str, Any] = {"type": kind, "title": str(raw.get("title", ""))}
        if kind == "header":
            out["text"] = str(raw.get("text", ""))
        elif kind == "timeline":
            milestones = []
            for m in raw.get("milestones", []):
                if "label" not in m or "when" not in m:
                    raise PackError("timeline milestone needs 'label' and 'when'")
                try:
                    expr = Expr(str(m["when"]), EVENT_FACT_NAMES)
                except ExprError as exc:
                    raise PackError("timeline milestone %r: %s" % (m["label"], exc)) from None
                milestones.append({"label": str(m["label"]), "when": expr})
            out["milestones"] = milestones
            out["include_speaks"] = bool(raw.get("include_speaks", True))
            out["include_tasks"] = bool(raw.get("include_tasks", True))
            out["include_marks"] = bool(raw.get("include_marks", False))
        elif kind == "watchlist":
            out["scope"] = str(raw.get("scope", "all"))
            if out["scope"] not in ("all", "final"):
                raise PackError("watchlist scope must be 'all' or 'final'")
            out["patterns"] = [Pattern(p, i) for p in raw.get("patterns", [])]
        elif kind == "gaps":
            out["threshold_s"] = float(raw.get("threshold_s", self.idle_threshold_s))
            out["screen_lines"] = int(raw.get("screen_lines", 12))
        elif kind == "spoken_vs_typed":
            out["diff_lines"] = int(raw.get("diff_lines", 8))
        elif kind == "openers":
            flags = re.IGNORECASE if raw.get("ignore_case", True) else 0
            compiled = []
            for p in raw.get("flag_patterns", []):
                try:
                    compiled.append(re.compile(str(p), flags))
                except re.error as exc:
                    raise PackError("openers flag pattern %r: %s" % (p, exc)) from None
            out["flag_patterns"] = compiled
        elif kind == "appendix":
            out["fields"] = [str(f) for f in raw.get("fields", ["notes", "appendix"])]
        return out

    def task_by_id(self, task_id: str) -> Optional[Dict[str, Any]]:
        for task in self.tasks:
            if task["id"] == task_id:
                return task
        return None

    def snapshot(self) -> Dict[str, Any]:
        """The raw data embedded in the transcript."""
        return self.raw

    @classmethod
    def from_snapshot(cls, data: Dict[str, Any]) -> "Pack":
        return cls(data)


def _load_task_file(path: str) -> Dict[str, Any]:
    if path.lower().endswith(".json"):
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    if "task" in data:
        data = data["task"]
    if not isinstance(data, dict):
        raise PackError("%s: task file must hold a table" % path)
    return data


def load_pack(path: str) -> Pack:
    """Load a pack from a directory (or a pack name under ./packs)."""
    if not os.path.isdir(path):
        candidate = os.path.join("packs", path)
        if os.path.isdir(candidate):
            path = candidate
        else:
            raise PackError("no pack directory at %r" % path)
    manifest = os.path.join(path, "pack.toml")
    if not os.path.isfile(manifest):
        raise PackError("%s has no pack.toml" % path)
    with open(manifest, "rb") as fh:
        data = tomllib.load(fh)

    tasks: List[Dict[str, Any]] = []
    tasks_dir = os.path.join(path, str(_table(data, "tasks").get("dir", "tasks")))
    if os.path.isdir(tasks_dir):
        for name in sorted(os.listdir(tasks_dir)):
            if name.lower().endswith((".toml", ".json")):
                tasks.append(_load_task_file(os.path.join(tasks_dir, name)))

    return Pack({"pack": data, "tasks": tasks, "dir": os.path.abspath(path)})
