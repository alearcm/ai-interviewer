"""Report generation from a pack's template.

Input: the transcript rows and nothing else. The pack itself is
rebuilt from the pack_snapshot row, so the same function serves the
live session and the offline regrade, and both produce identical
bytes for identical transcripts. No wall clock is consulted here —
all times shown are the offsets recorded in the rows.

Section renderers are generic mechanisms (timelines, pattern watch
lists, gaps, said-vs-typed, opening sentences); everything they look
for — expressions, regexes, thresholds, labels — comes from the pack.
"""

from __future__ import annotations

import difflib
from typing import Any, Dict, List, Optional, Tuple

from . import events
from .chat import fmt_t
from .facts import event_facts
from .pack import Pack
from .phrasing import split_sentences

_CONVERSATIONAL = (events.INTERVIEWER_MESSAGE, events.TASK_PRESENTED)


def _find_pack(rows: List[Dict[str, Any]]) -> Pack:
    for row in rows:
        if row["kind"] == events.PACK_SNAPSHOT:
            return Pack.from_snapshot(row["data"])
    raise ValueError("transcript has no pack_snapshot row; cannot rebuild the pack")


def _meta_lines(rows: List[Dict[str, Any]], pack: Pack) -> List[str]:
    start = next((r for r in rows if r["kind"] == events.SESSION_START), None)
    end = next((r for r in rows if r["kind"] == events.SESSION_END), None)
    tasks = [r for r in rows if r["kind"] == events.TASK_PRESENTED]
    speaks = [r for r in rows if r["kind"] == events.INTERVIEWER_MESSAGE]
    unprompted = [r for r in speaks if r.get("counted")]
    lines = []
    if start:
        lines.append("- pack: %s" % start.get("pack", pack.name))
        lines.append("- started: %s" % start.get("ts", "?"))
    if end:
        lines.append("- duration: %s (ended: %s)" % (fmt_t(end["t"]), end.get("reason", "?")))
    lines.append("- tasks presented: %s" % (", ".join(r["task_id"] for r in tasks) or "none"))
    lines.append(
        "- interviewer messages: %d (%d unprompted interjections)"
        % (len(speaks), len(unprompted))
    )
    return lines


def recurrence_counts(log_path: str) -> Dict[str, int]:
    """Tally the cross-session recurrence log by watch-pattern id.
    Missing or unreadable log = empty tally."""
    counts: Dict[str, int] = {}
    try:
        with open(log_path, "r", encoding="utf-8") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 2 and parts[1]:
                    counts[parts[1]] = counts.get(parts[1], 0) + 1
    except OSError:
        pass
    return counts


# -- watchlist ----------------------------------------------------------


def watchlist_hits(rows: List[Dict[str, Any]], pack: Pack) -> List[Dict[str, Any]]:
    """Every watch-list pattern match across saved snapshots, deduped
    by (path, pattern, matched line)."""
    hits: List[Dict[str, Any]] = []
    seen: set = set()
    patterns = []
    for section in pack.sections:
        if section["type"] == "watchlist":
            scope = section["scope"]
            for pattern in section["patterns"]:
                patterns.append((pattern, scope))
    if not patterns:
        return hits

    saved = [r for r in rows if r["kind"] == events.FILE_SAVED]
    finals = {}
    for row in saved:
        finals[row["path"]] = row

    # scope "task_final": the last save per path within each task's
    # slice — judges what was left behind at the end of every task,
    # ignoring intermediate states (and seeded starting points still
    # being rewritten)
    task_finals = []
    boundaries = [i for i, r in enumerate(rows) if r["kind"] == events.TASK_PRESENTED]
    for n, start in enumerate(boundaries):
        end = boundaries[n + 1] if n + 1 < len(boundaries) else len(rows)
        per_path = {}
        for row in rows[start:end]:
            if row["kind"] == events.FILE_SAVED:
                per_path[row["path"]] = row
        task_finals.extend(per_path.values())

    excludes = [s.get("exclude_lines") for s in pack.sections if s["type"] == "watchlist"]
    excludes = [e for e in excludes if e is not None]

    for pattern, scope in patterns:
        if scope == "final":
            source_rows = list(finals.values())
        elif scope == "task_final":
            source_rows = task_finals
        else:
            source_rows = saved
        for row in source_rows:
            path = row["path"]
            if not pattern.matches_file(path):
                continue
            content = row["content"]
            for match in pattern.rx.finditer(content):
                # evidence and exclusion both use the CONTAINING line,
                # not the matched fragment
                line_start = content.rfind("\n", 0, match.start()) + 1
                line_end = content.find("\n", match.start())
                if line_end == -1:
                    line_end = len(content)
                line = content[line_start:line_end].strip()
                if any(e.search(line) for e in excludes):
                    continue  # interviewer-authored line, not the candidate's
                key = (path, pattern.id, line)
                if key in seen:
                    continue
                seen.add(key)
                line_no = content[: match.start()].count("\n") + 1
                hits.append(
                    {
                        "t": row["t"],
                        "id": pattern.id,
                        "path": path,
                        "line_no": line_no,
                        "line": line,
                        "suggest": pattern.suggest,
                    }
                )
    hits.sort(key=lambda h: (h["t"], h["path"], h["line_no"]))
    return hits


# -- section renderers --------------------------------------------------


def _render_header(section: Dict[str, Any], rows: List[Dict[str, Any]], pack: Pack) -> str:
    return section.get("text", "")


def _render_timeline(section: Dict[str, Any], rows: List[Dict[str, Any]], pack: Pack) -> str:
    entries: List[Tuple[float, str]] = []
    pending = list(section["milestones"])
    for row in rows:
        if not pending:
            break
        facts = event_facts(row)
        still = []
        for milestone in pending:
            if milestone["when"].as_bool(facts):
                entries.append((row["t"], milestone["label"]))
            else:
                still.append(milestone)
        pending = still
    for milestone in pending:
        entries.append((float("inf"), "%s: never happened" % milestone["label"]))

    if section["include_tasks"]:
        for row in rows:
            if row["kind"] == events.TASK_PRESENTED:
                entries.append((row["t"], "task presented: %s" % row["task_id"]))
    if section["include_speaks"]:
        for row in rows:
            if row["kind"] == events.INTERVIEWER_MESSAGE:
                what = "interjection" if row.get("counted") else "reply"
                level = row.get("hint_level", 0)
                label = "interviewer %s (%s%s): %s" % (
                    what,
                    row.get("rule") or row.get("source", "?"),
                    (", hint L%d" % level) if level else "",
                    row.get("text", ""),
                )
                entries.append((row["t"], label))
    if section["include_marks"]:
        for row in rows:
            if row["kind"] == events.CLOCK_MARK:
                entries.append((row["t"], "clock mark: %s" % row.get("mark", "")))
    if section["include_writes"]:
        for row in rows:
            if row["kind"] == events.PAD_WRITE and row.get("rule") != "seed":
                entries.append(
                    (row["t"], "interviewer wrote into %s (%s)" % (row["path"], row.get("rule", "?")))
                )

    entries.sort(key=lambda e: e[0])
    lines = []
    for t, label in entries:
        stamp = "--:--" if t == float("inf") else fmt_t(t)
        lines.append("- %s  %s" % (stamp, label))
    return "\n".join(lines) or "(nothing to show)"


def _render_watchlist(section: Dict[str, Any], rows: List[Dict[str, Any]], pack: Pack) -> str:
    wanted = {p.id for p in section["patterns"]}
    hits = [h for h in watchlist_hits(rows, pack) if h["id"] in wanted]
    if not hits:
        return "(no watch-list matches)"
    lines = []
    for hit in hits:
        lines.append(
            "- %s  %s:%d  [%s]\n  wrote:   %s\n  instead: %s"
            % (fmt_t(hit["t"]), hit["path"], hit["line_no"], hit["id"], hit["line"], hit["suggest"] or "-")
        )
    return "\n".join(lines)


def _screen_before(rows: List[Dict[str, Any]], t: float, max_lines: int) -> Optional[str]:
    last = None
    for row in rows:
        if row["kind"] == events.FILE_SAVED and row["t"] <= t:
            last = row
    if last is None:
        return None
    tail = last["content"].splitlines()[-max_lines:]
    return "%s (as of %s):\n%s" % (last["path"], fmt_t(last["t"]), "\n".join("    " + l for l in tail))


def _render_gaps(section: Dict[str, Any], rows: List[Dict[str, Any]], pack: Pack) -> str:
    threshold = section["threshold_s"]
    activity = [r for r in rows if r["kind"] in events.ACTIVITY_KINDS]
    end = next((r for r in rows if r["kind"] == events.SESSION_END), None)
    anchors: List[Tuple[float, str]] = [(0.0, "session start")]
    for row in activity:
        anchors.append((row["t"], row["kind"]))
    if end is not None:
        anchors.append((end["t"], "session end"))

    lines = []
    for (t_prev, what_prev), (t_next, what_next) in zip(anchors, anchors[1:]):
        gap = t_next - t_prev
        if gap < threshold:
            continue
        screen = _screen_before(rows, t_prev, section["screen_lines"])
        lines.append(
            "- %s -> %s  (%d s, after %s, broken by %s)"
            % (fmt_t(t_prev), fmt_t(t_next), int(gap), what_prev, what_next)
        )
        if screen:
            lines.append("  on screen — " + screen.replace("\n", "\n  "))
        else:
            lines.append("  on screen — nothing saved yet")
    return "\n".join(lines) or "(no gaps over %d s)" % int(threshold)


def _render_spoken_vs_typed(section: Dict[str, Any], rows: List[Dict[str, Any]], pack: Pack) -> str:
    max_lines = section["diff_lines"]
    previous: Dict[str, List[str]] = {}
    lines: List[str] = []
    for row in rows:
        if row["kind"] == events.USER_MESSAGE:
            lines.append("- %s  SAID: %s" % (fmt_t(row["t"]), row["text"]))
        elif row["kind"] == events.INTERVIEWER_MESSAGE:
            lines.append("- %s  HEARD: %s" % (fmt_t(row["t"]), row["text"]))
        elif row["kind"] == events.RUN_EXECUTED:
            lines.append(
                "- %s  RAN: %s (exit %s)" % (fmt_t(row["t"]), row["cmd"], row["exit_status"])
            )
        elif row["kind"] == events.PAD_WRITE and row.get("rule") != "seed":
            body = "\n".join("      " + l for l in row.get("text", "").splitlines())
            lines.append("- %s  INTERVIEWER WROTE (%s):\n%s" % (fmt_t(row["t"]), row["path"], body))
        elif row["kind"] == events.FILE_SAVED:
            new = row["content"].splitlines()
            old = previous.get(row["path"], [])
            previous[row["path"]] = new
            delta = [
                d
                for d in difflib.unified_diff(old, new, lineterm="")
                if d and not d.startswith(("---", "+++", "@@"))
            ]
            delta = [d for d in delta if d[0] in "+-"]
            shown = delta[:max_lines]
            more = len(delta) - len(shown)
            body = "\n".join("      " + d for d in shown)
            if more > 0:
                body += "\n      [%d more changed lines]" % more
            lines.append("- %s  TYPED (%s):\n%s" % (fmt_t(row["t"]), row["path"], body or "      (no line changes)"))
    return "\n".join(lines) or "(nothing spoken or typed)"


def _render_openers(section: Dict[str, Any], rows: List[Dict[str, Any]], pack: Pack) -> str:
    lines = []
    last_conversational: Optional[str] = None
    for row in rows:
        if row["kind"] in _CONVERSATIONAL:
            last_conversational = row["kind"]
        elif row["kind"] == events.USER_MESSAGE:
            if last_conversational is not None:
                sentences = split_sentences(row["text"])
                opener = sentences[0] if sentences else row["text"]
                flags = [rx.pattern for rx in section["flag_patterns"] if rx.search(opener)]
                marker = ("  <- flagged: " + "; ".join(flags)) if flags else ""
                lines.append('- %s  "%s"%s' % (fmt_t(row["t"]), opener, marker))
            last_conversational = None
    return "\n".join(lines) or "(no answers given)"


def _render_messages(section: Dict[str, Any], rows: List[Dict[str, Any]], pack: Pack) -> str:
    lines = []
    for row in rows:
        if row["kind"] == events.USER_MESSAGE:
            lines.append("- %s  YOU: %s" % (fmt_t(row["t"]), row["text"]))
        elif row["kind"] == events.INTERVIEWER_MESSAGE:
            lines.append("- %s  INTERVIEWER: %s" % (fmt_t(row["t"]), row["text"]))
    return "\n".join(lines) or "(no messages)"


def _render_appendix(section: Dict[str, Any], rows: List[Dict[str, Any]], pack: Pack) -> str:
    presented = []
    for row in rows:
        if row["kind"] == events.TASK_PRESENTED:
            task = pack.task_by_id(row["task_id"])
            if task and task not in presented:
                presented.append(task)
    parts = []
    for task in presented:
        chunk = ["### %s (%s)" % (task["title"], task["id"])]
        for field in section["fields"]:
            value = task.get(field, "")
            if value:
                chunk.append("%s:\n%s" % (field, value))
        parts.append("\n\n".join(chunk))
    return "\n\n".join(parts) or "(no tasks presented)"


_RENDERERS = {
    "header": _render_header,
    "timeline": _render_timeline,
    "watchlist": _render_watchlist,
    "gaps": _render_gaps,
    "spoken_vs_typed": _render_spoken_vs_typed,
    "openers": _render_openers,
    "messages": _render_messages,
    "appendix": _render_appendix,
}


def build_report(rows: List[Dict[str, Any]]) -> str:
    pack = _find_pack(rows)
    parts = ["# %s" % pack.report_title, "\n".join(_meta_lines(rows, pack))]
    for i, section in enumerate(pack.sections):
        title = section.get("title") or section["type"]
        body = _RENDERERS[section["type"]](section, rows, pack)
        parts.append("## %d. %s\n\n%s" % (i + 1, title, body))
    return "\n\n".join(parts) + "\n"
